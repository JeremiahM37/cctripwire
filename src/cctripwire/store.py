"""SQLite state shared by every cctripwire process (hooks, gateway, daemon, CLI).

One file, WAL mode, short transactions. Cross-process coordination (concurrency
slots, spawn accounting) uses BEGIN IMMEDIATE so two gateways never both take the
last slot. Rows come back as plain dicts.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import Detection, Level, QuotaSample, TokenTotals

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS quota_samples (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, window TEXT NOT NULL, used_pct REAL NOT NULL,
  resets_at REAL, source TEXT NOT NULL, exact INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS qs_window_ts ON quota_samples(window, ts);
CREATE TABLE IF NOT EXISTS token_events (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, session_id TEXT, project TEXT, agent TEXT, model TEXT,
  input INTEGER, output INTEGER, cache_create INTEGER, cache_read INTEGER, weighted REAL,
  msg_key TEXT UNIQUE, invocation_id TEXT);
CREATE INDEX IF NOT EXISTS te_ts ON token_events(ts);
CREATE INDEX IF NOT EXISTS te_cover ON token_events(ts, project, agent, weighted);
CREATE INDEX IF NOT EXISTS te_session ON token_events(session_id, ts);
CREATE TABLE IF NOT EXISTS scan_offsets (path TEXT PRIMARY KEY, inode INTEGER, offset INTEGER, mtime REAL);
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY, project TEXT, cwd TEXT, agent TEXT, transcript_path TEXT,
  invocation_id TEXT, pid INTEGER, started REAL, last_seen REAL, ended REAL, status TEXT,
  stop_calls INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, session_id TEXT, agent_id TEXT, kind TEXT NOT NULL,
  tool TEXT, sig TEXT, ok INTEGER, detail TEXT);
CREATE INDEX IF NOT EXISTS ev_session_ts ON events(session_id, ts);
CREATE TABLE IF NOT EXISTS invocations (
  id TEXT PRIMARY KEY, parent_id TEXT, root_id TEXT, depth INTEGER, pid INTEGER, pgid INTEGER,
  project TEXT, agent TEXT, argv TEXT, prompt_hash TEXT, model TEXT, status TEXT NOT NULL,
  created REAL, started REAL, ended REAL, heartbeat REAL, exit_code INTEGER,
  tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, weighted REAL DEFAULT 0,
  cost_usd REAL, budget_weighted REAL, session_id TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS inv_status ON invocations(status);
CREATE INDEX IF NOT EXISTS inv_parent ON invocations(parent_id);
CREATE TABLE IF NOT EXISTS detections (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT, severity INTEGER, session_id TEXT,
  invocation_id TEXT, detail TEXT, advice TEXT, count INTEGER);
CREATE INDEX IF NOT EXISTS det_ts ON detections(ts);
CREATE TABLE IF NOT EXISTS interventions (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, action TEXT, target TEXT, level INTEGER,
  reason TEXT, policy TEXT);
CREATE INDEX IF NOT EXISTS int_ts ON interventions(ts);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, ts REAL);
"""

RUNNING_STATES = ("running", "suspended")


class Store:
    def __init__(self, path: str | os.PathLike, timeout: float = 15.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self.timeout = timeout
        self._local = threading.local()
        self.conn.executescript(SCHEMA)   # executescript manages its own transaction
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    # -- connection handling -------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None or getattr(self._local, "pid", None) != os.getpid():
            c = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None,
                                check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
            self._ensure_wal(c)
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
            self._local.pid = os.getpid()
        return c

    def _ensure_wal(self, c: sqlite3.Connection) -> None:
        """WAL persists in the file, so this only switches a new database. The switch needs an
        exclusive lock and can fail with SQLITE_BUSY without consulting the busy handler, so
        retry until the timeout when several processes create the database at once."""
        deadline = time.monotonic() + self.timeout
        delay = 0.01
        while True:
            try:
                if c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
                    return
                c.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) and "busy" not in str(e):
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.2)

    @contextmanager
    def tx(self, immediate: bool = False):
        c = self.conn
        if c.in_transaction:          # nested: join the outer transaction
            yield c
            return
        c.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def _rows(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]

    # -- quota ---------------------------------------------------------------
    def add_quota_sample(self, s: QuotaSample) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO quota_samples(ts,window,used_pct,resets_at,source,exact) VALUES (?,?,?,?,?,?)",
                      (s.ts, s.window, float(s.used_pct), s.resets_at, s.source, int(bool(s.exact))))

    def _sample(self, r: sqlite3.Row | dict | None) -> Optional[QuotaSample]:
        if r is None:
            return None
        return QuotaSample(ts=r["ts"], window=r["window"], used_pct=r["used_pct"], resets_at=r["resets_at"],
                           source=r["source"], exact=bool(r["exact"]))

    def latest_quota(self, window: str, source: str | None = None, exact: bool | None = None) -> Optional[QuotaSample]:
        sql, args = "SELECT * FROM quota_samples WHERE window=?", [window]
        if source is not None:
            sql += " AND source=?"; args.append(source)
        if exact is not None:
            sql += " AND exact=?"; args.append(int(exact))
        r = self.conn.execute(sql + " ORDER BY ts DESC, id DESC LIMIT 1", args).fetchone()
        return self._sample(r)

    def quota_samples(self, window: str, since: float, source: str | None = None,
                      exact: bool | None = None) -> list[QuotaSample]:
        sql, args = "SELECT * FROM quota_samples WHERE window=? AND ts>=?", [window, since]
        if source is not None:
            sql += " AND source=?"; args.append(source)
        if exact is not None:
            sql += " AND exact=?"; args.append(int(exact))
        return [self._sample(r) for r in self.conn.execute(sql + " ORDER BY ts", args).fetchall()]

    def windows_seen(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT window FROM quota_samples").fetchall()]

    # -- tokens --------------------------------------------------------------
    def add_token_event(self, *, ts: float, session_id: str | None, project: str | None, agent: str | None,
                        model: str | None, input: int, output: int, cache_create: int, cache_read: int,
                        weighted: float, msg_key: str | None, invocation_id: str | None = None) -> bool:
        """Insert one assistant-message usage record. Returns False if msg_key was already seen."""
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO token_events(ts,session_id,project,agent,model,input,output,cache_create,"
                "cache_read,weighted,msg_key,invocation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, session_id, project, agent, model, input, output, cache_create, cache_read, weighted,
                 msg_key, invocation_id))
            if cur.rowcount == 1:
                return True
            if invocation_id and msg_key:   # seen by another scanner first: attribute it
                c.execute("UPDATE token_events SET invocation_id=? WHERE msg_key=? AND invocation_id IS NULL",
                          (invocation_id, msg_key))
            return False

    def token_totals(self, since: float, until: float | None = None, *, project: str | None = None,
                     agent: str | None = None, session_id: str | None = None,
                     invocation_id: str | None = None) -> TokenTotals:
        sql = ("SELECT COALESCE(SUM(input),0), COALESCE(SUM(output),0), COALESCE(SUM(cache_create),0), "
               "COALESCE(SUM(cache_read),0), COALESCE(SUM(weighted),0), COUNT(*) FROM token_events WHERE ts>=?")
        args: list[Any] = [since]
        for col, val in (("project", project), ("agent", agent), ("session_id", session_id),
                         ("invocation_id", invocation_id)):
            if val is not None:
                sql += f" AND {col}=?"; args.append(val)
        if until is not None:
            sql += " AND ts<?"; args.append(until)
        r = self.conn.execute(sql, args).fetchone()
        return TokenTotals(input=r[0], output=r[1], cache_create=r[2], cache_read=r[3], weighted=r[4], messages=r[5])

    def token_series(self, since: float, bucket_s: int = 300) -> list[tuple[float, float]]:
        rows = self.conn.execute(
            "SELECT CAST(ts/? AS INTEGER)*? AS b, SUM(weighted) FROM token_events WHERE ts>=? GROUP BY b ORDER BY b",
            (bucket_s, bucket_s, since)).fetchall()
        return [(float(r[0]), float(r[1] or 0)) for r in rows]

    def weighted_by(self, field: str, since: float) -> dict[str, float]:
        if field not in ("project", "agent", "session_id", "model", "invocation_id"):
            raise ValueError(field)
        rows = self.conn.execute(
            f"SELECT {field}, SUM(weighted) FROM token_events WHERE ts>=? GROUP BY {field} ORDER BY 2 DESC",
            (since,)).fetchall()
        return {(r[0] or "?"): float(r[1] or 0) for r in rows}

    def weighted_by_project_agent(self, since: float) -> tuple[dict[str, float], dict[str, float]]:
        """Per-project and per-agent weighted totals in one covering-index scan."""
        proj: dict[str, float] = {}
        agent: dict[str, float] = {}
        for p, a, w in self.conn.execute("SELECT project, agent, SUM(weighted) FROM token_events "
                                         "WHERE ts>=? GROUP BY project, agent", (since,)):
            w = float(w or 0)
            proj[p or "?"] = proj.get(p or "?", 0.0) + w
            agent[a or "?"] = agent.get(a or "?", 0.0) + w
        return proj, agent

    def get_offset(self, path: str) -> tuple[int, int]:
        r = self.conn.execute("SELECT inode, offset FROM scan_offsets WHERE path=?", (path,)).fetchone()
        return (r[0], r[1]) if r else (0, 0)

    def set_offset(self, path: str, inode: int, offset: int, mtime: float = 0.0) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO scan_offsets VALUES (?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                      "inode=excluded.inode, offset=excluded.offset, mtime=excluded.mtime",
                      (path, inode, offset, mtime))

    # -- sessions ------------------------------------------------------------
    def upsert_session(self, session_id: str, **fields: Any) -> None:
        fields.setdefault("last_seen", time.time())
        cols = ["session_id"] + list(fields)
        vals = [session_id] + list(fields.values())
        upd = ", ".join(f"{k}=COALESCE(excluded.{k}, {k})" for k in fields)
        with self.tx() as c:
            c.execute(f"INSERT INTO sessions({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
                      f"ON CONFLICT(session_id) DO UPDATE SET {upd}", vals)
            if "started" not in fields:
                c.execute("UPDATE sessions SET started=COALESCE(started, last_seen) WHERE session_id=?",
                          (session_id,))

    def get_session(self, session_id: str) -> Optional[dict]:
        r = self.conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return dict(r) if r else None

    def bump_session_counter(self, session_id: str, column: str = "stop_calls") -> int:
        if column not in ("stop_calls",):
            raise ValueError(column)
        with self.tx(immediate=True) as c:
            c.execute(f"UPDATE sessions SET {column}=COALESCE({column},0)+1 WHERE session_id=?", (session_id,))
            r = c.execute(f"SELECT {column} FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return int(r[0]) if r else 0

    def reset_session_counter(self, session_id: str, column: str = "stop_calls") -> None:
        if column not in ("stop_calls",):
            raise ValueError(column)
        with self.tx() as c:
            c.execute(f"UPDATE sessions SET {column}=0 WHERE session_id=? AND {column}>0", (session_id,))

    def active_sessions(self, since: float) -> list[dict]:
        return self._rows("SELECT * FROM sessions WHERE last_seen>=? AND ended IS NULL ORDER BY last_seen DESC",
                          (since,))

    # -- hook events -----------------------------------------------------------
    def add_event(self, session_id: str | None, kind: str, *, tool: str | None = None, sig: str | None = None,
                  ok: bool | None = None, detail: str | None = None, ts: float | None = None,
                  agent_id: str | None = None) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO events(ts,session_id,agent_id,kind,tool,sig,ok,detail) VALUES (?,?,?,?,?,?,?,?)",
                            (ts or time.time(), session_id, agent_id, kind, tool, sig,
                             None if ok is None else int(ok), (detail or "")[:500]))
            return int(cur.lastrowid)

    def recent_events(self, session_id: str | None, since: float, kinds: Iterable[str] | None = None,
                      limit: int = 500) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE ts>=?", [since]
        if session_id is not None:
            sql += " AND session_id=?"; args.append(session_id)
        if kinds:
            ks = list(kinds)
            sql += f" AND kind IN ({','.join('?' * len(ks))})"; args += ks
        rows = self._rows(sql + " ORDER BY ts DESC, id DESC LIMIT ?", args + [limit])
        rows.reverse()   # oldest first
        return rows

    # -- invocations (gateway) -------------------------------------------------
    def create_invocation(self, inv: dict) -> None:
        cols = list(inv)
        with self.tx() as c:
            c.execute(f"INSERT INTO invocations({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                      [inv[k] for k in cols])

    def update_invocation(self, inv_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE invocations SET {sets} WHERE id=?", list(fields.values()) + [inv_id])

    def get_invocation(self, inv_id: str) -> Optional[dict]:
        r = self.conn.execute("SELECT * FROM invocations WHERE id=?", (inv_id,)).fetchone()
        return dict(r) if r else None

    def invocations(self, status: str | Iterable[str] | None = None, since: float | None = None,
                    limit: int = 200, parent_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM invocations WHERE 1=1", []
        if status is not None:
            sts = [status] if isinstance(status, str) else list(status)
            sql += f" AND status IN ({','.join('?' * len(sts))})"; args += sts
        if since is not None:
            sql += " AND created>=?"; args.append(since)
        if parent_id is not None:
            sql += " AND parent_id=?"; args.append(parent_id)
        return self._rows(sql + " ORDER BY created DESC LIMIT ?", args + [limit])

    # -- detections / interventions -------------------------------------------
    def add_detection(self, d: Detection, ts: float | None = None) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO detections(ts,kind,severity,session_id,invocation_id,detail,advice,count) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (ts or time.time(), d.kind, int(d.severity), d.session_id, d.invocation_id,
                             d.detail[:500], d.advice[:300], d.count))
            return int(cur.lastrowid)

    def detections(self, since: float = 0, limit: int = 100, session_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM detections WHERE ts>=?", [since]
        if session_id is not None:
            sql += " AND session_id=?"; args.append(session_id)
        return self._rows(sql + " ORDER BY ts DESC LIMIT ?", args + [limit])

    def add_intervention(self, action: str, target: str, reason: str, *, level: Level | int = 0,
                         policy: str | None = None, ts: float | None = None) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO interventions(ts,action,target,level,reason,policy) VALUES (?,?,?,?,?,?)",
                            (ts or time.time(), action, target, int(level), reason[:500], policy))
            return int(cur.lastrowid)

    def interventions(self, since: float = 0, limit: int = 100) -> list[dict]:
        return self._rows("SELECT * FROM interventions WHERE ts>=? ORDER BY ts DESC LIMIT ?", (since, limit))

    # -- kv --------------------------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        r = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def kv_set(self, key: str, value: Any) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO kv VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                      (key, json.dumps(value), time.time()))

    # -- maintenance -------------------------------------------------------------
    def prune(self, older_than_s: float = 35 * 86400) -> None:
        cut = time.time() - older_than_s
        with self.tx() as c:
            for t in ("quota_samples", "token_events", "events", "detections", "interventions"):
                c.execute(f"DELETE FROM {t} WHERE ts<?", (cut,))
            # Only finished work is history; anything live or awaiting a control request stays.
            c.execute("DELETE FROM invocations WHERE created<? AND status IN "
                      "('done','failed','killed','denied','timeout','crashed')", (cut,))


def open_store(cfg) -> Store:
    return Store(cfg.db_path)

"""Loop and anomaly detection for agent sessions.

Every method records raw events in the store and returns the detections that hold
*now*. Enforcement (hooks deny, gateway refuse) acts on the returned list, so a
condition that still holds keeps being reported. What the store keeps is deduped:
one row per (kind, session) per DEDUPE_S, so the dashboard is not flooded.

Progress rule: a successful Write/Edit/MultiEdit/NotebookEdit resets the retry and
failure counts of the session. Re-reading a file after editing it is therefore not
a retry. Prompts shorter than MIN_PROMPT_CHARS after normalisation never count.
"""
from __future__ import annotations

import os
import statistics
from typing import Callable, Optional, Union

from . import util
from .config import Config
from .models import Detection, Level
from .store import Store

WRITE_TOOLS = ("Edit", "MultiEdit", "NotebookEdit", "Write")
SPAWN_TOOLS = ("Agent", "Task")
DEDUPE_S = 300            # same (kind, session) recorded at most once per this many seconds
COMPACT_DEDUPE_S = 60     # PreCompact and SessionStart(source=compact) both report one compaction
SPAWN_WINDOW_S = 60
SPIKE_BUCKET_S = 600
SPIKE_BASELINE_S = 3600
MIN_PROMPT_CHARS = 20
ADVICE_MAX = 200

Clock = Union[Callable[[], float], float, None]


def _sig(tool: str, tool_input) -> str:
    return util.short_hash([tool, tool_input])


def _depth() -> int:
    try:
        return int(os.environ.get(util.ENV_DEPTH, "0") or 0)
    except ValueError:
        return 0


def _det(kind: str, severity: Level, session_id: Optional[str], detail: str, advice: str,
         count: int = 1, invocation_id: Optional[str] = None) -> Detection:
    return Detection(kind=kind, severity=severity, session_id=session_id, detail=detail[:500],
                     advice=" ".join(advice.split())[:ADVICE_MAX], count=count,
                     invocation_id=invocation_id)


class LoopDetector:
    def __init__(self, store: Store, cfg: Config, now: Clock = None):
        self.store = store
        self.cfg = cfg
        self._clock = now

    # -- plumbing --------------------------------------------------------------
    def now(self) -> float:
        c = self._clock
        if c is None:
            return util.now()
        return float(c() if callable(c) else c)

    def _t(self, key: str, default):
        return self.cfg.get(f"loops.{key}", default)

    def _event(self, session_id, kind: str, now: float, *, tool=None, sig=None, ok=None, detail=None) -> None:
        self.store.add_event(session_id, kind, tool=tool, sig=sig, ok=ok, detail=detail, ts=now)

    def _count(self, session_id, kinds, since: float, *, after_id: int = 0, sig=None, tools=None) -> int:
        sql = (f"SELECT COUNT(*) FROM events WHERE session_id IS ? AND ts>=? AND id>? "
               f"AND kind IN ({','.join('?' * len(kinds))})")
        args: list = [session_id, since, after_id, *kinds]
        if sig is not None:
            sql += " AND sig=?"
            args.append(sig)
        if tools:
            sql += f" AND tool IN ({','.join('?' * len(tools))})"
            args += list(tools)
        return int(self.store.conn.execute(sql, args).fetchone()[0])

    def _progress_id(self, session_id, since: float) -> int:
        """Id of the last successful write in the window (0 = none). Counts start after it."""
        sql = (f"SELECT COALESCE(MAX(id),0) FROM events WHERE session_id IS ? AND ts>=? "
               f"AND kind='tool_ok' AND tool IN ({','.join('?' * len(WRITE_TOOLS))})")
        return int(self.store.conn.execute(sql, (session_id, since, *WRITE_TOOLS)).fetchone()[0])

    def _call_state(self, session_id, sig: str, now: float) -> tuple[int, int, str]:
        """(tool_pre count, tool_fail count, last error) for one exact call since the last write."""
        since = now - float(self._t("window_s", 900))
        pid = self._progress_id(session_id, since)
        pre = self._count(session_id, ("tool_pre",), since, after_id=pid, sig=sig)
        fails = self._count(session_id, ("tool_fail",), since, after_id=pid, sig=sig)
        row = self.store.conn.execute(
            "SELECT detail FROM events WHERE session_id IS ? AND kind='tool_fail' AND sig=? AND id>? AND ts>=? "
            "ORDER BY id DESC LIMIT 1", (session_id, sig, pid, since)).fetchone()
        return pre, fails, (row[0] if row else "") or ""

    def _progress_dets(self, session_id, now: float, extra: int = 0) -> list[Detection]:
        since = now - float(self._t("window_s", 900))
        pid = self._progress_id(session_id, since)
        n = self._count(session_id, ("tool_pre",), since, after_id=pid) + extra
        if n < int(self._t("no_progress_calls", 40)):
            return []
        return [_det("no_progress", Level.WARN, session_id,
                     f"{n} tool calls with no file write or edit",
                     f"{n} tool calls since the last file write or edit. If stuck, write a short status "
                     "and change approach.", count=n)]

    def _record(self, det: Detection, now: float) -> None:
        row = self.store.conn.execute(
            "SELECT 1 FROM detections WHERE kind=? AND session_id IS ? AND ts>=? LIMIT 1",
            (det.kind, det.session_id, now - DEDUPE_S)).fetchone()
        if row is None:
            self.store.add_detection(det, ts=now)

    def _emit(self, dets: list[Detection], now: float) -> list[Detection]:
        for d in dets:
            self._record(d, now)
        return dets

    def _failure_det(self, session_id, tool: str, n: int, err: str) -> Detection:
        err = " ".join((err or "no error text").split())[:80]
        return _det("repeated_tool_failure", Level.THROTTLE, session_id,
                    f"{tool} failed {n}x: {err}",
                    f"This exact {tool[:40]} call failed {n}×: {err}. Change approach instead of retrying.",
                    count=n)

    # -- public API --------------------------------------------------------------
    def on_prompt(self, session_id, prompt: str) -> list[Detection]:
        norm = util.normalize_prompt(prompt or "")
        if len(norm) < MIN_PROMPT_CHARS:
            return []
        now = self.now()
        sig = util.short_hash(norm)
        since = now - float(self._t("window_s", 900))
        n = self._count(session_id, ("prompt",), since, sig=sig) + 1
        self._event(session_id, "prompt", now, sig=sig)
        if n < int(self._t("identical_prompt_threshold", 3)):
            return []
        return self._emit([_det(
            "identical_prompt", Level.THROTTLE, session_id,
            f"identical prompt sent {n} times", 
            f"This same request was sent {n} times in 15 min. Change the request or check the earlier answer "
            "instead of repeating it.", count=n)], now)

    def on_tool_pre(self, session_id, tool_name: str, tool_input) -> list[Detection]:
        now = self.now()
        tool = str(tool_name or "?")
        sig = _sig(tool, tool_input)
        pre, fails, err = self._call_state(session_id, sig, now)
        retries = pre + 1
        dets: list[Detection] = []
        if fails >= int(self._t("tool_failure_threshold", 4)):
            dets.append(self._failure_det(session_id, tool, fails, err))
        rt = int(self._t("retry_threshold", 6))
        if retries >= rt:
            dets.append(_det(
                "excessive_retries", Level.THROTTLE, session_id,
                f"{tool} identical call {retries} times",
                f"This exact {tool[:40]} call has run {retries}× with no file write in between. Reuse the "
                "result you already have instead of repeating it.", count=retries))
        if tool in SPAWN_TOOLS:
            spawns = self._count(session_id, ("tool_pre",), now - SPAWN_WINDOW_S, tools=SPAWN_TOOLS) + 1
            if spawns >= int(self._t("parallel_spawn_threshold", 5)):
                dets.append(_det(
                    "parallel_spawn", Level.THROTTLE, session_id,
                    f"{spawns} spawns within {SPAWN_WINDOW_S}s",
                    f"{spawns} subagents started within {SPAWN_WINDOW_S}s. Run them one at a time.",
                    count=spawns))
            depth, rd = _depth(), int(self._t("recursion_depth", 3))
            if depth >= rd:
                dets.append(_det(
                    "recursive_spawn", Level.THROTTLE, session_id,
                    f"spawn depth {depth} reached limit {rd}",
                    f"Nested spawn depth {depth} reached the limit {rd}. Do the remaining work in this "
                    "process instead of spawning.", count=depth))
        dets += self._progress_dets(session_id, now, extra=1)
        self._event(session_id, "tool_pre", now, tool=tool, sig=sig)
        return self._emit(dets, now)

    def on_tool_result(self, session_id, tool_name: str, tool_input, ok: bool,
                       error: Optional[str] = None) -> list[Detection]:
        now = self.now()
        tool = str(tool_name or "?")
        sig = _sig(tool, tool_input)
        dets: list[Detection] = []
        if ok:
            self._event(session_id, "tool_ok", now, tool=tool, sig=sig, ok=True)
        else:
            self._event(session_id, "tool_fail", now, tool=tool, sig=sig, ok=False,
                        detail=(error or "")[:200])
            _, fails, err = self._call_state(session_id, sig, now)
            if fails >= int(self._t("tool_failure_threshold", 4)):
                dets.append(self._failure_det(session_id, tool, fails, err))
        dets += self._progress_dets(session_id, now)
        return self._emit(dets, now)

    def on_compact(self, session_id) -> list[Detection]:
        now = self.now()
        last = self.store.conn.execute(
            "SELECT MAX(ts) FROM events WHERE session_id IS ? AND kind='compact'", (session_id,)).fetchone()[0]
        if last is not None and now - last < COMPACT_DEDUPE_S:
            return []
        self._event(session_id, "compact", now)
        win = float(self._t("window_s", 900))
        n = self._count(session_id, ("compact",), now - win)
        if n < int(self._t("context_rebuild_threshold", 3)):
            return []
        return self._emit([_det(
            "context_rebuild", Level.WARN, session_id,
            f"context compacted {n} times in {int(win // 60)} min",
            f"Context was compacted {n}× in {int(win // 60)} min. The task is probably too large for one "
            "session: checkpoint progress and split it.", count=n)], now)

    def check_token_spike(self, session_id=None) -> list[Detection]:
        now = self.now()
        conn = self.store.conn
        recent = self.store.token_totals(now - SPIKE_BUCKET_S, session_id=session_id).weighted
        sql = ("SELECT CAST(ts/? AS INTEGER) AS b, SUM(weighted) FROM token_events WHERE ts>=? AND ts<?")
        args: list = [SPIKE_BUCKET_S, now - SPIKE_BASELINE_S, now - SPIKE_BUCKET_S]
        if session_id is not None:
            sql += " AND session_id=?"
            args.append(session_id)
        buckets = {int(r[0]): float(r[1] or 0) for r in conn.execute(sql + " GROUP BY b", args)}
        b0 = int((now - SPIKE_BASELINE_S) // SPIKE_BUCKET_S)
        b1 = int((now - SPIKE_BUCKET_S) // SPIKE_BUCKET_S)
        base = statistics.median([buckets.get(b, 0.0) for b in range(b0, b1)] or [0.0])
        rate = recent / (SPIKE_BUCKET_S / 60)
        base_rate = base / (SPIKE_BUCKET_S / 60)
        factor = float(self._t("spike_factor", 4.0))
        if recent < float(self._t("spike_min_weighted", 50000)) or rate < factor * max(base_rate, 1.0):
            return []
        ratio = rate / max(base_rate, 1.0)
        return self._emit([_det(
            "token_spike", Level.WARN, session_id,
            f"{rate:.0f} weighted tokens/min, {ratio:.1f}x the trailing median",
            f"Token use is {ratio:.0f}× the recent median over the last 10 min. Batch remaining work and "
            "avoid re-reading large files.")], now)

    def check_invocation(self, prompt_hash: str, parent_id: Optional[str], depth: int) -> list[Detection]:
        """Gateway pre-spawn check for a `claude -p` invocation about to start."""
        now = self.now()
        win = float(self.cfg.get("gateway.duplicate_window_s", 600))
        prior = int(self.store.conn.execute(
            "SELECT COUNT(*) FROM invocations WHERE prompt_hash=? AND created>=? AND status<>'denied'",
            (prompt_hash, now - win)).fetchone()[0])
        dets: list[Detection] = []
        if prior >= int(self.cfg.get("gateway.max_duplicates", 3)):
            n = prior + 1
            dets.append(_det(
                "repeated_claude_p", Level.THROTTLE, None,
                f"identical claude -p prompt start {n} in {int(win)}s (parent {parent_id})",
                f"Identical claude -p prompt started {n}× in {int(win // 60)} min. Reuse the earlier result "
                "instead of starting another run.", count=n, invocation_id=parent_id))
        rd = int(self._t("recursion_depth", 3))
        if depth >= rd:
            dets.append(_det(
                "recursive_spawn", Level.THROTTLE, None,
                f"invocation depth {depth} reached limit {rd} (parent {parent_id})",
                f"Nested claude -p depth {depth} reached the limit {rd}. Do the work in this process "
                "instead of spawning.", count=depth, invocation_id=parent_id))
        return self._emit(dets, now)

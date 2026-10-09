"""Background daemon: scan, decide, record level changes and enforce on gateway runs.

One tick runs the steps in `_steps()` in order. Each step is isolated: an exception
is logged to `state_dir/daemon.log` and recorded in the tick report, and the next
step still runs. Sibling modules (quota, loops, procs, dashboard) are imported
lazily, so a missing one is reported as "unavailable" rather than failing the tick.
"""
from __future__ import annotations

import importlib
import os
import signal
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

from . import breaker
from . import interventions as iv
from .config import Config
from .models import Detection, Level, QuotaSample
from .store import Store, open_store
from .util import now as _now, pid_alive

LAST_LEVEL_KEY = "daemon:last_level"
HEARTBEAT_KEY = "daemon:heartbeat"
PRUNE_KEY = "daemon:last_prune"
BYPASS_SEEN_KEY = "daemon:bypass_seen"
BYPASS_PAUSED_KEY = "daemon:bypass_paused"
HOUSEKEEPING_EVERY_S = 3600
# Rows that still belong to a gateway process: if its pid is dead they are crashed.
LIVE_STATES = ("running", "suspended", "queued", "suspend_requested", "resume_requested", "kill_requested")


class _Unavailable(Exception):
    """A sibling module is not installed (yet)."""


class _Skip(Exception):
    """A step has nothing to do this tick (disabled, or its input is missing)."""


def _optional(modname: str):
    """Import `cctripwire.<modname>`; a missing cctripwire module becomes _Unavailable."""
    try:
        return importlib.import_module(f"cctripwire.{modname}")
    except ImportError as e:
        if (e.name or "").startswith("cctripwire."):
            raise _Unavailable(f"{modname} not available") from e
        raise


def _log(cfg: Config, msg: str) -> None:
    try:
        d = Path(cfg.state_dir)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "daemon.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:  # noqa: BLE001 - logging must not break the daemon
        pass


def _need_decision(ctx: dict):
    if ctx.get("dec") is None:
        raise _Skip("no decision this tick")
    return ctx["dec"]


def _created(row: dict) -> float:
    return float(row.get("created") or 0)


# -- process-group helpers -----------------------------------------------------

def _group_alive(pgid: int) -> bool:
    """True if any non-zombie process is in process group `pgid` (Linux /proc)."""
    proc = Path("/proc")
    if not proc.is_dir():
        try:
            os.killpg(pgid, 0)
            return True
        except OSError:
            return False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_bytes().rsplit(b")", 1)[1].split()
        except (OSError, IndexError):
            continue
        # fields[0] = state, fields[2] = process group
        if len(fields) > 2 and int(fields[2]) == pgid and fields[0] != b"Z":
            return True
    return False


def _kill_group(cfg: Config, pgid) -> bool:
    """SIGTERM a leftover process group, then SIGKILL after gateway.kill_grace_s."""
    try:
        pgid = int(pgid or 0)
    except (TypeError, ValueError):
        return False
    if pgid <= 1 or pgid == os.getpgrp():
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + float(cfg.get("gateway.kill_grace_s", 10))
    while _group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    return True


def _store_sample(store: Store, s: QuotaSample) -> None:
    """Store a sample unless the same reading is already the newest for its window."""
    last = store.latest_quota(s.window, source=s.source)
    if last is not None and last.ts == s.ts and last.used_pct == s.used_pct:
        return
    store.add_quota_sample(s)


# -- steps ----------------------------------------------------------------------

def _step_scan(ctx: dict) -> None:
    cfg, store = ctx["cfg"], ctx["store"]
    transcripts = _optional("quota.transcripts")
    if not cfg.get("sources.transcripts", True):
        raise _Skip("transcripts disabled")
    transcripts.scan_all(store, cfg)


def _step_oauth(ctx: dict) -> None:
    cfg, store = ctx["cfg"], ctx["store"]
    if not cfg.get("sources.oauth", False):
        raise _Skip("oauth disabled")
    oauth = _optional("quota.oauth")
    for s in oauth.poll(store, cfg, now=ctx["now"]) or []:
        _store_sample(store, s)


def _step_usage_url(ctx: dict) -> None:
    cfg, store = ctx["cfg"], ctx["store"]
    if not cfg.get("sources.usage_url"):
        raise _Skip("usage_url not set")
    usage_url = _optional("quota.usage_url")
    for s in usage_url.poll(store, cfg, now=ctx["now"]) or []:
        _store_sample(store, s)


def _step_decide(ctx: dict) -> None:
    engine = _optional("engine")
    snap, dec = engine.decide(ctx["store"], ctx["cfg"], now=ctx["now"], max_age=0)
    ctx["snap"], ctx["dec"] = snap, dec


def _step_levels(ctx: dict) -> None:
    dec = _need_decision(ctx)
    store, cfg = ctx["store"], ctx["cfg"]
    cur = str(dec.level)
    last = store.kv_get(LAST_LEVEL_KEY) or str(Level.OK)
    if cur == last:
        return
    reasons = "; ".join(dec.reasons) or "no rule fired"
    store.kv_set(LAST_LEVEL_KEY, cur)
    store.add_intervention("level_change", "global", f"{last} -> {cur}: {reasons}", level=dec.level,
                           policy=",".join(dec.rules) or None)
    body = f"{iv.status_line(ctx['snap'], dec)} | {reasons}"[:iv.MSG_LIMIT]
    iv.notify(cfg, f"cctripwire: level {cur.upper()}", body)


def _step_reap(ctx: dict) -> None:
    store, cfg, t = ctx["store"], ctx["cfg"], ctx["now"]
    for row in store.invocations(status=LIVE_STATES, limit=10000):
        pid = row.get("pid")
        if not pid or pid_alive(pid):
            continue
        reason = f"gateway pid {pid} exited while {row['status']}"
        with store.tx(immediate=True):
            cur = store.get_invocation(row["id"])
            if cur is None or cur["status"] not in LIVE_STATES:
                continue    # the gateway settled it between our read and this write
            store.update_invocation(row["id"], status="crashed", ended=t, reason=reason)
            store.add_intervention("crash", row["id"], reason)
        if _kill_group(cfg, row.get("pgid")):
            _log(cfg, f"crashed {row['id']}: killed leftover group {row.get('pgid')}")


def _checkpoint(ctx: dict, inv: dict, reason: str) -> None:
    store, cfg = ctx["store"], ctx["cfg"]
    rec = dict(inv)
    if rec.get("session_id"):
        sess = store.get_session(rec["session_id"])
        if sess:
            rec["cwd"] = sess.get("cwd")
    path = iv.write_checkpoint(cfg, rec, reason)
    store.add_intervention("checkpoint", inv["id"], f"checkpoint {path.name}: {reason}", level=ctx["dec"].level)


def _step_enforce(ctx: dict) -> None:
    dec = _need_decision(ctx)
    store, cfg = ctx["store"], ctx["cfg"]
    running = sorted(store.invocations(status="running", limit=10000), key=_created)

    if dec.level >= Level.STOP:
        pause = bool(cfg.get("daemon.pause_on_stop", True))
        reason = f"level STOP: {'; '.join(dec.reasons) or 'limit reached'}"
        for inv in running:
            try:
                _checkpoint(ctx, inv, reason)
            except Exception as e:  # noqa: BLE001 - still stop the run without its checkpoint
                _log(cfg, f"checkpoint failed for {inv['id']}: {e}")
            action = "suspend" if pause else "kill"
            iv.request(store, inv["id"], action, reason=reason, level=dec.level)
        return

    cap = dec.max_concurrency if dec.max_concurrency is not None else int(cfg.get("gateway.max_concurrency", 4))
    extras = running[cap:] if len(running) > cap else []
    for inv in extras:
        iv.request(store, inv["id"], "suspend",
                   reason=f"throttle: {len(running)} running > cap {cap}", level=dec.level)

    if not cfg.get("daemon.auto_resume", True) or breaker.tripped(store):
        return
    still_running = len(running) - len(extras)
    resume_pending = len(store.invocations(status="resume_requested", limit=10000))
    free = cap - still_running - resume_pending
    if free <= 0:
        return
    suspended = sorted(store.invocations(status="suspended", limit=10000), key=_created)
    for inv in suspended[:free]:
        iv.request(store, inv["id"], "resume", reason="level below STOP", level=dec.level)


def _step_bypass(ctx: dict) -> None:
    procs = _optional("procs")
    store, cfg, dec = ctx["store"], ctx["cfg"], ctx["dec"]
    found = list(procs.bypassers(procs.list_claude_processes()) or [])

    prev = store.kv_get(BYPASS_SEEN_KEY) or {}
    seen: dict = {}
    for p in found:
        key = str(p.pid)
        start = getattr(p, "start_time", None)
        seen[key] = start
        if key in prev and prev[key] == start:
            continue
        store.add_detection(Detection(
            kind="gateway_bypass", severity=Level.WARN, session_id=None,
            detail=f"claude pid {p.pid} runs outside the gateway",
            advice="This claude process is not routed through cctripwire run. Stop it or launch it via the gateway.",
            count=1))
    store.kv_set(BYPASS_SEEN_KEY, seen)

    paused = [int(x) for x in (store.kv_get(BYPASS_PAUSED_KEY) or [])]
    if dec is None:
        return
    if dec.level >= Level.STOP and cfg.get("daemon.enforce_bypass", False):
        for p in found:
            if p.pid in paused:
                continue
            try:
                os.kill(p.pid, signal.SIGSTOP)
            except OSError:
                continue
            paused.append(p.pid)
            store.add_intervention("suspend", f"pid:{p.pid}", "bypass at STOP: SIGSTOP", level=dec.level)
        store.kv_set(BYPASS_PAUSED_KEY, paused)
    elif paused and dec.level < Level.STOP and not breaker.tripped(store):
        for pid in paused:
            try:
                os.kill(pid, signal.SIGCONT)
            except OSError:
                pass
            store.add_intervention("resume", f"pid:{pid}", "bypass recovered: SIGCONT", level=dec.level)
        store.kv_set(BYPASS_PAUSED_KEY, [])


def _step_breaker(ctx: dict) -> None:
    """Trip on a quota surge or a pile of bypassing claude -p processes; while tripped,
    pause automated runs. Interactive sessions are never touched. Resuming is left to
    the enforce and bypass steps, which wait until the breaker is reset or cools off."""
    store, cfg, t = ctx["store"], ctx["cfg"], ctx["now"]
    if not breaker.enabled(cfg):
        raise _Skip("breaker disabled")
    if breaker.lifted(store, t):
        raise _Skip("breaker lifted by user")
    rec = breaker.tripped(store, t)
    found = None
    if rec is None:
        why = breaker.surge(store, cfg, t)
        limit = int(cfg.get("daemon.bypass_storm_max", 0) or 0)
        if why is None and limit:
            procs = _optional("procs")
            found = list(procs.bypassers(procs.list_claude_processes()) or [])
            if len(found) >= limit:
                why = f"{len(found)} claude -p processes running outside the gateway"
        if why is None:
            return
        rec = breaker.trip(store, cfg, why, t)
        if rec.get("observed"):
            if rec.get("new"):
                iv.notify(cfg, "cctripwire: breaker WOULD trip (observe mode)", why)
            raise _Skip("observe: " + why)
        iv.notify(cfg, "cctripwire: runaway breaker tripped", why)
    if not cfg.get("gateway.storm_pause_running", True):
        return
    reason = f"breaker: {rec.get('reason')}"
    for inv in store.invocations(status="running", limit=10000):
        iv.request(store, inv["id"], "suspend", reason=reason, level=Level.STOP)
    if int(cfg.get("daemon.bypass_storm_max", 0) or 0):
        procs = _optional("procs")
        if found is None:
            found = list(procs.bypassers(procs.list_claude_processes()) or [])
        paused = [int(x) for x in (store.kv_get(BYPASS_PAUSED_KEY) or [])]
        for p in found:
            if p.pid in paused:
                continue
            try:
                os.kill(p.pid, signal.SIGSTOP)
            except OSError:
                continue
            paused.append(p.pid)
            store.add_intervention("suspend", f"pid:{p.pid}", reason, level=Level.STOP)
        store.kv_set(BYPASS_PAUSED_KEY, paused)


def _step_loops(ctx: dict) -> None:
    loops = _optional("loops")
    # LoopDetector records its own detections; the return value is not needed here.
    loops.LoopDetector(ctx["store"], ctx["cfg"], now=ctx["now"]).check_token_spike(None)


def _step_prune(ctx: dict) -> None:
    store, t = ctx["store"], ctx["now"]
    last = float(store.kv_get(PRUNE_KEY) or 0)
    if t - last < HOUSEKEEPING_EVERY_S:
        raise _Skip("not due")
    store.prune()
    store.kv_set(PRUNE_KEY, t)


def _step_heartbeat(ctx: dict) -> None:
    dec = ctx.get("dec")
    ctx["store"].kv_set(HEARTBEAT_KEY, {
        "ts": ctx["now"], "pid": os.getpid(),
        "level": str(dec.level) if dec is not None else None,
        "steps": dict(ctx["report"]),
    })


def _steps() -> list[tuple[str, Callable[[dict], None]]]:
    # Resolved at call time so tests can replace individual steps.
    return [
        ("scan", _step_scan),
        ("oauth", _step_oauth),
        ("usage_url", _step_usage_url),
        ("decide", _step_decide),
        ("levels", _step_levels),
        ("reap", _step_reap),
        ("enforce", _step_enforce),
        ("bypass", _step_bypass),
        ("breaker", _step_breaker),
        ("loops", _step_loops),
        ("prune", _step_prune),
        ("heartbeat", _step_heartbeat),
    ]


# -- driver ---------------------------------------------------------------------

def tick(cfg: Config, store: Store | None = None, now: float | None = None) -> dict[str, str]:
    """One pass over every step. Never raises. Returns step name -> status."""
    report: dict[str, str] = {}
    t = _now() if now is None else float(now)
    try:
        if store is None:
            store = open_store(cfg)
    except Exception as e:  # noqa: BLE001
        _log(cfg, f"cannot open store: {e}")
        report["store"] = f"error: {e}"
        return report
    ctx = {"cfg": cfg, "store": store, "now": t, "dec": None, "snap": None, "report": report}
    for name, fn in _steps():
        try:
            fn(ctx)
            report[name] = "ok"
        except _Unavailable as e:
            report[name] = f"unavailable: {e}"
        except _Skip as e:
            report[name] = f"skipped: {e}"
        except Exception as e:  # noqa: BLE001 - one broken step must not stop the rest
            report[name] = f"error: {type(e).__name__}: {e}"[:200]
            _log(cfg, f"step {name} failed: {traceback.format_exc(limit=3)}")
    return report


def _start_dashboard(cfg: Config) -> None:
    try:
        dashboard = importlib.import_module("cctripwire.dashboard")
    except ImportError as e:
        _log(cfg, f"dashboard not started: {e}")
        return
    host = cfg.get("dashboard.host", "127.0.0.1")
    port = int(cfg.get("dashboard.port", 8377))

    def _serve() -> None:
        try:
            dashboard.serve(cfg, Store(cfg.db_path), host, port)
        except Exception as e:  # noqa: BLE001
            _log(cfg, f"dashboard stopped: {e}")

    threading.Thread(target=_serve, name="cctripwire-dashboard", daemon=True).start()


def run(cfg: Config, once: bool = False, stop_event: threading.Event | None = None, *,
        dashboard: bool = False, store: Store | None = None):
    """Run the daemon loop, or one tick when `once` (returns that tick's report).

    The loop ends when `stop_event` is set. Without one, SIGTERM and SIGINT stop it
    cleanly (only when called from the main thread).
    """
    if store is None:
        store = open_store(cfg)
    if once:
        return tick(cfg, store)

    stop = stop_event or threading.Event()
    restore: list[tuple[int, object]] = []
    if stop_event is None and threading.current_thread() is threading.main_thread():
        def _stop(signum, frame):
            stop.set()
        for sig in (signal.SIGTERM, signal.SIGINT):
            restore.append((sig, signal.signal(sig, _stop)))

    if dashboard:
        _start_dashboard(cfg)
    interval = max(1.0, float(cfg.get("daemon.interval_s", 15)))
    _log(cfg, f"daemon started pid={os.getpid()} interval={interval}s")
    try:
        while not stop.is_set():
            try:
                tick(cfg, store)
            except Exception as e:  # noqa: BLE001 - tick is designed not to raise; belt and braces
                _log(cfg, f"tick crashed: {e}")
            stop.wait(interval)
    finally:
        for sig, prev in restore:
            signal.signal(sig, prev)
        _log(cfg, "daemon stopped")
    return None

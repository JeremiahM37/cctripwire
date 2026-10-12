"""Runaway breaker: trips when `claude -p` launches pile up far beyond normal use.

It is independent of quota levels. It looks only at how many headless runs are queued or
were started recently, so ordinary work never comes near it, while a script that fires
hundreds of `claude -p` calls is cut off after the first few dozen. Once tripped, every new
`claude -p` is refused until the cool-off ends or `cctripwire breaker reset` is run.
"""
from __future__ import annotations

from . import util
from .models import FIVE_HOUR, SEVEN_DAY, Level

KEY = "breaker"            # {"tripped_at", "until", "reason"} while tripped
OFF_KEY = "breaker_off"    # {"until"}: user lifted the breaker for a while
RESET_KEY = "breaker_reset_at"   # launches before the last reset do not count again
OBSERVED_KEY = "breaker_observed"   # {"at", "reason"}: last would-trip in observe mode
ADVICE = ("Stop launching claude -p and tell the user. Only the user can lift this "
          "(`cctripwire breaker reset`).")


def enabled(cfg) -> bool:
    return any(float(cfg.get(k, 0) or 0) for k in (
        "gateway.storm_max_queued", "gateway.storm_max_started",
        "gateway.surge_five_hour_pct", "gateway.surge_seven_day_pct", "daemon.bypass_storm_max",
        "daemon.bypass_storm_started"))


def surge(store, cfg, now: float | None = None) -> str | None:
    """A reason when an exact quota meter rose by the configured points within surge_window_s."""
    now = util.now() if now is None else now
    win = float(cfg.get("gateway.surge_window_s", 600) or 600)
    for window, key, label in ((FIVE_HOUR, "gateway.surge_five_hour_pct", "5-hour"),
                               (SEVEN_DAY, "gateway.surge_seven_day_pct", "weekly")):
        limit = float(cfg.get(key, 0) or 0)
        if limit <= 0:
            continue
        samples = store.quota_samples(window, now - win, exact=True)
        if len(samples) < 2:
            continue
        last = samples[-1]
        same = [x for x in samples if util.same_reset(x.resets_at, last.resets_at)]
        low = min(same, key=lambda x: x.used_pct)
        rise = last.used_pct - low.used_pct
        if rise >= limit and last.ts > low.ts:
            mins = max(1, round((last.ts - low.ts) / 60))
            return (f"surge: {label} usage rose {rise:g} points in {mins} min "
                    f"({low.used_pct:g}% -> {last.used_pct:g}%)")
    return None


def _since(store, win: float, now: float) -> float:
    """Start of the launch-counting window: storm_window_s back, but never before the last
    reset or the end of a lift, so launches the user already cleared cannot re-trip it."""
    floor = now - win
    for value in (store.kv_get(RESET_KEY), (store.kv_get(OFF_KEY) or {}).get("until")):
        try:
            if value is not None and float(value) <= now:
                floor = max(floor, float(value))
        except (TypeError, ValueError):
            pass
    return floor


def bypass_rate(store, cfg, now: float | None = None) -> str | None:
    """A reason when too many claude -p processes were started outside the gateway within
    storm_window_s. The daemon samples processes, so short runs can be missed: the count is a
    lower bound."""
    now = util.now() if now is None else now
    limit = int(cfg.get("daemon.bypass_storm_started", 0) or 0)
    if limit <= 0:
        return None
    win = float(cfg.get("gateway.storm_window_s", 600) or 600)
    n = store.conn.execute("SELECT COUNT(*) FROM detections WHERE kind='gateway_bypass' AND ts>=?",
                           (_since(store, win, now),)).fetchone()[0]
    if n >= limit:
        return f"{n} claude -p runs launched outside the gateway in {util.fmt_duration(win)}"
    return None


def _live(value, now: float) -> dict | None:
    if not isinstance(value, dict):
        return None
    until = value.get("until")
    if until is not None and float(until) <= now:
        return None
    return value


def tripped(store, now: float | None = None) -> dict | None:
    """The trip record while the breaker is open, else None."""
    now = util.now() if now is None else now
    if _live(store.kv_get(OFF_KEY), now):
        return None
    return _live(store.kv_get(KEY), now)


def lifted(store, now: float | None = None) -> dict | None:
    now = util.now() if now is None else now
    return _live(store.kv_get(OFF_KEY), now)


def observing(cfg) -> bool:
    """Observe mode: record and notify what would have tripped, but never act on it."""
    return bool(cfg.get("gateway.storm_observe", False))


def trip(store, cfg, reason: str, now: float | None = None, force: bool = False) -> dict:
    """Open the breaker. Safe inside or outside a transaction.

    In observe mode nothing is opened: the returned record has observed=True, and the
    would-trip is recorded at most once per storm window (new=True on that first one)."""
    now = util.now() if now is None else now
    if observing(cfg) and not force:
        last = store.kv_get(OBSERVED_KEY) or {}
        win = float(cfg.get("gateway.storm_window_s", 600) or 600)
        new = not last or now - float(last.get("at", 0)) >= win
        if new:
            store.kv_set(OBSERVED_KEY, {"at": now, "reason": reason})
            store.add_intervention("breaker_would_trip", "gateway", f"observe mode: {reason}",
                                   level=Level.STOP)
        return {"tripped_at": now, "until": None, "reason": reason, "observed": True, "new": new}
    cool = float(cfg.get("gateway.storm_cooloff_s", 1800) or 0)
    rec = {"tripped_at": now, "until": now + cool if cool > 0 else None, "reason": reason}
    store.kv_set(KEY, rec)
    store.add_intervention("breaker_trip", "gateway", reason, level=Level.STOP)
    return rec


def check(store, cfg, now: float | None = None) -> tuple[str | None, bool]:
    """Call inside an immediate transaction, before inserting a new invocation.

    Returns (reason, newly_tripped). reason is None when the launch may proceed.
    """
    now = util.now() if now is None else now
    if not enabled(cfg) or lifted(store, now):
        return None, False
    rec = tripped(store, now)
    if rec:
        return str(rec.get("reason") or "runaway breaker tripped"), False
    why, _ = _storm(store, cfg, now)
    if why is None:
        return None, False
    rec = trip(store, cfg, why, now)
    if rec.get("observed"):
        return None, False
    return rec["reason"], True


def _storm(store, cfg, now: float) -> tuple[str | None, bool]:
    c = store.conn
    max_q = int(cfg.get("gateway.storm_max_queued", 0) or 0)
    if max_q:
        queued = c.execute("SELECT COUNT(*) FROM invocations WHERE status='queued'").fetchone()[0]
        if queued >= max_q:
            return f"{queued} claude -p runs already waiting for a slot", True
    max_s = int(cfg.get("gateway.storm_max_started", 0) or 0)
    if max_s:
        win = float(cfg.get("gateway.storm_window_s", 600) or 600)
        n = c.execute("SELECT COUNT(*) FROM invocations WHERE created>=? AND status<>'denied'",
                      (_since(store, win, now),)).fetchone()[0]
        if n >= max_s:
            return f"{n} claude -p runs launched in {util.fmt_duration(win)}", True
    why = surge(store, cfg, now)
    return why, why is not None


def reset(store) -> None:
    store.kv_set(KEY, None)
    store.kv_set(RESET_KEY, util.now())
    store.add_intervention("breaker_reset", "gateway", "breaker reset by user")


def lift(store, seconds: float, now: float | None = None) -> float:
    """Disable the breaker for `seconds` (for a deliberate large batch). Returns the end time."""
    now = util.now() if now is None else now
    until = now + float(seconds)
    store.kv_set(KEY, None)
    store.kv_set(OFF_KEY, {"until": until})
    store.add_intervention("breaker_off", "gateway", f"breaker lifted for {util.fmt_duration(seconds)}")
    return until


def status(store, cfg, now: float | None = None) -> dict:
    now = util.now() if now is None else now
    rec = tripped(store, now)
    off = lifted(store, now)
    seen = store.kv_get(OBSERVED_KEY) or {}
    return {
        "enabled": enabled(cfg),
        "observe": observing(cfg),
        "last_would_trip": seen or None,
        "tripped": bool(rec),
        "reason": rec.get("reason") if rec else None,
        "tripped_at": rec.get("tripped_at") if rec else None,
        "until": rec.get("until") if rec else None,
        "lifted_until": off.get("until") if off else None,
    }

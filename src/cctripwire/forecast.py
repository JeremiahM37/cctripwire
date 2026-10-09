"""Window states (used %, burn rate, ETA, pace) and the point-in-time Snapshot.

Exact provider readings win; an estimate is used when the reading is stale; otherwise the
state is unknown (used_pct None). A reset time that has passed also means unknown, since a
reset is not proof of 0%.
"""
from __future__ import annotations

from typing import Optional

from . import util
from .models import FIVE_HOUR, SEVEN_DAY, WINDOW_SECONDS, Snapshot, TokenTotals, WindowState
from .quota.estimator import estimate, per_pct
from .store import RUNNING_STATES

BURN_LOOKBACK_S = 1800
SEEN_LOOKBACK_S = 8 * 86400
ACTIVE_SESSION_S = 900


def _elapsed(resets_at: Optional[float], window: str, t: float) -> Optional[float]:
    if resets_at is None or window not in WINDOW_SECONDS:
        return None
    return min(1.0, max(0.0, 1 - (resets_at - t) / WINDOW_SECONDS[window]))


def _slope_per_hour(points: list[tuple[float, float]]) -> float:
    """Least-squares slope of (ts, used_pct) in pct per hour, floored at 0."""
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    den = sum((x - mx) ** 2 for x, _ in points)
    if den == 0:
        return 0.0
    return max(0.0, sum((x - mx) * (y - my) for x, y in points) / den * 3600)


def _burn(store, cfg, window: str, t: float, resets_at: Optional[float]) -> Optional[float]:
    """Burn rate in pct/hour: slope of recent exact readings, else tokens-per-calibration."""
    samples = [s for s in store.quota_samples(window, t - BURN_LOOKBACK_S, exact=True)
               if resets_at is None or util.same_reset(s.resets_at, resets_at)]
    if len(samples) >= 2 and samples[-1].ts - samples[0].ts >= 60:
        return _slope_per_hour([(s.ts, s.used_pct) for s in samples])
    ppp = per_pct(store, cfg, window, t)
    if not ppp:
        return None
    return store.token_totals(t - BURN_LOOKBACK_S).weighted / ppp / (BURN_LOOKBACK_S / 3600)


def _unknown(window: str, resets_at: Optional[float], age: Optional[float], stale: bool,
             t: float) -> WindowState:
    return WindowState(window=window, used_pct=None, resets_at=resets_at, exact=False, source="none",
                       age_s=age, burn_pct_per_hour=None, eta_s=None,
                       elapsed_frac=_elapsed(resets_at, window, t), pace_ratio=None, stale=stale)


def window_state(store, cfg, window: str, now: float | None = None) -> WindowState:
    """Best current picture of one window: fresh exact reading, else estimate, else unknown."""
    t = util.now() if now is None else now
    stale_after = float(cfg.get("general.stale_after_s", 900))
    base = store.latest_quota(window, exact=True)
    age = None if base is None else t - base.ts
    if base is not None and base.resets_at is not None and base.resets_at <= t:
        return _unknown(window, base.resets_at, age, True, t)
    if base is not None and age < stale_after:
        used, resets, exact, source, stale = base.used_pct, base.resets_at, True, base.source, False
    else:
        est = estimate(store, cfg, window, t)
        if est is None:
            return _unknown(window, base.resets_at if base else None, age, base is not None, t)
        used, resets, exact, source, stale = est.used_pct, est.resets_at, False, "estimate", base is not None
    burn = _burn(store, cfg, window, t, resets)
    eta = None
    if used is not None and burn and burn > 0:
        eta = max(0.0, 100 - used) / burn * 3600
        if resets is not None and t + eta >= resets:
            eta = None
    elapsed = _elapsed(resets, window, t)
    pace = used / (elapsed * 100) if used is not None and elapsed is not None and elapsed >= 0.05 else None
    return WindowState(window=window, used_pct=used, resets_at=resets, exact=exact, source=source,
                       age_s=age, burn_pct_per_hour=burn, eta_s=eta, elapsed_frac=elapsed,
                       pace_ratio=pace, stale=stale)


def _active_sessions(store, t: float) -> int:
    """Sessions seen by hooks/status line, plus any that wrote usage to a transcript recently."""
    since = t - ACTIVE_SESSION_S
    ids = {r["session_id"] for r in store.active_sessions(since)}
    ids.update(r[0] for r in store.conn.execute(
        "SELECT DISTINCT session_id FROM token_events WHERE ts>=? AND session_id IS NOT NULL", (since,)))
    return len(ids)


def build_snapshot(store, cfg, now: float | None = None) -> Snapshot:
    """Everything policy, hooks, gateway and the dashboard read, from indexed queries."""
    t = util.now() if now is None else now
    names = [FIVE_HOUR, SEVEN_DAY]
    seen = store.conn.execute("SELECT DISTINCT window FROM quota_samples WHERE ts>=?",
                              (t - SEEN_LOOKBACK_S,)).fetchall()
    names += [r[0] for r in seen if r[0] not in names]
    windows = {n: window_state(store, cfg, n, t) for n in names}
    five = windows[FIVE_HOUR]
    # The five-hour window starts at its reset minus 5 h when that is known, else 5 h back.
    start = t - WINDOW_SECONDS[FIVE_HOUR]
    if five.resets_at is not None and five.resets_at > t:
        start = five.resets_at - WINDOW_SECONDS[FIVE_HOUR]
    running = store.conn.execute(
        f"SELECT COUNT(*) FROM invocations WHERE status IN ({','.join('?' * len(RUNNING_STATES))})",
        RUNNING_STATES).fetchone()[0]
    by_project, by_agent = store.weighted_by_project_agent(start)
    return Snapshot(
        ts=t,
        windows=windows,
        tokens_per_min=store.token_totals(t - 600).weighted / 10,
        tokens_last_hour=store.token_totals(t - 3600),
        running_invocations=int(running),
        active_sessions=_active_sessions(store, t),
        by_project=dict(sorted(by_project.items(), key=lambda kv: -kv[1])),
        by_agent=dict(sorted(by_agent.items(), key=lambda kv: -kv[1])),
    )


__all__ = ["window_state", "build_snapshot"]

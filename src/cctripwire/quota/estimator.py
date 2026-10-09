"""Weighted tokens, calibration against provider readings, and window estimates."""
from __future__ import annotations

import statistics
from typing import Optional

from ..models import WINDOW_SECONDS, QuotaSample
from ..util import now as _now, same_reset

# Cost factor per model family (subscription allowance is roughly proportional to API cost).
MODEL_FACTORS = (("opus", 5.0), ("sonnet", 1.0), ("haiku", 1 / 3), ("fable", 5.0))
CALIB_MIN_DELTA = 2.0      # pct points; smaller deltas are too noisy to calibrate on
CALIB_MAX_PAIRS = 50
CALIB_LOOKBACK_S = 8 * 86400


def model_factor(model: str | None) -> float:
    m = (model or "").lower()
    for key, factor in MODEL_FACTORS:
        if key in m:
            return factor
    return 1.0


def counts(usage: dict | None) -> tuple[int, int, int, int]:
    """(input, output, cache_create, cache_read) from an Anthropic usage object."""
    u = usage or {}
    def n(key: str) -> int:
        v = u.get(key)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
    return (n("input_tokens"), n("output_tokens"),
            n("cache_creation_input_tokens"), n("cache_read_input_tokens"))


def weight(model: str | None, usage: dict | None) -> float:
    """Weighted tokens: in*1 + out*5 + cache_create*1.25 + cache_read*0.1, times the model factor."""
    i, o, cc, cr = counts(usage)
    return (i + 5 * o + 1.25 * cc + 0.1 * cr) * model_factor(model)


def per_pct_budget(cfg, window: str) -> Optional[float]:
    """Configured budget (weighted tokens per 1%) or None."""
    budget = float(cfg.get(f"sources.{window}_budget", 0) or 0)
    return budget / 100 if budget > 0 else None


def calibrate(store, window: str, now: float | None = None, *, max_age: float = 3600.0) -> Optional[float]:
    """Weighted tokens per 1% of the window: median over pairs of exact readings.

    A pair is two readings in the same reset period that rose by at least 2 points, with
    tokens recorded between them. The result is cached in kv `calib:<window>`.
    """
    t = _now() if now is None else now
    key = f"calib:{window}"
    cached = store.kv_get(key)
    if cached and 0 <= t - cached.get("ts", 0) <= max_age:
        return cached.get("per_pct")
    samples = [s for s in store.quota_samples(window, t - CALIB_LOOKBACK_S, exact=True) if s.resets_at]
    # Readings arrive every few seconds in whole percents, so consecutive pairs rarely differ by
    # 2 points: hold an anchor and pair it with the first later reading that has risen enough.
    pairs = []
    anchor = samples[0] if samples else None
    for b in samples[1:]:
        if not same_reset(b.resets_at, anchor.resets_at) or b.used_pct < anchor.used_pct:
            anchor = b                     # new reset period (or a drop): restart
        elif b.used_pct - anchor.used_pct >= CALIB_MIN_DELTA:
            pairs.append((anchor, b))
            anchor = b
    ratios = []
    for a, b in pairs[-CALIB_MAX_PAIRS:]:
        tokens = store.token_totals(a.ts, b.ts).weighted
        if tokens > 0:
            ratios.append(tokens / (b.used_pct - a.used_pct))
    if not ratios:
        return None
    per_pct = statistics.median(ratios)
    store.kv_set(key, {"ts": t, "per_pct": per_pct, "pairs": len(ratios)})
    return per_pct


def per_pct(store, cfg, window: str, now: float) -> Optional[float]:
    """Budget if configured, else the calibrated value."""
    return per_pct_budget(cfg, window) or calibrate(store, window, now)


def estimate(store, cfg, window: str, now: float | None = None) -> Optional[QuotaSample]:
    """Extrapolate utilisation from the latest exact reading plus tokens spent since.

    Without a usable base reading, a configured budget gives a rolling-window estimate.
    Returns None when neither is available.
    """
    t = _now() if now is None else now
    base = store.latest_quota(window, exact=True)
    window_s = WINDOW_SECONDS.get(window)
    usable = base is not None and not (base.resets_at and base.resets_at <= t)
    if usable and base.resets_at is None and window_s and t - base.ts > window_s:
        usable = False     # unknown reset and too old to extrapolate from
    ppp = per_pct(store, cfg, window, t)
    if not ppp:
        return None
    if usable:
        used = base.used_pct + store.token_totals(base.ts).weighted / ppp
        return QuotaSample(ts=t, window=window, used_pct=used, resets_at=base.resets_at,
                           source="estimate", exact=False)
    if per_pct_budget(cfg, window) and window_s:
        used = store.token_totals(t - window_s).weighted / ppp
        return QuotaSample(ts=t, window=window, used_pct=used, resets_at=None,
                           source="estimate", exact=False)
    return None


__all__ = ["weight", "counts", "model_factor", "calibrate", "estimate", "per_pct"]

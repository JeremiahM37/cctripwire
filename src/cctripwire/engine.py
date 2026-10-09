"""Integration seam: build a Snapshot and a Decision, with a short shared cache.

Hooks run on every tool call, so recomputing everything each time would be wasteful;
the last decision is cached in the store for `max_age` seconds and shared by all
processes.
"""
from __future__ import annotations

from dataclasses import asdict

from .config import Config
from .forecast import build_snapshot
from .models import Decision, Level, Snapshot
from .policy import active_override, evaluate_policy
from .store import Store
from .util import now as _now

CACHE_KEY = "decision_cache"


def decide(store: Store, cfg: Config, *, project: str | None = None, agent: str | None = None,
           now: float | None = None, max_age: float = 5.0) -> tuple[Snapshot, Decision]:
    t = _now() if now is None else now
    snap = None
    cache = store.kv_get(CACHE_KEY) if max_age > 0 else None
    if cache and 0 <= t - cache.get("ts", 0) <= max_age:
        snap = _snapshot_from_dict(cache["snap"])
    if snap is None:
        snap = build_snapshot(store, cfg, now=t)
        if max_age > 0:
            store.kv_set(CACHE_KEY, {"ts": t, "snap": snap.as_dict()})
    return snap, evaluate_policy(snap, cfg, project=project, agent=agent, override=active_override(store, t))


def _snapshot_from_dict(d: dict) -> Snapshot:
    from .models import TokenTotals, WindowState
    s = Snapshot(**{k: v for k, v in d.items() if k not in ("windows", "tokens_last_hour")})
    s.windows = {k: WindowState(**v) for k, v in d.get("windows", {}).items()}
    s.tokens_last_hour = TokenTotals(**d.get("tokens_last_hour", {}))
    return s


def invalidate(store: Store) -> None:
    store.kv_set(CACHE_KEY, None)


def decision_dict(dec: Decision) -> dict:
    d = asdict(dec)
    d["level"] = str(dec.level)
    return d


__all__ = ["decide", "invalidate", "decision_dict", "Level"]

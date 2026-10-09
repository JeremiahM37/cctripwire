"""Policy engine: Snapshot + config -> Decision.

Rules (config `rules`) are evaluated against the global snapshot. Per-project and
per-agent budgets (config `budgets`) can only raise the level for that scope. A
manual override (set by `cctripwire override` / `pause`) replaces the computed level
until it expires. The returned Decision carries the concrete effects of its level,
taken from config `levels.<name>`, so callers never re-derive them.
"""
from __future__ import annotations

from typing import Optional

from .config import Config
from .expr import evaluate
from .models import Decision, Level, Snapshot
from .util import fmt_tokens

OVERRIDE_KEY = "override"   # kv: {"level": "stop", "until": ts, "reason": "..."}


def rule_env(snap: Snapshot) -> dict:
    env: dict = {name: ws for name, ws in snap.windows.items()}
    env.update(tokens_per_min=snap.tokens_per_min, running=snap.running_invocations,
               sessions=snap.active_sessions)
    return env


def _apply_effects(d: Decision, cfg: Config, base_concurrency: int) -> Decision:
    d.max_concurrency = base_concurrency
    if d.level >= Level.THROTTLE:
        lc = cfg.level_cfg("throttle")
        d.max_concurrency = min(d.max_concurrency, int(lc.get("max_concurrency", 1)))
        d.min_interval_s = max(d.min_interval_s, float(lc.get("min_interval_s", 0)))
    if d.level >= Level.RESTRICT:
        lc = cfg.level_cfg("restrict")
        d.max_concurrency = min(d.max_concurrency, int(lc.get("max_concurrency", 1)))
        d.min_interval_s = max(d.min_interval_s, float(lc.get("min_interval_s", 0)))
        d.deny_tools = list(lc.get("deny_tools", []))
        d.deny_spawn = bool(lc.get("deny_spawn", True))
        d.downgrade_model = lc.get("downgrade_model") or None
    if d.level >= Level.STOP:
        lc = cfg.level_cfg("stop")
        d.max_concurrency = 0
        d.deny_spawn = True
        d.checkpoint = bool(lc.get("checkpoint", True))
    return d


def _raise(d: Decision, level: Level, rule: str, reason: str) -> None:
    if level > d.level:
        d.level = level
    d.rules.append(rule)
    d.reasons.append(reason)


def _budget_check(d: Decision, kind: str, name: Optional[str], used: Optional[float], cfg: Config) -> Optional[int]:
    b = cfg.budget_for(kind, name)
    if not b:
        return None
    label = f"{kind[:-1]} {name}"
    if used is not None:
        hard, soft = b.get("hard_weighted"), b.get("soft_weighted")
        if hard and used >= float(hard):
            _raise(d, Level.STOP, f"budget:{kind}:{name}:hard",
                   f"{label} used {fmt_tokens(used)} of its {fmt_tokens(hard)} 5h budget")
        elif soft and used >= float(soft):
            _raise(d, Level.THROTTLE, f"budget:{kind}:{name}:soft",
                   f"{label} past soft budget ({fmt_tokens(used)} / {fmt_tokens(soft)})")
    mc = b.get("max_concurrency")
    return int(mc) if mc is not None else None


def evaluate_policy(snap: Snapshot, cfg: Config, *, project: str | None = None, agent: str | None = None,
                    override: dict | None = None) -> Decision:
    d = Decision(level=Level.OK)
    env = rule_env(snap)
    for rule in cfg.get("rules", []):
        try:
            fired = bool(evaluate(rule["when"], env))
        except Exception:  # noqa: BLE001 - a broken rule must not break enforcement
            continue
        if fired:
            _raise(d, Level.parse(rule.get("level", "warn")), rule.get("name", "rule"),
                   rule.get("message") or rule.get("name", "rule"))

    caps = [int(cfg.get("gateway.max_concurrency", 4))]
    for kind, name, table in (("projects", project, snap.by_project), ("agents", agent, snap.by_agent)):
        if name:
            mc = _budget_check(d, kind, name, table.get(name), cfg)
            if mc is not None:
                caps.append(mc)

    if override:
        lvl = Level.parse(override.get("level", "ok"))
        d.level = lvl
        d.rules.append("override")
        d.reasons.insert(0, override.get("reason") or f"manual override: {lvl}")
    return _apply_effects(d, cfg, min(caps))


def active_override(store, now: float) -> dict | None:
    ov = store.kv_get(OVERRIDE_KEY)
    if not ov:
        return None
    until = ov.get("until")
    if until is not None and until < now:
        return None
    return ov

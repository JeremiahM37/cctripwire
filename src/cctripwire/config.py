"""Configuration: built-in defaults deep-merged with a TOML file.

Lookup order for the file: $CCTRIPWIRE_CONFIG, then ~/.config/cctripwire/config.toml.
Values are read with dotted keys: cfg.get("gateway.max_concurrency").
"""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

DEFAULTS: dict = {
    "general": {
        "state_dir": "~/.local/state/cctripwire",
        "claude_dir": "",            # "" = $CLAUDE_CONFIG_DIR or ~/.claude
        "stale_after_s": 900,        # a quota reading older than this is "stale"
        "agent_name": "",            # default agent label ("" = project dir name)
    },
    "sources": {
        "statusline": True,          # capture rate_limits from the status-line hook (official)
        "statusline_chain": "",      # previous statusLine command to run and print after ours
        "oauth": False,              # OPT-IN: undocumented account usage endpoint
        "oauth_min_interval_s": 300,
        "oauth_cooldown_s": 900,
        "usage_url": "",             # URL returning "agent-usage" JSON (see docs/usage-url.md)
        "usage_url_interval_s": 60,
        "transcripts": True,         # scan ~/.claude/projects/**/*.jsonl for token usage
        # Weighted-token budgets equivalent to 100% of a window. 0 = auto-calibrate
        # from paired exact readings; until calibrated, estimates are unavailable.
        "five_hour_budget": 0,
        "seven_day_budget": 0,
    },
    "gateway": {
        "real_claude": "",           # "" = auto-detect the real binary on PATH
        "max_concurrency": 4,        # simultaneous gateway invocations, all sessions
        "max_depth": 2,              # nesting depth of claude -p under claude -p
        "max_children": 4,           # live children per parent invocation
        "queue_timeout_s": 1800,     # give up waiting for a slot after this
        "queue_poll_s": 0.5,         # how often a queued gateway re-checks for a slot
        "per_invocation_tokens": 0,  # weighted-token cap per invocation, 0 = none
        "per_invocation_max_turns": 0,
        "per_invocation_budget_usd": 0.0,
        "duplicate_window_s": 600,   # identical prompt within this window counts as a repeat
        "max_duplicates": 3,         # deny the Nth identical invocation
        "spawn_rate_per_min": 12,    # new invocations per minute, all sessions
        "kill_grace_s": 10,
        "heartbeat_s": 5,
        "on_stop": "deny",           # "wait" = queue new work until the level drops below STOP
        "force_model": "",           # run every gateway invocation on this model ("" = as asked)
        # Runaway breaker: independent of quota levels. When tripped, every new claude -p is
        # refused (and running ones paused) until cool-off or `cctripwire breaker reset`.
        "storm_max_queued": 24,      # trip when this many runs are already waiting for a slot (0 = off)
        "storm_max_started": 40,     # trip when this many runs were launched within storm_window_s (0 = off)
        "storm_window_s": 600,
        "storm_cooloff_s": 1800,     # breaker re-closes by itself after this (0 = only by reset)
        "storm_observe": False,      # record + notify would-trips only; never refuse or pause
        "storm_pause_running": True, # SIGSTOP running gateway runs while tripped (daemon)
        "surge_five_hour_pct": 0,    # trip when the exact 5-hour meter rises this many points
        "surge_seven_day_pct": 0,    #   (or the weekly one) within surge_window_s; 0 = off
        "surge_window_s": 600,
    },
    "loops": {
        "window_s": 900,
        "identical_prompt_threshold": 3,
        "tool_failure_threshold": 4,     # same tool+input failing N times
        "retry_threshold": 6,            # same tool+input called N times regardless of outcome
        "no_progress_calls": 40,         # tool calls with no successful write/edit
        "spike_factor": 4.0,             # tokens/min vs trailing median
        "spike_min_weighted": 50000,
        "context_rebuild_threshold": 3,  # compactions within window
        "parallel_spawn_threshold": 5,   # spawns within 60s by one session
        "recursion_depth": 3,
    },
    "levels": {
        "warn": {"notify_interval_s": 600},
        "throttle": {"max_concurrency": 1, "min_interval_s": 30},
        "restrict": {
            "max_concurrency": 1,
            "min_interval_s": 60,
            "deny_tools": ["Agent", "Task", "Workflow", "WebSearch"],
            "deny_spawn": True,
            "downgrade_model": "haiku",
        },
        "stop": {
            "checkpoint": True,
            "checkpoint_tool_calls": 3,   # tool calls allowed for writing the checkpoint
            "checkpoint_file": "CHECKPOINT.md",
            "deny_spawn": True,
        },
    },
    # Rules fire when `when` is true. Expressions see each window by name
    # (five_hour, seven_day, ...) with fields used_pct, eta_s, burn_pct_per_hour,
    # elapsed_frac, pace_ratio, exact, stale; plus tokens_per_min, running, sessions.
    # Comparisons against an unknown (None) value are False.
    "rules": [
        {"name": "early-burn", "when": "five_hour.used_pct >= 50 and five_hour.elapsed_frac < 0.4",
         "level": "warn", "message": "5-hour window half used in under 40% of its time"},
        {"name": "weekly-pace", "when": "seven_day.used_pct >= 30 and seven_day.pace_ratio >= 1.5",
         "level": "warn", "message": "weekly allowance burning 1.5x faster than even pace"},
        {"name": "fast-exhaust", "when": "five_hour.eta_s < 1800 and five_hour.used_pct < 95",
         "level": "throttle", "message": "5-hour window predicted to run out within 30 min"},
        {"name": "five-hour-85", "when": "five_hour.used_pct >= 85",
         "level": "throttle", "message": "5-hour window at 85%"},
        {"name": "weekly-85", "when": "seven_day.used_pct >= 85",
         "level": "restrict", "message": "weekly allowance at 85%: expensive operations restricted"},
        {"name": "critical-5h", "when": "five_hour.used_pct >= 95",
         "level": "stop", "message": "5-hour window at 95%: checkpoint and stop"},
        {"name": "critical-weekly", "when": "seven_day.used_pct >= 95",
         "level": "stop", "message": "weekly allowance at 95%: checkpoint and stop"},
    ],
    # Per-project / per-agent budgets, keyed by project name (dir basename) or agent label:
    #   [budgets.projects.myrepo]
    #   soft_weighted = 2e6     # warn above this (5-hour window, weighted tokens)
    #   hard_weighted = 4e6     # stop above this
    #   max_concurrency = 2
    #   max_depth = 1
    #   per_invocation_tokens = 300000
    "budgets": {"projects": {}, "agents": {}},
    "hooks": {
        "block_prompts_at_stop": True,   # block new user prompts at STOP (override to continue)
        "enforce_loops": True,           # False = loop detections are recorded, never denied
    },
    "dashboard": {"host": "127.0.0.1", "port": 8377},
    "daemon": {
        "interval_s": 15,
        "enforce_bypass": False,     # act on claude processes that bypass the gateway
        "pause_on_stop": True,       # SIGSTOP gateway processes at STOP instead of killing
        "auto_resume": True,         # SIGCONT / release queue when level drops
        "notify_command": "",        # run (no shell) on level changes; $CCTRIPWIRE_TITLE / $CCTRIPWIRE_BODY
        "bypass_storm_max": 0,       # trip the breaker (and SIGSTOP them) at this many claude -p
                                     # processes running outside the gateway; 0 = off
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    def __init__(self, data: dict | None = None, path: Path | None = None):
        self.data = _merge(DEFAULTS, data or {})
        self.path = path

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    @property
    def state_dir(self) -> Path:
        env = os.environ.get("CCTRIPWIRE_HOME")
        return Path(env or os.path.expanduser(self.get("general.state_dir")))

    @property
    def db_path(self) -> Path:
        return self.state_dir / "tripwire.db"

    @property
    def claude_dir(self) -> Path:
        d = self.get("general.claude_dir") or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
        return Path(os.path.expanduser(d))

    def level_cfg(self, level_name: str) -> dict:
        return self.get(f"levels.{level_name}", {}) or {}

    def budget_for(self, kind: str, name: str | None) -> dict:
        """kind is 'projects' or 'agents'."""
        if not name:
            return {}
        return self.get(f"budgets.{kind}", {}).get(name, {}) or {}

    def validate(self) -> list[str]:
        """Return a list of problems (empty = valid)."""
        from .models import Level
        errs = []
        for i, r in enumerate(self.get("rules", [])):
            for key in ("name", "when", "level"):
                if key not in r:
                    errs.append(f"rules[{i}] missing '{key}'")
            try:
                Level.parse(r.get("level", "ok"))
            except KeyError:
                errs.append(f"rules[{i}] unknown level {r.get('level')!r}")
            try:
                from .expr import compile_expr
                compile_expr(r.get("when", "False"))
            except Exception as e:  # noqa: BLE001 - report any parse problem
                errs.append(f"rules[{i}] bad expression: {e}")
        errs += self._unknown_keys()
        for key in ("gateway.max_concurrency", "gateway.max_depth"):
            v = self.get(key)
            if not isinstance(v, int) or v < 0:
                errs.append(f"{key} must be a non-negative integer")
        return errs


    def _unknown_keys(self) -> list[str]:
        """Keys not in DEFAULTS: usually a typo, or a top-level key placed after a [table]."""
        free = {"budgets", "rules"}          # user-named entries
        errs = []
        for k, v in self.data.items():
            if k == "extend_rules":
                continue
            if k not in DEFAULTS:
                errs.append(f"unknown section or key '{k}'")
            elif isinstance(v, dict) and k not in free:
                for sub in v:
                    if sub not in DEFAULTS[k]:
                        errs.append(f"unknown key '{k}.{sub}'")
        return errs


def default_config_path() -> Path:
    env = os.environ.get("CCTRIPWIRE_CONFIG")
    if env:
        return Path(env)
    return Path(os.path.expanduser("~/.config/cctripwire/config.toml"))


def load(path: str | os.PathLike | None = None) -> Config:
    p = Path(path) if path else default_config_path()
    data: dict = {}
    if p.exists():
        with open(p, "rb") as f:
            data = tomllib.load(f)
        # A user-supplied rules list replaces the defaults unless they opt to extend.
        if data.get("extend_rules") and "rules" in data:
            data["rules"] = DEFAULTS["rules"] + data["rules"]
    return Config(data, p)

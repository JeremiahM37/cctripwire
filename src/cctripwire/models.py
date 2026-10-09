"""Shared data types. Every module exchanges these, never raw dicts."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import IntEnum
from typing import Optional


class Level(IntEnum):
    """Policy severity. Ordered: a higher level implies every lower effect."""
    OK = 0
    WARN = 1
    THROTTLE = 2
    RESTRICT = 3
    STOP = 4

    @classmethod
    def parse(cls, s: str) -> "Level":
        return cls[s.strip().upper()]

    def __str__(self) -> str:  # "warn", not "Level.WARN"
        return self.name.lower()


# Window names used everywhere. Extra model-scoped windows reported by a source
# (e.g. "seven_day_opus") are kept verbatim.
FIVE_HOUR = "five_hour"
SEVEN_DAY = "seven_day"
WINDOW_SECONDS = {FIVE_HOUR: 5 * 3600, SEVEN_DAY: 7 * 86400}


@dataclass
class QuotaSample:
    """One utilisation reading for one window."""
    ts: float
    window: str
    used_pct: float              # 0..100
    resets_at: Optional[float]   # unix seconds, None if unknown
    source: str                  # "statusline" | "oauth" | "estimate" | "mock"
    exact: bool                  # True only for provider-reported numbers


@dataclass
class WindowState:
    """Everything the policy engine knows about one window, at one instant."""
    window: str
    used_pct: Optional[float]          # None = unknown (never silently 0)
    resets_at: Optional[float]
    exact: bool                        # reported by the provider vs estimated
    source: str
    age_s: Optional[float]             # seconds since the reading
    burn_pct_per_hour: Optional[float]
    eta_s: Optional[float]             # predicted seconds until 100%, None = not on track to exhaust
    elapsed_frac: Optional[float]      # fraction of the window elapsed (0..1)
    pace_ratio: Optional[float]        # used_pct / (elapsed_frac*100); >1 = burning faster than even pace
    stale: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class TokenTotals:
    input: int = 0
    output: int = 0
    cache_create: int = 0
    cache_read: int = 0
    weighted: float = 0.0          # cost-equivalent units (see quota.estimator.weight)
    messages: int = 0

    def add(self, o: "TokenTotals") -> None:
        self.input += o.input; self.output += o.output
        self.cache_create += o.cache_create; self.cache_read += o.cache_read
        self.weighted += o.weighted; self.messages += o.messages


@dataclass
class Snapshot:
    """Point-in-time view consumed by policy, hooks, gateway, CLI and dashboard."""
    ts: float
    windows: dict = field(default_factory=dict)          # name -> WindowState
    tokens_per_min: float = 0.0                          # weighted, last 10 min, all agents
    tokens_last_hour: TokenTotals = field(default_factory=TokenTotals)
    running_invocations: int = 0
    active_sessions: int = 0
    by_project: dict = field(default_factory=dict)       # project -> weighted tokens in 5h window
    by_agent: dict = field(default_factory=dict)         # agent -> weighted tokens in 5h window

    def window(self, name: str) -> Optional[WindowState]:
        return self.windows.get(name)

    def as_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class Detection:
    """A loop/anomaly finding. `advice` is the short text shown to the agent."""
    kind: str            # identical_prompt | repeated_tool_failure | repeated_claude_p | recursive_spawn |
                         # excessive_retries | no_progress | token_spike | context_rebuild | parallel_spawn
    severity: Level
    session_id: Optional[str]
    detail: str          # human/log detail
    advice: str          # <= ~200 chars, sent to the agent
    count: int = 1
    invocation_id: Optional[str] = None


@dataclass
class Decision:
    """Result of evaluating policy for a scope (global / project / agent / invocation)."""
    level: Level
    reasons: list = field(default_factory=list)      # short strings, which rule fired and why
    rules: list = field(default_factory=list)        # names of fired rules
    max_concurrency: Optional[int] = None            # effective concurrency cap
    min_interval_s: float = 0.0                      # spacing between new invocations
    deny_tools: list = field(default_factory=list)   # tool names denied at this level
    deny_spawn: bool = False                         # deny subagent / claude -p spawns
    downgrade_model: Optional[str] = None            # model to force for new work, if any
    checkpoint: bool = False                         # request checkpoint before stopping

    @property
    def allowed(self) -> bool:
        return self.level < Level.STOP

    def message(self) -> str:
        return "; ".join(self.reasons)[:400]

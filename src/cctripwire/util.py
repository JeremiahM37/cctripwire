"""Small shared helpers: clock, hashing, process liveness, identity."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

# Gateway environment contract (set on every child the gateway launches).
ENV_INVOCATION = "CCTRIPWIRE_INVOCATION_ID"   # id of the gateway invocation this process belongs to
ENV_ROOT = "CCTRIPWIRE_ROOT_ID"               # id of the outermost invocation in the tree
ENV_DEPTH = "CCTRIPWIRE_DEPTH"                # 1 for a top-level claude -p, 2 for its child, ...
ENV_AGENT = "CCTRIPWIRE_AGENT"                # optional agent label set by the user/orchestrator
ENV_PROJECT = "CCTRIPWIRE_PROJECT"            # optional project label override
ENV_DISABLE = "CCTRIPWIRE_DISABLE"            # "1" = hooks/gateway pass through (emergency off switch)
ENV_REAL_CLAUDE = "CCTRIPWIRE_REAL_CLAUDE"    # path of the real claude binary (shim mode)


def now() -> float:
    """Wall clock, shiftable in tests with CCTRIPWIRE_TIME_OFFSET (seconds)."""
    return time.time() + float(os.environ.get("CCTRIPWIRE_TIME_OFFSET", "0") or 0)


RESET_TOLERANCE_S = 300.0


def same_reset(a: float | None, b: float | None, tol: float = RESET_TOLERANCE_S) -> bool:
    """True when two reported reset times name the same window period. Providers report
    them with sub-second (sometimes jittering) precision, so exact equality is too strict."""
    if a is None or b is None:
        return a is b
    return abs(a - b) <= tol


def short_hash(obj: Any, n: int = 16) -> str:
    if not isinstance(obj, (str, bytes)):
        obj = json.dumps(obj, sort_keys=True, default=str)
    if isinstance(obj, str):
        obj = obj.encode("utf-8", "replace")
    return hashlib.sha256(obj).hexdigest()[:n]


_WS = re.compile(r"\s+")


def normalize_prompt(text: str) -> str:
    """Collapse whitespace and case so trivially different prompts compare equal."""
    return _WS.sub(" ", (text or "").strip().lower())


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); treat it as dead.
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            state = f.read().rsplit(b")", 1)[1].split()[0]
        return state != b"Z"
    except (OSError, IndexError):
        return True


def project_name(cwd: str | None) -> str:
    env = os.environ.get(ENV_PROJECT)
    if env:
        return env
    if not cwd:
        return "?"
    return Path(cwd).name or cwd


def agent_name(cwd: str | None = None, default: str = "") -> str:
    return os.environ.get(ENV_AGENT) or default or project_name(cwd)


def disabled() -> bool:
    return os.environ.get(ENV_DISABLE, "") in ("1", "true", "yes")


def fmt_duration(s: float | None) -> str:
    if s is None:
        return "—"
    s = int(max(0, s))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"


def fmt_tokens(n: float | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit, div in (("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}"

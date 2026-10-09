"""Status-line hook: record the official rate_limits and print a one-line summary.

Claude Code pipes session JSON to the status-line command on stdin. Absent rate limits
mean "unknown", never 0%. This module never raises: any failure prints a minimal line.
"""
from __future__ import annotations

import json
import subprocess
from typing import Optional

from .. import util
from ..engine import decide, invalidate
from ..forecast import window_state
from ..models import FIVE_HOUR, SEVEN_DAY, QuotaSample
from ..util import fmt_duration

DEDUPE_S = 60
CHAIN_TIMEOUT_S = 2
FALLBACK = "⛨ cctripwire"
WINDOWS = ((FIVE_HOUR, "five_hour"), (SEVEN_DAY, "seven_day"))


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _record_limits(store, data: dict, now: float) -> bool:
    """Store exact samples from rate_limits; identical readings within 60 s are skipped."""
    rl = data.get("rate_limits")
    if not isinstance(rl, dict):
        return False
    stored = False
    for window, key in WINDOWS:
        item = rl.get(key)
        used = _num(item.get("used_percentage")) if isinstance(item, dict) else None
        if used is None:
            continue
        resets = _num(item.get("resets_at"))
        last = store.latest_quota(window, source="statusline")
        if last and now - last.ts < DEDUPE_S and last.used_pct == used and last.resets_at == resets:
            continue
        store.add_quota_sample(QuotaSample(ts=now, window=window, used_pct=used, resets_at=resets,
                                           source="statusline", exact=True))
        stored = True
    return stored


def _line(store, cfg, now: float, project: str, agent: str) -> str:
    """Compact summary, e.g. '⛨ 5h 42% ↗9%/h ~2h10m · 7d 31% · OK'."""
    parts = []
    five = window_state(store, cfg, FIVE_HOUR, now)
    seven = window_state(store, cfg, SEVEN_DAY, now)
    if five.used_pct is None:
        parts.append("5h —")
    else:
        s = f"5h {five.used_pct:.0f}%"
        if five.burn_pct_per_hour and five.burn_pct_per_hour > 0:
            s += f" ↗{five.burn_pct_per_hour:.0f}%/h"
        if five.eta_s is not None:
            s += f" ~{fmt_duration(five.eta_s)}"
        parts.append(s)
    parts.append("7d —" if seven.used_pct is None else f"7d {seven.used_pct:.0f}%")
    _, dec = decide(store, cfg, project=project, agent=agent, now=now, max_age=0)
    parts.append(dec.level.name)
    return "⛨ " + " · ".join(parts)


def _chain(cmd: str, stdin_text: str) -> str:
    """First line of the previous status-line command's output, or '' on any failure."""
    try:
        res = subprocess.run(cmd, shell=True, input=stdin_text, capture_output=True, text=True,
                             timeout=CHAIN_TIMEOUT_S)
        lines = res.stdout.splitlines()
        return lines[0] if lines else ""
    except Exception:  # noqa: BLE001 - a broken chained command must not break ours
        return ""


def handle(stdin_text: str, store, cfg, now: float | None = None) -> str:
    """Process one status-line invocation and return the text to print."""
    try:
        t = util.now() if now is None else now
        data = json.loads(stdin_text) if stdin_text and stdin_text.strip() else {}
        if not isinstance(data, dict):
            data = {}
        ws = data.get("workspace")
        cwd = data.get("cwd") or (ws.get("current_dir") if isinstance(ws, dict) else None)
        cwd = cwd if isinstance(cwd, str) else None
        sid = data.get("session_id")
        project = util.project_name(cwd)
        agent = util.agent_name(cwd)
        if isinstance(sid, str) and sid:
            store.upsert_session(sid, project=project, cwd=cwd, agent=agent,
                                 transcript_path=data.get("transcript_path"), last_seen=t)
        if cfg.get("sources.statusline", True) and _record_limits(store, data, t):
            invalidate(store)
        line = _line(store, cfg, t, project, agent)
    except Exception:  # noqa: BLE001 - the status line must always print something
        line = FALLBACK
    chained = cfg.get("sources.statusline_chain", "")
    if chained:
        first = _chain(chained, stdin_text or "")
        if first:
            return f"{first}\n{line}"
    return line


__all__ = ["handle"]

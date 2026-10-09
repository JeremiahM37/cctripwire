"""Claude Code hook entry point: `cctripwire hook` reads one hook event on stdin.

Fail open: any exception logs to state_dir/hook-errors.log and the hook exits 0 with
no output, so a broken cctripwire never blocks the agent. Behaviour per event is in
docs/ARCHITECTURE.md. Heavy modules (engine, quota) are imported lazily to keep each
hook call fast.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import config as _config
from . import util
from .loops import LoopDetector
from .models import Decision, Detection, Level
from .store import Store

CHECKPOINT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "TodoWrite"})
SPAWN_TOOLS = frozenset({"Agent", "Task"})
SUBAGENT_WINDOW_S = 1800
MSG_MAX = 300
REASON_MAX = 120
# `claude` invoked with -p/--print, in command position (start, after ; & | ( whitespace / or a backtick).
# Quoted strings are blanked first, so `grep -r "claude -p"` does not match.
CLAUDE_P_RE = re.compile(r"(^|[;&|(\s/`])claude\b[^\n]*\s(-p|--print)\b")
_QUOTED_RE = re.compile(r"\"[^\"\n]*\"|'[^'\n]*'")

GUIDANCE = {
    Level.WARN: "Prefer cheaper steps: reuse results you already have, avoid re-reading large files, "
                "no new subagents unless essential.",
}


@dataclass
class HookResult:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


# -- output helpers -------------------------------------------------------------
def _msg(text: str) -> str:
    t = " ".join(str(text).split())
    if not t.startswith("cctripwire:"):
        t = "cctripwire: " + t
    return t[:MSG_MAX]


def _pre_deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": _msg(reason)}}


def _ctx(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": _msg(text)}}


def _reason(dec: Decision) -> str:
    return (dec.message() or f"usage {dec.level}")[:REASON_MAX]


def _status(dec: Decision, cfg) -> str:
    text = f"usage {str(dec.level).upper()}: {_reason(dec)}."
    if dec.level == Level.WARN:
        text += " " + GUIDANCE[Level.WARN]
    elif dec.level == Level.THROTTLE:
        text += (f" Run at most {dec.max_concurrency} subagent/claude -p at a time; batch work; "
                 "avoid retries of failing commands.")
    elif dec.level == Level.RESTRICT:
        text += (f" Expensive tools ({', '.join(dec.deny_tools) or 'none'}) are disabled; finish the "
                 "current task directly, then stop.")
    elif dec.level >= Level.STOP:
        text += " Checkpoint progress and end the turn."
    return text


# -- shared plumbing ------------------------------------------------------------
def _session(p: dict, cfg, store: Store, now: float) -> tuple[str, str, str]:
    sid = p.get("session_id")
    if not sid:
        raise ValueError("hook payload has no session_id")
    cwd = p.get("cwd") or ""
    project = util.project_name(cwd)
    agent = util.agent_name(cwd, default=str(cfg.get("general.agent_name", "") or ""))
    store.upsert_session(sid, cwd=cwd or None, project=project, agent=agent,
                         transcript_path=p.get("transcript_path") or None,
                         invocation_id=os.environ.get(util.ENV_INVOCATION) or None,
                         pid=os.getppid(), last_seen=now)
    return sid, project, agent


def _scan(p: dict, store: Store, cfg, now: float) -> None:
    """Incremental transcript scan. Non-fatal: a scan problem must not hide a decision."""
    tp = p.get("transcript_path")
    if not tp or not Path(tp).is_file():
        return
    try:
        from .quota import transcripts
        transcripts.scan_file(store, Path(tp), invocation_id=os.environ.get(util.ENV_INVOCATION) or None)
    except Exception as e:  # noqa: BLE001
        _log_error(cfg, "transcript-scan", e)


def _decide(store: Store, cfg, project: str, agent: str, now: float) -> Decision:
    from . import engine
    return engine.decide(store, cfg, project=project, agent=agent, now=now)[1]


def _loops(store: Store, cfg, now: float) -> LoopDetector:
    return LoopDetector(store, cfg, now=now)


def _is_claude_p(command: str) -> bool:
    return bool(CLAUDE_P_RE.search(_QUOTED_RE.sub('""', command or "")))


def _response_failed(resp) -> bool:
    if not isinstance(resp, dict):
        return False
    if resp.get("is_error") is True or resp.get("interrupted") is True:
        return True
    for key in ("returncode", "exit_code"):
        v = resp.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v != 0:
            return True
    return False


def _resp_text(resp) -> str:
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp[:200]
    if isinstance(resp, dict):
        for key in ("error", "stderr", "content", "output"):
            if resp.get(key):
                return str(resp[key])[:200]
    try:
        return json.dumps(resp, default=str)[:200]
    except (TypeError, ValueError):
        return str(resp)[:200]


def _in_flight(store: Store, sid: str, now: float) -> int:
    row = store.conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN kind='subagent_start' THEN 1 ELSE -1 END),0) FROM events "
        "WHERE session_id=? AND kind IN ('subagent_start','subagent_stop') AND ts>=?",
        (sid, now - SUBAGENT_WINDOW_S)).fetchone()
    return max(0, int(row[0] or 0))


def _notify(store: Store, cfg, sid: str, dec: Decision, dets: list[Detection], now: float,
            event: str) -> Optional[dict]:
    """Tell the agent about a detection or a level rise. Re-notify at most every interval."""
    key = f"notified:{sid}"
    last = store.kv_get(key) or {}
    last_level = int(last.get("level", 0) or 0)
    last_ts = float(last.get("ts", 0) or 0)
    interval = float(cfg.get("levels.warn.notify_interval_s", 600))
    rose = int(dec.level) > last_level
    due = bool(dets) and now - last_ts >= interval
    if not (rose or due):
        if dec.level == Level.OK and last_level:
            store.kv_set(key, {"level": 0, "ts": last_ts})
        return None
    store.kv_set(key, {"level": int(dec.level), "ts": now})
    lines = [_status(dec, cfg)] if dec.level >= Level.WARN else []
    if cfg.get("hooks.enforce_loops", True):     # off: detections are only recorded
        lines += [d.advice for d in dets[:2]]
    if not lines:
        return None
    return _ctx(event, " ".join(lines))


# -- event handlers (p, cfg, store, now) -> output dict or None ------------------
def _h_session_start(p, cfg, store, now):
    sid, project, agent = _session(p, cfg, store, now)
    dets: list[Detection] = []
    if p.get("source") == "compact":
        dets = _loops(store, cfg, now).on_compact(sid)
    else:
        store.add_event(sid, "session_start", ts=now)
    _scan(p, store, cfg, now)
    dec = _decide(store, cfg, project, agent, now)
    lines = [_status(dec, cfg)] if dec.level >= Level.WARN else []
    if cfg.get("hooks.enforce_loops", True):
        lines += [d.advice for d in dets]
    return _ctx("SessionStart", " ".join(lines)) if lines else None


def _h_user_prompt(p, cfg, store, now):
    sid, project, agent = _session(p, cfg, store, now)
    dets = _loops(store, cfg, now).on_prompt(sid, p.get("prompt") or "")
    blocking = [d for d in dets if d.severity >= Level.THROTTLE]
    if blocking and cfg.get("hooks.enforce_loops", True):
        return {"decision": "block", "reason": _msg(blocking[0].advice)}
    if not cfg.get("hooks.block_prompts_at_stop", True):
        return None
    dec = _decide(store, cfg, project, agent, now)
    if dec.level >= Level.STOP:
        return {"decision": "block", "reason": _msg(
            f"usage at STOP ({_reason(dec)}). Set CCTRIPWIRE_DISABLE=1 or run "
            "`cctripwire override ok --for 30m` to continue.")}
    return None


def _h_pre_tool(p, cfg, store, now):
    sid, project, agent = _session(p, cfg, store, now)
    tool = str(p.get("tool_name") or "")
    tin = p.get("tool_input") or {}
    dec = _decide(store, cfg, project, agent, now)

    if dec.level >= Level.STOP:
        n = store.bump_session_counter(sid)
        allowance = int(cfg.level_cfg("stop").get("checkpoint_tool_calls", 3)) if dec.checkpoint else 0
        if n > allowance:
            return {"continue": False, "stopReason": _msg(
                f"usage at STOP ({_reason(dec)}). Halting this run; checkpoint if you have not written one.")}
        if tool in CHECKPOINT_TOOLS:
            return None
        ck = cfg.level_cfg("stop").get("checkpoint_file", "CHECKPOINT.md")
        return _pre_deny(f"{_reason(dec)}. Stop now: write a ≤15-line checkpoint (done / next steps / open "
                         f"questions) to {ck}, then end your turn.")
    # Recovered from STOP: give the next STOP episode its full checkpoint allowance again.
    store.reset_session_counter(sid)

    if dec.level >= Level.RESTRICT:
        if tool in dec.deny_tools:
            return _pre_deny(f"{_reason(dec)}. {tool} is restricted; do the work directly and sequentially.")
        if tool == "Bash" and dec.deny_spawn and _is_claude_p(str(tin.get("command", ""))):
            return _pre_deny(f"{_reason(dec)}. Reuse results already in this session; do not start new "
                             "claude -p runs.")

    if dec.level >= Level.THROTTLE and tool in SPAWN_TOOLS and dec.max_concurrency is not None:
        running = _in_flight(store, sid, now)
        if running >= dec.max_concurrency:
            return _pre_deny(f"{running} subagent(s) already running (limit {dec.max_concurrency}). "
                             "Run subagents one at a time: wait for the running one to finish.")

    if tool == "Bash" and _is_claude_p(str(tin.get("command", ""))):
        from . import breaker
        rec = breaker.tripped(store, now) if breaker.enabled(cfg) else None
        if rec:
            return _pre_deny(f"runaway breaker tripped: {rec.get('reason')}. {breaker.ADVICE}")

    blocking = [d for d in _loops(store, cfg, now).on_tool_pre(sid, tool, tin)
                if d.severity >= Level.THROTTLE]
    if blocking and cfg.get("hooks.enforce_loops", True):
        return _pre_deny(blocking[0].advice)
    return None


def _h_post_tool(p, cfg, store, now, failure: bool = False):
    sid, project, agent = _session(p, cfg, store, now)
    tool = str(p.get("tool_name") or "")
    tin = p.get("tool_input") or {}
    if failure:
        failed, err = True, str(p.get("error") or _resp_text(p.get("tool_response")))
    else:
        resp = p.get("tool_response")
        failed = _response_failed(resp)
        err = _resp_text(resp) if failed else None
        _scan(p, store, cfg, now)
    loops = _loops(store, cfg, now)
    dets = loops.on_tool_result(sid, tool, tin, ok=not failed, error=err)
    # In-flight subagents are counted from SubagentStart/SubagentStop only: a background
    # Agent call returns (PostToolUse) long before the subagent actually finishes.
    dets += loops.check_token_spike(sid)
    dec = _decide(store, cfg, project, agent, now)
    return _notify(store, cfg, sid, dec, dets, now,
                   "PostToolUseFailure" if failure else "PostToolUse")


def _h_pre_compact(p, cfg, store, now):
    sid, _, _ = _session(p, cfg, store, now)
    dets = _loops(store, cfg, now).on_compact(sid)   # never blocks: systemMessage only
    return {"systemMessage": _msg(dets[0].advice)} if dets else None


def _h_subagent(kind: str):
    def handler(p, cfg, store, now):
        sid, _, _ = _session(p, cfg, store, now)
        store.add_event(sid, kind, ts=now, agent_id=p.get("agent_id"), tool=p.get("agent_type"))
        return None
    return handler


def _h_stop(p, cfg, store, now):
    sid, _, _ = _session(p, cfg, store, now)
    store.add_event(sid, "stop", ts=now)
    _scan(p, store, cfg, now)
    return None   # never blocks: agents must be able to stop


def _h_session_end(p, cfg, store, now):
    sid, _, _ = _session(p, cfg, store, now)
    store.upsert_session(sid, ended=now, status="ended")
    return None


_HANDLERS: dict[str, Callable] = {
    "SessionStart": _h_session_start,
    "UserPromptSubmit": _h_user_prompt,
    "PreToolUse": _h_pre_tool,
    "PostToolUse": _h_post_tool,
    "PostToolUseFailure": lambda p, c, s, n: _h_post_tool(p, c, s, n, failure=True),
    "PreCompact": _h_pre_compact,
    "SubagentStart": _h_subagent("subagent_start"),
    "SubagentStop": _h_subagent("subagent_stop"),
    "Stop": _h_stop,
    "SessionEnd": _h_session_end,
}


# -- entry points -----------------------------------------------------------------
def _log_error(cfg, event: str, exc: BaseException) -> None:
    try:
        c = cfg if cfg is not None else _config.Config()
        d = c.state_dir
        d.mkdir(parents=True, exist_ok=True)
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-1500:]
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(d / "hook-errors.log", "a", encoding="utf-8") as f:
            f.write(f"{stamp} {event} {type(exc).__name__}: {str(exc)[:300]}\n{tb}\n")
    except Exception:  # noqa: BLE001 - logging must never raise
        pass


def _record(store, payload: dict, event: str, out: dict) -> None:
    """Log enforcing outputs (deny / block / halt) to the intervention history."""
    hso = out.get("hookSpecificOutput") or {}
    if hso.get("permissionDecision") == "deny":
        action, reason = "deny_tool", f"{payload.get('tool_name')}: {hso.get('permissionDecisionReason', '')}"
    elif out.get("decision") == "block":
        action, reason = "block_prompt", out.get("reason", "")
    elif out.get("continue") is False:
        action, reason = "halt_session", out.get("stopReason", "")
    else:
        return   # context-only output is advice, not an intervention
    try:
        store.add_intervention(action, f"session:{payload.get('session_id')}", reason, policy=f"hook:{event}")
    except Exception:  # noqa: BLE001 - logging must not break the hook
        pass


def run(stdin_text: str, cfg=None, store=None) -> HookResult:
    event = "?"
    try:
        if util.disabled():
            return HookResult()
        if cfg is None:
            cfg = _config.load()
        payload = json.loads(stdin_text)
        if not isinstance(payload, dict):
            raise ValueError("hook payload is not a JSON object")
        event = str(payload.get("hook_event_name") or "?")
        handler = _HANDLERS.get(event)
        if handler is None:
            return HookResult()
        if store is None:
            store = Store(cfg.db_path)
        out = handler(payload, cfg, store, util.now())
        if out:
            _record(store, payload, event, out)
        return HookResult(0, json.dumps(out), "") if out else HookResult()
    except Exception as e:  # noqa: BLE001 - fail open
        _log_error(cfg, event, e)
        return HookResult()


def main() -> int:
    try:
        text = sys.stdin.read()
    except Exception:  # noqa: BLE001
        text = ""
    res = run(text)
    if res.stdout:
        sys.stdout.write(res.stdout)
        sys.stdout.flush()
    if res.stderr:
        sys.stderr.write(res.stderr)
    return res.exit_code


if __name__ == "__main__":
    sys.exit(main())

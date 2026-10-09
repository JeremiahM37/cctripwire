"""Intervention helpers shared by the daemon, the CLI and the hooks.

Everything here is deterministic and never calls a model. The daemon decides
*what* to do; this module formats the human/agent-facing text, writes
checkpoints, records requests for the gateway to act on, and runs the
user-configured notify command.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .models import FIVE_HOUR, SEVEN_DAY, Decision, Level, Snapshot, WindowState
from .util import fmt_duration, now as _now

MSG_LIMIT = 300          # hard cap for anything sent to an agent (hook output)
ADVICE_LIMIT = 200       # Detection.advice contract
RESUME_PROMPT = "Continue from where you stopped; first re-read CHECKPOINT.md if present."
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# action -> (status written, statuses the request is valid from)
REQUEST_ACTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "suspend": ("suspend_requested", ("running",)),
    "resume": ("resume_requested", ("suspended", "suspend_requested")),
    "kill": ("kill_requested", ("running", "suspended", "queued", "suspend_requested", "resume_requested")),
}

_SHORT = {FIVE_HOUR: "5h", SEVEN_DAY: "7d"}


# -- status text ---------------------------------------------------------------

def _window_segment(name: str, ws: WindowState | None) -> str:
    label = _SHORT.get(name, name)
    if ws is None or ws.used_pct is None:
        return f"{label} ?"
    pct = f"{ws.used_pct:.0f}"
    seg = f"{label} {pct}% (exact)" if ws.exact else f"{label} ~{pct}%"
    if ws.burn_pct_per_hour is not None:
        seg += f" ↗{ws.burn_pct_per_hour:.0f}%/h"
    if ws.eta_s is not None:
        seg += f" ETA {fmt_duration(ws.eta_s)}"
    return seg


def status_line(snap: Snapshot | None, dec: Decision | None = None) -> str:
    """e.g. ``5h 42% (exact) ↗9%/h ETA 2h10m · 7d ~31% · level WARN``.

    Exact readings are marked ``(exact)``, estimates get a ``~`` prefix, and an
    unknown window is ``5h ?`` (never a silent 0%).
    """
    windows = (snap.windows if snap is not None else None) or {}
    names = [FIVE_HOUR, SEVEN_DAY] + sorted(n for n in windows if n not in (FIVE_HOUR, SEVEN_DAY))
    parts = [_window_segment(n, windows.get(n)) for n in names]
    if dec is not None:
        parts.append(f"level {dec.level.name}")
    return " · ".join(parts)


def guidance(dec: Decision, cfg: Config) -> str:
    """One or two sentences telling the agent how to behave at the current level."""
    lvl = dec.level
    if lvl >= Level.STOP:
        f = cfg.level_cfg("stop").get("checkpoint_file", "CHECKPOINT.md")
        return (f"Stop now: write a checkpoint of at most 15 lines (done / next steps / open questions) "
                f"to {f}, then end your turn.")
    if lvl >= Level.RESTRICT:
        tools = ", ".join(dec.deny_tools) or "none"
        return f"Expensive tools ({tools}) are disabled; finish the current task directly, then stop."
    if lvl >= Level.THROTTLE:
        n = dec.max_concurrency if dec.max_concurrency else 1
        return (f"Run at most {n} subagent/claude -p at a time; batch work; "
                f"avoid retries of failing commands.")
    if lvl >= Level.WARN:
        return ("Prefer cheaper steps: reuse results you already have, avoid re-reading large files, "
                "no new subagents unless essential.")
    return ""


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _top_detection(detections: Iterable[Any] | None) -> Any | None:
    best = None
    for d in detections or []:
        sev = int(_field(d, "severity", 0) or 0)
        if best is None or sev > best[0]:          # first of equal severity wins
            best = (sev, d)
    return best[1] if best else None


def agent_message(dec: Decision, detections: Iterable[Any] | None, cfg: Config, limit: int = MSG_LIMIT) -> str:
    """``cctripwire: LEVEL: reasons. advice guidance`` within ``limit`` characters.

    The top detection's advice is never dropped: guidance goes first, then the
    reasons are shortened. Returns "" when there is nothing to say at OK.
    """
    top = _top_detection(detections)
    advice = str(_field(top, "advice", "") or "").strip()[:ADVICE_LIMIT] if top is not None else ""
    if dec.level == Level.OK and not advice:
        return ""
    level = dec.level.name
    reasons = "; ".join(str(r) for r in dec.reasons)
    guide = guidance(dec, cfg)

    def build(r: str, tail: str) -> str:
        head = f"cctripwire: {level}: {r}." if r else f"cctripwire: {level}."
        return " ".join(x for x in (head, tail) if x)

    msg = build(reasons, " ".join(x for x in (advice, guide) if x))
    if len(msg) > limit:
        msg = build(reasons, advice)
    if len(msg) > limit:
        msg = build("", advice)
        for k in range(len(reasons) - 1, 0, -1):
            cand = build(reasons[:k].rstrip() + "…", advice)
            if len(cand) <= limit:
                msg = cand
                break
    return msg[:limit]


# -- checkpoints -----------------------------------------------------------------

def _checkpoint_dir(cfg: Config) -> Path:
    return cfg.state_dir / "checkpoints"


def _checkpoint_path(cfg: Config, inv_id: str) -> Path:
    if not _ID_RE.fullmatch(str(inv_id)):
        raise ValueError(f"invalid invocation id: {inv_id!r}")
    return _checkpoint_dir(cfg) / f"{inv_id}.json"


def _as_list(argv: Any) -> list:
    if argv is None:
        return []
    if isinstance(argv, str):
        try:
            v = json.loads(argv)
            return v if isinstance(v, list) else [argv]
        except ValueError:
            return [argv]
    return list(argv)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def resume_command(session_id: str) -> str:
    return f'cctripwire run -- claude -p --resume {shlex.quote(session_id)} "{RESUME_PROMPT}"'


def write_checkpoint(cfg: Config, inv: dict, reason: str) -> Path:
    """Persist what is needed to continue an invocation later. Returns the file path.

    `inv` is an invocations row, optionally with `cwd` added by the caller (the
    invocations table does not store it; sessions does).
    """
    inv_id = str(inv["id"])
    path = _checkpoint_path(cfg, inv_id)
    sid = inv.get("session_id")
    data = {
        "id": inv_id,
        "argv": _as_list(inv.get("argv")),
        "cwd": inv.get("cwd"),
        "project": inv.get("project"),
        "agent": inv.get("agent"),
        "model": inv.get("model"),
        "session_id": sid,
        "status": inv.get("status"),
        "tokens": {
            "input": int(inv.get("tokens_in") or 0),
            "output": int(inv.get("tokens_out") or 0),
            "weighted": float(inv.get("weighted") or 0.0),
        },
        "reason": reason,
        "created": _now(),
    }
    if sid:
        data["resume_cmd"] = resume_command(str(sid))
    _write_json(path, data)
    return path


def list_checkpoints(cfg: Config) -> list[dict]:
    d = _checkpoint_dir(cfg)
    if not d.is_dir():
        return []
    out: list[dict] = []
    for p in d.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data.setdefault("id", p.stem)
            out.append(data)
    out.sort(key=lambda x: float(x.get("created") or 0), reverse=True)
    return out


def resume_checkpoint(cfg: Config, inv_id: str, dry_run: bool = False, store=None):
    """Continue a checkpointed session.

    dry_run returns the argv that would run (list[str]). Otherwise the command is
    started in its own session, the checkpoint is marked resumed, and the Popen
    object is returned.
    """
    path = _checkpoint_path(cfg, inv_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    cmd = data.get("resume_cmd")
    if not cmd:
        raise ValueError(f"checkpoint {inv_id} has no session id, so it cannot be resumed")
    argv = shlex.split(cmd)
    if dry_run:
        return argv
    cwd = data.get("cwd")
    if not (cwd and os.path.isdir(cwd)):
        cwd = None
    proc = subprocess.Popen(argv, cwd=cwd, start_new_session=True)
    data["resumed"] = _now()
    data["resumed_pid"] = proc.pid
    _write_json(path, data)
    if store is not None:
        store.add_intervention("resume_checkpoint", inv_id, "resumed from checkpoint")
    return proc


# -- requests to the gateway ---------------------------------------------------

def request(store, inv_id: str, action: str, *, reason: str = "", level: Level | int = 0) -> bool:
    """Ask the gateway process that owns `inv_id` to suspend, resume or kill it.

    Sets the status to `<action>_requested` and records an intervention. The
    gateway performs the signal and moves the row on. Returns False (and writes
    nothing) if the invocation is missing or not in a state that allows the action.
    """
    if action not in REQUEST_ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    new_status, allowed = REQUEST_ACTIONS[action]
    with store.tx(immediate=True):
        inv = store.get_invocation(inv_id)
        if inv is None or inv["status"] not in allowed:
            return False
        store.update_invocation(inv_id, status=new_status)
        store.add_intervention(action, inv_id, reason or f"{action} requested", level=level)
    return True


# -- notification --------------------------------------------------------------

def notify(cfg: Config, title: str, body: str) -> bool:
    """Run `daemon.notify_command` (no shell) with CCTRIPWIRE_TITLE / CCTRIPWIRE_BODY set.

    Returns True if the command exited 0. Never raises.
    """
    try:
        cmd = str(cfg.get("daemon.notify_command", "") or "").strip()
        if not cmd:
            return False
        env = dict(os.environ, CCTRIPWIRE_TITLE=str(title), CCTRIPWIRE_BODY=str(body))
        done = subprocess.run(shlex.split(cmd), env=env, timeout=5, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return done.returncode == 0
    except Exception:  # noqa: BLE001 - notification must never break enforcement
        return False

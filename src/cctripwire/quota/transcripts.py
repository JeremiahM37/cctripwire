"""Read Claude Code transcripts (~/.claude/projects/**/*.jsonl) into token_events.

Claude Code writes one JSONL line per content block of an API response, and each
line repeats the same message.id, requestId and usage. Each response is counted once.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .. import util
from .estimator import weight, counts

DEFAULT_MAX_AGE_S = 8 * 86400


def _parse_ts(value, default: float) -> float:
    """ISO 8601 (with Z or offset) to unix seconds; falls back to `default`."""
    if not isinstance(value, str):
        return default
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return default
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _record(line: bytes) -> Optional[dict]:
    """Parse one assistant line into a token record, or None if it is not one."""
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None
    msg = obj.get("message")
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(usage, dict):
        return None
    mid = msg.get("id")
    key = f"{mid}:{obj.get('requestId')}" if mid else obj.get("uuid")
    return {"obj": obj, "usage": usage, "model": msg.get("model"), "key": key}


def scan_file(store, path, *, project: str | None = None, agent: str | None = None,
              invocation_id: str | None = None) -> int:
    """Ingest new assistant usage lines from one transcript. Returns the number of new events.

    Resumes from the stored offset. A rotated (new inode) or truncated file restarts at 0.
    A final line without a newline is consumed only if it is complete JSON, so a line that
    is still being written is picked up on the next scan.
    """
    p = Path(path)
    try:
        st = os.stat(p)
    except OSError:
        return 0
    key = str(p)
    old_inode, old_off = store.get_offset(key)
    off = 0 if (st.st_ino != old_inode or st.st_size < old_off) else old_off
    new = 0
    consumed = off
    with store.tx() as c:
        with open(p, "rb") as f:
            f.seek(off)
            for raw in f:
                line = raw.rstrip(b"\r\n")
                if not raw.endswith(b"\n") and not _is_json(line):
                    break                                  # partial line: wait for the rest
                consumed += len(raw)
                rec = _record(line)
                if rec is None:
                    continue
                obj = rec["obj"]
                cwd = obj.get("cwd")
                proj = project or (Path(cwd).name if isinstance(cwd, str) and cwd else None)
                i, o, cc, cr = counts(rec["usage"])
                ag = agent or util.agent_name(cwd if isinstance(cwd, str) else None)
                if store.add_token_event(
                        ts=_parse_ts(obj.get("timestamp"), util.now()), session_id=obj.get("sessionId"),
                        project=proj, agent=ag, model=rec["model"], input=i, output=o, cache_create=cc,
                        cache_read=cr, weighted=weight(rec["model"], rec["usage"]), msg_key=rec["key"],
                        invocation_id=invocation_id):
                    new += 1
        store.set_offset(key, st.st_ino, consumed, st.st_mtime)
    return new


def _is_json(line: bytes) -> bool:
    try:
        json.loads(line)
        return True
    except ValueError:
        return False


def scan_all(store, cfg, *, max_age_s: float = DEFAULT_MAX_AGE_S) -> int:
    """Scan every transcript under claude_dir/projects modified within max_age_s."""
    root = cfg.claude_dir / "projects"
    if not root.is_dir():
        return 0
    cutoff = util.now() - max_age_s
    total = 0
    for p in root.rglob("*.jsonl"):
        try:
            if p.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        total += scan_file(store, p)
    return total


def find_session_file(cfg, session_id: str) -> Optional[Path]:
    """Path of the top-level transcript for a session id, or None."""
    if not session_id or "/" in session_id or session_id in (".", ".."):
        return None
    root = cfg.claude_dir / "projects"
    return next(root.glob(f"*/{session_id}.jsonl"), None) if root.is_dir() else None


__all__ = ["scan_file", "scan_all", "find_session_file"]

"""Process supervision: find claude processes and signal their process groups.

Everything here reads /proc directly (Linux only, stdlib only). Signal helpers
tolerate ESRCH/EPERM: a group that is already gone is not an error.
"""
from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

ENV_INVOCATION = "CCTRIPWIRE_INVOCATION_ID"
_NODE_NAMES = ("node", "nodejs", "bun")


@dataclass
class ProcInfo:
    pid: int
    ppid: int
    pgid: int
    state: str
    cmdline: list = field(default_factory=list)
    start_time: int = 0          # clock ticks since boot
    rss_kb: int = 0
    cpu_ticks: int = 0           # utime + stime
    invocation_id: str | None = None
    is_print: bool = False


def read_stat(pid: int) -> dict | None:
    """Parse /proc/<pid>/stat. Returns None if the process has vanished."""
    try:
        data = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    try:
        rest = data.rsplit(b")", 1)[1].split()
        # rest[0] is field 3 (state); field N lives at rest[N-3].
        return {
            "state": rest[0].decode(),
            "ppid": int(rest[1]),
            "pgid": int(rest[2]),
            "cpu": int(rest[11]) + int(rest[12]),
            "start": int(rest[19]),
            "rss_kb": int(rest[21]) * (os.sysconf("SC_PAGE_SIZE") // 1024),
        }
    except (IndexError, ValueError):
        return None


def _cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    return [p.decode("utf-8", "replace") for p in parts]


def _environ_value(pid: int, key: str) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None   # other user's process, or gone
    prefix = key.encode() + b"="
    for item in raw.split(b"\0"):
        if item.startswith(prefix):
            return item[len(prefix):].decode("utf-8", "replace")
    return None


def is_claude_argv(argv: list[str]) -> bool:
    """True for a `claude` binary, or a node/bun process running a claude cli entry point."""
    if not argv:
        return False
    base = os.path.basename(argv[0])
    if base.startswith("python") and any("cctripwire" in a for a in argv):
        return False          # our own gateway / daemon / shim-exec'd python
    if base == "claude":
        return True
    if base in _NODE_NAMES:
        return any(os.path.basename(a) in ("claude", "cli.js") and "claude" in a for a in argv[1:3])
    return False


def list_claude_processes() -> list[ProcInfo]:
    me = os.getpid()
    out: list[ProcInfo] = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == me:
            continue
        argv = _cmdline(pid)
        if not is_claude_argv(argv):
            continue
        st = read_stat(pid)
        if st is None or st["state"] == "Z":
            continue
        out.append(ProcInfo(
            pid=pid, ppid=st["ppid"], pgid=st["pgid"], state=st["state"], cmdline=argv,
            start_time=st["start"], rss_kb=st["rss_kb"], cpu_ticks=st["cpu"],
            invocation_id=_environ_value(pid, ENV_INVOCATION),
            is_print=("-p" in argv or "--print" in argv),
        ))
    return out


def process_tree(procs: list[ProcInfo]) -> dict[int, list[int]]:
    """pid -> list of direct child pids, restricted to the given processes."""
    tree: dict[int, list[int]] = {p.pid: [] for p in procs}
    for p in procs:
        if p.ppid in tree:
            tree[p.ppid].append(p.pid)
    return tree


def bypassers(procs: list[ProcInfo]) -> list[ProcInfo]:
    """Print-mode claude processes that were not started through the gateway."""
    return [p for p in procs if p.is_print and not p.invocation_id]


def group_members(pgid: int) -> list[int]:
    """Live (non-zombie) pids in a process group."""
    if pgid is None or pgid <= 1:
        return []
    out = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        st = read_stat(int(name))
        if st and st["pgid"] == pgid and st["state"] != "Z":
            out.append(int(name))
    return out


def group_alive(pgid: int | None) -> bool:
    return bool(group_members(pgid)) if pgid else False


def signal_group(pgid: int | None, sig: int) -> bool:
    """Send a signal to a process group. Returns False if it did not exist or was not ours."""
    if pgid is None or pgid <= 1:
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError, OverflowError):
        return False


def terminate_group(pgid: int | None, grace_s: float = 10.0) -> bool:
    """SIGCONT (so stopped members can act on it), SIGTERM, wait up to grace_s, then SIGKILL.

    Returns True if the group had live members when called.
    """
    if not group_alive(pgid):
        return False
    signal_group(pgid, signal.SIGCONT)
    signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace_s)
    while group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if group_alive(pgid):
        signal_group(pgid, signal.SIGKILL)
        deadline = time.monotonic() + 2.0
        while group_alive(pgid) and time.monotonic() < deadline:
            time.sleep(0.02)
    return True


def suspend(pgid: int | None) -> bool:
    return signal_group(pgid, signal.SIGSTOP)


def resume(pgid: int | None) -> bool:
    return signal_group(pgid, signal.SIGCONT)

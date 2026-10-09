"""procs.py: /proc scanning and process-group signalling. Uses real short-lived processes."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from cctripwire import procs


@pytest.fixture
def children():
    started: list[subprocess.Popen] = []

    def spawn(argv, **kw):
        kw.setdefault("start_new_session", True)
        p = subprocess.Popen(argv, **kw)
        started.append(p)
        return p

    yield spawn
    for p in started:
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_read_stat_for_self():
    st = procs.read_stat(os.getpid())
    assert st is not None
    assert st["pgid"] == os.getpgid(0)
    assert st["ppid"] == os.getppid()
    assert st["rss_kb"] > 0
    assert procs.read_stat(2_000_000_000) is None


def test_is_claude_argv_rules():
    assert procs.is_claude_argv(["/usr/bin/claude", "-p", "hi"])
    assert procs.is_claude_argv(["claude"])
    assert procs.is_claude_argv(["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js", "-p"])
    assert not procs.is_claude_argv(["/bin/sh", "/home/x/shim/claude", "-p"])
    assert not procs.is_claude_argv(["python3", "-c", "import cctripwire", "claude"])
    assert not procs.is_claude_argv(["vim", "claude.md"])
    assert not procs.is_claude_argv([])


def test_list_claude_processes_reads_invocation_and_print_flag(children):
    # `exec -a claude sh ...` gives a process whose argv[0] is "claude".
    env = dict(os.environ, CCTRIPWIRE_INVOCATION_ID="inv42")
    p = children(["bash", "-c", "exec -a claude sh -c 'sleep 30' x -p hi"], env=env)
    assert _wait(lambda: any(i.pid == p.pid for i in procs.list_claude_processes()))
    info = next(i for i in procs.list_claude_processes() if i.pid == p.pid)
    assert info.invocation_id == "inv42"
    assert info.is_print is True
    assert info.pgid == p.pid
    assert info.rss_kb > 0
    assert p.pid in procs.process_tree(procs.list_claude_processes())


def test_bypassers_are_print_claude_without_invocation(children):
    env = {k: v for k, v in os.environ.items() if k != "CCTRIPWIRE_INVOCATION_ID"}
    p = children(["bash", "-c", "exec -a claude sh -c 'sleep 30' x -p bypass"], env=env)
    assert _wait(lambda: any(i.pid == p.pid for i in procs.list_claude_processes()))
    byp = procs.bypassers(procs.list_claude_processes())
    assert any(b.pid == p.pid for b in byp)


def test_own_python_processes_are_ignored(children):
    p = children([sys.executable, "-c", "import time; time.sleep(30)  # cctripwire gateway"])
    time.sleep(0.2)
    assert all(i.pid != p.pid for i in procs.list_claude_processes())


def test_signal_missing_group_is_tolerated():
    assert procs.signal_group(999_999_999, signal.SIGTERM) is False
    assert procs.signal_group(None, signal.SIGTERM) is False
    assert procs.terminate_group(999_999_999, grace_s=0.1) is False
    assert procs.suspend(999_999_999) is False and procs.resume(999_999_999) is False


def test_terminate_group_stops_a_running_group(children):
    p = children(["sleep", "30"])
    assert procs.group_alive(p.pid)
    t0 = time.monotonic()
    assert procs.terminate_group(p.pid, grace_s=3) is True
    assert time.monotonic() - t0 < 3
    assert p.wait(timeout=5) is not None
    assert not procs.group_alive(p.pid)


def test_suspend_resume_round_trip(children):
    p = children(["sleep", "30"])
    assert procs.suspend(p.pid) is True
    assert _wait(lambda: procs.read_stat(p.pid)["state"] == "T")
    assert procs.resume(p.pid) is True
    assert _wait(lambda: procs.read_stat(p.pid)["state"] != "T")


def test_terminate_kills_stopped_group(children):
    """A stopped process ignores SIGTERM until continued; terminate_group must wake it first."""
    p = children(["sleep", "30"])
    procs.suspend(p.pid)
    assert _wait(lambda: procs.read_stat(p.pid)["state"] == "T")
    assert procs.terminate_group(p.pid, grace_s=3) is True
    assert p.wait(timeout=5) is not None


def test_terminate_escalates_to_sigkill_when_ignored(children):
    script = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    p = children([sys.executable, "-c", script])
    time.sleep(0.5)   # let the handler install before signalling
    t0 = time.monotonic()
    assert procs.terminate_group(p.pid, grace_s=0.3) is True
    assert time.monotonic() - t0 < 4
    assert p.wait(timeout=5) == -signal.SIGKILL


def test_group_members_ignore_zombies(children):
    p = children(["true"])
    p.wait(timeout=5)                     # reaped: no zombie left
    assert procs.group_members(p.pid) == []

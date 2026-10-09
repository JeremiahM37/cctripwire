"""End-to-end, black-box scenarios across cctripwire's CLI, gateway, hooks and daemon.

Everything runs as real subprocesses (`python -m cctripwire ...`) against tests/fake_claude.py,
reached through a shim on PATH. The real `claude` is never executed and no network is used.
Every process group started here is killed in the fixture teardown.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cctripwire import forecast, gateway, procs, util
from cctripwire.config import load
from cctripwire.models import QuotaSample
from cctripwire.store import Store

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fake_claude.py"
PY = sys.executable
EXIT_DENIED = 77

GW = {
    "max_concurrency": 2, "max_depth": 2, "max_children": 10, "queue_timeout_s": 60,
    "queue_poll_s": 0.1, "spawn_rate_per_min": 1000, "kill_grace_s": 1, "heartbeat_s": 0.2,
    "duplicate_window_s": 600, "max_duplicates": 3,
}


def wait_for(pred, timeout=20.0, step=0.05, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"timed out waiting for {what}")


def _toml(sections: dict) -> str:
    out = []
    for name, values in sections.items():
        out.append(f"[{name}]")
        for k, v in values.items():
            if isinstance(v, bool):
                out.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, str):
                out.append(f"{k} = {json.dumps(v)}")
            else:
                out.append(f"{k} = {v}")
        out.append("")
    return "\n".join(out)


class Sandbox:
    """One isolated cctripwire home: state dir, config, shim dir, work dir, fake claude."""

    def __init__(self, root: Path, gateway_over: dict | None = None):
        self.root = root
        self.home = root / "home"
        self.claude_dir = root / "claude"
        self.shim = root / "shim"
        self.work = root / "e2e-proj"          # project name is the dir basename
        self.work.mkdir(parents=True)
        self.log = root / "fake.log"
        self.cfg_path = root / "config.toml"
        gw = dict(GW)
        gw.update(gateway_over or {})
        self.cfg_path.write_text(_toml({"general": {"claude_dir": str(self.claude_dir)}, "gateway": gw}))
        gateway.install_shim(self.shim, python=PY)     # real entry point: python -m cctripwire
        self.popens: list[subprocess.Popen] = []
        self._n = 0
        self.base = {k: v for k, v in os.environ.items() if not k.startswith(("CCTRIPWIRE_", "FAKE_", "CLAUDE_"))}
        self.base.update({
            "PYTHONPATH": str(ROOT / "src"),
            "CCTRIPWIRE_HOME": str(self.home),
            "CCTRIPWIRE_CONFIG": str(self.cfg_path),
            "CCTRIPWIRE_REAL_CLAUDE": str(FAKE),
            "CCTRIPWIRE_SHIM_DIR": str(self.shim),
            "PATH": f"{self.shim}{os.pathsep}{os.environ.get('PATH', '')}",
            "CLAUDE_CONFIG_DIR": str(self.claude_dir),
            "FAKE_LOG": str(self.log),
            "FAKE_SLEEP": "0",
        })

    # -- environment and processes ---------------------------------------------
    @property
    def db(self) -> Path:
        return self.home / "tripwire.db"

    def env(self, **extra) -> dict:
        e = dict(self.base)
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def cli(self, *args, stdin: str | None = None, timeout: float = 60, **env) -> subprocess.CompletedProcess:
        return subprocess.run([PY, "-m", "cctripwire", *args], input=stdin, capture_output=True, text=True,
                              env=self.env(**env), timeout=timeout)

    def popen(self, argv: list[str], **env) -> subprocess.Popen:
        self._n += 1
        out = open(self.root / f"out{self._n}.txt", "wb")
        err = open(self.root / f"err{self._n}.txt", "wb")
        p = subprocess.Popen(argv, env=self.env(**env), stdout=out, stderr=err, start_new_session=True)
        out.close()
        err.close()
        p._e2e_out = self.root / f"out{self._n}.txt"   # noqa: SLF001 - test bookkeeping
        p._e2e_err = self.root / f"err{self._n}.txt"   # noqa: SLF001
        self.popens.append(p)
        return p

    def gate(self, args: list[str], **env) -> subprocess.Popen:
        """Start `cctripwire run -- <args>` (the gateway) without waiting."""
        return self.popen([PY, "-m", "cctripwire", "run", "--", *args], **env)

    def gate_run(self, args: list[str], timeout: float = 60, **env) -> tuple[int, str, str]:
        p = self.gate(args, **env)
        rc = p.wait(timeout=timeout)
        return rc, p._e2e_out.read_text(errors="replace"), p._e2e_err.read_text(errors="replace")  # noqa: SLF001

    def hook(self, payload: dict) -> dict | None:
        payload = {"cwd": str(self.work), **payload}
        r = subprocess.run([PY, "-m", "cctripwire", "hook"], input=json.dumps(payload), capture_output=True,
                           text=True, env=self.env(), timeout=30)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout) if r.stdout.strip() else None

    # -- database views ----------------------------------------------------------
    def store(self) -> Store:
        return Store(self.db)

    def rows(self, status=None) -> list[dict]:
        if not self.db.exists():
            return []
        s = self.store()
        try:
            return s.invocations(status=status, limit=10000)
        finally:
            s.close()

    def row(self, inv_id: str) -> dict:
        s = self.store()
        try:
            return s.get_invocation(inv_id)
        finally:
            s.close()

    def interventions(self) -> list[dict]:
        s = self.store()
        try:
            return s.interventions(limit=10000)
        finally:
            s.close()

    def detections(self, session_id=None) -> list[dict]:
        s = self.store()
        try:
            return s.detections(limit=10000, session_id=session_id)
        finally:
            s.close()

    def cleanup(self) -> None:
        for p in self.popens:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        if self.db.exists():
            try:
                pgids = [r["pgid"] for r in self.rows() if r.get("pgid")]
            except Exception:  # noqa: BLE001
                pgids = []
            for pgid in pgids:
                procs.signal_group(int(pgid), signal.SIGKILL)


@pytest.fixture
def sb(tmp_path):
    boxes: list[Sandbox] = []

    def factory(gateway_over: dict | None = None, name: str = "box") -> Sandbox:
        box = Sandbox(tmp_path / f"{name}{len(boxes)}", gateway_over)
        boxes.append(box)
        return box

    yield factory
    for box in boxes:
        box.cleanup()


HOLD_LOCK = """
import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute("CREATE TABLE IF NOT EXISTS x(a)")
c.execute("BEGIN IMMEDIATE")
print("LOCKED", flush=True)
time.sleep(1.5)
c.execute("COMMIT")
"""

OPEN_STORE = """
import sys
from cctripwire.store import Store
Store(sys.argv[1]).close()
print("ok")
"""

def _state(pgid: int) -> str | None:
    st = procs.read_stat(pgid)
    return st["state"] if st else None


# ------------------------------------------------------------------ 1. swarm under rising quota

ORCHESTRATOR = r"""
import json, subprocess, sys
n = int(sys.argv[1])
ps = [subprocess.Popen(["claude", "-p", f"swarm child {i}"], stdout=subprocess.DEVNULL) for i in range(n)]
print(json.dumps([p.wait() for p in ps]), flush=True)
"""


def test_swarm_suspends_at_stop_and_resumes(sb):
    box = sb(name="swarm")
    # Precondition: the database already exists (any earlier hook or CLI call creates it).
    # Creating it while six gateways race to open it is the separate xfail bug below.
    box.store().close()
    orch_out = box.root / "orch.json"
    orch = subprocess.Popen([PY, "-c", ORCHESTRATOR, "6"], env=box.env(FAKE_SLEEP="6"),
                            stdout=open(orch_out, "wb"), start_new_session=True)
    box.popens.append(orch)

    running = wait_for(lambda: (lambda r, q: r if len(r) == 2 and len(q) == 4 else None)(
        box.rows(["running"]), box.rows(["queued"])), what="2 running, 4 queued")
    pgids = {r["id"]: int(r["pgid"]) for r in running}
    for pg in pgids.values():
        assert _state(pg) in ("S", "R")

    # Rising quota: 96% of the five-hour window is critical (STOP).
    assert box.cli("mock-quota", "5h", "96", "--resets-in", "2h").returncode == 0
    assert box.cli("tick").returncode == 0

    wait_for(lambda: len(box.rows(["suspended"])) == 2, what="both running children suspended")
    for pg in pgids.values():
        wait_for(lambda pg=pg: _state(pg) == "T", what=f"pgid {pg} stopped (state T)")

    denied = [r for r in box.rows(["denied"]) if "STOP" in (r.get("reason") or "")]
    assert len(denied) == 4 and all(r["exit_code"] == EXIT_DENIED for r in denied)
    for inv_id in pgids:
        ck = json.loads((box.home / "checkpoints" / f"{inv_id}.json").read_text())
        assert "STOP" in ck["reason"]
    actions = {(i["action"], i["target"]) for i in box.interventions()}
    assert all(("suspend", inv_id) in actions and ("checkpoint", inv_id) in actions for inv_id in pgids)

    late = box.gate_run(["-p", "late launch after STOP"])
    assert late[0] == EXIT_DENIED and "usage at STOP" in late[2]

    # Recovery: back to 40% -> suspended children resume and everything finishes.
    assert box.cli("mock-quota", "5h", "40", "--resets-in", "2h").returncode == 0
    assert box.cli("tick").returncode == 0
    for inv_id, pg in pgids.items():
        wait_for(lambda inv_id=inv_id: box.row(inv_id)["status"] == "running", what=f"{inv_id} resumed")
        assert _state(pg) != "T"
    assert orch.wait(timeout=60) == 0
    rcs = json.loads(orch_out.read_text())
    assert sorted(rcs) == [0, 0, EXIT_DENIED, EXIT_DENIED, EXIT_DENIED, EXIT_DENIED]
    for inv_id in pgids:
        assert box.row(inv_id)["status"] == "done"
        assert not procs.group_members(pgids[inv_id])


# ------------------------------------------------------------------ 2. missing / inaccurate usage

def test_no_samples_is_ok_and_unknown_never_zero(sb):
    box = sb(name="nodata")
    st = json.loads(box.cli("status", "--json").stdout)
    assert st["level"] == "ok"
    s = box.store()
    try:
        ws = forecast.window_state(s, load(box.cfg_path), "five_hour")
    finally:
        s.close()
    assert ws.used_pct is None


def test_stale_and_expired_samples_are_not_exact(sb):
    box = sb(name="stale")
    cfg = load(box.cfg_path)
    stale_after = float(cfg.get("general.stale_after_s"))
    now = util.now()
    s = box.store()
    try:
        # Exact but older than stale_after_s: must not be reported as a live exact reading.
        s.add_quota_sample(QuotaSample(ts=now - 3 * stale_after, window="five_hour", used_pct=50.0,
                                       resets_at=now + 3600, source="statusline", exact=True))
        ws = forecast.window_state(s, cfg, "five_hour", now=now)
        assert ws.used_pct is None and ws.exact is False
        # Fresh reading whose reset time has already passed: unknown, not 0%.
        s.add_quota_sample(QuotaSample(ts=now - 30, window="seven_day", used_pct=40.0,
                                       resets_at=now - 5, source="statusline", exact=True))
        ws7 = forecast.window_state(s, cfg, "seven_day", now=now)
        assert ws7.used_pct is None
    finally:
        s.close()
    st = json.loads(box.cli("status", "--json").stdout)
    assert st["level"] == "ok"


@pytest.mark.parametrize("payload", ['{"rate_limits": ', "not json at all", '{"rate_limits": [1, 2]}'])
def test_corrupted_statusline_still_prints_a_line(sb, payload):
    box = sb(name="statusline")
    r = box.cli("statusline", stdin=payload)
    assert r.returncode == 0
    assert r.stdout.strip()


# Regression: WAL switch on a fresh DB used to fail at once while another connection held the lock.
def test_first_open_waits_for_a_held_write_lock(sb):
    box = sb(name="firstopen")
    box.home.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen([PY, "-I", "-c", HOLD_LOCK, str(box.db)], stdout=subprocess.PIPE,
                              start_new_session=True)
    box.popens.append(holder)
    assert holder.stdout.readline().strip() == b"LOCKED"
    try:
        r = subprocess.run([PY, "-I", "-c", OPEN_STORE, str(box.db)], capture_output=True, text=True,
                           env=box.env(), timeout=30)
    finally:
        holder.wait(timeout=10)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"


# ------------------------------------------------------------------ 3. crashed agents and recovery

def test_child_crash_marks_invocation_failed(sb):
    box = sb(name="crash")
    rc, _, _ = box.gate_run(["-p", "crash halfway"], FAKE_CRASH="1", FAKE_TURNS="2")
    assert rc == 1
    rows = box.rows()
    assert len(rows) == 1 and rows[0]["status"] == "failed" and rows[0]["exit_code"] == 1


def test_gateway_sigkill_is_reaped_and_orphan_group_killed(sb):
    box = sb(name="sigkill")
    gw = box.gate(["-p", "long job"], FAKE_SLEEP="60")
    row = wait_for(lambda: (box.rows(["running"]) or [None])[0], what="running row")
    inv_id, pgid = row["id"], int(row["pgid"])
    assert procs.group_alive(pgid)

    os.kill(gw.pid, signal.SIGKILL)
    gw.wait(timeout=10)
    assert procs.group_alive(pgid)              # the child survives its gateway: an orphan

    assert box.cli("tick").returncode == 0
    assert box.row(inv_id)["status"] == "crashed"
    assert "exited" in box.row(inv_id)["reason"]
    wait_for(lambda: not procs.group_alive(pgid), what="orphan group killed")


WRITER = r"""
import sys, time
from cctripwire.store import Store
s = Store(sys.argv[1])
s.add_intervention("probe", "writer", "committed-0")
print("READY", flush=True)
n = 0
while True:
    with s.tx(immediate=True):
        for i in range(200):
            s.add_intervention("probe", "writer", f"batch-{n}-{i}")
        time.sleep(0.02)          # hold the write lock mid-transaction
    n += 1
"""


def test_db_survives_kill9_mid_transaction(sb):
    box = sb(name="kill9")
    box.home.mkdir(parents=True, exist_ok=True)
    w = subprocess.Popen([PY, "-c", WRITER, str(box.db)], env=box.env(), stdout=subprocess.PIPE,
                         start_new_session=True)
    box.popens.append(w)
    assert w.stdout.readline().strip() == b"READY"
    time.sleep(0.5)
    os.kill(w.pid, signal.SIGKILL)
    w.wait(timeout=10)

    s = Store(box.db)
    try:
        assert s.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        batches = [i["reason"] for i in s.interventions(limit=100000) if i["reason"].startswith("batch-")]
        assert batches, "writer committed nothing"
        # Each batch is one transaction of 200 rows: a torn batch would show a non-multiple count.
        assert len(batches) % 200 == 0
    finally:
        s.close()
    assert box.cli("status", "--json").returncode == 0
    assert box.cli("tick").returncode == 0


# ------------------------------------------------------------------ 4. recursive and repeated loops

def test_recursive_claude_p_stops_at_max_depth(sb):
    box = sb(name="recurse")
    rc, _, _ = box.gate_run(["-p", "recurse"], FAKE_RECURSE="1", timeout=60)
    assert rc == EXIT_DENIED
    rows = box.rows()
    assert max(r["depth"] for r in rows) == 3          # max_depth 2 => depth 3 is the refused one
    refused = [r for r in rows if r["depth"] == 3]
    assert len(refused) == 1 and refused[0]["status"] == "denied"
    assert "exceeds limit 2" in refused[0]["reason"]


def test_identical_prompts_denied_after_max_duplicates(sb):
    box = sb(name="dup")
    prompt = "summarise the weekly report please"
    codes = [box.gate_run(["-p", prompt])[0] for _ in range(4)]
    assert codes == [0, 0, 0, EXIT_DENIED]
    rc, _, err = box.gate_run(["-p", prompt])
    assert rc == EXIT_DENIED and "Reuse the earlier result" in err
    denied = [r for r in box.rows(["denied"]) if "identical prompt" in (r["reason"] or "")]
    assert len(denied) == 2
    for r in denied:
        assert r["exit_code"] == EXIT_DENIED
        assert any(i["action"] == "denied" and i["target"] == r["id"] for i in box.interventions())


# ------------------------------------------------------------------ 5. hook false-positive guard

def _pre(tool, tin, sid):
    return {"hook_event_name": "PreToolUse", "session_id": sid, "tool_name": tool, "tool_input": tin}


def _post(tool, tin, sid, resp):
    return {"hook_event_name": "PostToolUse", "session_id": sid, "tool_name": tool, "tool_input": tin,
            "tool_response": resp}


def _is_denied(out) -> bool:
    return bool(out and out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny")


def test_edit_test_cycle_is_not_flagged(sb):
    box = sb(name="fp")
    sid = "s-edit-cycle"
    test_cmd = {"command": "pytest tests/test_widget.py -q"}
    for i in range(10):
        out = box.hook(_pre("Bash", test_cmd, sid))
        assert not _is_denied(out), f"false positive on pytest run {i}"
        last = i == 9
        box.hook(_post("Bash", test_cmd, sid, {"stdout": "", "returncode": 0 if last else 1}))
        if not last:
            edit = {"file_path": "widget.py", "old_string": f"v{i}", "new_string": f"v{i + 1}"}
            assert not _is_denied(box.hook(_pre("Edit", edit, sid)))
            box.hook(_post("Edit", edit, sid, {"success": True}))
    kinds = {d["kind"] for d in box.detections(sid)}
    assert not kinds & {"repeated_tool_failure", "excessive_retries"}


def test_same_failing_call_without_edits_is_denied_and_logged(sb):
    box = sb(name="retry")
    sid = "s-retry"
    cmd = {"command": "pytest tests/test_widget.py::test_boom -q"}
    for _ in range(4):
        assert not _is_denied(box.hook(_pre("Bash", cmd, sid)))
        box.hook(_post("Bash", cmd, sid, {"stderr": "AssertionError: boom", "returncode": 1}))
    out = box.hook(_pre("Bash", cmd, sid))
    assert _is_denied(out)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "failed 4" in reason and "Change approach" in reason
    logged = [i for i in box.interventions() if i["action"] == "deny_tool" and i["target"] == f"session:{sid}"]
    assert logged and "Bash" in logged[0]["reason"]


# ------------------------------------------------------------------ 6. graceful checkpoint at STOP

def test_stop_override_forces_checkpoint_then_halts(sb):
    box = sb(name="stopck")
    assert box.cli("override", "stop", "--for", "1h").returncode == 0
    sid = "s-stop"
    first = box.hook(_pre("Bash", {"command": "make build"}, sid))
    assert _is_denied(first)
    assert "CHECKPOINT.md" in first["hookSpecificOutput"]["permissionDecisionReason"]
    assert box.hook(_pre("Write", {"file_path": "CHECKPOINT.md", "content": "done"}, sid)) is None
    assert box.hook(_pre("Edit", {"file_path": "CHECKPOINT.md", "old_string": "a", "new_string": "b"}, sid)) is None
    halt = box.hook(_pre("Bash", {"command": "make build"}, sid))
    assert halt is not None and halt.get("continue") is False
    assert "STOP" in halt["stopReason"]
    actions = {i["action"] for i in box.interventions() if i["target"] == f"session:{sid}"}
    assert {"deny_tool", "halt_session"} <= actions


# ------------------------------------------------------------------ 7. process cleanup on kill

HANG = """#!/bin/sh
sleep 120 &
echo $! > "$HANG_PIDFILE"
wait
"""


def test_kill_removes_whole_group_including_grandchild(sb):
    box = sb(name="kill")
    script = box.root / "hang-claude"
    script.write_text(HANG)
    script.chmod(0o755)
    pidfile = box.root / "sleep.pid"
    gw = box.gate(["-p", "hang"], CCTRIPWIRE_REAL_CLAUDE=str(script), HANG_PIDFILE=str(pidfile))
    row = wait_for(lambda: (box.rows(["running"]) or [None])[0], what="running row")
    inv_id, pgid = row["id"], int(row["pgid"])
    sleep_pid = int(wait_for(lambda: pidfile.exists() and pidfile.read_text().strip(), what="grandchild pid"))
    assert sleep_pid in procs.group_members(pgid)

    assert box.cli("kill", inv_id).returncode == 0
    assert gw.wait(timeout=20) == 143
    assert box.row(inv_id)["status"] == "killed"
    wait_for(lambda: not procs.group_members(pgid), what="process group empty")
    wait_for(lambda: not util.pid_alive(sleep_pid), what="grandchild sleep gone")

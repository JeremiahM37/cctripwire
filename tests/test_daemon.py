"""Tests for daemon.py. Drives levels through the real policy engine via the
`override` kv key. Spawned sleepers are always cleaned up."""
from __future__ import annotations

import importlib
import json
import os
import shlex
import signal
import subprocess
import sys
import textwrap
import threading
import time
import types
from types import SimpleNamespace

import pytest

from cctripwire import daemon
from cctripwire.config import Config
from cctripwire.store import Store

T0 = time.time()   # recent timestamps: store.prune() would delete ancient transient rows
ALL_STEPS = ["scan", "oauth", "usage_url", "decide", "levels", "reap", "enforce", "bypass", "breaker",
             "loops", "prune", "heartbeat"]


@pytest.fixture
def engine_available(monkeypatch):
    """engine.py imports cctripwire.forecast at module level. If the real forecast is
    not there yet, provide a minimal stub for the duration of the test only."""
    try:
        importlib.import_module("cctripwire.forecast")
        yield
        return
    except ImportError:
        pass

    def build_snapshot(store, cfg, now=None):
        from cctripwire.models import Snapshot
        return Snapshot(ts=now or time.time(),
                        running_invocations=len(store.invocations(status="running", limit=100000)))

    stub = types.ModuleType("cctripwire.forecast")
    stub.build_snapshot = build_snapshot
    monkeypatch.setitem(sys.modules, "cctripwire.forecast", stub)
    prev = sys.modules.pop("cctripwire.engine", None)
    importlib.import_module("cctripwire.engine")
    yield
    sys.modules.pop("cctripwire.engine", None)
    if prev is not None:
        sys.modules["cctripwire.engine"] = prev


@pytest.fixture
def env(tmp_path, monkeypatch, engine_available):
    state = tmp_path / "state"
    monkeypatch.setenv("CCTRIPWIRE_HOME", str(state))
    monkeypatch.delenv("CCTRIPWIRE_TIME_OFFSET", raising=False)
    monkeypatch.delenv("CCTRIPWIRE_DISABLE", raising=False)
    notify_log = tmp_path / "notify.log"
    script = tmp_path / "notify.py"
    script.write_text(textwrap.dedent(f"""
        import os
        with open({str(notify_log)!r}, "a") as f:
            f.write(os.environ["CCTRIPWIRE_TITLE"] + "|" + os.environ["CCTRIPWIRE_BODY"] + "\\n")
    """))
    cfg = Config({
        "general": {"state_dir": str(state), "claude_dir": str(tmp_path / "claude")},
        "gateway": {"max_concurrency": 2, "kill_grace_s": 2},
        "daemon": {"notify_command": f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"},
    })
    store = Store(cfg.db_path)
    spawned: list[subprocess.Popen] = []
    yield SimpleNamespace(cfg=cfg, store=store, state=state, notify_log=notify_log, tmp=tmp_path,
                          spawned=spawned)
    store.close()
    for p in spawned:
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait(timeout=5)


def spawn_group(env) -> subprocess.Popen:
    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    env.spawned.append(p)
    return p


def add_inv(store, inv_id, status="running", created=T0, pid=None, pgid=None, session_id=None):
    store.create_invocation({
        "id": inv_id, "status": status, "created": created, "pid": pid, "pgid": pgid,
        "session_id": session_id, "argv": json.dumps(["claude", "-p", "x"]), "project": "proj",
        "agent": "agent", "depth": 1, "root_id": inv_id, "prompt_hash": "h" + inv_id,
        "tokens_in": 0, "tokens_out": 0, "weighted": 0.0,
    })


def level_changes(store):
    return [i for i in store.interventions(limit=1000) if i["action"] == "level_change"]


def test_level_change_recorded_once_and_notified(env):
    store = env.store
    daemon.tick(env.cfg, store)                     # baseline: OK, nothing recorded
    assert level_changes(store) == []

    store.kv_set("override", {"level": "stop"})
    reports = [daemon.tick(env.cfg, store) for _ in range(3)]
    changes = level_changes(store)
    assert len(changes) == 1
    assert changes[0]["level"] == 4 and "ok -> stop" in changes[0]["reason"]
    lines = env.notify_log.read_text().splitlines()
    assert len(lines) == 1 and lines[0].startswith("cctripwire: level STOP|")
    assert all(r["levels"] == "ok" for r in reports)

    store.kv_set("override", None)                  # back to OK is a second, single change
    daemon.tick(env.cfg, store)
    daemon.tick(env.cfg, store)
    assert len(level_changes(store)) == 2
    assert len(env.notify_log.read_text().splitlines()) == 2


def test_stop_checkpoints_and_suspends_running(env, tmp_path):
    store = env.store
    child = spawn_group(env)
    store.upsert_session("sess-a", cwd=str(tmp_path / "work"), project="proj", agent="agent")
    add_inv(store, "inv-a", status="running", created=T0, pid=os.getpid(), pgid=child.pid, session_id="sess-a")

    store.kv_set("override", {"level": "stop"})
    daemon.tick(env.cfg, store)

    assert store.get_invocation("inv-a")["status"] == "suspend_requested"
    ck = json.loads((env.state / "checkpoints" / "inv-a.json").read_text())
    assert ck["session_id"] == "sess-a"
    assert ck["cwd"] == str(tmp_path / "work")
    assert "--resume sess-a" in ck["resume_cmd"]
    actions = [i["action"] for i in store.interventions(limit=1000)]
    assert "checkpoint" in actions and "suspend" in actions

    # Repeated ticks do not re-request or re-checkpoint the same invocation.
    daemon.tick(env.cfg, store)
    assert [i["action"] for i in store.interventions(limit=1000)].count("checkpoint") == 1
    assert child.poll() is None                     # SIGSTOP is the gateway's job, not ours


def test_stop_without_pause_requests_kill(env):
    store = env.store
    child = spawn_group(env)
    env.cfg.data["daemon"]["pause_on_stop"] = False
    add_inv(store, "inv-k", status="running", created=T0, pid=os.getpid(), pgid=child.pid)

    store.kv_set("override", {"level": "stop"})
    daemon.tick(env.cfg, store)
    assert store.get_invocation("inv-k")["status"] == "kill_requested"


def test_recovery_resumes_suspended_oldest_first_up_to_cap(env):
    store = env.store
    for n in (3, 1, 2):                              # insert out of order on purpose
        add_inv(store, f"inv-{n}", status="suspended", created=T0 + n, pid=os.getpid())

    store.kv_set("override", {"level": "ok"})        # cap = gateway.max_concurrency = 2
    daemon.tick(env.cfg, store)

    assert store.get_invocation("inv-1")["status"] == "resume_requested"
    assert store.get_invocation("inv-2")["status"] == "resume_requested"
    assert store.get_invocation("inv-3")["status"] == "suspended"


def test_over_cap_suspends_newest_extras(env):
    store = env.store
    for n in (1, 2, 3):
        add_inv(store, f"inv-{n}", status="running", created=T0 + n, pid=os.getpid())

    store.kv_set("override", {"level": "throttle"})  # throttle caps concurrency at 1
    daemon.tick(env.cfg, store)

    assert store.get_invocation("inv-1")["status"] == "running"       # oldest is kept
    assert store.get_invocation("inv-2")["status"] == "suspend_requested"
    assert store.get_invocation("inv-3")["status"] == "suspend_requested"
    throttle = [i for i in store.interventions(limit=1000) if i["action"] == "suspend"]
    assert len(throttle) == 2 and all("throttle" in i["reason"] for i in throttle)


def test_dead_gateway_row_is_crashed_and_its_group_killed(env):
    store = env.store
    dead = subprocess.Popen(["true"])
    dead.wait()                                      # a pid that no longer exists
    child = spawn_group(env)
    add_inv(store, "inv-dead", status="running", created=T0, pid=dead.pid, pgid=child.pid)

    daemon.tick(env.cfg, store)

    row = store.get_invocation("inv-dead")
    assert row["status"] == "crashed"
    assert "exited" in row["reason"]
    assert child.wait(timeout=10) != 0               # leftover group was signalled
    assert any(i["action"] == "crash" for i in store.interventions(limit=1000))


def test_pid_none_rows_are_not_reaped(env):
    store = env.store
    add_inv(store, "inv-q", status="queued", created=T0, pid=None)
    daemon.tick(env.cfg, store)
    assert store.get_invocation("inv-q")["status"] == "queued"


def test_tick_survives_every_step_raising(env, monkeypatch):
    def boom(ctx):
        raise RuntimeError("boom")

    for name in ALL_STEPS:
        monkeypatch.setattr(daemon, f"_step_{name}", boom)
    report = daemon.tick(env.cfg, env.store)
    assert sorted(report) == sorted(ALL_STEPS)
    assert all(v.startswith("error: RuntimeError: boom") for v in report.values())
    assert (env.state / "daemon.log").read_text().count("step ") == len(ALL_STEPS)


def test_tick_still_heartbeats_when_other_steps_raise(env, monkeypatch):
    def boom(ctx):
        raise RuntimeError("boom")

    for name in ALL_STEPS:
        if name != "heartbeat":
            monkeypatch.setattr(daemon, f"_step_{name}", boom)
    report = daemon.tick(env.cfg, env.store)
    assert report["heartbeat"] == "ok"
    hb = env.store.kv_get(daemon.HEARTBEAT_KEY)
    assert hb["pid"] == os.getpid() and "scan" in hb["steps"]


def test_run_once_returns_report_and_loop_stops_on_event(env):
    report = daemon.run(env.cfg, once=True, store=env.store)
    assert set(report) == set(ALL_STEPS)
    assert env.store.kv_get(daemon.HEARTBEAT_KEY) is not None

    stop = threading.Event()
    t = threading.Thread(target=daemon.run, args=(env.cfg,), kwargs={"stop_event": stop, "store": env.store})
    t.start()
    time.sleep(0.3)
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()

"""Gateway tests. Every scenario runs the gateway as a real subprocess against fake_claude.py.

The gateway is started through `python -c` (no __main__ needed). Nested `claude` calls go
through a shim dir installed by install_shim(). All child process groups are killed in the
fixture teardown, so a failing test cannot leave fake claude processes behind.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from cctripwire import gateway, procs
from cctripwire.store import Store

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fake_claude.py"

# Stub the policy forecast only when the real module is missing (it is owned by another worker).
PRELUDE = (
    "import sys, time, types, importlib.util\n"
    "if importlib.util.find_spec('cctripwire.forecast') is None:\n"
    "    from cctripwire.models import Snapshot\n"
    "    _m = types.ModuleType('cctripwire.forecast')\n"
    "    _m.build_snapshot = lambda store, cfg, now=None: Snapshot(ts=now or time.time())\n"
    "    sys.modules['cctripwire.forecast'] = _m\n"
    "from cctripwire.gateway import main\n"
)
GATEWAY_CODE = PRELUDE + "sys.exit(main(sys.argv[1:]))\n"


def _toml(d: dict) -> str:
    lines = []
    for section, values in d.items():
        lines.append(f"[{section}]")
        for k, v in values.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, str):
                lines.append(f"{k} = {json.dumps(v)}")
            else:
                lines.append(f"{k} = {v}")
        lines.append("")
    return "\n".join(lines)


def wait_for(pred, timeout=15.0, step=0.05, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"timed out waiting for {what}")


class Result:
    def __init__(self, rc, out, err):
        self.rc, self.out, self.err = rc, out, err


class Env:
    """One isolated cctripwire home: state dir, config file, shim dir, fake claude."""

    def __init__(self, root: Path, gateway_over: dict | None = None):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.home = root / "home"
        self.claude_dir = root / "claude"
        self.shim = root / "shim"
        self.log = root / "fake.log"
        self.popens: list[subprocess.Popen] = []
        self._n = 0
        gw = {
            "max_concurrency": 2, "max_depth": 2, "max_children": 4, "queue_timeout_s": 20,
            "queue_poll_s": 0.1, "spawn_rate_per_min": 1000, "kill_grace_s": 1, "heartbeat_s": 0.2,
            "duplicate_window_s": 600, "max_duplicates": 3,
        }
        gw.update(gateway_over or {})
        self.cfg_path = root / "config.toml"
        self.cfg_path.write_text(_toml({"general": {"claude_dir": str(self.claude_dir)}, "gateway": gw}))
        gateway.install_shim(self.shim, entry=GATEWAY_CODE)
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
            "FAKE_TRANSCRIPT_DIR": str(self.claude_dir),
        })

    # -- helpers --------------------------------------------------------------
    @property
    def db(self) -> Path:
        return self.home / "tripwire.db"

    def store(self) -> Store:
        return Store(self.db)

    def env(self, extra: dict | None = None) -> dict:
        e = dict(self.base)
        e.update(extra or {})
        return e

    def spawn(self, args, extra=None, stdin_data: bytes | None = None):
        self._n += 1
        out = self.root / f"out{self._n}.txt"
        err = self.root / f"err{self._n}.txt"
        fo, fe = open(out, "wb"), open(err, "wb")
        stdin = subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL
        p = subprocess.Popen([sys.executable, "-c", GATEWAY_CODE, "run", *args], env=self.env(extra),
                             stdout=fo, stderr=fe, stdin=stdin, start_new_session=True)
        fo.close()
        fe.close()
        self.popens.append(p)
        if stdin_data is not None:
            p.stdin.write(stdin_data)
            p.stdin.close()
        p._out, p._err = out, err  # noqa: SLF001 - test bookkeeping
        return p

    def run(self, args, extra=None, timeout=60, stdin_data: bytes | None = None) -> Result:
        p = self.spawn(args, extra, stdin_data=stdin_data)
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill_group(p)
            raise
        return Result(rc, p._out.read_text(errors="replace"), p._err.read_text(errors="replace"))

    def kill_group(self, p: subprocess.Popen) -> None:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass

    def live_rows(self) -> list[dict]:
        return self.store().invocations(status=["queued", "running", "suspended"])

    def fake_events(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text().splitlines() if x.strip()]

    def cleanup(self) -> None:
        for p in self.popens:
            if p.poll() is None:
                self.kill_group(p)
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        if self.db.exists():
            try:
                rows = self.store().conn.execute(
                    "SELECT pgid FROM invocations WHERE pgid IS NOT NULL AND status IN "
                    "('queued','running','suspended')").fetchall()
            except Exception:  # noqa: BLE001
                rows = []
            for (pgid,) in rows:
                if procs.group_alive(pgid):
                    procs.signal_group(pgid, signal.SIGKILL)


@pytest.fixture
def make_env(tmp_path):
    envs: list[Env] = []

    def factory(gateway_over: dict | None = None) -> Env:
        e = Env(tmp_path / f"e{len(envs)}", gateway_over)
        envs.append(e)
        return e

    yield factory
    for e in envs:
        e.cleanup()


# ---------------------------------------------------------------- in-process units

def test_install_shim_writes_marked_executable(tmp_path):
    path = gateway.install_shim(tmp_path / "bin", entry="print('x')")
    assert path.name == "claude"
    assert os.access(path, os.X_OK)
    text = path.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert gateway.SHIM_MARKER in text
    assert "run --shim -- \"$@\"" in text


def test_resolve_real_claude_precedence_and_shim_skip(tmp_path, monkeypatch):
    from cctripwire.config import Config
    shim = gateway.install_shim(tmp_path / "shim", entry="pass")
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real = real_dir / "claude"
    real.write_text("#!/bin/sh\necho real\n")
    real.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}{real_dir}")
    monkeypatch.setenv("CCTRIPWIRE_SHIM_DIR", str(shim.parent))
    monkeypatch.delenv("CCTRIPWIRE_REAL_CLAUDE", raising=False)
    cfg = Config({})
    assert gateway.resolve_real_claude(cfg) == str(real)
    monkeypatch.setenv("CCTRIPWIRE_REAL_CLAUDE", str(FAKE))
    assert gateway.resolve_real_claude(cfg) == str(FAKE)
    monkeypatch.setenv("CCTRIPWIRE_REAL_CLAUDE", str(shim))
    with pytest.raises(RuntimeError):
        gateway.resolve_real_claude(cfg)
    monkeypatch.delenv("CCTRIPWIRE_REAL_CLAUDE")
    monkeypatch.setenv("PATH", str(shim.parent))
    with pytest.raises(FileNotFoundError):
        gateway.resolve_real_claude(cfg)


def test_split_args_and_rewrite():
    from cctripwire.config import Config
    opts, args = gateway._split_args(["run", "--budget-tokens", "99", "--project=p", "--", "-p", "hi"])
    assert opts["budget_tokens"] == "99" and opts["project"] == "p" and args == ["-p", "hi"]
    opts, args = gateway._split_args(["run", "--", "--version"])
    assert args == ["--version"]
    cfg = Config({"gateway": {"per_invocation_max_turns": 7}})
    out, session = gateway._rewrite(["--model", "opus", "-p", "hi"], cfg, "haiku")
    assert out.count("--model") == 1 and out[out.index("--model") + 1] == "haiku"
    assert "opus" not in out
    assert "--max-turns" in out and out[out.index("--max-turns") + 1] == "7"
    assert out[out.index("--session-id") + 1] == session
    out, session = gateway._rewrite(["-p", "x", "--resume", "abc"], cfg, None)
    assert session == "abc" and "--session-id" not in out


# ---------------------------------------------------------------- subprocess scenarios

def test_non_print_args_pass_through_exec(make_env):
    env = make_env()
    r = env.run(["--", "--version"])
    assert r.rc == 0
    assert "fake claude" in r.out
    assert not env.db.exists()          # exec path never touches the state store


def test_exit_code_propagates_and_crash_is_failed(make_env):
    env = make_env()
    r = env.run(["--", "-p", "exit five"], extra={"FAKE_EXIT": "5"})
    assert r.rc == 5
    r = env.run(["--", "-p", "crash"], extra={"FAKE_CRASH": "1", "FAKE_TURNS": "2"})
    assert r.rc == 1
    statuses = [row["status"] for row in env.store().invocations(limit=10)]
    assert statuses.count("failed") == 2


def test_stdin_prompt_is_refed_to_child(make_env):
    env = make_env()
    r = env.run(["--", "-p"], stdin_data=b"prompt from stdin")
    assert r.rc == 0
    starts = [e for e in env.fake_events() if e["event"] == "start"]
    assert starts and starts[-1]["prompt"] == "prompt from stdin"


def test_concurrency_cap_with_eight_parallel_launches(make_env):
    env = make_env({"max_concurrency": 2})
    samples: list[int] = []
    stop = threading.Event()

    def sampler():
        store = env.store()
        while not stop.is_set():
            samples.append(len(store.invocations(status=["running", "suspended"], limit=100)))
            time.sleep(0.03)

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    try:
        ps = [env.spawn(["--", "-p", f"job {i}"], extra={"FAKE_SLEEP": "0.6"}) for i in range(8)]
        for p in ps:
            assert p.wait(timeout=60) == 0
    finally:
        stop.set()
        th.join(timeout=5)
    assert samples and max(samples) <= 2, f"DB saw {max(samples)} running"
    # Independent check: overlap of the fake's own start/end timestamps.
    points = []
    for e in env.fake_events():
        if e["event"] in ("start", "end"):
            points.append((e["t"], 1 if e["event"] == "start" else -1))
    points.sort()
    cur = best = 0
    for _, d in points:
        cur += d
        best = max(best, cur)
    assert 1 <= best <= 2
    assert best == 2, "expected the cap to be reached with 8 launches"


def test_recursion_stopped_at_max_depth(make_env):
    env = make_env({"max_depth": 2, "max_concurrency": 4})
    r = env.run(["--", "-p", "recurse"], extra={"FAKE_RECURSE": "1"}, timeout=90)
    assert r.rc == 77
    assert "depth" in r.err and "exceeds limit 2" in r.err
    denied = env.store().invocations(status="denied")
    assert any("depth 3" in (row["reason"] or "") for row in denied)


def test_duplicate_prompt_denied_after_max_duplicates(make_env):
    env = make_env({"max_duplicates": 3})
    rcs = [env.run(["--", "-p", "same prompt"]).rc for _ in range(4)]
    assert rcs == [0, 0, 0, 77]
    last = env.run(["--", "-p", "same prompt"])
    assert "identical prompt" in last.err


def test_queue_timeout_returns_75(make_env):
    env = make_env({"max_concurrency": 1, "queue_timeout_s": 1})
    holder = env.spawn(["--", "-p", "holder"], extra={"FAKE_SLEEP": "6"})
    wait_for(lambda: env.store().invocations(status="running", limit=5), what="holder running")
    t0 = time.monotonic()
    r = env.run(["--", "-p", "waiter"], timeout=30)
    elapsed = time.monotonic() - t0
    assert r.rc == 75
    assert "queued-timeout" in r.err
    assert 0.8 <= elapsed < 10
    assert any(row["status"] == "timeout" for row in env.store().invocations(limit=10))
    env.kill_group(holder)
    holder.wait(timeout=10)


def test_per_invocation_token_budget_kills_with_78(make_env):
    env = make_env()
    t0 = time.monotonic()
    r = env.run(["--budget-tokens", "1000", "--", "-p", "big job"],
                extra={"FAKE_TOKENS": "100000", "FAKE_SLEEP": "8"}, timeout=60)
    elapsed = time.monotonic() - t0
    assert r.rc == 78, r.err
    assert "budget" in r.err
    assert elapsed < 7.5
    row = env.store().invocations(status="killed", limit=5)[0]
    assert "budget" in (row["reason"] or "")
    assert row["weighted"] > 1000


def test_gateway_sigkill_reaps_row_and_kills_orphan(make_env):
    env = make_env({"max_concurrency": 1})
    a = env.spawn(["--", "-p", "orphan"], extra={"FAKE_SLEEP": "30"})
    row = wait_for(lambda: next((r for r in env.store().invocations(status="running", limit=5)
                                 if r["pgid"]), None), what="orphan running")
    pgid, inv_a = row["pgid"], row["id"]
    os.kill(a.pid, signal.SIGKILL)
    a.wait(timeout=10)
    assert procs.group_alive(pgid), "orphan should survive its gateway"
    r = env.run(["--", "-p", "fresh"], extra={"FAKE_SLEEP": "0"}, timeout=30)
    assert r.rc == 0
    assert env.store().get_invocation(inv_a)["status"] == "crashed"
    wait_for(lambda: not procs.group_alive(pgid), timeout=10, what="orphan group killed")


def test_suspend_resume_kill_via_status(make_env):
    env = make_env({"max_concurrency": 1})
    a = env.spawn(["--", "-p", "controlled"], extra={"FAKE_SLEEP": "30"})
    row = wait_for(lambda: next((r for r in env.store().invocations(status="running", limit=5)
                                 if r["pgid"]), None), what="controlled running")
    inv, pgid = row["id"], row["pgid"]
    store = env.store()
    store.update_invocation(inv, status="suspend_requested")
    wait_for(lambda: store.get_invocation(inv)["status"] == "suspended", what="suspended status")
    assert procs.read_stat(pgid)["state"] == "T"
    store.update_invocation(inv, status="resume_requested")
    wait_for(lambda: store.get_invocation(inv)["status"] == "running"
             and procs.read_stat(pgid)["state"] != "T", what="resumed")
    store.update_invocation(inv, status="kill_requested")
    assert a.wait(timeout=20) == 143
    assert store.get_invocation(inv)["status"] == "killed"
    wait_for(lambda: not procs.group_alive(pgid), what="group gone")


def test_stop_level_denies_text_and_json(make_env):
    env = make_env()
    store = env.store()
    store.kv_set("override", {"level": "stop", "until": None, "reason": "manual test stop"})
    r = env.run(["--", "-p", "blocked"])
    assert r.rc == 77
    assert "usage at STOP" in r.err and "manual test stop" in r.err
    r = env.run(["--", "--output-format", "json", "-p", "blocked json"])
    assert r.rc == 77
    obj = json.loads(r.out.strip().splitlines()[-1])
    assert obj["type"] == "result" and obj["subtype"] == "error_cctripwire" and obj["is_error"] is True
    assert obj["cctripwire"]["level"] == "stop" and "STOP" in obj["result"]


def test_restrict_forces_downgrade_model(make_env):
    env = make_env()
    env.store().kv_set("override", {"level": "restrict", "until": None, "reason": "test"})
    r = env.run(["--", "--model", "sonnet", "-p", "hello restrict"])
    assert r.rc == 0, r.err
    start = [e for e in env.fake_events() if e["event"] == "start"][-1]
    assert "haiku" in start["argv"] and "sonnet" not in start["argv"]
    row = env.store().invocations(status="done", limit=1)[0]
    assert row["model"] == "haiku"
    assert env.store().interventions()[0]["action"] == "downgrade" or any(
        i["action"] == "downgrade" for i in env.store().interventions())


def test_nested_claude_p_runs_through_shim(make_env):
    env = make_env({"max_depth": 3, "max_concurrency": 4})
    r = env.run(["--", "-p", "parent"], extra={"FAKE_SPAWN": "1", "FAKE_SPAWN_ONLY_TOP": "1"})
    assert r.rc == 0, r.err
    rows = env.store().invocations(status="done", limit=10)
    depths = sorted(row["depth"] for row in rows)
    assert depths == [1, 2]   # the child's own spawn is suppressed by FAKE_SPAWN_ONLY_TOP
    child = next(row for row in rows if row["depth"] == 2)
    parent = next(row for row in rows if row["depth"] == 1)
    assert child["parent_id"] == parent["id"]


def test_nested_chain_does_not_deadlock_under_cap_one(make_env):
    # Parents blocked on a child are not "active", so a depth-3 chain fits a cap of 1.
    env = make_env({"max_depth": 3, "max_concurrency": 1, "queue_timeout_s": 8})
    r = env.run(["--", "-p", "chain"], extra={"FAKE_SPAWN": "1", "FAKE_SPAWN_MAX_DEPTH": "3"})
    assert r.rc == 0, r.err
    assert sorted(row["depth"] for row in env.store().invocations(status="done", limit=10)) == [1, 2, 3]


def test_parallel_children_share_cap_without_deadlock(make_env):
    env = make_env({"max_depth": 2, "max_concurrency": 2, "queue_timeout_s": 15})
    r = env.run(["--", "-p", "fan"], extra={"FAKE_SPAWN": "4", "FAKE_PARALLEL": "1",
                                             "FAKE_SPAWN_ONLY_TOP": "1", "FAKE_SLEEP": "0.4"})
    assert r.rc == 0, r.err
    assert len(env.store().invocations(status="done", limit=10)) == 5

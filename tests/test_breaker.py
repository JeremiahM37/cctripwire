"""Runaway breaker: storms of claude -p are cut off, ordinary work never notices it.

Gateway scenarios run real gateway subprocesses against fake_claude.py (no tokens).
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from cctripwire import breaker, daemon, hooks, util
from cctripwire.config import Config
from cctripwire.models import FIVE_HOUR, SEVEN_DAY, QuotaSample
from cctripwire.quota import usage_url
from cctripwire.store import Store

from test_gateway import make_env  # noqa: F401 - fixture

RESET = 2_000_000_000.0


def fake_starts(env) -> int:
    return sum(1 for e in env.fake_events() if e["event"] == "start")


# -- gateway: storms --------------------------------------------------------------

def test_burst_of_120_parallel_launches_is_cut_off(make_env):
    """The incident shape: a script fires a large batch of claude -p at once."""
    env = make_env({"max_concurrency": 4, "storm_max_queued": 8, "storm_max_started": 0,
                    "queue_timeout_s": 60})
    t0 = time.monotonic()
    ps = [env.spawn(["--", "-p", f"eval case {i}", "--model", "opus"], extra={"FAKE_SLEEP": "2"})
          for i in range(120)]
    rcs = [p.wait(timeout=120) for p in ps]
    elapsed = time.monotonic() - t0
    started = fake_starts(env)
    assert started <= 12, f"{started} fake sessions actually started"
    assert rcs.count(77) >= 100, rcs
    assert breaker.tripped(env.store()) is not None
    # Refused launches fail fast; nobody sat in the queue for its 60 s timeout.
    assert elapsed < 50, elapsed
    err = next(p._err.read_text() for p, rc in zip(ps, rcs) if rc == 77)
    assert "runaway" in err and "breaker reset" in err


def test_launch_window_limit_trips_and_reset_reopens(make_env):
    env = make_env({"storm_max_queued": 0, "storm_max_started": 3, "max_duplicates": 0})
    for i in range(3):
        assert env.run(["--", "-p", f"job {i}"]).rc == 0
    r = env.run(["--", "-p", "job 3"])
    assert r.rc == 77 and "3 claude -p runs launched" in r.err
    # Still tripped: even a different prompt is refused, and nothing ran.
    assert env.run(["--", "-p", "other"]).rc == 77
    assert fake_starts(env) == 3
    breaker.reset(env.store())
    # The launch count is still over the limit, so the next launch re-trips: reset alone
    # is for after the storm; `off` is for a deliberate batch.
    breaker.lift(env.store(), 600)
    assert env.run(["--", "-p", "job 4"]).rc == 0
    assert fake_starts(env) == 4


def test_normal_work_never_trips_it(make_env):
    """Sequential and modestly parallel use stays far below the limits."""
    env = make_env({"max_concurrency": 4, "storm_max_queued": 20, "storm_max_started": 40,
                    "max_duplicates": 0})
    for i in range(5):
        assert env.run(["--", "-p", "same prompt"]).rc == 0
    ps = [env.spawn(["--", "-p", f"p{i}"], extra={"FAKE_SLEEP": "0.3"}) for i in range(8)]
    assert [p.wait(timeout=60) for p in ps] == [0] * 8
    assert breaker.tripped(env.store()) is None


def test_force_model_rewrites_child_model(make_env):
    env = make_env({"force_model": "haiku"})
    assert env.run(["--", "-p", "hi", "--model", "opus"]).rc == 0
    start = next(e for e in env.fake_events() if e["event"] == "start")
    argv = start.get("argv") or []
    assert "haiku" in argv and "opus" not in argv


# -- surge -----------------------------------------------------------------------

@pytest.fixture
def st(tmp_path):
    cfg = Config({"general": {"state_dir": str(tmp_path)},
                  "gateway": {"surge_five_hour_pct": 5, "surge_seven_day_pct": 3, "surge_window_s": 600}})
    return SimpleNamespace(cfg=cfg, store=Store(cfg.db_path), dir=tmp_path)


def sample(store, ts, pct, window=FIVE_HOUR, resets=RESET, source="usage_url"):
    store.add_quota_sample(QuotaSample(ts=ts, window=window, used_pct=pct, resets_at=resets,
                                       source=source, exact=True))


def test_surge_fires_on_fast_rise(st):
    now = 1_900_000_000.0
    sample(st.store, now - 480, 10)
    sample(st.store, now - 240, 13)
    sample(st.store, now, 16)
    why = breaker.surge(st.store, st.cfg, now)
    assert why and "5-hour usage rose 6 points in 8 min" in why


def test_surge_ignores_slow_rise_and_resets(st):
    now = 1_900_000_000.0
    sample(st.store, now - 3000, 10)       # outside the 10 min window
    sample(st.store, now - 300, 14)
    sample(st.store, now, 17)              # +3 in 5 min: below 5
    assert breaker.surge(st.store, st.cfg, now) is None
    # Window reset between readings: 90% -> 6% must not count, nor 6 -> 9 across it.
    sample(st.store, now - 200, 90, window=SEVEN_DAY, resets=RESET)
    sample(st.store, now, 6, window=SEVEN_DAY, resets=RESET + 7 * 86400)
    assert breaker.surge(st.store, st.cfg, now) is None


def test_weekly_surge(st):
    now = 1_900_000_000.0
    sample(st.store, now - 500, 40, window=SEVEN_DAY)
    sample(st.store, now, 43, window=SEVEN_DAY)
    assert "weekly usage rose 3 points" in breaker.surge(st.store, st.cfg, now)


def test_surge_off_by_default(tmp_path):
    cfg = Config({"general": {"state_dir": str(tmp_path)}})
    store = Store(cfg.db_path)
    now = 1_900_000_000.0
    sample(store, now - 60, 0)
    sample(store, now, 50)
    assert breaker.surge(store, cfg, now) is None


# -- daemon --------------------------------------------------------------------------

def _inv(store, inv_id, status, now):
    store.create_invocation(dict(id=inv_id, parent_id=None, root_id=inv_id, depth=1, pid=None, pgid=None,
                                 project="p", agent="a", argv="[]", prompt_hash="h", model=None,
                                 status=status, created=now, started=now, ended=None, heartbeat=now,
                                 exit_code=None, tokens_in=0, tokens_out=0, weighted=0.0, cost_usd=None,
                                 budget_weighted=None, session_id=None, reason=None))


def test_daemon_trips_on_surge_pauses_runs_and_resumes_after_reset(st, monkeypatch):
    monkeypatch.setattr(daemon, "_step_scan", lambda ctx: None)
    monkeypatch.setattr(daemon, "_step_bypass", lambda ctx: None)
    monkeypatch.setattr(daemon, "_step_reap", lambda ctx: None)
    now = util.now()
    _inv(st.store, "r1", "running", now)
    sample(st.store, now - 300, 20)
    sample(st.store, now - 1, 27)
    report = daemon.tick(st.cfg, st.store, now=now)
    assert report["breaker"] == "ok", report
    assert breaker.tripped(st.store)
    assert st.store.get_invocation("r1")["status"] == "suspend_requested"
    # The gateway would apply it; simulate that, then check nothing resumes while tripped.
    st.store.update_invocation("r1", status="suspended")
    daemon.tick(st.cfg, st.store, now=now + 15)
    assert st.store.get_invocation("r1")["status"] == "suspended"
    breaker.reset(st.store)
    st.store.conn.execute("DELETE FROM quota_samples")
    st.store.conn.commit()
    daemon.tick(st.cfg, st.store, now=now + 30)
    assert st.store.get_invocation("r1")["status"] == "resume_requested"


# -- hooks ---------------------------------------------------------------------------

def _pre(cfg, store, tool, tin):
    payload = json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": "/w/p",
                          "tool_name": tool, "tool_input": tin})
    r = hooks.run(payload, cfg=cfg, store=store)
    return json.loads(r.stdout) if r.stdout else None


def test_hook_denies_claude_p_only_while_tripped(tmp_path, monkeypatch):
    monkeypatch.delenv("CCTRIPWIRE_DISABLE", raising=False)
    cfg = Config({"general": {"state_dir": str(tmp_path)}})
    store = Store(cfg.db_path)
    cmd = "for i in $(seq 1000); do claude -p \"case $i\" --model opus & done"
    assert _pre(cfg, store, "Bash", {"command": cmd}) is None
    breaker.trip(store, cfg, "runaway: test")
    o = _pre(cfg, store, "Bash", {"command": cmd})
    assert o["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "breaker reset" in o["hookSpecificOutput"]["permissionDecisionReason"]
    assert _pre(cfg, store, "Bash", {"command": "pytest -q"}) is None


def test_runaway_only_preset_never_denies_repeated_work(tmp_path, monkeypatch):
    monkeypatch.delenv("CCTRIPWIRE_DISABLE", raising=False)
    import tomllib
    from pathlib import Path
    data = tomllib.loads((Path(__file__).parents[1] / "examples" / "runaway-only.toml").read_text())
    data.setdefault("general", {})["state_dir"] = str(tmp_path)
    cfg = Config(data)
    assert cfg.validate() == []
    store = Store(cfg.db_path)
    store.kv_set("override", None)
    for _ in range(30):
        assert _pre(cfg, store, "Bash", {"command": "pytest -q"}) is None
    for i in range(12):
        assert _pre(cfg, store, "Agent", {"prompt": f"task {i}"}) is None
    # Very high quota does not limit anything either: there are no level rules.
    sample(store, util.now(), 99)
    assert _pre(cfg, store, "Agent", {"prompt": "one more"}) is None


# -- usage_url source --------------------------------------------------------------------

PAYLOAD = {"providers": [
    {"id": "codex", "status": "ok", "updated_at": 1.0,
     "buckets": [{"windows": [{"label": "Weekly", "used_percent": 50, "resets_at": 9}]}]},
    {"id": "claude", "status": "ok", "updated_at": 1791523646.7,
     "buckets": [{"id": "claude", "windows": [
         {"label": "5-hour", "used_percent": 3, "resets_at": 1791526799.6, "reset_passed": False},
         {"label": "Weekly · all models", "used_percent": 4, "resets_at": 1792105199.6, "reset_passed": False},
         {"label": "Weekly · Fable", "used_percent": 0, "resets_at": 1792105200.0, "reset_passed": False},
         {"label": "5-hour", "used_percent": 80, "resets_at": 1.0, "reset_passed": True},
     ]}]},
]}


def test_usage_url_parse():
    got = {(s.window, s.used_pct) for s in usage_url.parse(PAYLOAD)}
    assert got == {(FIVE_HOUR, 3.0), (SEVEN_DAY, 4.0), ("seven_day_fable", 0.0)}
    s = usage_url.parse(PAYLOAD)[0]
    assert s.exact and s.source == "usage_url" and s.ts == 1791523646.7


def test_usage_url_bad_input():
    assert usage_url.parse(None) == []
    assert usage_url.parse({"providers": [{"id": "claude", "status": "unavailable"}]}) == []
    assert usage_url.parse({"providers": [{"id": "claude", "status": "ok", "updated_at": 1,
                                           "buckets": [{"windows": [{"label": "5-hour"}]}]}]}) == []


def test_daemon_trips_on_bypass_storm_and_freezes_them(st, monkeypatch):
    """claude -p started by absolute path skips the gateway; the daemon still catches a pile."""
    import signal as _signal
    from cctripwire import procs
    cfg = Config({"general": {"state_dir": str(st.dir)}, "daemon": {"bypass_storm_max": 3}})
    for name in ("scan", "bypass", "reap"):
        monkeypatch.setattr(daemon, f"_step_{name}", lambda ctx: None)
    fake = [SimpleNamespace(pid=100000 + i, start_time=i) for i in range(4)]
    monkeypatch.setattr(procs, "list_claude_processes", lambda: fake)
    monkeypatch.setattr(procs, "bypassers", lambda ps: list(ps))
    sent = []
    monkeypatch.setattr(daemon.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    daemon.tick(cfg, st.store, now=util.now())
    rec = breaker.tripped(st.store)
    assert rec and "4 claude -p processes running outside the gateway" in rec["reason"]
    assert sorted(p for p, s in sent if s == _signal.SIGSTOP) == [p.pid for p in fake]
    # Below the limit nothing happens.
    breaker.reset(st.store)
    st.store.kv_set(daemon.BYPASS_PAUSED_KEY, [])
    sent.clear()
    monkeypatch.setattr(procs, "list_claude_processes", lambda: fake[:2])
    daemon.tick(cfg, st.store, now=util.now())
    assert breaker.tripped(st.store) is None and not sent


# -- observe mode ----------------------------------------------------------------------

def test_observe_mode_gateway_never_refuses(make_env):
    env = make_env({"storm_max_queued": 0, "storm_max_started": 2, "max_duplicates": 0,
                    "storm_observe": True})
    for i in range(5):
        assert env.run(["--", "-p", f"job {i}"]).rc == 0
    assert fake_starts(env) == 5
    store = env.store()
    assert breaker.tripped(store) is None
    seen = [i for i in store.interventions(limit=100) if i["action"] == "breaker_would_trip"]
    assert len(seen) == 1 and "claude -p runs launched" in seen[0]["reason"]   # once per window


def test_observe_mode_daemon_records_but_never_pauses(st, monkeypatch):
    from cctripwire import procs
    st.cfg.data["gateway"]["storm_observe"] = True
    st.cfg.data.setdefault("daemon", {})["bypass_storm_max"] = 3
    for name in ("scan", "bypass", "reap"):
        monkeypatch.setattr(daemon, f"_step_{name}", lambda ctx: None)
    fake = [SimpleNamespace(pid=100000 + i, start_time=i) for i in range(4)]
    monkeypatch.setattr(procs, "list_claude_processes", lambda: fake)
    monkeypatch.setattr(procs, "bypassers", lambda ps: list(ps))
    sent = []
    monkeypatch.setattr(daemon.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    now = util.now()
    _inv(st.store, "r1", "running", now)
    sample(st.store, now - 300, 20)
    sample(st.store, now - 1, 27)
    for k in range(3):
        report = daemon.tick(st.cfg, st.store, now=now + 15 * k)
        assert report["breaker"].startswith("skipped: observe: surge"), report
    assert breaker.tripped(st.store) is None and not sent
    assert st.store.get_invocation("r1")["status"] == "running"
    assert [i["action"] for i in st.store.interventions(limit=100)].count("breaker_would_trip") == 1
    assert breaker.status(st.store, st.cfg)["last_would_trip"]["reason"].startswith("surge")
    # A manual trip still works in observe mode.
    breaker.trip(st.store, st.cfg, "by hand", force=True)
    assert breaker.tripped(st.store)

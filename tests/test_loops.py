from types import SimpleNamespace

import pytest

from cctripwire import util
from cctripwire.config import Config
from cctripwire.loops import LoopDetector
from cctripwire.models import Level
from cctripwire.store import Store


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CCTRIPWIRE_HOME", str(tmp_path))
    monkeypatch.delenv(util.ENV_DEPTH, raising=False)
    cfg = Config({"general": {"state_dir": str(tmp_path)}})
    store = Store(cfg.db_path)
    clock = {"t": 1_000_000.0}
    det = LoopDetector(store, cfg, now=lambda: clock["t"])
    return SimpleNamespace(store=store, cfg=cfg, clock=clock, det=det)


def kinds(dets):
    return [d.kind for d in dets]


LONG = "please fix the failing login test now"


def test_identical_prompt_threshold_and_dedupe(env):
    d = env.det
    assert d.on_prompt("s1", LONG) == []
    assert d.on_prompt("s1", "  PLEASE fix the failing   login test now ") == []
    dets = d.on_prompt("s1", LONG)
    assert kinds(dets) == ["identical_prompt"]
    assert dets[0].severity == Level.THROTTLE
    assert len(dets[0].advice) <= 200
    d.on_prompt("s1", LONG)   # still detected, but only one row is stored in 5 min
    rows = env.store.detections(session_id="s1")
    assert [r["kind"] for r in rows] == ["identical_prompt"]


def test_identical_prompt_other_session_not_counted(env):
    for _ in range(2):
        env.det.on_prompt("s1", LONG)
    assert env.det.on_prompt("s2", LONG) == []


@pytest.mark.parametrize("p", ["continue", "yes", "Continue.", "ok do it"])
def test_short_prompts_never_count(env, p):
    for _ in range(10):
        assert env.det.on_prompt("s1", p) == []


def test_repeated_tool_failure_denies_with_advice(env):
    d = env.det
    inp = {"command": "pytest -q tests/x.py"}
    for _ in range(4):
        assert d.on_tool_pre("s1", "Bash", inp) == []
        assert d.on_tool_result("s1", "Bash", inp, ok=False, error="ImportError: no module foo") is not None
    dets = d.on_tool_pre("s1", "Bash", inp)
    assert kinds(dets) == ["repeated_tool_failure"]
    assert dets[0].severity == Level.THROTTLE
    assert "failed 4×" in dets[0].advice and "ImportError" in dets[0].advice
    assert "Change approach" in dets[0].advice
    assert len(dets[0].advice) <= 200


def test_edit_between_failures_resets_count(env):
    d = env.det
    inp = {"command": "make test"}
    for _ in range(3):
        d.on_tool_pre("s1", "Bash", inp)
        d.on_tool_result("s1", "Bash", inp, ok=False, error="fail")
    d.on_tool_pre("s1", "Edit", {"file_path": "a.py"})
    d.on_tool_result("s1", "Edit", {"file_path": "a.py"}, ok=True)
    assert d.on_tool_pre("s1", "Bash", inp) == []
    for _ in range(3):
        d.on_tool_pre("s1", "Bash", inp)
        d.on_tool_result("s1", "Bash", inp, ok=False, error="fail")
    assert d.on_tool_pre("s1", "Bash", inp) == []   # 3 failures since the edit: below threshold


def test_excessive_retries(env):
    d = env.det
    inp = {"file_path": "/x/big.md"}
    for _ in range(5):
        assert d.on_tool_pre("s1", "Read", inp) == []
        d.on_tool_result("s1", "Read", inp, ok=True)
    dets = d.on_tool_pre("s1", "Read", inp)
    assert kinds(dets) == ["excessive_retries"]
    assert dets[0].severity == Level.THROTTLE and len(dets[0].advice) <= 200


def test_reread_after_edit_is_not_a_retry(env):
    d = env.det
    inp = {"file_path": "/x/a.py"}
    for _ in range(5):
        d.on_tool_pre("s1", "Read", inp)
        d.on_tool_result("s1", "Read", inp, ok=True)
    d.on_tool_pre("s1", "Edit", {"file_path": "/x/a.py", "old_string": "a", "new_string": "b"})
    d.on_tool_result("s1", "Edit", {"file_path": "/x/a.py", "old_string": "a", "new_string": "b"}, ok=True)
    assert d.on_tool_pre("s1", "Read", inp) == []


def test_no_progress_is_warn_only(env):
    d = env.det
    for i in range(39):
        assert d.on_tool_pre("s1", "Read", {"file_path": f"f{i}"}) == []
        d.on_tool_result("s1", "Read", {"file_path": f"f{i}"}, ok=True)
    dets = d.on_tool_pre("s1", "Read", {"file_path": "f39"})
    assert kinds(dets) == ["no_progress"]
    assert dets[0].severity == Level.WARN


def test_parallel_spawn(env):
    d = env.det
    for i in range(4):
        assert d.on_tool_pre("s1", "Agent", {"prompt": f"task {i}"}) == []
    dets = d.on_tool_pre("s1", "Agent", {"prompt": "task 4"})
    assert kinds(dets) == ["parallel_spawn"]
    assert dets[0].severity == Level.THROTTLE
    env.clock["t"] += 61   # outside the 60 s window the count resets
    assert d.on_tool_pre("s1", "Agent", {"prompt": "task 5"}) == []


def test_recursive_spawn_uses_env_depth(env, monkeypatch):
    monkeypatch.setenv(util.ENV_DEPTH, "2")
    assert env.det.on_tool_pre("s1", "Agent", {"prompt": "x"}) == []
    monkeypatch.setenv(util.ENV_DEPTH, "3")
    assert kinds(env.det.on_tool_pre("s1", "Agent", {"prompt": "y"})) == ["recursive_spawn"]


def test_context_rebuild_after_three_compactions(env):
    d = env.det
    assert d.on_compact("s1") == []
    env.clock["t"] += 120
    assert d.on_compact("s1") == []
    env.clock["t"] += 120
    dets = d.on_compact("s1")
    assert kinds(dets) == ["context_rebuild"]
    assert dets[0].severity == Level.WARN


def test_compaction_pre_and_post_hook_counted_once(env):
    d = env.det
    d.on_compact("s1")
    env.clock["t"] += 5   # PreCompact and SessionStart(compact) fire seconds apart
    d.on_compact("s1")
    env.clock["t"] += 120
    assert d.on_compact("s1") == []   # only two real compactions so far


def _tok(env, ts, weighted, i):
    env.store.add_token_event(ts=ts, session_id="s1", project="p", agent="a", model="claude-sonnet",
                              input=1, output=1, cache_create=0, cache_read=0, weighted=weighted,
                              msg_key=f"k{i}", invocation_id=None)


def test_token_spike_fires(env):
    now = env.clock["t"]
    for i, ago in enumerate([1200, 1800, 2400, 3000]):   # trailing baseline: 10k per 10 min
        _tok(env, now - ago, 10000, i)
    _tok(env, now - 60, 60000, 10)                        # burst in the last 10 min
    dets = env.det.check_token_spike()
    assert kinds(dets) == ["token_spike"]
    assert dets[0].severity == Level.WARN


def test_token_spike_quiet_when_proportional(env):
    now = env.clock["t"]
    for i, ago in enumerate([1200, 1800, 2400, 3000]):
        _tok(env, now - ago, 10000, i)
    _tok(env, now - 60, 12000, 10)
    assert env.det.check_token_spike() == []


def _inv(env, i, ph, status="done", created=None, depth=1):
    env.store.create_invocation({"id": f"inv{i}", "prompt_hash": ph, "status": status,
                                 "created": env.clock["t"] - 10 if created is None else created,
                                 "depth": depth})


def test_check_invocation_repeated_claude_p(env):
    for i in range(2):
        _inv(env, i, "h1")
    assert env.det.check_invocation("h1", None, 1) == []
    _inv(env, 2, "h1")
    dets = env.det.check_invocation("h1", "parent", 1)
    assert kinds(dets) == ["repeated_claude_p"]
    assert dets[0].severity == Level.THROTTLE and len(dets[0].advice) <= 200


def test_check_invocation_ignores_denied_and_old(env):
    for i in range(3):
        _inv(env, i, "h2", status="denied")
    _inv(env, 3, "h2", created=env.clock["t"] - 3600)
    assert env.det.check_invocation("h2", None, 1) == []


def test_check_invocation_recursive_depth(env):
    assert env.det.check_invocation("hx", "p", 2) == []
    assert kinds(env.det.check_invocation("hx", "p", 3)) == ["recursive_spawn"]

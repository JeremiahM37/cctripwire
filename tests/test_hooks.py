import json
import time
from types import SimpleNamespace

import pytest

from cctripwire import hooks, util
from cctripwire.config import Config
from cctripwire.store import Store


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CCTRIPWIRE_HOME", str(tmp_path))
    monkeypatch.delenv("CCTRIPWIRE_DISABLE", raising=False)
    monkeypatch.delenv(util.ENV_DEPTH, raising=False)
    monkeypatch.delenv(util.ENV_INVOCATION, raising=False)
    cfg = Config({"general": {"state_dir": str(tmp_path)}})
    store = Store(cfg.db_path)
    return SimpleNamespace(cfg=cfg, store=store, dir=tmp_path)


def force(store, level):
    store.kv_set("override", {"level": level, "until": None, "reason": f"test {level}"})


def ev(name, sid="s1", **kw):
    return json.dumps({"hook_event_name": name, "session_id": sid, "cwd": "/work/proj", **kw})


def pre(env, tool, tin, sid="s1"):
    return hooks.run(ev("PreToolUse", sid, tool_name=tool, tool_input=tin), cfg=env.cfg, store=env.store)


def out(res):
    return json.loads(res.stdout) if res.stdout else None


def deny_reason(res):
    o = out(res)
    assert o and o["hookSpecificOutput"]["permissionDecision"] == "deny", res
    return o["hookSpecificOutput"]["permissionDecisionReason"]


# -- OK / plumbing -----------------------------------------------------------------
def test_ok_level_no_output(env):
    r = pre(env, "Bash", {"command": "ls"})
    assert r.exit_code == 0 and r.stdout == "" and r.stderr == ""


def test_session_row_is_upserted(env):
    pre(env, "Bash", {"command": "ls"})
    s = env.store.get_session("s1")
    assert s["project"] == "proj" and s["cwd"] == "/work/proj"


def test_disabled_is_noop(env, monkeypatch):
    force(env.store, "stop")
    monkeypatch.setenv("CCTRIPWIRE_DISABLE", "1")
    r = pre(env, "Write", {"file_path": "x"})
    assert r.stdout == "" and r.exit_code == 0


def test_malformed_stdin_fails_open_and_logs(env):
    r = hooks.run("{not json", cfg=env.cfg, store=env.store)
    assert (r.exit_code, r.stdout) == (0, "")
    log = (env.dir / "hook-errors.log").read_text()
    assert "JSONDecodeError" in log


def test_handler_exception_fails_open_and_logs(env, monkeypatch):
    from cctripwire import engine

    def boom(*a, **k):
        raise RuntimeError("engine exploded")
    monkeypatch.setattr(engine, "decide", boom)
    r = pre(env, "Bash", {"command": "ls"})
    assert (r.exit_code, r.stdout) == (0, "")
    assert "RuntimeError: engine exploded" in (env.dir / "hook-errors.log").read_text()


# -- STOP -------------------------------------------------------------------------------
def test_stop_checkpoint_flow(env):
    force(env.store, "stop")
    deny = deny_reason(pre(env, "Bash", {"command": "ls"}))         # call 1: not a checkpoint tool
    assert deny.startswith("cctripwire:") and "CHECKPOINT.md" in deny and len(deny) <= 300
    assert pre(env, "Write", {"file_path": "CHECKPOINT.md"}).stdout == ""   # call 2: allowed
    assert pre(env, "Write", {"file_path": "CHECKPOINT.md"}).stdout == ""   # call 3: allowed
    r = pre(env, "Bash", {"command": "ls"})                                 # call 4: halt
    assert out(r)["continue"] is False
    assert out(r)["stopReason"].startswith("cctripwire:")
    assert env.store.get_session("s1")["stop_calls"] == 4


def test_stop_blocks_user_prompt(env):
    force(env.store, "stop")
    r = hooks.run(ev("UserPromptSubmit", prompt="Please keep working on the migration"),
                  cfg=env.cfg, store=env.store)
    o = out(r)
    assert o["decision"] == "block" and o["reason"].startswith("cctripwire: usage at STOP")
    assert "CCTRIPWIRE_DISABLE=1" in o["reason"]


def test_stop_and_precompact_never_block(env):
    force(env.store, "stop")
    for name in ("Stop", "PreCompact", "SessionEnd", "SubagentStop"):
        r = hooks.run(ev(name), cfg=env.cfg, store=env.store)
        assert r.exit_code == 0
        assert "continue" not in r.stdout and "decision" not in r.stdout


# -- RESTRICT -----------------------------------------------------------------------------
def test_restrict_denies_agent_and_claude_p_only(env):
    force(env.store, "restrict")
    assert "restricted" in deny_reason(pre(env, "Agent", {"prompt": "do x"}))
    assert "claude -p" in deny_reason(pre(env, "Bash", {"command": 'claude -p "summarise"'}))
    assert "claude -p" in deny_reason(pre(env, "Bash", {"command": "cd /w && /usr/bin/claude --print hi"}))
    assert pre(env, "Bash", {"command": "echo claude"}).stdout == ""
    assert pre(env, "Bash", {"command": 'grep -r "claude -p" src'}).stdout == ""
    assert pre(env, "Bash", {"command": "claude --version"}).stdout == ""


# -- THROTTLE ----------------------------------------------------------------------------
def test_throttle_limits_inflight_subagents(env):
    force(env.store, "throttle")
    assert pre(env, "Agent", {"prompt": "first"}).stdout == ""
    hooks.run(ev("SubagentStart", agent_id="a1", agent_type="Explore"), cfg=env.cfg, store=env.store)
    assert "one at a time" in deny_reason(pre(env, "Agent", {"prompt": "second"}))
    hooks.run(ev("SubagentStop", agent_id="a1"), cfg=env.cfg, store=env.store)
    assert pre(env, "Agent", {"prompt": "third"}).stdout == ""


def test_throttle_does_not_touch_plain_tools(env):
    force(env.store, "throttle")
    hooks.run(ev("SubagentStart", agent_id="a1"), cfg=env.cfg, store=env.store)
    assert pre(env, "Bash", {"command": "ls"}).stdout == ""


# -- loop detections on PreToolUse ----------------------------------------------------------
def test_repeated_failure_denied_at_pre_tool(env):
    inp = {"command": "pytest tests/x.py"}
    for _ in range(4):
        pre(env, "Bash", inp)
        hooks.run(ev("PostToolUseFailure", tool_name="Bash", tool_input=inp, error="boom 2"),
                  cfg=env.cfg, store=env.store)
    reason = deny_reason(pre(env, "Bash", inp))
    assert "failed 4×" in reason and reason.startswith("cctripwire:")


def test_edit_clears_failure_block(env):
    inp = {"command": "pytest tests/x.py"}
    for _ in range(4):
        pre(env, "Bash", inp)
        hooks.run(ev("PostToolUseFailure", tool_name="Bash", tool_input=inp, error="boom"),
                  cfg=env.cfg, store=env.store)
    assert pre(env, "Edit", {"file_path": "x.py"}).stdout == ""
    hooks.run(ev("PostToolUse", tool_name="Edit", tool_input={"file_path": "x.py"},
                 tool_response={"success": True}), cfg=env.cfg, store=env.store)
    assert pre(env, "Bash", inp).stdout == ""


# -- PostToolUse notification dedupe ---------------------------------------------------------
def test_post_tool_notification_is_deduped(env):
    force(env.store, "warn")
    first = hooks.run(ev("PostToolUse", tool_name="Read", tool_input={"file_path": "a"},
                         tool_response={}), cfg=env.cfg, store=env.store)
    assert "usage WARN" in out(first)["hookSpecificOutput"]["additionalContext"]
    second = hooks.run(ev("PostToolUse", tool_name="Read", tool_input={"file_path": "a"},
                          tool_response={}), cfg=env.cfg, store=env.store)
    assert second.stdout == ""


def test_post_tool_failure_detection_notifies_once(env):
    inp = {"command": "make"}
    results = []
    for _ in range(5):
        results.append(hooks.run(ev("PostToolUseFailure", tool_name="Bash", tool_input=inp, error="err x"),
                                 cfg=env.cfg, store=env.store).stdout)
    assert results[:3] == ["", "", ""]
    ctx = json.loads(results[3])
    assert "failed 4×" in ctx["hookSpecificOutput"]["additionalContext"]
    assert results[4] == ""


def test_post_tool_response_error_detection(env):
    assert hooks._response_failed({"is_error": True})
    assert hooks._response_failed({"exit_code": 2})
    assert hooks._response_failed({"interrupted": True})
    assert not hooks._response_failed({"exit_code": 0, "stdout": "ok"})
    assert not hooks._response_failed("plain text")


# -- SessionStart -------------------------------------------------------------------------------
def test_session_start_warn_adds_context(env):
    force(env.store, "warn")
    r = hooks.run(ev("SessionStart", source="startup"), cfg=env.cfg, store=env.store)
    ctx = out(r)["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("cctripwire:") and "WARN" in ctx and len(ctx) <= 300


def test_session_start_ok_silent(env):
    assert hooks.run(ev("SessionStart", source="startup"), cfg=env.cfg, store=env.store).stdout == ""


# -- latency ----------------------------------------------------------------------------------------
def test_pre_tool_latency_with_10k_events(env):
    now = util.now()
    rows = [(now - i, "s-bulk", "proj", "agent", "claude-sonnet", 10, 20, 0, 0, 30.0, f"bulk{i}", None)
            for i in range(10_000)]
    with env.store.tx() as c:
        c.executemany(
            "INSERT INTO token_events(ts,session_id,project,agent,model,input,output,cache_create,cache_read,"
            "weighted,msg_key,invocation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    pre(env, "Bash", {"command": "warm"})   # warm the decision cache and connection
    times = []
    for i in range(200):
        t0 = time.perf_counter()
        pre(env, "Bash", {"command": f"echo {i}"})
        times.append(time.perf_counter() - t0)
    times.sort()
    p95 = times[int(len(times) * 0.95) - 1]
    print(f"PreToolUse p95={p95 * 1000:.1f} ms (target < 60 ms)")
    assert p95 < 0.150


def test_enforcing_outputs_are_logged_as_interventions(tmp_path, monkeypatch):
    from cctripwire import hooks
    from cctripwire.config import Config
    from cctripwire.store import Store
    monkeypatch.setenv("CCTRIPWIRE_HOME", str(tmp_path))
    cfg = Config()
    st = Store(cfg.db_path)
    st.kv_set("override", {"level": "restrict"})
    payload = {"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": str(tmp_path),
               "tool_name": "Agent", "tool_input": {"prompt": "x"}}
    r = hooks.run(json.dumps(payload), cfg=cfg, store=st)
    assert "deny" in r.stdout
    acts = st.interventions()
    assert acts and acts[0]["action"] == "deny_tool" and acts[0]["target"] == "session:s1"


def test_stop_allowance_resets_after_recovery(tmp_path, monkeypatch):
    from cctripwire import hooks
    from cctripwire.config import Config
    from cctripwire.store import Store
    monkeypatch.setenv("CCTRIPWIRE_HOME", str(tmp_path))
    cfg = Config()
    st = Store(cfg.db_path)
    call = lambda tool: hooks.run(json.dumps({"hook_event_name": "PreToolUse", "session_id": "s", "cwd": "/w",
                                               "tool_name": tool, "tool_input": {}}), cfg=cfg, store=st).stdout
    st.kv_set("override", {"level": "stop"})
    for _ in range(3):
        call("Write")
    assert '"continue": false' in call("Write")          # allowance used up
    st.kv_set("override", None); st.kv_set("decision_cache", None)
    assert call("Read") == ""                            # recovered: counter reset
    st.kv_set("override", {"level": "stop"}); st.kv_set("decision_cache", None)
    assert call("Write") == ""                           # new STOP episode: checkpoint allowed again

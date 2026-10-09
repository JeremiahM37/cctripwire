"""Orchestrator-owned core: expressions, policy engine, store concurrency."""
import multiprocessing as mp
import time

import pytest

from cctripwire.config import Config
from cctripwire.expr import evaluate, compile_expr
from cctripwire.models import Level, Snapshot, WindowState
from cctripwire.policy import evaluate_policy, active_override
from cctripwire.store import Store


def ws(name, used=None, eta=None, elapsed=None, pace=None, exact=True):
    return WindowState(window=name, used_pct=used, resets_at=None, exact=exact, source="mock", age_s=1,
                       burn_pct_per_hour=None, eta_s=eta, elapsed_frac=elapsed, pace_ratio=pace)


def snap(**windows):
    return Snapshot(ts=time.time(), windows={k: ws(k, **v) for k, v in windows.items()})


# --- expressions ------------------------------------------------------------
def test_expr_none_never_fires():
    assert evaluate("x.used_pct >= 50", {"x": ws("x")}) is False
    assert evaluate("x.eta_s < 1800", {"x": ws("x")}) is False
    assert evaluate("missing.used_pct > 1", {}) is False
    assert evaluate("x.used_pct is None", {"x": ws("x")}) is True


@pytest.mark.parametrize("bad", ["__import__('os')", "x[0]", "(lambda: 1)()", "x.__class__", "f(1)"])
def test_expr_rejects_unsafe(bad):
    with pytest.raises((ValueError, SyntaxError)):
        compile_expr(bad)


def test_expr_arith_and_chain():
    assert evaluate("1 < a.b * 2 <= 10", {"a": {"b": 5}}) is True
    assert evaluate("a / 0 > 1", {"a": 3}) is False


# --- policy -----------------------------------------------------------------
def test_default_policy_levels():
    cfg = Config()
    assert evaluate_policy(snap(five_hour={"used": 10, "elapsed": 0.5}), cfg).level == Level.OK
    d = evaluate_policy(snap(five_hour={"used": 55, "elapsed": 0.2}), cfg)
    assert d.level == Level.WARN and "early-burn" in d.rules
    d = evaluate_policy(snap(five_hour={"used": 60, "eta": 900, "elapsed": 0.5}), cfg)
    assert d.level == Level.THROTTLE and d.max_concurrency == 1 and d.min_interval_s == 30
    d = evaluate_policy(snap(seven_day={"used": 88}), cfg)
    assert d.level == Level.RESTRICT and "Agent" in d.deny_tools and d.deny_spawn and d.downgrade_model == "haiku"
    d = evaluate_policy(snap(five_hour={"used": 96}), cfg)
    assert d.level == Level.STOP and d.max_concurrency == 0 and d.checkpoint and not d.allowed


def test_unknown_quota_is_not_zero_and_does_not_fire():
    d = evaluate_policy(snap(five_hour={}, seven_day={}), Config())
    assert d.level == Level.OK and d.reasons == []


def test_project_budget_and_override():
    cfg = Config({"budgets": {"projects": {"p": {"soft_weighted": 100, "hard_weighted": 200, "max_concurrency": 2}}}})
    s = snap(five_hour={"used": 1})
    s.by_project = {"p": 150}
    d = evaluate_policy(s, cfg, project="p")
    assert d.level == Level.THROTTLE and d.max_concurrency == 1
    s.by_project = {"p": 250}
    assert evaluate_policy(s, cfg, project="p").level == Level.STOP
    assert evaluate_policy(s, cfg, project="other").level == Level.OK
    s.by_project = {"p": 10}
    assert evaluate_policy(s, cfg, project="p").max_concurrency == 2
    d = evaluate_policy(s, cfg, project="p", override={"level": "ok", "reason": "manual"})
    assert d.level == Level.OK


def test_broken_rule_is_skipped():
    cfg = Config({"rules": [{"name": "bad", "when": "1 +", "level": "stop"},
                            {"name": "good", "when": "five_hour.used_pct > 5", "level": "warn"}]})
    assert cfg.validate()  # reports the bad rule
    assert evaluate_policy(snap(five_hour={"used": 10}), cfg).level == Level.WARN


def test_override_expiry(tmp_path):
    st = Store(tmp_path / "g.db")
    st.kv_set("override", {"level": "stop", "until": 100.0})
    assert active_override(st, 50.0)["level"] == "stop"
    assert active_override(st, 150.0) is None


# --- store concurrency ------------------------------------------------------
def _slot_worker(path, n, cap, out):
    st = Store(path)
    got = 0
    for i in range(n):
        with st.tx(immediate=True) as c:
            running = c.execute("SELECT COUNT(*) FROM invocations WHERE status='running'").fetchone()[0]
            if running < cap:
                c.execute("INSERT INTO invocations(id,status,created) VALUES (?,?,?)",
                          (f"{mp.current_process().pid}-{i}", "running", time.time()))
                got += 1
    out.put(got)


def test_immediate_tx_never_oversubscribes(tmp_path):
    path = tmp_path / "g.db"
    Store(path)
    q = mp.Queue()
    ps = [mp.Process(target=_slot_worker, args=(path, 50, 7, q)) for _ in range(6)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(30)
    assert sum(q.get() for _ in ps) == 7
    assert Store(path).invocations(status="running").__len__() == 7


def test_token_dedupe_and_attribution(tmp_path):
    st = Store(tmp_path / "g.db")
    kw = dict(ts=1, session_id="s", project="p", agent="a", model="m", input=1, output=1,
              cache_create=0, cache_read=0, weighted=6.0, msg_key="k")
    assert st.add_token_event(**kw) is True
    assert st.add_token_event(**kw, invocation_id="inv") is False
    assert st.token_totals(0).messages == 1
    assert st.token_totals(0, invocation_id="inv").weighted == 6.0


def test_prune_keeps_live_invocations(tmp_path):
    st = Store(tmp_path / "g.db")
    for i, status in enumerate(["done", "crashed", "running", "suspend_requested", "kill_requested", "queued"]):
        st.create_invocation({"id": str(i), "status": status, "created": 1.0})
    st.prune(older_than_s=10)
    assert sorted(r["status"] for r in st.invocations()) == ["kill_requested", "queued", "running", "suspend_requested"]


def test_validate_flags_misplaced_and_unknown_keys():
    cfg = Config({"daemon": {"extend_rules": True}, "gatway": {}, "gateway": {"on_stop": "wait"}})
    errs = cfg.validate()
    assert "unknown key 'daemon.extend_rules'" in errs and "unknown section or key 'gatway'" in errs
    assert not any("on_stop" in e for e in errs)
    assert Config().validate() == []


def _fresh_open_worker(path, q):
    try:
        Store(path).kv_set("x", 1)
        q.put(0)
    except Exception:  # noqa: BLE001
        q.put(1)


def test_concurrent_first_open_of_fresh_db(tmp_path):
    """Several processes creating the database at once must all succeed (WAL switch race)."""
    failures = 0
    for trial in range(10):
        path = tmp_path / f"t{trial}.db"
        q = mp.Queue()
        ps = [mp.Process(target=_fresh_open_worker, args=(path, q)) for _ in range(8)]
        for p in ps:
            p.start()
        for p in ps:
            p.join(30)
        failures += sum(q.get() for _ in ps)
    assert failures == 0

"""Status-line hook: exact samples from rate_limits, dedupe, unknown-not-zero, never raises."""
import json

import pytest

from cctripwire.config import Config
from cctripwire.models import FIVE_HOUR, SEVEN_DAY
from cctripwire.quota import statusline as S
from cctripwire.store import Store

NOW = 1_800_000_000.0


def payload(five=None, seven=None, sid="sess-1", cwd="/home/u/myrepo"):
    d = {"session_id": sid, "cwd": cwd, "transcript_path": "/x/y.jsonl",
         "workspace": {"current_dir": cwd}, "model": {"id": "claude-opus-4-1", "display_name": "Opus"},
         "cost": {"total_cost_usd": 0.1}}
    rl = {}
    if five is not None:
        rl["five_hour"] = five
    if seven is not None:
        rl["seven_day"] = seven
    if rl:
        d["rate_limits"] = rl
    return json.dumps(d)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "g.db")
    yield s
    s.close()


@pytest.fixture
def cfg():
    return Config({})


def test_stores_exact_samples_and_prints_summary(store, cfg):
    out = S.handle(payload(five={"used_percentage": 42.0, "resets_at": NOW + 7200},
                           seven={"used_percentage": 31.0, "resets_at": NOW + 3 * 86400}),
                   store, cfg, now=NOW)
    s5 = store.latest_quota(FIVE_HOUR, source="statusline")
    assert s5.used_pct == 42.0 and s5.exact and s5.resets_at == NOW + 7200
    assert store.latest_quota(SEVEN_DAY, source="statusline").used_pct == 31.0
    assert out.startswith("⛨ ")
    assert "5h 42%" in out and "7d 31%" in out
    assert out.endswith("OK")


def test_absent_rate_limits_is_unknown_not_zero(store, cfg):
    out = S.handle(payload(), store, cfg, now=NOW)
    assert store.latest_quota(FIVE_HOUR) is None
    assert store.latest_quota(SEVEN_DAY) is None
    assert "5h —" in out and "7d —" in out
    assert "0%" not in out


def test_per_window_partial_reading(store, cfg):
    S.handle(payload(seven={"used_percentage": 5.0, "resets_at": NOW + 99}), store, cfg, now=NOW)
    assert store.latest_quota(FIVE_HOUR) is None
    assert store.latest_quota(SEVEN_DAY, source="statusline").used_pct == 5.0


def test_identical_readings_deduped_within_60s(store, cfg):
    p = payload(five={"used_percentage": 10.0, "resets_at": NOW + 100})
    S.handle(p, store, cfg, now=NOW)
    S.handle(p, store, cfg, now=NOW + 30)
    assert len(store.quota_samples(FIVE_HOUR, 0)) == 1
    S.handle(p, store, cfg, now=NOW + 61)
    assert len(store.quota_samples(FIVE_HOUR, 0)) == 2


def test_changed_reading_stored_even_within_60s(store, cfg):
    S.handle(payload(five={"used_percentage": 10.0, "resets_at": NOW + 100}), store, cfg, now=NOW)
    S.handle(payload(five={"used_percentage": 11.0, "resets_at": NOW + 100}), store, cfg, now=NOW + 5)
    assert [s.used_pct for s in store.quota_samples(FIVE_HOUR, 0)] == [10.0, 11.0]


def test_session_upserted(store, cfg):
    S.handle(payload(), store, cfg, now=NOW)
    sess = store.get_session("sess-1")
    assert sess["project"] == "myrepo"
    assert sess["cwd"] == "/home/u/myrepo"
    assert sess["transcript_path"] == "/x/y.jsonl"
    assert sess["last_seen"] == NOW


def test_statusline_source_disabled_stores_nothing(tmp_path, store):
    cfg = Config({"sources": {"statusline": False}})
    S.handle(payload(five={"used_percentage": 42.0, "resets_at": NOW + 100}), store, cfg, now=NOW)
    assert store.latest_quota(FIVE_HOUR) is None


@pytest.mark.parametrize("bad", [
    "", "not json", "[1, 2]", '{"rate_limits": "oops"}',
    '{"rate_limits": {"five_hour": {"used_percentage": "x"}}}',
    '{"rate_limits": {"five_hour": {"used_percentage": true}}}',
])
def test_bad_input_never_raises(store, cfg, bad):
    out = S.handle(bad, store, cfg, now=NOW)
    assert out.startswith("⛨")


def test_chained_command_first_line_prepended(store, tmp_path):
    cfg = Config({"sources": {"statusline_chain": "echo hello; echo second"}})
    out = S.handle(payload(), store, cfg, now=NOW)
    lines = out.splitlines()
    assert lines[0] == "hello"
    assert lines[1].startswith("⛨")


def test_failing_chain_is_ignored(store):
    cfg = Config({"sources": {"statusline_chain": "no_such_command_cctripwire_test_xyz"}})
    out = S.handle(payload(), store, cfg, now=NOW)
    assert "\n" not in out and out.startswith("⛨")


def test_slow_chain_times_out(store, monkeypatch):
    monkeypatch.setattr(S, "CHAIN_TIMEOUT_S", 0.2)
    cfg = Config({"sources": {"statusline_chain": "sleep 5; echo late"}})
    out = S.handle(payload(), store, cfg, now=NOW)
    assert out.startswith("⛨") and "late" not in out

"""Window states, burn/ETA/pace, snapshot contents, and build_snapshot speed on 100k events."""
import random
import time

import pytest

from cctripwire.config import Config
from cctripwire.forecast import build_snapshot, window_state
from cctripwire.models import FIVE_HOUR, SEVEN_DAY, QuotaSample, WINDOW_SECONDS
from cctripwire.store import Store

NOW = 1_800_000_000.0


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "g.db")
    yield s
    s.close()


@pytest.fixture
def cfg():
    return Config({})


def reading(store, window, ts, used, resets, source="statusline"):
    store.add_quota_sample(QuotaSample(ts=ts, window=window, used_pct=used, resets_at=resets,
                                       source=source, exact=True))


def tokens(store, ts, weighted, i=[0]):
    i[0] += 1
    store.add_token_event(ts=ts, session_id="s", project="p", agent="a", model="m", input=0, output=0,
                          cache_create=0, cache_read=0, weighted=weighted, msg_key=f"t{i[0]}")


def test_fresh_exact_reading(store, cfg):
    reading(store, FIVE_HOUR, NOW - 60, 40.0, NOW + 7200)
    ws = window_state(store, cfg, FIVE_HOUR, NOW)
    assert ws.used_pct == 40.0 and ws.exact and ws.source == "statusline"
    assert ws.age_s == 60 and ws.stale is False
    assert ws.elapsed_frac == pytest.approx(1 - 7200 / WINDOW_SECONDS[FIVE_HOUR])
    assert ws.pace_ratio == pytest.approx(40.0 / (ws.elapsed_frac * 100))


def test_no_data_is_unknown_never_zero(store, cfg):
    ws = window_state(store, cfg, FIVE_HOUR, NOW)
    assert ws.used_pct is None and ws.exact is False
    assert ws.burn_pct_per_hour is None and ws.eta_s is None and ws.pace_ratio is None


def test_passed_reset_is_unknown(store, cfg):
    reading(store, FIVE_HOUR, NOW - 10, 90.0, NOW - 5)
    ws = window_state(store, cfg, FIVE_HOUR, NOW)
    assert ws.used_pct is None and ws.stale is True


def test_stale_reading_falls_back_to_estimate(store):
    cfg = Config({"sources": {"five_hour_budget": 1000}})          # 10 weighted tokens per 1%
    reading(store, FIVE_HOUR, NOW - 2000, 10.0, NOW + 10000)       # older than stale_after_s (900)
    tokens(store, NOW - 100, 200)
    ws = window_state(store, cfg, FIVE_HOUR, NOW)
    assert ws.used_pct == pytest.approx(30.0)
    assert ws.exact is False and ws.source == "estimate" and ws.stale is True


def test_burn_rate_from_least_squares_and_eta(store, cfg):
    resets = NOW + 10000
    for ts, used in ((NOW - 1200, 10.0), (NOW - 600, 15.0), (NOW, 20.0)):
        reading(store, FIVE_HOUR, ts, used, resets)
    ws = window_state(store, cfg, FIVE_HOUR, NOW)
    assert ws.burn_pct_per_hour == pytest.approx(30.0)            # 5 pct per 10 min
    assert ws.eta_s == pytest.approx(80 / 30 * 3600)              # 9600 s, before the reset
    assert ws.eta_s < resets - NOW


def test_eta_is_none_when_it_lands_after_reset(store, cfg):
    resets = NOW + 5000
    for ts, used in ((NOW - 1200, 10.0), (NOW - 600, 15.0), (NOW, 20.0)):
        reading(store, FIVE_HOUR, ts, used, resets)
    ws = window_state(store, cfg, FIVE_HOUR, NOW)
    assert ws.burn_pct_per_hour == pytest.approx(30.0)
    assert ws.eta_s is None


def test_burn_falls_back_to_token_velocity(store):
    cfg = Config({"sources": {"five_hour_budget": 1000}})          # 10 weighted tokens per 1%
    resets = NOW + 10000
    reading(store, FIVE_HOUR, NOW - 100, 10.0, resets)             # one reading: no slope
    tokens(store, NOW - 60, 900)                                   # 900 tokens in last 30 min
    ws = window_state(store, cfg, FIVE_HOUR, NOW)
    # 900 tokens = 90 pct over 0.5 h -> 180 pct per hour
    assert ws.burn_pct_per_hour == pytest.approx(180.0)
    assert ws.eta_s == pytest.approx(90 / 180 * 3600)


def test_no_pace_before_five_percent_of_window(store, cfg):
    reading(store, FIVE_HOUR, NOW, 3.0, NOW + WINDOW_SECONDS[FIVE_HOUR])
    ws = window_state(store, cfg, FIVE_HOUR, NOW)
    assert ws.elapsed_frac == pytest.approx(0.0)
    assert ws.pace_ratio is None


def test_snapshot_contents(store, cfg):
    reading(store, FIVE_HOUR, NOW - 30, 42.0, NOW + 3600)
    reading(store, SEVEN_DAY, NOW - 30, 31.0, NOW + 86400)
    reading(store, "seven_day_opus", NOW - 30, 12.0, NOW + 86400, source="oauth")
    tokens(store, NOW - 300, 6000)
    tokens(store, NOW - 4000, 1200)
    store.add_token_event(ts=NOW - 100, session_id="s9", project="other", agent="ag", model="m", input=0,
                          output=0, cache_create=0, cache_read=0, weighted=600, msg_key="other")
    store.create_invocation({"id": "inv1", "status": "running", "created": NOW, "project": "p"})
    store.create_invocation({"id": "inv2", "status": "done", "created": NOW, "project": "p"})
    store.upsert_session("s1", last_seen=NOW - 60)
    store.upsert_session("old", last_seen=NOW - 3600)
    snap = build_snapshot(store, cfg, now=NOW)
    assert set(snap.windows) == {FIVE_HOUR, SEVEN_DAY, "seven_day_opus"}
    assert snap.windows[FIVE_HOUR].used_pct == 42.0
    assert snap.tokens_per_min == pytest.approx((6000 + 600) / 10)
    assert snap.tokens_last_hour.weighted == pytest.approx(6000 + 600)   # the 4000 s-old event is outside
    assert snap.tokens_last_hour.messages == 2
    assert snap.running_invocations == 1
    assert snap.active_sessions == 3   # 1 hook-registered + 2 seen only through recent transcript usage
    assert snap.by_project["p"] == pytest.approx(6000 + 1200)
    assert snap.by_project["other"] == pytest.approx(600)


def test_snapshot_always_has_core_windows_even_when_empty(store, cfg):
    snap = build_snapshot(store, cfg, now=NOW)
    assert set(snap.windows) == {FIVE_HOUR, SEVEN_DAY}
    assert snap.windows[FIVE_HOUR].used_pct is None
    assert snap.by_project == {} and snap.tokens_per_min == 0


def test_build_snapshot_fast_with_100k_events(store, cfg):
    rng = random.Random(1)
    reading(store, FIVE_HOUR, NOW - 60, 50.0, NOW + 3600)
    reading(store, SEVEN_DAY, NOW - 60, 20.0, NOW + 86400)
    rows = []
    for i in range(100_000):
        rows.append((NOW - rng.uniform(0, 8 * 3600), f"s{i % 50}", f"proj{i % 20}", f"ag{i % 5}",
                     "claude-sonnet-4-5", 1, 2, 3, 4, rng.uniform(1, 500), f"m{i}", None))
    with store.tx() as c:
        c.executemany("INSERT INTO token_events(ts,session_id,project,agent,model,input,output,"
                      "cache_create,cache_read,weighted,msg_key,invocation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      rows)
    build_snapshot(store, cfg, now=NOW)            # warm the page cache
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        snap = build_snapshot(store, cfg, now=NOW)
        best = min(best, time.perf_counter() - t0)
    assert snap.tokens_last_hour.messages > 0
    # Target is < 20 ms; 50 ms leaves room for slow CI machines.
    assert best < 0.050, f"build_snapshot took {best * 1000:.1f} ms"

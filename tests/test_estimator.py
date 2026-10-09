"""Weights, calibration against exact readings, and window estimates."""
import itertools

import pytest

from cctripwire.config import Config
from cctripwire.models import FIVE_HOUR, QuotaSample
from cctripwire.quota import estimator as E
from cctripwire.store import Store

T0 = 1_800_000_000.0
RESET = T0 + 10_000
_seq = itertools.count()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "g.db")
    yield s
    s.close()


def add_tokens(store, ts, weighted):
    store.add_token_event(ts=ts, session_id="s", project="p", agent="a", model="m", input=0, output=0,
                          cache_create=0, cache_read=0, weighted=weighted, msg_key=f"k{next(_seq)}")


def add_reading(store, ts, used, resets=RESET, source="statusline"):
    store.add_quota_sample(QuotaSample(ts=ts, window=FIVE_HOUR, used_pct=used, resets_at=resets,
                                       source=source, exact=True))


# -- weights -------------------------------------------------------------------

def test_weight_formula_and_opus_factor():
    usage = {"input_tokens": 100, "output_tokens": 10, "cache_creation_input_tokens": 40,
             "cache_read_input_tokens": 1000}
    # (100*1 + 10*5 + 40*1.25 + 1000*0.1) = 300, times opus 5
    assert E.weight("claude-opus-4-1", usage) == pytest.approx(1500)


def test_model_factors():
    assert E.weight("claude-sonnet-4-5", {"input_tokens": 10}) == pytest.approx(10)
    assert E.weight("claude-haiku-4-5", {"output_tokens": 3}) == pytest.approx(5)
    assert E.weight("fable-5", {"input_tokens": 2}) == pytest.approx(10)
    assert E.weight("some-future-model", {"input_tokens": 7}) == pytest.approx(7)
    assert E.model_factor(None) == 1.0


def test_missing_and_bool_usage_fields_count_as_zero():
    assert E.weight(None, None) == 0
    assert E.weight("claude-sonnet", {"input_tokens": True, "output_tokens": "9"}) == 0
    assert E.counts({"cache_read_input_tokens": 4}) == (0, 0, 0, 4)


# -- calibration -----------------------------------------------------------------

def build_calibration(store):
    """Three pairs, each giving 500 weighted tokens per 1%, plus one outlier pair at 1000."""
    add_reading(store, T0, 10)
    add_tokens(store, T0 + 100, 1000)
    add_reading(store, T0 + 600, 12)             # +2 pct over 1000 tokens -> 500
    add_tokens(store, T0 + 900, 1500)
    add_reading(store, T0 + 1200, 15)            # +3 pct over 1500 tokens -> 500
    add_tokens(store, T0 + 1300, 10000)
    add_reading(store, T0 + 1500, 25)            # +10 pct over 10000 tokens -> 1000 (outlier)
    add_tokens(store, T0 + 1600, 500)


def test_calibrate_is_median_of_pairs(store):
    build_calibration(store)
    assert E.calibrate(store, FIVE_HOUR, now=T0 + 2000, max_age=0) == pytest.approx(500)


def test_calibrate_is_cached_in_kv(store):
    build_calibration(store)
    E.calibrate(store, FIVE_HOUR, now=T0 + 2000, max_age=0)
    add_reading(store, T0 + 1800, 40)            # would change the answer if recomputed
    assert E.calibrate(store, FIVE_HOUR, now=T0 + 2100, max_age=3600) == pytest.approx(500)
    assert store.kv_get("calib:five_hour")["pairs"] == 3


def test_calibrate_ignores_small_deltas_resets_and_missing_tokens(store):
    add_reading(store, T0, 10)
    add_tokens(store, T0 + 10, 1000)
    add_reading(store, T0 + 100, 11)                          # +1 is below the threshold
    add_reading(store, T0 + 200, 20, resets=RESET + 5 * 3600)        # different reset period
    add_reading(store, T0 + 300, 30, resets=RESET + 5 * 3600)        # +10 but no tokens in between
    assert E.calibrate(store, FIVE_HOUR, now=T0 + 400, max_age=0) is None


def test_calibrate_none_without_readings(store):
    assert E.calibrate(store, FIVE_HOUR, now=T0, max_age=0) is None


# -- estimates -------------------------------------------------------------------

def test_estimate_extrapolates_from_latest_exact_reading(store):
    build_calibration(store)                     # latest exact reading: 25% at T0+1500
    add_tokens(store, T0 + 1700, 2500)           # 500 (already in the build) + 2500 = 3000 / 500 = +6
    est = E.estimate(store, Config({}), FIVE_HOUR, now=T0 + 1800)
    assert est.used_pct == pytest.approx(31.0)
    assert est.exact is False and est.source == "estimate"
    assert est.resets_at == RESET


def test_estimate_uses_configured_budget_when_no_reading(store):
    cfg = Config({"sources": {"five_hour_budget": 10000}})
    add_tokens(store, T0 - 100, 1000)
    est = E.estimate(store, cfg, FIVE_HOUR, now=T0)
    assert est.used_pct == pytest.approx(10.0)       # 1000 / (10000/100)
    assert est.resets_at is None and est.exact is False


def test_estimate_none_without_budget_or_calibration(store):
    assert E.estimate(store, Config({}), FIVE_HOUR, now=T0) is None


def test_passed_reset_is_not_extrapolated_without_budget(store):
    build_calibration(store)
    add_reading(store, T0 + 3000, 50, resets=T0 + 2500)       # its reset has already passed
    assert E.estimate(store, Config({}), FIVE_HOUR, now=T0 + 3100) is None


def test_calibrates_from_whole_percent_steps():
    """Status-line readings rise 1 point at a time; calibration must still find pairs."""
    import tempfile, pathlib
    from cctripwire.store import Store
    from cctripwire.models import QuotaSample
    from cctripwire.quota.estimator import calibrate
    st = Store(pathlib.Path(tempfile.mkdtemp()) / "g.db")
    t0, reset = 1_000_000.0, 1_010_000.0
    for i in range(10):                        # 10%..19%, one point per minute
        st.add_quota_sample(QuotaSample(t0 + 60 * i, "five_hour", 10.0 + i, reset, "statusline", True))
        st.add_token_event(ts=t0 + 60 * i + 30, session_id="s", project="p", agent="a", model="m", input=0,
                           output=0, cache_create=0, cache_read=0, weighted=1000.0, msg_key=f"k{i}")
    assert calibrate(st, "five_hour", now=t0 + 600) == 1000.0   # 1000 weighted tokens per point

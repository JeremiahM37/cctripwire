"""OAuth usage poller. Never touches the network: urllib.request.urlopen is monkeypatched."""
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest

from cctripwire.config import Config
from cctripwire.models import FIVE_HOUR, SEVEN_DAY
from cctripwire.quota import oauth
from cctripwire.store import Store

NOW = 1_800_000_000.0
TOKEN = "SECRET-TOKEN-VALUE"
BODY = {
    "five_hour": {"utilization": 6.0, "resets_at": "2026-10-08T21:00:00.481943+00:00"},
    "seven_day": {"utilization": 31.5, "resets_at": "2026-10-12T00:00:00+00:00"},
    "seven_day_opus": None,
    "limits": [
        {"name": "Weekly Opus", "utilization": 12, "resets_at": "2026-10-12T00:00:00Z"},
        {"name": "Mystery Thing", "utilization": 3},            # no window hint: ignored
        {"name": "Seven Day Sonnet", "utilization": "x"},       # non-numeric: ignored
        {"window": "five_hour", "utilization": 9},              # duplicate of a known window
    ],
}


class FakeResp:
    def __init__(self, payload):
        self._data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "g.db")
    yield s
    s.close()


@pytest.fixture
def cfg(tmp_path):
    claude = tmp_path / "claude"
    claude.mkdir()
    (claude / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": TOKEN}}))
    return Config({"general": {"claude_dir": str(claude)}, "sources": {"oauth": True}})


@pytest.fixture
def net(monkeypatch):
    """Record urlopen calls; the response is set by the test via net.reply."""
    class Net:
        calls = []
        reply = None

    def fake_urlopen(req, timeout=None):
        Net.calls.append(req)
        if isinstance(Net.reply, BaseException):
            raise Net.reply
        return FakeResp(Net.reply)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return Net


def test_parse_known_windows():
    out = {s.window: s for s in oauth.parse_usage(BODY, NOW)}
    assert set(out) == {FIVE_HOUR, SEVEN_DAY, "weekly_opus"}     # seven_day_opus null, junk dropped
    five = out[FIVE_HOUR]
    assert five.used_pct == 6.0
    assert five.resets_at == datetime(2026, 10, 8, 21, 0, 0, 481943, tzinfo=timezone.utc).timestamp()
    assert five.exact and five.source == "oauth" and five.ts == NOW
    assert out["weekly_opus"].used_pct == 12
    assert out["weekly_opus"].resets_at == datetime(2026, 10, 12, tzinfo=timezone.utc).timestamp()


def test_numeric_reset_in_seconds_and_milliseconds():
    s = oauth.parse_usage({"five_hour": {"utilization": 1, "resets_at": NOW + 100},
                           "seven_day": {"utilization": 2, "resets_at": (NOW + 100) * 1000}}, NOW)
    by = {x.window: x for x in s}
    assert by[FIVE_HOUR].resets_at == NOW + 100
    assert by[SEVEN_DAY].resets_at == pytest.approx(NOW + 100)


def test_missing_reset_is_none_not_error():
    (s,) = oauth.parse_usage({"five_hour": {"utilization": 0}}, NOW)
    assert s.used_pct == 0.0 and s.resets_at is None


@pytest.mark.parametrize("data", [None, "text", [], {}, {"five_hour": "x"},
                                  {"five_hour": {"utilization": True}}, {"five_hour": {"resets_at": 5}},
                                  {"limits": "nope"}, {"limits": [None, 3]}])
def test_unexpected_shapes_yield_nothing(data):
    assert oauth.parse_usage(data, NOW) == []


def test_disabled_by_default_makes_no_request(tmp_path, store, net):
    cfg = Config({"general": {"claude_dir": str(tmp_path)}})     # sources.oauth defaults to False
    assert oauth.poll(store, cfg, now=NOW) == []
    assert net.calls == []


def test_success_stores_exact_samples_with_headers(store, cfg, net):
    net.reply = BODY
    samples = oauth.poll(store, cfg, now=NOW)
    assert {s.window for s in samples} == {FIVE_HOUR, SEVEN_DAY, "weekly_opus"}
    (req,) = net.calls
    assert req.get_header("Authorization") == f"Bearer {TOKEN}"
    assert req.get_header("Anthropic-beta") == oauth.BETA
    assert req.full_url == oauth.USAGE_URL
    assert store.latest_quota(FIVE_HOUR, source="oauth", exact=True).used_pct == 6.0
    assert store.kv_get("oauth_status") == "ok"


def test_token_never_persisted(store, cfg, net):
    net.reply = urllib.error.URLError("boom " + TOKEN)
    oauth.poll(store, cfg, now=NOW)
    dumped = json.dumps(store.conn.execute("SELECT * FROM kv").fetchall(), default=str)
    assert TOKEN not in dumped
    assert TOKEN not in str(store.kv_get("oauth_status"))


def test_min_interval_respected(store, cfg, net):
    net.reply = BODY
    oauth.poll(store, cfg, now=NOW)
    oauth.poll(store, cfg, now=NOW + 10)
    assert len(net.calls) == 1
    oauth.poll(store, cfg, now=NOW + 301)
    assert len(net.calls) == 2


def test_429_sets_cooldown(store, cfg, net):
    net.reply = urllib.error.HTTPError(oauth.USAGE_URL, 429, "Too Many Requests", {}, None)
    assert oauth.poll(store, cfg, now=NOW) == []
    assert store.kv_get("oauth_cooldown_until") == NOW + 900
    assert "429" in store.kv_get("oauth_status")
    assert oauth.poll(store, cfg, now=NOW + 400) == []     # past min interval but inside cooldown
    assert len(net.calls) == 1
    net.reply = BODY
    assert oauth.poll(store, cfg, now=NOW + 1000)
    assert len(net.calls) == 2


@pytest.mark.parametrize("failure", [
    urllib.error.URLError("no route"),
    urllib.error.HTTPError(oauth.USAGE_URL, 500, "Server Error", {}, None),
    TimeoutError("slow"),
    b"<html>not json</html>",
    {"unexpected": "shape"},
])
def test_surprises_degrade_to_empty_with_status(store, cfg, net, failure):
    net.reply = failure
    assert oauth.poll(store, cfg, now=NOW) == []
    assert store.kv_get("oauth_status").startswith("error")
    assert store.latest_quota(FIVE_HOUR, source="oauth") is None


def test_missing_credentials_makes_no_request(tmp_path, store, net):
    cfg = Config({"general": {"claude_dir": str(tmp_path)}, "sources": {"oauth": True}})
    assert oauth.poll(store, cfg, now=NOW) == []
    assert net.calls == []
    assert store.kv_get("oauth_status").startswith("error")

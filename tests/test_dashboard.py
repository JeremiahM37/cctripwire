"""Dashboard tests: a real ThreadingHTTPServer on port 0, hit with http.client.

Read-only checks against a temp store. Sibling modules are used when present;
the forecast and procs modules are stubbed only when they cannot be imported.
"""
from __future__ import annotations

import http.client
import importlib
import json
import sys
import threading
import types

import pytest

from cctripwire import dashboard, util
from cctripwire.config import load
from cctripwire.models import Detection, Level, QuotaSample, Snapshot, WindowState
from cctripwire.store import Store


def _stub_build_snapshot(store, cfg, now=None):
    t = util.now() if now is None else now
    snap = Snapshot(ts=t)
    for w in store.windows_seen():
        q = store.latest_quota(w)
        if q is not None:
            snap.windows[w] = WindowState(window=w, used_pct=q.used_pct, resets_at=q.resets_at, exact=q.exact,
                                          source=q.source, age_s=t - q.ts, burn_pct_per_hour=None,
                                          eta_s=None, elapsed_frac=None, pace_ratio=None)
    return snap


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CCTRIPWIRE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CCTRIPWIRE_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    for k in ("CCTRIPWIRE_TIME_OFFSET", "CCTRIPWIRE_DISABLE", "CCTRIPWIRE_PROJECT", "CCTRIPWIRE_AGENT"):
        monkeypatch.delenv(k, raising=False)
    try:
        importlib.import_module("cctripwire.forecast")
    except ImportError:
        stub = types.ModuleType("cctripwire.forecast")
        stub.build_snapshot = _stub_build_snapshot
        monkeypatch.setitem(sys.modules, "cctripwire.forecast", stub)
        monkeypatch.setattr(sys.modules["cctripwire"], "forecast", stub, raising=False)
    return tmp_path


@pytest.fixture
def server(env):
    cfg = load(None)
    store = Store(env / "state" / "tripwire.db")
    srv = dashboard.make_server(cfg, store, "127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespaceLike(port=srv.server_address[1], store=store, cfg=cfg)
    finally:
        srv.shutdown()
        srv.server_close()


class SimpleNamespaceLike:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _get(port: int, path: str, headers: dict | None = None, method: str = "GET"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


# -- pure helpers -----------------------------------------------------------------------

@pytest.mark.parametrize("w,expected", [
    ({"used_pct": None, "exact": True}, "unknown"),
    ({"used_pct": None, "exact": False}, "unknown"),
    ({"used_pct": 0.0, "exact": True}, "exact"),
    ({"used_pct": 12.0, "exact": False}, "estimate"),
])
def test_window_quality(w, expected):
    assert dashboard.window_quality(w) == expected


def test_json_has_no_nan():
    assert dashboard._json({"a": float("nan"), "b": [float("inf"), 1.5]}) == b'{"a":null,"b":[null,1.5]}'


def test_host_check_blocks_foreign_names():
    assert dashboard._host_ok("127.0.0.1:8377", "127.0.0.1")
    assert dashboard._host_ok("localhost:8377", "127.0.0.1")
    assert dashboard._host_ok("[::1]:8377", "::1")
    assert not dashboard._host_ok("evil.example:80", "127.0.0.1")
    assert dashboard._host_ok("anything.example", "0.0.0.0")   # all-interfaces bind: no allowlist


def test_make_server_defaults_to_loopback(env):
    cfg = load(None)
    store = Store(env / "state" / "tripwire.db")
    srv = dashboard.make_server(cfg, store, port=0)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()


# -- HTTP ---------------------------------------------------------------------------------

def test_page_is_self_contained_and_small(server):
    status, headers, body = _get(server.port, "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert "Content-Security-Policy" in headers
    assert len(body) < 25_000
    assert b"<title>cctripwire</title>" in body
    assert b'name="viewport"' in body
    assert b"prefers-color-scheme" in body
    assert b"api/status" in body and b"5000" in body
    # no CDN or external asset; the only http:// string is the SVG namespace
    assert b'src="http' not in body and b'href="http' not in body
    assert body.count(b"http://") == 1 and b"www.w3.org/2000/svg" in body
    # text is only ever set with textContent, never parsed as HTML
    assert b"innerHTML" not in body and b"insertAdjacentHTML" not in body


def test_all_api_endpoints_return_json(server):
    for path in ("/api/status", "/api/history?hours=24", "/api/loops", "/api/interventions",
                 "/api/processes", "/api/limits"):
        status, headers, body = _get(server.port, path)
        assert status == 200, path
        assert headers["Content-Type"].startswith("application/json"), path
        assert isinstance(json.loads(body), dict), path


def test_status_empty_db(server):
    _, _, body = _get(server.port, "/api/status")
    p = json.loads(body)
    assert p["level"] == "ok"
    assert [w["window"] for w in p["windows"]][:2] == ["five_hour", "seven_day"]
    assert all(w["quality"] == "unknown" and w["used_pct"] is None for w in p["windows"][:2])
    assert p["running_invocations"] == 0
    assert p["invocations"] == []
    assert p["sessions"] == []


def test_status_exact_badge_from_mock_sample(server):
    server.store.add_quota_sample(QuotaSample(ts=util.now(), window="five_hour", used_pct=61.0,
                                              resets_at=util.now() + 3600, source="mock", exact=True))
    _, _, body = _get(server.port, "/api/status")
    w = {x["window"]: x for x in json.loads(body)["windows"]}["five_hour"]
    assert w["quality"] == "exact"
    assert w["used_pct"] == pytest.approx(61.0)


def test_history_buckets_and_samples(server):
    server.store.add_token_event(ts=util.now() - 300, session_id="s", project="p", agent="a", model="haiku",
                                 input=1, output=1, cache_create=0, cache_read=0, weighted=100.0, msg_key="k1")
    server.store.add_quota_sample(QuotaSample(ts=util.now() - 60, window="five_hour", used_pct=20.0,
                                              resets_at=util.now() + 3600, source="mock", exact=True))
    _, _, body = _get(server.port, "/api/history?hours=24")
    h = json.loads(body)
    assert sum(b["weighted"] for b in h["buckets"]) == pytest.approx(100.0)
    assert h["bucket_s"] == 300
    assert h["samples"]["five_hour"][0]["used_pct"] == pytest.approx(20.0)
    assert h["samples"]["seven_day"] == []


def test_loops_and_interventions_are_returned_raw(server):
    server.store.add_detection(Detection(kind="identical_prompt", severity=Level.THROTTLE, session_id="s1",
                                         detail="<script>alert(1)</script>", advice="stop"))
    server.store.add_intervention("suspend", "inv7", "over cap", level=Level.THROTTLE)
    _, _, body = _get(server.port, "/api/loops?hours=24")
    rows = json.loads(body)["detections"]
    assert rows[0]["detail"] == "<script>alert(1)</script>"   # escaping is the page's job
    assert rows[0]["severity"] == "throttle"
    _, _, body = _get(server.port, "/api/interventions?hours=24")
    ivs = json.loads(body)["interventions"]
    assert ivs[0]["action"] == "suspend" and ivs[0]["level"] == "throttle"


def test_processes_never_500s(server):
    status, _, body = _get(server.port, "/api/processes")
    assert status == 200
    assert "processes" in json.loads(body)


def test_limits_reports_defaults(server):
    _, _, body = _get(server.port, "/api/limits")
    p = json.loads(body)
    assert p["gateway"]["max_concurrency"] == 4
    assert any(r["name"] == "critical-5h" for r in p["rules"])
    assert p["problems"] == []


def test_unknown_path_is_404(server):
    status, _, body = _get(server.port, "/api/nope")
    assert status == 404
    assert json.loads(body)["error"] == "not found"


def test_writes_are_not_accepted(server):
    status, _, _ = _get(server.port, "/api/status", method="POST")
    assert status >= 400


def test_foreign_host_header_is_rejected(server):
    status, _, _ = _get(server.port, "/api/status", headers={"Host": "evil.example:80"})
    assert status == 403
    status, _, _ = _get(server.port, "/api/status", headers={"Host": f"localhost:{server.port}"})
    assert status == 200


def test_index_html_alias(server):
    status, _, body = _get(server.port, "/index.html")
    assert status == 200 and b"cctripwire" in body

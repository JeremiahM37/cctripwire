"""Read-only web dashboard and the JSON payloads it shares with the CLI.

The payload builders live here, not in cli.py, so `cctripwire status --json` and
`GET /api/status` are the same data. Every route is GET-only and reads the shared
store; nothing served here changes state (control stays in the CLI). The policy
engine and process scanner are imported inside the builders so importing this
module stays cheap.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .models import FIVE_HOUR, SEVEN_DAY, Level, WindowState
from .util import now as _now

WINDOW_ORDER = (FIVE_HOUR, SEVEN_DAY)
LIVE_SESSION_S = 900
LIVE_INVOCATION_STATES = ("queued", "running", "suspended")
HTML_TYPE = "text/html; charset=utf-8"
JSON_TYPE = "application/json; charset=utf-8"
PAGE_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
_LOOPBACK = ("localhost", "127.0.0.1", "::1")
_ALL_IFACES = ("", "0.0.0.0", "::")


def attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from a dict or an object (procs rows may be either)."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def window_quality(w: dict) -> str:
    """'unknown' (no reading), 'exact' (provider-reported) or 'estimate' (inferred)."""
    if w.get("used_pct") is None:
        return "unknown"
    return "exact" if w.get("exact") else "estimate"


def _window_dict(name: str, ws: WindowState | None) -> dict:
    if ws is None:
        ws = WindowState(window=name, used_pct=None, resets_at=None, exact=False, source="none",
                         age_s=None, burn_pct_per_hour=None, eta_s=None, elapsed_frac=None, pace_ratio=None)
    d = ws.as_dict()
    d["quality"] = window_quality(d)
    return d


def ordered_windows(snap) -> list[dict]:
    """five_hour and seven_day are always present (unknown if no data); others follow by name."""
    names = list(WINDOW_ORDER) + sorted(n for n in snap.windows if n not in WINDOW_ORDER)
    return [_window_dict(n, snap.windows.get(n)) for n in names]


def _top(d: dict, n: int = 10) -> dict:
    return dict(sorted(d.items(), key=lambda kv: -float(kv[1] or 0))[:n])


def _level_name(v: Any) -> str:
    try:
        return str(Level(int(v)))
    except (TypeError, ValueError):
        return "?"


def _inv_row(r: dict) -> dict:
    # argv and prompt_hash are left out on purpose: they can carry prompt text.
    keys = ("id", "parent_id", "depth", "status", "project", "agent", "model", "created", "started",
            "ended", "tokens_in", "tokens_out", "weighted", "reason")
    out = {k: r.get(k) for k in keys}
    out["weighted"] = out["weighted"] or 0.0
    return out


def _session_row(r: dict) -> dict:
    return {k: r.get(k) for k in ("session_id", "project", "agent", "status", "last_seen", "stop_calls")}


def status_payload(cfg, store, *, max_age: float = 2.0, now: float | None = None) -> dict:
    from . import breaker, engine
    from .policy import active_override

    t = _now() if now is None else now
    snap, dec = engine.decide(store, cfg, now=t, max_age=max_age)
    return {
        "ts": t,
        "level": str(dec.level),
        "level_num": int(dec.level),
        "reasons": list(dec.reasons),
        "rules": list(dec.rules),
        "max_concurrency": dec.max_concurrency,
        "min_interval_s": dec.min_interval_s,
        "deny_tools": list(dec.deny_tools),
        "deny_spawn": dec.deny_spawn,
        "downgrade_model": dec.downgrade_model,
        "checkpoint": dec.checkpoint,
        "override": active_override(store, t),
        "breaker": breaker.status(store, cfg, t),
        "windows": ordered_windows(snap),
        "tokens_per_min": snap.tokens_per_min,
        "tokens_last_hour": asdict(snap.tokens_last_hour),
        "running_invocations": snap.running_invocations,
        "active_sessions": snap.active_sessions,
        "by_project": _top(snap.by_project),
        "by_agent": _top(snap.by_agent),
        "invocations": [_inv_row(r) for r in store.invocations(status=list(LIVE_INVOCATION_STATES), limit=50)],
        "sessions": [_session_row(r) for r in store.active_sessions(t - LIVE_SESSION_S)[:50]],
    }


def history_payload(cfg, store, *, hours: float = 24.0, bucket_s: int = 300, now: float | None = None) -> dict:
    t = _now() if now is None else now
    hours = min(max(float(hours), 0.25), 24.0 * 14)
    bucket_s = max(60, int(bucket_s))
    since = t - hours * 3600
    return {
        "since": since,
        "until": t,
        "bucket_s": bucket_s,
        "buckets": [{"ts": ts, "weighted": v} for ts, v in store.token_series(since, bucket_s)],
        "samples": {w: [{"ts": q.ts, "used_pct": q.used_pct, "exact": q.exact, "source": q.source}
                        for q in store.quota_samples(w, since)]
                    for w in (FIVE_HOUR, SEVEN_DAY)},
    }


def loops_payload(store, *, window_s: float = 86400.0, now: float | None = None) -> dict:
    since = (_now() if now is None else now) - window_s
    rows = store.detections(since=since, limit=100)
    return {"detections": [{"ts": r["ts"], "kind": r["kind"], "severity": _level_name(r["severity"]),
                            "session_id": r["session_id"], "invocation_id": r["invocation_id"],
                            "detail": r["detail"], "advice": r["advice"], "count": r["count"]}
                           for r in rows]}


def interventions_payload(store, *, window_s: float = 86400.0, now: float | None = None) -> dict:
    since = (_now() if now is None else now) - window_s
    rows = store.interventions(since=since, limit=100)
    return {"interventions": [{"ts": r["ts"], "action": r["action"], "target": r["target"],
                               "level": _level_name(r["level"]), "reason": r["reason"], "policy": r["policy"]}
                              for r in rows]}


def processes_payload() -> dict:
    """Live claude processes. A scan failure is reported in `error`, never raised."""
    try:
        from . import procs
        ps = list(procs.list_claude_processes())
        byp = {attr(p, "pid") for p in procs.bypassers(ps)}
    except Exception as e:  # noqa: BLE001 - the page shows the reason instead of breaking
        return {"processes": [], "error": f"{type(e).__name__}: {e}"}
    rows = []
    for p in ps:
        argv = attr(p, "cmdline") or []
        rows.append({"pid": attr(p, "pid"), "ppid": attr(p, "ppid"), "pgid": attr(p, "pgid"),
                     "rss_kb": attr(p, "rss_kb"), "cpu": attr(p, "cpu"),
                     "invocation_id": attr(p, "invocation_id"), "is_print": bool(attr(p, "is_print")),
                     "bypass": attr(p, "pid") in byp,
                     "cmd": os.path.basename(str(argv[0])) if argv else "?"})
    return {"processes": rows, "error": None}


def limits_payload(cfg) -> dict:
    return {"rules": cfg.get("rules", []), "levels": cfg.get("levels", {}),
            "budgets": cfg.get("budgets", {}), "gateway": cfg.get("gateway", {}),
            "dashboard": cfg.get("dashboard", {}), "problems": cfg.validate()}


def _finite(o: Any) -> Any:
    """JSON has no NaN/Infinity; the browser's JSON.parse would reject them."""
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, dict):
        return {str(k): _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    return o


def _json(obj: Any) -> bytes:
    return json.dumps(_finite(obj), default=str, separators=(",", ":")).encode("utf-8")


def page_bytes() -> bytes:
    return resources.files("cctripwire").joinpath("web/index.html").read_bytes()


def _num(query: dict, key: str, default: float) -> float:
    try:
        v = float(query.get(key, [default])[0])
    except (TypeError, ValueError, IndexError):
        return default
    return v if math.isfinite(v) else default


def route(path: str, query: dict, cfg, store) -> tuple[int, str, bytes]:
    if path in ("/", "/index.html"):
        return 200, HTML_TYPE, page_bytes()
    hours = _num(query, "hours", 24.0)
    window_s = min(max(hours, 0.25), 24.0 * 14) * 3600
    if path == "/api/status":
        data = status_payload(cfg, store)
    elif path == "/api/history":
        data = history_payload(cfg, store, hours=hours)
    elif path == "/api/loops":
        data = loops_payload(store, window_s=window_s)
    elif path == "/api/interventions":
        data = interventions_payload(store, window_s=window_s)
    elif path == "/api/processes":
        data = processes_payload()
    elif path == "/api/limits":
        data = limits_payload(cfg)
    else:
        return 404, JSON_TYPE, _json({"error": "not found", "path": path})
    return 200, JSON_TYPE, _json(data)


def _host_name(host: str) -> str:
    host = host.strip()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host
    return host.split(":", 1)[0].lower()


def _host_ok(host_header: str, bound_host: str) -> bool:
    """Reject foreign Host names when bound to loopback (DNS-rebinding guard)."""
    if bound_host in _ALL_IFACES:
        return True    # reachable on purpose (0.0.0.0); no Host allowlist
    name = _host_name(host_header or "")
    return not name or name in _LOOPBACK or name == bound_host.lower()


def _handler(cfg, store, bound_host: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "cctripwire-dashboard"
        sys_version = ""

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if not _host_ok(self.headers.get("Host", ""), bound_host):
                self._reply(403, "text/plain; charset=utf-8", b"cctripwire: unexpected Host header\n")
                return
            url = urlsplit(self.path)
            try:
                status, ctype, body = route(url.path, parse_qs(url.query), cfg, store)
            except Exception as e:  # noqa: BLE001 - report, keep the server up
                status, ctype, body = 500, JSON_TYPE, _json({"error": f"{type(e).__name__}: {e}"})
            self._reply(status, ctype, body)

        def _reply(self, status: int, ctype: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy", PAGE_CSP)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            return  # keep the terminal quiet; the CLI prints the URL once

    return Handler


def make_server(cfg, store, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    """Bind without serving. Port 0 picks a free port (see server.server_address)."""
    h = host if host is not None else str(cfg.get("dashboard.host", "127.0.0.1"))
    p = int(port) if port is not None else int(cfg.get("dashboard.port", 8377))
    srv = ThreadingHTTPServer((h, p), _handler(cfg, store, h))
    srv.daemon_threads = True
    return srv


def serve(cfg, store, host: str | None = None, port: int | None = None) -> None:
    srv = make_server(cfg, store, host, port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


__all__ = ["serve", "make_server", "status_payload", "history_payload", "loops_payload",
           "interventions_payload", "processes_payload", "limits_payload", "window_quality", "route"]

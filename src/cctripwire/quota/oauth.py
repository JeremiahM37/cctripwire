"""Opt-in poll of the account usage endpoint (undocumented; disabled unless sources.oauth).

Never logs or stores the access token. Any surprise yields [] and a kv `oauth_status`.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from .. import util
from ..models import QuotaSample

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BETA = "oauth-2025-04-20"
KNOWN_WINDOWS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")
WINDOW_HINT = re.compile(r"(hour|day|week|month|session|opus|sonnet)")


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _reset(v: Any) -> Optional[float]:
    """resets_at as unix seconds: ISO 8601 string (naive = UTC) or a number (ms if huge)."""
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    n = _num(v)
    if n is None:
        return None
    return n / 1000 if n > 1e11 else n


def _sanitize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def parse_usage(data: Any, now: float) -> list[QuotaSample]:
    """Pure: turn an endpoint response into exact samples. Unknown shapes yield fewer samples."""
    if not isinstance(data, dict):
        return []
    out: list[QuotaSample] = []
    seen = set()

    def add(name: str, item: Any) -> None:
        if not isinstance(item, dict) or name in seen:
            return
        used = _num(item.get("utilization"))
        if used is None:
            return
        seen.add(name)
        out.append(QuotaSample(ts=now, window=name, used_pct=used, resets_at=_reset(item.get("resets_at")),
                               source="oauth", exact=True))

    for key in KNOWN_WINDOWS:
        add(key, data.get(key))
    limits = data.get("limits")
    if isinstance(limits, list):
        for item in limits:
            if not isinstance(item, dict):
                continue
            raw = next((item[k] for k in ("window", "name", "type", "id")
                        if isinstance(item.get(k), str) and item.get(k)), "")
            name = _sanitize(raw)
            if name and WINDOW_HINT.search(name):
                add(name, item)
    return out


def _state(store, status: str) -> None:
    store.kv_set("oauth_status", status)


def poll(store, cfg, now: float | None = None) -> list[QuotaSample]:
    """Fetch and store exact samples. Returns the samples stored (empty when skipped or failed)."""
    t = util.now() if now is None else now
    if not cfg.get("sources.oauth"):
        return []
    if t < (store.kv_get("oauth_cooldown_until") or 0):
        return []
    last = store.kv_get("oauth_last")
    if last is not None and t - last < float(cfg.get("sources.oauth_min_interval_s", 300)):
        return []
    store.kv_set("oauth_last", t)
    try:
        with open(cfg.claude_dir / ".credentials.json", "rb") as f:
            token = json.load(f)["claudeAiOauth"]["accessToken"]
    except (OSError, ValueError, KeyError, TypeError):
        _state(store, "error: no OAuth credentials")
        return []
    req = urllib.request.Request(USAGE_URL, headers={"Authorization": f"Bearer {token}",
                                                     "anthropic-beta": BETA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
        samples = parse_usage(body, t)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            store.kv_set("oauth_cooldown_until", t + float(cfg.get("sources.oauth_cooldown_s", 900)))
            _state(store, "error: rate limited (429)")
        else:
            _state(store, f"error: HTTP {e.code}")
        return []
    except Exception as e:  # noqa: BLE001 - undocumented endpoint: any failure degrades to []
        _state(store, f"error: {type(e).__name__}: {str(e).replace(token, '***')[:120]}")
        return []
    for s in samples:
        store.add_quota_sample(s)
    _state(store, "ok" if samples else "error: no usable windows in response")
    return samples


__all__ = ["parse_usage", "poll"]

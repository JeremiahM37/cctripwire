"""Read exact quota from an HTTP endpoint that already polls the provider (opt-in).

`sources.usage_url` points at a JSON document in the "agent-usage" shape served by a
usage endpoint (see docs/usage-url.md):

    {"providers": [{"id": "claude", "status": "ok", "updated_at": <epoch>,
                    "buckets": [{"windows": [{"label": "5-hour", "used_percent": 3,
                                              "resets_at": <epoch>, "reset_passed": false}]}]}]}

Reusing a poller that already exists means cctripwire adds no second client of the
undocumented usage endpoint. Samples are stamped with the provider's `updated_at`, so a
cached reading is never presented as fresher than it is. Any surprise yields [].
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Any

from ..models import FIVE_HOUR, SEVEN_DAY, QuotaSample

SOURCE = "usage_url"
STATUS_KEY = "usage_url_status"
LAST_KEY = "usage_url_last"


def _window(label: str) -> str | None:
    t = label.lower()
    if "5" in t and "hour" in t:
        return FIVE_HOUR
    if "week" in t:
        if "all" in t or "·" not in label:
            return SEVEN_DAY
        scope = re.sub(r"[^a-z0-9]+", "_", label.split("·", 1)[1].lower()).strip("_")
        return f"seven_day_{scope}" if scope else None
    return None


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def parse(data: Any, provider: str = "claude") -> list[QuotaSample]:
    """Pure: agent-usage JSON -> exact samples for `provider`."""
    if not isinstance(data, dict):
        return []
    out: list[QuotaSample] = []
    for p in data.get("providers") or []:
        if not isinstance(p, dict) or p.get("id") != provider or p.get("status") != "ok":
            continue
        ts = _num(p.get("updated_at"))
        if ts is None:
            continue
        for b in p.get("buckets") or []:
            for w in (b or {}).get("windows") or [] if isinstance(b, dict) else []:
                if not isinstance(w, dict) or w.get("reset_passed"):
                    continue
                name = _window(str(w.get("label") or ""))
                used = _num(w.get("used_percent"))
                if name is None or used is None:
                    continue
                out.append(QuotaSample(ts=ts, window=name, used_pct=used,
                                       resets_at=_num(w.get("resets_at")), source=SOURCE, exact=True))
    return out


def poll(store, cfg, now: float) -> list[QuotaSample]:
    url = str(cfg.get("sources.usage_url", "") or "")
    if not url:
        return []
    last = store.kv_get(LAST_KEY)
    if last is not None and now - float(last) < float(cfg.get("sources.usage_url_interval_s", 60)):
        return []
    store.kv_set(LAST_KEY, now)
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "cctripwire"})
        with urllib.request.urlopen(req, timeout=20) as r:
            samples = parse(json.loads(r.read()))
    except Exception as e:  # noqa: BLE001 - an unavailable source is reported, never fatal
        store.kv_set(STATUS_KEY, f"error: {type(e).__name__}")
        return []
    store.kv_set(STATUS_KEY, "ok" if samples else "no claude windows in response")
    return samples


__all__ = ["parse", "poll", "SOURCE"]

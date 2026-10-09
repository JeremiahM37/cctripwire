# `sources.usage_url`: reading usage from an endpoint you already run

The surge trigger needs **exact** 5-hour and weekly percentages. Claude Code reports them to the
status line, which `cctripwire setup` installs. If you already run something that polls your
plan's usage (a dashboard, a menu-bar widget with an HTTP API), point cctripwire at it instead,
so it adds no second poller:

```toml
[sources]
usage_url = "http://127.0.0.1:9000/api/agent-usage"
usage_url_interval_s = 60
```

The endpoint must return JSON in this shape. Only the `claude` provider is read; providers whose
`status` is not `ok`, and windows with `reset_passed: true`, are skipped.

```json
{"providers": [{"id": "claude", "status": "ok", "updated_at": 1791528542.8,
  "buckets": [{"windows": [
    {"label": "5-hour",              "used_percent": 31, "resets_at": 1791544799},
    {"label": "Weekly · all models", "used_percent": 22, "resets_at": 1792105199},
    {"label": "Weekly · Opus",       "used_percent": 9,  "resets_at": 1792105200}
  ]}]}]}
```

`5-hour` maps to the 5-hour window, `Weekly · all models` to the weekly window, and any other
`Weekly · X` to a model-scoped weekly meter. `updated_at` is used as the reading time, so a
cached response is never mistaken for a fresh one. Failures are reported by `cctripwire doctor`
and never read as 0 %.

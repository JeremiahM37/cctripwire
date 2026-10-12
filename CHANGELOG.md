# Changelog

## 0.1.0 (unreleased)

First public release.

- Runaway breaker on headless `claude -p` runs: trips on a burst of queued launches, too many
  launches in a window, a surge in exact 5-hour or weekly utilisation, or processes that
  bypass the shim (many at once, or many launched within the window). While tripped, new launches are refused at no cost, running ones are
  paused, and agents are told to stop and ask.
- Observe mode (`storm_observe`) that only records and notifies would-trips.
- `cctripwire setup` / `teardown`: one-command install of the runaway-only preset, hooks, status
  line, shim and daemon (systemd user service, or launchd on macOS).
- `cctripwire demo`: a 200-launch storm against a stand-in `claude`, spending no tokens.
- Budget mode: quota levels, per-project and per-agent budgets, loop detection, checkpoints.
- Web dashboard, `status` / `top` / `history` / `interventions` CLI views.

# Limitations

These are things cctripwire cannot see or cannot stop, and how much each one matters.

## Measurement

- **Exact utilisation exists only where Claude Code reports it.** The 5-hour and weekly
  percentages come from the status-line `rate_limits`. They are present only for Pro/Max
  accounts, only after a session's first response, and only in sessions that run the cctripwire
  status line. Headless `claude -p` runs have no status line. If no interactive session with the
  status line is open, readings go stale after `stale_after_s` (15 min). After that cctripwire uses a
  calibrated estimate (marked `~`) or reports *unknown*.
- **Estimates are approximations.** Allowance accounting is not published. cctripwire converts
  tokens to cost-weighted units (input 1, output 5, cache write 1.25, cache read 0.1, × a model
  factor) and calibrates units-per-percent from pairs of exact readings. Model mix, cache
  behaviour and server-side changes all shift that ratio. Calibration also only sees tokens in
  transcripts on this machine. Usage from claude.ai, the desktop app, other machines or another
  `CLAUDE_CONFIG_DIR` counts against the same allowance but is invisible to cctripwire, so estimates
  run *low*. Exact readings already include it.
- **Model-scoped weekly limits** (for example a separate Opus or Fable meter) are tracked only
  when a source reports them. The status line currently reports only `five_hour` and
  `seven_day`. The opt-in OAuth source can report more.
- **The OAuth source is undocumented.** It is off by default and may break or change without
  notice. When that happens it reports "unavailable", never zero.
- **Transcript format is internal** to Claude Code. A format change makes the scanner find no
  usage. It does not corrupt anything, and `cctripwire doctor` shows freshness.
- **A reset is not 0 %.** When `resets_at` passes, the window is shown as unknown until a new
  reading arrives.

## Enforcement

- **Overshoot is bounded, not zero.**
  - Hooks act at tool-call boundaries, so a single long model response cannot be interrupted
    mid-generation by a hook.
  - The gateway's per-invocation token budget is checked every `heartbeat_s` from the
    transcript, so a run can exceed its budget by up to one heartbeat of output.
  - `--max-budget-usd` (added when configured) has the same caveat on Claude Code's side.
  - Set thresholds with margin; the defaults stop at 95 %.
- **The gateway only sees processes that go through it.** A `claude -p` started with an absolute
  path to the real binary, from an environment without the shim first on PATH, or with
  `CCTRIPWIRE_DISABLE=1`, bypasses admission control. What still applies:
  - Hooks still run inside those sessions (unless launched with `--bare`), and at RESTRICT/STOP
    they deny Bash commands that start `claude -p`.
  - The daemon lists bypassing processes (`cctripwire top`, detection `gateway_bypass`) and, if
    `daemon.enforce_bypass = true`, SIGSTOPs them at STOP.
  - Quoted command strings (`bash -c "claude -p …"`) are not matched by the hook pattern. That is
    deliberate: it avoids false positives on `grep "claude -p"`. The shim covers that case.
- **`--bare` sessions skip hooks entirely.** Only the gateway and daemon apply to them.
- **Running subagents (Agent/Task tool) cannot be killed individually.** They run inside the
  parent's process. cctripwire denies new ones, and at STOP halts the session through hooks.
- **SIGSTOP pauses a process, not the API request it is waiting on.** A suspended `claude -p` that
  was mid-request may see that request time out when resumed. Claude Code generally retries, but
  a resumed run can fail. Killed or suspended work gets a checkpoint with a `claude -p --resume
  <session>` command so it can be restarted cleanly.
- **Concurrency counts active work.** An invocation whose child is queued or running is treated
  as waiting on it and does not count against `max_concurrency`. Without this, a nested chain
  deadlocks against its own slots. A parent that launches a child in the background and keeps
  generating is therefore under-counted.
- **Blocking prompts at STOP also affects humans.** With hooks installed in an interactive
  session, typing a prompt at STOP is refused with instructions (`cctripwire override ok --for 30m`
  or `CCTRIPWIRE_DISABLE=1`). Set `hooks.block_prompts_at_stop = false` to allow it.
- **Escape hatches are honour-system.** An agent with shell access could set `CCTRIPWIRE_DISABLE=1`
  or run `cctripwire override`. cctripwire is a budget guard for cooperative agents, not a security
  boundary. Each override is recorded in the intervention log.

## Runaway breaker

- **It counts launches, not tokens.** A single `claude -p` that runs for hours is not a storm.
  Per-invocation budgets and the meter-rise (surge) trigger cover that case.
- **The first few runs of a storm still happen.** Up to `max_concurrency` runs start before the
  queue fills. Those runs are paused while the breaker is tripped, not killed.
- **Surge needs fresh exact readings.** The trigger needs two exact samples of the same window
  inside `surge_window_s`. A `usage_url` endpoint that caches (many poll every 5 minutes) delays
  detection by that much. The status line is fresher, but only while an interactive
  session is open.
- **A surge pauses automation even when an interactive session caused it.** Interactive sessions
  are never paused. If your own heavy session moves the 5-hour meter 5 points in 10 minutes,
  queued and running `claude -p` work is paused until you reset the breaker.
- **The bypass trigger SIGSTOPs processes it did not start.** It applies to `claude -p`
  processes outside the gateway (absolute path, `CCTRIPWIRE_DISABLE=1`). Set `bypass_storm_max`
  above the number of headless runs your tools legitimately keep alive at once.
- **Launches outside the gateway are sampled, not counted exactly.** The daemon looks every
  15 seconds, so `bypass_storm_started` misses runs shorter than that and the real number of
  launches is higher than the one it reports. New launches outside the gateway are not refused
  while the breaker is tripped; they are paused on the next daemon tick.
- **An agent can wait for a reset.** It can also batch work below the queue limit. The launch
  window limit (`storm_max_started`) caps that throughput.

## Platform

- Tested on Linux. Process control (bypass detection, pausing) reads `/proc`. On macOS the
  hooks, the gateway and `setup` (launchd) are expected to work but are untested; process
  listing does not, so the bypass trigger is inactive there. Windows is not supported.
- One SQLite database per user. Several machines are not coordinated.
- Hook cost is one Python process start per hook event: p50 53 ms, p95 58 ms on the test machine
  (`docs/results/benchmarks.md`), of which the cctripwire logic itself is about 0.14 ms. That cost
  is paid on every tool call. The gateway adds about 63 ms per `claude -p`.
- `gateway.spawn_rate_per_min` (default 12) is a hard admission gate. A script that runs many very
  short `claude -p` calls back to back waits up to 60 s for the 13th call. Raise it for legitimate
  high-rate batch work.

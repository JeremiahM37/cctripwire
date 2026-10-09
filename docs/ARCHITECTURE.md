# Architecture

cctripwire is a set of small, short-lived processes that share one SQLite database. There is
no required server: hooks, the status line and the `claude -p` gateway each open the
database, do their work in a few milliseconds and exit. The optional daemon adds periodic
scanning, enforcement on already-running work, and the dashboard.

```
                    ┌──────────────── Claude Code session (interactive or -p) ────────────────┐
                    │  status line ──► cctripwire statusline   (rate_limits: exact 5h / 7d %)   │
                    │  every hook  ──► cctripwire hook         (prompts, tool calls, failures,   │
                    │                                        compactions, subagents)          │
                    │  Bash: claude -p ... ──► shim ──► cctripwire run (gateway) ──► real claude │
                    └───────────────┬──────────────────────────────┬──────────────────────────┘
                                    │                              │
        ~/.claude/projects/*.jsonl  │        ┌─────────────────────▼──────────────┐
        (per-message token usage) ──┼──────► │  tripwire.db  (SQLite, WAL)         │ ◄── cctripwire CLI
        OAuth usage API (opt-in) ───┘        │  quota_samples  token_events        │ ◄── dashboard :8377
                                             │  sessions  events  invocations      │
                                             │  detections  interventions  kv      │
                                             └─────────────────────▲──────────────┘
                                                                   │
                                   cctripwire daemon (optional): scan, decide, suspend/resume,
                                   bypass detection, checkpoints, notifications
```

## Data flow

1. **Measure.** Three sources, ranked by authority:
   - *Status line* `rate_limits.five_hour/seven_day.used_percentage` + `resets_at`. This is the
     documented, provider-reported number (Pro/Max only, after the first response). Stored as
     `exact` samples.
   - *OAuth usage endpoint* (`/api/oauth/usage`, the one `/usage` uses). Undocumented, so it is
     **off by default**; when enabled it is rate-limited, cooled down on 429, and any surprise
     yields "unavailable", never zero.
   - *Transcripts* (`~/.claude/projects/**/*.jsonl`). Per-message `usage` gives exact *token*
     counts, attributed to session / project / agent / gateway invocation. Tokens are not the
     quota, so they are converted to **weighted tokens** (input 1, output 5, cache write 1.25,
     cache read 0.1, × model factor) and used for attribution, velocity, and estimates.
2. **Estimate.** `estimator.calibrate` learns *weighted tokens per 1 %* of each window from
   pairs of exact readings and the tokens burned between them. With a calibration, the latest
   exact reading plus tokens since then gives an estimate that is labelled `~` everywhere. Without
   exact data and without a configured budget the window is **unknown**, and no rule fires on it.
3. **Forecast.** `forecast.window_state` computes burn rate (least squares over the last 30 min of
   the current reset period, falling back to token velocity ÷ calibration), ETA to 100 %
   (only when that is before the reset), elapsed fraction of the window, and pace ratio.
4. **Decide.** `policy.evaluate_policy` runs the configured rules (a tiny, sandboxed expression
   language; comparisons with unknown values are false), applies per-project/agent budgets that
   can only *raise* the level, and honours a manual override. Levels are cumulative:
   `OK < WARN < THROTTLE < RESTRICT < STOP`, and each carries concrete effects
   (concurrency cap, spacing, denied tools, spawn denial, model downgrade, checkpoint).
   `engine.decide` caches the result for 5 s in the DB so a burst of hook calls costs one
   computation.
5. **Detect loops.** `loops.LoopDetector` looks at the session's recent hook events:
   identical prompts, the same failing tool call, retries without an intervening edit,
   long runs without a successful write, compaction storms, subagent bursts, and token spikes;
   the gateway adds repeated identical `claude -p` prompts and recursion depth.
6. **Intervene** at the cheapest effective point:
   - *Hooks* deny a specific tool call with a one-line reason the model reads (denying the 4th
     identical failing `npm test` with the error), inject one line of context when the level
     rises (rate-limited, nothing at OK), block prompts at STOP, and run the **checkpoint
     protocol**: at STOP only Write/Edit are allowed for N calls so the agent can write
     `CHECKPOINT.md`, then `continue:false` halts it.
   - *Gateway* refuses (exit 77) or queues `claude -p` before it spends anything: depth,
     duplicates, spawn rate, concurrency, per-parent fan-out, STOP. Running invocations are
     capped by per-invocation token budgets (exit 78) and can be suspended (SIGSTOP), resumed
     and killed through the DB control channel.
   - *Daemon* suspends running gateway work at STOP after writing a resumable checkpoint
     (`claude -p --resume <session>`), suspends the newest extras when over the concurrency cap,
     resumes oldest-first when the budget recovers, and flags `claude -p` processes that bypass
     the gateway (optionally pausing them at STOP).

## Why these mechanisms

| Concern | Mechanism | Reason |
|---|---|---|
| Authoritative quota | status line `rate_limits` | Documented and provider-computed; the only exact source that needs no private API. |
| Before-the-fact enforcement | PreToolUse deny, gateway admission | Both act before tokens are spent. A Stop-hook fuse (burnstop's approach) is always one turn late. |
| Nested agents | env-var lineage (`CCTRIPWIRE_INVOCATION_ID/ROOT_ID/DEPTH`) + PATH shim | Survives any number of `claude -p` layers without ptrace or root; children inherit it automatically. |
| Cross-session coordination | SQLite `BEGIN IMMEDIATE` | One file, no daemon dependency; atomic slot admission across unrelated processes. |
| Crash safety | pid liveness + process groups | A SIGKILLed gateway leaves a row whose pid is dead; the next admitter reaps it and kills the orphaned group. |
| Cheap feedback | deterministic ≤300-char messages, silent at OK | Explaining waste must not itself waste tokens. |

## Process lineage

The gateway assigns each `claude -p` an invocation id and exports it with the root id and
depth. The child's own hooks see the same variables, so its session row is linked to the
invocation, and any `claude -p` it starts (through the shim on PATH) becomes a child row with
`depth+1`. Parent/child fan-out (`max_children`), total concurrency, and depth are therefore
enforced across the whole tree, even when the tree spans several independent top-level sessions.

## Failure modes by design

- Hooks **fail open**: an exception in cctripwire never breaks a Claude Code session; it is logged to
  `hook-errors.log`. The gateway fails open for *monitoring* (transcript scan errors) but never for
  *limits* (depth, concurrency).
- Unknown quota is never treated as 0 %. A passed `resets_at` makes the window unknown until a new
  reading arrives.
- `CCTRIPWIRE_DISABLE=1` is a per-process emergency bypass; `cctripwire override ok --for 1h` is the global one.

## Module map

| Module | Role |
|---|---|
| `models.py`, `config.py`, `store.py`, `util.py` | shared types, TOML config with defaults, SQLite state, helpers |
| `expr.py`, `policy.py`, `engine.py` | rule language, policy evaluation, cached decision |
| `quota/transcripts.py`, `quota/statusline.py`, `quota/oauth.py`, `quota/estimator.py`, `forecast.py` | measurement, calibration, forecasting |
| `loops.py`, `hooks.py` | loop detection and Claude Code hook handling |
| `gateway.py`, `procs.py` | `claude -p` gateway, shim, process inspection/signals |
| `interventions.py`, `daemon.py` | messages, checkpoints, control requests, background enforcement |
| `cli.py`, `dashboard.py`, `web/index.html` | CLI and local dashboard |

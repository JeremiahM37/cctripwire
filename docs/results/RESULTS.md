# Validation results

## Automated tests

`python -m pytest -q`: **291 passed** in about 40 s. No real `claude` and no network are involved:
`tests/fake_claude.py` stands in for the binary and quota is mocked.

| Requirement | Where it is tested |
|---|---|
| Concurrent process spawning | `test_gateway.py`: 8 parallel launches never exceed the cap; 32-way admission in `bench/` |
| Recursive invocation loops | `test_gateway.py`: recursion stopped at `max_depth` (exit 77); depth-3 chain under cap 1 does not deadlock; `test_e2e.py` |
| Quota exhaustion | `test_e2e.py`: swarm under rising quota → checkpoint + SIGSTOP (verified `/proc` state `T`) → resume; `test_daemon.py`, `test_core.py` levels |
| Missing / inaccurate usage | `test_e2e.py`, `test_forecast.py`: no data → unknown (never 0); stale → not exact; passed reset → unknown; corrupt status-line JSON |
| Crashed agents | `test_gateway.py`, `test_e2e.py`: child crash → `failed`; SIGKILLed gateway → row `crashed`, orphan group killed |
| Restart recovery | `test_e2e.py`: kill -9 a writer mid-transaction, store still opens; `test_core.py`: 8 processes creating a fresh DB at once |
| False-positive loop detection | `test_loops.py`, `test_e2e.py`: edit-test-edit-test is never denied; "continue" prompts ignored; quoted `grep "claude -p"` not matched |
| Graceful checkpointing | `test_hooks.py`, `test_e2e.py`: STOP allows N checkpoint writes then halts; allowance resets after recovery; daemon checkpoint JSON with resume command |
| Process cleanup | `test_e2e.py`: `cctripwire kill` removes the whole group, including grandchildren; every suite asserts no leftover fake processes |

`verify` (project `.verify.yaml`): **PASS: 4/4 steps passed (backend=web)**.
The four steps were:

1. the full suite plus a leak check;
2. the example config validates;
3. the dashboard API reports a seeded STOP state as exact;
4. the dashboard rendered in headless Chromium at phone size and passed a vision check.

## Live checks against the real Claude Code (2.1.295, Haiku, throwaway project)

| Check | Result |
|---|---|
| Hooks installed by `cctripwire install` fire in `claude -p` | session, prompt, Bash pre/post and Stop recorded; tokens attributed to the project |
| RESTRICT + agent tries `claude -p` via Bash | denied before running; the model quoted the reason and did not retry |
| STOP + new prompt | prompt blocked: `num_turns 0`, `total_cost_usd 0` |
| `cctripwire run -- -p … --model haiku` with the real binary | exit 0; `--session-id` injected; child's hook session linked to the invocation; 108 output tokens attributed |
| `cctripwire scan` over this machine's real `~/.claude/projects` | 42,567 token events: 12 s first scan, 0.4 s incremental |
| `cctripwire top` on this machine | 10 live interactive sessions classified, 0 bypassing `claude -p` |

## Performance

Measured by `bench/bench.py`; full report in [benchmarks.md](benchmarks.md).

| Metric | Result |
|---|---|
| Hook as Claude Code runs it (subprocess) | p50 52.6 ms, p95 57.6 ms (cctripwire logic in-process: 0.13 ms) |
| Status line | p50 73 ms |
| Gateway overhead per `claude -p` | p50 62 ms |
| 32 concurrent gateways, cap 4 | max overlap 4, 0 lock errors, all 32 completed |
| Snapshot | 3 ms at 10k token events, 46 ms at 100k, cached 5 s across processes |
| Transcript scan | 172k lines/s first pass; 3 ms no-change rescan |
| Daemon tick | 62 ms with 50 running invocations and 100k token events |

## Runaway breaker drills (2026-10-08, real Claude Code 2.1.295)

The drills ran in `scripts/sandbox.sh` sandboxes. Each sandbox had its own state and
project-only hooks, the shim first on PATH, and `force_model = "haiku"`. The limits were
2 running and 3 waiting.

| Drill | Result |
|---|---|
| 40 parallel `claude -p --model opus` from a shell loop | 2 ran (both on `claude-haiku-5-5`), 38 refused with exit 77; whole storm over in 9 s |
| Incident replay: a Haiku agent asked to write and run a script launching 30 parallel Opus eval cases | 2 cases ran, 28 refused; the agent's Bash tool used the shim, it read the refusal, did not retry or work around it, and told the user to run `cctripwire breaker reset` (4 turns, $0.008 for the agent) |
| `sources.usage_url` against a live usage endpoint | exact 5-hour 4 %, weekly 3 % and a model-scoped weekly meter read in one daemon tick |
| Surge: two exact 5-hour readings 30 % → 36 % | daemon tripped the breaker: "surge: 5-hour usage rose 6 points" |

Cost of all drills was about $0.53 at list price, almost all of it from the four child runs.
Each child created a ~129k-token prompt cache: Claude Code's default context for a
`claude -p` in a fresh directory, even with `--tools ""`. The account's weekly utilisation stayed at 3 % throughout.

Automated: `tests/test_breaker.py` (16 tests). It covers:

- a 120-launch parallel burst against the fake binary: at most 12 started, the rest failed fast;
- the launch-window trip, reset and lift;
- normal use never tripping;
- surge on fast rises, ignored for slow rises and across window resets;
- the daemon pausing runs while tripped and resuming them after reset;
- the bypass storm SIGSTOP;
- hook denial of `claude -p` only while tripped;
- the runaway-only preset never denying repeated work or many subagents, even at 99 %;
- `usage_url` parsing.

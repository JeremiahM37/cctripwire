# How cctripwire compares (surveyed 2026-10-08)

Survey done before design. Stars and push dates are from the GitHub API on 2026-10-08. Feature
details come from each project's README; their source was not executed.

## Existing tools

| Project | Data source | Enforces? | Notes |
|---|---|---|---|
| [SteveHuang27GitHub/claude-usage-monitor](https://github.com/SteveHuang27GitHub/claude-usage-monitor) (Python, 0★, pushed 2026-08-21) | status-line `rate_limits` saved to a state file; a 3-minute poller of the unofficial OAuth usage endpoint | Partly: nudges, and at level 2 a PreToolUse hook denies Agent/Task/Workflow. Never kills. | Linear regression over 20 min of samples for time-to-100 %; auto-checkpoint at 95 % via `git add -A && git commit`; wraps the existing status line. |
| [phuryn/burnstop](https://github.com/phuryn/burnstop) (Python stdlib, 10★, pushed 2026-06-22) | transcripts, recomputed in a Stop hook each turn | **Yes**: `continue:false` halts the run | Token/$ caps including subagents; repeated-tool-call loop heuristic. Acts one turn late; cannot stop a running subagent. |
| [Yuefan/claude-monitor](https://github.com/Yuefan/claude-monitor) (Python/Tk, 0★, pushed 2026-07-21) | transcripts; optional undocumented live quota | No | 5-hour blocks estimated by flooring the first message to the hour; dedupes on `message.id + requestId`. |
| [Maciek-roboblog/Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor) (8.7k★) | transcripts | No | Terminal monitor with predictions. |
| [ccusage/ccusage](https://github.com/ccusage/ccusage) (18.9k★) | transcripts | No | Daily, monthly and session usage reports. |

### What we reused, and what we did differently

- **Reused:** the status line as the exact source (SteveHuang27), transcript dedupe on
  `message.id:requestId` (Yuefan, ccusage), halting with `continue:false` (burnstop), and
  denying spawn tools in PreToolUse (SteveHuang27).
- **Different:**
  - Enforcement happens *before* spending: PreToolUse denial and gateway admission, rather than a
    Stop-hook fuse.
  - Process-level control of `claude -p` trees: lineage, depth, fan-out, cross-session
    concurrency, SIGSTOP/SIGCONT.
  - Loop detection beyond identical tool calls, with false-positive guards (edits reset counters).
  - Calibrated estimates kept separate from exact values.
  - The OAuth endpoint is opt-in only.
  - Checkpoints are scoped to a file the agent writes, never `git add -A`.

None of the surveyed tools govern nested `claude -p` processes or coordinate concurrency across
independent sessions. That gap is what the gateway fills.

## Claude Code integration points (docs fetched 2026-10-08; local CLI 2.1.295)

- **Hooks** used: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure,
  PreCompact, SubagentStart, SubagentStop, Stop, SessionEnd.
  - PreToolUse can deny via `hookSpecificOutput.permissionDecision: "deny"` with a reason the
    model reads.
  - `{"continue": false, "stopReason"}` halts the session.
  - UserPromptSubmit can block with `decision: "block"`.
  - Hooks run under `claude -p` unless `--bare`.
- **Status line** stdin carries `rate_limits.five_hour` / `.seven_day` `{used_percentage,
  resets_at}`. They are present only for Pro/Max subscribers after the first API response, each
  window may be independently absent, and a window is dropped once its reset passes.
- **`claude -p`** supports `--max-turns`, `--max-budget-usd` (client-side estimate, may
  overshoot; v2.1.217+), `--session-id <uuid>`, and `--output-format json|stream-json`. The JSON
  result carries `usage`, `total_cost_usd` and `num_turns`, and we observed all three on 2.1.295.
- **Transcripts**: `~/.claude/projects/<encoded cwd>/<session>.jsonl`. The format is internal.
  Verified locally: assistant lines carry `message.usage` with `input_tokens`, `output_tokens`,
  `cache_creation_input_tokens` and `cache_read_input_tokens`, and one API response spans
  several lines with the same `message.id` + `requestId`.
- **OAuth usage endpoint** (`api.anthropic.com/api/oauth/usage`, used by `/usage`) is
  undocumented. cctripwire reads it only when `sources.oauth = true`, never refreshes credentials,
  backs off on 429, and treats any unexpected response as unavailable.

## Verified live (2026-10-08, Claude Code 2.1.295, Haiku)

These were run against the real binary in a throwaway project:

- Hooks registered the session, prompt, Bash pre/post and Stop events, and attributed tokens to
  the project.
- RESTRICT denied a Bash `claude -p …` call before it ran. The model reported the reason and did
  not work around it.
- STOP blocked the prompt itself: 0 turns, $0.
- The gateway in front of the real binary injected `--session-id`, linked the child's hook
  session to the invocation, and attributed its tokens.

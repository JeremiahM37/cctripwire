# Budget mode

cctripwire's default `setup` installs the **runaway-only** preset, which never limits normal work.
Budget mode is the full policy engine: quota levels, per-project budgets and loop enforcement.
Use it for unattended agents you want held inside a share of your allowance.

```bash
cctripwire config init --force  # replaces the runaway-only preset with the full commented defaults
cctripwire limits               # shows the effective rules, levels and gateway limits
```

## What happens at each level

| Level | Default trigger | Effect |
|---|---|---|
| WARN | 5h ≥ 50 % in the first 40 % of the window; weekly pace ≥ 1.5× | one line of context to the agent (rate-limited) |
| THROTTLE | predicted exhaustion < 30 min; 5h ≥ 85 % | concurrency 1, 30 s spacing between new `claude -p`, one subagent at a time |
| RESTRICT | weekly ≥ 85 % | denies Agent/Task/Workflow/WebSearch and nested `claude -p`; new gateway work forced to `haiku` |
| STOP | 5h or weekly ≥ 95 % | prompts blocked; tools denied except up to 3 Write/Edit calls to write `CHECKPOINT.md`, then the session halts; gateway work checkpointed + suspended; new work refused |

Per-project / per-agent budgets (`[budgets.projects.<name>]`) can only raise the level for
that scope. All rules are editable; see [examples/config.toml](../examples/config.toml).

Escape hatches: `CCTRIPWIRE_DISABLE=1` (one process), `cctripwire override ok --for 1h` (global,
expires), `cctripwire resume` (clear an override).

## Loop detection

| Detector | Fires on | Default action |
|---|---|---|
| identical prompt | same normalized prompt 3× in 15 min (short prompts like "continue" ignored) | prompt blocked with advice |
| repeated tool failure | same tool + input failing 4× with no successful edit in between | that call denied, error quoted |
| excessive retries | same call 6× with no edit in between | denied |
| no progress | 40 tool calls without a successful write | advice |
| repeated `claude -p` | identical prompt run 3× within 10 min | 4th refused: "reuse the earlier result" |
| recursive spawning | depth > `max_depth` | refused |
| parallel spawning | > 5 subagent spawns in 60 s | denied |
| context rebuild | 3 compactions in 15 min | advice (compaction is never blocked) |
| token spike | tokens/min > 4× trailing median | advice |

Every finding is in `cctripwire loops`, every action in `cctripwire interventions`, and both are on
the dashboard.


## Manual installation

`cctripwire setup` does all of this for you. These are the individual steps.

### 1. Hooks and status line (monitoring + in-session enforcement)

```bash
# one project:
cctripwire install --settings ./.claude/settings.json --statusline
# every session of this user (you choose to touch the global file):
cctripwire install --settings ~/.claude/settings.json --statusline
```

`install` backs up the file first (`*.cctripwire-bak-<ts>`), is idempotent, and keeps any other
hooks. If you already had a status line, it prints the command so you can keep it with
`sources.statusline_chain`. `cctripwire uninstall --settings …` removes only cctripwire's entries.

The status line is what supplies the **exact** 5-hour / weekly percentages, and Claude Code
only sends them to the status line (Pro/Max plans, after the first response). Install it in at
least one regularly used session.

### 2. Gateway for `claude -p` (process-level enforcement)

```bash
cctripwire install-shim                     # writes ~/.local/share/cctripwire/shim/claude
export PATH="$HOME/.local/share/cctripwire/shim:$PATH"   # in the agent's environment
```

With the shim first on PATH, every `claude -p …`, including ones an agent runs from its Bash
tool, at any depth, goes through `cctripwire run`. Interactive `claude` invocations are passed
straight to the real binary, and hooks govern those. You can also call the gateway explicitly:

```bash
cctripwire run --project myrepo --agent nightly --budget-tokens 400000 -- -p "fix the flaky test" --model sonnet
```

Exit codes: `77` denied (depth, duplicate, STOP), `75` no slot within `queue_timeout_s`,
`78` killed for exceeding its per-invocation budget; otherwise the child's own code. With
`--output-format json`, a refusal also prints a JSON result object with `is_error: true`, so a
parent agent parsing the output sees the reason.

### 3. Daemon and dashboard (optional, recommended for unattended agents)

```bash
cp examples/systemd/cctripwire.service ~/.config/systemd/user/
systemctl --user enable --now cctripwire          # = cctripwire daemon --dashboard
xdg-open http://127.0.0.1:8377/
```

Every 15 s the daemon scans transcripts, re-evaluates policy, writes checkpoints and suspends
gateway work at STOP, suspends the newest extras when over the concurrency cap, resumes them
oldest-first when the budget recovers, and reports `claude -p` processes that bypass the gateway.


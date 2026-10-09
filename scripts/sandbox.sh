#!/usr/bin/env bash
# Try cctripwire in an isolated chat without touching ~/.claude/settings.json or your PATH.
#
#   scripts/sandbox.sh [DIR]            set up DIR (default: a new temp dir) and open `claude` there
#   scripts/sandbox.sh --setup DIR      only set it up; prints how to use it
#
# Inside DIR:
#   .claude/settings.json   cctripwire hooks for this project only
#   .cctripwire/config.toml    runaway-only preset with drill-sized limits and force_model = haiku,
#                           so every claude -p the agent launches runs on Haiku, whatever it asks for
#   .cctripwire/bin/claude     shim: claude -p goes through the gateway; interactive claude passes through
#   .cctripwire/state/         the sandbox's own database (your real state is untouched)
#   ./cctripwire               cctripwire CLI bound to the sandbox (./cctripwire status, ./cctripwire breaker reset)
#
# Then ask the chat for something like:
#   "Write and run a script that evaluates 200 prompts in parallel, one `claude -p --model opus` each."
# Expected: a handful run (on Haiku), the breaker trips, the rest fail instantly at no cost,
# and the agent is told to stop and ask you. Watch it with `./cctripwire status` or `./cctripwire top`.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
py="$repo/.venv/bin/python"
[ -x "$py" ] || { echo "no venv at $repo/.venv (python3 -m venv .venv && .venv/bin/pip install -e .)" >&2; exit 1; }

setup_only=0
if [ "${1:-}" = "--setup" ]; then setup_only=1; shift; fi
dir=${1:-$(mktemp -d "${TMPDIR:-/tmp}/cctripwire-sandbox.XXXXXX")}
mkdir -p "$dir"
dir=$(cd "$dir" && pwd)

real=${CCTRIPWIRE_REAL_CLAUDE:-$(command -v claude || true)}
if [ -z "$real" ] || grep -q cctripwire-shim "$real" 2>/dev/null; then
  echo "cannot find the real claude binary; set CCTRIPWIRE_REAL_CLAUDE" >&2; exit 1
fi

g="$dir/.cctripwire"
mkdir -p "$g/bin" "$g/state" "$dir/.claude"
env_lines="CCTRIPWIRE_HOME='$g/state' CCTRIPWIRE_CONFIG='$g/config.toml' CCTRIPWIRE_SHIM_DIR='$g/bin' CCTRIPWIRE_REAL_CLAUDE='$real' PYTHONPATH='$repo/src'"

cat > "$g/config.toml" <<'EOF'
# Sandbox: the runaway-only preset with small limits so a drill costs almost nothing.
rules = []

[hooks]
block_prompts_at_stop = false
enforce_loops = false

[gateway]
force_model = "haiku"       # every claude -p runs on Haiku, whatever the agent asks for
per_invocation_max_turns = 3
max_concurrency = 2
max_depth = 2
max_children = 4
spawn_rate_per_min = 60
max_duplicates = 0
queue_timeout_s = 300
storm_max_queued = 3        # 2 running + 3 waiting: a burst of 6 trips it
storm_max_started = 6
storm_window_s = 600
storm_cooloff_s = 0
surge_five_hour_pct = 5
surge_seven_day_pct = 3

[daemon]
interval_s = 5
bypass_storm_max = 6
EOF

cat > "$g/bin/claude" <<EOF
#!/bin/sh
# cctripwire-shim (sandbox): routes claude -p through the cctripwire gateway
export $env_lines
exec '$py' -m cctripwire run --shim -- "\$@"
EOF
chmod +x "$g/bin/claude"

cat > "$dir/cctripwire" <<EOF
#!/bin/sh
export $env_lines
exec '$py' -m cctripwire "\$@"
EOF
chmod +x "$dir/cctripwire"

hook="$env_lines '$py' -m cctripwire hook"
"$py" - "$dir/.claude/settings.json" "$hook" <<'EOF'
import json, sys
path, cmd = sys.argv[1], sys.argv[2]
events = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
          "PreCompact", "SubagentStart", "SubagentStop", "Stop", "SessionEnd"]
matcher = {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
hooks = {}
for e in events:
    g = {"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}
    if e in matcher:
        g = {"matcher": "*", **g}
    hooks[e] = [g]
json.dump({"hooks": hooks}, open(path, "w"), indent=2)
EOF

cat <<EOF
sandbox ready: $dir
  ./cctripwire status | top | breaker | breaker reset     (run inside $dir)
  daemon (pauses running runs on a trip): $dir/cctripwire daemon
  remove when done: rm -rf '$dir'
EOF
[ "$setup_only" = 1 ] && exit 0
cd "$dir"
export PATH="$g/bin:$PATH"
eval "export $env_lines"
exec "$real" --model haiku

#!/usr/bin/env bash
# Run an unattended agent with cctripwire in front of every claude -p it (or its children) starts.
#
#   examples/agent-fleet.sh myrepo "Fix the failing tests and open a summary in NOTES.md"
#
# Prerequisites (once): `cctripwire install-shim`, `cctripwire install --settings <repo>/.claude/settings.json`,
# and ideally the daemon (`systemctl --user enable --now cctripwire`).
set -euo pipefail
repo=${1:?repo dir}; task=${2:?task}

# 1. The shim first on PATH: nested `claude -p` calls from the agent's Bash tool are gated too.
export PATH="$HOME/.local/share/cctripwire/shim:$PATH"
# 2. Label the work so budgets and the dashboard can attribute it.
export CCTRIPWIRE_AGENT="fleet-$(basename "$repo")"

cd "$repo"
# 3. Refuse to start at all if the allowance is already critical.
level=$(cctripwire status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["level"])')
if [ "$level" = "stop" ]; then
  echo "cctripwire level is STOP; not starting. Resume later with: cctripwire checkpoints" >&2
  exit 75
fi

# 4. The top-level run itself goes through the gateway with its own budget.
#    Exit 77 = refused, 75 = no slot in time, 78 = per-invocation budget exceeded.
set +e
cctripwire run --agent "$CCTRIPWIRE_AGENT" --budget-tokens 3000000 -- \
  -p "$task" --model sonnet --max-turns 80 --output-format json > .cctripwire-last-result.json
rc=$?
set -e
case $rc in
  0)  echo "done";;
  77|75) echo "cctripwire refused/queued-out: $(python3 -c 'import json;print(json.load(open(".cctripwire-last-result.json"))["result"])')" >&2;;
  78) echo "stopped at its token budget; see CHECKPOINT.md and 'cctripwire checkpoints'" >&2;;
  *)  echo "agent exited $rc" >&2;;
esac
exit $rc

#!/usr/bin/env bash
# Seed a demo state (5-hour window at 96% = STOP) and serve the dashboard in the
# foreground on :8398. Used as `launch.command` by .verify.yaml.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
export CCTRIPWIRE_HOME=${TMPDIR:-/tmp}/cctripwire-verify-state CCTRIPWIRE_CONFIG=/nonexistent
rm -rf "$CCTRIPWIRE_HOME"
G="$here/.venv/bin/cctripwire"
"$G" mock-quota five_hour 96 --resets-in 40m >/dev/null
"$G" mock-quota seven_day 61 --resets-in 76h >/dev/null
exec "$G" dashboard --port 8398

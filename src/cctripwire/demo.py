"""`cctripwire demo`: replay a runaway storm against a stand-in `claude`, spending no tokens.

Everything happens in a temporary directory with its own config, state and shim, so the user's
real setup is never touched. The stand-in sleeps for a few seconds like a real run would.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import gateway

FAKE = """#!/bin/sh
# stand-in for claude: pretend to work, spend nothing
sleep "${CCTRIPWIRE_DEMO_SLEEP:-3}"
echo "done"
"""

CONFIG = """rules = []
[hooks]
block_prompts_at_stop = false
enforce_loops = false
[gateway]
max_concurrency = 10
max_depth = 3
max_children = 10
spawn_rate_per_min = 100000
max_duplicates = 0
queue_timeout_s = 120
storm_max_queued = 20
storm_max_started = 40
storm_window_s = 600
storm_cooloff_s = 0
"""


def run(n: int = 200, sleep_s: float = 3.0, out=print) -> dict:
    with tempfile.TemporaryDirectory(prefix="cctripwire-demo-") as tmp:
        t = Path(tmp)
        fake = t / "real-claude"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        (t / "config.toml").write_text(CONFIG)
        shim = gateway.install_shim(t / "shim", python=sys.executable)
        env = dict(os.environ, CCTRIPWIRE_HOME=str(t / "state"), CCTRIPWIRE_CONFIG=str(t / "config.toml"),
                   CCTRIPWIRE_SHIM_DIR=str(t / "shim"), CCTRIPWIRE_REAL_CLAUDE=str(fake),
                   CCTRIPWIRE_DEMO_SLEEP=str(sleep_s))
        env.pop("CCTRIPWIRE_DISABLE", None)
        out(f"$ for i in $(seq {n}); do claude -p \"eval case $i\" --model opus & done; wait")
        t0 = time.monotonic()
        procs = [subprocess.Popen([str(shim), "-p", f"eval case {i}", "--model", "opus"], env=env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                 for i in range(n)]
        results = [(p.wait(), p.stderr.read().decode(errors="replace")) for p in procs]
        for p in procs:
            p.stderr.close()
        elapsed = time.monotonic() - t0
        ran = sum(1 for rc, _ in results if rc == 0)
        refused = sum(1 for rc, _ in results if rc == gateway.EXIT_DENIED)
        other = n - ran - refused
        msg = next((e.strip().splitlines()[0] for rc, e in results if rc == gateway.EXIT_DENIED and e.strip()), "")
        out("")
        out(f"  launched  {n}")
        out(f"  ran       {ran:<6}(the first wave, before the storm was recognised)")
        out(f"  refused   {refused:<6}(exit {gateway.EXIT_DENIED}, before claude started: 0 tokens)")
        if other:
            out(f"  other     {other}")
        out(f"  elapsed   {elapsed:.1f}s")
        if msg:
            out("")
            out(f"  {msg}")
        out("")
        out("Nothing here touched your real Claude Code setup. On a real machine the daemon also pauses")
        out("the runs that did start, and agents are told to stop and ask you.")
        return {"launched": n, "ran": ran, "refused": refused, "other": other, "elapsed_s": elapsed}

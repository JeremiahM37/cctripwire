#!/usr/bin/env python3
"""Fake `claude` binary for cctripwire gateway tests. Behaviour is driven by FAKE_* env vars.

  FAKE_SLEEP=<s>          total seconds to run (default 0)
  FAKE_TURNS=<n>          assistant turns to simulate (default 1)
  FAKE_TOKENS=<n>         output tokens per turn (default 0)
  FAKE_INPUT=<n>          input tokens per turn (default 0)
  FAKE_EXIT=<rc>          exit code (default 0)
  FAKE_SPAWN=<n>          run `claude -p child-i` n times (sequential; FAKE_PARALLEL=1 for parallel)
  FAKE_SPAWN_ONLY_TOP=1   with FAKE_SPAWN, spawn only at CCTRIPWIRE_DEPTH 1 (not in children)
  FAKE_SPAWN_MAX_DEPTH=n  with FAKE_SPAWN, spawn only while CCTRIPWIRE_DEPTH < n
  FAKE_RECURSE=1          run `claude -p recurse` and exit with its code (forever, until denied)
  FAKE_CRASH=1            os._exit(1) halfway through the turns
  FAKE_TRANSCRIPT_DIR=<d> write Claude Code style JSONL to <d>/projects/<cwd>/<session>.jsonl
  FAKE_LOG=<file>         append a JSON line per start/end event (for overlap checks)

Spawned children use `claude` from PATH, so tests put the cctripwire shim first.
"""
import datetime
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

VALUE_OPTS = {
    "--output-format", "--input-format", "--model", "--session-id", "--max-turns",
    "--max-budget-usd", "--resume", "-r", "--system-prompt", "--append-system-prompt",
    "--permission-mode", "--allowedTools", "--disallowedTools", "--add-dir", "--mcp-config",
    "--settings",
}


def opt(args, name):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def positional(args):
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in VALUE_OPTS:
            skip = True
            continue
        if a.startswith("-"):
            continue
        return a
    return None


def log(event, **kw):
    path = os.environ.get("FAKE_LOG")
    if not path:
        return
    rec = {"event": event, "t": time.time(), "pid": os.getpid(), "argv": sys.argv[1:], **kw}
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def write_turn(tdir, sid, cwd, model, t, out_tok, in_tok):
    path = Path(tdir) / "projects" / cwd.replace("/", "-") / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "type": "assistant",
        "sessionId": sid,
        "cwd": cwd,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "requestId": f"req_{uuid.uuid4().hex[:8]}",
        "message": {
            "id": f"msg_{uuid.uuid4().hex[:12]}",
            "model": model,
            "role": "assistant",
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        },
    }
    with open(path, "a") as f:
        f.write(json.dumps(line) + "\n")


def spawn(prompts, parallel):
    rcs = []
    procs = []
    for p in prompts:
        cmd = ["claude", "-p", p]
        if parallel:
            procs.append(subprocess.Popen(cmd))
        else:
            rcs.append(subprocess.call(cmd))
    for p in procs:
        rcs.append(p.wait())
    return rcs


def main():
    args = sys.argv[1:]
    if "--version" in args:
        print("2.0.0 (fake claude)")
        return 0
    if not ("-p" in args or "--print" in args):
        print("fake claude: interactive mode is not supported")
        return 0
    prompt = positional(args)
    if prompt is None:
        prompt = sys.stdin.read()
    fmt = opt(args, "--output-format") or "text"
    sid = opt(args, "--session-id") or opt(args, "--resume") or opt(args, "-r") or str(uuid.uuid4())
    model = opt(args, "--model") or "sonnet"
    cwd = os.getcwd()
    turns = max(1, int(os.environ.get("FAKE_TURNS", "1")))
    out_tok = int(os.environ.get("FAKE_TOKENS", "0"))
    in_tok = int(os.environ.get("FAKE_INPUT", "0"))
    sleep_s = float(os.environ.get("FAKE_SLEEP", "0"))
    tdir = os.environ.get("FAKE_TRANSCRIPT_DIR")
    log("start", prompt=prompt, session=sid, model=model)

    if os.environ.get("FAKE_RECURSE"):
        rc = subprocess.call(["claude", "-p", "recurse"])
        log("end", prompt=prompt, rc=rc)
        return rc

    spawned = []
    top_level = os.environ.get("CCTRIPWIRE_DEPTH", "1") in ("", "1")
    depth_ok = int(os.environ.get("CCTRIPWIRE_DEPTH") or 1) < int(os.environ.get("FAKE_SPAWN_MAX_DEPTH") or 1_000)
    if os.environ.get("FAKE_SPAWN") and depth_ok and (top_level or not os.environ.get("FAKE_SPAWN_ONLY_TOP")):
        spawned = spawn([f"child-{i} of {prompt}" for i in range(int(os.environ["FAKE_SPAWN"]))],
                        parallel=bool(os.environ.get("FAKE_PARALLEL")))

    for t in range(turns):
        if tdir:
            write_turn(tdir, sid, cwd, model, t, out_tok, in_tok)
        if os.environ.get("FAKE_CRASH") and t == turns // 2:
            os._exit(1)
        time.sleep(sleep_s / turns)

    if fmt == "json":
        print(json.dumps({
            "type": "result", "subtype": "success", "is_error": False, "result": f"fake: {prompt[:40]}",
            "session_id": sid, "total_cost_usd": 0.001,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
        }))
    elif fmt == "stream-json":
        print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "model": model}))
        for t in range(turns):
            print(json.dumps({"type": "assistant", "session_id": sid,
                              "message": {"id": f"m{t}", "model": model, "role": "assistant",
                                          "content": [{"type": "text", "text": "fake"}],
                                          "usage": {"input_tokens": in_tok, "output_tokens": out_tok,
                                                    "cache_creation_input_tokens": 0,
                                                    "cache_read_input_tokens": 0}}}))
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "session_id": sid, "total_cost_usd": 0.001}))
    else:
        print(f"fake reply to: {prompt[:40]}")
    sys.stdout.flush()
    rc = int(os.environ.get("FAKE_EXIT", "0") or 0)
    if spawned and rc == 0:
        rc = next((r for r in spawned if r), 0)
    log("end", prompt=prompt, rc=rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())

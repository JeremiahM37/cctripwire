#!/usr/bin/env python3
"""cctripwire benchmark harness.

Run from the repo root:

    .venv/bin/python bench/bench.py --out docs/results/benchmarks.md

Everything runs under a temporary CCTRIPWIRE_HOME / CLAUDE_CONFIG_DIR, and the only
"claude" ever executed is tests/fake_claude.py (through a tiny sh wrapper). No
network access is used. Numbers are written exactly as measured; percentiles are
nearest-rank over the raw samples.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import platform
import re
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fake_claude.py"
PY = sys.executable
MODEL = "claude-sonnet-5-5"
DEVNULL = subprocess.DEVNULL


# ----------------------------------------------------------------- helpers

def pct(samples: list[float], p: float) -> float:
    """Nearest-rank percentile."""
    s = sorted(samples)
    k = max(0, math.ceil(p / 100 * len(s)) - 1)
    return s[k]


def summ(samples: list[float]) -> dict:
    return {"n": len(samples), "p50": pct(samples, 50), "p95": pct(samples, 95),
            "max": max(samples), "min": min(samples), "mean": statistics.fmean(samples)}


def fmt(x: float, digits: int = 2) -> str:
    return f"{x:.{digits}f}"


def machine_info() -> dict:
    cpu = "unknown"
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    mem = "unknown"
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                mem = f"{int(line.split()[1]) / 1048576:.1f} GiB"
                break
    except OSError:
        pass
    commit = "unknown"
    try:
        commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=10).stdout.strip() or commit
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "date": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "cpu": cpu,
        "cores": os.cpu_count(),
        "memory": mem,
        "python": f"{platform.python_implementation()} {platform.python_version()}",
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "cctripwire_commit": commit,
        "python_exe": PY,
    }


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Bench:
    def __init__(self, workdir: Path):
        self.root = workdir
        self.root.mkdir(parents=True, exist_ok=True)
        self.procs: list[subprocess.Popen] = []
        self.wrapper = self.root / "bin" / "claude"
        self.wrapper.parent.mkdir(parents=True, exist_ok=True)
        # The gateway execs CCTRIPWIRE_REAL_CLAUDE; point it at the fake, never at a real claude.
        self.wrapper.write_text(f'#!/bin/sh\nexec "{PY}" "{FAKE}" "$@"\n')
        self.wrapper.chmod(0o755)
        self.proj = self.root / "work" / "bench-project"
        self.proj.mkdir(parents=True, exist_ok=True)
        self.base_env = {k: v for k, v in os.environ.items()
                         if not k.startswith(("CCTRIPWIRE_", "FAKE_", "CLAUDE_"))}

    def section(self, name: str) -> dict:
        """Per-section isolated config, state dir and claude dir."""
        d = self.root / name
        state, claude = d / "state", d / "claude"
        state.mkdir(parents=True, exist_ok=True)
        claude.mkdir(parents=True, exist_ok=True)
        return {"dir": d, "state": state, "claude": claude}

    def config(self, name: str, gateway_extra: dict | None = None) -> Path:
        s = self.section(name)
        lines = ["[general]", f'state_dir = "{s["state"]}"', f'claude_dir = "{s["claude"]}"', "",
                 "[gateway]", "max_concurrency = 4"]
        for k, v in (gateway_extra or {}).items():
            lines.append(f"{k} = {v}")
        p = s["dir"] / "config.toml"
        p.write_text("\n".join(lines) + "\n")
        return p

    def env(self, name: str, cfg: Path, **extra: str) -> dict:
        s = self.section(name)
        e = dict(self.base_env)
        e.update({
            "CCTRIPWIRE_HOME": str(s["state"]),
            "CCTRIPWIRE_CONFIG": str(cfg),
            "CCTRIPWIRE_REAL_CLAUDE": str(self.wrapper),
            "CCTRIPWIRE_SHIM_DIR": str(s["dir"] / "shim"),
            "CLAUDE_CONFIG_DIR": str(s["claude"]),
            "FAKE_SLEEP": "0",
        })
        e.update(extra)
        return e

    def cleanup(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    try:
                        p.kill()
                    except OSError:
                        pass
                p.wait()


def cctripwire_cmd(*args: str) -> list[str]:
    return [PY, "-m", "cctripwire", *args]


# ----------------------------------------------------------------- data fill

def fill_events(store, n: int, now: float, seed: int = 1) -> None:
    import random
    rnd = random.Random(seed)
    tools = ["Bash", "Read", "Edit", "Grep", "Write"]
    rows = []
    for _ in range(n):
        rows.append((now - rnd.uniform(0, 1800), f"bench-sess-{rnd.randrange(20):02d}", None,
                     "PreToolUse", rnd.choice(tools), None, 1, None))
    with store.tx() as c:
        c.executemany("INSERT INTO events(ts,session_id,agent_id,kind,tool,sig,ok,detail) "
                      "VALUES (?,?,?,?,?,?,?,?)", rows)


def fill_tokens(store, n: int, now: float, start: int, seed: int = 2, span: float = 5 * 3600) -> None:
    import random
    rnd = random.Random(seed + start)
    rows = []
    for k in range(n):
        i, o = rnd.randint(50, 4000), rnd.randint(20, 1500)
        cc, cr = rnd.randint(0, 3000), rnd.randint(0, 40000)
        rows.append((now - rnd.uniform(0, span), f"bench-sess-{rnd.randrange(20):02d}",
                     f"proj-{rnd.randrange(8):02d}", f"agent-{rnd.randrange(4)}", MODEL,
                     i, o, cc, cr, i + 5 * o + 1.25 * cc + 0.1 * cr, f"bench-msg-{start + k}", None))
    with store.tx() as c:
        c.executemany(
            "INSERT INTO token_events(ts,session_id,project,agent,model,input,output,cache_create,"
            "cache_read,weighted,msg_key,invocation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def pre_tool_payload(cwd: str) -> str:
    return json.dumps({"session_id": "bench-hook-s1", "transcript_path": None, "cwd": cwd,
                       "hook_event_name": "PreToolUse", "tool_name": "Bash",
                       "tool_input": {"command": "ls -la src"}})


def statusline_payload(cwd: str, i: int, now: float) -> str:
    return json.dumps({"session_id": "bench-hook-s1", "cwd": cwd, "transcript_path": None,
                       "rate_limits": {"five_hour": {"used_percentage": 40.0 + (i % 5), "resets_at": now + 3600},
                                       "seven_day": {"used_percentage": 20.0, "resets_at": now + 4 * 86400}}})


def count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


# ----------------------------------------------------------------- sections

def sec_hooks(b: Bench) -> dict:
    log("[1+2] hooks / statusline")
    cfg_path = b.config("hooks")
    env = b.env("hooks", cfg_path)
    os.environ.update(env)  # in-process reads the same config/state as the subprocesses
    from cctripwire import config as cfgmod, hooks
    from cctripwire.store import Store
    cfg = cfgmod.load(cfg_path)
    store = Store(cfg.db_path)
    now = time.time()
    t0 = time.perf_counter()
    fill_events(store, 10_000, now)
    fill_tokens(store, 50_000, now, start=0)
    fill_s = time.perf_counter() - t0
    err_log = cfg.state_dir / "hook-errors.log"
    err_before = count_lines(err_log)
    payload = pre_tool_payload(str(b.proj))

    sub_ms, bad = [], 0
    for _ in range(100):
        t = time.perf_counter()
        r = subprocess.run(cctripwire_cmd("hook"), input=payload, env=env, capture_output=True, text=True,
                           timeout=120)
        sub_ms.append((time.perf_counter() - t) * 1000)
        if r.returncode != 0:
            bad += 1
    hook_errs = count_lines(err_log) - err_before

    inproc_ms = []
    for _ in range(100):
        t = time.perf_counter()
        hooks.run(payload, cfg=cfg, store=store)
        inproc_ms.append((time.perf_counter() - t) * 1000)

    sl_ms, sl_bad, sl_out = [], 0, ""
    for i in range(50):
        t = time.perf_counter()
        r = subprocess.run(cctripwire_cmd("statusline"), input=statusline_payload(str(b.proj), i, time.time()),
                           env=env, capture_output=True, text=True, timeout=120)
        sl_ms.append((time.perf_counter() - t) * 1000)
        if r.returncode != 0:
            sl_bad += 1
        sl_out = r.stdout.strip()
    store.close()
    return {"fill_s": fill_s, "hook_sub": summ(sub_ms), "hook_nonzero_rc": bad,
            "hook_errors_logged": hook_errs, "hook_inproc": summ(inproc_ms),
            "statusline": summ(sl_ms), "statusline_nonzero_rc": sl_bad, "statusline_last_output": sl_out}


def sec_gateway_overhead(b: Bench) -> dict:
    log("[3] gateway overhead")
    # The shipped spawn_rate_per_min=12 holds the 13th invocation inside any 60 s window for
    # ~60 s. A sequential loop of 30 runs measures that limiter, so it is raised here.
    cfg_path = b.config("gateway", gateway_extra={"spawn_rate_per_min": 100000})
    env = b.env("gateway", cfg_path, FAKE_SLEEP="0")
    gw_ms, fk_ms, diff_ms, rcs = [], [], [], []
    for i in range(30):
        def gw():
            t = time.perf_counter()
            r = subprocess.run(cctripwire_cmd("run", "--", "-p", f"gw overhead {i}"), env=env, cwd=b.proj,
                               stdin=DEVNULL, capture_output=True, text=True, timeout=120)
            return (time.perf_counter() - t) * 1000, r.returncode

        def fk():
            t = time.perf_counter()
            r = subprocess.run([str(b.wrapper), "-p", f"fake overhead {i}"], env=env, cwd=b.proj,
                               stdin=DEVNULL, capture_output=True, text=True, timeout=120)
            return (time.perf_counter() - t) * 1000, r.returncode

        # Alternate which runs first so neither side systematically gets the warm cache.
        if i % 2 == 0:
            g, f = gw(), fk()
        else:
            f, g = fk(), gw()
        gw_ms.append(g[0]); fk_ms.append(f[0]); diff_ms.append(g[0] - f[0]); rcs.append((g[1], f[1]))
    gw_err = count_lines(Path(b.section("gateway")["state"]) / "gateway-errors.log")
    return {"gateway": summ(gw_ms), "fake": summ(fk_ms), "paired_diff": summ(diff_ms),
            "rc_pairs_nonzero": sum(1 for g, f in rcs if g != 0 or f != 0), "gateway_errors_lines": gw_err,
            "invocations": _inv_summary(Path(b.section("gateway")["state"]))}


def _inv_summary(state: Path) -> list[str]:
    """Recorded invocation outcomes (status, reason, count) straight from tripwire.db."""
    db = state / "tripwire.db"
    if not db.exists():
        return []
    c = sqlite3.connect(db)
    try:
        rows = c.execute("SELECT status, COALESCE(reason, ''), COUNT(*) FROM invocations "
                         "GROUP BY status, reason ORDER BY 3 DESC").fetchall()
    finally:
        c.close()
    return [f"{st} x{n}" + (f": {rsn[:160]}" if rsn else "") for st, rsn, n in rows]


def _overlap(log_path: Path) -> tuple[int, int, int]:
    ev = []
    starts = ends = 0
    for line in log_path.read_text().splitlines():
        r = json.loads(line)
        if r["event"] == "start":
            ev.append((r["t"], 1)); starts += 1
        elif r["event"] == "end":
            ev.append((r["t"], -1)); ends += 1
    ev.sort()                       # at equal timestamps, ends are counted before starts
    cur = mx = 0
    for _, d in ev:
        cur += d
        mx = max(mx, cur)
    return mx, starts, ends


def _busy(state: Path) -> tuple[int, int, list[str]]:
    p = state / "gateway-errors.log"
    if not p.exists():
        return 0, 0, []
    lines = p.read_text(errors="replace").splitlines()
    busy = [l for l in lines if re.search(r"locked|busy", l, re.I)]
    uniq: list[str] = []
    for l in lines:
        if len(uniq) < 3 and l not in uniq:
            uniq.append(l[:200])
    return len(busy), len(lines), uniq


def sec_admission(b: Bench) -> dict:
    log("[4] admission under 32 concurrent gateways")
    out = {}
    variants = {"defaults (spawn_rate_per_min=12, queue_poll_s=2.0)": {},
                "spawn_rate_per_min=100000, queue_poll_s=2.0": {"spawn_rate_per_min": "100000"},
                "spawn_rate_per_min=100000, queue_poll_s=0.1": {"spawn_rate_per_min": "100000",
                                                               "queue_poll_s": "0.1"}}
    for idx, (label, extra) in enumerate(variants.items()):
        name = f"admission{idx}"
        cfg_path = b.config(name, gateway_extra=extra)
        flog = b.section(name)["dir"] / "fake.log"
        env = b.env(name, cfg_path, FAKE_SLEEP="0.3", FAKE_LOG=str(flog))
        t0 = time.perf_counter()
        procs = []
        for i in range(32):
            p = subprocess.Popen(cctripwire_cmd("run", "--wait", "--", "-p", f"adm {i}"), env=env, cwd=b.proj,
                                 stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL)
            b.procs.append(p)
            procs.append(p)
        rcs = []
        for p in procs:
            rcs.append(p.wait(timeout=600))
        wall = time.perf_counter() - t0
        mx, starts, ends = _overlap(flog)
        busy, total_err_lines, sample = _busy(b.section(name)["state"])
        out[label] = {"wall_s": wall, "max_concurrency_observed": mx, "fake_starts": starts,
                      "fake_ends": ends, "rc_counts": {str(rc): rcs.count(rc) for rc in sorted(set(rcs))},
                      "busy_error_lines": busy, "gateway_errors_lines": total_err_lines,
                      "gateway_errors_sample": sample,
                      "invocations": _inv_summary(b.section(name)["state"])}
    return out


def sec_snapshot(b: Bench) -> dict:
    log("[5] build_snapshot")
    cfg_path = b.config("snapshot")
    env = b.env("snapshot", cfg_path)
    os.environ.update(env)
    from cctripwire import config as cfgmod
    from cctripwire.forecast import build_snapshot
    from cctripwire.store import Store
    cfg = cfgmod.load(cfg_path)
    store = Store(cfg.db_path)
    now = time.time()
    cur = 0
    res = {}
    for target in (10_000, 100_000, 300_000):
        fill_tokens(store, target - cur, now, start=cur)
        cur = target
        ms = []
        for _ in range(10):
            t = time.perf_counter()
            build_snapshot(store, cfg, now=now)
            ms.append((time.perf_counter() - t) * 1000)
        res[target] = {"median_ms": statistics.median(ms), "min_ms": min(ms), "max_ms": max(ms),
                       "token_rows": store.conn.execute("SELECT COUNT(*) FROM token_events").fetchone()[0]}
    store.close()
    return res


def sec_transcripts(b: Bench) -> dict:
    log("[6] transcript scan")
    cfg_path = b.config("transcripts")
    env = b.env("transcripts", cfg_path)
    os.environ.update(env)
    from cctripwire import config as cfgmod
    from cctripwire.quota import transcripts
    from cctripwire.store import Store
    cfg = cfgmod.load(cfg_path)
    store = Store(cfg.db_path)
    proj_root = cfg.claude_dir / "projects"
    base = time.time() - 7200
    n_files, asst_per_file = 200, 2000
    t_gen = time.perf_counter()

    def line_user(sid, cwd, ts, k):
        return json.dumps({"type": "user", "sessionId": sid, "cwd": cwd, "timestamp": ts,
                           "message": {"role": "user", "content": f"bench prompt {k}"}})

    def line_asst(sid, cwd, ts, tag, k):
        return json.dumps({"type": "assistant", "sessionId": sid, "cwd": cwd, "timestamp": ts,
                           "requestId": f"req_{tag}_{k}",
                           "message": {"id": f"msg_{tag}_{k}", "model": MODEL,
                                       "usage": {"input_tokens": 120 + k % 50, "output_tokens": 300 + k % 90,
                                                 "cache_creation_input_tokens": 40, "cache_read_input_tokens": 900}}})

    def iso(sec: float) -> str:
        return dt.datetime.fromtimestamp(sec, dt.timezone.utc).isoformat().replace("+00:00", "Z")

    files = []
    for f in range(n_files):
        sid = f"bench-tx-{f:03d}"
        cwd = f"/home/bench/work/proj-{f % 20:02d}"
        d = proj_root / cwd.replace("/", "-")
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{sid}.jsonl"
        lines = []
        for k in range(asst_per_file):
            ts = iso(base + (f * asst_per_file + k) * 0.5)
            lines.append(line_user(sid, cwd, ts, k))
            lines.append(line_asst(sid, cwd, ts, f"{f}_{k}", k))
        p.write_text("\n".join(lines) + "\n")
        files.append((p, sid, cwd))
    gen_s = time.perf_counter() - t_gen
    total_lines = n_files * asst_per_file * 2

    t = time.perf_counter()
    new_full = transcripts.scan_all(store, cfg)
    full_s = time.perf_counter() - t

    t = time.perf_counter()
    new_noop = transcripts.scan_all(store, cfg)
    noop_s = time.perf_counter() - t

    appended = 5
    add_lines = 0
    for p, sid, cwd in files:
        extra = []
        for k in range(appended):
            ts = iso(time.time() - 60 + k)
            extra.append(line_user(sid, cwd, ts, 10_000 + k))
            extra.append(line_asst(sid, cwd, ts, f"new_{p.stem}", 10_000 + k))
        with open(p, "a") as fh:
            fh.write("\n".join(extra) + "\n")
        add_lines += len(extra)
    t = time.perf_counter()
    new_incr = transcripts.scan_all(store, cfg)
    incr_s = time.perf_counter() - t
    store.close()
    return {"files": n_files, "assistant_lines_per_file": asst_per_file, "total_lines": total_lines,
            "gen_s": gen_s, "full_s": full_s, "full_new_events": new_full,
            "full_lines_per_s": total_lines / full_s, "full_assistant_per_s": (total_lines // 2) / full_s,
            "noop_s": noop_s, "noop_new_events": new_noop,
            "incr_appended_lines": add_lines, "incr_s": incr_s, "incr_new_events": new_incr,
            "incr_lines_per_s": add_lines / incr_s if incr_s > 0 else float("nan")}


def start_fake_invocation(b: Bench, env: dict, i: int, store) -> subprocess.Popen:
    e = dict(env, FAKE_SLEEP="600")
    p = subprocess.Popen([str(b.wrapper), "-p", f"daemon bench {i}"], env=e, cwd=b.proj,
                         stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL, start_new_session=True)
    b.procs.append(p)
    now = time.time()
    store.create_invocation({
        "id": f"bench-inv-{i:03d}", "parent_id": None, "root_id": f"bench-inv-{i:03d}", "depth": 1,
        "pid": p.pid, "pgid": p.pid, "project": "bench-project", "agent": "bench", "argv": "[]",
        "prompt_hash": f"bench{i:04d}", "model": MODEL, "status": "running", "created": now,
        "started": now, "ended": None, "heartbeat": now, "exit_code": None, "tokens_in": 0,
        "tokens_out": 0, "weighted": 0.0, "cost_usd": None, "budget_weighted": None,
        "session_id": None, "reason": None})
    return p


def _proc_mem() -> dict:
    """VmRSS split into anonymous, file-backed and shared-memory pages (kB), from /proc/self/status."""
    out = {}
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            key, _, val = line.partition(":")
            if key in ("VmRSS", "RssAnon", "RssFile", "RssShmem", "VmHWM"):
                out[key] = int(val.split()[0])
    except OSError:
        pass
    return out


def daemon_child(cfg_path: str) -> None:
    """Runs in its own process so ru_maxrss measures only the daemon work."""
    import resource
    from cctripwire import config as cfgmod, daemon
    from cctripwire.store import Store
    cfg = cfgmod.load(cfg_path)
    store = Store(cfg.db_path)
    rss_open = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    mem_open = _proc_mem()
    ticks, reports = [], []
    for _ in range(20):
        t = time.perf_counter()
        rep = daemon.tick(cfg, store)
        ticks.append((time.perf_counter() - t) * 1000)
        reports.append(rep)
    rss_end = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(json.dumps({"ticks_ms": ticks, "rss_after_open_kb": rss_open, "rss_after_20_ticks_kb": rss_end,
                      "mem_open_kb": mem_open, "mem_end_kb": _proc_mem(),
                      "first_report": reports[0], "last_report": reports[-1]}))


def sec_daemon(b: Bench) -> dict:
    log("[7] daemon tick")
    cfg_path = b.config("daemon")
    env = b.env("daemon", cfg_path)
    os.environ.update(env)
    from cctripwire import config as cfgmod
    from cctripwire.store import Store
    cfg = cfgmod.load(cfg_path)
    store = Store(cfg.db_path)
    fill_tokens(store, 100_000, time.time(), start=0)
    fakes = [start_fake_invocation(b, env, i, store) for i in range(50)]
    store.close()
    try:
        r = subprocess.run([PY, str(Path(__file__).resolve()), "--daemon-child", str(cfg_path)], env=env,
                           capture_output=True, text=True, timeout=900, stdin=DEVNULL)
        if r.returncode != 0:
            return {"error": r.stderr[-2000:]}
        data = json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        for p in fakes:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()
    ticks = data["ticks_ms"]
    store2 = Store(cfg.db_path)
    running_after = store2.conn.execute("SELECT status, COUNT(*) FROM invocations GROUP BY status").fetchall()
    store2.close()
    return {"first_tick_ms": ticks[0], "steady_ticks_ms": summ(ticks[1:]), "all_ticks_ms": summ(ticks),
            "rss_after_open_kb": data["rss_after_open_kb"], "rss_after_20_ticks_kb": data["rss_after_20_ticks_kb"],
            "first_report": data["first_report"], "last_report": data["last_report"],
            "mem_open_kb": data["mem_open_kb"], "mem_end_kb": data["mem_end_kb"],
            "db_bytes": {f: (cfg.db_path.parent / f).stat().st_size if (cfg.db_path.parent / f).exists() else 0
                         for f in ("tripwire.db", "tripwire.db-wal", "tripwire.db-shm")},
            "invocation_status_after": {s: c for s, c in running_after}}


# ----------------------------------------------------------------- report

def render(info: dict, res: dict, out: Path, workdir: Path) -> str:
    L = []
    a = L.append
    a("# cctripwire benchmarks")
    a("")
    a("Generated by `bench/bench.py` (raw measurements; percentiles are nearest-rank).")
    a("")
    a("## Machine")
    a("")
    for k in ("date", "cpu", "cores", "memory", "python", "python_exe", "sqlite", "platform", "cctripwire_commit"):
        a(f"- **{k}**: {info[k]}")
    a("")
    a("Real `claude` was never executed: every gateway/daemon child is `tests/fake_claude.py` via a wrapper "
      "script placed in `CCTRIPWIRE_REAL_CLAUDE`. All state lives under a temporary directory.")
    a("")

    def row(name, s, unit="ms"):
        return f"| {name} | {s['n']} | {fmt(s['p50'])} | {fmt(s['p95'])} | {fmt(s['max'])} | {fmt(s['mean'])} |"

    hdr = "| metric | n | p50 | p95 | max | mean |\n|---|---|---|---|---|---|"
    h = res["hooks"]
    a("## 1. Hook latency (`cctripwire hook`, PreToolUse)")
    a("")
    a("DB pre-filled with 10,000 `events` rows and 50,000 `token_events` rows. "
      f"Fill took {fmt(h['fill_s'])} s (not timed in results).")
    a("")
    a(hdr)
    a(row("subprocess `python -m cctripwire hook` (wall, ms)", h["hook_sub"]))
    a(row("in-process `hooks.run` (ms)", h["hook_inproc"]))
    a("")
    a(f"Non-zero exit codes from the subprocess: {h['hook_nonzero_rc']}. "
      f"New lines appended to hook-errors.log during the subprocess runs: {h['hook_errors_logged']}.")
    a("")
    a("## 2. Status-line latency (`cctripwire statusline`, 50 runs)")
    a("")
    a(hdr)
    a(row("subprocess (wall, ms)", h["statusline"]))
    a("")
    a(f"Non-zero exit codes: {h['statusline_nonzero_rc']}. Last output: `{h['statusline_last_output']}`")
    a("")
    g = res["gateway"]
    a("## 3. Gateway overhead (`cctripwire run -- -p x` vs fake claude alone, FAKE_SLEEP=0, 30 pairs)")
    a("")
    a("Order within each pair alternates. `paired diff` = gateway minus fake for the same pair.")
    a("")
    a(hdr)
    a(row("gateway `cctripwire run -- -p x` (ms)", g["gateway"]))
    a(row("fake claude alone (ms)", g["fake"]))
    a(row("paired diff gateway - fake (ms)", g["paired_diff"]))
    a("")
    a(f"Pairs with a non-zero exit: {g['rc_pairs_nonzero']}. gateway-errors.log lines: {g['gateway_errors_lines']}.")
    a("")
    a("Recorded invocation outcomes: " + "; ".join(g["invocations"]))
    a("")
    a("The shipped `spawn_rate_per_min` of 12 is raised to 100000 for this section. In the first run of this "
      "script, with the shipped value, the 13th and 25th sequential gateway runs each waited about 60 s "
      "for the 60 s window to clear, which is what drives the 60 s p95 in that run.")
    a("")
    ad = res["admission"]
    a("## 4. Admission: 32 concurrent gateways, FAKE_SLEEP=0.3, max_concurrency=4, `--wait`")
    a("")
    a("| variant | total wall (s) | max concurrent fakes (overlap) | fake starts / ends | gateway exit codes | busy/locked lines in gateway-errors.log | all gateway-errors.log lines |")
    a("|---|---|---|---|---|---|---|")
    for label, v in ad.items():
        rc = ", ".join(f"{k}: {n}" for k, n in v["rc_counts"].items())
        a(f"| {label} | {fmt(v['wall_s'], 3)} | {v['max_concurrency_observed']} | "
          f"{v['fake_starts']} / {v['fake_ends']} | {rc} | {v['busy_error_lines']} | {v['gateway_errors_lines']} |")
    a("")
    for label, v in ad.items():
        a(f"Recorded invocation outcomes, {label}: " + "; ".join(v["invocations"]))
        a("")
    for label, v in ad.items():
        if v["gateway_errors_sample"]:
            a(f"Sample gateway-errors.log lines ({label}):")
            a("")
            for l in v["gateway_errors_sample"]:
                a(f"    {l}")
            a("")
    s = res["snapshot"]
    a("## 5. build_snapshot (median of 10, ms)")
    a("")
    a("| token_events rows | median ms | min ms | max ms |")
    a("|---|---|---|---|")
    for k, v in s.items():
        a(f"| {v['token_rows']} | {fmt(v['median_ms'], 3)} | {fmt(v['min_ms'], 3)} | {fmt(v['max_ms'], 3)} |")
    a("")
    a("No `quota_samples` were inserted, so the window estimator takes its no-data path here.")
    a("")
    t = res["transcripts"]
    a("## 6. Transcript scan throughput")
    a("")
    a(f"{t['files']} files, {t['assistant_lines_per_file']:,} assistant lines per file, "
      f"one user line before each assistant line: {t['total_lines']:,} lines total "
      f"({t['total_lines'] // 2:,} assistant). Generation took {fmt(t['gen_s'])} s (not timed).")
    a("")
    a("| pass | wall (s) | lines | lines/s | new token events returned |")
    a("|---|---|---|---|---|")
    a(f"| first full scan | {fmt(t['full_s'], 3)} | {t['total_lines']:,} | {t['full_lines_per_s']:,.0f} | {t['full_new_events']:,} |")
    a(f"| rescan, no changes | {fmt(t['noop_s'], 3)} | {t['total_lines']:,} (skipped via offsets) | n/a | {t['noop_new_events']} |")
    a(f"| incremental after appending | {fmt(t['incr_s'], 3)} | {t['incr_appended_lines']:,} | {t['incr_lines_per_s']:,.0f} | {t['incr_new_events']:,} |")
    a("")
    a(f"First full scan: {t['full_assistant_per_s']:,.0f} assistant lines/s.")
    a("")
    d = res["daemon"]
    a("## 7. Daemon tick (50 running fake invocations, 100,000 token events)")
    a("")
    if "error" in d:
        a(f"Daemon child failed: `{d['error']}`")
    else:
        a(hdr)
        a(row("tick, first (ms)", {"n": 1, "p50": d["first_tick_ms"], "p95": d["first_tick_ms"],
                                    "max": d["first_tick_ms"], "mean": d["first_tick_ms"]}))
        a(row("tick, steady state (ticks 2-20)", d["steady_ticks_ms"]))
        a(row("tick, all 20", d["all_ticks_ms"]))
        a("")
        a(f"Daemon ru_maxrss after open: {d['rss_after_open_kb']:,} KiB; after 20 ticks: {d['rss_after_20_ticks_kb']:,} KiB.")
        a("")
        a("Caveat: the child's own peak is VmHWM in the table below (reset on exec). The ru_maxrss figures above "
          "are far larger than the child's VmHWM at open, which points to a high-water mark carried over from the "
          "benchmark parent process at fork time (the parent holds the 100,000-row fill in memory). Read VmHWM "
          "as the daemon's peak.")
        a("")
        a("/proc/self/status breakdown (kB), at open and after 20 ticks:")
        a("")
        a("| point | VmRSS | RssAnon | RssFile | RssShmem | VmHWM |")
        a("|---|---|---|---|---|---|")
        for label, m in (("open", d["mem_open_kb"]), ("after 20 ticks", d["mem_end_kb"])):
            a(f"| {label} | " + " | ".join(str(m.get(k, "n/a")) for k in
                                           ("VmRSS", "RssAnon", "RssFile", "RssShmem", "VmHWM")) + " |")
        a("")
        a(f"tripwire.db sizes after the run (bytes): {d['db_bytes']}")
        a("")
        a(f"Invocation statuses after the run: {d['invocation_status_after']}")
        a("")
        a(f"First tick report: `{d['first_report']}`")
        a("")
        a(f"Last tick report: `{d['last_report']}`")
    a("")
    a("---")
    a(f"Scratch directory used: `{workdir}`.")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "docs" / "results" / "benchmarks.md"))
    ap.add_argument("--workdir", default=None, help="scratch dir (default: a new temp dir, removed at the end)")
    ap.add_argument("--keep", action="store_true", help="keep the scratch dir")
    ap.add_argument("--daemon-child", metavar="CONFIG", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.daemon_child:
        daemon_child(args.daemon_child)
        return 0

    if not FAKE.exists():
        print(f"missing {FAKE}", file=sys.stderr)
        return 2
    info = machine_info()
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="cctripwire-bench-"))
    b = Bench(workdir)
    res: dict = {}
    try:
        # Sections 1 and 2 share the hooks DB.
        hs = sec_hooks(b)
        res["hooks"] = hs
        res["gateway"] = sec_gateway_overhead(b)
        res["admission"] = sec_admission(b)
        res["snapshot"] = sec_snapshot(b)
        res["transcripts"] = sec_transcripts(b)
        res["daemon"] = sec_daemon(b)
    finally:
        b.cleanup()
        # Keep the parent process clean of the isolated env it borrowed.
        for k in [k for k in os.environ if k.startswith(("CCTRIPWIRE_", "FAKE_", "CLAUDE_"))]:
            os.environ.pop(k, None)
    out = Path(args.out)
    if not out.is_absolute():
        out = Path.cwd() / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(info, res, out, workdir))
    log(f"wrote {out}")
    if not args.keep and not args.workdir:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

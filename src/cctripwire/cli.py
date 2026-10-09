"""`cctripwire` command line.

Each subcommand is a thin layer over the module that owns the behaviour. Modules
still being written by other workers (gateway, hooks, daemon, procs, interventions,
quota.*) are imported inside the subcommand, so `cctripwire --help` and the hook path
stay light, and a missing module fails only the command that needs it.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import shutil
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import dashboard, util
from .config import default_config_path, load
from .models import FIVE_HOUR, SEVEN_DAY, Level, QuotaSample
from .policy import OVERRIDE_KEY
from .store import Store

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
               "PreCompact", "SubagentStart", "SubagentStop", "Stop", "SessionEnd")
MATCHER_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")
HOOK_TIMEOUT_S = 10
WINDOW_ALIASES = {"5h": FIVE_HOUR, "five_hour": FIVE_HOUR, "7d": SEVEN_DAY, "seven_day": SEVEN_DAY}
LEVEL_CODES = {"ok": "32", "warn": "33", "throttle": "33;1", "restrict": "31", "stop": "31;1"}

EXAMPLE_CONFIG = """\
# cctripwire configuration. Every key is optional; commented values are the built-in defaults.
# Keys you leave out keep their defaults. Run `cctripwire limits` to see the effective values.

[general]
# state_dir = "~/.local/state/cctripwire"     # or $CCTRIPWIRE_HOME
# claude_dir = ""                           # "" = $CLAUDE_CONFIG_DIR or ~/.claude
# stale_after_s = 900

[sources]
# statusline = true
# statusline_chain = ""                     # previous statusLine command; `install --statusline` prints it
# oauth = false                             # opt-in, undocumented account endpoint
# five_hour_budget = 0                      # weighted tokens per 100% (0 = auto-calibrate)
# seven_day_budget = 0

[gateway]
# max_concurrency = 4
# max_depth = 2
# spawn_rate_per_min = 12

[levels.restrict]
# deny_tools = ["Agent", "Task", "Workflow", "WebSearch"]

# [budgets.projects.myrepo]
# soft_weighted = 2e6
# hard_weighted = 4e6
"""


class CliError(Exception):
    """An expected failure: printed as one line, exit status 1."""


# -- argument types -----------------------------------------------------------------

_DUR_FULL = re.compile(r"(?:\d+(?:\.\d+)?[smhd]?)+")
_DUR_PART = re.compile(r"(\d+(?:\.\d+)?)([smhd]?)")
_UNIT_S = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(text: str) -> float:
    """'90' -> 90s, '30m', '2h', '1h30m' -> seconds."""
    t = str(text).strip().lower()
    if not _DUR_FULL.fullmatch(t):
        raise argparse.ArgumentTypeError(f"bad duration {text!r} (examples: 90, 30m, 2h, 1h30m)")
    return sum(float(n) * _UNIT_S[u] for n, u in _DUR_PART.findall(t))


def parse_level(text: str) -> Level:
    try:
        return Level.parse(text)
    except KeyError:
        raise argparse.ArgumentTypeError(f"unknown level {text!r} (ok, warn, throttle, restrict, stop)") from None


def window_name(text: str) -> str:
    t = text.strip().lower()
    return WINDOW_ALIASES.get(t, t)


# -- small helpers ------------------------------------------------------------------

def _open(args) -> tuple[Any, Store]:
    cfg = load(args.config)
    return cfg, Store(cfg.db_path)


def _invalidate(store: Store) -> None:
    from . import engine   # imported lazily: it pulls in the forecast module
    engine.invalidate(store)


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _clock(ts: float | None, fmt: str = "%H:%M:%S") -> str:
    if ts is None:
        return "-"
    return time.strftime(fmt, time.localtime(ts))


def _paint(text: str, code: str) -> str:
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        return f"\x1b[{code}m{text}\x1b[0m"
    return text


def _label_window(name: str) -> str:
    return {FIVE_HOUR: "5h", SEVEN_DAY: "7d"}.get(name, name)


def _quality_word(w: dict) -> str:
    return dashboard.window_quality(w)


# -- status rendering (text) ----------------------------------------------------------

def _window_line(w: dict, now: float) -> str:
    q = _quality_word(w)
    if w["used_pct"] is None:
        used = "?"
    else:
        used = ("~" if q == "estimate" else "") + f"{w['used_pct']:.0f}%"
    parts = [f"  {_label_window(w['window']):<10}{used:>6}  {q:<9}"]
    if w.get("age_s") is not None:
        parts.append(f"age {util.fmt_duration(w['age_s'])}")
    if w.get("source") not in (None, "", "none"):
        parts.append(f"via {w['source']}")
    if w.get("burn_pct_per_hour") is not None:
        parts.append(f"burn {w['burn_pct_per_hour']:+.1f}%/h")
    if w.get("eta_s") is not None:
        parts.append(f"ETA {util.fmt_duration(w['eta_s'])}")
    if w.get("resets_at"):
        parts.append(f"resets in {util.fmt_duration(w['resets_at'] - now)}")
    if w.get("stale"):
        parts.append("STALE")
    return "  ".join(parts)


def render_status(p: dict) -> str:
    lines = []
    lv = p["level"]
    head = f"cctripwire  {_clock(p['ts'])}  level {_paint(lv.upper(), LEVEL_CODES.get(lv, '0'))}"
    if p.get("override"):
        head += "  (manual override)"
    lines.append(head)
    for r in p["reasons"][:6]:
        lines.append(f"  - {r}")
    br = p.get("breaker") or {}
    if br.get("tripped"):
        until = f" until {_clock(br['until'], '%H:%M')}" if br.get("until") else ""
        lines.append(_paint(f"RUNAWAY BREAKER TRIPPED{until}: {br.get('reason')}", "1;31"))
        lines.append("  claude -p launches are refused; `cctripwire breaker reset` to lift")
    elif br.get("lifted_until"):
        lines.append(f"runaway breaker lifted until {_clock(br['lifted_until'], '%H:%M')}")
    elif br.get("observe"):
        seen = br.get("last_would_trip")
        lines.append("runaway breaker: observe only" + (
            f" (would have tripped {_clock(seen['at'], '%H:%M')}: {seen['reason']})" if seen else ""))
    lines.append("")
    for w in p["windows"]:
        lines.append(_window_line(w, p["ts"]))
    lines.append("")
    tl = p["tokens_last_hour"]
    lines.append(f"tokens/min {util.fmt_tokens(p['tokens_per_min'])}   last hour "
                 f"{util.fmt_tokens(tl['weighted'])} weighted ({tl['messages']} messages)")
    lines.append(f"running invocations {p['running_invocations']}   active sessions {p['active_sessions']}")
    if p["by_project"]:
        lines.append("projects: " + ", ".join(f"{k} {util.fmt_tokens(v)}" for k, v in list(p["by_project"].items())[:5]))
    if p["by_agent"]:
        lines.append("agents:   " + ", ".join(f"{k} {util.fmt_tokens(v)}" for k, v in list(p["by_agent"].items())[:5]))
    lines.append("quality: exact = reported by Claude Code, ~estimate = inferred, ? = unknown")
    return "\n".join(lines)


# -- read-only commands -----------------------------------------------------------------

def cmd_status(args) -> int:
    cfg, store = _open(args)
    p = dashboard.status_payload(cfg, store, max_age=0)
    if args.json:
        _print_json(p)
    else:
        print(render_status(p))
    return 0


def cmd_watch(args) -> int:
    cfg, store = _open(args)
    n = 0
    try:
        while True:
            p = dashboard.status_payload(cfg, store, max_age=0)
            if sys.stdout.isatty():
                sys.stdout.write("\x1b[H\x1b[2J")
            print(render_status(p))
            print(f"-- every {args.interval:g}s, Ctrl-C to exit")
            sys.stdout.flush()
            n += 1
            if args.iterations and n >= args.iterations:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def _tree(rows: list[dict]) -> list[tuple[int, dict]]:
    """Depth-first (depth, row) pairs; a process whose parent is not listed is a root."""
    pids = {r["pid"] for r in rows}
    kids: dict[Any, list[dict]] = defaultdict(list)
    roots = []
    for r in rows:
        if r["ppid"] in pids and r["ppid"] != r["pid"]:
            kids[r["ppid"]].append(r)
        else:
            roots.append(r)
    out: list[tuple[int, dict]] = []
    seen: set = set()

    def walk(r: dict, depth: int) -> None:
        if r["pid"] in seen:
            return
        seen.add(r["pid"])
        out.append((depth, r))
        for c in sorted(kids.get(r["pid"], []), key=lambda x: x["pid"] or 0):
            walk(c, depth + 1)

    for r in sorted(roots, key=lambda x: x["pid"] or 0):
        walk(r, 0)
    return out


def _cmd_label(argv: list) -> str:
    if not argv:
        return "?"
    s = " ".join(os.path.basename(str(a)) if i == 0 else str(a) for i, a in enumerate(argv))
    return s[:60]


def cmd_top(args) -> int:
    from . import procs
    cfg, store = _open(args)
    ps = list(procs.list_claude_processes())
    byp = {dashboard.attr(p, "pid") for p in procs.bypassers(ps)}
    rows = [{"pid": dashboard.attr(p, "pid"), "ppid": dashboard.attr(p, "ppid"),
             "pgid": dashboard.attr(p, "pgid"), "rss_kb": dashboard.attr(p, "rss_kb"),
             "cpu": dashboard.attr(p, "cpu"), "invocation_id": dashboard.attr(p, "invocation_id"),
             "is_print": bool(dashboard.attr(p, "is_print")),
             "bypass": dashboard.attr(p, "pid") in byp,
             "cmd": _cmd_label(dashboard.attr(p, "cmdline") or [])} for p in ps]
    invs = store.invocations(status=list(dashboard.LIVE_INVOCATION_STATES), limit=50)
    if args.json:
        _print_json({"processes": rows, "invocations": [dashboard._inv_row(r) for r in invs]})
        return 0
    gateway_n = sum(1 for r in rows if r["invocation_id"])
    print(f"claude processes: {len(rows)}   gateway: {gateway_n}   bypass: {len(byp)}")
    for depth, r in _tree(rows):
        kind = "BYPASS" if r["bypass"] else ("gateway" if r["invocation_id"] else "interactive")
        mem = f"{(r['rss_kb'] or 0) / 1024:.0f}M"
        inv = r["invocation_id"] or "-"
        print(f"{'  ' * depth}{r['pid']!s:<7} {kind:<11} rss {mem:>6}  inv {inv:<12} {r['cmd']}")
    if not rows:
        print("no claude processes")
    print("")
    print("invocations:")
    if not invs:
        print("  none")
    now = util.now()
    for r in invs:
        age = util.fmt_duration(now - (r.get("created") or now))
        print(f"  {r['id']:<12} {r['status']:<10} depth {r.get('depth') or 0}  "
              f"{util.fmt_tokens(r.get('weighted'))} tok  {r.get('project') or '-'}  {age} ago")
    return 0


def cmd_history(args) -> int:
    cfg, store = _open(args)
    h = dashboard.history_payload(cfg, store, hours=args.hours, bucket_s=args.bucket)
    if args.json:
        _print_json(h)
        return 0
    buckets = h["buckets"]
    peak = max((b["weighted"] for b in buckets), default=0) or 1
    if not buckets:
        print("no token activity in this window")
    for b in buckets:
        bar = "#" * max(1, round(40 * b["weighted"] / peak)) if b["weighted"] > 0 else ""
        print(f"{_clock(b['ts'], '%m-%d %H:%M')}  {util.fmt_tokens(b['weighted']):>7}  {bar}")
    for win, samples in h["samples"].items():
        for q in samples:
            kind = "exact" if q["exact"] else "estimate"
            print(f"{_clock(q['ts'], '%m-%d %H:%M')}  {_label_window(win):<4} {q['used_pct']:5.1f}%  "
                  f"{kind} ({q['source']})")
    return 0


def cmd_loops(args) -> int:
    cfg, store = _open(args)
    rows = dashboard.loops_payload(store, window_s=args.since)["detections"]
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("no detections")
    for r in rows:
        text = r["advice"] or r["detail"] or ""
        print(f"{_clock(r['ts'])}  {r['severity']:<9} {r['kind']:<22} {(r['session_id'] or '-')[:8]:<8} {text}")
    return 0


def cmd_interventions(args) -> int:
    cfg, store = _open(args)
    rows = dashboard.interventions_payload(store, window_s=args.since)["interventions"]
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("no interventions")
    for r in rows:
        print(f"{_clock(r['ts'])}  {r['action']:<12} {(r['target'] or '-'):<14} {r['level']:<9} {r['reason'] or ''}")
    return 0


def cmd_limits(args) -> int:
    cfg = load(args.config)
    p = dashboard.limits_payload(cfg)
    if args.json:
        _print_json(p)
        return 0
    print("rules:")
    for r in p["rules"]:
        print(f"  {r.get('name', '?'):<18} {str(r.get('level', '?')):<9} {r.get('when', '')}")
    for name, values in p["levels"].items():
        print(f"level {name}: " + ", ".join(f"{k}={v}" for k, v in values.items()))
    for kind in ("projects", "agents"):
        for name, b in (p["budgets"].get(kind) or {}).items():
            print(f"budget {kind[:-1]} {name}: " + ", ".join(f"{k}={v}" for k, v in b.items()))
    print("gateway: " + ", ".join(f"{k}={v}" for k, v in p["gateway"].items()))
    for problem in p["problems"]:
        print(f"PROBLEM: {problem}")
    return 0


def cmd_checkpoints(args) -> int:
    from . import interventions
    cfg = load(args.config)
    items = list(interventions.list_checkpoints(cfg) or [])
    if args.json:
        _print_json(items)
        return 0
    if not items:
        print("no checkpoints")
    for it in items:
        if isinstance(it, dict):
            print(f"{it.get('inv_id') or it.get('id') or '?'}  {_clock(it.get('created'), '%m-%d %H:%M') if isinstance(it.get('created'), (int, float)) else (it.get('created') or '')}  {it.get('reason') or ''}")
        else:
            print(str(it))
    return 0


def cmd_resume_checkpoint(args) -> int:
    from . import interventions
    cfg = load(args.config)
    res = interventions.resume_checkpoint(cfg, args.inv_id, dry_run=args.dry_run)
    if args.dry_run:
        print(shlex.join(map(str, res)) if isinstance(res, (list, tuple)) else str(res))
        return 0
    rc = getattr(res, "returncode", None)
    return rc if isinstance(rc, int) else 0


def cmd_doctor(args) -> int:
    from . import breaker, gateway
    cfg = load(args.config)
    results: list[tuple[str, str, str]] = []

    def add(level: str, name: str, detail: str) -> None:
        results.append((level, name, detail))

    errs = cfg.validate()
    where = str(cfg.path) if cfg.path and cfg.path.exists() else "defaults (no config file)"
    add("ok" if not errs else "fail", "config", f"{where}: " + ("valid" if not errs else "; ".join(errs)))

    store = None
    try:
        store = Store(cfg.db_path)
        with store.tx(immediate=True):   # takes the write lock; writes nothing
            pass
        add("ok", "database", f"{cfg.db_path} writable")
    except Exception as e:  # noqa: BLE001 - report, keep checking
        add("fail", "database", f"{cfg.db_path}: {e}")

    try:
        real = gateway.resolve_real_claude(cfg)
        add("ok", "claude", real)
    except FileNotFoundError:
        add("warn", "claude", "real claude binary not found (set gateway.real_claude or CCTRIPWIRE_REAL_CLAUDE)")
    except RuntimeError as e:
        add("warn", "claude", str(e))
    first = shutil.which("claude")
    if first and _is_shim(first):
        add("ok", "shim", f"{first} is first on PATH: claude -p goes through the breaker")
    else:
        add("warn", "shim", "not first on PATH in this shell: claude -p skips the gateway "
            "(`cctripwire setup --edit-rc`, then open a new shell)")
    if store is not None:
        st = breaker.status(store, cfg)
        if not st["enabled"]:
            add("warn", "breaker", "disabled (no storm or surge limits configured)")
        elif st["tripped"]:
            add("fail", "breaker", f"TRIPPED: {st['reason']} (`cctripwire breaker reset`)")
        elif st["lifted_until"]:
            add("warn", "breaker", f"off until {_clock(st['lifted_until'], '%H:%M')}")
        elif st["observe"]:
            seen = st.get("last_would_trip")
            add("ok", "breaker", "observe only" + (f"; last would-trip: {seen['reason']}" if seen else
                                                   "; no would-trips recorded"))
        else:
            add("ok", "breaker", "armed")

    hook_where, sl_where, bad = [], [], []
    for label, path in (("project", Path.cwd() / ".claude" / "settings.json"),
                        ("user", cfg.claude_dir / "settings.json")):
        if not path.exists():
            continue
        try:
            s = _read_settings(path)
        except (OSError, ValueError) as e:
            bad.append(f"{path}: {e}")
            continue
        if _has_cctripwire_hooks(s):
            hook_where.append(f"{label} {path}")
        if _has_cctripwire_statusline(s):
            sl_where.append(f"{label} {path}")
    for b in bad:
        add("fail", "settings", b)
    add("ok" if hook_where else "warn", "hooks",
        "; ".join(hook_where) or "not installed (run `cctripwire install`)")
    add("ok" if sl_where else "warn", "statusLine",
        "; ".join(sl_where) or "not installed (`cctripwire install --statusline`)")

    now = util.now()
    stale = float(cfg.get("general.stale_after_s", 900))
    if store is not None:
        q = store.latest_quota(FIVE_HOUR, source="statusline")
        if q is None:
            add("warn", "statusline data", "no status-line reading yet (needs an active session with statusLine)")
        else:
            age = now - q.ts
            add("ok" if age < stale else "warn", "statusline data",
                f"last 5h reading {util.fmt_duration(age)} ago" + ("" if age < stale else " (stale)"))
        if not cfg.get("sources.oauth"):
            add("ok", "oauth", "disabled (opt-in)")
        else:
            st = store.kv_get("oauth_status")
            add("ok" if not st else "warn", "oauth", st or "enabled, no errors recorded")
        hb = store.kv_get("daemon:heartbeat")
        ts = hb.get("ts") if isinstance(hb, dict) else hb
        interval = float(cfg.get("daemon.interval_s", 15))
        if not isinstance(ts, (int, float)):
            add("warn", "daemon", "no heartbeat (run `cctripwire daemon` or `cctripwire tick`)")
        else:
            age = now - float(ts)
            add("ok" if age < 3 * interval else "warn", "daemon",
                f"heartbeat {util.fmt_duration(age)} ago")

    for level, name, detail in results:
        print(f"{level:<5} {name:<16} {detail}")
    return 1 if any(lv == "fail" for lv, _, _ in results) else 0


# -- config ---------------------------------------------------------------------------

def cmd_config(args) -> int:
    if args.action == "show":
        _print_json(load(args.config).data)
        return 0
    if args.action == "validate":
        cfg = load(args.config)
        errs = cfg.validate()
        for e in errs:
            print(e)
        if errs:
            return 1
        print("config ok")
        return 0
    target = Path(args.config) if args.config else default_config_path()
    if target.exists() and not args.force:
        raise CliError(f"{target} exists (use --force to overwrite)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    print(f"wrote {target}")
    return 0


# -- settings.json install / uninstall ---------------------------------------------------

def cctripwire_command(kind: str) -> str:
    """`cctripwire <kind>` by absolute path when the console script exists, else `python -m`."""
    exe = shutil.which("cctripwire")
    if exe:
        return f"{shlex.quote(os.path.abspath(exe))} {kind}"
    return f"{shlex.quote(sys.executable)} -m cctripwire {kind}"


def is_cctripwire_command(cmd: Any) -> bool:
    if not isinstance(cmd, str):
        return False
    return "cctripwire" in cmd and any(tok in ("hook", "statusline") for tok in cmd.split())


def _is_shim(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return b"cctripwire-shim" in f.read(4096)
    except OSError:
        return False


def _has_cctripwire_hooks(settings: dict) -> bool:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    return any(is_cctripwire_command(h.get("command")) for groups in hooks.values() if isinstance(groups, list)
               for g in groups if isinstance(g, dict) for h in (g.get("hooks") or []) if isinstance(h, dict))


def _has_cctripwire_statusline(settings: dict) -> bool:
    sl = settings.get("statusLine")
    return isinstance(sl, dict) and is_cctripwire_command(sl.get("command"))


def _settings_path(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser().absolute()
    return Path.cwd() / ".claude" / "settings.json"


def _read_settings(path: Path) -> dict:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("top level is not a JSON object")
    return data


def _load_or_raise(path: Path) -> dict:
    try:
        return _read_settings(path)
    except (OSError, ValueError) as e:
        raise CliError(f"cannot read {path} ({e}); not modified") from None


def _strip_cctripwire_hooks(settings: dict) -> None:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = []
        for g in groups:
            inner = g.get("hooks") if isinstance(g, dict) else None
            if isinstance(inner, list) and inner:
                rest = [h for h in inner if not (isinstance(h, dict) and is_cctripwire_command(h.get("command")))]
                if not rest:
                    continue            # the group existed only for cctripwire
                g = {**g, "hooks": rest}
            kept.append(g)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)


def _apply_hooks(settings: dict, cmd: str) -> None:
    _strip_cctripwire_hooks(settings)
    hooks = settings.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        group: dict = {"hooks": [{"type": "command", "command": cmd, "timeout": HOOK_TIMEOUT_S}]}
        if event in MATCHER_EVENTS:
            group = {"matcher": "*", **group}
        hooks.setdefault(event, []).append(group)


def _apply_statusline(settings: dict, cmd: str) -> str | None:
    """Install our statusLine. Returns a foreign command it displaced (to chain), if any."""
    prev = settings.get("statusLine")
    chained = None
    if isinstance(prev, dict) and isinstance(prev.get("command"), str) and not is_cctripwire_command(prev["command"]):
        chained = prev["command"]
    settings["statusLine"] = {"type": "command", "command": cmd}
    return chained


def _remove_statusline(settings: dict) -> None:
    if _has_cctripwire_statusline(settings):
        del settings["statusLine"]


def _backup_path(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    cand = path.with_name(f"{path.name}.cctripwire-bak-{stamp}")
    n = 1
    while cand.exists():
        cand = path.with_name(f"{path.name}.cctripwire-bak-{stamp}-{n}")
        n += 1
    return cand


def _save_settings(path: Path, settings: dict) -> Path | None:
    """Write atomically, backing up the previous file first. Returns the backup path."""
    backup = None
    if path.exists():
        backup = _backup_path(path)
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".cctripwire-tmp")
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


def cmd_install(args) -> int:
    path = _settings_path(args.settings)
    settings = _load_or_raise(path)
    before = json.dumps(settings, sort_keys=True)
    hooks_on = not args.no_hooks
    notes, chained = [], None
    if hooks_on:
        _apply_hooks(settings, cctripwire_command("hook"))
        notes.append(f"{len(HOOK_EVENTS)} hook events")
    if args.statusline:
        chained = _apply_statusline(settings, cctripwire_command("statusline"))
        notes.append("statusLine")
    if args.dry_run:
        _print_json(settings)
        print(f"dry run: {path} not changed")
        return 0
    if json.dumps(settings, sort_keys=True) == before:
        print(f"up to date: {path}")
    else:
        backup = _save_settings(path, settings)
        if backup:
            print(f"backup: {backup}")
        print(f"installed {', '.join(notes)} into {path}")
    if chained:
        print("an existing statusLine command was displaced. To keep showing it, add this to the config:")
        print("  [sources]")
        print(f"  statusline_chain = {json.dumps(chained)}")
    return 0


def cmd_uninstall(args) -> int:
    path = _settings_path(args.settings)
    if not path.exists():
        print(f"nothing to do: {path} does not exist")
        return 0
    settings = _load_or_raise(path)
    before = json.dumps(settings, sort_keys=True)
    _strip_cctripwire_hooks(settings)
    _remove_statusline(settings)
    if json.dumps(settings, sort_keys=True) == before:
        print(f"nothing to remove in {path}")
        return 0
    backup = _save_settings(path, settings)
    if backup:
        print(f"backup: {backup}")
    print(f"removed cctripwire entries from {path}")
    print("if a statusline_chain was configured, it is not restored to settings.json automatically")
    return 0


# -- install-shim / gateway / hooks / daemon ------------------------------------------

def cmd_setup(args) -> int:
    from . import installer
    settings = Path(args.settings).expanduser() if args.settings else None
    for line in installer.setup(observe=args.observe, shim=not args.no_shim, edit_rc=args.edit_rc,
                                service=not args.no_daemon, statusline=not args.no_statusline,
                                settings=settings, dry_run=args.dry_run):
        print(line)
    if not args.dry_run:
        print("done. Check it with `cctripwire doctor`; undo everything with `cctripwire teardown`.")
    return 0


def cmd_demo(args) -> int:
    from . import demo
    demo.run(n=args.n, sleep_s=args.sleep)
    return 0


def cmd_teardown(args) -> int:
    from . import installer
    settings = Path(args.settings).expanduser() if args.settings else None
    for line in installer.teardown(settings=settings):
        print(line)
    return 0


def cmd_install_shim(args) -> int:
    from . import gateway
    path = gateway.install_shim(Path(args.dir).expanduser() if args.dir else None)
    print(f"shim installed{': ' + str(path) if path else ''}")
    return 0


def _gateway_run(rest: list[str]) -> int:
    from . import gateway
    rc = gateway.main(list(rest))
    return rc if isinstance(rc, int) else 0


def cmd_run(args) -> int:
    return _gateway_run(args.rest)


def cmd_hook(args) -> int:
    from . import hooks
    rc = hooks.main()
    return rc if isinstance(rc, int) else 0


def cmd_statusline(args) -> int:
    try:
        text = "" if sys.stdin.isatty() else sys.stdin.read()
        from .quota import statusline
        cfg, store = _open(args)
        out = statusline.handle(text, store, cfg)
    except Exception:  # noqa: BLE001 - the status line must never fail loudly
        out = "cctripwire"
    sys.stdout.write(str(out).rstrip("\n") + "\n")
    return 0


def cmd_scan(args) -> int:
    from .quota import transcripts
    cfg, store = _open(args)
    n = transcripts.scan_all(store, cfg)
    print(f"scanned transcripts: {n} new token events")
    return 0


def cmd_tick(args) -> int:
    from . import daemon
    cfg, _ = _open(args)
    daemon.run(cfg, once=True)
    return 0


def cmd_daemon(args) -> int:
    from . import daemon
    cfg, store = _open(args)
    srv = None
    if args.dashboard:
        srv = dashboard.make_server(cfg, store, args.host, args.port)
        threading.Thread(target=srv.serve_forever, name="cctripwire-dashboard", daemon=True).start()
        print(f"dashboard: http://{srv.server_address[0]}:{srv.server_address[1]}/", file=sys.stderr)
    try:
        daemon.run(cfg, once=False)
    except KeyboardInterrupt:
        pass
    finally:
        if srv is not None:
            srv.shutdown()
            srv.server_close()
    return 0


def cmd_dashboard(args) -> int:
    cfg, store = _open(args)
    srv = dashboard.make_server(cfg, store, args.host, args.port)
    host, port = srv.server_address[0], srv.server_address[1]
    print(f"dashboard: http://{host}:{port}/  (Ctrl-C to stop)", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


# -- control commands ------------------------------------------------------------------

def _set_override(store: Store, level: Level, until: float | None, reason: str | None) -> None:
    store.kv_set(OVERRIDE_KEY, {"level": str(level), "until": until,
                                "reason": reason or f"manual override: {level}"})
    _invalidate(store)


def cmd_override(args) -> int:
    cfg, store = _open(args)
    if args.level.strip().lower() == "clear":
        store.kv_set(OVERRIDE_KEY, None)
        _invalidate(store)
        print("override cleared")
        return 0
    lvl = parse_level(args.level)
    until = None if args.duration is None else util.now() + args.duration
    _set_override(store, lvl, until, args.reason)
    print(f"override: {lvl} " + (f"until {_clock(until, '%H:%M')}" if until else "(until cleared)"))
    return 0


def cmd_pause(args) -> int:
    cfg, store = _open(args)
    until = None if args.duration is None else util.now() + args.duration
    _set_override(store, Level.STOP, until, "paused by cctripwire pause")
    print("paused: level STOP " + (f"until {_clock(until, '%H:%M')}" if until else "(until resumed)"))
    return 0


def cmd_resume(args) -> int:
    cfg, store = _open(args)
    store.kv_set(OVERRIDE_KEY, None)
    _invalidate(store)
    print("resumed: override cleared")
    return 0


def cmd_breaker(args) -> int:
    from . import breaker
    cfg, store = _open(args)
    if args.action == "reset":
        breaker.reset(store)
        print("breaker reset: claude -p launches allowed; paused runs resume on the next daemon tick")
        return 0
    if args.action == "off":
        until = breaker.lift(store, args.duration or 3600)
        print(f"breaker off until {_clock(until, '%H:%M')}")
        return 0
    if args.action == "trip":
        breaker.trip(store, cfg, args.reason or "tripped by user", force=True)
        print("breaker tripped")
        return 0
    st = breaker.status(store, cfg)
    if args.json:
        _print_json(st)
    elif not st["enabled"]:
        print("breaker: disabled (no storm or surge limits configured)")
    elif st["tripped"]:
        until = f" until {_clock(st['until'], '%H:%M')}" if st["until"] else " until reset"
        print(f"breaker: TRIPPED{until}: {st['reason']}")
    elif st["lifted_until"]:
        print(f"breaker: off until {_clock(st['lifted_until'], '%H:%M')}")
    else:
        print("breaker: observe only (never refuses or pauses)" if st["observe"] else "breaker: armed")
    seen = st.get("last_would_trip")
    if seen and st["observe"]:
        print(f"  last would-trip {_clock(seen['at'], '%m-%d %H:%M')}: {seen['reason']}")
    return 0


def cmd_mock_quota(args) -> int:
    cfg, store = _open(args)
    window = window_name(args.window)
    if not 0 <= args.pct <= 100:
        raise CliError("PCT must be between 0 and 100")
    now = util.now()
    store.add_quota_sample(QuotaSample(ts=now, window=window, used_pct=float(args.pct),
                                       resets_at=now + args.resets_in, source="mock", exact=True))
    _invalidate(store)
    print(f"mock sample: {_label_window(window)} {args.pct:g}% exact, resets in {util.fmt_duration(args.resets_in)}")
    return 0


def cmd_inv_action(args) -> int:
    from . import interventions
    cfg, store = _open(args)
    if store.get_invocation(args.inv_id) is None:
        raise CliError(f"no such invocation: {args.inv_id}")
    interventions.request(store, args.inv_id, args.inv_action)
    print(f"{args.inv_id}: {args.inv_action} requested")
    return 0


# -- parser ---------------------------------------------------------------------------

def _add_server_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--host", default=None, help="bind address (default: dashboard.host, 127.0.0.1)")
    sp.add_argument("--port", type=int, default=None, help="port (default: dashboard.port, 8377)")


def _json_flag(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--json", action="store_true", help="machine-readable output")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cctripwire",
                                description="cctripwire: a circuit breaker for runaway Claude Code agents.")
    p.add_argument("--config", metavar="PATH",
                   help="config file (default: $CCTRIPWIRE_CONFIG or ~/.config/cctripwire/config.toml)")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def cmd(name: str, func, help_text: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text, description=help_text)
        sp.set_defaults(func=func)
        return sp

    su = cmd("setup", cmd_setup, "one-command install: runaway-only config, hooks, status line, shim, daemon")
    su.add_argument("--observe", action="store_true", help="breaker only records would-trips (try first)")
    su.add_argument("--edit-rc", action="store_true", help="add the shim to PATH in ~/.bashrc and ~/.zshrc")
    su.add_argument("--no-shim", action="store_true", help="skip the claude shim")
    su.add_argument("--no-daemon", action="store_true", help="skip the background service")
    su.add_argument("--no-statusline", action="store_true", help="leave the status line alone")
    su.add_argument("--settings", metavar="PATH", help="Claude Code settings (default: ~/.claude/settings.json)")
    su.add_argument("--dry-run", action="store_true", help="say what would change, change nothing")
    dm = cmd("demo", cmd_demo, "watch the breaker stop a 200-run storm, with a stand-in claude (no tokens)")
    dm.add_argument("-n", type=int, default=200, help="parallel launches (default 200)")
    dm.add_argument("--sleep", type=float, default=3.0, help=argparse.SUPPRESS)
    td = cmd("teardown", cmd_teardown, "undo setup (keeps config and state)")
    td.add_argument("--settings", metavar="PATH", help="Claude Code settings (default: ~/.claude/settings.json)")
    _json_flag(cmd("status", cmd_status, "windows, level, running work"))
    w = cmd("watch", cmd_watch, "redraw status every N seconds")
    w.add_argument("-n", "--interval", type=float, default=5.0, metavar="SECS")
    w.add_argument("--iterations", type=int, help=argparse.SUPPRESS)
    _json_flag(cmd("top", cmd_top, "live claude processes: gateway vs bypass"))
    h = cmd("history", cmd_history, "weighted tokens per bucket and quota samples")
    h.add_argument("--hours", type=float, default=24.0)
    h.add_argument("--bucket", type=int, default=3600, metavar="SECS")
    _json_flag(h)
    for name, func, what in (("loops", cmd_loops, "loop detections"),
                             ("interventions", cmd_interventions, "intervention log")):
        sp = cmd(name, func, what)
        sp.add_argument("--since", type=parse_duration, default=86400.0, metavar="DUR", help="look-back, e.g. 24h")
        _json_flag(sp)
    _json_flag(cmd("limits", cmd_limits, "effective rules, levels, budgets, gateway limits"))
    cf = cmd("config", cmd_config, "show, validate or init the config file")
    cf.add_argument("action", choices=["show", "validate", "init"])
    cf.add_argument("--force", action="store_true", help="init: overwrite an existing file")
    ins = cmd("install", cmd_install, "merge cctripwire hooks (and optionally statusLine) into settings.json")
    ins.add_argument("--settings", metavar="PATH", help="settings file (default: ./.claude/settings.json)")
    ins.add_argument("--no-hooks", dest="no_hooks", action="store_true", help="skip the hooks (statusLine only)")
    ins.add_argument("--hooks", action="store_true", help=argparse.SUPPRESS)   # hooks are the default
    ins.add_argument("--statusline", action="store_true", help="also install the statusLine (exact quota source)")
    ins.add_argument("--dry-run", action="store_true", help="print the result, change nothing")
    un = cmd("uninstall", cmd_uninstall, "remove only cctripwire entries from settings.json")
    un.add_argument("--settings", metavar="PATH", help="settings file (default: ./.claude/settings.json)")
    shim = cmd("install-shim", cmd_install_shim, "write a claude shim that sends print-mode calls through the gateway")
    shim.add_argument("--dir", metavar="DIR")
    run = cmd("run", cmd_run, "run claude through the gateway: cctripwire run [opts] -- <claude args>")
    run.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    cmd("hook", cmd_hook, "hook entry point (stdin = Claude Code hook JSON)")
    cmd("statusline", cmd_statusline, "status-line entry point (stdin = Claude Code status JSON)")
    cmd("scan", cmd_scan, "scan transcripts once")
    cmd("tick", cmd_tick, "run one daemon cycle")
    d = cmd("daemon", cmd_daemon, "run the enforcement daemon")
    d.add_argument("--dashboard", action="store_true", help="also serve the dashboard")
    _add_server_args(d)
    _add_server_args(cmd("dashboard", cmd_dashboard, "serve the web dashboard only"))
    ov = cmd("override", cmd_override, "force a level until it expires, or 'clear'")
    ov.add_argument("level", help="ok|warn|throttle|restrict|stop, or 'clear'")
    ov.add_argument("--for", dest="duration", type=parse_duration, metavar="DUR")
    ov.add_argument("--reason")
    ps = cmd("pause", cmd_pause, "shorthand for override stop")
    ps.add_argument("--for", dest="duration", type=parse_duration, metavar="DUR")
    cmd("resume", cmd_resume, "clear any override")
    for name, action in (("suspend", "suspend"), ("cont", "resume"), ("kill", "kill")):
        sp = cmd(name, cmd_inv_action, f"{action} a gateway invocation")
        sp.add_argument("inv_id")
        sp.set_defaults(inv_action=action)
    _json_flag(cmd("checkpoints", cmd_checkpoints, "list checkpoints written at STOP"))
    rc = cmd("resume-checkpoint", cmd_resume_checkpoint, "resume a checkpointed invocation")
    rc.add_argument("inv_id")
    rc.add_argument("--dry-run", action="store_true")
    br = cmd("breaker", cmd_breaker, "runaway breaker: status, reset, off --for DUR, trip")
    br.add_argument("action", nargs="?", default="status", choices=("status", "reset", "off", "trip"))
    br.add_argument("--for", dest="duration", type=parse_duration, metavar="DUR")
    br.add_argument("--reason")
    _json_flag(br)
    mq = cmd("mock-quota", cmd_mock_quota, "insert a mock exact quota reading (testing and demos)")
    mq.add_argument("window", type=window_name, help="5h, 7d or a window name")
    mq.add_argument("pct", type=float)
    mq.add_argument("--resets-in", type=parse_duration, default=7200.0, metavar="DUR")
    cmd("doctor", cmd_doctor, "check configuration, hooks and data freshness")
    return p


def _guarded(fn) -> int:
    try:
        rc = fn()
        return 0 if rc is None else int(rc)
    except SystemExit as e:           # a delegated module (hooks, gateway) chose its own exit
        if isinstance(e.code, int):
            return e.code
        return 0 if e.code is None else 1
    except KeyboardInterrupt:
        return 130
    except CliError as e:
        print(f"cctripwire: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - one line for the user unless debugging
        if os.environ.get("CCTRIPWIRE_DEBUG"):
            raise
        print(f"cctripwire: error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list[:1] == ["run"]:      # gateway passthrough: everything after `run` is the gateway's
        return _guarded(lambda: _gateway_run(args_list[1:]))
    parser = build_parser()
    try:
        args = parser.parse_args(args_list)
    except SystemExit as e:           # argparse already printed the message
        return int(e.code) if isinstance(e.code, int) else 2
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    return _guarded(lambda: func(args))


__all__ = ["main", "build_parser", "cctripwire_command", "render_status", "CliError"]

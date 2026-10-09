"""Gateway for `claude -p`: admission, queueing, budgets and supervision.

`cctripwire run [opts] -- <claude args>` runs the real claude binary as a child in its
own process group. Print-mode invocations (`-p`/`--print`) are admitted against
concurrency, depth, duplicate and policy limits, queued when no slot is free, and
watched for token budgets and suspend/resume/kill requests. Everything else is
exec'd straight through to the real binary.

Concurrency, depth and duplicate limits are enforced here and never depend on the
policy engine or loop detector, which are imported lazily and fail open.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import breaker
from . import config as _config
from . import procs
from . import util
from .models import Level
from .store import Store

ENV_PARENT_PID = "CCTRIPWIRE_PARENT_PID"
ENV_SHIM_DIR = "CCTRIPWIRE_SHIM_DIR"
SHIM_MARKER = "cctripwire-shim"

EXIT_DENIED = 77
EXIT_QUEUE_TIMEOUT = 75
EXIT_BUDGET = 78
EXIT_KILLED = 143
EXIT_NOT_FOUND = 127

PRINT_FLAGS = ("-p", "--print")
RESUME_FLAGS = ("--resume", "-r", "--continue", "-c", "--session-id")
VALUE_OPTS = frozenset({
    "--model", "--output-format", "--input-format", "--session-id", "--resume", "-r",
    "--max-turns", "--max-budget-usd", "--system-prompt", "--append-system-prompt",
    "--system-prompt-file", "--append-system-prompt-file", "--allowedTools", "--allowed-tools",
    "--disallowedTools", "--disallowed-tools", "--tools", "--add-dir", "--permission-mode",
    "--permission-prompt-tool", "--mcp-config", "--settings", "--setting-sources", "--agents",
    "--agent", "--fallback-model", "--betas", "--plugin-dir", "--json-schema",
    "--max-thinking-tokens", "--debug-file",
})


# ---------------------------------------------------------------- shim / resolution

def default_shim_dir() -> Path:
    return Path(os.path.expanduser("~/.local/share/cctripwire/shim"))


def shim_dir() -> Path:
    return Path(os.environ.get(ENV_SHIM_DIR) or default_shim_dir())


def install_shim(dir=None, *, python: str | None = None, entry: str | None = None) -> Path:
    """Write an executable `claude` shim into `dir` that routes through the gateway.

    `entry` is Python source to run instead of `-m cctripwire` (used by tests, where the
    package has no __main__). The shim is `exec <python> ... run --shim -- "$@"`.
    """
    d = Path(os.path.expanduser(str(dir))) if dir else default_shim_dir()
    d.mkdir(parents=True, exist_ok=True)
    py = shlex.quote(python or sys.executable)
    if entry is not None:
        call = f"{py} -c {shlex.quote(entry)}"
    else:
        call = f"{py} -m cctripwire"
    path = d / "claude"
    path.write_text(
        "#!/bin/sh\n"
        f"# {SHIM_MARKER}: routes `claude` through the cctripwire gateway\n"
        f"exec {call} run --shim -- \"$@\"\n"
    )
    path.chmod(0o755)
    return path


def _is_shim(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return SHIM_MARKER.encode() in f.read(512)
    except OSError:
        return False


def resolve_real_claude(cfg=None) -> str:
    """Path of the real claude binary: $CCTRIPWIRE_REAL_CLAUDE, then gateway.real_claude,
    then the first executable `claude` on PATH that is not a cctripwire shim.
    Raises FileNotFoundError when none exists, RuntimeError when the answer is a shim."""
    env = os.environ.get(util.ENV_REAL_CLAUDE)
    if env:
        if _is_shim(Path(env)):
            raise RuntimeError(f"{util.ENV_REAL_CLAUDE} points at a cctripwire shim: {env}")
        return env
    if cfg is not None:
        configured = cfg.get("gateway.real_claude")
        if configured:
            return os.path.expanduser(str(configured))
    sd = shim_dir()
    try:
        sd_real = sd.resolve()
    except OSError:
        sd_real = sd
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        cand = Path(d) / "claude"
        if not (cand.is_file() and os.access(cand, os.X_OK)):
            continue
        try:
            under_shim = cand.resolve().parent == sd_real
        except OSError:
            under_shim = False
        if under_shim or _is_shim(cand):
            continue
        return str(cand)
    raise FileNotFoundError("real claude binary not found on PATH (set CCTRIPWIRE_REAL_CLAUDE)")


# ---------------------------------------------------------------- argument helpers

def _split_args(argv: list[str]) -> tuple[dict, list[str]]:
    """Parse cctripwire options up to `--`. Without `--`, the first unknown token starts claude args."""
    opts: dict = {"wait": None, "project": None, "agent": None, "budget_tokens": None, "shim": False}
    args = list(argv)
    if args and args[0] == "run":
        args = args[1:]
    valued = {"--project": "project", "--agent": "agent", "--budget-tokens": "budget_tokens"}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return opts, args[i + 1:]
        if a == "--shim":
            opts["shim"] = True
        elif a == "--wait":
            opts["wait"] = True
        elif a == "--no-wait":
            opts["wait"] = False
        elif a in valued and i + 1 < len(args):
            opts[valued[a]] = args[i + 1]
            i += 1
        elif "=" in a and a.split("=", 1)[0] in valued:
            k, v = a.split("=", 1)
            opts[valued[k]] = v
        else:
            return opts, args[i:]
        i += 1
    return opts, []


def _opt(args: list[str], name: str) -> str | None:
    for i, a in enumerate(args):
        if a == name:
            return args[i + 1] if i + 1 < len(args) else None
        if a.startswith(name + "="):
            return a[len(name) + 1:]
    return None


def _has(args: list[str], *names: str) -> bool:
    return any(a in names or any(a.startswith(n + "=") for n in names) for a in args)


def _is_print(args: list[str]) -> bool:
    return any(a in PRINT_FLAGS for a in args)


def _positional(args: list[str]) -> str | None:
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


def _num(v) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _int_env(name: str) -> int:
    try:
        return int(os.environ.get(name) or 0)
    except ValueError:
        return 0


def _read_prompt(args: list[str]):
    """Return (prompt, stdin_file). stdin is slurped and re-fed to the child when no positional prompt."""
    pos = _positional(args)
    if pos is not None:
        return pos, None
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return "", None
        data = sys.stdin.buffer.read()
    except (OSError, ValueError):
        return "", None
    tf = tempfile.TemporaryFile()
    tf.write(data)
    tf.seek(0)
    return data.decode("utf-8", "replace"), tf


def _rewrite(args: list[str], cfg, downgrade: str | None) -> tuple[list[str], str | None]:
    """Add session id, turn/budget caps and a model downgrade, unless already present."""
    out = list(args)
    session = _opt(out, "--session-id") or _opt(out, "--resume") or _opt(out, "-r")
    if not _has(out, *RESUME_FLAGS):
        session = str(uuid.uuid4())
        out = ["--session-id", session] + out
    turns = int(cfg.get("gateway.per_invocation_max_turns", 0) or 0)
    if turns > 0 and not _has(out, "--max-turns"):
        out += ["--max-turns", str(turns)]
    usd = float(cfg.get("gateway.per_invocation_budget_usd", 0) or 0)
    if usd > 0 and not _has(out, "--max-budget-usd"):
        out += ["--max-budget-usd", str(usd)]
    if downgrade:
        if _has(out, "--model"):
            fixed, skip = [], False
            for i, a in enumerate(out):
                if skip:
                    skip = False
                    continue
                if a == "--model" and i + 1 < len(out):
                    fixed += ["--model", downgrade]
                    skip = True
                elif a.startswith("--model="):
                    fixed.append(f"--model={downgrade}")
                else:
                    fixed.append(a)
            out = fixed
        else:
            out = ["--model", downgrade] + out
    return out, session


def _child_env(real: str, inv_id: str, root: str, depth: int, project: str, agent: str) -> dict:
    env = dict(os.environ)
    env[util.ENV_INVOCATION] = inv_id
    env[util.ENV_ROOT] = root
    env[util.ENV_DEPTH] = str(depth)
    env[ENV_PARENT_PID] = str(os.getpid())
    env[util.ENV_REAL_CLAUDE] = real
    env[util.ENV_PROJECT] = project
    env[util.ENV_AGENT] = agent
    _prepend_shim_path(env)
    return env


def _prepend_shim_path(env: dict) -> None:
    sd = str(shim_dir())
    if not os.path.isdir(sd):
        return
    path = env.get("PATH", "")
    if not path.split(os.pathsep, 1)[0] == sd:
        env["PATH"] = sd + (os.pathsep + path if path else "")


def _exec(real: str, args: list[str]) -> int:
    env = dict(os.environ)
    env[util.ENV_REAL_CLAUDE] = real
    _prepend_shim_path(env)
    try:
        sys.stdout.flush()
        os.execve(real, [real, *args], env)
    except OSError as e:
        print(f"cctripwire: cannot exec {real}: {e}", file=sys.stderr)
        return EXIT_NOT_FOUND
    return EXIT_NOT_FOUND  # unreachable


# ---------------------------------------------------------------- policy / loop seams

def _log(cfg, msg: str) -> None:
    try:
        p = cfg.state_dir / "gateway-errors.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except Exception:  # noqa: BLE001 - logging must never break a launch
        pass


def _decide(cfg, store, project: str, agent: str):
    """Policy decision, or None when the engine is unavailable (monitoring fails open)."""
    try:
        from . import engine
        return engine.decide(store, cfg, project=project, agent=agent)[1]
    except Exception as e:  # noqa: BLE001
        _log(cfg, f"policy unavailable: {e!r}")
        return None


def _loop_check(cfg, store, phash: str, parent: str | None, depth: int) -> list:
    try:
        from .loops import LoopDetector
        return list(LoopDetector(store, cfg).check_invocation(phash, parent, depth) or [])
    except Exception as e:  # noqa: BLE001
        _log(cfg, f"loop check unavailable: {e!r}")
        return []


# ---------------------------------------------------------------- weighting

def _weigh(model: str | None, usage: dict) -> float:
    try:
        from .quota.estimator import weight
        return float(weight(model, usage))
    except Exception:  # noqa: BLE001 - estimator absent: same formula inline
        pass

    def g(k):
        return float(usage.get(k) or 0)

    base = (g("input_tokens") + 5 * g("output_tokens")
            + 1.25 * g("cache_creation_input_tokens") + 0.1 * g("cache_read_input_tokens"))
    m = (model or "").lower()
    factor = 5.0 if ("opus" in m or "fable" in m) else (1 / 3 if "haiku" in m else 1.0)
    return base * factor


def _usage_line(line: bytes | str) -> tuple[str | None, float, dict | None]:
    """(message_id, weighted, obj) for an assistant usage line; (None, 0, obj) otherwise."""
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None, 0.0, None
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None, 0.0, obj if isinstance(obj, dict) else None
    msg = obj.get("message") or {}
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None, 0.0, obj
    mid = f"{msg.get('id')}:{obj.get('requestId')}"
    return mid, _weigh(msg.get("model"), usage), obj


class _Run:
    """Mutable state shared by the main thread, the stdout pump and the monitor."""

    def __init__(self, fmt: str):
        self.fmt = fmt
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.kill_reason: str | None = None
        self.kill_code: int | None = None
        self.suspended = False
        self.usage: dict[str, float] = {}      # stream-json assistant usage by message id
        self.tx_usage: dict[str, float] = {}   # fallback transcript reader
        self.tx_offsets: dict[str, int] = {}
        self.lines: list[bytes] = []           # json output (single object) for cost parsing
        self.cost: float | None = None

    def kill(self, reason: str, code: int) -> bool:
        with self.lock:
            if self.kill_reason is None:
                self.kill_reason, self.kill_code = reason, code
                return True
            return False

    def stream_weight(self) -> float:
        return sum(self.usage.values())


def _pump(stream, run: _Run) -> None:
    out = sys.stdout.buffer
    for line in iter(stream.readline, b""):
        try:
            out.write(line)
            out.flush()
        except (OSError, ValueError):
            pass
        if run.fmt == "json":
            run.lines.append(line)
        elif run.fmt == "stream-json":
            mid, w, obj = _usage_line(line)
            if mid is not None:
                run.usage[mid] = max(run.usage.get(mid, 0.0), w)
            if obj and obj.get("type") == "result" and "total_cost_usd" in obj:
                run.cost = obj.get("total_cost_usd")
    stream.close()


def _fallback_transcript_weight(cfg, run: _Run, session: str | None) -> float:
    """Local incremental reader used only when the transcripts module is unavailable."""
    if session:
        base = cfg.claude_dir / "projects"
        files = sorted(base.glob(f"*/{session}.jsonl")) if base.is_dir() else []
        if files:
            path = files[0]
            key = str(path)
            try:
                size = path.stat().st_size
                off = run.tx_offsets.get(key, 0)
                if size < off:
                    off = 0
                if size > off:
                    with open(path, "rb") as f:
                        f.seek(off)
                        data = f.read(size - off)
                    end = data.rfind(b"\n")
                    if end >= 0:
                        for line in data[:end].split(b"\n"):
                            mid, w, _ = _usage_line(line)
                            if mid is not None:
                                run.tx_usage[mid] = max(run.tx_usage.get(mid, 0.0), w)
                        run.tx_offsets[key] = off + end + 1
            except OSError:
                pass
    return sum(run.tx_usage.values())


def _transcript_weight(store, cfg, run: _Run, inv_id: str, session: str | None,
                       project: str, agent: str) -> float:
    try:
        from . import transcripts
        if session:
            path = transcripts.find_session_file(cfg, session)
            if path:
                transcripts.scan_file(store, path, project=project, agent=agent, invocation_id=inv_id)
        return float(store.token_totals(0, invocation_id=inv_id).weighted)
    except Exception:  # noqa: BLE001 - absent or failing: local reader keeps budgets working
        return _fallback_transcript_weight(cfg, run, session)


def _weighted_used(store, cfg, run: _Run, inv_id: str, session, project, agent) -> float:
    return max(run.stream_weight(), _transcript_weight(store, cfg, run, inv_id, session, project, agent))


# ---------------------------------------------------------------- admission

def _reap(store) -> list[int]:
    """Mark rows whose gateway died as crashed. Returns their pgids to kill after the transaction."""
    c = store.conn
    now = util.now()
    pgids: list[int] = []
    rows = c.execute("SELECT id, pid, pgid FROM invocations WHERE status IN ('queued','running','suspended')").fetchall()
    for r in rows:
        if r["pid"] == os.getpid() or util.pid_alive(r["pid"]):
            continue
        c.execute("UPDATE invocations SET status='crashed', ended=?, reason=? WHERE id=?",
                  (now, "gateway exited before the invocation finished", r["id"]))
        store.add_intervention("reap", r["id"], "stale gateway: orphaned process group reaped")
        if r["pgid"]:
            pgids.append(int(r["pgid"]))
    return pgids


def _admit(store, cfg, inv_id: str, cap: int, min_interval: float, max_children: int) -> bool:
    """Take a slot for `inv_id` if it fits and no older queued row would take it first.
    Must run inside an immediate transaction."""
    c = store.conn
    now = util.now()
    me = c.execute("SELECT id, parent_id, created FROM invocations WHERE id=?", (inv_id,)).fetchone()
    if me is None:
        return False
    # Count only *active* work: a parent with a queued or live child is blocked waiting on it,
    # so counting it too would let a nested chain deadlock against its own slots.
    running = c.execute(
        "SELECT COUNT(*) FROM invocations i WHERE status IN ('running','suspended') AND NOT EXISTS "
        "(SELECT 1 FROM invocations k WHERE k.parent_id=i.id AND k.status IN ('queued','running','suspended'))"
    ).fetchone()[0]
    last = c.execute("SELECT COALESCE(MAX(started), 0) FROM invocations").fetchone()[0]
    recent = c.execute("SELECT COUNT(*) FROM invocations WHERE started>=?", (now - 60,)).fetchone()[0]
    rate = int(cfg.get("gateway.spawn_rate_per_min", 12))
    global_ok = now - float(last) >= min_interval and recent < rate

    def fits(parent_id: str | None) -> bool:
        if cap <= 0 or running >= cap or not global_ok:
            return False
        if parent_id:
            kids = c.execute("SELECT COUNT(*) FROM invocations WHERE parent_id=? AND status IN ('running','suspended')",
                             (parent_id,)).fetchone()[0]
            if kids >= max_children:
                return False
        return True

    if not fits(me["parent_id"]):
        return False
    older = c.execute(
        "SELECT id, parent_id FROM invocations WHERE status='queued' AND id<>? "
        "AND (created<? OR (created=? AND id<?)) ORDER BY created, id",
        (inv_id, me["created"], me["created"], inv_id)).fetchall()
    for q in older:
        if fits(q["parent_id"]):
            return False
    c.execute("UPDATE invocations SET status='running', started=?, heartbeat=?, pid=? WHERE id=?",
              (now, now, os.getpid(), inv_id))
    return True


def _acquire_slot(store, cfg, inv_id: str, parent: str | None, wait_mode: bool, decide) -> str | None:
    """Queue until admitted. Returns None on admission, 'timeout', 'stop' or 'storm' otherwise."""
    deadline = time.monotonic() + float(cfg.get("gateway.queue_timeout_s", 1800))
    poll = max(0.02, float(cfg.get("gateway.queue_poll_s", 2.0)))
    grace = float(cfg.get("gateway.kill_grace_s", 10))
    cap_cfg = int(cfg.get("gateway.max_concurrency", 4))
    max_children = int(cfg.get("gateway.max_children", 4))
    while True:
        dec = decide()
        cap, min_interval = cap_cfg, 0.0
        if dec is not None:
            if dec.level >= Level.STOP and not wait_mode:
                return "stop"
            if dec.max_concurrency is not None:
                cap = min(cap, int(dec.max_concurrency))
            min_interval = float(dec.min_interval_s or 0.0)
        if breaker.enabled(cfg) and breaker.tripped(store):
            return "storm"
        with store.tx(immediate=True):
            stale = _reap(store)
            admitted = _admit(store, cfg, inv_id, cap, min_interval, max_children)
        for pg in stale:
            procs.terminate_group(pg, grace)
        if admitted:
            return None
        if time.monotonic() >= deadline:
            return "timeout"
        time.sleep(poll)


def _forward_signals(pgid: int) -> dict:
    def handler(signum, frame):
        procs.signal_group(pgid, signum)

    prev: dict = {}
    if threading.current_thread() is threading.main_thread():
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            prev[s] = signal.signal(s, handler)
    return prev


# ---------------------------------------------------------------- print-mode flow

def _run_print(cfg, real: str, opts: dict, args: list[str], prompt: str, stdin_f) -> int:
    t0 = util.now()
    inv_id = uuid.uuid4().hex[:12]
    parent = os.environ.get(util.ENV_INVOCATION) or None
    depth = _int_env(util.ENV_DEPTH) + 1
    root = os.environ.get(util.ENV_ROOT) or inv_id
    cwd = os.getcwd()
    project = opts.get("project") or util.project_name(cwd)
    agent = opts.get("agent") or util.agent_name(cwd, str(cfg.get("general.agent_name") or ""))
    fmt = _opt(args, "--output-format") or "text"
    json_mode = fmt == "json"
    phash = util.short_hash(util.normalize_prompt(prompt))
    model = _opt(args, "--model")
    proj_budget = cfg.budget_for("projects", project)
    budget = (_num(opts.get("budget_tokens")) or _num(proj_budget.get("per_invocation_tokens"))
              or _num(cfg.get("gateway.per_invocation_tokens")))
    wait_mode = opts["wait"] if opts.get("wait") is not None else cfg.get("gateway.on_stop", "deny") == "wait"
    max_depth = int(cfg.get("gateway.max_depth", 2))
    if proj_budget.get("max_depth") is not None:
        max_depth = min(max_depth, int(proj_budget["max_depth"]))

    base = dict(id=inv_id, parent_id=parent, root_id=root, depth=depth, pid=os.getpid(), pgid=None,
                project=project, agent=agent, argv=json.dumps(args)[:1000], prompt_hash=phash,
                model=model, status="queued", created=t0, started=None, ended=None, heartbeat=t0,
                exit_code=None, tokens_in=0, tokens_out=0, weighted=0.0, cost_usd=None,
                budget_weighted=budget or None, session_id=None, reason=None)
    state = {"row": False}
    try:
        store = Store(cfg.db_path)
    except Exception as e:  # noqa: BLE001
        store = None
        store_err = repr(e)

    def refuse(kind: str, code: int, reason: str, advice: str, level, status: str = "denied") -> int:
        text = f"cctripwire: {kind}: {reason}. {advice}"[:300]
        print(text, file=sys.stderr)
        if json_mode:
            obj = {"type": "result", "subtype": "error_cctripwire", "is_error": True, "result": text,
                   "cctripwire": {"reason": reason, "level": str(level)}}
            print(json.dumps(obj))
        sys.stdout.flush()
        if store is not None:
            try:
                fields = dict(status=status, ended=util.now(), reason=reason, exit_code=code)
                if state["row"]:
                    store.update_invocation(inv_id, **fields)
                else:
                    store.create_invocation({**base, **fields})
                    state["row"] = True
                store.add_intervention(kind, inv_id, reason, level=level)
            except Exception as e:  # noqa: BLE001
                _log(cfg, f"could not record refusal: {e!r}")
        return code

    # 1. depth: needs no store, so it holds even if the database is broken
    if depth > max_depth:
        return refuse("denied", EXIT_DENIED,
                      f"recursive claude -p depth {depth} exceeds limit {max_depth}",
                      "Do this step inline instead of spawning another claude -p.", Level.RESTRICT)
    if store is None:
        return refuse("denied", EXIT_DENIED, f"state store unavailable ({store_err})",
                      "Fix the cctripwire database, or set CCTRIPWIRE_DISABLE=1 to bypass.", Level.OK)

    # 2. policy
    dec = _decide(cfg, store, project, agent)
    level = dec.level if dec is not None else Level.OK
    if dec is not None and dec.level >= Level.STOP and not wait_mode:
        return refuse("denied", EXIT_DENIED, f"usage at STOP: {dec.message()}",
                      "Checkpoint your work and stop; retry after the window resets, or use --wait.", level)
    if dec is not None and dec.deny_spawn and depth >= 2:
        return refuse("denied", EXIT_DENIED, f"nested claude -p denied while usage is {level}",
                      "Do this step inline instead of spawning claude -p.", level)

    # 3. loops and duplicates
    dets = _loop_check(cfg, store, phash, parent, depth)
    severe = [d for d in dets if d.severity >= Level.RESTRICT]
    if severe:
        d0 = severe[0]
        return refuse("denied", EXIT_DENIED, f"loop detected: {d0.kind}: {d0.detail}",
                      d0.advice or "Change approach instead of repeating this.", max(d.severity for d in severe))
    max_dup = int(cfg.get("gateway.max_duplicates", 3))
    dup_window = float(cfg.get("gateway.duplicate_window_s", 600))
    if max_dup > 0:
        prior = store.conn.execute(
            "SELECT COUNT(*) FROM invocations WHERE prompt_hash=? AND created>=? AND status<>'denied'",
            (phash, t0 - dup_window)).fetchone()[0]
        if prior >= max_dup:
            return refuse("denied", EXIT_DENIED,
                          f"identical prompt already ran {prior}x in the last {int(dup_window)}s",
                          "Reuse the earlier result instead of re-running this prompt.", level)

    # 4. runaway breaker, checked and recorded atomically so a burst of launches counts exactly
    with store.tx(immediate=True):
        storm, newly = breaker.check(store, cfg, t0)
        if storm is None:
            store.create_invocation(base)
            state["row"] = True
    if storm is not None:
        if newly:
            from . import interventions
            interventions.notify(cfg, "cctripwire: runaway breaker tripped", storm)
        return refuse("runaway breaker", EXIT_DENIED, storm, breaker.ADVICE, Level.STOP)

    # 5. queue for a slot
    outcome = _acquire_slot(store, cfg, inv_id, parent, bool(wait_mode),
                            lambda: _decide(cfg, store, project, agent))
    if outcome == "timeout":
        return refuse("queued-timeout", EXIT_QUEUE_TIMEOUT,
                      f"no slot within {cfg.get('gateway.queue_timeout_s', 1800)}s",
                      "Retry later, or raise gateway.queue_timeout_s.", level, status="timeout")
    if outcome == "stop":
        return refuse("denied", EXIT_DENIED, "usage reached STOP while queued",
                      "Checkpoint your work and stop; retry after the window resets.", level)
    if outcome == "storm":
        rec = breaker.tripped(store) or {}
        return refuse("runaway breaker", EXIT_DENIED, str(rec.get("reason") or "tripped"),
                      breaker.ADVICE, Level.STOP)

    # 6. rewrite arguments
    downgrade = str(cfg.get("gateway.force_model", "") or "") or None
    if dec is not None and dec.downgrade_model:
        downgrade = dec.downgrade_model
    if downgrade and downgrade == model:
        downgrade = None
    child_args, session = _rewrite(args, cfg, downgrade)
    if downgrade:
        store.add_intervention("downgrade", inv_id, f"model forced to {downgrade}", level=level)
        model = downgrade

    # 7. launch
    env = _child_env(real, inv_id, root, depth, project, agent)
    stdout = subprocess.PIPE if fmt in ("json", "stream-json") else None
    try:
        proc = subprocess.Popen([real, *child_args], env=env, stdin=stdin_f, stdout=stdout,
                                start_new_session=True)
    except OSError as e:
        print(f"cctripwire: cannot start {real}: {e}", file=sys.stderr)
        store.update_invocation(inv_id, status="failed", ended=util.now(), exit_code=EXIT_NOT_FOUND,
                                reason=str(e))
        return EXIT_NOT_FOUND
    pgid = proc.pid
    store.update_invocation(inv_id, pgid=pgid, session_id=session, model=model)

    run = _Run(fmt)
    hb = max(0.05, float(cfg.get("gateway.heartbeat_s", 5)))
    grace = float(cfg.get("gateway.kill_grace_s", 10))

    def monitor() -> None:
        while not run.stop.wait(hb):
            try:
                store.update_invocation(inv_id, heartbeat=util.now())
                st = (store.get_invocation(inv_id) or {}).get("status")
                if st == "suspend_requested" and not run.suspended:
                    procs.suspend(pgid)
                    run.suspended = True
                    store.update_invocation(inv_id, status="suspended")
                elif st == "resume_requested":
                    procs.resume(pgid)
                    run.suspended = False
                    store.update_invocation(inv_id, status="running")
                elif st == "kill_requested":
                    if run.kill("stopped on request", EXIT_KILLED):
                        store.add_intervention("kill", inv_id, "stopped on request", level=level)
                        procs.terminate_group(pgid, grace)
                if budget > 0 and run.kill_reason is None:
                    w = _weighted_used(store, cfg, run, inv_id, session, project, agent)
                    if w > budget:
                        msg = (f"per-invocation budget: {util.fmt_tokens(w)} weighted tokens "
                               f"> {util.fmt_tokens(budget)}")
                        if run.kill(msg, EXIT_BUDGET):
                            store.add_intervention("kill", inv_id, msg, level=Level.THROTTLE)
                            procs.terminate_group(pgid, grace)
            except Exception as e:  # noqa: BLE001 - monitoring fails open
                _log(cfg, f"monitor {inv_id}: {e!r}")

    pump = None
    if stdout is not None:
        pump = threading.Thread(target=_pump, args=(proc.stdout, run), daemon=True)
        pump.start()
    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    prev = _forward_signals(pgid)
    try:
        rc = proc.wait()
    finally:
        for s, h in prev.items():
            signal.signal(s, h)
        run.stop.set()
        mon.join(timeout=grace + 5)
        if pump is not None:
            pump.join(timeout=5)
    if procs.group_alive(pgid):
        procs.terminate_group(pgid, grace)   # leftovers the child left behind

    if rc < 0:
        rc = 128 - rc
    if run.kill_reason is not None:
        status, code = "killed", int(run.kill_code or EXIT_KILLED)
        print(f"cctripwire: killed: {run.kill_reason}", file=sys.stderr)
    else:
        status, code = ("done" if rc == 0 else "failed"), rc

    fields = dict(status=status, ended=util.now(), exit_code=code, heartbeat=util.now(),
                  reason=run.kill_reason)
    try:
        w = _weighted_used(store, cfg, run, inv_id, session, project, agent)
        totals = store.token_totals(0, invocation_id=inv_id)
        fields.update(weighted=w, tokens_in=totals.input, tokens_out=totals.output)
        cost = run.cost
        if json_mode and run.lines:
            try:
                last = json.loads(run.lines[-1])
                cost = last.get("total_cost_usd", cost)
            except (ValueError, AttributeError):
                pass
        fields["cost_usd"] = cost
        store.update_invocation(inv_id, **fields)
    except Exception as e:  # noqa: BLE001
        _log(cfg, f"final accounting {inv_id}: {e!r}")
    return code


def _run_print_guarded(cfg, real, opts, args, prompt, stdin_f) -> int:
    try:
        return _run_print(cfg, real, opts, args, prompt, stdin_f)
    except KeyboardInterrupt:
        return 130


# ---------------------------------------------------------------- entry point

def main(argv: list[str] | None = None) -> int:
    """`cctripwire run [--project P] [--agent A] [--budget-tokens N] [--wait|--no-wait] [--shim] -- <claude args>`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    opts, args = _split_args(argv)
    try:
        cfg = _config.load()
    except Exception as e:  # noqa: BLE001 - a broken config must not stop claude
        print(f"cctripwire: config unreadable, using defaults: {e}", file=sys.stderr)
        cfg = _config.Config({})
    try:
        real = resolve_real_claude(cfg)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"cctripwire: {e}", file=sys.stderr)
        return EXIT_NOT_FOUND
    if util.disabled() or not _is_print(args):
        return _exec(real, args)
    prompt, stdin_f = _read_prompt(args)
    try:
        return _run_print_guarded(cfg, real, opts, args, prompt, stdin_f)
    finally:
        if stdin_f is not None:
            stdin_f.close()


__all__ = ["main", "install_shim", "resolve_real_claude", "default_shim_dir", "shim_dir"]

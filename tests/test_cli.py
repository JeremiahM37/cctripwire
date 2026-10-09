"""CLI tests: subcommands run through cli.main(argv) with stdout captured.

Sibling workers' modules are imported lazily by the CLI. Anything not importable
yet is replaced by a small stub for the duration of one test (`_provide`), so these
tests pass whether or not those modules exist. Nothing here touches the network,
the real `claude` binary, or ~/.claude: every path is under tmp_path.
"""
from __future__ import annotations

import importlib
import io
import json
import sys
import types
from types import SimpleNamespace

import pytest

from cctripwire import cli, util
from cctripwire.config import Config
from cctripwire.models import Detection, Level, QuotaSample, Snapshot, WindowState
from cctripwire.store import Store


# -- fixtures and stubs -------------------------------------------------------------

def _stub_build_snapshot(store, cfg, now=None):
    """Minimal stand-in for forecast.build_snapshot: latest reading per window."""
    t = util.now() if now is None else now
    snap = Snapshot(ts=t)
    for w in store.windows_seen():
        q = store.latest_quota(w)
        if q is not None:
            snap.windows[w] = WindowState(window=w, used_pct=q.used_pct, resets_at=q.resets_at, exact=q.exact,
                                          source=q.source, age_s=t - q.ts, burn_pct_per_hour=None,
                                          eta_s=None, elapsed_frac=None, pace_ratio=None)
    return snap


def _ensure_forecast(monkeypatch) -> None:
    try:
        importlib.import_module("cctripwire.forecast")
        return
    except ImportError:
        pass
    stub = types.ModuleType("cctripwire.forecast")
    stub.build_snapshot = _stub_build_snapshot
    monkeypatch.setitem(sys.modules, "cctripwire.forecast", stub)
    monkeypatch.setattr(sys.modules["cctripwire"], "forecast", stub, raising=False)


def _provide(monkeypatch, dotted: str, **attrs):
    """Return cctripwire.<dotted>, stubbing the module if it is not importable yet."""
    full = f"cctripwire.{dotted}"
    try:
        mod = importlib.import_module(full)
    except ImportError:
        mod = types.ModuleType(full)
        parent_name, leaf = full.rsplit(".", 1)
        monkeypatch.setitem(sys.modules, full, mod)
        monkeypatch.setattr(importlib.import_module(parent_name), leaf, mod, raising=False)
    for k, v in attrs.items():
        monkeypatch.setattr(mod, k, v, raising=False)
    return mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CCTRIPWIRE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CCTRIPWIRE_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    for k in ("CCTRIPWIRE_TIME_OFFSET", "CCTRIPWIRE_DISABLE", "CCTRIPWIRE_PROJECT", "CCTRIPWIRE_AGENT",
              "CCTRIPWIRE_INVOCATION_ID", "NO_COLOR"):
        monkeypatch.delenv(k, raising=False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    _ensure_forecast(monkeypatch)
    return tmp_path


def _db(env) -> Store:
    return Store(env / "state" / "tripwire.db")


def _run(capsys, argv):
    capsys.readouterr()
    rc = cli.main(argv)
    out, err = capsys.readouterr()
    return rc, out, err


def _status_json(capsys, argv=("status", "--json")):
    rc, out, _ = _run(capsys, list(argv))
    assert rc == 0, out
    return json.loads(out)


# -- argument types --------------------------------------------------------------------

@pytest.mark.parametrize("text,seconds", [("90", 90.0), ("30m", 1800.0), ("2h", 7200.0),
                                          ("1h30m", 5400.0), ("1d", 86400.0)])
def test_parse_duration_accepts(text, seconds):
    assert cli.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "soon", "2 hours", "5x"])
def test_parse_duration_rejects(text):
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_duration(text)


def test_no_command_prints_help(env, capsys):
    rc, out, _ = _run(capsys, [])
    assert rc == 2
    assert "usage: cctripwire" in out


def test_bad_override_level_is_rejected(env, capsys):
    rc, _, err = _run(capsys, ["override", "loud"])
    assert rc == 1
    assert "unknown level" in err


# -- status ---------------------------------------------------------------------------

def test_status_empty_db_is_unknown_not_zero(env, capsys):
    p = _status_json(capsys)
    windows = {w["window"]: w for w in p["windows"]}
    assert list(windows)[:2] == ["five_hour", "seven_day"]
    for name in ("five_hour", "seven_day"):
        assert windows[name]["used_pct"] is None
        assert windows[name]["quality"] == "unknown"
    assert p["level"] == "ok"

    rc, out, _ = _run(capsys, ["status"])
    assert rc == 0
    assert "unknown" in out and "5h" in out


def test_mock_quota_is_exact_in_status(env, capsys):
    rc, out, _ = _run(capsys, ["mock-quota", "5h", "42", "--resets-in", "2h"])
    assert rc == 0 and "42" in out
    w = {x["window"]: x for x in _status_json(capsys)["windows"]}["five_hour"]
    assert w["used_pct"] == pytest.approx(42.0)
    assert w["exact"] is True
    assert w["quality"] == "exact"
    assert w["source"] == "mock"


def test_mock_quota_alias_7d(env, capsys):
    assert _run(capsys, ["mock-quota", "7d", "10"])[0] == 0
    w = {x["window"]: x for x in _status_json(capsys)["windows"]}["seven_day"]
    assert w["used_pct"] == pytest.approx(10.0)


def test_mock_quota_rejects_out_of_range(env, capsys):
    rc, _, err = _run(capsys, ["mock-quota", "5h", "140"])
    assert rc == 1
    assert "between 0 and 100" in err


def test_render_marks_estimate_and_unknown(env):
    base = {"level": "warn", "ts": 1000.0, "reasons": ["early-burn: half used"], "override": None,
            "tokens_per_min": 0.0, "tokens_last_hour": {"weighted": 0.0, "messages": 0},
            "running_invocations": 0, "active_sessions": 1, "by_project": {}, "by_agent": {}}
    est = {"window": "five_hour", "used_pct": 30.0, "exact": False, "source": "estimate", "age_s": 60.0,
           "burn_pct_per_hour": 4.0, "eta_s": None, "resets_at": 1000.0 + 3600, "stale": False}
    unk = {"window": "seven_day", "used_pct": None, "exact": False, "source": "none", "age_s": None,
           "burn_pct_per_hour": None, "eta_s": None, "resets_at": None, "stale": False}
    text = cli.render_status({**base, "windows": [est, unk]})
    assert "~30%" in text and "estimate" in text
    assert "?" in text and "unknown" in text
    assert "early-burn" in text


def test_status_override_pause_resume_and_expiry(env, capsys, monkeypatch):
    assert _run(capsys, ["pause", "--for", "30m"])[0] == 0
    p = _status_json(capsys)
    assert p["level"] == "stop"
    assert p["override"]["level"] == "stop"

    assert _run(capsys, ["resume"])[0] == 0
    assert _status_json(capsys)["override"] is None

    assert _run(capsys, ["override", "warn", "--for", "10m", "--reason", "testing"])[0] == 0
    p = _status_json(capsys)
    assert p["level"] == "warn" and p["override"]["reason"] == "testing"

    # Ten minutes later the override has expired and no longer applies.
    monkeypatch.setenv("CCTRIPWIRE_TIME_OFFSET", str(3600))
    p = _status_json(capsys)
    assert p["override"] is None
    assert p["level"] == "ok"


def test_override_clear(env, capsys):
    _run(capsys, ["override", "throttle"])
    assert _status_json(capsys)["level"] == "throttle"
    rc, out, _ = _run(capsys, ["override", "clear"])
    assert rc == 0 and "cleared" in out
    assert _status_json(capsys)["level"] == "ok"


def test_watch_iterations(env, capsys):
    rc, out, _ = _run(capsys, ["watch", "-n", "0.01", "--iterations", "2"])
    assert rc == 0
    assert out.count("every 0.01s") == 2


# -- history / loops / interventions / limits -------------------------------------------

def test_history_json_sums_token_events(env, capsys):
    db = _db(env)
    db.add_token_event(ts=util.now() - 120, session_id="s1", project="p", agent="a", model="sonnet",
                       input=10, output=20, cache_create=0, cache_read=0, weighted=50.0, msg_key="m1")
    db.add_token_event(ts=util.now() - 90, session_id="s1", project="p", agent="a", model="sonnet",
                       input=1, output=1, cache_create=0, cache_read=0, weighted=25.0, msg_key="m2")
    rc, out, _ = _run(capsys, ["history", "--hours", "1", "--bucket", "3600", "--json"])
    assert rc == 0
    h = json.loads(out)
    assert sum(b["weighted"] for b in h["buckets"]) == pytest.approx(75.0)
    assert set(h["samples"]) == {"five_hour", "seven_day"}


def test_history_text_empty(env, capsys):
    rc, out, _ = _run(capsys, ["history"])
    assert rc == 0
    assert "no token activity" in out


def test_loops_empty_and_populated(env, capsys):
    rc, out, _ = _run(capsys, ["loops"])
    assert rc == 0 and "no detections" in out

    _db(env).add_detection(Detection(kind="identical_prompt", severity=Level.THROTTLE,
                                     session_id="abcdef123456", detail="same prompt 4x",
                                     advice="Stop repeating the same prompt."))
    rc, out, _ = _run(capsys, ["loops", "--since", "24h", "--json"])
    rows = json.loads(out)
    assert len(rows) == 1
    assert rows[0]["kind"] == "identical_prompt"
    assert rows[0]["severity"] == "throttle"


def test_interventions_populated(env, capsys):
    _db(env).add_intervention("deny", "inv1", "recursive claude -p depth 3 exceeds limit 2", level=Level.STOP)
    rc, out, _ = _run(capsys, ["interventions", "--json"])
    rows = json.loads(out)
    assert rows[0]["action"] == "deny"
    assert rows[0]["level"] == "stop"


def test_limits_json(env, capsys):
    rc, out, _ = _run(capsys, ["limits", "--json"])
    assert rc == 0
    p = json.loads(out)
    assert p["gateway"]["max_concurrency"] == 4
    assert any(r["name"] == "critical-5h" for r in p["rules"])
    assert p["problems"] == []


# -- config ---------------------------------------------------------------------------

def test_config_init_validate_show(env, capsys, monkeypatch, tmp_path):
    target = tmp_path / "conf" / "config.toml"
    monkeypatch.setenv("CCTRIPWIRE_CONFIG", str(target))
    assert _run(capsys, ["config", "init"])[0] == 0
    assert target.exists()
    assert _run(capsys, ["config", "init"])[0] == 1          # refuses to overwrite
    assert _run(capsys, ["config", "init", "--force"])[0] == 0
    assert _run(capsys, ["config", "validate"])[0] == 0
    rc, out, _ = _run(capsys, ["config", "show"])
    assert rc == 0
    assert "gateway" in json.loads(out)


def test_config_validate_reports_problems(env, capsys, monkeypatch, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('[[rules]]\nname = "x"\nwhen = "1 +"\nlevel = "loud"\n', encoding="utf-8")
    monkeypatch.setenv("CCTRIPWIRE_CONFIG", str(bad))
    rc, out, _ = _run(capsys, ["config", "validate"])
    assert rc == 1
    assert "unknown level" in out or "bad expression" in out


# -- doctor -----------------------------------------------------------------------------

def test_doctor_on_empty_environment(env, capsys):
    rc, out, err = _run(capsys, ["doctor"])
    assert rc in (0, 1)
    assert "Traceback" not in out + err
    for name in ("config", "database", "hooks", "statusLine", "statusline data", "daemon"):
        assert name in out
    assert "not installed" in out


# -- install / uninstall -------------------------------------------------------------------

def _cctripwire_cmds(settings: dict) -> list[str]:
    out = []
    for groups in settings.get("hooks", {}).values():
        for g in groups:
            for h in g.get("hooks", []):
                if cli.is_cctripwire_command(h.get("command")):
                    out.append(h["command"])
    return out


@pytest.mark.parametrize("cmd,expected", [
    ("/usr/local/bin/cctripwire hook", True),
    ("/usr/bin/python3 -m cctripwire statusline", True),
    ("echo hello", False),
    ("/usr/bin/other-tool --hook", False),
])
def test_is_cctripwire_command(cmd, expected):
    assert cli.is_cctripwire_command(cmd) is expected


def test_cctripwire_command_forms(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/opt/bin/cctripwire")
    assert cli.cctripwire_command("hook") == "/opt/bin/cctripwire hook"
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    cmd = cli.cctripwire_command("statusline")
    assert cmd.endswith(" -m cctripwire statusline")
    assert cmd.startswith(cli.shlex.quote(sys.executable))


def test_install_uninstall_roundtrip(env, capsys):
    settings_path = env / "work" / "settings.json"
    foreign = {
        "model": "opus",
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo foreign"}]}]},
    }
    settings_path.write_text(json.dumps(foreign), encoding="utf-8")

    rc, out, _ = _run(capsys, ["install", "--settings", str(settings_path)])
    assert rc == 0, out
    data = json.loads(settings_path.read_text())
    assert data["model"] == "opus"
    assert set(cli.HOOK_EVENTS) <= set(data["hooks"])
    assert len(_cctripwire_cmds(data)) == len(cli.HOOK_EVENTS)
    assert all(cli.is_cctripwire_command(c) and c.endswith(" hook") for c in _cctripwire_cmds(data))
    pre = data["hooks"]["PreToolUse"]
    assert pre[0]["matcher"] == "Bash"                       # foreign group kept, first
    assert pre[1]["matcher"] == "*"
    assert pre[1]["hooks"][0]["timeout"] == cli.HOOK_TIMEOUT_S
    assert "matcher" not in data["hooks"]["SessionStart"][0]
    assert "statusLine" not in data
    backups = list(settings_path.parent.glob("settings.json.cctripwire-bak-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == foreign

    # Idempotent: a second run changes nothing and makes no new backup.
    text_after_first = settings_path.read_text()
    rc, out, _ = _run(capsys, ["install", "--settings", str(settings_path)])
    assert rc == 0 and "up to date" in out
    assert settings_path.read_text() == text_after_first
    assert len(list(settings_path.parent.glob("settings.json.cctripwire-bak-*"))) == 1

    # statusLine is opt-in and idempotent too.
    assert _run(capsys, ["install", "--settings", str(settings_path), "--statusline"])[0] == 0
    data = json.loads(settings_path.read_text())
    assert data["statusLine"]["type"] == "command"
    assert cli.is_cctripwire_command(data["statusLine"]["command"])
    assert data["statusLine"]["command"].endswith(" statusline")
    assert len(_cctripwire_cmds(data)) == len(cli.HOOK_EVENTS)

    rc, out, _ = _run(capsys, ["uninstall", "--settings", str(settings_path)])
    assert rc == 0
    data = json.loads(settings_path.read_text())
    assert data["model"] == "opus"
    assert "statusLine" not in data
    assert _cctripwire_cmds(data) == []
    assert data["hooks"] == {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo foreign"}]}]}


def test_install_default_is_project_scope(env, capsys):
    work = env / "work"
    rc, out, _ = _run(capsys, ["install", "--dry-run"])
    assert rc == 0
    assert '"hooks"' in out
    assert not (work / ".claude" / "settings.json").exists()

    rc, out, _ = _run(capsys, ["install"])
    assert rc == 0
    assert (work / ".claude" / "settings.json").exists()
    assert not (env / "claude" / "settings.json").exists()


def test_install_dry_run_changes_nothing(env, capsys):
    target = env / "dry" / "settings.json"
    rc, out, _ = _run(capsys, ["install", "--settings", str(target), "--statusline", "--dry-run"])
    assert rc == 0
    assert "statusLine" in out
    assert not target.exists()


def test_install_chains_displaced_statusline(env, capsys):
    target = env / "work" / "settings.json"
    target.write_text(json.dumps({"statusLine": {"type": "command", "command": "/usr/local/bin/my-line"}}))
    rc, out, _ = _run(capsys, ["install", "--settings", str(target), "--statusline"])
    assert rc == 0
    assert 'statusline_chain = "/usr/local/bin/my-line"' in out
    assert cli.is_cctripwire_command(json.loads(target.read_text())["statusLine"]["command"])


def test_install_refuses_invalid_json(env, capsys):
    target = env / "work" / "settings.json"
    target.write_text("{not json", encoding="utf-8")
    rc, _, err = _run(capsys, ["install", "--settings", str(target)])
    assert rc == 1
    assert "not modified" in err
    assert target.read_text() == "{not json"


def test_uninstall_missing_file_is_noop(env, capsys):
    rc, out, _ = _run(capsys, ["uninstall", "--settings", str(env / "nope.json")])
    assert rc == 0 and "does not exist" in out


# -- dispatch to sibling modules (stubbed when absent) -----------------------------------

def test_hook_dispatches_to_hooks_main(env, capsys, monkeypatch):
    calls = []
    _provide(monkeypatch, "hooks", main=lambda: calls.append(1))
    rc, out, _ = _run(capsys, ["hook"])
    assert rc == 0 and calls == [1] and out == ""


def test_statusline_prints_handle_result(env, capsys, monkeypatch):
    seen = {}

    def handle(text, store, cfg, now=None):
        seen["text"] = text
        return "5h 42% OK"

    _provide(monkeypatch, "quota.statusline", handle=handle)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"rate_limits": {}}'))
    rc, out, _ = _run(capsys, ["statusline"])
    assert rc == 0
    assert out == "5h 42% OK\n"
    assert seen["text"] == '{"rate_limits": {}}'


def test_statusline_never_fails(env, capsys, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("corrupt input")

    _provide(monkeypatch, "quota.statusline", handle=boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO("garbage"))
    rc, out, _ = _run(capsys, ["statusline"])
    assert rc == 0
    assert out == "cctripwire\n"


def test_scan_reports_count(env, capsys, monkeypatch):
    _provide(monkeypatch, "quota.transcripts", scan_all=lambda store, cfg, **kw: 7)
    rc, out, _ = _run(capsys, ["scan"])
    assert rc == 0
    assert "7 new token events" in out


def test_tick_runs_one_daemon_cycle(env, capsys, monkeypatch):
    calls = []
    _provide(monkeypatch, "daemon", run=lambda cfg, once=False, **kw: calls.append(once))
    rc, _, _ = _run(capsys, ["tick"])
    assert rc == 0
    assert calls == [True]


def test_run_passes_everything_after_run_to_gateway(env, capsys, monkeypatch):
    seen = {}

    def gw_main(argv):
        seen["argv"] = argv
        return 3

    _provide(monkeypatch, "gateway", main=gw_main)
    rc, _, _ = _run(capsys, ["run", "--project", "x", "--", "claude", "-p", "hi"])
    assert rc == 3
    assert seen["argv"] == ["--project", "x", "--", "claude", "-p", "hi"]


def test_install_shim_dispatch(env, capsys, monkeypatch, tmp_path):
    seen = {}

    def install_shim(path=None):
        seen["path"] = path
        return tmp_path / "shim" / "claude"

    _provide(monkeypatch, "gateway", install_shim=install_shim)
    rc, out, _ = _run(capsys, ["install-shim", "--dir", str(tmp_path / "shim")])
    assert rc == 0
    assert seen["path"] == tmp_path / "shim"
    assert "shim installed" in out


def test_suspend_and_cont_request_interventions(env, capsys, monkeypatch):
    _db(env).create_invocation({"id": "inv1", "status": "running", "created": util.now()})
    calls = []
    _provide(monkeypatch, "interventions", request=lambda store, inv_id, action: calls.append((inv_id, action)))
    assert _run(capsys, ["cont", "inv1"])[0] == 0
    assert _run(capsys, ["suspend", "inv1"])[0] == 0
    assert calls == [("inv1", "resume"), ("inv1", "suspend")]
    rc, _, err = _run(capsys, ["kill", "nope"])
    assert rc == 1 and "no such invocation" in err


def test_checkpoints_and_resume_dry_run(env, capsys, monkeypatch):
    _provide(monkeypatch, "interventions",
             list_checkpoints=lambda cfg: [{"inv_id": "i1", "reason": "stop", "created": None}],
             resume_checkpoint=lambda cfg, inv_id, dry_run=False: ["cctripwire", "run", "--", "claude", "-p",
                                                                   "--resume", "sess-1"])
    rc, out, _ = _run(capsys, ["checkpoints"])
    assert rc == 0 and "i1" in out
    rc, out, _ = _run(capsys, ["resume-checkpoint", "i1", "--dry-run"])
    assert rc == 0 and "--resume sess-1" in out


def test_top_separates_gateway_from_bypass(env, capsys, monkeypatch):
    procs_list = [
        SimpleNamespace(pid=100, ppid=1, pgid=100, cmdline=["claude", "-p", "rogue"], rss_kb=2048,
                        cpu=1.0, invocation_id=None, is_print=True),
        SimpleNamespace(pid=101, ppid=100, pgid=101, cmdline=["claude"], rss_kb=1024,
                        cpu=0.0, invocation_id="inv9", is_print=False),
    ]
    _provide(monkeypatch, "procs",
             list_claude_processes=lambda: procs_list,
             bypassers=lambda ps: [p for p in ps if p.is_print and not p.invocation_id])
    rc, out, _ = _run(capsys, ["top"])
    assert rc == 0
    assert "bypass: 1" in out
    assert "BYPASS" in out
    assert "gateway" in out
    rc, out, _ = _run(capsys, ["top", "--json"])
    rows = json.loads(out)["processes"]
    assert {r["pid"]: r["bypass"] for r in rows} == {100: True, 101: False}


def test_daemon_tick_and_dashboard_help_do_not_need_store_writes(env, capsys):
    rc, out, _ = _run(capsys, ["dashboard", "--help"])
    assert rc == 0
    assert "--port" in out


def test_install_statusline_also_installs_hooks_unless_no_hooks(tmp_path, monkeypatch):
    import json as _json
    from cctripwire import cli
    monkeypatch.setenv("CCTRIPWIRE_HOME", str(tmp_path / "state"))
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    assert cli.main(["install", "--settings", str(a), "--statusline"]) == 0
    da = _json.loads(a.read_text())
    assert "statusLine" in da and "PreToolUse" in da["hooks"]
    assert cli.main(["install", "--settings", str(b), "--statusline", "--no-hooks"]) == 0
    db = _json.loads(b.read_text())
    assert "statusLine" in db and not db.get("hooks")

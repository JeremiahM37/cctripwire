"""Tests for interventions.py: text formatting, checkpoints, requests, notify."""
from __future__ import annotations

import json
import sys

import pytest

from cctripwire import interventions as iv
from cctripwire.config import Config
from cctripwire.models import Decision, Detection, Level, Snapshot, WindowState
from cctripwire.store import Store


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("CCTRIPWIRE_HOME", str(state))
    cfg = Config({"general": {"state_dir": str(state), "claude_dir": str(tmp_path / "claude")}})
    store = Store(cfg.db_path)
    yield store, cfg
    store.close()


def _ws(name, used, exact, burn=None, eta=None):
    return WindowState(window=name, used_pct=used, resets_at=None, exact=exact, source="mock",
                       age_s=10.0, burn_pct_per_hour=burn, eta_s=eta, elapsed_frac=0.4, pace_ratio=1.0)


def _add_inv(store, inv_id, status="running", created=1.0, pid=None, pgid=None, session_id=None):
    store.create_invocation({
        "id": inv_id, "status": status, "created": created, "pid": pid, "pgid": pgid,
        "session_id": session_id, "argv": json.dumps(["claude", "-p", "hi"]), "project": "proj",
        "agent": "agent", "depth": 1, "root_id": inv_id, "prompt_hash": "h" + inv_id,
        "tokens_in": 10, "tokens_out": 20, "weighted": 110.0,
    })


def test_status_line_exact_estimate_unknown():
    snap = Snapshot(ts=0.0, windows={
        "five_hour": _ws("five_hour", 42.0, True, burn=9.0, eta=2 * 3600 + 600),
        "seven_day": _ws("seven_day", 31.0, False),
    })
    dec = Decision(level=Level.WARN)
    assert iv.status_line(snap, dec) == (
        "5h 42% (exact) ↗9%/h ETA 2h10m · 7d ~31% · level WARN")

    # Estimate-only five-hour window with no burn or ETA, and no seven-day data at all.
    snap2 = Snapshot(ts=0.0, windows={"five_hour": _ws("five_hour", 12.4, False)})
    assert iv.status_line(snap2, None) == "5h ~12% · 7d ?"

    # Unknown used_pct is never shown as 0%.
    snap3 = Snapshot(ts=0.0, windows={"five_hour": _ws("five_hour", None, False)})
    assert iv.status_line(snap3, Decision(level=Level.OK)) == "5h ? · 7d ? · level OK"

    assert iv.status_line(None, None) == "5h ? · 7d ?"


def test_guidance_per_level(env):
    _, cfg = env
    assert iv.guidance(Decision(level=Level.OK), cfg) == ""
    assert "cheaper steps" in iv.guidance(Decision(level=Level.WARN), cfg)
    assert "at most 1 subagent" in iv.guidance(Decision(level=Level.THROTTLE, max_concurrency=1), cfg)
    restrict = iv.guidance(Decision(level=Level.RESTRICT, deny_tools=["Agent", "WebSearch"]), cfg)
    assert "Agent, WebSearch" in restrict
    stop = iv.guidance(Decision(level=Level.STOP), cfg)
    assert "CHECKPOINT.md" in stop


def test_agent_message_contains_top_advice_and_fits_limit(env):
    _, cfg = env
    advice_top = "This exact Bash call failed 4×: boom. Change approach instead of retrying."
    dets = [
        Detection(kind="token_spike", severity=Level.WARN, session_id="s", detail="", advice="minor", count=1),
        Detection(kind="repeated_tool_failure", severity=Level.THROTTLE, session_id="s", detail="",
                  advice=advice_top, count=4),
    ]
    dec = Decision(level=Level.THROTTLE, reasons=["5-hour window at 85%"], max_concurrency=1)
    msg = iv.agent_message(dec, dets, cfg)
    assert len(msg) <= 300
    assert advice_top in msg
    assert msg.startswith("cctripwire: THROTTLE: 5-hour window at 85%.")


def test_agent_message_keeps_advice_when_reasons_are_huge(env):
    _, cfg = env
    advice_top = "Stop retrying the same command; change the approach."
    det = Detection(kind="excessive_retries", severity=Level.THROTTLE, session_id=None, detail="",
                    advice=advice_top, count=7)
    dec = Decision(level=Level.THROTTLE, reasons=["x" * 180, "y" * 180, "z" * 180], max_concurrency=1)
    msg = iv.agent_message(dec, [det], cfg)
    assert len(msg) <= 300
    assert advice_top in msg
    assert "…" in msg          # reasons were shortened, not the advice


def test_agent_message_empty_at_ok_without_detections(env):
    _, cfg = env
    assert iv.agent_message(Decision(level=Level.OK), [], cfg) == ""


def test_checkpoint_write_list_and_resume_dry_run(env, tmp_path):
    store, cfg = env
    inv = {"id": "inv1", "argv": json.dumps(["claude", "-p", "do it"]), "cwd": str(tmp_path),
           "project": "proj", "agent": "agent", "session_id": "sess-1", "status": "running",
           "tokens_in": 5, "tokens_out": 7, "weighted": 40.0}
    path = iv.write_checkpoint(cfg, inv, "level STOP: test")
    assert path == cfg.state_dir / "checkpoints" / "inv1.json"
    data = json.loads(path.read_text())
    for key in ("argv", "cwd", "project", "agent", "session_id", "tokens", "reason", "created", "resume_cmd"):
        assert key in data
    assert data["argv"] == ["claude", "-p", "do it"]
    assert data["tokens"]["weighted"] == 40.0
    assert "--resume sess-1" in data["resume_cmd"]

    listed = iv.list_checkpoints(cfg)
    assert [c["id"] for c in listed] == ["inv1"]

    argv = iv.resume_checkpoint(cfg, "inv1", dry_run=True)
    assert argv[:2] == ["cctripwire", "run"]
    assert argv[argv.index("--resume") + 1] == "sess-1"
    assert argv[-1] == iv.RESUME_PROMPT
    # A dry run must not mark the checkpoint as resumed.
    assert "resumed" not in json.loads(path.read_text())


def test_checkpoint_without_session_cannot_resume(env):
    _, cfg = env
    path = iv.write_checkpoint(cfg, {"id": "inv2", "argv": [], "session_id": None}, "stop")
    assert "resume_cmd" not in json.loads(path.read_text())
    with pytest.raises(ValueError):
        iv.resume_checkpoint(cfg, "inv2", dry_run=True)


def test_checkpoint_rejects_path_traversal(env):
    _, cfg = env
    with pytest.raises(ValueError):
        iv.write_checkpoint(cfg, {"id": "../evil", "argv": []}, "x")


def test_request_transitions_and_guards(env):
    store, _ = env
    _add_inv(store, "a", status="running")
    _add_inv(store, "b", status="suspended")

    assert iv.request(store, "a", "suspend", reason="test") is True
    assert store.get_invocation("a")["status"] == "suspend_requested"
    # Suspending again from suspend_requested is refused, not silently re-applied.
    assert iv.request(store, "a", "suspend") is False

    assert iv.request(store, "b", "resume") is True
    assert store.get_invocation("b")["status"] == "resume_requested"

    assert iv.request(store, "missing", "kill") is False
    with pytest.raises(ValueError):
        iv.request(store, "a", "explode")

    actions = [i["action"] for i in store.interventions()]
    assert actions.count("suspend") == 1 and actions.count("resume") == 1


def test_notify_runs_command_with_env_and_never_raises(env, tmp_path):
    _, cfg = env
    out = tmp_path / "notify.txt"
    script = tmp_path / "notify.py"
    script.write_text(
        "import os\n"
        f"open({str(out)!r}, 'a').write(os.environ['CCTRIPWIRE_TITLE'] + '|' + os.environ['CCTRIPWIRE_BODY'] + '\\n')\n")
    cfg.data["daemon"]["notify_command"] = f"{sys.executable} {script}"
    assert iv.notify(cfg, "cctripwire: level STOP", "5h 95%") is True
    assert out.read_text() == "cctripwire: level STOP|5h 95%\n"

    cfg.data["daemon"]["notify_command"] = "/nonexistent/definitely-not-a-binary"
    assert iv.notify(cfg, "t", "b") is False          # missing binary: False, no exception

    cfg.data["daemon"]["notify_command"] = ""
    assert iv.notify(cfg, "t", "b") is False          # unset: nothing runs

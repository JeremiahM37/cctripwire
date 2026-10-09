"""`cctripwire setup` / `teardown` in a throwaway HOME. The service step is skipped (--no-daemon)
so no real systemd or launchd unit is touched."""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from cctripwire import cli, installer


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("CCTRIPWIRE_CONFIG", "CCTRIPWIRE_HOME", "CLAUDE_CONFIG_DIR", "CCTRIPWIRE_SHIM_DIR"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({
        "model": "opus", "statusLine": {"type": "command", "command": "my-statusline.sh"}}))
    (tmp_path / ".bashrc").write_text("alias ll='ls -l'\n")
    (tmp_path / ".zshrc").write_text("")
    return tmp_path


def settings(home: Path) -> dict:
    return json.loads((home / ".claude" / "settings.json").read_text())


def test_preset_in_package_matches_example():
    example = Path(__file__).resolve().parents[1] / "examples" / "runaway-only.toml"
    assert installer.preset_text() == example.read_text()
    assert tomllib.loads(installer.preset_text(observe=True))["gateway"]["storm_observe"] is True


def test_setup_then_teardown_round_trip(home, capsys):
    assert cli.main(["setup", "--no-daemon", "--edit-rc", "--observe"]) == 0
    cfg = tomllib.loads((home / ".config" / "cctripwire" / "config.toml").read_text())
    assert cfg["rules"] == [] and cfg["gateway"]["storm_observe"] is True
    assert cfg["sources"]["statusline_chain"] == "my-statusline.sh"
    s = settings(home)
    assert s["model"] == "opus"
    assert "cctripwire" in s["statusLine"]["command"]
    assert all("cctripwire" in json.dumps(v) for v in s["hooks"].values())
    shim = home / ".local" / "share" / "cctripwire" / "shim" / "claude"
    assert shim.exists() and "cctripwire-shim" in shim.read_text()
    assert installer.RC_MARK in (home / ".bashrc").read_text()
    assert installer.RC_MARK in (home / ".zshrc").read_text()

    # Idempotent: a second run changes nothing it already did.
    assert cli.main(["setup", "--no-daemon", "--edit-rc"]) == 0
    assert (home / ".bashrc").read_text().count(installer.RC_MARK) == 1
    assert "kept existing" in capsys.readouterr().out

    assert cli.main(["teardown"]) == 0
    s = settings(home)
    assert "hooks" not in s or not s["hooks"]
    assert s["statusLine"]["command"] == "my-statusline.sh"      # restored
    assert s["model"] == "opus"
    assert not shim.exists()
    assert (home / ".bashrc").read_text() == "alias ll='ls -l'\n"
    assert (home / ".config" / "cctripwire" / "config.toml").exists()   # config is kept


def test_dry_run_changes_nothing(home):
    before = (home / ".claude" / "settings.json").read_text()
    assert cli.main(["setup", "--dry-run", "--edit-rc"]) == 0
    assert (home / ".claude" / "settings.json").read_text() == before
    assert not (home / ".config" / "cctripwire").exists()
    assert installer.RC_MARK not in (home / ".bashrc").read_text()


def test_service_files_name_the_daemon():
    assert "cctripwire" in installer.systemd_unit() and " daemon" in installer.systemd_unit()
    assert "<string>daemon</string>" in installer.launchd_plist()

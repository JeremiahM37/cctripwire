"""`cctripwire setup` / `cctripwire teardown`: one-command install of the runaway-only setup.

setup writes the preset config (unless one exists), merges the hooks and status line into the
user's Claude Code settings, writes the `claude` shim, and installs the daemon as a user service
(systemd on Linux, launchd on macOS). Every step is idempotent, and teardown reverses all of
them except the config and state, which are left for the user.
"""
from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from . import gateway
from .config import default_config_path

UNIT_NAME = "cctripwire.service"
LAUNCHD_LABEL = "io.github.cctripwire"
RC_MARK = "# cctripwire shim"


def claude_settings_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return Path(os.path.expanduser(base)) / "settings.json"


def preset_text(observe: bool = False) -> str:
    text = resources.files("cctripwire").joinpath("presets/runaway-only.toml").read_text(encoding="utf-8")
    if observe:
        text = text.replace("storm_observe = false", "storm_observe = true ", 1)
    return text


def daemon_argv() -> list[str]:
    exe = shutil.which("cctripwire")
    return [os.path.abspath(exe), "daemon"] if exe else [sys.executable, "-m", "cctripwire", "daemon"]


def systemd_unit() -> str:
    return (
        "[Unit]\n"
        "Description=cctripwire runaway breaker for Claude Code\n\n"
        "[Service]\n"
        f"ExecStart={' '.join(shlex.quote(a) for a in daemon_argv())}\n"
        "Restart=on-failure\nRestartSec=10\nNice=10\nMemoryMax=300M\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def launchd_plist() -> str:
    args = "".join(f"    <string>{a}</string>\n" for a in daemon_argv())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f"  <key>Label</key><string>{LAUNCHD_LABEL}</string>\n"
        f"  <key>ProgramArguments</key><array>\n{args}  </array>\n"
        "  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n"
        "</dict></plist>\n"
    )


def _service_path() -> Path | None:
    system = platform.system()
    if system == "Linux":
        return Path(os.path.expanduser(f"~/.config/systemd/user/{UNIT_NAME}"))
    if system == "Darwin":
        return Path(os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist"))
    return None


def _run(argv: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def _systemctl_env() -> dict:
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def install_service(dry_run: bool = False) -> str:
    path = _service_path()
    if path is None:
        return f"no user service manager known for {platform.system()}; run `cctripwire daemon` yourself"
    body = systemd_unit() if path.suffix == ".service" else launchd_plist()
    if dry_run:
        return f"would write {path} and start it"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    if path.suffix == ".service":
        os.environ.update({k: v for k, v in _systemctl_env().items() if k == "XDG_RUNTIME_DIR"})
        ok1, _ = _run(["systemctl", "--user", "daemon-reload"])
        ok2, err = _run(["systemctl", "--user", "enable", "--now", UNIT_NAME])
        if not (ok1 and ok2):
            return f"wrote {path}, but could not start it ({err}); run: systemctl --user enable --now {UNIT_NAME}"
        return f"daemon running as systemd user service {UNIT_NAME}"
    _run(["launchctl", "unload", str(path)])
    ok, err = _run(["launchctl", "load", "-w", str(path)])
    if not ok:
        return f"wrote {path}, but launchctl failed ({err}); run: launchctl load -w {path}"
    return f"daemon running as launchd agent {LAUNCHD_LABEL}"


def remove_service() -> str:
    path = _service_path()
    if path is None or not path.exists():
        return "no service installed"
    if path.suffix == ".service":
        os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        _run(["systemctl", "--user", "disable", "--now", UNIT_NAME])
        path.unlink()
        _run(["systemctl", "--user", "daemon-reload"])
    else:
        _run(["launchctl", "unload", "-w", str(path)])
        path.unlink()
    return f"removed {path}"


def rc_files() -> list[Path]:
    home = Path(os.path.expanduser("~"))
    return [home / n for n in (".bashrc", ".zshrc") if (home / n).exists()]


def path_line() -> str:
    return f'export PATH="{gateway.default_shim_dir()}:$PATH"  {RC_MARK}'


def add_shim_to_rc(dry_run: bool = False) -> list[Path]:
    changed = []
    for rc in rc_files():
        text = rc.read_text(encoding="utf-8", errors="replace")
        if RC_MARK in text:
            continue
        if not dry_run:
            with open(rc, "a", encoding="utf-8") as f:
                f.write(("" if text.endswith("\n") or not text else "\n") + path_line() + "\n")
        changed.append(rc)
    return changed


def remove_shim_from_rc() -> list[Path]:
    changed = []
    for rc in rc_files():
        lines = rc.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        kept = [ln for ln in lines if RC_MARK not in ln]
        if len(kept) != len(lines):
            rc.write_text("".join(kept), encoding="utf-8")
            changed.append(rc)
    return changed


def setup(*, observe: bool = False, shim: bool = True, edit_rc: bool = False, service: bool = True,
          statusline: bool = True, settings: Path | None = None, dry_run: bool = False) -> list[str]:
    """Run every setup step and return one line per step."""
    from . import cli   # cli imports this module lazily; keep the cycle one-way at import time

    out: list[str] = []
    cfg_path = default_config_path()
    if cfg_path.exists():
        out.append(f"config: kept existing {cfg_path}")
    else:
        if not dry_run:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(preset_text(observe), encoding="utf-8")
        out.append(f"config: wrote runaway-only preset to {cfg_path}" + (" (observe mode)" if observe else ""))

    spath = settings or claude_settings_path()
    data = cli._load_or_raise(spath)
    cli._apply_hooks(data, cli.cctripwire_command("hook"))
    chained = cli._apply_statusline(data, cli.cctripwire_command("statusline")) if statusline else None
    if not dry_run:
        backup = cli._save_settings(spath, data)
        out.append(f"hooks: merged into {spath}" + (f" (backup {backup})" if backup else ""))
        if chained and cfg_path.exists():
            text = cfg_path.read_text(encoding="utf-8")
            if "statusline_chain" not in text.split("[sources]", 1)[-1].split("\n[", 1)[0]:
                text = text.replace("[sources]\n", f"[sources]\nstatusline_chain = {_toml_str(chained)}\n", 1)
                cfg_path.write_text(text, encoding="utf-8")
            out.append("status line: your previous status line still shows after ours")
    else:
        out.append(f"hooks: would merge into {spath}")

    if shim:
        if dry_run:
            out.append(f"shim: would write {gateway.default_shim_dir() / 'claude'}")
        else:
            p = gateway.install_shim(None)
            out.append(f"shim: wrote {p}")
        if edit_rc:
            changed = add_shim_to_rc(dry_run)
            if changed:
                out.append("shim: PATH line added to " + ", ".join(map(str, changed)) + " (open a new shell)")
            elif rc_files():
                out.append("shim: PATH line already in your shell rc files")
            else:
                out.append(f"shim: no ~/.bashrc or ~/.zshrc found; add this line to your shell profile:\n    {path_line()}")
        else:
            out.append(f"shim: put it first on PATH in new shells:\n    {path_line()}")
    if service:
        out.append(install_service(dry_run))
    else:
        out.append("daemon: not installed; run `cctripwire daemon` to enable surge and bypass detection")
    return out


def teardown(*, settings: Path | None = None) -> list[str]:
    from . import cli

    out: list[str] = []
    out.append(remove_service())
    spath = settings or claude_settings_path()
    if spath.exists():
        data = cli._load_or_raise(spath)
        cli._strip_cctripwire_hooks(data)
        had_ours = cli._has_cctripwire_statusline(data)
        cli._remove_statusline(data)
        chain = _configured_chain()
        if had_ours and chain:
            data["statusLine"] = {"type": "command", "command": chain}
            out.append("status line: restored your previous one")
        cli._save_settings(spath, data)
        out.append(f"hooks: removed from {spath}")
    shim = gateway.default_shim_dir() / "claude"
    if shim.exists():
        shim.unlink()
        out.append(f"shim: removed {shim}")
    changed = remove_shim_from_rc()
    if changed:
        out.append("shim: PATH line removed from " + ", ".join(map(str, changed)))
    out.append(f"kept config {default_config_path()} and the state directory")
    return out


def _configured_chain() -> str:
    try:
        from .config import load
        return str(load().get("sources.statusline_chain") or "")
    except Exception:  # noqa: BLE001 - a broken config must not stop teardown
        return ""


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

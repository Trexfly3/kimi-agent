"""Shortcuts that open the desktop app, so it can be started like any other program.

* Windows: "Kimi Agent" in the Start menu and on the desktop.
* macOS: "Kimi Agent.app" in ~/Applications (so it shows up in Launchpad and Spotlight).
* Linux: an entry in the applications menu and a launcher on the desktop.

The one-line installers call ``kimi-agent --install-shortcuts``; ``--remove-shortcuts`` undoes it.
"""

from __future__ import annotations

import base64
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

from .. import __version__

APP_NAME = "Kimi Agent"
DESKTOP_ID = "kimi-agent"
BUNDLE_ID = "io.github.trexfly3.kimi-agent"
DESCRIPTION = "An AI agent that gets things done on your computer"
STATIC = Path(__file__).resolve().parent / "static"

# Creates (or removes) "Kimi Agent.lnk" in the Start menu and on the desktop; prints each path it touched.
# Values come in through environment variables, so nothing needs quoting.
_WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$folders = @([Environment]::GetFolderPath('Programs'))
if ($env:KA_DESKTOP -eq '1') { $folders += [Environment]::GetFolderPath('Desktop') }
foreach ($folder in $folders) {
    if (-not $folder) { continue }
    $link = Join-Path $folder ($env:KA_NAME + '.lnk')
    if ($env:KA_MODE -eq 'remove') {
        if (Test-Path -LiteralPath $link) { Remove-Item -LiteralPath $link -Force; $link }
        continue
    }
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
    $shortcut.TargetPath = $env:KA_TARGET
    $shortcut.Arguments = $env:KA_ARGS
    $shortcut.WorkingDirectory = $env:USERPROFILE
    $shortcut.IconLocation = $env:KA_ICON
    $shortcut.Description = $env:KA_DESCRIPTION
    $shortcut.Save()
    $link
}
"""


def launcher_command() -> list[str]:
    """The command that starts the desktop app without a terminal window."""
    windows = os.name == "nt"
    name = "kimi-agent-gui.exe" if windows else "kimi-agent-gui"
    for candidate in (Path(sysconfig.get_path("scripts")) / name, shutil.which("kimi-agent-gui")):
        if candidate and Path(candidate).is_file():
            return [str(Path(candidate).absolute())]
    python = Path(sys.executable)
    if windows and python.with_name("pythonw.exe").is_file():
        python = python.with_name("pythonw.exe")  # no console window
    return [str(python), "-m", "kimi_agent.gui"]


def install_shortcuts(desktop: bool = True) -> list[Path]:
    """Create the shortcuts for this platform; returns their paths."""
    command = launcher_command()
    if sys.platform == "win32":
        return _windows("install", command, desktop)
    if sys.platform == "darwin":
        return [install_macos_app(Path.home() / "Applications", command)]
    return install_linux_entries(_xdg_data_home(), _desktop_dir() if desktop else None, command)


def remove_shortcuts() -> list[Path]:
    """Remove the shortcuts that :func:`install_shortcuts` creates; returns the paths removed."""
    if sys.platform == "win32":
        return _windows("remove", [], True)
    if sys.platform == "darwin":
        return _remove([Path.home() / "Applications" / f"{APP_NAME}.app"])
    desktop = _desktop_dir()
    return _remove([_xdg_data_home() / "applications" / f"{DESKTOP_ID}.desktop",
                    *([desktop / f"{DESKTOP_ID}.desktop"] if desktop else [])])


# ---------------------------------------------------------------------- Windows
def _windows(mode: str, command: list[str], desktop: bool) -> list[Path]:
    env = {**os.environ, "KA_MODE": mode, "KA_DESKTOP": "1" if desktop else "0", "KA_NAME": APP_NAME,
           "KA_TARGET": command[0] if command else "", "KA_ARGS": subprocess.list2cmdline(command[1:]),
           "KA_ICON": f"{STATIC / 'icon.ico'},0", "KA_DESCRIPTION": DESCRIPTION}
    encoded = base64.b64encode(_WINDOWS_SCRIPT.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"PowerShell exited with {result.returncode}")
    return [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------- macOS
def install_macos_app(applications: Path, command: list[str]) -> Path:
    """Write a small app bundle that starts the launcher in the background and returns at once.

    Returning straight away matters: macOS only starts an app that is not already running, so a bundle that
    stayed open would make later clicks do nothing. The launcher reuses a running server by itself.
    """
    app = applications / f"{APP_NAME}.app"
    contents = app / "Contents"
    if app.exists():
        shutil.rmtree(app)
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    executable = contents / "MacOS" / DESKTOP_ID
    executable.write_text(f'#!/bin/sh\n{shlex.join(["/usr/bin/nohup", *command])} "$@" >/dev/null 2>&1 &\n',
                          encoding="utf-8")
    executable.chmod(0o755)
    shutil.copyfile(STATIC / "icon.icns", contents / "Resources" / "icon.icns")
    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": DESKTOP_ID,
        "CFBundleIconFile": "icon",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSUIElement": True,  # no Dock icon for the launcher itself; the window has its own
        "NSHighResolutionCapable": True,
    }
    with open(contents / "Info.plist", "wb") as fh:
        plistlib.dump(info, fh)
    lsregister = Path("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
                      "/Support/lsregister")
    if lsregister.is_file():  # make Launchpad and Spotlight notice it (and its icon) right away
        subprocess.run([str(lsregister), "-f", str(app)], capture_output=True, timeout=60, check=False)
    return app


# ---------------------------------------------------------------------- Linux
def install_linux_entries(data_home: Path, desktop_dir: Path | None, command: list[str]) -> list[Path]:
    """Add a menu entry (and a desktop launcher when ``desktop_dir`` exists)."""
    entry = desktop_entry(command)
    targets = [data_home / "applications" / f"{DESKTOP_ID}.desktop"]
    if desktop_dir and desktop_dir.is_dir():
        targets.append(desktop_dir / f"{DESKTOP_ID}.desktop")
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(entry, encoding="utf-8")
        path.chmod(0o755)  # desktop launchers must be executable
    if len(targets) > 1 and shutil.which("gio"):  # GNOME asks before running untrusted desktop launchers
        _quiet(["gio", "set", str(targets[1]), "metadata::trusted", "true"])
    if shutil.which("update-desktop-database"):
        _quiet(["update-desktop-database", str(targets[0].parent)])
    return targets


def desktop_entry(command: list[str]) -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        f"Name={APP_NAME}",
        "GenericName=AI agent",
        f"Comment={DESCRIPTION}",
        "Exec=" + " ".join(_exec_arg(arg) for arg in command),
        f"Icon={STATIC / 'icon.png'}",
        "Terminal=false",
        "Categories=Utility;Development;",
        "StartupNotify=false",
        "",
    ])


def _exec_arg(arg: str) -> str:
    """Quote one argument for a desktop entry's Exec key (see the Desktop Entry Specification)."""
    arg = arg.replace("%", "%%")
    if re.search(r"""[\s"'\\><~|&;$*?#()`]""", arg):
        arg = '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'
    return arg.replace("\\", "\\\\")  # the file format's own escaping comes on top


def _xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _desktop_dir() -> Path | None:
    home = Path.home()
    desktop = home / "Desktop"
    if shutil.which("xdg-user-dir"):
        try:
            found = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            found = ""
        if found.strip() and Path(found.strip()) != home:
            desktop = Path(found.strip())
    return desktop if desktop.is_dir() else None


def _quiet(argv: list[str]) -> None:
    try:
        subprocess.run(argv, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def _remove(paths: list[Path]) -> list[Path]:
    removed = []
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
        else:
            continue
        removed.append(path)
    return removed

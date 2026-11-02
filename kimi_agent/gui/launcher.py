"""Desktop launcher: start (or reuse) the local server and open it in a browser app window."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from .. import __version__
from ..config import default_data_dir, load_dotenv

log = logging.getLogger("kimi_agent.gui")

IDLE_GRACE_SECONDS = 120.0  # quit this long after the last window closed (when nothing is running)
IDLE_MAX_SECONDS = 1800.0  # ... or this long even if a task is still running
WINDOW_SIZE = "1240,860"


def fix_streams() -> None:
    """pythonw (the Windows GUI launcher) has no stdout/stderr; give them somewhere to go."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115


def _has_console() -> bool:
    try:
        return bool(sys.stderr and sys.stderr.isatty())
    except (AttributeError, ValueError):
        return False


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def show_error(message: str) -> None:
    """Report a problem in the terminal, or in a dialog when started from a shortcut (no terminal to print to)."""
    log.error("%s", message)
    if _has_console():
        print(f"kimi-agent: {message}", file=sys.stderr, flush=True)
        return
    try:
        if sys.platform == "win32":
            import ctypes

            # MB_ICONERROR | MB_SETFOREGROUND | MB_TOPMOST
            ctypes.windll.user32.MessageBoxW(None, message, "Kimi Agent", 0x10 | 0x10000 | 0x40000)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            script = f"display alert {_applescript_string('Kimi Agent')} message {_applescript_string(message)} as critical"
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=3600, check=False)
        else:
            for argv in (["zenity", "--error", "--no-markup", "--title=Kimi Agent", f"--text={message}"],
                         ["kdialog", "--title", "Kimi Agent", "--error", message],
                         ["notify-send", "Kimi Agent", message]):
                if shutil.which(argv[0]):
                    subprocess.run(argv, capture_output=True, timeout=3600, check=False)
                    break
    except Exception:
        log.debug("could not show an error dialog", exc_info=True)


def _extend_path() -> None:
    """Apps started from Finder or an app menu get a minimal PATH; add the usual places for user-installed tools."""
    if os.name == "nt":
        return
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    extra = [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin"]
    missing = [p for p in extra if p not in parts and Path(p).is_dir()]
    if missing:
        os.environ["PATH"] = os.pathsep.join(parts + missing)


def find_browser() -> list[str] | None:
    """A Chromium-based browser that supports --app windows (Edge, Chrome, Brave, Chromium)."""
    override = os.environ.get("KIMI_AGENT_BROWSER", "").strip()
    if override:
        return [override]
    candidates: list[Path] = []
    if sys.platform == "win32":
        bases = [os.environ.get(k) for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
                    r"BraveSoftware\Brave-Browser\Application\brave.exe", r"Chromium\Application\chrome.exe"):
            candidates += [Path(base) / rel for base in bases if base]
    elif sys.platform == "darwin":
        for app, binary in (("Google Chrome", "Google Chrome"), ("Microsoft Edge", "Microsoft Edge"),
                            ("Brave Browser", "Brave Browser"), ("Chromium", "Chromium")):
            for root in (Path("/Applications"), Path.home() / "Applications"):
                candidates.append(root / f"{app}.app" / "Contents" / "MacOS" / binary)
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
                     "microsoft-edge-stable", "brave-browser", "brave"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
    for path in candidates:
        if path.is_file():
            return [str(path)]
    return None


def _spawn(argv: list[str]) -> None:
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                              "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)


def open_window(url: str) -> bool:
    """Open ``url`` as an app window (no tabs or address bar); fall back to the default browser."""
    browser = find_browser()
    if browser:
        try:
            _spawn([*browser, f"--app={url}", "--new-window", f"--window-size={WINDOW_SIZE}"])
            return True
        except OSError as exc:
            log.warning("could not start %s: %s", browser[0], exc)
    try:
        return webbrowser.open(url)
    except webbrowser.Error:
        return False


def _read_lock(lock_path: Path) -> dict[str, Any] | None:
    try:
        info = json.loads(lock_path.read_text(encoding="utf-8"))
        return {"pid": int(info["pid"]), "port": int(info["port"]), "secret": str(info["secret"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def launcher_mac(secret: str, role: str, path: str, nonce: str) -> str:
    """Proof that ``role`` (``launcher`` or ``server``) knows the lock file's secret, without revealing it."""
    return hmac.new(secret.encode(), f"{role}\n{path}\n{nonce}".encode(), hashlib.sha256).hexdigest()


def _ask_running(info: dict[str, Any], path: str) -> dict[str, Any] | None:
    """POST to the instance named in the lock file; None unless it runs and proves that it knows the secret.

    The secret itself is never sent: after a crash, another program could be listening on the old port.
    """
    if not _process_alive(info["pid"]):
        return None
    nonce = secrets.token_hex(16)
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}{path}", data=b"{}", method="POST",
        headers={"Authorization": f"KimiAgent {nonce}.{launcher_mac(info['secret'], 'launcher', path, nonce)}",
                 "Content-Type": "application/json", "X-Kimi-Agent": "1"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never send this through a proxy
    try:
        with opener.open(request, timeout=5) as response:
            data = json.loads(response.read())
    except (OSError, ValueError):
        return None
    proof = launcher_mac(info["secret"], "server", path, nonce)
    if not isinstance(data, dict) or not hmac.compare_digest(str(data.get("proof", "")).encode(), proof.encode()):
        log.warning("port %s answered without proving that it is Kimi Agent; ignoring it", info["port"])
        return None
    return data


def _request_window(lock_path: Path) -> str | None:
    """Ask an already running instance for a new launch link."""
    info = _read_lock(lock_path)
    if not info:
        return None
    reply = _ask_running(info, "/api/launch")
    url = str(reply.get("url") or "") if reply else ""
    return url if url.startswith(f"http://127.0.0.1:{info['port']}/auth?code=") else None


def _process_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def quit_running(data_dir: Path, timeout: float = 20.0) -> bool:
    """Ask a running desktop app to quit and wait for it to exit; False if none was running.

    The installers use this before updating, because Windows cannot replace files that are in use.
    """
    lock_path = data_dir / "gui.lock.json"
    info = _read_lock(lock_path)
    if not info or _ask_running(info, "/api/quit") is None:
        return False
    pid = info["pid"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = _read_lock(lock_path)
        released = not current or current["pid"] != pid
        if released and (pid == os.getpid() or not _process_alive(pid)):
            return True
        time.sleep(0.1)
    raise TimeoutError(f"the desktop app (process {pid}) did not quit within {timeout:.0f} seconds")


def _write_lock(lock_path: Path, port: int, secret: str) -> None:
    from .app import _write_private

    _write_private(lock_path, json.dumps({"pid": os.getpid(), "port": port, "secret": secret,
                                          "version": __version__}))


def _remove_lock(lock_path: Path) -> None:
    try:
        if json.loads(lock_path.read_text(encoding="utf-8")).get("pid") == os.getpid():
            lock_path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def _watchdog(server: Any, app: Any) -> None:
    alone_since: float | None = None
    while not server.stopping.wait(2.0):
        if app.bus.subscribers:
            alone_since = None
            continue
        now = time.monotonic()
        if alone_since is None:
            alone_since = max(now, app.last_activity)
        idle = now - alone_since
        if (idle >= IDLE_GRACE_SECONDS and not app.running) or idle >= IDLE_MAX_SECONDS:
            log.info("no windows open for %.0fs; shutting down", idle)
            server.request_shutdown()
            return


def _data_dir(overrides: dict[str, Any]) -> Path:
    return Path(overrides.get("data_dir") or default_data_dir()).expanduser()


NO_BROWSER = ("Kimi Agent could not open a browser window. Install Microsoft Edge or Google Chrome, or open "
              "this link in your browser within two minutes:\n\n{link}")


def run_gui(overrides: dict[str, Any] | None = None, config_file: str | None = None, *, port: int | None = None,
            open_browser: bool = True, use_mcp: bool = True, default_workdir: str | None = None) -> int:
    """Run the desktop app until its last window has been closed for a while (or until interrupted)."""
    fix_streams()
    _extend_path()
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    lock_path = _data_dir(overrides) / "gui.lock.json"
    try:  # the .env files load_settings reads, in its order: KIMI_AGENT_BROWSER is needed before the settings
        load_dotenv(Path.cwd() / ".env")
        load_dotenv(lock_path.parent / ".env")
    except OSError:
        pass  # load_settings reports it

    url = _request_window(lock_path) if port is None else None
    if url:  # already running: just open another window
        if open_browser and open_window(url):
            return 0
        if open_browser and not _has_console():
            show_error(NO_BROWSER.format(link=url))
        else:
            print(f"Kimi Agent is already running. Open this link to connect:\n  {url}", flush=True)
        return 0

    from ..cli import setup_logging
    from .app import GuiApp
    from .server import GuiServer

    try:
        app = GuiApp(overrides, config_file, use_mcp=use_mcp, default_workdir=default_workdir)
        log_path = setup_logging(app.settings)
    except Exception as exc:
        show_error(f"Kimi Agent could not start: {exc}")
        return 1
    try:
        server = GuiServer(app, port)
    except OSError as exc:
        show_error(f"Kimi Agent could not start its local server: {exc}")
        return 1
    _write_lock(lock_path, server.port, server.secret)
    log.info("desktop app %s listening on %s (log: %s)", __version__, server.url, log_path)
    app.start()

    def on_terminate(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        for name in ("SIGTERM", "SIGHUP"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), on_terminate)

    console = _has_console()
    opened = open_browser and open_window(server.launch_url(ttl=120.0))
    if opened or (open_browser and not console):  # nobody could press Ctrl+C, so quit once unused
        threading.Thread(target=_watchdog, args=(server, app), name="kimi-agent-watchdog", daemon=True).start()
    if not opened:
        link = server.launch_url(ttl=3600.0)
        if open_browser and not console:
            threading.Thread(target=show_error, args=(NO_BROWSER.format(link=link),), daemon=True).start()
        else:
            print(f"Kimi Agent is running. Open this link in your browser to connect (it works once):\n  {link}\n"
                  "Press Ctrl+C to quit.", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stopping.set()
        try:
            app.close()
        finally:
            server.server_close()
            _remove_lock(lock_path)
    return 0


def desktop_command(args: argparse.Namespace, overrides: dict[str, Any] | None = None) -> int:
    """``--quit-gui``, ``--install-shortcuts`` and ``--remove-shortcuts`` (used by the installers)."""
    if args.quit_gui:
        try:
            was_running = quit_running(_data_dir(overrides or {}))
        except TimeoutError as exc:
            print(f"kimi-agent: {exc}", file=sys.stderr)
            return 1
        print("Closed the desktop app." if was_running else "The desktop app is not running.")
        return 0
    from . import shortcuts

    try:
        if args.install_shortcuts:
            for path in shortcuts.install_shortcuts():
                print(f"Added {path}")
        if args.remove_shortcuts:
            for path in shortcuts.remove_shortcuts():
                print(f"Removed {path}")
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"kimi-agent: could not update the shortcuts: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point of the ``kimi-agent-gui`` launcher (desktop shortcuts)."""
    from ..cli import build_parser, overrides_from_args

    parser = build_parser()
    parser.prog = "kimi-agent-gui"
    args: argparse.Namespace = parser.parse_args(argv)
    if args.quit_gui or args.install_shortcuts or args.remove_shortcuts:
        return desktop_command(args, overrides_from_args(args))
    return run_gui(overrides_from_args(args), args.config, port=args.port, open_browser=not args.no_browser,
                   use_mcp=not args.no_mcp, default_workdir=None if args.workdir else str(Path.home()))

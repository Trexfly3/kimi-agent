"""Optional computer-use tools: screenshots, mouse, keyboard and clipboard.

Requires a graphical session plus `pip install 'kimi-agent[computer]'` (pyautogui, mss, pillow, pyperclip).
Coordinates are always in the pixel space of the most recent screenshot; they are mapped to real screen
coordinates (handles HiDPI scaling and downscaled screenshots).
"""

from __future__ import annotations

import base64
import io
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Literal

from kimi_agent.tools.base import Image, Tool, ToolContext, ToolError, ToolResult, tool

MAX_WIDTH = 1280
MAX_HEIGHT = 800
IS_MAC = sys.platform == "darwin"

KEY_ALIASES = {
    "control": "ctrl", "ctl": "ctrl", "return": "enter", "escape": "esc", "del": "delete", "ins": "insert",
    "pgup": "pageup", "page_up": "pageup", "pgdn": "pagedown", "page_down": "pagedown", "arrowup": "up",
    "arrowdown": "down", "arrowleft": "left", "arrowright": "right", "spacebar": "space", " ": "space",
    "option": "alt", "opt": "alt", "caps_lock": "capslock", "bksp": "backspace",
}


def computer_available() -> tuple[bool, str]:
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        if os.environ.get("WAYLAND_DISPLAY"):
            return False, "pure Wayland sessions are not supported (an X11/XWayland DISPLAY is required)"
        return False, "no graphical display found (DISPLAY is not set)"
    missing = []
    for module in ("mss", "PIL", "pyautogui"):
        try:
            __import__(module)
        except Exception as exc:
            missing.append(f"{module} ({type(exc).__name__})")
    if missing:
        return False, f"missing dependencies: {', '.join(missing)}. Install with: pip install 'kimi-agent[computer]'"
    return True, ""


def _gui() -> Any:
    import pyautogui

    pyautogui.FAILSAFE = True  # slam the mouse into a screen corner to abort
    pyautogui.PAUSE = 0.05
    return pyautogui


def _monitor(index: int) -> dict[str, int]:
    import mss

    with mss.mss() as sct:
        monitors = sct.monitors
    if index < 1 or index >= len(monitors):
        raise ToolError(f"Monitor {index} does not exist. Available monitors: 1-{len(monitors) - 1}.")
    return dict(monitors[index])


def _mapping(ctx: ToolContext, monitor: int = 1) -> dict[str, Any]:
    mapping = ctx.state.get("screen")
    if mapping is None:
        mon = _monitor(monitor)
        scale = min(1.0, MAX_WIDTH / mon["width"], MAX_HEIGHT / mon["height"])
        mapping = {
            "monitor": monitor, "left": mon["left"], "top": mon["top"], "scale": scale,
            "width": max(1, round(mon["width"] * scale)), "height": max(1, round(mon["height"] * scale)),
        }
        ctx.state["screen"] = mapping
    return mapping


def capture(ctx: ToolContext, monitor: int = 1) -> tuple[Image, str]:
    import mss
    from PIL import Image as PILImage

    mon = _monitor(monitor)
    with mss.mss() as sct:
        raw = sct.grab(mon)
        img = PILImage.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    scale = min(1.0, MAX_WIDTH / mon["width"], MAX_HEIGHT / mon["height"])
    size = (max(1, round(mon["width"] * scale)), max(1, round(mon["height"] * scale)))
    if img.size != size:
        img = img.resize(size, PILImage.LANCZOS)
    ctx.state["screen"] = {"monitor": monitor, "left": mon["left"], "top": mon["top"], "scale": scale,
                           "width": size[0], "height": size[1]}
    buffer = io.BytesIO()
    img.save(buffer, "JPEG", quality=85)
    data = buffer.getvalue()
    try:
        folder = ctx.data_dir / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{datetime.now():%Y%m%d-%H%M%S-%f}.jpg").write_bytes(data)
    except OSError:
        pass
    return Image(base64.b64encode(data).decode("ascii"), "image/jpeg"), f"{size[0]}x{size[1]}"


def _to_screen(ctx: ToolContext, x: int, y: int) -> tuple[int, int]:
    m = _mapping(ctx)
    if not (0 <= x < m["width"] and 0 <= y < m["height"]):
        raise ToolError(f"Coordinates ({x}, {y}) are outside the screenshot area ({m['width']}x{m['height']}).")
    return round(m["left"] + x / m["scale"]), round(m["top"] + y / m["scale"])


def _after_action(ctx: ToolContext, message: str) -> ToolResult:
    if ctx.settings.computer_auto_screenshot and ctx.vision:
        time.sleep(0.7)
        image, size = capture(ctx)
        return ToolResult(f"{message}\nScreenshot after the action ({size}) attached.", images=[image])
    return ToolResult(message)


def _guarded(func: Any, *args: Any, **kwargs: Any) -> Any:
    gui = _gui()
    try:
        return func(gui, *args, **kwargs)
    except gui.FailSafeException as exc:
        raise ToolError("Aborted: the user triggered the fail-safe (mouse moved to a screen corner).") from exc


def _normalize_key(key: str) -> str:
    k = key.strip().lower()
    k = KEY_ALIASES.get(k, k)
    if k in ("cmd", "command", "super", "meta", "win", "windows"):
        k = "command" if IS_MAC else "win"
    return k


@tool(category="read", summarize=lambda a: f"monitor {a.get('monitor', 1)}")
def screenshot(ctx: ToolContext, monitor: int = 1, delay: float = 0) -> ToolResult:
    """Take a screenshot of the screen. All mouse coordinates refer to this screenshot's pixels.

    Args:
        monitor: Monitor number (1 = primary).
        delay: Seconds to wait before capturing (e.g. for a page to load).
    """
    if not ctx.vision:
        raise ToolError("Vision is disabled, so screenshots cannot be viewed. Enable it with --vision.")
    if delay > 0:
        time.sleep(min(float(delay), 30.0))
    image, size = capture(ctx, monitor)
    return ToolResult(f"Screenshot of monitor {monitor} ({size}) attached.", images=[image])


@tool(category="gui", summarize=lambda a: f"{a.get('button', 'left')} x{a.get('clicks', 1)} at "
                                          f"({a.get('x')}, {a.get('y')})")
def mouse_click(
    ctx: ToolContext,
    x: int,
    y: int,
    button: Literal["left", "right", "middle"] = "left",
    clicks: int = 1,
    modifiers: list[str] | None = None,
) -> ToolResult:
    """Click at a position of the latest screenshot.

    Args:
        x: X coordinate in screenshot pixels.
        y: Y coordinate in screenshot pixels.
        button: Mouse button.
        clicks: Number of clicks (2 = double-click).
        modifiers: Keys to hold while clicking, e.g. ["ctrl"] or ["shift"].
    """
    sx, sy = _to_screen(ctx, x, y)
    keys = [_normalize_key(k) for k in modifiers or []]

    def do(gui: Any) -> None:
        for key in keys:
            gui.keyDown(key)
        try:
            gui.click(sx, sy, clicks=max(1, min(int(clicks), 3)), interval=0.08, button=button)
        finally:
            for key in reversed(keys):
                gui.keyUp(key)

    _guarded(do)
    return _after_action(ctx, f"Clicked {button} x{clicks} at ({x}, {y}).")


@tool(category="gui", summarize=lambda a: f"({a.get('x')}, {a.get('y')})")
def mouse_move(ctx: ToolContext, x: int, y: int) -> ToolResult:
    """Move the mouse pointer (e.g. to hover over an element).

    Args:
        x: X coordinate in screenshot pixels.
        y: Y coordinate in screenshot pixels.
    """
    sx, sy = _to_screen(ctx, x, y)
    _guarded(lambda gui: gui.moveTo(sx, sy, duration=0.2))
    return _after_action(ctx, f"Moved the mouse to ({x}, {y}).")


@tool(category="gui", summarize=lambda a: f"({a.get('start_x')}, {a.get('start_y')}) -> "
                                          f"({a.get('end_x')}, {a.get('end_y')})")
def mouse_drag(ctx: ToolContext, start_x: int, start_y: int, end_x: int, end_y: int,
               button: Literal["left", "right", "middle"] = "left") -> ToolResult:
    """Drag with the mouse button held from one position to another.

    Args:
        start_x: Start X coordinate in screenshot pixels.
        start_y: Start Y coordinate in screenshot pixels.
        end_x: End X coordinate in screenshot pixels.
        end_y: End Y coordinate in screenshot pixels.
        button: Mouse button to hold.
    """
    x1, y1 = _to_screen(ctx, start_x, start_y)
    x2, y2 = _to_screen(ctx, end_x, end_y)

    def do(gui: Any) -> None:
        gui.moveTo(x1, y1, duration=0.2)
        gui.dragTo(x2, y2, duration=0.6, button=button)

    _guarded(do)
    return _after_action(ctx, f"Dragged from ({start_x}, {start_y}) to ({end_x}, {end_y}).")


@tool(category="gui", summarize=lambda a: f"{a.get('direction', 'down')} x{a.get('amount', 3)}")
def scroll(ctx: ToolContext, x: int, y: int, direction: Literal["up", "down", "left", "right"] = "down",
           amount: int = 3) -> ToolResult:
    """Scroll at a position.

    Args:
        x: X coordinate in screenshot pixels to scroll at.
        y: Y coordinate in screenshot pixels to scroll at.
        direction: Scroll direction.
        amount: Number of scroll steps.
    """
    sx, sy = _to_screen(ctx, x, y)
    steps = max(1, min(int(amount), 50))

    def do(gui: Any) -> None:
        gui.moveTo(sx, sy)
        if direction in ("up", "down"):
            gui.scroll(steps * (1 if direction == "up" else -1) * (1 if IS_MAC else 3))
        elif sys.platform.startswith("linux"):
            gui.hscroll(steps * (-1 if direction == "left" else 1) * 3)
        else:
            with gui.hold("shift"):
                gui.scroll(steps * (1 if direction == "left" else -1) * 3)

    _guarded(do)
    return _after_action(ctx, f"Scrolled {direction} by {steps} at ({x}, {y}).")


def _paste(gui: Any, text: str) -> None:
    import pyperclip

    try:
        previous = pyperclip.paste()
    except Exception:
        previous = None
    pyperclip.copy(text)
    gui.hotkey("command" if IS_MAC else "ctrl", "v")
    time.sleep(0.3)
    if previous is not None:
        try:
            pyperclip.copy(previous)
        except Exception:
            pass


@tool(category="gui", summarize=lambda a: str(a.get("text", "")))
def type_text(ctx: ToolContext, text: str, press_enter: bool = False) -> ToolResult:
    """Type text with the keyboard into the focused element (click it first).

    Args:
        text: Text to type. Non-ASCII text is pasted via the clipboard.
        press_enter: Press Enter after typing.
    """
    def do(gui: Any) -> None:
        if text.isascii():
            gui.write(text, interval=0.01)
        else:
            try:
                _paste(gui, text)
            except ImportError as exc:
                raise ToolError("Typing non-ASCII text requires pyperclip (pip install pyperclip).") from exc
        if press_enter:
            gui.press("enter")

    _guarded(do)
    return _after_action(ctx, f"Typed {len(text)} characters" + (" and pressed Enter." if press_enter else "."))


@tool(category="gui", summarize=lambda a: "+".join(map(str, a.get("keys") or [])))
def press_keys(ctx: ToolContext, keys: list[str], presses: int = 1) -> ToolResult:
    """Press a key or a key combination, e.g. ["enter"], ["ctrl", "c"], ["alt", "tab"], ["cmd", "space"].

    Args:
        keys: Keys pressed together (modifiers first). Names like enter, esc, tab, backspace, delete, up,
            down, left, right, home, end, pageup, pagedown, f1-f12, ctrl, alt, shift, cmd/win, or a character.
        presses: How many times to press the combination.
    """
    names = [_normalize_key(k) for k in keys if str(k).strip()]
    if not names:
        raise ToolError("keys is empty.")
    gui = _gui()
    unknown = [k for k in names if k not in gui.KEYBOARD_KEYS]
    if unknown:
        raise ToolError(f"Unknown key name(s): {', '.join(unknown)}.")

    def do(g: Any) -> None:
        for _ in range(max(1, min(int(presses), 50))):
            if len(names) == 1:
                g.press(names[0])
            else:
                g.hotkey(*names)

    _guarded(do)
    return _after_action(ctx, f"Pressed {'+'.join(names)}" + (f" x{presses}." if presses > 1 else "."))


@tool(category="gui")
def clipboard_read(ctx: ToolContext) -> str:
    """Read the text content of the system clipboard."""
    try:
        import pyperclip

        return pyperclip.paste() or "(clipboard is empty)"
    except Exception as exc:
        raise ToolError(f"Could not read the clipboard: {exc}") from exc


@tool(category="gui", summarize=lambda a: str(a.get("text", "")))
def clipboard_write(ctx: ToolContext, text: str) -> str:
    """Put text on the system clipboard.

    Args:
        text: The text to copy.
    """
    try:
        import pyperclip

        pyperclip.copy(text)
    except Exception as exc:
        raise ToolError(f"Could not write to the clipboard: {exc}") from exc
    return f"Copied {len(text)} characters to the clipboard."


@tool(category="gui", summarize=lambda a: str(a.get("target", "")))
def open_target(ctx: ToolContext, target: str) -> ToolResult:
    """Open a URL, file or folder with its default application (e.g. a web page in the browser).

    Args:
        target: URL or path to open.
    """
    if "://" not in target:
        path = ctx.resolve(target)
        if not path.exists():
            raise ToolError(f"Not found: {path}")
        target = str(path)
    try:
        if sys.platform == "win32":
            os.startfile(target)  # type: ignore[attr-defined]
        else:
            opener = "open" if IS_MAC else (shutil.which("xdg-open") or shutil.which("gio"))
            if not opener:
                raise ToolError("No opener found (install xdg-utils).")
            argv = [opener, "open", target] if opener.endswith("gio") else [opener, target]
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        raise ToolError(f"Could not open {target}: {exc}") from exc
    time.sleep(1.5)
    return _after_action(ctx, f"Opened {target}.")


COMPUTER_TOOLS: list[Tool] = [
    screenshot, mouse_click, mouse_move, mouse_drag, scroll, type_text, press_keys,
    clipboard_read, clipboard_write, open_target,
]

"""Event plumbing between the agent (worker thread) and browser windows.

``EventBus`` keeps the current transcript as an ordered set of items (so a window that connects or reconnects
gets a full replay) and fans live events out to every connected window. ``WebUI`` implements the agent's UI
interface (see ``kimi_agent.ui.UI``) by publishing those events; prompts (approvals, questions) block the
worker until a window answers or the run is stopped.
"""

from __future__ import annotations

import base64
import itertools
import json
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from .render import code_block, render_markdown

MAX_ARGS_CHARS = 8_000
MAX_OUTPUT_CHARS = 16_000
MAX_SUMMARY_CHARS = 400
MAX_IMAGES_PER_TOOL = 4
HIDDEN_TOOLS = frozenset({"ask_user", "update_plan"})
REASONING_FLUSH = 0.08


def clip(text: str, limit: int) -> str:
    """Keep the head and tail of long text."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n… {len(text) - limit:,} characters omitted …\n{text[-half:]}"


def dumps(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)


class Subscriber:
    """One connected window: a queue of serialized events, or a request for a full resync."""

    def __init__(self, limit: int = 5000) -> None:
        self.cond = threading.Condition()
        self.queue: deque[str] = deque()
        self.resync = True
        self.closed = False
        self.limit = limit

    def push(self, data: str) -> None:
        with self.cond:
            if self.resync:
                return  # a full snapshot is coming anyway
            if len(self.queue) >= self.limit:  # the window is not keeping up: replay instead
                self.queue.clear()
                self.resync = True
            else:
                self.queue.append(data)
            self.cond.notify()

    def request_resync(self) -> None:
        with self.cond:
            self.queue.clear()
            self.resync = True
            self.cond.notify()

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify()

    def wait(self, timeout: float) -> tuple[bool, list[str]]:
        """Wait for events. Returns (needs_resync, events)."""
        with self.cond:
            if not self.queue and not self.resync and not self.closed:
                self.cond.wait(timeout)
            if self.resync:
                return True, []
            items = list(self.queue)
            self.queue.clear()
            return False, items


class EventBus:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.items: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.subscribers: set[Subscriber] = set()
        self.state_provider: Callable[[], dict[str, Any]] = dict
        self._ids = itertools.count(1)

    def new_id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids)}"

    def put(self, item: dict[str, Any], live: dict[str, Any] | None = None, move_to_end: bool = False) -> None:
        """Store a transcript item (replacing an older version with the same id) and broadcast it.

        Items are never mutated after being put; callers pass a fresh dict for every update.
        """
        with self.lock:
            if move_to_end:
                self.items.pop(item["id"], None)
            self.items[item["id"]] = item
            self._broadcast(live if live is not None else item)

    def get(self, item_id: str) -> dict[str, Any] | None:
        return self.items.get(item_id)

    def send(self, event: dict[str, Any]) -> None:
        """Broadcast an event that is not part of the transcript (state changes, toasts)."""
        with self.lock:
            self._broadcast(event)

    def reset(self, items: list[dict[str, Any]]) -> None:
        """Replace the whole transcript; every window gets a fresh replay."""
        with self.lock:
            self.items.clear()
            for item in items:
                self.items[item["id"]] = item
            for sub in self.subscribers:
                sub.request_resync()

    def _broadcast(self, event: dict[str, Any]) -> None:
        if not self.subscribers:
            return
        data = dumps(event)
        for sub in self.subscribers:
            sub.push(data)

    def subscribe(self) -> Subscriber:
        sub = Subscriber()
        with self.lock:
            self.subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self.lock:
            self.subscribers.discard(sub)
        sub.close()

    def snapshot(self, sub: Subscriber | None = None) -> list[str]:
        """A reset event with the current state followed by the transcript, serialized."""
        with self.lock:
            if sub is not None:
                with sub.cond:
                    sub.resync = False
                    sub.queue.clear()
            out = [dumps({"type": "reset", "state": self.state_provider(), "count": len(self.items)})]
            out += [dumps(item) for item in self.items.values()]
            return out

    def close_all(self) -> None:
        with self.lock:
            for sub in list(self.subscribers):
                sub.close()


# Served as the Content-Type of /api/image: tools (including remote MCP servers) must not choose it freely.
SAFE_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"})


class ImageStore:
    """Tool screenshots and attached images, served at /api/image/<id> (keeps SSE events small)."""

    def __init__(self, limit_bytes: int = 160_000_000) -> None:
        self.lock = threading.Lock()
        self.images: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
        self.total = 0
        self.limit = limit_bytes

    def add(self, data: bytes, mime: str) -> str:
        mime = str(mime or "").split(";", 1)[0].strip().lower()
        if mime not in SAFE_IMAGE_TYPES:
            mime = "application/octet-stream"
        image_id = secrets.token_urlsafe(12)
        with self.lock:
            self.images[image_id] = (mime, data)
            self.total += len(data)
            while self.total > self.limit and len(self.images) > 1:
                _, (_, old) = self.images.popitem(last=False)
                self.total -= len(old)
        return f"/api/image/{image_id}"

    def add_base64(self, data: str, mime: str) -> str | None:
        try:
            return self.add(base64.b64decode(data, validate=False), mime)
        except (ValueError, TypeError):
            return None

    def add_data_url(self, url: str) -> str | None:
        if not url.startswith("data:") or "," not in url:
            return None
        header, data = url[5:].split(",", 1)
        mime = header.split(";", 1)[0] or "image/png"
        return self.add_base64(data, mime) if ";base64" in header else None

    def get(self, image_id: str) -> tuple[str, bytes] | None:
        with self.lock:
            return self.images.get(image_id)


class Prompt:
    """A question to the user (approval, ask_user, confirm) that blocks the agent until answered."""

    def __init__(self, prompt_id: str, kind: str, choices: tuple[str, ...] = ()) -> None:
        self.id = prompt_id
        self.kind = kind
        self.choices = choices
        self.done = threading.Event()
        self.answer: Any = None
        self.cancelled = False

    def resolve(self, answer: Any) -> None:
        self.answer = answer
        self.done.set()

    def cancel(self) -> None:
        self.cancelled = True
        self.done.set()

    def wait(self) -> Any:
        while not self.done.wait(0.2):  # short slices: the worker stays interruptible
            pass
        if self.cancelled:
            raise KeyboardInterrupt
        return self.answer


def tool_label(tool: Any) -> dict[str, Any]:
    return {"name": getattr(tool, "name", "tool"), "category": getattr(tool, "category", "")}


class WebUI:
    """The agent UI for browser windows. All methods run on the agent's worker thread."""

    def __init__(self, app: Any, parent: str | None = None) -> None:
        self.app = app
        self.bus: EventBus = app.bus
        self.parent = parent
        self.show_reasoning = True
        self._tool: dict[str, Any] | None = None
        self._reset_response()

    def _reset_response(self) -> None:
        self._msg_id: str | None = None
        self._text = ""
        self._last_render = 0.0
        self._render_cost = 0.0
        self._streamed = False
        self._r_id: str | None = None
        self._r_text = ""
        self._r_pending = ""
        self._r_start = 0.0
        self._r_last = 0.0

    def _base(self, kind: str, item_id: str) -> dict[str, Any]:
        item: dict[str, Any] = {"type": kind, "id": item_id}
        if self.parent:
            item["parent"] = self.parent
        return item

    # -- spinner / status
    def start_spinner(self, text: str) -> None:
        self.app.set_status(text)

    def stop_spinner(self) -> None:
        self.app.set_status("")

    @contextmanager
    def spinner(self, text: str) -> Iterator[None]:
        self.start_spinner(text)
        try:
            yield
        finally:
            self.stop_spinner()

    # -- model responses
    def begin_response(self) -> None:
        self._reset_response()
        self.app.set_status("Thinking…")

    def stream_reasoning(self, text: str) -> None:
        if not text:
            return
        now = time.monotonic()
        if self._r_id is None:
            self._r_id = self.bus.new_id("r")
            self._r_start = now
            self.app.set_status("Thinking…")
        self._r_text += text
        self._r_pending += text
        if now - self._r_last >= REASONING_FLUSH:
            self._flush_reasoning(done=False)

    def _flush_reasoning(self, done: bool) -> None:
        if self._r_id is None:
            return
        self._r_last = time.monotonic()
        seconds = round(self._r_last - self._r_start)
        item = self._base("reasoning", self._r_id) | {"text": self._r_text, "done": done, "seconds": seconds}
        live = self._base("reasoning", self._r_id) | {"delta": self._r_pending, "done": done, "seconds": seconds}
        self._r_pending = ""
        self.bus.put(item, live)
        if done:
            self._r_id = None

    def stream_content(self, text: str) -> None:
        if not text:
            return
        if self._r_id is not None:
            self._flush_reasoning(done=True)
        if self._msg_id is None:
            self._msg_id = self.bus.new_id("a")
            self.app.set_status("Writing…")
        self._text += text
        self._streamed = True
        now = time.monotonic()
        if now - self._last_render >= max(0.1, self._render_cost * 4):
            self._render(streaming=True)

    def _render(self, streaming: bool) -> None:
        if self._msg_id is None:
            return
        start = time.monotonic()
        html = render_markdown(self._text)
        self._last_render = time.monotonic()
        self._render_cost = self._last_render - start
        self.bus.put(self._base("assistant", self._msg_id) | {"html": html, "text": self._text,
                                                               "streaming": streaming})

    def end_response(self) -> None:
        if self._r_id is not None:
            self._flush_reasoning(done=True)
        if self._msg_id is not None:
            self._render(streaming=False)
            self._msg_id = None

    def assistant_message(self, content: str, final: bool) -> None:
        if self._streamed or not content.strip():
            return
        self.end_response()
        self._streamed = True
        self.markdown_block(content)

    # -- tools
    def _update_tool(self, **changes: Any) -> None:
        if self._tool is None:
            return
        self._tool = self._tool | changes
        self.bus.put(self._tool)
        if self._tool.get("status") in ("pending", "approval", "running"):
            self.app.active_tools[self._tool["id"]] = self._tool
        else:
            self.app.active_tools.pop(self._tool["id"], None)

    def tool_call(self, tool: Any, args: dict[str, Any], summary: str) -> None:
        self.end_response()
        try:
            args_text = json.dumps(args, indent=2, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            args_text = str(args)
        self._tool = self._base("tool", self.bus.new_id("t")) | tool_label(tool) | {
            "summary": clip(" ".join(str(summary or "").split()), MAX_SUMMARY_CHARS),
            "args": clip(args_text, MAX_ARGS_CHARS),
            "status": "pending",
            "hidden": getattr(tool, "name", "") in HIDDEN_TOOLS,
        }
        if getattr(tool, "name", "") == "run_command":
            self._tool["command"] = clip(str(args.get("command", "")), MAX_ARGS_CHARS)
        self._update_tool()

    @contextmanager
    def tool_running(self, tool: Any, summary: str) -> Iterator[None]:
        self._update_tool(status="running", started=time.time())
        self.app.set_status(f"Running {getattr(tool, 'name', 'tool')}…")
        try:
            yield
        finally:
            self.app.set_status("")

    def tool_result(self, tool: Any, result: Any, elapsed: float) -> None:
        images = []
        for image in list(getattr(result, "images", None) or [])[:MAX_IMAGES_PER_TOOL]:
            url = self.app.images.add_base64(image.data, image.mime)
            if url:
                images.append(url)
        error = bool(getattr(result, "is_error", False))
        changes: dict[str, Any] = {"status": "error" if error else "done", "output": clip(result.output or "",
                                                                                         MAX_OUTPUT_CHARS),
                                   "elapsed": round(elapsed, 2), "images": images}
        if error:
            changes["hidden"] = False
        self._update_tool(**changes)

    def confirm_tool(self, tool: Any, args: dict[str, Any], decision: Any,
                     preview: tuple[str, str] | None) -> tuple[str, str]:
        self.end_response()
        if preview and preview[0]:
            body = code_block(clip(preview[0], 60_000), preview[1] or "text", label="preview")
        else:
            body = code_block(json.dumps(args, indent=2, ensure_ascii=False, default=str), "json",
                              label="arguments")
        prompt_id = self.bus.new_id("p")
        item = self._base("prompt", prompt_id) | tool_label(tool) | {
            "kind": "approval", "tool": self._tool["id"] if self._tool else None,
            "risk": getattr(decision, "risk", "normal"), "reason": getattr(decision, "reason", ""),
            "always": getattr(decision, "always", None), "preview": body,
        }
        self._update_tool(status="approval")
        answer = self.app.ask(item, ("yes", "no", "always") if item["always"] else ("yes", "no"))
        choice = str(answer.get("answer", "no")) if isinstance(answer, dict) else "no"
        feedback = str(answer.get("feedback", "") if isinstance(answer, dict) else "").strip()[:4000]
        if choice not in ("yes", "no", "always") or (choice == "always" and not item["always"]):
            choice = "no"
        if choice == "no":
            self._update_tool(status="denied", output=f"Denied: {feedback}" if feedback else "Denied")
        return choice, feedback

    def ask_user(self, question: str, choices: list[str]) -> str:
        self.end_response()
        item = self._base("prompt", self.bus.new_id("p")) | {
            "kind": "question", "question": render_markdown(question), "choices": [str(c) for c in choices]}
        answer = self.app.ask(item)
        return str(answer if isinstance(answer, str) else (answer or {}).get("answer", "")).strip()

    def confirm(self, question: str, default: bool = False) -> bool:
        self.end_response()
        item = self._base("prompt", self.bus.new_id("p")) | {
            "kind": "confirm", "question": question, "default": default}
        answer = self.app.ask(item)
        if isinstance(answer, dict):
            answer = answer.get("answer")
        if isinstance(answer, str):
            return answer.lower() in ("yes", "y", "true", "1")
        return bool(answer) if answer is not None else default

    def show_plan(self, plan: list[dict[str, Any]], explanation: str = "") -> None:
        self.end_response()
        steps = [{"step": str(s.get("step", "")), "status": str(s.get("status", "pending"))} for s in plan]
        item_id = f"plan-{self.parent}" if self.parent else "plan"
        self.bus.put(self._base("plan", item_id) | {"steps": steps, "explanation": explanation or ""},
                     move_to_end=True)

    # -- messages
    def _notice(self, level: str, message: str) -> None:
        self.end_response()
        item = self._base("notice", self.bus.new_id("n")) | {"level": level, "text": str(message)}
        if level == "info" and self._tool is not None and self._tool.get("status") == "running":
            item["parent"] = self._tool["id"]  # e.g. "Sub-agent started" belongs to the delegate_task row
        self.bus.put(item)

    def info(self, message: str) -> None:
        self._notice("info", message)

    def success(self, message: str) -> None:
        self._notice("success", message)

    def warn(self, message: str) -> None:
        self._notice("warn", message)

    def error(self, message: str) -> None:
        self._notice("error", message)

    def print(self, renderable: Any = "", **kwargs: Any) -> None:
        text = renderable if isinstance(renderable, str) else _plain(renderable)
        if text.strip():
            self._notice("info", text)

    def markdown_block(self, text: str) -> None:
        self.end_response()
        self.bus.put(self._base("assistant", self.bus.new_id("a")) | {
            "html": render_markdown(text), "text": text, "streaming": False})

    def usage_line(self, usage: Any, context_tokens: int, window: int, extra: str = "") -> None:
        return  # the window shows usage from the app state

    def final(self, content: str) -> None:
        return

    def emit(self, event: dict[str, Any]) -> None:
        return

    def child(self, prefix: str) -> WebUI:
        return WebUI(self.app, parent=self._tool["id"] if self._tool else self.parent)


def _plain(renderable: Any) -> str:
    import io

    from rich.console import Console

    console = Console(file=io.StringIO(), width=100, color_system=None, force_terminal=False)
    console.print(renderable)
    return console.file.getvalue()  # type: ignore[attr-defined]

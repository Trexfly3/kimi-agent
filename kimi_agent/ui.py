"""Terminal UI built on rich: live Markdown streaming, tool activity, approvals, questions and plans."""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

CATEGORY_STYLE = {
    "read": "cyan",
    "internal": "magenta",
    "network": "blue",
    "write": "yellow",
    "execute": "red",
    "gui": "bright_red",
    "external": "green",
}
RISK_STYLE = {"safe": "green", "normal": "yellow", "dangerous": "red", "catastrophic": "bold red"}
PLAN_ICONS = {"done": ("✔", "green"), "in_progress": ("▶", "bold yellow"), "pending": ("○", "dim")}
CODE_THEME = "monokai"
MAX_PREVIEW_LINES = 80


class MarkdownStream:
    """Render streamed Markdown live: finished lines are printed permanently, the last few stay in a Live area."""

    def __init__(self, console: Console, live_lines: int = 6, min_interval: float = 0.08) -> None:
        self.console = console
        self.live_lines = live_lines
        self.min_interval = min_interval
        self.text = ""
        self.printed = 0
        self.last_render = 0.0
        self.live: Live | None = None

    def _lines(self) -> list[str]:
        with self.console.capture() as capture:
            self.console.print(Markdown(self.text, code_theme=CODE_THEME))
        lines = capture.get().splitlines(keepends=True)
        while lines and not lines[-1].strip():
            lines.pop()
        return lines

    def feed(self, chunk: str) -> None:
        self.text += chunk
        if self.live is None:
            self.live = Live(Text(""), console=self.console, auto_refresh=False, transient=False,
                             vertical_overflow="visible")
            self.live.start()
        now = time.monotonic()
        if now - self.last_render >= self.min_interval:
            self.last_render = now
            self._render(final=False)

    def _render(self, final: bool) -> None:
        assert self.live is not None
        lines = self._lines()
        stable = len(lines) if final else max(0, len(lines) - self.live_lines)
        if stable > self.printed:
            self.live.console.print(Text.from_ansi("".join(lines[self.printed:stable]).rstrip("\n")))
            self.printed = stable
        rest = "".join(lines[self.printed:]).rstrip("\n")
        self.live.update(Text.from_ansi(rest) if rest else Text(""), refresh=True)

    def close(self) -> None:
        if self.live is None:
            return
        try:
            self._render(final=True)
        finally:
            self.live.update(Text(""), refresh=True)
            self.live.stop()
            self.live = None


def sessions_table(sessions: list[Any], title: str = "Saved sessions (newest first)") -> Table:
    table = Table(title=title, title_justify="left", show_edge=False)
    for column in ("id", "updated", "turns", "model", "directory", "title"):
        table.add_column(column, overflow="fold", style="bold" if column == "id" else None)
    home = str(Path.home())
    for s in sessions:
        cwd = "~" + s.cwd[len(home):] if s.cwd.startswith(home) else s.cwd
        table.add_row(s.id, s.updated[:16].replace("T", " "), str(s.turns), s.model, cwd, s.title)
    return table


class UI:
    """Interactive terminal UI. Sub-agents get a child UI with a prefix and no content streaming."""

    def __init__(self, console: Console | None = None, *, show_reasoning: bool = False, stream: bool = True,
                 quiet: bool = False, verbose: bool = False, prefix: str = "", markdown: bool = True) -> None:
        self.console = console or Console(stderr=not sys.stdout.isatty(), highlight=False)
        self.show_reasoning = show_reasoning
        self.stream = stream
        self.quiet = quiet
        self.verbose = verbose
        self.prefix = prefix
        self.markdown = markdown and self.console.is_terminal
        self._status: Any = None
        self._mode: str | None = None  # "reasoning" | "content" while a response streams
        self._md: MarkdownStream | None = None
        self._reasoning_chars = 0
        self._last_status_update = 0.0
        self._lock = threading.RLock()

    # -- spinner
    def start_spinner(self, text: str) -> None:
        if self.quiet or not self.console.is_terminal:
            return
        with self._lock:
            if self._status is None:
                self._status = self.console.status(Text(self.prefix + text, style="dim"), spinner="dots")
                self._status.start()
            else:
                self._status.update(Text(self.prefix + text, style="dim"))

    def stop_spinner(self) -> None:
        with self._lock:
            if self._status is not None:
                self._status.stop()
                self._status = None

    @contextmanager
    def spinner(self, text: str) -> Iterator[None]:
        self.start_spinner(text)
        try:
            yield
        finally:
            self.stop_spinner()

    # -- model response streaming
    def begin_response(self) -> None:
        self._mode = None
        self._reasoning_chars = 0
        self.start_spinner("Thinking…")

    def stream_reasoning(self, text: str) -> None:
        if not text:
            return
        self._reasoning_chars += len(text)
        if self.show_reasoning and self.stream and not self.quiet:
            self.stop_spinner()
            if self._mode != "reasoning":
                self._finish_mode()
                self.console.print(Text(self.prefix + "Thinking:", style="dim bold"))
                self._mode = "reasoning"
            self.console.print(Text(text, style="dim italic"), end="")
            return
        now = time.monotonic()
        if self._status is not None and now - self._last_status_update > 0.25:
            self._last_status_update = now
            self.start_spinner(f"Thinking… ({self._reasoning_chars:,} chars of reasoning)")

    def stream_content(self, text: str) -> None:
        if not text or not self.stream or self.quiet:
            return
        self.stop_spinner()
        if self._mode != "content":
            self._finish_mode()
            self._mode = "content"
            if self.markdown:
                self._md = MarkdownStream(self.console)
        if self._md is not None:
            self._md.feed(text)
        else:
            self.console.print(text, end="", markup=False, highlight=False)

    def _finish_mode(self) -> None:
        if self._mode == "reasoning":
            self.console.print()
            self.console.print()
        elif self._mode == "content":
            if self._md is not None:
                self._md.close()
                self._md = None
            else:
                self.console.print()
        self._mode = None

    def end_response(self) -> None:
        self.stop_spinner()
        self._finish_mode()

    def assistant_message(self, content: str, final: bool) -> None:
        """Called after each model response. Shows text that was not streamed live."""
        if self.stream or self.quiet or not content.strip():
            return
        if not final:
            self.console.print(Text(self.prefix + content.strip(), style="dim"))

    # -- tools
    def tool_call(self, tool: Any, args: dict[str, Any], summary: str) -> None:
        if self.quiet:
            return
        self.end_response()
        style = CATEGORY_STYLE.get(getattr(tool, "category", ""), "white")
        line = Text(self.prefix, style="dim")
        line.append("● ", style=style)
        line.append(tool.name, style="bold")
        if summary:
            line.append("  " + summary, style="dim")
        self.console.print(line, overflow="ellipsis", no_wrap=True)

    @contextmanager
    def tool_running(self, tool: Any, summary: str) -> Iterator[None]:
        # Interactive tools (ask_user, delegate_task, plan) must not run under a spinner.
        if getattr(tool, "category", "") == "internal":
            yield
            return
        with self.spinner(f"Running {tool.name}…"):
            yield

    def tool_result(self, tool: Any, result: Any, elapsed: float) -> None:
        if self.quiet:
            return
        if getattr(tool, "category", "") == "internal" and not result.is_error and tool.name != "delegate_task":
            return
        text = (result.output or "").strip()
        lines = text.splitlines() or ["(no output)"]
        limit = 12 if self.verbose else (1 if not result.is_error else 4)
        shown = lines[:limit]
        style = "red" if result.is_error else "dim"
        for i, line in enumerate(shown):
            lead = "  ⎿ " if i == 0 else "    "
            self.console.print(Text(self.prefix + lead + line[:400], style=style), overflow="ellipsis",
                               no_wrap=True)
        extra = []
        if len(lines) > limit:
            extra.append(f"+{len(lines) - limit} lines")
        if result.images:
            extra.append(f"{len(result.images)} image(s)")
        if elapsed >= 1:
            extra.append(f"{elapsed:.1f}s")
        if extra:
            self.console.print(Text(self.prefix + "    " + " · ".join(extra), style="dim"))

    def confirm_tool(self, tool: Any, args: dict[str, Any], decision: Any,
                     preview: tuple[str, str] | None) -> tuple[str, str]:
        """Ask for approval. Returns (answer, feedback): answer is yes | no | always."""
        self.end_response()
        risk = getattr(decision, "risk", "normal")
        if preview and preview[0]:
            text, lexer = preview
            lines = text.splitlines()
            if len(lines) > MAX_PREVIEW_LINES:
                text = "\n".join(lines[:MAX_PREVIEW_LINES]) + f"\n… ({len(lines) - MAX_PREVIEW_LINES} more lines)"
            body: Any = Syntax(text, lexer or "text", theme=CODE_THEME, word_wrap=True, background_color="default")
        else:
            body = Syntax(json.dumps(args, indent=2, ensure_ascii=False), "json", theme=CODE_THEME, word_wrap=True,
                          background_color="default")
        title = Text.assemble((self.prefix, "dim"), (tool.name, "bold"))
        subtitle = Text(f"{risk} · {decision.reason}" if decision.reason else risk, style=RISK_STYLE.get(risk, ""))
        self.console.print(Panel(body, title=title, subtitle=subtitle, border_style=RISK_STYLE.get(risk, "yellow"),
                                 title_align="left", subtitle_align="left"))
        careful = risk in ("dangerous", "catastrophic")
        options = "[bold]y[/] yes · [bold]n[/] no"
        if decision.always:
            options += f" · [bold]a[/] always allow {escape(decision.always)}"
        options += " · or type instructions"
        default_hint = "" if careful else " (Enter = yes)"
        while True:
            try:
                answer = self.console.input(f"{options}{default_hint}\n[bold yellow]Allow?[/] ").strip()
            except EOFError:
                return "no", ""
            lowered = answer.lower()
            if lowered in ("y", "yes") or (not answer and not careful):
                return "yes", ""
            if lowered in ("n", "no"):
                return "no", ""
            if lowered in ("a", "always") and decision.always:
                return "always", ""
            if not answer:
                continue
            return "no", answer

    def ask_user(self, question: str, choices: list[str]) -> str:
        self.end_response()
        self.console.print(Panel(Markdown(question), title=Text(self.prefix + "Question", style="bold cyan"),
                                 border_style="cyan", title_align="left"))
        for i, choice in enumerate(choices, 1):
            self.console.print(f"  [bold]{i}.[/] {escape(choice)}", highlight=False)
        try:
            answer = self.console.input("[bold cyan]> [/]").strip()
        except EOFError:
            return ""
        if choices and answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        return answer

    def confirm(self, question: str, default: bool = False) -> bool:
        self.end_response()
        hint = "Y/n" if default else "y/N"
        try:
            answer = self.console.input(f"[bold yellow]{escape(question)}[/] \\[{hint}] ").strip().lower()
        except EOFError:
            return default
        return default if not answer else answer in ("y", "yes")

    def show_plan(self, plan: list[dict[str, Any]], explanation: str = "") -> None:
        if self.quiet:
            return
        self.end_response()
        text = Text()
        if explanation:
            text.append(explanation.strip() + "\n", style="italic")
        for i, step in enumerate(plan):
            icon, style = PLAN_ICONS.get(step.get("status", "pending"), PLAN_ICONS["pending"])
            text.append(f"{icon} ", style=style)
            text.append(str(step.get("step", "")), style="strike dim" if step.get("status") == "done" else style)
            if i < len(plan) - 1:
                text.append("\n")
        self.console.print(Panel(text, title=self.prefix + "Plan", title_align="left", border_style="magenta",
                                 expand=False))

    # -- messages
    def info(self, message: str) -> None:
        if not self.quiet:
            self.end_response()
            self.console.print(Text(self.prefix + message, style="cyan"))

    def success(self, message: str) -> None:
        if not self.quiet:
            self.end_response()
            self.console.print(Text(self.prefix + message, style="green"))

    def warn(self, message: str) -> None:
        self.end_response()
        self.console.print(Text(self.prefix + "⚠ " + message, style="yellow"))

    def error(self, message: str) -> None:
        self.end_response()
        self.console.print(Text(self.prefix + "✖ " + message, style="bold red"))

    def print(self, renderable: Any = "", **kwargs: Any) -> None:
        self.end_response()
        self.console.print(renderable, **kwargs)

    def markdown_block(self, text: str) -> None:
        self.end_response()
        self.console.print(Markdown(text, code_theme=CODE_THEME) if self.markdown else text)

    def usage_line(self, usage: Any, context_tokens: int, window: int, extra: str = "") -> None:
        if self.quiet or usage is None:
            return
        parts = [f"context {context_tokens:,}/{window:,} ({100 * context_tokens / max(window, 1):.0f}%)",
                 f"in {usage.prompt_tokens:,}", f"out {usage.completion_tokens:,}"]
        if usage.cached_tokens:
            parts.append(f"cached {usage.cached_tokens:,}")
        if extra:
            parts.append(extra)
        self.console.print(Text(self.prefix + " · ".join(parts), style="dim"), justify="right")

    def final(self, content: str) -> None:
        """Deliver the final answer when it was not streamed (piped output / quiet mode)."""
        if self.stream and not self.quiet:
            return
        if content:
            sys.stdout.write(content.rstrip() + "\n")
            sys.stdout.flush()

    def child(self, prefix: str) -> UI:
        return UI(self.console, show_reasoning=False, stream=False, quiet=self.quiet, verbose=self.verbose,
                  prefix=self.prefix + prefix, markdown=self.markdown)


class JsonUI(UI):
    """Machine-readable output: one JSON event per line on stdout. Never interactive."""

    def __init__(self, prefix: str = "") -> None:
        super().__init__(Console(stderr=True, highlight=False), stream=False, quiet=True, prefix=prefix)

    def emit(self, event: dict[str, Any]) -> None:
        if self.prefix:
            event["agent"] = self.prefix.strip()
        sys.stdout.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()

    def assistant_message(self, content: str, final: bool) -> None:
        if content.strip() and not final:
            self.emit({"type": "assistant", "content": content})

    def tool_call(self, tool: Any, args: dict[str, Any], summary: str) -> None:
        self.emit({"type": "tool_call", "name": tool.name, "arguments": args})

    def tool_result(self, tool: Any, result: Any, elapsed: float) -> None:
        self.emit({"type": "tool_result", "name": tool.name, "output": result.output[:4000],
                   "is_error": result.is_error, "images": len(result.images), "seconds": round(elapsed, 3)})

    def show_plan(self, plan: list[dict[str, Any]], explanation: str = "") -> None:
        self.emit({"type": "plan", "plan": plan, "explanation": explanation})

    def info(self, message: str) -> None:
        self.emit({"type": "info", "message": message})

    def success(self, message: str) -> None:
        self.emit({"type": "info", "message": message})

    def warn(self, message: str) -> None:
        self.emit({"type": "warning", "message": message})

    def error(self, message: str) -> None:
        self.emit({"type": "error", "message": message})

    def usage_line(self, usage: Any, context_tokens: int, window: int, extra: str = "") -> None:
        return

    def final(self, content: str) -> None:
        return  # the CLI emits a "final" event including usage

    def child(self, prefix: str) -> UI:
        return JsonUI(prefix=self.prefix + prefix)

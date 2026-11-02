"""Interactive REPL: prompt_toolkit input with completion, slash commands, !shell escapes and @file mentions."""

from __future__ import annotations

import base64
import dataclasses
import logging
import os
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory as PromptHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.columns import Columns
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .attachments import expand_mentions
from .config import APPROVAL_MODES, PROVIDERS, ConfigError
from .llm import LLMError, content_text
from .prompts import INIT_PROMPT
from .session import export_markdown, make_title
from .tools.base import ToolError, one_line, truncate_middle
from .tools.files import IGNORED_DIRS
from .tools.shell import IS_WINDOWS, build_argv, execute, extract_cwd
from .ui import sessions_table

log = logging.getLogger("kimi_agent.repl")

EXIT = object()
APPROVAL_HELP = {
    "ask": "confirm every command, file change and GUI action",
    "auto": "run safe commands and edit files inside the project without asking (default)",
    "yolo": "never ask, except for catastrophic commands",
}
EFFORT_LEVELS = [("low", "fast, shallow reasoning"), ("medium", "balanced"), ("high", "deep reasoning"),
                 ("max", "maximum reasoning (Kimi)"), ("off", "use the model's default")]
SHELL_OUTPUT_LIMIT = 10_000
FILE_SCAN_LIMIT = 20_000
FILE_SCAN_TTL = 15.0
HELP_TIPS = (
    "[bold]Input[/]    Enter sends · Esc+Enter, Ctrl+J or a trailing \\ adds a line · Ctrl+X Ctrl+E opens $EDITOR · "
    "↑/↓ history · Tab completes\n"
    "[bold]Context[/]  @path attaches a file, folder, image or PDF · !command runs a shell command "
    "(its output goes with your next message)\n"
    "[bold]Control[/]  Ctrl+C interrupts the agent or clears the line · Ctrl+D or /exit quits"
)
STYLE = Style.from_dict({
    "prompt": "bold ansicyan",
    "prompt.yolo": "bold ansired",
    "continuation": "ansibrightblack",
    "bottom-toolbar": "noreverse bg:default ansibrightblack",
    "toolbar.model": "bold ansicyan",
    "toolbar.yolo": "bold ansired",
    "toolbar.ask": "ansigreen",
})


@dataclass
class Command:
    name: str
    func: Callable[..., Any]
    help: str
    usage: str = ""
    aliases: tuple[str, ...] = ()
    args: Callable[[Repl], list[tuple[str, str]]] | None = None  # argument completions: (value, description)
    path_args: str = ""  # "dir" or "any": complete the argument as a path


COMMANDS: dict[str, Command] = {}
ALIASES: dict[str, str] = {}


def command(name: str, help: str, usage: str = "", aliases: tuple[str, ...] = (),
            args: Callable[[Repl], list[tuple[str, str]]] | None = None, path_args: str = "") -> Callable:
    def register(func: Callable[..., Any]) -> Callable[..., Any]:
        COMMANDS[name] = Command(name, func, help, usage, aliases, args, path_args)
        for alias in aliases:
            ALIASES[alias] = name
        return func
    return register


def resolve_command(name: str) -> tuple[Command | None, list[str]]:
    """Exact name, alias or unique prefix. Returns (command, candidates when ambiguous)."""
    name = name.lower()
    if name in COMMANDS:
        return COMMANDS[name], []
    if name in ALIASES:
        return COMMANDS[ALIASES[name]], []
    matches = sorted({n for n in COMMANDS if n.startswith(name)}
                     | {target for alias, target in ALIASES.items() if alias.startswith(name)})
    if len(matches) == 1:
        return COMMANDS[matches[0]], []
    return None, matches


def short_path(path: Path | str) -> str:
    text, home = str(path), str(Path.home())
    if text == home or text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text


# --------------------------------------------------------------------------- completion

class ReplCompleter(Completer):
    """Completes /commands and their arguments, and @path mentions (with project-wide fuzzy matching)."""

    def __init__(self, repl: Repl) -> None:
        self.repl = repl
        self._files: list[str] = []
        self._files_root: Path | None = None
        self._files_time = 0.0

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterator[Completion]:
        text = document.text_before_cursor
        try:
            if text.startswith("/") and "\n" not in text:
                yield from self._command_completions(text)
                return
            word = text.split()[-1] if text and not text[-1].isspace() else ""
            if word.startswith("@"):
                yield from self._mention_completions(word[1:])
        except Exception:
            log.debug("completion failed", exc_info=True)

    def _command_completions(self, text: str) -> Iterator[Completion]:
        if " " not in text:
            prefix = text[1:].lower()
            for cmd in COMMANDS.values():
                if cmd.name.startswith(prefix):
                    yield Completion("/" + cmd.name, start_position=-len(text), display_meta=cmd.help)
            if prefix:
                for alias, target in ALIASES.items():
                    if alias.startswith(prefix) and alias != prefix:
                        yield Completion("/" + alias, start_position=-len(text), display_meta=f"= /{target}")
            return
        name, _, arg = text[1:].partition(" ")
        cmd, _ = resolve_command(name)
        if cmd is None:
            return
        if cmd.path_args:
            yield from self._path_completions(arg, dirs_only=cmd.path_args == "dir")
        elif cmd.args is not None and " " not in arg:
            for value, meta in cmd.args(self.repl):
                if value.lower().startswith(arg.lower()):
                    yield Completion(value, start_position=-len(arg), display_meta=meta)

    def _path_completions(self, partial: str, dirs_only: bool = False, quote: bool = False) -> Iterator[Completion]:
        cwd = self.repl.runtime.ctx.cwd
        expanded = os.path.expanduser(partial).replace("\\", "/") if not IS_WINDOWS else os.path.expanduser(partial)
        if "/" in expanded:
            head, tail = expanded.rsplit("/", 1)
            base = Path(head or "/")
            if not base.is_absolute():
                base = cwd / base
        else:
            tail, base = expanded, cwd
        try:
            entries = sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        count = 0
        for entry in entries:
            name = entry.name
            if not name.lower().startswith(tail.lower()):
                continue
            if (name.startswith(".") and not tail.startswith(".")) or (name in IGNORED_DIRS and not tail):
                continue
            is_dir = entry.is_dir()
            if dirs_only and not is_dir:
                continue
            value = name + ("/" if is_dir else "")
            if quote and " " in partial[: len(partial) - len(tail)] + value:
                yield Completion(f'"{partial[: len(partial) - len(tail)]}{value}"', start_position=-len(partial),
                                 display=value)
            else:
                yield Completion(value, start_position=-len(tail), display=value)
            count += 1
            if count >= 200:
                return

    def _mention_completions(self, partial: str) -> Iterator[Completion]:
        if partial.startswith('"'):
            return
        yield from self._path_completions(partial, quote=True)
        if not partial or "/" in partial or partial.startswith(("~", ".")):
            return
        needle = partial.lower()
        count = 0
        for rel in self._project_files():
            name = rel.rstrip("/").rsplit("/", 1)[-1].lower()
            if "/" not in rel.rstrip("/") and name.startswith(needle):
                continue  # already offered as a direct child
            if needle in name or (len(needle) >= 3 and needle in rel.lower()):
                yield Completion(f'"{rel}"' if " " in rel else rel, start_position=-len(partial), display=rel)
                count += 1
                if count >= 50:
                    return

    def _project_files(self) -> list[str]:
        root = self.repl.runtime.ctx.cwd
        now = time.monotonic()
        if self._files_root == root and now - self._files_time < FILE_SCAN_TTL:
            return self._files
        files: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.startswith("."))
            rel_dir = os.path.relpath(dirpath, root)
            prefix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
            files.extend(prefix + d + "/" for d in dirnames)
            files.extend(prefix + f for f in sorted(filenames))
            if len(files) >= FILE_SCAN_LIMIT:
                break
        self._files, self._files_root, self._files_time = files, root, now
        return files


# --------------------------------------------------------------------------- the REPL

class Repl:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.ui = runtime.ui
        self.settings = runtime.settings
        self.pending_shell: list[str] = []
        self.last_answer = ""
        self.models: list[str] = []
        self.completer = ReplCompleter(self)
        self._session: PromptSession | None = None
        self._last_interrupt = 0.0
        self._context_tokens = 0
        self._update_context()

    @property
    def agent(self) -> Any:
        return self.runtime.agent

    @property
    def ctx(self) -> Any:
        return self.runtime.ctx

    # -- prompt
    def prompt_session(self) -> PromptSession:
        if self._session is None:
            history = self.settings.data_path / "history"
            history.parent.mkdir(parents=True, exist_ok=True)
            self._session = PromptSession(
                history=PromptHistory(str(history)),
                completer=self.completer,
                complete_in_thread=True,
                complete_while_typing=True,
                auto_suggest=AutoSuggestFromHistory(),
                key_bindings=self._key_bindings(),
                multiline=True,
                prompt_continuation=lambda width, line, wrap: [("class:continuation", "·".rjust(width - 1) + " ")],
                bottom_toolbar=self._toolbar,
                style=STYLE,
                enable_open_in_editor=True,
                reserve_space_for_menu=6,
            )
        return self._session

    @staticmethod
    def _key_bindings() -> KeyBindings:
        kb = KeyBindings()

        @kb.add("enter")
        def _enter(event: Any) -> None:
            buf = event.current_buffer
            state = buf.complete_state
            if state is not None and state.current_completion is not None:
                buf.apply_completion(state.current_completion)
                return
            doc = buf.document
            if doc.is_cursor_at_the_end and doc.text.endswith("\\"):
                buf.delete_before_cursor(1)
                buf.insert_text("\n")
                return
            buf.validate_and_handle()

        @kb.add("escape", "enter")
        @kb.add("c-j")
        def _newline(event: Any) -> None:
            event.current_buffer.insert_text("\n")

        @kb.add("c-c")
        def _interrupt(event: Any) -> None:
            buf = event.current_buffer
            if buf.text:
                buf.reset()
            else:
                event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

        return kb

    def _prompt_message(self) -> list[tuple[str, str]]:
        style = "class:prompt.yolo" if self.settings.approval == "yolo" else "class:prompt"
        return [(style, "❯ ")]

    def _toolbar(self) -> list[tuple[str, str]]:
        s = self.settings
        window = self.agent.context.window if self.agent else 0
        pct = 100 * self._context_tokens / window if window else 0
        approval_style = {"yolo": "class:toolbar.yolo", "ask": "class:toolbar.ask"}.get(s.approval, "")
        parts = [("class:toolbar.model", f" {s.model}"), ("", f" · {s.provider} · "),
                 (approval_style, s.approval), ("", f" · context {pct:.0f}% · {short_path(self.ctx.cwd)}")]
        if self.pending_shell:
            parts.append(("", f" · {len(self.pending_shell)} shell output(s) queued"))
        return parts

    def read(self) -> str:
        return self.prompt_session().prompt(self._prompt_message)

    def _update_context(self) -> None:
        if self.agent is not None:
            try:
                self._context_tokens = self.agent.context_tokens()
            except Exception:
                log.debug("context estimate failed", exc_info=True)

    # -- main loop
    def banner(self) -> None:
        s = self.settings
        text = Text()
        text.append("✻ Kimi Agent ", style="bold cyan")
        text.append(f"{__version__}\n", style="dim")
        text.append("  model     ", style="dim")
        text.append(s.model, style="bold")
        text.append(f" · {s.provider} · {self.agent.context.window:,} tokens\n", style="dim")
        text.append("  approval  ", style="dim")
        text.append(s.approval, style="bold red" if s.approval == "yolo" else "bold")
        text.append(f" · {len(self.runtime.registry)} tools", style="dim")
        if self.runtime.mcp is not None:
            servers = [f"{row['name']} {'✓' if row['state'] == 'running' else '✖'}"
                       for row in self.runtime.mcp.status() if row["state"] != "disabled"]
            if servers:
                text.append(" · MCP: " + ", ".join(servers), style="dim")
        text.append("\n  cwd       ", style="dim")
        text.append(short_path(self.ctx.cwd) + "\n\n")
        text.append("  /help commands · @file attach · !cmd shell · Esc+Enter new line", style="dim")
        self.ui.print(Panel(text, border_style="cyan", expand=False))
        if s.approval == "yolo":
            self.ui.warn("YOLO mode: the agent runs commands and edits files without asking "
                         "(catastrophic commands still need confirmation).")
        if s.computer_use and "screenshot" in self.runtime.registry:
            self.ui.warn("Computer use is on: the agent can see your screen and control the mouse and keyboard.")

    def run(self, initial: str | None = None) -> int:
        self.banner()
        if initial:
            self.handle(initial)
        while True:
            try:
                text = self.read()
            except KeyboardInterrupt:
                now = time.monotonic()
                if now - self._last_interrupt < 2.0:
                    break
                self._last_interrupt = now
                self.ui.print(Text("(Press Ctrl+C again or Ctrl+D to exit.)", style="dim"))
                continue
            except EOFError:
                break
            if self.handle(text) is EXIT:
                break
        if self.runtime.save_session() is not None:
            self.ui.print(Text(f"Session saved. Resume it with: kimi-agent -r {self.runtime.session.id}",
                               style="dim"))
        return 0

    def handle(self, text: str) -> Any:
        """Process one line of input: a /command, a !shell command or a message for the agent."""
        stripped = text.strip()
        if not stripped:
            return None
        if stripped.startswith("/"):
            parts = stripped[1:].split(None, 1)
            name = parts[0] if parts else "help"
            arg = parts[1].strip() if len(parts) > 1 else ""
            cmd, candidates = resolve_command(name)
            if cmd is not None:
                try:
                    return cmd.func(self, arg)
                except KeyboardInterrupt:
                    self.ui.warn("Interrupted.")
                except (ToolError, ConfigError, LLMError, OSError, ValueError) as exc:
                    self.ui.error(f"/{cmd.name}: {exc}")
                except Exception as exc:
                    log.exception("command /%s failed", cmd.name)
                    self.ui.error(f"/{cmd.name} failed: {exc!r}")
                return None
            if candidates:
                self.ui.error(f"Ambiguous command /{name}: " + ", ".join("/" + c for c in candidates))
                return None
            if "/" not in name:  # an absolute path such as /etc/hosts is a message, not a command
                self.ui.error(f"Unknown command /{name}. Type /help for the list of commands.")
                return None
        if stripped.startswith("!"):
            self.shell(stripped[1:].strip())
            return None
        self.submit(stripped)
        return None

    def submit(self, text: str) -> None:
        content, notes = expand_mentions(text, self.ctx, bool(self.settings.vision))
        for note in notes:
            self.ui.info(note)
        if self.pending_shell:
            prefix = "\n\n".join(self.pending_shell)
            self.pending_shell.clear()
            if isinstance(content, str):
                content = f"{prefix}\n\n{content}"
            else:
                content = [{"type": "text", "text": prefix}] + list(content)
        self.execute(content)

    def execute(self, content: Any) -> None:
        agent = self.agent
        started = time.monotonic()
        try:
            answer = agent.run(content)
        except KeyboardInterrupt:
            self.ui.stop_spinner()
            self.ui.warn("Interrupted. Send a message to continue, or /undo to drop the request.")
        except LLMError as exc:
            self.ui.error(str(exc))
        except Exception as exc:
            log.exception("agent run failed")
            self.ui.error(f"Unexpected error: {exc!r} (details in {self.settings.data_path / 'logs'})")
        else:
            self.last_answer = answer
            self.ui.final(answer)
            self._update_context()
            self.ui.usage_line(agent.usage, self._context_tokens, agent.context.window,
                               f"{time.monotonic() - started:.0f}s")
        finally:
            self._update_context()
            try:
                self.runtime.save_session()
            except OSError as exc:
                self.ui.warn(f"Could not save the session: {exc}")

    def shell(self, command: str) -> None:
        """!command: run a shell command for the user; the output is queued for the next message."""
        if not command:
            self.ui.info("Usage: !<command> runs a shell command; its output is sent with your next message.")
            return
        ctx = self.ctx
        track = not IS_WINDOWS
        try:
            with self.ui.spinner(f"$ {one_line(command, 60)}"):
                result = execute(build_argv(self.settings, command, track), ctx.cwd,
                                 float(self.settings.command_timeout))
        except KeyboardInterrupt:
            self.ui.warn("Interrupted.")
            return
        except OSError as exc:
            self.ui.error(f"Could not run the command: {exc}")
            return
        output = result.output
        if track:
            output, new_cwd = extract_cwd(output)
            if new_cwd and Path(new_cwd).is_dir() and Path(new_cwd) != ctx.cwd:
                ctx.cwd = Path(new_cwd)
                self.ui.info(f"Working directory: {new_cwd}")
        output = output.rstrip()
        if output:
            self.ui.print(Text(output), highlight=False)
        status = "timed out" if result.timed_out else f"exit code {result.exit_code}"
        self.ui.print(Text(f"({status}; the output will be sent with your next message)", style="dim"))
        self.pending_shell.append(
            f"<shell_command>\n$ {command}\n{truncate_middle(output, SHELL_OUTPUT_LIMIT)}\n({status})\n</shell_command>")

    # -- helpers
    def _last_assistant_text(self) -> str:
        for message in reversed(self.agent.messages):
            if message.get("role") == "assistant":
                text = content_text(message.get("content"))
                if text.strip():
                    return text
        return ""

    def _undo(self) -> dict[str, Any] | None:
        message = self.agent.pop_turn()
        if message is None:
            self.ui.info("Nothing to undo.")
            return None
        restored: list[str] = []
        turn = message.get("_file_turn")
        if turn is not None and self.ctx.file_history is not None:
            restored = self.ctx.file_history.undo_turn(int(turn))
        self.ui.success("Removed the last request: " + one_line(content_text(message.get("content")), 80))
        for line in restored:
            self.ui.info("  " + line)
        self.ui.print(Text("Note: effects of shell commands and other tools are not undone.", style="dim"))
        self.last_answer = self._last_assistant_text()
        self._update_context()
        self.runtime.save_session()
        return message

    # ------------------------------------------------------------------ argument completions
    def _provider_choices(self) -> list[tuple[str, str]]:
        choices = [(name, p.description or p.base_url) for name, p in PROVIDERS.items()]
        choices += [(name, str(cfg.get("base_url", "custom provider")))
                    for name, cfg in (self.settings.providers or {}).items() if name not in PROVIDERS]
        return choices

    def _model_choices(self) -> list[tuple[str, str]]:
        names = list(dict.fromkeys(self.models + [p.model for p in PROVIDERS.values() if p.model]))
        return [(name, "current" if name == self.settings.model else "") for name in names]

    def _session_choices(self) -> list[tuple[str, str]]:
        return [(s.id, one_line(s.title, 50)) for s in self.runtime.sessions.list(limit=20)]

    def _process_choices(self) -> list[tuple[str, str]]:
        return [("stop", "stop a background process")] + [
            (bp.id, one_line(bp.command, 50)) for bp in self.runtime.processes.list()]

    # ------------------------------------------------------------------ session commands
    @command("help", "show commands and shortcuts", aliases=("?",))
    def cmd_help(self, arg: str) -> None:
        table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
        table.add_column(style="bold cyan", no_wrap=True)
        table.add_column()
        for cmd in COMMANDS.values():
            usage = f"/{cmd.name}" + (f" {cmd.usage}" if cmd.usage else "")
            extra = f" [dim](also {', '.join('/' + a for a in cmd.aliases)})[/]" if cmd.aliases else ""
            table.add_row(escape(usage), escape(cmd.help) + extra)
        self.ui.print(table)
        self.ui.print()
        self.ui.print(Text.from_markup(HELP_TIPS))

    @command("exit", "save the session and quit", aliases=("quit", "q"))
    def cmd_exit(self, arg: str) -> Any:
        return EXIT

    @command("new", "start a new conversation (the current one is saved)", aliases=("clear", "reset"))
    def cmd_new(self, arg: str) -> None:
        self.runtime.new_session()
        self.last_answer = ""
        self.pending_shell.clear()
        self._update_context()
        self.ui.success(f"New session {self.runtime.session.id}. The previous conversation was saved (/resume).")

    @command("sessions", "list saved sessions")
    def cmd_sessions(self, arg: str) -> None:
        sessions = self.runtime.sessions.list(limit=20)
        if not sessions:
            self.ui.info("No saved sessions yet.")
            return
        self.ui.print(sessions_table(sessions))

    @command("resume", "resume a saved session", usage="[id]", args=lambda r: r._session_choices())
    def cmd_resume(self, arg: str) -> None:
        if not arg:
            sessions = [s for s in self.runtime.sessions.list(limit=10) if s.id != self.runtime.session.id]
            if not sessions:
                self.ui.info("No other saved sessions.")
                return
            choices = [f"{s.id} · {s.updated[:16].replace('T', ' ')} · {one_line(s.title, 60)}" for s in sessions]
            answer = self.ui.ask_user("Which session? (number or id; Enter cancels)", choices)
            if not answer.strip():
                return
            arg = answer.split()[0]
        session = self.runtime.resume(arg)
        self.last_answer = self._last_assistant_text()
        self.pending_shell.clear()
        self._update_context()
        self.ui.success(f"Resumed session {session.id} ({session.turns} requests): {session.title}")
        if self.last_answer:
            self.ui.print(Text("Last answer:", style="dim"))
            self.ui.markdown_block(truncate_middle(self.last_answer, 3_000))

    @command("export", "export the conversation as Markdown", usage="[file]", path_args="any")
    def cmd_export(self, arg: str) -> None:
        messages = [m for m in self.agent.messages if m.get("role") != "system"]
        if not any(m.get("role") == "user" for m in messages):
            self.ui.info("Nothing to export yet.")
            return
        path = self.ctx.resolve(arg) if arg else self.ctx.cwd / f"kimi-session-{self.runtime.session.id}.md"
        title = self.runtime.session.title or make_title(messages) or "Kimi Agent session"
        path.write_text(export_markdown(messages, title), encoding="utf-8")
        self.ui.success(f"Exported the conversation to {path}")

    @command("copy", "copy the last answer to the clipboard")
    def cmd_copy(self, arg: str) -> None:
        text = self.last_answer or self._last_assistant_text()
        if not text:
            self.ui.info("Nothing to copy yet.")
            return
        try:
            import pyperclip

            pyperclip.copy(text)
            self.ui.success("Copied the last answer to the clipboard.")
            return
        except Exception:
            pass
        sys.stdout.write("\033]52;c;" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "\a")
        sys.stdout.flush()
        self.ui.success("Sent the last answer to the terminal clipboard (OSC 52).")

    # ------------------------------------------------------------------ conversation commands
    @command("compact", "summarize the conversation to free context", usage="[focus]")
    def cmd_compact(self, arg: str) -> None:
        if not self.agent.compact(arg):
            self.ui.info("Nothing to compact yet.")
        self._update_context()
        self.runtime.save_session()

    @command("undo", "remove the last request and revert its file changes")
    def cmd_undo(self, arg: str) -> None:
        self._undo()

    @command("retry", "undo the last request and run it again")
    def cmd_retry(self, arg: str) -> None:
        message = self._undo()
        if message is not None:
            self.execute(message.get("content"))

    @command("init", "analyze the project and write an AGENTS.md for future sessions")
    def cmd_init(self, arg: str) -> None:
        self.execute(INIT_PROMPT + (f"\n\nAdditional instructions from the user: {arg}" if arg else ""))

    @command("plan", "show the current plan")
    def cmd_plan(self, arg: str) -> None:
        if self.ctx.plan:
            self.ui.show_plan(self.ctx.plan)
        else:
            self.ui.info("No plan yet. The agent makes one for multi-step tasks.")

    @command("system", "show the system prompt")
    def cmd_system(self, arg: str) -> None:
        prompt = content_text(self.agent.messages[0].get("content"))
        self.ui.print(Panel(Text(prompt), title=f"System prompt (~{len(prompt) // 4:,} tokens)",
                            title_align="left", border_style="dim"))

    @command("tokens", "show token usage and context size", aliases=("usage", "context"))
    def cmd_tokens(self, arg: str) -> None:
        self._update_context()
        u, window = self.agent.usage, self.agent.context.window
        table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
        table.add_column(style="dim")
        table.add_column()
        table.add_row("context", f"~{self._context_tokens:,} / {window:,} tokens "
                                 f"({100 * self._context_tokens / max(window, 1):.1f}%), compacts at "
                                 f"{self.settings.compact_threshold:.0%}")
        table.add_row("messages", str(len(self.agent.messages)))
        table.add_row("input", f"{u.prompt_tokens:,} tokens" + (f" ({u.cached_tokens:,} cached)" if u.cached_tokens else ""))
        table.add_row("output", f"{u.completion_tokens:,} tokens"
                                + (f" ({u.reasoning_tokens:,} reasoning)" if u.reasoning_tokens else ""))
        self.ui.print(table)

    # ------------------------------------------------------------------ model commands
    @command("model", "show or switch the model", usage="[name]", args=lambda r: r._model_choices())
    def cmd_model(self, arg: str) -> None:
        s = self.settings
        if not arg:
            self.ui.info(f"Model {s.model} · provider {s.provider} · {s.base_url}")
            self.ui.print(Text("Switch with /model NAME; list available models with /models.", style="dim"))
            return
        self.runtime.set_model(arg)
        self._update_context()
        self.ui.success(f"Model: {arg}")

    @command("models", "list the models offered by the provider")
    def cmd_models(self, arg: str) -> None:
        with self.ui.spinner("Fetching models…"):
            models = self.runtime.llm.list_models()
        self.models = sorted(models)
        if not models:
            self.ui.info("The provider returned no models.")
            return
        items = [Text(m, style="bold green" if m == self.settings.model else "") for m in self.models]
        self.ui.print(Columns(items, padding=(0, 3)))
        self.ui.print(Text(f"{len(models)} models · switch with /model NAME", style="dim"))

    @command("provider", "show or switch the provider", usage="[name [model]]", args=lambda r: r._provider_choices())
    def cmd_provider(self, arg: str) -> None:
        s = self.settings
        if not arg:
            table = Table(show_edge=False)
            for column in ("", "provider", "default model", "base URL", "key"):
                table.add_column(column)
            for name, preset in PROVIDERS.items():
                key = ("✓" if any(os.environ.get(env) for env in preset.key_envs) else "–") if preset.needs_key else ""
                table.add_row("●" if name == s.provider else "", name, preset.model or "-",
                              preset.base_url or "(set base_url)", key)
            for name, cfg in (s.providers or {}).items():
                if name not in PROVIDERS:
                    table.add_row("●" if name == s.provider else "", name, str(cfg.get("model", "-")),
                                  str(cfg.get("base_url", "")), "")
            self.ui.print(table)
            self.ui.print(Text("Switch with /provider NAME [MODEL].", style="dim"))
            return
        name, _, model = arg.partition(" ")
        if name not in PROVIDERS and name not in (s.providers or {}):
            self.ui.error(f"Unknown provider {name!r}. Run /provider to see the list.")
            return
        self.runtime.set_provider(name, model.strip() or None)
        self._update_context()
        self.ui.success(f"Provider {s.provider} · model {s.model} · {s.base_url}")

    @command("effort", "show or set the reasoning effort", usage="[low|medium|high|max|off]",
             args=lambda r: EFFORT_LEVELS)
    def cmd_effort(self, arg: str) -> None:
        if not arg:
            self.ui.info(f"Reasoning effort: {self.settings.reasoning_effort or 'model default'}")
            return
        value = arg.lower()
        self.settings.reasoning_effort = None if value in ("off", "default", "none") else value
        self.runtime.llm.reconfigure()
        self.ui.success(f"Reasoning effort: {self.settings.reasoning_effort or 'model default'}")

    @command("reasoning", "show or hide the model's thinking", usage="[on|off]",
             args=lambda r: [("on", "show reasoning"), ("off", "hide reasoning")])
    def cmd_reasoning(self, arg: str) -> None:
        value = (not self.settings.show_reasoning) if not arg else arg.lower() in ("on", "yes", "true", "1", "show")
        self.settings.show_reasoning = value
        self.ui.show_reasoning = value
        self.ui.success(f"Reasoning display {'on' if value else 'off'}.")

    # ------------------------------------------------------------------ tools and safety
    @command("approval", "show or set the approval mode", usage="[ask|auto|yolo]",
             args=lambda r: list(APPROVAL_HELP.items()))
    def cmd_approval(self, arg: str) -> None:
        if not arg:
            self.ui.info(self.runtime.policy.describe())
            for mode in APPROVAL_MODES:
                marker = "●" if mode == self.settings.approval else "○"
                self.ui.print(Text(f"  {marker} {mode:<5} {APPROVAL_HELP.get(mode, '')}"))
            return
        mode = arg.lower()
        if mode not in APPROVAL_MODES:
            self.ui.error(f"Unknown approval mode {mode!r}; use one of: {', '.join(APPROVAL_MODES)}.")
            return
        self.settings.approval = mode
        self.agent.refresh_system_prompt()
        self.ui.success(f"Approval mode: {mode} ({APPROVAL_HELP.get(mode, '')})")
        if mode == "yolo":
            self.ui.warn("The agent will now run commands and change files without asking.")

    @command("tools", "list the available tools")
    def cmd_tools(self, arg: str) -> None:
        table = Table(show_edge=False)
        for column in ("tool", "kind", "description"):
            table.add_column(column, overflow="fold" if column == "description" else "ellipsis")
        for tool in self.runtime.registry:
            first = (tool.description or "").strip().splitlines()
            table.add_row(tool.name, tool.category, one_line(first[0] if first else "", 90))
        self.ui.print(table)
        self.ui.print(Text(f"{len(self.runtime.registry)} tools · approval mode {self.settings.approval}",
                           style="dim"))

    @command("mcp", "show MCP servers, or restart them", usage="[reload]",
             args=lambda r: [("reload", "restart all MCP servers and reload their tools")])
    def cmd_mcp(self, arg: str) -> None:
        mcp = self.runtime.mcp
        if mcp is None:
            self.ui.info("No MCP servers are running. Add [mcp_servers.NAME] sections to "
                         f"{self.settings.data_path / 'config.toml'} (see kimi-agent --init-config).")
            return
        if arg.lower() in ("reload", "restart"):
            with self.ui.spinner("Restarting MCP servers…"):
                mcp.close()
                errors = mcp.start()
            for error in errors:
                self.ui.warn(error)
            self.runtime.rebuild_tools()
            self._update_context()
        table = Table(show_edge=False)
        for column in ("server", "state", "transport", "tools", "trusted", "details"):
            table.add_column(column, overflow="fold")
        for row in mcp.status():
            state_style = {"running": "green", "failed": "red"}.get(row["state"], "dim")
            table.add_row(row["name"], f"[{state_style}]{row['state']}[/]", row["kind"], str(row["tools"]),
                          "yes" if row["trust"] else "no", escape(row["error"] or row["server"] or ""))
        self.ui.print(table)

    @command("ps", "list background processes, or stop one", usage="[stop ID]", args=lambda r: r._process_choices())
    def cmd_ps(self, arg: str) -> None:
        processes = self.runtime.processes
        words = arg.split()
        if words and words[0] in ("stop", "kill"):
            if len(words) < 2:
                self.ui.error("Usage: /ps stop ID")
                return
            bp = processes.stop(words[1])
            self.ui.success(f"Stopped {bp.id}: {bp.command} ({bp.status()})")
            return
        procs = processes.list()
        if not procs:
            self.ui.info("No background processes.")
            return
        table = Table(show_edge=False)
        for column in ("id", "status", "running for", "command", "directory"):
            table.add_column(column, overflow="fold")
        for bp in procs:
            minutes, seconds = divmod(int(time.time() - bp.started), 60)
            table.add_row(bp.id, bp.status(), f"{minutes}m{seconds:02d}s" if bp.running else "-",
                          bp.command, short_path(bp.cwd))
        self.ui.print(table)

    @command("memory", "show or edit persistent memory", usage="[add TEXT | forget TEXT | clear]",
             args=lambda r: [("add", "remember a fact"), ("forget", "forget facts containing TEXT"),
                             ("clear", "forget everything")])
    def cmd_memory(self, arg: str) -> None:
        memory = self.runtime.memory
        if memory is None:
            self.ui.info("Memory is disabled (memory = false in the config).")
            return
        sub, _, rest = arg.partition(" ")
        sub, rest = sub.lower(), rest.strip()
        if not sub:
            facts = memory.facts()
            if not facts:
                self.ui.info(f"Nothing remembered yet. Stored in {memory.path}")
                return
            for i, fact in enumerate(facts, 1):
                self.ui.print(Text(f"{i:>3}. {fact}"))
            self.ui.print(Text(f"Stored in {memory.path}", style="dim"))
            return
        if sub == "add" and rest:
            if memory.add(rest):
                self.ui.success(f"Remembered: {rest}")
            else:
                self.ui.info("Already remembered.")
        elif sub in ("forget", "remove", "rm") and rest:
            removed = memory.remove(rest)
            if removed:
                for fact in removed:
                    self.ui.success(f"Forgot: {fact}")
            else:
                self.ui.info(f"No memory contains {rest!r}.")
        elif sub == "clear":
            if not self.ui.confirm("Delete all memories?", default=False):
                return
            memory.clear()
            self.ui.success("Memory cleared.")
        else:
            self.ui.error("Usage: /memory [add TEXT | forget TEXT | clear]")
            return
        self.agent.refresh_system_prompt()

    # ------------------------------------------------------------------ environment
    @command("cd", "show or change the working directory", usage="[dir]", path_args="dir")
    def cmd_cd(self, arg: str) -> None:
        if not arg:
            self.ui.info(f"Working directory: {self.ctx.cwd}")
            return
        target = self.runtime.change_directory(arg.strip().strip('"'))
        self._update_context()
        self.ui.success(f"Working directory: {target}")

    @command("config", "show the effective settings and file locations")
    def cmd_config(self, arg: str) -> None:
        s = self.settings
        data = dataclasses.asdict(s)
        if data.get("api_key"):
            key = str(data["api_key"])
            data["api_key"] = key if key == "not-needed" else (key[:4] + "…" + key[-2:] if len(key) > 10 else "***")
        for name in ("mcp_servers", "providers"):
            data[name] = ", ".join(data.get(name) or {}) or "-"
        data["extra_headers"] = {k: "***" for k in data.get("extra_headers") or {}}  # often carry credentials
        table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
        table.add_column(style="dim")
        table.add_column(overflow="fold")
        for key, value in data.items():
            table.add_row(key, escape(repr(value) if not isinstance(value, str) else value or "''"))
        self.ui.print(table)
        paths = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
        paths.add_column(style="dim")
        paths.add_column()
        for label, path in (("config file", s.data_path / "config.toml"), ("sessions", s.data_path / "sessions"),
                            ("log", s.data_path / "logs" / "kimi-agent.log"),
                            ("memory", s.data_path / "memory.md"), ("history", s.data_path / "history")):
            paths.add_row(label, str(path) + ("" if path.exists() else " (not created yet)"))
        self.ui.print()
        self.ui.print(paths)

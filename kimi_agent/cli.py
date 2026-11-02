"""Command-line interface: one-shot tasks and an interactive REPL with slash commands."""

from __future__ import annotations

import argparse
import dataclasses
import logging
import logging.handlers
import sys
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__
from .attachments import expand_mentions
from .config import (
    APPROVAL_MODES,
    PROVIDERS,
    TOOL_MODES,
    ConfigError,
    default_data_dir,
    load_settings,
    missing_api_key_message,
)
from .llm import LLMError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kimi-agent",
        description="An autonomous AI agent with full computer access, powered by Kimi or any "
                    "OpenAI-compatible model.",
        epilog="Examples:\n  kimi-agent                              # interactive session\n"
               "  kimi-agent \"fix the failing tests\"      # one-shot task\n"
               "  git diff | kimi-agent \"review this\"     # piped input\n"
               "  kimi-agent -p ollama -m qwen3:8b        # local model\n"
               "  kimi-agent -c                           # continue the last session\n"
               "  kimi-agent --gui                        # open the desktop app",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("prompt", nargs="*", help="task to run; omit to start an interactive session")
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="stay in the interactive session after running the prompt")

    model = parser.add_argument_group("model")
    model.add_argument("-p", "--provider", help="provider preset (see --list-providers)")
    model.add_argument("-m", "--model", help="model name")
    model.add_argument("--base-url", help="OpenAI-compatible API base URL")
    model.add_argument("--api-key", help="API key (prefer environment variables)")
    model.add_argument("--tool-mode", choices=TOOL_MODES, help="native or prompt-based (text) tool calling")
    model.add_argument("--vision", action=argparse.BooleanOptionalAction, default=None,
                       help="send images to the model")
    model.add_argument("--temperature", type=float)
    model.add_argument("--max-tokens", type=int, help="maximum output tokens per response")
    model.add_argument("--reasoning-effort", help="reasoning effort for models that support it (e.g. low, high)")
    model.add_argument("--context-window", type=int, help="context window size in tokens")
    model.add_argument("--no-stream", action="store_true", help="disable streaming responses")

    agent = parser.add_argument_group("agent")
    agent.add_argument("-a", "--approval", choices=APPROVAL_MODES, help="approval mode (default: auto)")
    agent.add_argument("-y", "--yolo", action="store_true",
                       help="approve every action automatically (except catastrophic commands)")
    agent.add_argument("-C", "--cd", dest="workdir", metavar="DIR", help="working directory")
    agent.add_argument("--computer", action="store_true", help="enable screen, mouse and keyboard control")
    agent.add_argument("--no-web", action="store_true", help="disable web search and fetch")
    agent.add_argument("--no-mcp", action="store_true", help="do not start MCP servers")
    agent.add_argument("--no-memory", action="store_true", help="disable persistent memory")
    agent.add_argument("--max-steps", type=int, help="maximum tool-calling steps per request")
    agent.add_argument("--show-reasoning", action="store_true", help="print the model's reasoning")

    sessions = parser.add_argument_group("sessions")
    sessions.add_argument("-c", "--continue", dest="continue_session", action="store_true",
                          help="continue the most recent session")
    sessions.add_argument("-r", "--resume", metavar="ID", help="resume a saved session by id (or prefix)")
    sessions.add_argument("--list-sessions", action="store_true", help="list saved sessions and exit")

    desktop = parser.add_argument_group("desktop app")
    desktop.add_argument("--gui", action="store_true", help="open the desktop app (a window in your browser)")
    desktop.add_argument("--no-browser", action="store_true",
                         help="with --gui: print a sign-in link instead of opening a window")
    desktop.add_argument("--port", type=int, help="with --gui: port of the local server (default: 41753)")
    desktop.add_argument("--install-shortcuts", action="store_true",
                         help="add the desktop app to the Start menu/Applications and the desktop, then exit")
    desktop.add_argument("--remove-shortcuts", action="store_true", help="remove those shortcuts and exit")
    desktop.add_argument("--quit-gui", action="store_true", help="close the running desktop app and exit")

    other = parser.add_argument_group("other")
    other.add_argument("--config", metavar="FILE", help="additional config file")
    other.add_argument("--init-config", action="store_true", help="write an example config file and exit")
    other.add_argument("--list-providers", action="store_true", help="list provider presets and exit")
    other.add_argument("--json", action="store_true", help="print JSON events instead of text (non-interactive)")
    other.add_argument("-q", "--quiet", action="store_true", help="only print the final answer")
    other.add_argument("-v", "--verbose", action="store_true", help="show more tool output")
    other.add_argument("--debug", action="store_true", help="write a detailed debug log")
    other.add_argument("--version", action="version", version=f"kimi-agent {__version__}")
    return parser


def overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {
        "provider": args.provider,
        "model": args.model,
        "base_url": args.base_url,
        "api_key": args.api_key,
        "tool_mode": args.tool_mode,
        "vision": args.vision,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "reasoning_effort": args.reasoning_effort,
        "context_window": args.context_window,
        "approval": "yolo" if args.yolo else args.approval,
        "workdir": args.workdir,
        "max_steps": args.max_steps,
    }
    if args.no_stream:
        overrides["stream"] = False
    if args.computer:
        overrides["computer_use"] = True
    if args.no_web:
        overrides["web"] = False
    if args.no_memory:
        overrides["memory"] = False
    if args.show_reasoning:
        overrides["show_reasoning"] = True
    if args.debug:
        overrides["debug"] = True
    return overrides


def setup_logging(settings: Any) -> Path:
    log_dir = settings.data_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "kimi-agent.log"
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger("kimi_agent")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.DEBUG if settings.debug else logging.INFO)
    logger.propagate = False
    return path


# --------------------------------------------------------------------------- entry points

def print_providers() -> None:
    from rich.console import Console
    from rich.table import Table

    table = Table(title="Provider presets", show_lines=False)
    for column in ("name", "default model", "base URL", "API key env", "context"):
        table.add_column(column, overflow="fold", no_wrap=column in ("name", "context"))
    for p in PROVIDERS.values():
        table.add_row(p.name, p.model or "-", p.base_url or "(--base-url)", " / ".join(p.key_envs) or "-",
                      f"{p.context_window:,}")
    Console().print(table)


def print_sessions(store: Any) -> None:
    from rich.console import Console

    from .ui import sessions_table

    sessions = store.list(limit=30)
    console = Console()
    console.print(sessions_table(sessions) if sessions else "No saved sessions.")


def init_config(data_dir: Path) -> int:
    target = data_dir / "config.toml"
    if target.exists():
        print(f"{target} already exists; not overwriting.", file=sys.stderr)
        return 1
    template = resources.files("kimi_agent").joinpath("data/config.example.toml").read_text(encoding="utf-8")
    data_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(template, encoding="utf-8")
    print(f"Wrote {target}")
    return 0


def read_stdin_prompt(prompt: str) -> str:
    data = sys.stdin.read()
    if not data.strip():
        return prompt
    if not prompt:
        return data
    return f"{prompt}\n\n<stdin>\n{data.rstrip()}\n</stdin>"


def run_once(runtime: Any, prompt: str, args: argparse.Namespace) -> int:
    ui = runtime.ui
    agent = runtime.agent
    content, notes = expand_mentions(prompt, runtime.ctx, bool(runtime.settings.vision))
    for note in notes:
        ui.info(note)
    try:
        result = agent.run(content)
    except KeyboardInterrupt:
        ui.warn("Interrupted.")
        return 130
    except LLMError as exc:
        ui.error(str(exc))
        if args.json:
            ui.emit({"type": "final", "error": str(exc), "session": runtime.session.id})
        return 1
    if args.json:
        ui.emit({"type": "final", "content": result, "usage": dataclasses.asdict(agent.usage),
                 "session": runtime.session.id})
    else:
        ui.final(result)
        ui.usage_line(agent.usage, agent.context_tokens(), agent.context.window, f"session {runtime.session.id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_providers:
        print_providers()
        return 0
    if args.init_config:
        return init_config(default_data_dir())
    if args.quit_gui or args.install_shortcuts or args.remove_shortcuts:
        from .gui.launcher import desktop_command

        return desktop_command(args, overrides_from_args(args))
    if args.gui or args.no_browser:
        from .gui import run_gui

        overrides = overrides_from_args(args)
        overrides["workdir"] = args.workdir or str(Path.cwd())
        return run_gui(overrides, args.config, port=args.port, open_browser=not args.no_browser,
                       use_mcp=not args.no_mcp)

    try:
        settings, warnings = load_settings(overrides_from_args(args), args.config)
    except ConfigError as exc:
        print(f"kimi-agent: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings)

    from .runtime import Runtime
    from .session import SessionStore
    from .ui import UI, JsonUI

    if args.list_sessions:
        print_sessions(SessionStore(settings.data_path / "sessions"))
        return 0

    prompt = " ".join(args.prompt).strip()
    stdin_tty = sys.stdin.isatty()
    if not stdin_tty:
        if args.interactive:
            print("kimi-agent: interactive mode needs a terminal on stdin.", file=sys.stderr)
            return 2
        prompt = read_stdin_prompt(prompt).strip()
        if not prompt:
            print("kimi-agent: no prompt given (pass a task as an argument or pipe it on stdin).", file=sys.stderr)
            return 2
    repl = not prompt or args.interactive
    if repl and args.json:
        print("kimi-agent: --json needs a prompt (it is non-interactive).", file=sys.stderr)
        return 2

    if args.json:
        ui: Any = JsonUI()
    else:
        ui = UI(show_reasoning=settings.show_reasoning, stream=sys.stdout.isatty() and not args.quiet,
                quiet=args.quiet, verbose=args.verbose)
    for warning in warnings:
        ui.warn(warning)
    key_message = missing_api_key_message(settings)
    if key_message:
        ui.error(key_message)
        return 2

    runtime = Runtime(settings, ui, interactive=stdin_tty and not args.json, use_mcp=not args.no_mcp)
    try:
        runtime.start()
        if args.resume or args.continue_session:
            try:
                session = runtime.resume(args.resume)
                ui.info(f"Resumed session {session.id}: {session.title}")
            except (FileNotFoundError, ValueError) as exc:
                ui.error(str(exc))
                return 1
        if not repl:
            return run_once(runtime, prompt, args)
        from .repl import Repl

        return Repl(runtime).run(initial=prompt or None)
    except KeyboardInterrupt:
        ui.warn("Interrupted.")
        return 130
    finally:
        runtime.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

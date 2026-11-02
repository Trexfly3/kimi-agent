"""High-level Python API: run the agent from your own code, with your own tools."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import fields
from typing import Any

from .config import ConfigError, Settings, load_settings, missing_api_key_message
from .runtime import Runtime
from .tools.base import Tool


def create_runtime(*, tools: Iterable[Tool] = (), ui: Any = None, interactive: bool = False, use_mcp: bool = True,
                   config_file: str | None = None, **settings: Any) -> Runtime:
    """Create and start a Runtime; ``runtime.agent.run(text)`` then runs one request and returns the answer.

    Keyword arguments override the configuration (``~/.kimi-agent/config.toml``, ``KIMI_AGENT_*`` environment
    variables), e.g. ``provider="openrouter", model="moonshotai/kimi-k3", approval="yolo"``. Without a ``ui``
    nothing is printed except warnings; pass ``kimi_agent.ui.UI()`` for the full terminal display. With
    ``interactive=False`` actions that need approval are refused, so choose the approval mode accordingly.
    Use the runtime as a context manager (or call ``close()``) to save the session and stop background processes.
    """
    unknown = sorted(set(settings) - {f.name for f in fields(Settings)})
    if unknown:
        raise TypeError(f"unknown setting(s): {', '.join(unknown)}")
    loaded, warnings = load_settings(settings, config_file)
    if ui is None:
        from .ui import UI

        ui = UI(stream=False, quiet=True)
    for warning in warnings:
        ui.warn(warning)
    message = missing_api_key_message(loaded)
    if message:
        raise ConfigError(message)
    runtime = Runtime(loaded, ui, interactive=interactive, use_mcp=use_mcp, tools=tools)
    try:
        runtime.start()
    except BaseException:
        runtime.close()
        raise
    return runtime


def run(task: str, **kwargs: Any) -> str:
    """Run one task to completion and return the final answer. Accepts the arguments of ``create_runtime``."""
    with create_runtime(**kwargs) as runtime:
        assert runtime.agent is not None
        return runtime.agent.run(task)

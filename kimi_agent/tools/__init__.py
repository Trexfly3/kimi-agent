"""Tool registry and the built-in tool set."""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Iterator
from typing import Any

from kimi_agent.tools.base import Tool, ToolContext, ToolError, ToolResult, tool

__all__ = ["Tool", "ToolContext", "ToolError", "ToolRegistry", "ToolResult", "build_registry", "tool"]


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools:
            self.register(t)

    def register(self, tool_: Tool, replace: bool = False) -> None:
        if tool_.name in self._tools and not replace:
            raise ValueError(f"duplicate tool name: {tool_.name}")
        self._tools[tool_.name] = tool_

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def without(self, *names: str) -> ToolRegistry:
        return ToolRegistry(t for n, t in self._tools.items() if n not in names)

    def remove_matching(self, patterns: Iterable[str]) -> list[str]:
        removed = [n for n in self._tools if any(fnmatch.fnmatchcase(n, p) for p in patterns)]
        for name in removed:
            del self._tools[name]
        return removed

    def __iter__(self) -> Iterator[Tool]:
        return iter(list(self._tools.values()))

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools


def build_registry(settings: Any, extra: Iterable[Tool] = ()) -> tuple[ToolRegistry, list[str]]:
    """Create the registry of enabled tools. Returns (registry, notes about unavailable tools)."""
    from kimi_agent.tools.files import FILE_TOOLS
    from kimi_agent.tools.interaction import INTERACTION_TOOLS, SUBAGENT_TOOLS
    from kimi_agent.tools.memory import MEMORY_TOOLS
    from kimi_agent.tools.python_exec import PYTHON_TOOLS
    from kimi_agent.tools.shell import SHELL_TOOLS
    from kimi_agent.tools.web import WEB_TOOLS, kimi_builtin_search_tool, web_search

    notes: list[str] = []
    registry = ToolRegistry([*FILE_TOOLS, *SHELL_TOOLS, *PYTHON_TOOLS, *INTERACTION_TOOLS])
    if settings.web:
        for t in WEB_TOOLS:
            registry.register(t)
        if settings.kimi_web_search:
            if settings.preset.kimi:
                registry.unregister(web_search.name)
                registry.register(kimi_builtin_search_tool())
            else:
                notes.append("kimi_web_search only works with the official Kimi API providers; using web_search.")
    if settings.memory:
        for t in MEMORY_TOOLS:
            registry.register(t)
    if settings.subagents:
        for t in SUBAGENT_TOOLS:
            registry.register(t)
    if settings.computer_use:
        from kimi_agent.tools.computer import COMPUTER_TOOLS, computer_available

        ok, reason = computer_available()
        if ok:
            for t in COMPUTER_TOOLS:
                registry.register(t)
            if not settings.vision:
                notes.append("Computer use is enabled but vision is off; screenshots will not work (--vision).")
        else:
            notes.append(f"Computer use disabled: {reason}.")
    for t in extra:
        if t.name in registry:
            notes.append(f"Tool name conflict: {t.name} (skipped)")
            continue
        registry.register(t)
    if settings.disabled_tools:
        registry.remove_matching(settings.disabled_tools)
    return registry, notes

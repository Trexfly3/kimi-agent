"""Custom tools from plugins: Python files or importable modules listed in the ``plugins`` setting.

A plugin defines tools with the ``@tool`` decorator (``from kimi_agent import tool``). All module-level ``Tool``
objects are used, or only those in a module-level ``TOOLS`` list when it is defined.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from types import ModuleType

from .tools.base import Tool


def _is_path(spec: str) -> bool:
    return spec.endswith(".py") or "/" in spec or "\\" in spec or spec.startswith("~")


def _find_file(spec: str, search: Sequence[Path]) -> Path:
    path = Path(spec).expanduser()
    candidates = [path] if path.is_absolute() else [base / path for base in search]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError("no such file: " + " or ".join(str(c) for c in candidates))


def import_plugin(spec: str, search: Sequence[Path] = ()) -> ModuleType:
    """Import a plugin from a file path (relative paths are tried in each ``search`` directory) or a module name."""
    if not _is_path(spec):
        return importlib.import_module(spec)
    path = _find_file(spec, search)
    name = "kimi_agent_plugin_" + re.sub(r"\W", "_", path.stem)
    if name in sys.modules and getattr(sys.modules[name], "__file__", None) == str(path):
        return sys.modules[name]
    module_spec = importlib.util.spec_from_file_location(name, path)
    if module_spec is None or module_spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[name] = module  # dataclasses and pickling look the module up by name
    try:
        module_spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def module_tools(module: ModuleType) -> list[Tool]:
    declared = getattr(module, "TOOLS", None)
    if declared is None:
        return [value for value in vars(module).values() if isinstance(value, Tool)]
    tools = list(declared)
    invalid = [repr(t) for t in tools if not isinstance(t, Tool)]
    if invalid:
        raise TypeError("TOOLS may only contain @tool functions, got " + ", ".join(invalid))
    return tools


def load_plugins(specs: Iterable[str], search: Sequence[Path] = ()) -> tuple[list[Tool], list[str]]:
    """Load the tools of every plugin. Returns (tools, error messages); a broken plugin never stops the agent."""
    tools: list[Tool] = []
    errors: list[str] = []
    for spec in specs:
        try:
            found = module_tools(import_plugin(spec, search))
        except Exception as exc:
            errors.append(f"Plugin {spec} failed to load: {type(exc).__name__}: {exc}")
            continue
        if not found:
            errors.append(f"Plugin {spec} defines no tools (decorate functions with @tool).")
        tools.extend(found)
    return tools, errors

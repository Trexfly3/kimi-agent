"""Tool abstraction: JSON-schema generation from Python signatures, results and runtime context."""

from __future__ import annotations

import dataclasses
import functools
import inspect
import json
import os
import re
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints, is_typeddict

# Tool categories drive the approval policy (see safety.py):
#   read      - no side effects
#   internal  - only touches the agent's own state (plan, memory, own background processes)
#   network   - talks to the internet (fetch / search)
#   write     - modifies files
#   execute   - runs code or shell commands
#   gui       - controls mouse / keyboard
#   external  - third-party MCP tools
CATEGORIES = ("read", "internal", "network", "write", "execute", "gui", "external")


class ToolError(Exception):
    """An expected tool failure. The message is returned to the model as the tool result."""


@dataclass
class Image:
    data: str  # base64-encoded
    mime: str = "image/png"

    def data_url(self) -> str:
        return f"data:{self.mime};base64,{self.data}"


@dataclass
class ToolResult:
    output: str
    images: list[Image] = field(default_factory=list)
    is_error: bool = False


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    category: str = "read"
    summarize: Callable[[dict[str, Any]], str] | None = None
    preview: Callable[[Any, dict[str, Any]], tuple[str, str]] | None = None  # (text, lexer)
    trusted: bool = False
    strict_args: bool = True  # drop arguments not in the schema (Python tools)
    builtin: dict[str, Any] | None = None  # raw schema for provider builtin functions
    hints: dict[str, Any] = field(default_factory=dict)  # e.g. MCP annotations such as readOnlyHint

    def schema(self) -> dict[str, Any]:
        if self.builtin is not None:
            return self.builtin
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }

    def summary(self, args: dict[str, Any]) -> str:
        if self.summarize is not None:
            try:
                return one_line(str(self.summarize(args)))
            except Exception:
                pass
        return default_summary(args)

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return to_result(self.func(ctx, **args))


@dataclass
class ToolContext:
    """Runtime state shared by tools."""

    settings: Any
    cwd: Path
    root: Path
    ui: Any = None
    interactive: bool = False
    file_history: Any = None
    processes: Any = None
    python: Any = None
    memory: Any = None
    plan: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    spawn_subagent: Callable[[str], str] | None = None
    depth: int = 0

    @property
    def vision(self) -> bool:
        return bool(getattr(self.settings, "vision", False))

    @property
    def data_dir(self) -> Path:
        return Path(self.settings.data_dir)

    def resolve(self, path: str | os.PathLike[str]) -> Path:
        p = Path(os.path.expandvars(os.path.expanduser(str(path))))
        if not p.is_absolute():
            p = self.cwd / p
        return Path(os.path.normpath(p))

    def display(self, path: Path) -> str:
        """Path relative to the cwd when inside it, absolute otherwise."""
        try:
            rel = path.relative_to(self.cwd)
            return rel.as_posix() if str(rel) != "." else "."
        except ValueError:
            return str(path)

    def fork(self) -> ToolContext:
        return dataclasses.replace(self, plan=[], depth=self.depth + 1)


def one_line(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def default_summary(args: dict[str, Any]) -> str:
    for value in args.values():
        if isinstance(value, str) and value.strip():
            return one_line(value)
    return one_line(json.dumps(args, ensure_ascii=False)) if args else ""


def truncate_middle(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head = int(limit * 0.4)
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n[... {omitted:,} characters truncated ...]\n\n{text[-tail:]}"


def to_result(value: Any) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    if value is None:
        return ToolResult("OK")
    if isinstance(value, str):
        return ToolResult(value)
    try:
        return ToolResult(json.dumps(value, indent=2, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return ToolResult(str(value))


# --------------------------------------------------------------------------- schema generation

_PRIMITIVES: dict[Any, str] = {str: "string", int: "integer", float: "number", bool: "boolean"}


def type_to_schema(tp: Any) -> dict[str, Any]:
    if tp is Any or tp is inspect.Parameter.empty:
        return {"type": "string"}
    origin = get_origin(tp)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return type_to_schema(args[0])
        return {"anyOf": [type_to_schema(a) for a in args]}
    if origin is Literal:
        values = list(get_args(tp))
        schema: dict[str, Any] = {"enum": values}
        json_type = _PRIMITIVES.get(type(values[0]))
        if json_type:
            schema = {"type": json_type, "enum": values}
        return schema
    if origin in (list, tuple, set, frozenset):
        args = get_args(tp)
        return {"type": "array", "items": type_to_schema(args[0]) if args else {"type": "string"}}
    if tp in (list, tuple, set):
        return {"type": "array", "items": {"type": "string"}}
    if origin is dict or tp is dict:
        return {"type": "object"}
    if is_typeddict(tp):
        hints = get_type_hints(tp)
        required_keys = getattr(tp, "__required_keys__", frozenset(hints))
        return {
            "type": "object",
            "properties": {k: type_to_schema(v) for k, v in hints.items()},
            "required": [k for k in hints if k in required_keys],
        }
    if tp in _PRIMITIVES:
        return {"type": _PRIMITIVES[tp]}
    return {"type": "string"}


_SECTION_RE = re.compile(r"^(Args|Arguments|Parameters|Returns|Raises|Examples?|Notes?|Yields):\s*$")
_PARAM_RE = re.compile(r"^\s{0,4}(\w+)\s*(?:\([^)]*\))?:\s*(.*)$")


def parse_docstring(doc: str | None) -> tuple[str, dict[str, str]]:
    """Parse a Google-style docstring into (description, {param: description})."""
    if not doc:
        return "", {}
    lines = inspect.cleandoc(doc).splitlines()
    description: list[str] = []
    params: dict[str, str] = {}
    section: str | None = None
    current: str | None = None
    for line in lines:
        header = _SECTION_RE.match(line.strip()) if not line.startswith(" ") else None
        if header:
            section = header.group(1)
            current = None
            continue
        if section is None:
            description.append(line)
        elif section in ("Args", "Arguments", "Parameters"):
            indent = len(line) - len(line.lstrip())
            match = _PARAM_RE.match(line)
            if match and indent <= 4:
                current = match.group(1)
                params[current] = match.group(2).strip()
            elif current and line.strip():
                params[current] = (params[current] + " " + line.strip()).strip()
    return "\n".join(description).strip(), params


def takes_context(func: Callable[..., Any]) -> bool:
    """Whether the first parameter receives the ToolContext (named ctx/context or annotated ToolContext)."""
    params = list(inspect.signature(func).parameters.values())
    if not params or params[0].kind not in (inspect.Parameter.POSITIONAL_ONLY,
                                            inspect.Parameter.POSITIONAL_OR_KEYWORD):
        return False
    first = params[0]
    return first.name in ("ctx", "context") or first.annotation in (ToolContext, "ToolContext")


def schema_from_signature(func: Callable[..., Any], param_docs: dict[str, str],
                          skip_first: bool = True) -> dict[str, Any]:
    sig = inspect.signature(func)
    hints = get_type_hints(func)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in list(sig.parameters.values())[1 if skip_first else 0:]:  # skip the ToolContext parameter
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        prop = type_to_schema(hints.get(param.name, str))
        if param.name in param_docs:
            prop["description"] = param_docs[param.name]
        if param.default is inspect.Parameter.empty:
            required.append(param.name)
        elif isinstance(param.default, (str, int, float, bool)):
            prop["default"] = param.default
        properties[param.name] = prop
    return {"type": "object", "properties": properties, "required": required}


def tool(
    name: str | Callable[..., Any] | None = None,
    *,
    category: str = "read",
    summarize: Callable[[dict[str, Any]], str] | None = None,
    preview: Callable[[Any, dict[str, Any]], tuple[str, str]] | None = None,
    description: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> Any:
    """Turn a function into a Tool, deriving its JSON schema from type hints and a Google-style docstring.

    Use as ``@tool`` or ``@tool(category="write")``. The function may take the ToolContext as its first
    parameter (``ctx``). ``category`` decides approval: read/internal tools never ask, network tools are
    allowed in auto mode, write tools are allowed inside the working directory in auto mode, and
    execute/gui/external tools ask unless the approval mode is yolo.
    """
    if callable(name):  # bare @tool
        return tool()(name)
    if category not in CATEGORIES:
        raise ValueError(f"unknown tool category {category!r}")

    def decorator(func: Callable[..., Any]) -> Tool:
        doc_description, param_docs = parse_docstring(func.__doc__)
        with_ctx = takes_context(func)
        call = func if with_ctx else functools.wraps(func)(lambda ctx, **kwargs: func(**kwargs))
        return Tool(
            name=name or func.__name__,
            description=description or doc_description,
            parameters=parameters or schema_from_signature(func, param_docs, skip_first=with_ctx),
            func=call,
            category=category,
            summarize=summarize,
            preview=preview,
        )

    return decorator


# --------------------------------------------------------------------------- argument handling

def _coerce(value: Any, schema: dict[str, Any]) -> Any:
    expected = schema.get("type")
    try:
        if expected == "integer":
            if isinstance(value, str):
                return int(value.strip())
            if isinstance(value, float) and value.is_integer():
                return int(value)
        elif expected == "number" and isinstance(value, str):
            return float(value.strip())
        elif expected == "boolean" and isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        elif expected == "array" and isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else [value]
            except ValueError:
                return [value]
        elif expected == "object" and isinstance(value, str):
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else value
        elif expected == "string" and value is not None and not isinstance(value, str):
            return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    except (ValueError, TypeError):
        pass
    return value


def prepare_arguments(tool_: Tool, args: Any) -> dict[str, Any]:
    """Validate required arguments and coerce primitive types. Raises ToolError for the model."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError(f"Tool arguments must be a JSON object, got {type(args).__name__}.")
    properties: dict[str, Any] = tool_.parameters.get("properties", {}) or {}
    required = tool_.parameters.get("required", []) or []
    missing = [r for r in required if args.get(r) is None]
    if missing:
        expected = ", ".join(
            f"{k}{'' if k in required else '?'}: {v.get('type', 'any')}" for k, v in properties.items()
        )
        raise ToolError(f"Missing required argument(s): {', '.join(missing)}. Parameters: {expected}")
    result: dict[str, Any] = {}
    for key, value in args.items():
        if key in properties:
            result[key] = _coerce(value, properties[key])
        elif not tool_.strict_args:
            result[key] = value
    if tool_.strict_args:
        # Drop explicit nulls for optional parameters so Python defaults apply.
        result = {k: v for k, v in result.items() if v is not None or k in required}
    return result

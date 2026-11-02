"""Example plugin: custom tools for the kimi-agent CLI.

Enable it in ~/.kimi-agent/config.toml:

    plugins = ["/absolute/path/to/examples/plugin_project_stats.py"]

or for a single run:

    KIMI_AGENT_PLUGINS=examples/plugin_project_stats.py kimi-agent "How big is this project? Add a TODO to check it."

Every module-level @tool function becomes a tool (or only those listed in TOOLS = [...]). The tool's category
decides when the user must approve a call; see the README.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from kimi_agent import ToolContext, ToolError, tool

SKIP_DIRS = {".git", ".hg", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".mypy_cache"}


@tool  # category "read" (the default) never asks for approval, so the tool must not change anything
def project_stats(ctx: ToolContext, path: str = ".", top: int = 10) -> str:
    """Count files and lines per file extension in a directory tree (skips .git, node_modules, venvs...).

    Args:
        path: Directory to analyze, relative to the working directory.
        top: How many extensions to list.
    """
    root = ctx.resolve(path)
    if not root.is_dir():
        raise ToolError(f"Not a directory: {root}")  # the message is sent to the model as the tool result
    files: Counter[str] = Counter()
    lines: Counter[str] = Counter()
    for file in _walk(root):
        ext = file.suffix.lower() or "(none)"
        files[ext] += 1
        try:
            with file.open("rb") as f:
                lines[ext] += sum(1 for _ in f)
        except OSError:
            continue
    rows = [f"{ext:10} {files[ext]:6,} files {count:10,} lines" for ext, count in lines.most_common(top)]
    return "\n".join(rows) or "No files found."


def _walk(root: Path) -> Iterator[Path]:
    for entry in root.iterdir():
        if entry.is_symlink():
            continue
        if entry.is_dir():
            if entry.name not in SKIP_DIRS:
                yield from _walk(entry)
        elif entry.is_file():
            yield entry


@tool(category="write")  # auto mode allows writes inside the working directory; ask mode always asks
def add_todo(ctx: ToolContext, text: str) -> str:
    """Append a task to TODO.md in the working directory.

    Args:
        text: The task, in one line.
    """
    path = ctx.resolve("TODO.md")
    if ctx.file_history is not None:
        ctx.file_history.record(path)  # lets the user revert this change with /undo
    with path.open("a", encoding="utf-8") as f:
        f.write(f"- [ ] {' '.join(text.split())}\n")
    return f"Added to {ctx.display(path)}."

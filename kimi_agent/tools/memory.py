"""Long-term memory: facts the agent keeps across sessions (stored as a Markdown list)."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from kimi_agent.tools.base import Tool, ToolContext, ToolError, tool

MAX_FACT_CHARS = 1000
MAX_PROMPT_CHARS = 8000


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def facts(self) -> list[str]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        facts: list[str] = []
        for line in lines:
            if line.startswith(("- ", "* ")):
                facts.append(line[2:].strip())
            elif line.startswith("  ") and facts:  # continuation line
                facts[-1] += " " + line.strip()
        return [f for f in facts if f]

    def _write(self, facts: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        body = "# Kimi Agent memory\n\n" + "".join(f"- {f}\n" for f in facts)
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, fact: str) -> bool:
        fact = " ".join(fact.split())
        with self._lock:
            facts = self.facts()
            if any(f.lower() == fact.lower() for f in facts):
                return False
            facts.append(fact)
            self._write(facts)
            return True

    def remove(self, text: str) -> list[str]:
        needle = " ".join(text.split()).lower()
        with self._lock:
            facts = self.facts()
            removed = [f for f in facts if needle in f.lower()]
            if removed:
                self._write([f for f in facts if needle not in f.lower()])
            return removed

    def clear(self) -> None:
        with self._lock:
            self._write([])

    def render(self, limit: int = MAX_PROMPT_CHARS) -> str:
        """Memory as Markdown for the system prompt (most recent facts win when over the limit)."""
        lines: list[str] = []
        size = 0
        for fact in reversed(self.facts()):
            size += len(fact) + 3
            if size > limit:
                break
            lines.append(f"- {fact}")
        return "\n".join(reversed(lines))


def _store(ctx: ToolContext) -> MemoryStore:
    if ctx.memory is None:
        raise ToolError("Memory is disabled.")
    return ctx.memory


@tool(category="internal", summarize=lambda a: str(a.get("fact", "")))
def remember(ctx: ToolContext, fact: str) -> str:
    """Save a durable fact to long-term memory; it is shown to you in all future sessions.

    Use for stable information worth keeping: user preferences, facts about their machine or projects,
    conventions, important paths. Not for temporary task state. Never store secrets (passwords, API keys).

    Args:
        fact: One self-contained fact, e.g. "User prefers pnpm over npm".
    """
    fact = fact.strip()
    if not fact:
        raise ToolError("fact is empty.")
    if len(fact) > MAX_FACT_CHARS:
        raise ToolError(f"Facts must be under {MAX_FACT_CHARS} characters; save a shorter summary.")
    if _store(ctx).add(fact):
        return f"Remembered: {fact}"
    return "Already in memory."


@tool(category="internal", summarize=lambda a: str(a.get("text", "")))
def forget(ctx: ToolContext, text: str) -> str:
    """Remove facts from long-term memory that contain the given text (case-insensitive).

    Args:
        text: Text identifying the fact(s) to remove.
    """
    removed = _store(ctx).remove(text)
    if not removed:
        return f"No memory entries contain {text!r}."
    return "Forgot:\n" + "\n".join(f"- {f}" for f in removed)


MEMORY_TOOLS: list[Tool] = [remember, forget]

"""Kimi Agent: an autonomous, tool-using agent for Kimi and other OpenAI-compatible models.

Quick start::

    import kimi_agent

    @kimi_agent.tool()
    def word_count(ctx, text: str) -> int:
        \"\"\"Count the words in a text.\"\"\"
        return len(text.split())

    print(kimi_agent.run("How many words are in 'to be or not to be'?", tools=[word_count]))
"""

from __future__ import annotations

import importlib
from typing import Any

__version__ = "0.2.0"

_EXPORTS = {
    "tool": "kimi_agent.tools.base",
    "Tool": "kimi_agent.tools.base",
    "ToolContext": "kimi_agent.tools.base",
    "ToolError": "kimi_agent.tools.base",
    "ToolResult": "kimi_agent.tools.base",
    "Image": "kimi_agent.tools.base",
    "Runtime": "kimi_agent.runtime",
    "create_runtime": "kimi_agent.api",
    "run": "kimi_agent.api",
}

__all__ = ["__version__", *_EXPORTS]


def __getattr__(name: str) -> Any:
    # Lazy exports keep `import kimi_agent` (and `kimi-agent --version`) fast.
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'kimi_agent' has no attribute {name!r}")
    return getattr(importlib.import_module(module), name)

"""Desktop app: a local web app shown in a browser app window (``kimi-agent --gui`` or ``kimi-agent-gui``)."""

from __future__ import annotations

from typing import Any


def run_gui(*args: Any, **kwargs: Any) -> int:
    from .launcher import run_gui as _run_gui

    return _run_gui(*args, **kwargs)


def main(argv: list[str] | None = None) -> int:
    from .launcher import main as _main

    return _main(argv)


__all__ = ["main", "run_gui"]

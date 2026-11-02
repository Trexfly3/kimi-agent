"""run_python: a persistent Python interpreter (notebook-style) in a separate process."""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

from kimi_agent.tools.base import Tool, ToolContext, ToolError, ToolResult, tool, truncate_middle
from kimi_agent.tools.shell import IS_WINDOWS, command_env

WORKER = Path(__file__).with_name("_pyworker.py")


def default_python() -> str:
    return shutil.which("python3") or shutil.which("python") or sys.executable


class PythonSession:
    def __init__(self, python: str | None = None) -> None:
        self.python = python or default_python()
        self.proc: subprocess.Popen[bytes] | None = None
        self._responses: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _start(self) -> None:
        env = command_env()
        env["MPLBACKEND"] = "Agg"
        kwargs: dict[str, object] = {}
        if not IS_WINDOWS:
            kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(
                [self.python, "-u", str(WORKER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=env, **kwargs,  # type: ignore[arg-type]
            )
        except OSError as exc:
            raise ToolError(f"Could not start Python interpreter {self.python!r}: {exc}") from exc
        self._responses = queue.Queue()
        threading.Thread(target=self._read, args=(self.proc, self._responses), daemon=True).start()

    @staticmethod
    def _read(proc: subprocess.Popen[bytes], responses: queue.Queue[str | None]) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            responses.put(raw.decode("utf-8", errors="replace"))
        responses.put(None)

    def _kill(self) -> None:
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.proc = None

    def reset(self) -> None:
        with self._lock:
            self._kill()

    close = reset

    def execute(self, code: str, cwd: Path, timeout: float) -> tuple[str, bool]:
        with self._lock:
            if not self.alive:
                self._start()
            assert self.proc is not None and self.proc.stdin is not None
            request = json.dumps({"code": code, "cwd": str(cwd)}) + "\n"
            try:
                self.proc.stdin.write(request.encode("utf-8"))
                self.proc.stdin.flush()
            except OSError:
                self._kill()
                raise ToolError("The Python interpreter died; it will be restarted on the next call.") from None
            try:
                line = self._responses.get(timeout=timeout)
            except queue.Empty:
                self._kill()
                return f"[Timed out after {timeout:g}s; the interpreter was restarted and all state was lost.]", False
            except KeyboardInterrupt:
                self._interrupt()
                raise
            if line is None:
                code_ = self.proc.poll() if self.proc else None
                self._kill()
                return f"[The Python interpreter exited unexpectedly (code {code_}); state was lost.]", False
            try:
                message = json.loads(line)
            except ValueError:
                return line, False
            return str(message.get("output", "")), bool(message.get("ok", False))

    def _interrupt(self) -> None:
        """Interrupt running code but keep the interpreter state when possible."""
        if self.proc is None:
            return
        if IS_WINDOWS:
            self._kill()
            return
        try:
            os.kill(self.proc.pid, signal.SIGINT)
            self._responses.get(timeout=3)
        except (OSError, queue.Empty):
            self._kill()


@tool(
    category="execute",
    summarize=lambda a: next((ln for ln in str(a.get("code", "")).splitlines() if ln.strip()), ""),
    preview=lambda ctx, a: (str(a.get("code", "")), "python"),
)
def run_python(ctx: ToolContext, code: str, timeout: int = 120, reset: bool = False) -> ToolResult:
    """Execute Python code in a persistent interpreter (like a Jupyter notebook).

    Variables, imports and functions persist between calls. The value of the last expression is printed
    automatically. Good for calculations, data analysis, parsing files and quick scripts. Runs in the
    current working directory; matplotlib uses the non-interactive Agg backend (save figures to files).

    Args:
        code: Python code to execute.
        timeout: Seconds to wait; on timeout the interpreter is restarted and its state is lost.
        reset: Restart the interpreter (clearing all state) before running the code.
    """
    session: PythonSession = ctx.python
    if reset:
        session.reset()
    output, ok = session.execute(code, ctx.cwd, max(1, int(timeout)))
    output = truncate_middle(output.rstrip("\n"), ctx.settings.max_output_chars)
    return ToolResult(output if output.strip() else "(no output)", is_error=not ok)


PYTHON_TOOLS: list[Tool] = [run_python]

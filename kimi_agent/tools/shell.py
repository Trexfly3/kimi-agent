"""Shell command execution and background process management."""

from __future__ import annotations

import atexit
import codecs
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kimi_agent.tools.base import Tool, ToolContext, ToolError, ToolResult, tool, truncate_middle

IS_WINDOWS = os.name == "nt"
CWD_MARKER = "__KIMI_AGENT_CWD__"
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def clean_output(text: str) -> str:
    """Strip ANSI escapes and collapse carriage-return progress bars to their final state."""
    text = strip_ansi(text).replace("\r\n", "\n")
    if "\r" in text:
        lines = []
        for line in text.split("\n"):
            segments = [s for s in line.split("\r") if s]
            lines.append(segments[-1] if segments else "")
        text = "\n".join(lines)
    return text


def command_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "NO_COLOR": "1",
        "FORCE_COLOR": "0",
        "CLICOLOR": "0",
        "TERM": "dumb",
        "PYTHONUNBUFFERED": "1",
        "DEBIAN_FRONTEND": "noninteractive",
        "KIMI_AGENT": "1",
    })
    return env


def find_shell(settings: Any) -> str:
    if getattr(settings, "shell", ""):
        return settings.shell
    if IS_WINDOWS:
        return shutil.which("pwsh") or shutil.which("powershell") or "cmd.exe"
    return shutil.which("bash") or "/bin/sh"


def build_argv(settings: Any, command: str, track_cwd: bool) -> list[str]:
    shell = find_shell(settings)
    name = Path(shell).name.lower()
    if IS_WINDOWS:
        if name.startswith("cmd"):
            return [shell, "/d", "/s", "/c", command]
        return [shell, "-NoProfile", "-NonInteractive", "-Command", command]
    script = command
    if track_cwd:
        script = f"{command}\n__ka_ec=$?\nprintf '\\n{CWD_MARKER}%s\\n' \"$(pwd)\"\nexit $__ka_ec"
    return [shell, "-c", script]


def extract_cwd(output: str) -> tuple[str, str | None]:
    idx = output.rfind(CWD_MARKER)
    if idx == -1:
        return output, None
    line_end = output.find("\n", idx)
    cwd = output[idx + len(CWD_MARKER) : line_end if line_end != -1 else None].strip()
    before = output[:idx]
    if before.endswith("\n"):
        before = before[:-1]
    after = output[line_end + 1 :] if line_end != -1 else ""
    return before + after, cwd or None


def kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Terminate a process and everything it spawned."""
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    except OSError:
        pass


def _popen(argv: list[str], cwd: Path, stdin: int) -> subprocess.Popen[bytes]:
    kwargs: dict[str, Any] = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(
            argv, cwd=str(cwd), stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=command_env(), **kwargs,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"Could not start shell {argv[0]!r}: {exc}") from exc


def _pump(stream: Any, sink: Callable[[bytes], None]) -> None:
    try:
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                break
            sink(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


class OutputBuffer:
    """Keeps the head and the tail of a potentially huge byte stream."""

    def __init__(self, head_limit: int = 1_000_000, tail_limit: int = 1_000_000) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.dropped = 0
        self.head_limit = head_limit
        self.tail_limit = tail_limit
        self.lock = threading.Lock()

    def write(self, data: bytes) -> None:
        with self.lock:
            if len(self.head) < self.head_limit:
                take = self.head_limit - len(self.head)
                self.head += data[:take]
                data = data[take:]
            if data:
                self.tail += data
                if len(self.tail) > self.tail_limit:
                    cut = len(self.tail) - self.tail_limit
                    self.dropped += cut
                    del self.tail[:cut]

    def getvalue(self) -> str:
        with self.lock:
            text = bytes(self.head).decode("utf-8", errors="replace")
            if self.dropped:
                text += f"\n[... {self.dropped:,} bytes of output dropped ...]\n"
            return text + bytes(self.tail).decode("utf-8", errors="replace")


@dataclass
class ExecResult:
    exit_code: int | None
    output: str
    timed_out: bool = False
    lingering: bool = False


def execute(argv: list[str], cwd: Path, timeout: float) -> ExecResult:
    proc = _popen(argv, cwd, subprocess.DEVNULL)
    buffer = OutputBuffer()
    reader = threading.Thread(target=_pump, args=(proc.stdout, buffer.write), daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc)
    except KeyboardInterrupt:
        kill_tree(proc)
        raise
    reader.join(timeout=1 if timed_out else 2)
    lingering = reader.is_alive() and not timed_out
    return ExecResult(proc.returncode, buffer.getvalue(), timed_out, lingering)


# --------------------------------------------------------------------------- run_command

def _command_preview(ctx: ToolContext, args: dict[str, Any]) -> tuple[str, str]:
    text = str(args.get("command", ""))
    if args.get("cwd"):
        text = f"# in {ctx.resolve(args['cwd'])}\n{text}"
    else:
        text = f"# in {ctx.cwd}\n{text}"
    return text, "bash"


@tool(category="execute", summarize=lambda a: str(a.get("command", "")), preview=_command_preview)
def run_command(ctx: ToolContext, command: str, timeout: int | None = None, cwd: str | None = None) -> ToolResult:
    """Run a shell command (bash; PowerShell on Windows) and return its combined stdout/stderr and exit code.

    Commands run non-interactively with no stdin, so use non-interactive flags (-y, --yes, --no-pager).
    The working directory persists between calls: `cd somewhere` affects later commands. Never use this
    for servers, watchers or other commands that don't exit on their own; use start_process instead.
    Prefer read_file / edit_file / search_files over cat / sed / grep for working with files.

    Args:
        command: The command to run. Chain multiple commands with && or ;.
        timeout: Seconds before the command is killed (default 300).
        cwd: Directory to run in instead of the current working directory (does not change it).
    """
    workdir = ctx.resolve(cwd) if cwd else ctx.cwd
    if not workdir.is_dir():
        raise ToolError(f"Working directory does not exist: {workdir}")
    limit = max(1, int(timeout or ctx.settings.command_timeout))
    track = not IS_WINDOWS and not cwd
    result = execute(build_argv(ctx.settings, command, track), workdir, limit)
    output = result.output
    changed_dir = False
    if track:
        output, new_cwd = extract_cwd(output)
        if new_cwd and Path(new_cwd).is_dir() and Path(new_cwd) != ctx.cwd:
            ctx.cwd = Path(new_cwd)
            changed_dir = True
    output = truncate_middle(clean_output(output).rstrip("\n"), ctx.settings.max_output_chars)
    parts = [output if output.strip() else "(no output)"]
    if result.timed_out:
        parts.append(f"[Timed out after {limit}s; the process was killed. Use start_process for long-running work.]")
    elif result.lingering:
        parts.append("[The command finished but a background process it started still holds the output open; "
                     "its further output is not captured. Use start_process for long-running processes.]")
    if result.exit_code is not None:
        parts.append(f"[exit code: {result.exit_code}]")
    if changed_dir:
        parts.append(f"[working directory is now {ctx.cwd}]")
    return ToolResult("\n".join(parts), is_error=bool(result.timed_out or (result.exit_code or 0) != 0))


# --------------------------------------------------------------------------- background processes

class StreamLog:
    """Thread-safe, size-capped text log supporting incremental reads."""

    def __init__(self, cap: int = 2_000_000) -> None:
        self._chunks: deque[str] = deque()
        self._size = 0
        self._start = 0  # absolute offset of the first retained character
        self._end = 0
        self._read = 0
        self._cap = cap
        self._lock = threading.Lock()

    def append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._chunks.append(text)
            self._size += len(text)
            self._end += len(text)
            while self._size > self._cap and len(self._chunks) > 1:
                old = self._chunks.popleft()
                self._size -= len(old)
                self._start += len(old)

    def _unread(self) -> tuple[str, int]:
        skipped = max(0, self._start - self._read)
        position = max(self._read, self._start)
        return "".join(self._chunks)[position - self._start :], skipped

    def peek_new(self) -> str:
        with self._lock:
            return self._unread()[0]

    def read_new(self) -> tuple[str, int]:
        with self._lock:
            text, skipped = self._unread()
            self._read = self._end
            return text, skipped

    def tail(self, chars: int) -> str:
        with self._lock:
            return "".join(self._chunks)[-chars:]


@dataclass
class BackgroundProcess:
    id: str
    command: str
    cwd: Path
    proc: subprocess.Popen[bytes]
    log: StreamLog
    started: float = field(default_factory=time.time)

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def status(self) -> str:
        code = self.proc.poll()
        return "running" if code is None else f"exited with code {code}"


class ProcessManager:
    def __init__(self) -> None:
        self._procs: dict[str, BackgroundProcess] = {}
        self._counter = 0
        self._lock = threading.Lock()
        atexit.register(self.stop_all)

    def start(self, settings: Any, command: str, cwd: Path) -> BackgroundProcess:
        proc = _popen(build_argv(settings, command, track_cwd=False), cwd, subprocess.PIPE)
        log = StreamLog()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        thread = threading.Thread(
            target=_pump, args=(proc.stdout, lambda b: log.append(clean_output(decoder.decode(b)))), daemon=True
        )
        thread.start()
        with self._lock:
            self._counter += 1
            bp = BackgroundProcess(f"p{self._counter}", command, cwd, proc, log)
            self._procs[bp.id] = bp
        return bp

    def get(self, process_id: str) -> BackgroundProcess:
        bp = self._procs.get(process_id.strip())
        if bp is None:
            known = ", ".join(self._procs) or "none"
            raise ToolError(f"No background process with id {process_id!r}. Known ids: {known}.")
        return bp

    def list(self) -> list[BackgroundProcess]:
        return list(self._procs.values())

    def stop(self, process_id: str) -> BackgroundProcess:
        bp = self.get(process_id)
        if bp.running:
            kill_tree(bp.proc)
        try:
            bp.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        return bp

    def stop_all(self) -> None:
        for bp in list(self._procs.values()):
            if bp.running:
                kill_tree(bp.proc)


def _wait_for_output(bp: BackgroundProcess, seconds: float, wait_for: str | None) -> None:
    deadline = time.monotonic() + max(0.0, min(float(seconds), 300.0))
    while time.monotonic() < deadline:
        if not bp.running:
            time.sleep(0.2)  # let the reader drain the pipe
            return
        if wait_for and wait_for in bp.log.peek_new():
            return
        time.sleep(0.1)


def _format_new_output(bp: BackgroundProcess, max_chars: int) -> str:
    text, skipped = bp.log.read_new()
    text = text.rstrip("\n")
    note = f"[{skipped:,} earlier characters were dropped]\n" if skipped else ""
    return note + (truncate_middle(text, max_chars) if text.strip() else "(no new output)")


@tool(category="execute", summarize=lambda a: str(a.get("command", "")), preview=_command_preview)
def start_process(ctx: ToolContext, command: str, cwd: str | None = None, wait_seconds: float = 3) -> str:
    """Start a long-running command (dev server, watcher, long build...) in the background.

    Returns a process id plus the output produced during the first few seconds. Check on it later with
    process_output, send input with process_input and terminate it with stop_process.

    Args:
        command: The shell command to run.
        cwd: Directory to run in (defaults to the current working directory).
        wait_seconds: Seconds to wait for initial output before returning (max 300).
    """
    workdir = ctx.resolve(cwd) if cwd else ctx.cwd
    if not workdir.is_dir():
        raise ToolError(f"Working directory does not exist: {workdir}")
    bp = ctx.processes.start(ctx.settings, command, workdir)
    _wait_for_output(bp, wait_seconds, None)
    output = _format_new_output(bp, ctx.settings.max_output_chars)
    return f"Started process {bp.id} (pid {bp.proc.pid}). Status: {bp.status()}.\nOutput so far:\n{output}"


@tool(category="internal", summarize=lambda a: str(a.get("process_id", "")))
def process_output(ctx: ToolContext, process_id: str, wait_seconds: float = 0, wait_for: str | None = None) -> str:
    """Get a background process's status and the output it produced since the last check.

    Args:
        process_id: Id returned by start_process (e.g. 'p1').
        wait_seconds: Wait up to this many seconds for more output first (max 300). Ends early when the
            process exits or `wait_for` appears.
        wait_for: Text to wait for in the output, e.g. 'Listening on'.
    """
    bp = ctx.processes.get(process_id)
    if wait_seconds:
        _wait_for_output(bp, wait_seconds, wait_for)
    output = _format_new_output(bp, ctx.settings.max_output_chars)
    return f"Process {bp.id} ({bp.command}): {bp.status()}\n{output}"


@tool(category="execute", summarize=lambda a: f"{a.get('process_id', '')} <- {a.get('text', '')}",
      preview=lambda ctx, a: (str(a.get("text", "")), "text"))
def process_input(ctx: ToolContext, process_id: str, text: str, newline: bool = True) -> str:
    """Send text to a background process's standard input.

    Args:
        process_id: Id of the process.
        text: Text to send.
        newline: Append a newline (press Enter) after the text.
    """
    bp = ctx.processes.get(process_id)
    if not bp.running or bp.proc.stdin is None:
        raise ToolError(f"Process {bp.id} is not running ({bp.status()}).")
    try:
        bp.proc.stdin.write((text + ("\n" if newline else "")).encode("utf-8"))
        bp.proc.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        raise ToolError(f"Could not write to process {bp.id}: {exc}") from exc
    _wait_for_output(bp, 1.0, None)
    return f"Sent input to {bp.id}. New output:\n{_format_new_output(bp, ctx.settings.max_output_chars)}"


@tool(category="internal", summarize=lambda a: str(a.get("process_id", "")))
def stop_process(ctx: ToolContext, process_id: str) -> str:
    """Stop a background process (and its child processes) and return its remaining output.

    Args:
        process_id: Id of the process to stop.
    """
    bp = ctx.processes.stop(process_id)
    time.sleep(0.2)
    return f"Stopped {bp.id} ({bp.status()}). Final output:\n{_format_new_output(bp, ctx.settings.max_output_chars)}"


@tool(category="internal")
def list_processes(ctx: ToolContext) -> str:
    """List background processes started with start_process and their status."""
    procs = ctx.processes.list()
    if not procs:
        return "No background processes."
    now = time.time()
    return "\n".join(
        f"{bp.id}: {bp.status()} | {int(now - bp.started)}s | cwd {bp.cwd} | {bp.command}" for bp in procs
    )


SHELL_TOOLS: list[Tool] = [run_command, start_process, process_output, process_input, stop_process, list_processes]

"""File-system tools: read, write, edit, list, find, search, delete (+ undo history)."""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from kimi_agent.media import IMAGE_MIME, pdf_to_text, prepare_image
from kimi_agent.tools.base import Tool, ToolContext, ToolError, ToolResult, tool

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", ".next", ".idea", ".gradle", ".terraform",
}
MAX_LINE_CHARS = 2000
LEXERS = {
    ".py": "python", ".js": "javascript", ".jsx": "jsx", ".ts": "typescript", ".tsx": "tsx", ".json": "json",
    ".md": "markdown", ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".yml": "yaml", ".yaml": "yaml",
    ".toml": "toml", ".html": "html", ".css": "css", ".rs": "rust", ".go": "go", ".java": "java",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".rb": "ruby", ".php": "php", ".sql": "sql",
    ".kt": "kotlin", ".swift": "swift", ".cs": "csharp", ".lua": "lua", ".ini": "ini", ".xml": "xml",
}


# --------------------------------------------------------------------------- helpers

def guess_lexer(path: str | Path) -> str:
    return LEXERS.get(Path(str(path)).suffix.lower(), "text")


def read_text(path: Path) -> str:
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def is_binary(sample: bytes) -> bool:
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
        return False
    except UnicodeDecodeError as exc:
        if exc.start >= len(sample) - 3:  # multi-byte character cut at the sample boundary
            return False
    control = sum(1 for b in sample if b < 32 and b not in (9, 10, 12, 13, 27))
    return control / len(sample) > 0.1


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def unified_diff(old: str, new: str, label: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=f"a/{label}", tofile=f"b/{label}"
    )
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


def number_lines(text: str, offset: int, limit: int, label: str, char_budget: int) -> str:
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return f"{label} is empty."
    offset = max(1, int(offset))
    limit = max(1, int(limit))
    if offset > total:
        raise ToolError(f"offset {offset} is past the end of the file ({total} lines).")
    end = min(total, offset + limit - 1)
    width = len(str(end))
    out: list[str] = []
    used = 0
    last = offset - 1
    for i in range(offset, end + 1):
        line = lines[i - 1]
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + f" [... line truncated, {len(line):,} chars total]"
        entry = f"{i:>{width}}\t{line}"
        if used + len(entry) > char_budget and out:
            break
        out.append(entry)
        used += len(entry) + 1
        last = i
    result = "\n".join(out)
    if offset > 1 or last < total:
        result += f"\n\n[Showing lines {offset}-{last} of {total}."
        result += f" Use offset={last + 1} to continue reading.]" if last < total else "]"
    return result


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob (supports *, **, ?, [abc], {a,b}) to a compiled regex."""
    i, n, out = 0, len(pattern), []
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j + 1
                continue
        elif c == "{":
            j = pattern.find("}", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                alternatives = pattern[i + 1 : j].split(",")
                out.append("(?:" + "|".join(glob_to_regex(a).pattern[1:-1] for a in alternatives) + ")")
                i = j + 1
                continue
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def iter_files(root: Path, include_hidden: bool = False):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in IGNORED_DIRS and (include_hidden or not d.startswith("."))
        )
        for name in sorted(filenames):
            if include_hidden or not name.startswith("."):
                yield Path(dirpath) / name


def _record(ctx: ToolContext, path: Path) -> None:
    if ctx.file_history is not None:
        ctx.file_history.record(path)


class FileHistory:
    """Snapshots of files before modification, grouped by user turn, for /undo."""

    def __init__(self, max_entries: int = 500) -> None:
        self._entries: list[tuple[int, Path, bytes | None]] = []
        self._turn = 0
        self.max_entries = max_entries

    def begin_turn(self) -> None:
        self._turn += 1

    def record(self, path: Path) -> None:
        try:
            before = path.read_bytes() if path.is_file() else None
        except OSError:
            return
        self._entries.append((self._turn, path, before))
        if len(self._entries) > self.max_entries:
            del self._entries[0]

    @property
    def turn(self) -> int:
        return self._turn

    def undo_last_turn(self) -> list[str]:
        if not self._entries:
            return []
        return self.undo_turn(self._entries[-1][0])

    def undo_turn(self, turn: int) -> list[str]:
        """Revert the changes recorded during ``turn`` (only if they are the most recent ones)."""
        messages: list[str] = []
        while self._entries and self._entries[-1][0] == turn:
            _, path, before = self._entries.pop()
            try:
                if before is None:
                    if path.exists():
                        path.unlink()
                        messages.append(f"removed {path}")
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(before)
                    messages.append(f"restored {path}")
            except OSError as exc:
                messages.append(f"failed to restore {path}: {exc}")
        return messages

    def __len__(self) -> int:
        return len(self._entries)


# --------------------------------------------------------------------------- read

def _summary_read(args: dict[str, Any]) -> str:
    path = str(args.get("path", ""))
    if args.get("offset") and int(args["offset"]) > 1:
        return f"{path} (from line {args['offset']})"
    return path


@tool(category="read", summarize=_summary_read)
def read_file(ctx: ToolContext, path: str, offset: int = 1, limit: int = 2000) -> ToolResult:
    """Read a file from the local filesystem.

    Text files are returned with line numbers (like `cat -n`). Images (png, jpg, gif, webp) are shown to
    you when vision is enabled. PDFs are converted to text. Always read a file before editing it.

    Args:
        path: Path to the file (absolute, or relative to the current working directory).
        offset: 1-based line number to start reading from.
        limit: Maximum number of lines to return.
    """
    p = ctx.resolve(path)
    if not p.exists():
        raise ToolError(f"File not found: {p}")
    if p.is_dir():
        raise ToolError(f"{p} is a directory. Use list_directory to see its contents.")
    suffix = p.suffix.lower()
    if suffix in IMAGE_MIME:
        size = p.stat().st_size
        if not ctx.vision:
            return ToolResult(f"{p} is an image ({size:,} bytes). Vision is disabled, so it cannot be displayed.")
        image, desc = prepare_image(p.read_bytes(), IMAGE_MIME[suffix])
        return ToolResult(f"Image {p} ({desc}) is attached.", images=[image])
    data = p.read_bytes()
    if suffix == ".pdf":
        text = pdf_to_text(data)
    else:
        if is_binary(data[:8192]):
            raise ToolError(
                f"{p} appears to be a binary file ({len(data):,} bytes). "
                "Use run_command (e.g. `file`, `xxd | head`) to inspect it."
            )
        text = data.decode("utf-8", errors="replace")
    budget = max(2000, int(ctx.settings.max_output_chars * 0.9))
    return ToolResult(number_lines(text, offset, limit, str(p), budget))


# --------------------------------------------------------------------------- write / edit

def _preview_write(ctx: ToolContext, args: dict[str, Any]) -> tuple[str, str]:
    p = ctx.resolve(args.get("path", ""))
    new = str(args.get("content", ""))
    if p.is_file():
        try:
            diff = unified_diff(read_text(p), new, ctx.display(p))
        except OSError:
            diff = ""
        return (diff or "(no changes)"), "diff"
    return new, guess_lexer(p)


@tool(
    category="write",
    summarize=lambda a: f"{a.get('path', '')} ({len(str(a.get('content', '')).splitlines())} lines)",
    preview=_preview_write,
)
def write_file(ctx: ToolContext, path: str, content: str) -> str:
    """Create a new file or completely overwrite an existing file.

    Parent directories are created automatically. Prefer edit_file to change part of an existing file.

    Args:
        path: Path of the file to write.
        content: The complete content of the file.
    """
    p = ctx.resolve(path)
    if p.is_dir():
        raise ToolError(f"{p} is a directory.")
    existed = p.exists()
    _record(ctx, p)
    write_text(p, content)
    lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return f"{'Overwrote' if existed else 'Created'} {p} ({lines} lines, {len(content.encode('utf-8')):,} bytes)."


def apply_edit(text: str, old: str, new: str, replace_all: bool) -> tuple[str, int, int, str]:
    """Return (new_text, replacements, first_match_position, inserted_text). Raises ToolError with guidance."""
    if not old:
        raise ToolError("old_string is empty. To create or overwrite a whole file use write_file.")
    if old == new:
        raise ToolError("old_string and new_string are identical; nothing to change.")
    count = text.count(old)
    if count == 0 and "\r\n" in text and "\n" in old and "\r\n" not in old:
        crlf_old = old.replace("\n", "\r\n")
        if text.count(crlf_old):
            old, new = crlf_old, new.replace("\r\n", "\n").replace("\n", "\r\n")
            count = text.count(old)
    if count == 0:
        raise ToolError(_not_found_message(text, old))
    if count > 1 and not replace_all:
        lines = [text.count("\n", 0, m.start()) + 1 for m in re.finditer(re.escape(old), text)][:10]
        raise ToolError(
            f"old_string matches {count} locations (lines {', '.join(map(str, lines))}). "
            "Include more surrounding context to make it unique, or set replace_all to true."
        )
    position = text.find(old)
    if replace_all:
        return text.replace(old, new), count, position, new
    return text.replace(old, new, 1), 1, position, new


def _not_found_message(text: str, old: str) -> str:
    message = "old_string was not found in the file. It must match exactly, including whitespace and indentation."
    first = next((line.strip() for line in old.splitlines() if line.strip()), "")
    if first:
        for number, line in enumerate(text.splitlines(), 1):
            if line.strip() == first:
                message += (
                    f" A line matching the first line of old_string (ignoring indentation) is at line {number}; "
                    "check the exact whitespace."
                )
                break
    return message + " Re-read the file with read_file to get its current content."


def _snippet(text: str, position: int, length: int, context: int = 3, max_lines: int = 30) -> str:
    start_line = text.count("\n", 0, position)
    end_line = start_line + text.count("\n", position, position + length)
    lines = text.splitlines()
    lo = max(0, start_line - context)
    hi = min(len(lines), end_line + context + 1, lo + max_lines)
    width = len(str(hi))
    return "\n".join(f"{i + 1:>{width}}\t{lines[i]}" for i in range(lo, hi))


def _preview_edit(ctx: ToolContext, args: dict[str, Any]) -> tuple[str, str]:
    p = ctx.resolve(args.get("path", ""))
    try:
        old_text = read_text(p)
        new_text, _, _, _ = apply_edit(
            old_text, str(args.get("old_string", "")), str(args.get("new_string", "")),
            bool(args.get("replace_all", False)),
        )
    except (OSError, ToolError) as exc:
        return f"(edit will fail: {exc})", "text"
    return unified_diff(old_text, new_text, ctx.display(p)), "diff"


@tool(category="write", summarize=lambda a: str(a.get("path", "")), preview=_preview_edit)
def edit_file(ctx: ToolContext, path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Edit a file by replacing an exact string with a new string.

    old_string must match the file exactly (including whitespace and indentation) and must be unique in
    the file unless replace_all is true; include enough surrounding lines to make it unique. Read the file
    first. Use write_file to create new files.

    Args:
        path: Path of the file to edit.
        old_string: The exact existing text to replace.
        new_string: The replacement text.
        replace_all: Replace every occurrence instead of requiring a unique match.
    """
    p = ctx.resolve(path)
    if not p.is_file():
        raise ToolError(f"File not found: {p}. Use write_file to create a new file.")
    text = read_text(p)
    new_text, count, position, inserted = apply_edit(text, old_string, new_string, replace_all)
    _record(ctx, p)
    write_text(p, new_text)
    snippet = _snippet(new_text, position, len(inserted))
    plural = "s" if count != 1 else ""
    return f"Edited {p} ({count} replacement{plural}). Updated region:\n{snippet}"


# --------------------------------------------------------------------------- list / find / search

@tool(category="read", summarize=lambda a: str(a.get("path", ".")))
def list_directory(ctx: ToolContext, path: str = ".", depth: int = 2, show_hidden: bool = False) -> str:
    """List files and directories as a tree (directories end with '/').

    Bulky directories such as .git, node_modules and virtualenvs are shown but not expanded.

    Args:
        path: Directory to list.
        depth: How many levels deep to recurse (1 = only direct children).
        show_hidden: Include entries whose names start with '.'.
    """
    root = ctx.resolve(path)
    if not root.exists():
        raise ToolError(f"Directory not found: {root}")
    if not root.is_dir():
        raise ToolError(f"{root} is a file, not a directory.")
    limit = 500
    lines = [f"{root}/"]
    count = 0

    def walk(directory: Path, prefix: str, level: int) -> None:
        nonlocal count
        try:
            entries = sorted(os.scandir(directory), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError as exc:
            lines.append(f"{prefix}[cannot read: {exc.strerror}]")
            return
        for entry in entries:
            if not show_hidden and entry.name.startswith("."):
                continue
            if count >= limit:
                return
            count += 1
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                if entry.name in IGNORED_DIRS:
                    lines.append(f"{prefix}{entry.name}/ (not expanded)")
                    continue
                suffix = " -> symlink" if entry.is_symlink() else ""
                lines.append(f"{prefix}{entry.name}/{suffix}")
                if level < depth and not entry.is_symlink():
                    walk(Path(entry.path), prefix + "  ", level + 1)
            else:
                try:
                    size = human_size(entry.stat(follow_symlinks=False).st_size)
                except OSError:
                    size = "?"
                lines.append(f"{prefix}{entry.name} ({size})")

    walk(root, "  ", 1)
    if count >= limit:
        lines.append(f"[Listing truncated at {limit} entries; list a subdirectory or reduce depth.]")
    if len(lines) == 1:
        lines.append("  (empty)")
    return "\n".join(lines)


@tool(category="read", summarize=lambda a: f"{a.get('pattern', '')} in {a.get('path', '.')}")
def find_files(ctx: ToolContext, pattern: str, path: str = ".", limit: int = 200) -> str:
    """Find files by name with a glob pattern; results are sorted newest first.

    Patterns without '/' match file names at any depth (e.g. '*.py', 'test_*.ts'). Patterns containing '/'
    match paths relative to the search directory (e.g. 'src/**/*.tsx'). Supports *, **, ?, [abc] and {a,b}.

    Args:
        pattern: Glob pattern to match.
        path: Directory to search in.
        limit: Maximum number of results.
    """
    root = ctx.resolve(path)
    if not root.is_dir():
        raise ToolError(f"Not a directory: {root}")
    pattern = pattern.strip()
    while pattern.startswith("./"):
        pattern = pattern[2:]
    regex = glob_to_regex(pattern)
    match_name = "/" not in pattern
    include_hidden = pattern.startswith(".") or "/." in pattern
    matches: list[Path] = []
    for file in iter_files(root, include_hidden):
        target = file.name if match_name else file.relative_to(root).as_posix()
        if regex.match(target):
            matches.append(file)
            if len(matches) >= 20_000:
                break

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    matches.sort(key=mtime, reverse=True)
    if not matches:
        return f"No files matching '{pattern}' under {root}."
    lines = [ctx.display(p) for p in matches[:limit]]
    if len(matches) > limit:
        lines.append(f"[{len(matches) - limit} more matches not shown]")
    return "\n".join(lines)


def _search_with_rg(rg: str, ctx: ToolContext, target: Path, pattern: str, glob: str | None,
                    ignore_case: bool, context_lines: int) -> list[str]:
    cmd = [rg, "--line-number", "--no-heading", "--color=never", "--max-columns=400",
           "--max-columns-preview", "--max-filesize=20M"]
    if ignore_case:
        cmd.append("--ignore-case")
    if context_lines:
        cmd += ["--context", str(context_lines)]
    if glob:
        cmd += ["--glob", glob]
    if not any(part in IGNORED_DIRS for part in target.parts):
        # Match the Python fallback: skip vendored/cache dirs even without a .gitignore.
        for name in sorted(IGNORED_DIRS):
            cmd += ["--glob", f"!{name}/"]
    display_target = ctx.display(target)
    cmd += ["--regexp", pattern, "--", display_target]
    try:
        proc = subprocess.run(cmd, cwd=ctx.cwd, capture_output=True, text=True, errors="replace", timeout=60)
    except subprocess.TimeoutExpired:
        raise ToolError("Search timed out after 60s; narrow the path or pattern.") from None
    if proc.returncode == 1:
        return []
    if proc.returncode not in (0, 1):
        raise ToolError(f"Search failed: {proc.stderr.strip()}")
    return proc.stdout.splitlines()


def _search_with_python(ctx: ToolContext, target: Path, pattern: str, glob: str | None,
                        ignore_case: bool, context_lines: int, max_results: int) -> list[str]:
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise ToolError(f"Invalid regular expression: {exc}") from None
    glob_regex = glob_to_regex(glob) if glob else None
    files = [target] if target.is_file() else iter_files(target)
    out: list[str] = []
    for file in files:
        if glob_regex and not glob_regex.match(file.name):
            continue
        try:
            if file.stat().st_size > 20 * 1024 * 1024:
                continue
            data = file.read_bytes()
        except OSError:
            continue
        if is_binary(data[:8192]):
            continue
        lines = data.decode("utf-8", errors="replace").splitlines()
        hits = [i for i, line in enumerate(lines) if regex.search(line)]
        if not hits:
            continue
        label = ctx.display(file)
        if context_lines:
            hit_set = set(hits)
            shown = sorted({j for i in hits for j in range(max(0, i - context_lines),
                                                           min(len(lines), i + context_lines + 1))})
            previous = None
            for j in shown:
                if previous is not None and j != previous + 1:
                    out.append("--")
                sep = ":" if j in hit_set else "-"
                out.append(f"{label}{sep}{j + 1}{sep}{lines[j][:400]}")
                previous = j
        else:
            out.extend(f"{label}:{i + 1}:{lines[i][:400]}" for i in hits)
        if len(out) > max_results * 2:
            break
    return out


@tool(category="read", summarize=lambda a: f"/{a.get('pattern', '')}/ in {a.get('path', '.')}")
def search_files(
    ctx: ToolContext,
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    ignore_case: bool = False,
    context_lines: int = 0,
    max_results: int = 200,
) -> str:
    """Search file contents with a regular expression (like `grep -rn`), returning `path:line:text` matches.

    Uses ripgrep when installed (respects .gitignore). Searches recursively when path is a directory.

    Args:
        pattern: Regular expression to search for.
        path: File or directory to search.
        glob: Only search files matching this glob, e.g. '*.py' or '*.{ts,tsx}'.
        ignore_case: Case-insensitive matching.
        context_lines: Lines of context to show around each match.
        max_results: Maximum number of output lines.
    """
    target = ctx.resolve(path)
    if not target.exists():
        raise ToolError(f"Path not found: {target}")
    rg = shutil.which("rg")
    if rg:
        lines = _search_with_rg(rg, ctx, target, pattern, glob, ignore_case, context_lines)
    else:
        lines = _search_with_python(ctx, target, pattern, glob, ignore_case, context_lines, max_results)
    if not lines:
        return f"No matches for /{pattern}/ in {ctx.display(target)}."
    if len(lines) > max_results:
        return "\n".join(lines[:max_results]) + f"\n[{len(lines) - max_results} more lines not shown; refine the search]"
    return "\n".join(lines)


# --------------------------------------------------------------------------- delete

def _preview_delete(ctx: ToolContext, args: dict[str, Any]) -> tuple[str, str]:
    p = ctx.resolve(args.get("path", ""))
    if p.is_dir():
        try:
            count = sum(1 for _ in p.rglob("*"))
        except OSError:
            count = -1
        return f"Delete directory {p} ({count} entries inside)", "text"
    return f"Delete file {p}", "text"


@tool(category="write", summarize=lambda a: str(a.get("path", "")), preview=_preview_delete)
def delete_path(ctx: ToolContext, path: str, recursive: bool = False) -> str:
    """Delete a file or directory. Deleted files (not directories) can be restored by the user with /undo.

    Args:
        path: Path of the file or directory to delete.
        recursive: Required to delete a non-empty directory and everything inside it.
    """
    p = ctx.resolve(path)
    if p == Path(p.anchor) or p == Path.home():
        raise ToolError(f"Refusing to delete {p}.")
    if not p.exists() and not p.is_symlink():
        raise ToolError(f"Not found: {p}")
    if p.is_dir() and not p.is_symlink():
        if any(p.iterdir()) and not recursive:
            raise ToolError(f"{p} is a non-empty directory; set recursive=true to delete it and its contents.")
        shutil.rmtree(p)
        return f"Deleted directory {p}."
    _record(ctx, p)
    p.unlink()
    return f"Deleted {p}."


FILE_TOOLS: list[Tool] = [read_file, write_file, edit_file, list_directory, find_files, search_files, delete_path]

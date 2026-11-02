"""@path mentions: attach files, directory listings, images and PDFs to a user message."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .media import IMAGE_MIME, pdf_to_text, prepare_image
from .tools.base import ToolContext, ToolError
from .tools.files import IGNORED_DIRS, is_binary

MENTION_RE = re.compile(r'(?<![\w@])@("([^"]+)"|(\S+))')
MAX_ATTACHMENT_CHARS = 100_000


def _directory_listing(path: Path, limit: int = 200) -> str:
    entries = []
    try:
        for entry in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if entry.name in IGNORED_DIRS:
                continue
            entries.append(entry.name + ("/" if entry.is_dir() else ""))
            if len(entries) >= limit:
                entries.append("…")
                break
    except OSError as exc:
        return f"(cannot list: {exc})"
    return "\n".join(entries) or "(empty)"


def expand_mentions(text: str, ctx: ToolContext, vision: bool) -> tuple[Any, list[str]]:
    """Attach files referenced as @path. Returns (message content, notes for the user)."""
    blocks: list[str] = []
    images: list[Any] = []
    notes: list[str] = []
    seen: set[Path] = set()
    for match in MENTION_RE.finditer(text):
        raw = match.group(2) or match.group(3).rstrip(".,;:!?)]}'\"")
        path = ctx.resolve(raw)
        if path in seen or not path.exists():
            continue
        seen.add(path)
        shown = ctx.display(path)
        try:
            if path.is_dir():
                blocks.append(f'<directory path="{shown}">\n{_directory_listing(path)}\n</directory>')
                notes.append(f"attached directory listing {shown}")
                continue
            data = path.read_bytes()
            suffix = path.suffix.lower()
            if suffix in IMAGE_MIME:
                if vision:
                    image, desc = prepare_image(data, IMAGE_MIME[suffix])
                    images.append(image)
                    notes.append(f"attached image {shown} ({desc})")
                else:
                    notes.append(f"skipped image {shown}: vision is off for this model")
                continue
            if suffix == ".pdf":
                content = pdf_to_text(data)
            elif is_binary(data[:8192]):
                notes.append(f"skipped binary file {shown}")
                continue
            else:
                content = data.decode("utf-8", errors="replace")
        except (OSError, ToolError) as exc:
            notes.append(f"could not attach {shown}: {exc}")
            continue
        if len(content) > MAX_ATTACHMENT_CHARS:
            content = content[:MAX_ATTACHMENT_CHARS] + "\n[... truncated; use read_file for the rest ...]"
            notes.append(f"attached {shown} (truncated)")
        else:
            notes.append(f"attached {shown}")
        blocks.append(f'<file path="{shown}">\n{content.rstrip(chr(10))}\n</file>')
    if not blocks and not images:
        return text, notes
    body = text + ("\n\n" + "\n\n".join(blocks) if blocks else "")
    if images:
        parts: list[dict[str, Any]] = [{"type": "text", "text": body}]
        parts += [{"type": "image_url", "image_url": {"url": img.data_url()}} for img in images]
        return parts, notes
    return body, notes

"""Conversation persistence: save, list and resume sessions as JSON files."""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import secrets
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .llm import content_text, strip_images

# Attachments added to a request (!cmd output, @file contents) are not part of the title.
ATTACHMENT_RE = re.compile(r"<(shell_command|file|directory)\b[^>]*>.*?</\1>", re.S)


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Session:
    id: str
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)
    cwd: str = ""
    provider: str = ""
    model: str = ""
    title: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    plan: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def turns(self) -> int:
        return sum(1 for m in self.messages if m.get("role") == "user" and m.get("_turn"))


def new_session_id() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


def make_title(messages: list[dict[str, Any]], limit: int = 70) -> str:
    for msg in messages:
        if msg.get("role") == "user" and msg.get("_turn"):
            raw = content_text(msg.get("content"))
            text = " ".join(ATTACHMENT_RE.sub(" ", raw).split()) or " ".join(raw.split())
            if text:
                return text if len(text) <= limit else text[: limit - 1] + "…"
    return "(empty)"


class SessionStore:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def path(self, session_id: str) -> Path:
        return self.directory / f"{session_id}.json"

    def save(self, session: Session) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        session.updated = _now()
        if not session.title:
            session.title = make_title(session.messages)
        data = asdict(session)
        data["messages"] = strip_images(session.messages, "[image not saved in session]")
        target = self.path(session.id)
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return target

    def load(self, session_id: str) -> Session:
        """Load by exact id or unique prefix. Raises FileNotFoundError / ValueError."""
        path = self.path(session_id)
        if not path.is_file():
            matches = sorted(self.directory.glob(f"{session_id}*.json")) if self.directory.is_dir() else []
            if not matches:
                raise FileNotFoundError(f"No session matching '{session_id}'.")
            if len(matches) > 1:
                ids = ", ".join(m.stem for m in matches[:5])
                raise ValueError(f"Session id '{session_id}' is ambiguous: {ids}")
            path = matches[0]
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f for f in Session.__dataclass_fields__}
        return Session(**{k: v for k, v in data.items() if k in known})

    def list(self, limit: int = 20, cwd: str | None = None) -> list[Session]:
        if not self.directory.is_dir():
            return []
        files = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        sessions = []
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if cwd and data.get("cwd") != cwd:
                continue
            data["messages"] = data.get("messages", [])
            known = {f for f in Session.__dataclass_fields__}
            sessions.append(Session(**{k: v for k, v in data.items() if k in known}))
            if len(sessions) >= limit:
                break
        return sessions

    def latest(self, cwd: str | None = None) -> Session | None:
        sessions = self.list(limit=1, cwd=cwd)
        return sessions[0] if sessions else None

    def delete(self, session_id: str) -> None:
        self.path(session_id).unlink(missing_ok=True)


def export_markdown(messages: list[dict[str, Any]], title: str = "Kimi Agent session") -> str:
    lines = [f"# {title}", ""]
    for msg in messages:
        role = msg.get("role")
        text = content_text(msg.get("content")).strip()
        if role == "system":
            continue
        if role == "user":
            heading = "Summary of earlier conversation" if msg.get("_summary") else "User"
            lines += [f"## {heading}", "", text, ""]
        elif role == "assistant":
            if text:
                lines += ["## Agent", "", text, ""]
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                lines += [f"**Tool call:** `{fn.get('name')}`", "", "```json", fn.get("arguments", ""), "```", ""]
        elif role == "tool":
            if len(text) > 5_000:
                text = text[:5_000] + "\n[... truncated ...]"
            lines += [f"**Result ({msg.get('name', 'tool')}):**", "", "```", text, "```", ""]
    return "\n".join(lines)

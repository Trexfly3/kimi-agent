"""Context-window management: token estimation, pruning and LLM-based compaction."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from .llm import ContextOverflowError, LLMError, Usage, content_text, strip_images
from .tools.base import truncate_middle

log = logging.getLogger("kimi_agent.context")

IMAGE_CHARS = 3_000  # an image costs very roughly 1k tokens
DEFAULT_TOKENS_PER_CHAR = 1 / 3.2
KEEP_IMAGES = 3
SUMMARY_TAG = "conversation_summary"

SUMMARY_SYSTEM = (
    "You are a precise assistant that compresses an AI agent's working history into a summary the agent can "
    "continue from. You never call tools and never continue the task yourself."
)

SUMMARY_INSTRUCTIONS = """Above is the earlier part of a session between a user and an autonomous AI agent that works \
on the user's computer with tools. The agent's context window is full, so this part will be replaced by your summary. \
Write a dense, factual summary that lets the agent continue seamlessly. Include:

1. User requests: every task, question and preference the user stated (quote key wording and constraints).
2. Progress: what has been done, decisions made and why, and what was verified (tests run, outputs observed).
3. Files and resources: paths of files read, created or modified with the essence of each change; important \
code snippets, commands, URLs, ports, process ids, credentials locations (never secret values).
4. Errors and fixes: problems hit and how they were resolved, plus dead ends to avoid.
5. Current state and next steps: what was being worked on right before this point and what remains to do.
{focus}
Write in English unless the user wrote in another language. Use concise Markdown bullet points. Do not invent \
anything that is not in the history."""


def message_chars(msg: dict[str, Any]) -> int:
    total = 0
    content = msg.get("content")
    if isinstance(content, str):
        total += len(content)
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                total += IMAGE_CHARS
            else:
                total += len(str(part.get("text", "")))
    for key in ("reasoning_content", "reasoning"):
        value = msg.get(key)
        if isinstance(value, str):
            total += len(value)
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        total += len(fn.get("name", "")) + len(fn.get("arguments", "")) + 30
    return total + 12  # role and formatting overhead


def raw_size(messages: list[dict[str, Any]], schemas: list[dict[str, Any]] | None = None) -> int:
    size = sum(message_chars(m) for m in messages)
    if schemas:
        size += len(json.dumps(schemas, ensure_ascii=False))
    return size


class ContextManager:
    def __init__(self, settings: Any, llm: Any) -> None:
        self.settings = settings
        self.llm = llm
        self.tokens_per_char = DEFAULT_TOKENS_PER_CHAR
        self.last_prompt_tokens = 0
        self.compactions = 0
        self.window_limit = 0  # learned from overflow errors

    @property
    def window(self) -> int:
        configured = int(self.settings.context_window or 128_000)
        return min(configured, self.window_limit) if self.window_limit else configured

    def on_overflow(self, messages: list[dict[str, Any]], schemas: list[dict[str, Any]] | None = None) -> None:
        """The server rejected a request as too long: assume the real window is smaller than that request."""
        estimate = self.estimate(messages, schemas)
        if estimate < self.window:
            self.window_limit = max(2_048, int(estimate * 0.8))
            log.warning("context overflow at ~%d tokens; assuming a window of %d", estimate, self.window_limit)

    @property
    def threshold(self) -> int:
        ratio = min(max(float(self.settings.compact_threshold or 0.8), 0.3), 0.95)
        return int(self.window * ratio)

    def estimate(self, messages: list[dict[str, Any]], schemas: list[dict[str, Any]] | None = None) -> int:
        return int(raw_size(messages, schemas) * self.tokens_per_char)

    def calibrate(self, messages: list[dict[str, Any]], schemas: list[dict[str, Any]] | None,
                  usage: Usage | None) -> None:
        """Learn the chars-per-token ratio of the current model from real usage numbers."""
        if usage is None or usage.prompt_tokens <= 0:
            return
        self.last_prompt_tokens = usage.prompt_tokens
        size = raw_size(messages, schemas)
        if size < 2_000:
            return
        ratio = min(max(usage.prompt_tokens / size, 0.12), 1.2)
        self.tokens_per_char = 0.5 * self.tokens_per_char + 0.5 * ratio if self.compactions else ratio

    def needs_compaction(self, messages: list[dict[str, Any]], schemas: list[dict[str, Any]] | None = None) -> bool:
        return self.estimate(messages, schemas) > self.threshold

    def fixed_tokens(self, messages: list[dict[str, Any]], schemas: list[dict[str, Any]] | None = None) -> int:
        """Tokens compaction cannot reduce: the system prompt and the tool definitions."""
        return self.estimate([m for m in messages[:1] if m.get("role") == "system"], schemas)

    # -- pruning
    @staticmethod
    def prune(messages: list[dict[str, Any]], keep_recent: int = 8, tool_limit: int = 4_000,
              keep_images: int = KEEP_IMAGES) -> list[dict[str, Any]]:
        """Cheap, lossy reduction: drop old images and shorten old tool outputs."""
        out = list(messages)
        with_images = [i for i, m in enumerate(out) if isinstance(m.get("content"), list)
                       and any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])]
        for i in with_images[:-keep_images] if keep_images else with_images:
            out[i] = strip_images([out[i]], "[older image removed to save context]")[0]
        cutoff = max(0, len(out) - keep_recent)
        for i in range(cutoff):
            m = out[i]
            if m.get("role") == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > tool_limit:
                out[i] = {**m, "content": truncate_middle(m["content"], tool_limit)}
        return out

    # -- compaction
    def maybe_compact(self, messages: list[dict[str, Any]], schemas: list[dict[str, Any]] | None = None, *,
                      force: bool = False, focus: str = "", extra: str = "",
                      on_status: Callable[[str], None] | None = None) -> list[dict[str, Any]] | None:
        """Return a smaller message list when the context is (nearly) full, otherwise None."""
        if not force and not self.needs_compaction(messages, schemas):
            return None
        before = self.estimate(messages, schemas)
        if not force and before - self.fixed_tokens(messages, schemas) < self.window * 0.15:
            return None  # the system prompt and tools fill the window; compacting the short history won't help
        pruned = self.prune(messages)
        if not force and not self.needs_compaction(pruned, schemas):
            log.info("context pruned (%d -> %d est. tokens)", self.estimate(messages, schemas),
                     self.estimate(pruned, schemas))
            return pruned
        if on_status:
            on_status("Compacting conversation history…")
        result = self.summarize(pruned, schemas, focus=focus, extra=extra)
        if result is None or self.needs_compaction(result, schemas):
            # Nothing to summarize (e.g. one huge turn): shrink everything except the very end.
            base = result or pruned
            result = self.prune(base, keep_recent=2, tool_limit=1_500, keep_images=1)
        if self.estimate(result, schemas) >= before:
            return None
        self.compactions += 1
        return result

    def _split_index(self, messages: list[dict[str, Any]], tail_tokens: int) -> int:
        """Index where the kept tail starts. Never splits an assistant tool call from its results."""
        n = len(messages)
        used = 0
        split = n
        for i in range(n - 1, 0, -1):
            used += int(message_chars(messages[i]) * self.tokens_per_char)
            if used > tail_tokens:
                break
            split = i
        split = max(split, 1)
        if split >= n:
            split = n - 1
        if messages[split].get("role") == "tool":
            forward = split
            while forward < n and messages[forward].get("role") == "tool":
                forward += 1
            if forward < n:
                split = forward
            else:
                while split > 1 and messages[split].get("role") == "tool":
                    split -= 1
        return split

    def summarize(self, messages: list[dict[str, Any]], schemas: list[dict[str, Any]] | None = None, *,
                  focus: str = "", extra: str = "") -> list[dict[str, Any]] | None:
        if len(messages) < 3:
            return None
        system = messages[0] if messages[0].get("role") == "system" else None
        start = 1 if system else 0
        split = self._split_index(messages, int(self.window * 0.25))
        head, tail = messages[start:split], messages[split:]
        if not head:
            return None
        summary = self._summarize_text(head, focus)
        if summary is None:
            note = (f"[{len(head)} earlier messages were removed to fit the context window and could not be "
                    "summarized. Ask the user or re-inspect files if you need details from before this point.]")
        else:
            note = (f"<{SUMMARY_TAG}>\n{summary.strip()}\n</{SUMMARY_TAG}>\n\n"
                    "(The earlier part of this session was compacted into the summary above. Continue the work "
                    "from where it left off; re-read files if you need exact contents.)")
        if extra:
            note += f"\n\n{extra}"
        new: list[dict[str, Any]] = [system] if system else []
        new.append({"role": "user", "content": note, "_summary": True})
        if tail and tail[0].get("role") == "user":
            new.append({"role": "assistant", "content": "Understood. I have the summary and will continue from it."})
        new.extend(tail)
        log.info("compacted %d messages into a summary (%s)", len(head), "ok" if summary else "dropped")
        return new

    def render_transcript(self, messages: list[dict[str, Any]], tool_limit: int = 3_000) -> str:
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role")
            text = content_text(msg.get("content")).strip()
            if role == "user":
                if msg.get("_summary"):
                    lines.append(f"[EARLIER SUMMARY]\n{text}")
                else:
                    lines.append(f"[USER]\n{text}")
            elif role == "assistant":
                parts = [text] if text else []
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    parts.append(f"-> called {fn.get('name')}({truncate_middle(fn.get('arguments', ''), 1_500)})")
                lines.append("[AGENT]\n" + "\n".join(parts))
            elif role == "tool":
                lines.append(f"[TOOL RESULT {msg.get('name', '')}]\n{truncate_middle(text, tool_limit)}")
        return "\n\n".join(lines)

    def _summarize_text(self, head: list[dict[str, Any]], focus: str) -> str | None:
        budget_chars = int(self.window * 0.6 / max(self.tokens_per_char, 0.1))
        tool_limit = 3_000
        transcript = self.render_transcript(head, tool_limit)
        while len(transcript) > budget_chars and tool_limit > 300:
            tool_limit //= 2
            transcript = self.render_transcript(head, tool_limit)
        focus_line = f"\nPay special attention to: {focus}\n" if focus else ""
        for _attempt in range(2):
            if len(transcript) > budget_chars:
                transcript = truncate_middle(transcript, budget_chars)
            request = [
                {"role": "system", "content": SUMMARY_SYSTEM},
                {"role": "user", "content": f"<history>\n{transcript}\n</history>\n\n"
                                            + SUMMARY_INSTRUCTIONS.format(focus=focus_line)},
            ]
            try:
                response = self.llm.complete(request, tools=None)
            except ContextOverflowError:
                budget_chars //= 2
                continue
            except LLMError as exc:
                log.warning("summarization failed: %s", exc)
                return None
            if response.content.strip():
                return response.content
            return None
        return None

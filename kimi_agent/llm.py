"""OpenAI-compatible chat client: streaming, native or text-based tool calling, retries and fallbacks."""

from __future__ import annotations

import ast
import json
import logging
import queue
import random
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
import openai

log = logging.getLogger("kimi_agent.llm")

Callback = Callable[[str], None]
T = TypeVar("T")
_SLICE = 0.1  # how often a waiting thread wakes up so it can be interrupted


def _interruptible(fn: Callable[[], T], cleanup: Callable[[T], None] | None = None) -> T:
    """Run a blocking network call in a helper thread and wait for it in short slices.

    A thread blocked in a socket read cannot be interrupted by the desktop app's Stop button (an async
    KeyboardInterrupt only fires between Python bytecodes), so the wait happens here instead. If the wait is
    interrupted, ``cleanup`` is applied to the result once the abandoned call finishes.
    """
    box: dict[str, Any] = {}
    done = threading.Event()
    lock = threading.Lock()

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # handed to the waiting thread
            box["error"] = exc
        with lock:
            done.set()
            abandoned = box.get("abandoned", False)
        if abandoned and cleanup is not None and "value" in box:
            try:
                cleanup(box["value"])
            except Exception:
                log.debug("cleanup after an interrupted request failed", exc_info=True)

    threading.Thread(target=target, name="kimi-agent-request", daemon=True).start()
    try:
        while not done.wait(_SLICE):
            pass
    except BaseException:
        with lock:
            box["abandoned"] = not done.is_set()
        if not box["abandoned"] and cleanup is not None and "value" in box:
            cleanup(box["value"])
        raise
    if "error" in box:
        raise box["error"]
    return box["value"]


def _pumped(items: Iterable[T]) -> Iterator[T]:
    """Iterate ``items`` in a helper thread (see ``_interruptible``), yielding them in this thread."""
    pipe: queue.Queue[tuple[str, Any]] = queue.Queue()

    def target() -> None:
        try:
            for item in items:
                pipe.put(("item", item))
        except BaseException as exc:
            pipe.put(("error", exc))
        else:
            pipe.put(("end", None))

    threading.Thread(target=target, name="kimi-agent-stream", daemon=True).start()
    while True:
        try:
            kind, value = pipe.get(timeout=_SLICE)
        except queue.Empty:
            continue
        if kind == "item":
            yield value
        elif kind == "error":
            raise value
        else:
            return


def _close_quietly(stream: Any) -> None:
    try:
        stream.close()
    except Exception:
        pass


class LLMError(Exception):
    """A user-facing error from the model API."""


class ContextOverflowError(LLMError):
    """The request did not fit in the model's context window."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text (always valid JSON once finalized)
    parse_error: str | None = None  # set when the model produced invalid arguments
    raw_arguments: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "type": "function", "function": {"name": self.name, "arguments": self.arguments}}


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, other: Usage | None) -> None:
        if other is None:
            return
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cached_tokens += other.cached_tokens
        self.reasoning_tokens += other.reasoning_tokens

    @classmethod
    def from_any(cls, raw: Any) -> Usage | None:
        if raw is None:
            return None
        if hasattr(raw, "model_dump"):
            raw = raw.model_dump()
        if not isinstance(raw, dict):
            return None
        prompt_details = raw.get("prompt_tokens_details") or {}
        completion_details = raw.get("completion_tokens_details") or {}
        return cls(
            prompt_tokens=int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0),
            completion_tokens=int(raw.get("completion_tokens") or raw.get("output_tokens") or 0),
            cached_tokens=int((prompt_details or {}).get("cached_tokens") or raw.get("cached_tokens") or 0),
            reasoning_tokens=int((completion_details or {}).get("reasoning_tokens") or 0),
        )


@dataclass
class LLMResponse:
    content: str = ""
    reasoning: str = ""
    reasoning_key: str | None = None  # field name to send reasoning back with ("reasoning_content" / "reasoning")
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: Usage | None = None

    def assistant_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.reasoning and self.reasoning_key:
            message[self.reasoning_key] = self.reasoning
        if self.tool_calls:
            message["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        return message


# --------------------------------------------------------------------------- argument parsing

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z_]*\s*(.*?)\s*```\s*$", re.S)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def parse_arguments(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Leniently parse tool-call arguments. Returns (arguments, error)."""
    if isinstance(raw, dict):
        return raw, None
    if raw is None:
        return {}, None
    text = str(raw).strip()
    if not text:
        return {}, None
    candidates = [text]
    fence = _FENCE_RE.match(text)
    if fence:
        candidates.append(fence.group(1))
    error = ""
    decoder = json.JSONDecoder(strict=False)
    for candidate in candidates:
        for variant in (candidate, _TRAILING_COMMA_RE.sub(r"\1", candidate)):
            try:
                value, end = decoder.raw_decode(variant)
            except ValueError as exc:
                error = str(exc)
                continue
            if isinstance(value, str):  # double-encoded JSON
                try:
                    value = json.loads(value, strict=False)
                except ValueError:
                    pass
            if value is None:
                return {}, None
            if isinstance(value, dict):
                return value, None
            return None, f"expected a JSON object, got {type(value).__name__}"
        try:
            value = ast.literal_eval(candidate)  # Python-style dicts: single quotes, True/False/None
        except (ValueError, SyntaxError, MemoryError, RecursionError, TypeError):
            continue
        if isinstance(value, dict):
            return value, None
    return None, f"invalid JSON ({error})" if error else "invalid JSON"


# --------------------------------------------------------------------------- tool calls embedded in text

TOOL_MARKERS = ("<tool_call>", "<|tool_calls_section_begin|>", "<|tool_call_begin|>", "<function=")
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|\Z)", re.S)
_KIMI_CALL_RE = re.compile(
    r"<\|tool_call_begin\|>\s*(\S+?)\s*<\|tool_call_argument_begin\|>\s*(.*?)\s*(?:<\|tool_call_end\|>|\Z)", re.S
)
_KIMI_STRIP_RE = re.compile(r"<\|tool_calls_section_begin\|>.*|<\|tool_call_begin\|>.*", re.S)
_FUNCTION_XML_RE = re.compile(r"<function=([^>\s]+)>\s*(.*?)\s*(?:</function>|\Z)", re.S)
_PARAMETER_XML_RE = re.compile(r"<parameter=([^>\s]+)>\n?(.*?)\n?(?:</parameter>|(?=<parameter=)|\Z)", re.S)
_FENCED_JSON_RE = re.compile(r"```(?:json|tool_call|tool|javascript)?\s*(\{.*?\})\s*```", re.S)
_THINK_RE = re.compile(r"<think(?:ing)?>(.*?)(?:</think(?:ing)?>|\Z)", re.S)


def kimi_tool_name(raw_id: str) -> str:
    """'functions.read_file:3' -> 'read_file'."""
    name = raw_id.strip()
    if name.startswith("functions."):
        name = name[len("functions."):]
    return re.sub(r":\d+$", "", name)


def _xml_value(text: str) -> Any:
    stripped = text.strip()
    if stripped[:1] in "[{" or stripped in ("true", "false", "null") or re.fullmatch(r"-?\d+(\.\d+)?", stripped):
        try:
            return json.loads(stripped)
        except ValueError:
            pass
    return text


def _xml_call(body: str) -> tuple[str, dict[str, Any]] | None:
    match = _FUNCTION_XML_RE.search(body)
    if not match:
        return None
    params = {k: _xml_value(v) for k, v in _PARAMETER_XML_RE.findall(match.group(2))}
    return match.group(1), params


def extract_tool_calls(
    text: str, known: set[str] | None = None, allow_bare_json: bool = False
) -> tuple[str, list[tuple[str, Any, str | None]]]:
    """Find tool calls written as text. Returns (text without the calls, [(name, arguments, id)])."""
    calls: list[tuple[str, Any, str | None]] = []
    clean = text
    if "<|tool_call_begin|>" in text:
        for raw_id, args in _KIMI_CALL_RE.findall(text):
            calls.append((kimi_tool_name(raw_id), args, raw_id if ":" in raw_id else None))
        clean = _KIMI_STRIP_RE.sub("", text)
    elif "<tool_call>" in text:
        for body in _TOOL_CALL_RE.findall(text):
            xml = _xml_call(body)
            if xml:
                calls.append((xml[0], xml[1], None))
                continue
            obj, _ = parse_arguments(body)
            if obj and isinstance(obj.get("name"), str):
                calls.append((obj["name"], obj.get("arguments", obj.get("parameters", {})), None))
        clean = _TOOL_CALL_RE.sub("", text)
    elif "<function=" in text:
        for match in _FUNCTION_XML_RE.finditer(text):
            xml = _xml_call(match.group(0))
            if xml:
                calls.append((xml[0], xml[1], None))
        clean = _FUNCTION_XML_RE.sub("", text)
    elif allow_bare_json and known:
        for match in _FENCED_JSON_RE.finditer(text):
            obj, _ = parse_arguments(match.group(1))
            if obj and obj.get("name") in known:
                calls.append((obj["name"], obj.get("arguments", obj.get("parameters", {})), None))
                clean = clean.replace(match.group(0), "")
        if not calls:
            obj, _ = parse_arguments(text) if text.strip().startswith("{") else (None, None)
            if obj and obj.get("name") in known:
                calls.append((obj["name"], obj.get("arguments", obj.get("parameters", {})), None))
                clean = ""
    return clean.strip(), calls


def split_think(text: str) -> tuple[str, str]:
    """Separate <think>...</think> reasoning that some models put in the content. Returns (content, thinking)."""
    if "</think>" in text and "<think>" not in text:  # template already opened the think block
        thinking, _, rest = text.partition("</think>")
        return rest.strip(), thinking.strip()
    if "<think" not in text:
        return text, ""
    thinking = "\n".join(m.strip() for m in _THINK_RE.findall(text))
    return _THINK_RE.sub("", text).strip(), thinking


class StreamFilter:
    """Routes streamed content: <think> blocks go to reasoning, tool-call markup is hidden from the user."""

    def __init__(self, on_content: Callback | None, on_reasoning: Callback | None) -> None:
        self.on_content = on_content
        self.on_reasoning = on_reasoning
        self.buffer = ""
        self.state = "start"  # start | think | content | hidden

    def _emit(self, text: str) -> None:
        if text and self.on_content:
            self.on_content(text)

    def feed(self, text: str) -> None:
        self.buffer += text
        while self.buffer:
            if self.state == "hidden":
                self.buffer = ""
                return
            if self.state == "start":
                stripped = self.buffer.lstrip()
                if not stripped:
                    return
                for tag in ("<think>", "<thinking>"):
                    if stripped.startswith(tag):
                        self.buffer = stripped[len(tag):]
                        self.state = "think"
                        break
                    if tag.startswith(stripped):
                        return  # might still become a think tag
                else:
                    self.buffer = stripped
                    self.state = "content"
                continue
            if self.state == "think":
                for tag in ("</think>", "</thinking>"):
                    index = self.buffer.find(tag)
                    if index >= 0:
                        if self.on_reasoning and index:
                            self.on_reasoning(self.buffer[:index])
                        self.buffer = self.buffer[index + len(tag):]
                        self.state = "start"  # skip whitespace after the block (it may arrive in later chunks)
                        break
                else:
                    keep = 12
                    if len(self.buffer) > keep:
                        if self.on_reasoning:
                            self.on_reasoning(self.buffer[:-keep])
                        self.buffer = self.buffer[-keep:]
                    return
                continue
            # content
            positions = [i for i in (self.buffer.find(m) for m in TOOL_MARKERS) if i >= 0]
            if positions:
                self._emit(self.buffer[: min(positions)])
                self.buffer = ""
                self.state = "hidden"
                return
            hold = 0
            for marker in TOOL_MARKERS:
                for size in range(min(len(marker) - 1, len(self.buffer)), 0, -1):
                    if self.buffer.endswith(marker[:size]):
                        hold = max(hold, size)
                        break
            self._emit(self.buffer[: len(self.buffer) - hold])
            self.buffer = self.buffer[len(self.buffer) - hold:]
            return

    def flush(self) -> None:
        if self.state == "think" and self.on_reasoning and self.buffer:
            self.on_reasoning(self.buffer)
        elif self.state == "content" or (self.state == "start" and self.buffer.strip()):
            self._emit(self.buffer.lstrip() if self.state == "start" else self.buffer)
        self.buffer = ""


# --------------------------------------------------------------------------- schemas & message conversion

def normalize_schema(schema: Any) -> dict[str, Any]:
    """Make a JSON schema palatable to picky providers: inline $refs, drop meta keys, ensure types."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    defs: dict[str, Any] = {**(schema.get("definitions") or {}), **(schema.get("$defs") or {})}

    def resolve(node: Any, seen: tuple[str, ...] = ()) -> Any:
        if isinstance(node, list):
            return [resolve(n, seen) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node and isinstance(node["$ref"], str):
            name = node["$ref"].rsplit("/", 1)[-1]
            if name in defs and name not in seen and len(seen) < 16:
                merged = dict(resolve(defs[name], seen + (name,)))
                merged.update({k: resolve(v, seen) for k, v in node.items() if k != "$ref"})
                return merged
            return {"type": "object"}
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("$schema", "$id", "$defs", "definitions", "$comment"):
                continue
            if key == "properties" and isinstance(value, dict):
                out[key] = {name: ensure_type(resolve(sub, seen)) for name, sub in value.items()}
            elif key in ("enum", "const", "default", "examples", "required"):
                out[key] = value
            else:
                out[key] = resolve(value, seen)
        return out

    def ensure_type(node: Any) -> Any:
        if not isinstance(node, dict) or "type" in node:
            return node
        if any(k in node for k in ("anyOf", "oneOf", "allOf", "const", "not")):
            return node
        if node.get("enum"):
            kinds = {type(v) for v in node["enum"]}
            mapping = {str: "string", int: "integer", float: "number", bool: "boolean"}
            if len(kinds) == 1 and next(iter(kinds)) in mapping:
                node["type"] = mapping[next(iter(kinds))]
            return node
        if "properties" in node:
            node["type"] = "object"
        elif "items" in node:
            node["type"] = "array"
        else:
            node["type"] = "string"
        return node

    result = resolve(schema)
    if result.get("type") != "object":
        result = {"type": "object", "properties": result.get("properties", {}), **(
            {"required": result["required"]} if "required" in result else {})}
    result.setdefault("properties", {})
    return result


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
                elif part.get("type") == "image_url":
                    parts.append("[image]")
        return "\n".join(parts)
    return "" if content is None else str(content)


def has_images(messages: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"]
                                                   if isinstance(p, dict))
        for m in messages
    )


def strip_images(messages: list[dict[str, Any]], note: str = "[image omitted]") -> list[dict[str, Any]]:
    out = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in content):
            text = "\n".join(
                str(p.get("text", "")) if p.get("type") == "text" else note
                for p in content if isinstance(p, dict)
            )
            msg = {**msg, "content": text}
        out.append(msg)
    return out


def _as_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return list(content)
    return [{"type": "text", "text": content_text(content)}]


def _merge_user(previous: dict[str, Any], content: Any) -> None:
    old = previous["content"]
    if isinstance(old, str) and isinstance(content, str):
        previous["content"] = f"{old}\n\n{content}" if old else content
    else:
        previous["content"] = _as_parts(old) + _as_parts(content)


TEXT_TOOL_PROTOCOL = """# Tool use

You can call the tools listed below. To call a tool, write a block in exactly this format:

<tool_call>
{{"name": "tool_name", "arguments": {{"param": "value"}}}}
</tool_call>

Rules:
- The block must contain one valid JSON object with "name" and "arguments". Escape quotes and newlines inside JSON strings.
- You can make several tool calls in one message, one block each. After your tool calls, stop and wait: the results are sent back to you in <tool_result> blocks.
- Never write <tool_result> blocks yourself and never invent tool results.
- When no tool is needed, answer normally without any tool call block.

Available tools:
<tools>
{tools}
</tools>"""


def text_tool_prompt(schemas: list[dict[str, Any]]) -> str:
    lines = []
    for schema in schemas:
        if schema.get("type") != "function":
            continue
        fn = schema["function"]
        lines.append(json.dumps({"name": fn["name"], "description": fn.get("description", ""),
                                 "parameters": fn.get("parameters", {})}, ensure_ascii=False))
    return TEXT_TOOL_PROTOCOL.format(tools="\n".join(lines))


def to_text_mode(messages: list[dict[str, Any]], schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert a native tool-calling history to plain messages using the <tool_call> text protocol."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            content = content_text(msg.get("content"))
            if not out and schemas:
                content = f"{content}\n\n{text_tool_prompt(schemas)}"
            out.append({"role": "system", "content": content})
        elif role == "assistant":
            content = content_text(msg.get("content"))
            blocks = []
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                args, _ = parse_arguments(fn.get("arguments"))
                blocks.append("<tool_call>\n" + json.dumps(
                    {"name": fn.get("name", ""), "arguments": args if args is not None else fn.get("arguments")},
                    ensure_ascii=False) + "\n</tool_call>")
            if blocks:
                content = (content.rstrip() + "\n\n" if content.strip() else "") + "\n".join(blocks)
            out.append({"role": "assistant", "content": content})
        elif role in ("tool", "user"):
            if role == "tool":
                content: Any = (f'<tool_result name="{msg.get("name", "")}" id="{msg.get("tool_call_id", "")}">\n'
                                f'{content_text(msg.get("content"))}\n</tool_result>')
            else:
                content = msg.get("content")
            if out and out[-1]["role"] == "user":
                _merge_user(out[-1], content)
            else:
                out.append({"role": "user", "content": content})
    if schemas and (not out or out[0]["role"] != "system"):
        out.insert(0, {"role": "system", "content": text_tool_prompt(schemas)})
    return out


def prepare_native(messages: list[dict[str, Any]], kimi: bool, keep_reasoning: bool) -> list[dict[str, Any]]:
    out = []
    for msg in messages:
        m = dict(msg)
        role = m.get("role")
        if role == "assistant":
            if not keep_reasoning:
                m.pop("reasoning_content", None)
                m.pop("reasoning", None)
            if m.get("tool_calls") and not m.get("content"):
                if kimi:
                    m.pop("content", None)
                else:
                    m["content"] = ""
        elif role == "tool" and not kimi:
            m.pop("name", None)
        out.append(m)
    return out


# --------------------------------------------------------------------------- client

_TOOLS_UNSUPPORTED_RE = re.compile(
    r"(?:does not support|doesn't support|not support(?:ed)?|unsupported|not enabled|not available|requires?)"
    r"[^.\n]{0,60}\b(?:tools?|tool[_ ]calls?|tool[_ ]choice|function[_ ]call(?:ing)?|functions)\b"
    r"|\b(?:tools?|tool[_ ]choice|function[_ ]call(?:ing)?)\b[^.\n]{0,60}"
    r"(?:not supported|unsupported|not enabled|is not allowed|requires?)"
    r"|enable-auto-tool-choice|tool-call-parser|--jinja",
    re.I,
)
_IMAGES_UNSUPPORTED_RE = re.compile(r"image|vision|multimodal|multi-modal|image_url", re.I)
_OVERFLOW_RE = re.compile(
    r"context[_ ]length|context window|maximum context|too many tokens|prompt is too long|input is too long"
    r"|exceed(?:s|ed)? (?:the )?(?:model'?s? )?(?:maximum|max|context|token)|token limit|reduce the length"
    r"|request too large|too long for|maximum prompt length",
    re.I,
)
_QUOTA_RE = re.compile(r"insufficient[_ ](?:quota|balance)|exceeded your current quota|billing|suspended", re.I)
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}


class LLMClient:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._client: openai.OpenAI | None = None
        self.notices: list[str] = []
        self._counter = 0
        self.reset_capabilities()

    def reset_capabilities(self) -> None:
        self.text_mode = self.settings.tool_mode == "text"
        self.stream_options_ok = True
        self.keep_reasoning = True
        self.images_ok = True
        self.send_reasoning_effort = True
        self.send_sampling = True

    def reconfigure(self) -> None:
        """Call after changing provider/model/base_url/api_key."""
        self._client = None
        self.reset_capabilities()

    @property
    def client(self) -> openai.OpenAI:
        if self._client is None:
            s = self.settings
            self._client = openai.OpenAI(
                base_url=s.base_url,
                api_key=s.api_key or "not-needed",
                timeout=httpx.Timeout(s.request_timeout, connect=30.0),
                max_retries=0,
                default_headers=dict(s.extra_headers) or None,
            )
        return self._client

    @property
    def is_kimi(self) -> bool:
        url = self.settings.base_url.lower()
        return bool(self.settings.preset.kimi) or "moonshot" in url or "kimi.com" in url

    def _notice(self, text: str) -> None:
        log.info(text)
        self.notices.append(text)

    def drain_notices(self) -> list[str]:
        notices, self.notices = self.notices, []
        return notices

    def new_call_id(self, name: str) -> str:
        call_id = f"functions.{name}:{self._counter}"
        self._counter += 1
        return call_id

    # -- request building
    def prepare_messages(self, messages: list[dict[str, Any]], schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Keys starting with "_" are agent bookkeeping (turn markers etc.) and never sent.
        messages = [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]
        if not self.images_ok or not self.settings.vision:
            messages = strip_images(messages, "[image omitted: the model cannot view images]")
        if self.text_mode:
            return to_text_mode(messages, [s for s in schemas if s.get("type") == "function"])
        return prepare_native(messages, self.is_kimi, self.keep_reasoning)

    def _tool_params(self, schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tools = []
        for schema in schemas:
            if schema.get("type") == "function":
                fn = dict(schema["function"])
                fn["parameters"] = normalize_schema(fn.get("parameters"))
                tools.append({"type": "function", "function": fn})
            else:
                tools.append(schema)
        return tools

    def build_params(self, messages: list[dict[str, Any]], schemas: list[dict[str, Any]]) -> dict[str, Any]:
        s = self.settings
        params: dict[str, Any] = {"model": s.model, "messages": self.prepare_messages(messages, schemas)}
        if schemas and not self.text_mode:
            params["tools"] = self._tool_params(schemas)
        if self.text_mode and schemas:
            params["stop"] = ["<tool_result"]
        if s.temperature is not None and self.send_sampling:
            params["temperature"] = s.temperature
        if s.top_p is not None and self.send_sampling:
            params["top_p"] = s.top_p
        # Newer fields go through extra_body: older openai SDK versions reject them as keyword arguments.
        extra: dict[str, Any] = {}
        if s.max_tokens:
            if self.is_kimi:
                extra["max_completion_tokens"] = s.max_tokens
            else:
                params["max_tokens"] = s.max_tokens
        if s.reasoning_effort and self.send_reasoning_effort:
            extra["reasoning_effort"] = s.reasoning_effort
        extra.update(s.extra_body or {})
        if extra:
            params["extra_body"] = extra
        return params

    # -- public API
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_content: Callback | None = None,
        on_reasoning: Callback | None = None,
        on_retry: Callback | None = None,
    ) -> LLMResponse:
        """Get the model's next reply. ``on_retry`` receives a short status line before each retry."""
        schemas = list(tools or [])
        attempt = 0
        while True:
            emitted = [False]
            try:
                return self._complete_once(messages, schemas, on_content, on_reasoning, emitted)
            except openai.APIStatusError as exc:
                action = self._classify_status_error(exc, messages, schemas)
                if action == "retry":
                    continue
                if action == "backoff" and attempt < self.settings.max_retries and not emitted[0]:
                    attempt += 1
                    reason = "Rate limited" if exc.status_code == 429 else f"Server error {exc.status_code}"
                    self._sleep(attempt, exc, on_retry, reason)
                    continue
                raise self._friendly(exc) from exc
            except (openai.APIConnectionError, httpx.TransportError) as exc:
                if attempt < self.settings.max_retries and not emitted[0]:
                    attempt += 1
                    self._sleep(attempt, None, on_retry, "Connection problem")
                    continue
                kind = "timed out" if isinstance(exc, (openai.APITimeoutError, httpx.TimeoutException)) else "failed"
                raise LLMError(f"Connection to {self.settings.base_url} {kind}: {exc}") from exc
            except openai.APIError as exc:  # e.g. an error event inside the stream
                text = str(getattr(exc, "message", "") or exc)
                if _OVERFLOW_RE.search(text):
                    raise ContextOverflowError(text) from exc
                if attempt < self.settings.max_retries and not emitted[0]:
                    attempt += 1
                    self._sleep(attempt, None, on_retry, "API error")
                    continue
                raise LLMError(f"API error from {self.settings.base_url}: {text}") from exc

    def list_models(self) -> list[str]:
        try:
            return sorted(m.id for m in self.client.models.list())
        except openai.APIStatusError as exc:
            raise self._friendly(exc) from exc
        except openai.APIError as exc:
            raise LLMError(f"Could not list models: {exc}") from exc

    # -- internals
    def _sleep(self, attempt: int, exc: openai.APIStatusError | None, on_retry: Callback | None = None,
               reason: str = "Request failed") -> None:
        delay = min(60.0, 2.0 ** attempt + random.random())
        if exc is not None and exc.response is not None:
            retry_after = exc.response.headers.get("retry-after")
            try:
                if retry_after:
                    delay = min(120.0, max(1.0, float(retry_after)))
            except ValueError:
                pass
        log.info("retrying in %.1fs (attempt %d)", delay, attempt)
        if on_retry is not None:
            on_retry(f"{reason}; retrying (attempt {attempt} of {self.settings.max_retries})…")
        self._wait(delay)

    @staticmethod
    def _wait(seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:  # short naps keep the thread interruptible
            time.sleep(min(_SLICE, left))

    @staticmethod
    def _error_text(exc: openai.APIStatusError) -> str:
        body = exc.body
        text = ""
        if isinstance(body, dict):
            error = body.get("error", body)
            if isinstance(error, dict):
                text = str(error.get("message") or error.get("msg") or "")
            elif error:
                text = str(error)
            if not text:
                text = json.dumps(body, ensure_ascii=False)
        elif body:
            text = str(body)
        if not text:
            text = str(exc.message or exc)
        return text[:800]

    def _classify_status_error(self, exc: openai.APIStatusError, messages: list[dict[str, Any]],
                               schemas: list[dict[str, Any]]) -> str:
        status = exc.status_code
        text = self._error_text(exc)
        log.warning("API error %s: %s", status, text)
        if status == 429:
            return "raise" if _QUOTA_RE.search(text) else "backoff"
        if status == 413 or _OVERFLOW_RE.search(text):
            raise ContextOverflowError(f"The conversation is too long for the model's context window ({text})")
        # Feature fallbacks come before retrying: some servers report unsupported features as 500s.
        if status in (400, 422, 404, 500):
            lowered = text.lower()
            if self.stream_options_ok and self.settings.stream and ("stream_options" in lowered or
                                                                     "include_usage" in lowered):
                self.stream_options_ok = False
                return "retry"
            if (not self.text_mode and schemas and self.settings.tool_mode == "auto"
                    and _TOOLS_UNSUPPORTED_RE.search(text)):
                self.text_mode = True
                self._notice("The model/server does not support native tool calling; switched to text-based tool "
                             "calls (set tool_mode = \"text\" to make this permanent).")
                return "retry"
            if self.images_ok and has_images(messages) and _IMAGES_UNSUPPORTED_RE.search(text):
                self.images_ok = False
                self._notice("The model rejected image input; images will be omitted (use --no-vision).")
                return "retry"
            if self.send_reasoning_effort and self.settings.reasoning_effort and "reasoning_effort" in lowered:
                self.send_reasoning_effort = False
                self._notice("The server rejected reasoning_effort; it will not be sent.")
                return "retry"
            sampling = [name for name in ("temperature", "top_p") if getattr(self.settings, name) is not None]
            if self.send_sampling and any(name in lowered for name in sampling):
                # Some models (e.g. kimi-k3) use fixed sampling and reject overrides.
                self.send_sampling = False
                self._notice(f"The model rejected {' and '.join(sampling)}; the model's defaults will be used.")
                return "retry"
            if self.keep_reasoning and ("reasoning_content" in lowered or "reasoning" in lowered):
                self.keep_reasoning = False
                return "retry"
        if status in RETRYABLE_STATUS:
            return "backoff"
        return "raise"

    def _friendly(self, exc: openai.APIStatusError) -> LLMError:
        s = self.settings
        status = exc.status_code
        detail = self._error_text(exc)
        where = f"{s.provider} ({s.base_url}, model {s.model})"
        if status == 401:
            return LLMError(f"Authentication failed (401) for {where}. Check your API key. Details: {detail}")
        if status == 402:
            return LLMError(f"Payment required (402) at {where}: {detail}")
        if status == 403:
            return LLMError(f"Access denied (403) at {where}: {detail}")
        if status == 404:
            return LLMError(f"Not found (404) at {where}. The model name or base URL is probably wrong "
                            f"(list models with /models). Details: {detail}")
        if status == 429:
            return LLMError(f"Rate limited or out of quota (429) at {where}: {detail}")
        return LLMError(f"API error {status} from {where}: {detail}")

    def _complete_once(self, messages: list[dict[str, Any]], schemas: list[dict[str, Any]],
                       on_content: Callback | None, on_reasoning: Callback | None, emitted: list[bool]) -> LLMResponse:
        params = self.build_params(messages, schemas)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("request: %s", json.dumps({k: v for k, v in params.items()}, ensure_ascii=False,
                                                 default=str)[:50_000])

        def content_cb(text: str) -> None:
            emitted[0] = True
            if on_content:
                on_content(text)

        def reasoning_cb(text: str) -> None:
            emitted[0] = True
            if on_reasoning:
                on_reasoning(text)

        stream_filter = StreamFilter(content_cb, reasoning_cb)
        if not self.settings.stream:
            response = _interruptible(lambda: self.client.chat.completions.create(**params))
            if not response.choices:
                raise LLMError(f"The API returned no choices: {response}")
            choice = response.choices[0]
            message = choice.message
            reasoning, key = self._reasoning_of(message)
            if reasoning:
                reasoning_cb(reasoning)
            content = message.content or ""
            stream_filter.feed(content)
            stream_filter.flush()
            raw_calls = [{"id": tc.id or "", "name": tc.function.name or "", "arguments": tc.function.arguments or ""}
                         for tc in (message.tool_calls or []) if getattr(tc, "function", None)]
            usage = Usage.from_any(response.usage)
            return self._finalize(content, reasoning, key, raw_calls, choice.finish_reason, usage, schemas)

        if self.stream_options_ok:
            params["stream_options"] = {"include_usage": True}
        stream = _interruptible(lambda: self.client.chat.completions.create(stream=True, **params),
                                cleanup=_close_quietly)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_key: str | None = None
        calls: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        usage: Usage | None = None
        try:
            for chunk in _pumped(stream):
                chunk_usage = Usage.from_any(getattr(chunk, "usage", None))
                if chunk_usage:
                    usage = chunk_usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                choice_usage = Usage.from_any(getattr(choice, "usage", None))  # Kimi puts usage in the choice
                if choice_usage:
                    usage = choice_usage
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                delta = choice.delta
                if delta is None:
                    continue
                reasoning, key = self._reasoning_of(delta)
                if reasoning:
                    reasoning_parts.append(reasoning)
                    reasoning_key = reasoning_key or key
                    reasoning_cb(reasoning)
                if delta.content:
                    content_parts.append(delta.content)
                    stream_filter.feed(delta.content)
                for tc in delta.tool_calls or []:
                    self._accumulate_call(calls, tc)
        finally:
            _close_quietly(stream)
        stream_filter.flush()
        raw_calls = [calls[i] for i in sorted(calls)]
        return self._finalize("".join(content_parts), "".join(reasoning_parts), reasoning_key, raw_calls,
                              finish_reason, usage, schemas)

    @staticmethod
    def _reasoning_of(obj: Any) -> tuple[str, str | None]:
        for key in ("reasoning_content", "reasoning"):
            value = getattr(obj, key, None)
            if value is None:
                extra = getattr(obj, "model_extra", None) or {}
                value = extra.get(key)
            if isinstance(value, str) and value:
                return value, key
        return "", None

    @staticmethod
    def _accumulate_call(calls: dict[int, dict[str, str]], tc: Any) -> None:
        index = getattr(tc, "index", None)
        if index is None:
            index = max(calls) if calls and not getattr(tc, "id", None) else len(calls)
        entry = calls.get(index)
        if entry is not None and tc.id and entry["id"] and tc.id != entry["id"] and entry["name"]:
            index = max(calls) + 1  # server reused an index for a new call
            entry = None
        if entry is None:
            entry = calls[index] = {"id": "", "name": "", "arguments": ""}
        if tc.id:
            entry["id"] = tc.id
        fn = getattr(tc, "function", None)
        if fn is not None:
            if fn.name:
                if not entry["name"] or fn.name.startswith(entry["name"]):
                    entry["name"] = fn.name
                else:
                    entry["name"] += fn.name
            if fn.arguments:
                entry["arguments"] += fn.arguments

    def _finalize(self, content: str, reasoning: str, reasoning_key: str | None, raw_calls: list[dict[str, str]],
                  finish_reason: str | None, usage: Usage | None, schemas: list[dict[str, Any]]) -> LLMResponse:
        content, thinking = split_think(content)
        if thinking and not reasoning:
            reasoning = thinking
        known = {s["function"]["name"] for s in schemas if s.get("type") == "function"}
        known |= {s.get("function", {}).get("name", "") for s in schemas if s.get("type") != "function"}
        tool_calls: list[ToolCall] = []
        for raw in raw_calls:
            if not raw.get("name"):
                continue
            tool_calls.append(ToolCall(raw.get("id") or self.new_call_id(raw["name"]), raw["name"],
                                       raw.get("arguments") or "{}"))
        if tool_calls:
            if any(m in content for m in TOOL_MARKERS):
                content, _ = extract_tool_calls(content, known)
        elif schemas and (self.text_mode or any(m in content for m in TOOL_MARKERS)):
            content, parsed = extract_tool_calls(content, known, allow_bare_json=self.text_mode)
            for name, args, call_id in parsed:
                arguments = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
                tool_calls.append(ToolCall(call_id or self.new_call_id(name), name, arguments))
        for call in tool_calls:
            args, error = parse_arguments(call.arguments)
            if error is not None or args is None:
                call.parse_error = error or "invalid arguments"
                call.raw_arguments = call.arguments
                call.arguments = "{}"  # keep the history valid for servers that re-parse arguments
            else:
                try:
                    strict_ok = isinstance(json.loads(call.arguments), dict)
                except ValueError:
                    strict_ok = False
                if not strict_ok:
                    call.arguments = json.dumps(args, ensure_ascii=False)
        if finish_reason == "tool_calls" and not tool_calls:
            finish_reason = "stop"
        log.debug("response: finish=%s content=%r tool_calls=%s usage=%s", finish_reason, content[:2000],
                  [(c.name, c.arguments[:500]) for c in tool_calls], usage)
        return LLMResponse(content=content.strip(), reasoning=reasoning, reasoning_key=reasoning_key,
                           tool_calls=tool_calls, finish_reason=finish_reason, usage=usage)

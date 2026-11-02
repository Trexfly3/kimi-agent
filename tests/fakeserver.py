"""A scriptable fake OpenAI-compatible chat completions server for tests.

Replies are dicts: {"content", "reasoning", "tool_calls": [{"name", "arguments", "id"?}], "finish_reason",
"usage", "status" + "error" (to fail the request)}. They are served in order, or produced by a handler
function ``handler(request_body, server) -> reply``.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Reply = dict[str, Any]


def _pieces(text: str, size: int) -> Iterator[str]:
    for i in range(0, len(text), size):
        yield text[i:i + size]


def _arguments(call: dict[str, Any]) -> str:
    args = call.get("arguments", {})
    return args if isinstance(args, str) else json.dumps(args)


def _finish(reply: Reply) -> str:
    return reply.get("finish_reason") or ("tool_calls" if reply.get("tool_calls") else "stop")


def _usage(reply: Reply) -> dict[str, Any]:
    return reply.get("usage") or {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}


def completion(body: dict[str, Any], reply: Reply) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": reply.get("content", "")}
    if reply.get("reasoning"):
        message["reasoning_content"] = reply["reasoning"]
    if reply.get("tool_calls"):
        message["tool_calls"] = [{"id": c.get("id", f"call_{i}"), "type": "function",
                                  "function": {"name": c["name"], "arguments": _arguments(c)}}
                                 for i, c in enumerate(reply["tool_calls"])]
    return {"id": "chatcmpl-fake", "object": "chat.completion", "created": 0, "model": body.get("model", ""),
            "choices": [{"index": 0, "message": message, "finish_reason": _finish(reply)}], "usage": _usage(reply)}


def stream_chunks(body: dict[str, Any], reply: Reply, size: int) -> Iterator[dict[str, Any]]:
    def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 0, "model": body.get("model", ""),
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    yield chunk({"role": "assistant", "content": ""})
    for piece in _pieces(reply.get("reasoning", ""), size):
        yield chunk({"reasoning_content": piece})
    for piece in _pieces(reply.get("content", ""), size):
        yield chunk({"content": piece})
    for i, call in enumerate(reply.get("tool_calls") or []):
        yield chunk({"tool_calls": [{"index": i, "id": call.get("id", f"call_{i}"), "type": "function",
                                     "function": {"name": call["name"], "arguments": ""}}]})
        for piece in _pieces(_arguments(call), size):
            yield chunk({"tool_calls": [{"index": i, "function": {"arguments": piece}}]})
    yield chunk({}, _finish(reply))
    if (body.get("stream_options") or {}).get("include_usage"):
        yield {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 0, "model": body.get("model", ""),
               "choices": [], "usage": _usage(reply)}


class FakeOpenAI:
    def __init__(self, replies: list[Reply] | None = None,
                 handler: Callable[[dict[str, Any], FakeOpenAI], Reply] | None = None,
                 models: tuple[str, ...] = ("fake-model", "fake-model-large"), chunk_size: int = 5) -> None:
        self.replies = list(replies or [])
        self.handler = handler
        self.models = list(models)
        self.chunk_size = chunk_size
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def __enter__(self) -> FakeOpenAI:
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def next_reply(self, body: dict[str, Any]) -> Reply:
        if self.handler is not None:
            return self.handler(body, self)
        if not self.replies:
            return {"content": "(no more scripted replies)"}
        return self.replies.pop(0)

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _json(self, status: int, payload: Any) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path.rstrip("/").endswith("/models"):
                    self._json(200, {"object": "list", "data": [
                        {"id": m, "object": "model", "created": 0, "owned_by": "fake"} for m in fake.models]})
                else:
                    self._json(404, {"error": {"message": f"no route {self.path}"}})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    self._json(404, {"error": {"message": f"no route {self.path}"}})
                    return
                fake.requests.append(body)
                fake.headers.append({k.lower(): v for k, v in self.headers.items()})
                reply = fake.next_reply(body)
                if reply.get("status"):
                    self._json(int(reply["status"]), {"error": {"message": reply.get("error", "error"),
                                                                "type": "invalid_request_error"}})
                    return
                if not body.get("stream"):
                    self._json(200, completion(body, reply))
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for chunk in stream_chunks(body, reply, fake.chunk_size):
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

        return Handler


def tool_call(name: str, **arguments: Any) -> Reply:
    return {"tool_calls": [{"name": name, "arguments": arguments}]}

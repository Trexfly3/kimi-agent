from __future__ import annotations

import json
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fakeserver import FakeOpenAI, tool_call

from kimi_agent.mcp import MCPManager, convert_result, expand_vars, iter_sse, tool_name
from kimi_agent.runtime import Runtime
from kimi_agent.tools.base import ToolError

SERVER_SCRIPT = str(Path(__file__).with_name("fake_mcp_server.py"))
FAKE_TOOLS = ["echo", "add", "fail", "image", "slow", "roots", "env", "crash", "grow"]


def stdio(**extra):
    return {"command": sys.executable, "args": [SERVER_SCRIPT], **extra}


@pytest.fixture
def make_manager(tmp_path):
    managers = []

    def make(configs):
        manager = MCPManager(configs, tmp_path, tmp_path / "data")
        managers.append(manager)
        return manager
    yield make
    for manager in managers:
        manager.close()


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --------------------------------------------------------------------------- helpers

def test_expand_vars(monkeypatch):
    monkeypatch.setenv("MCP_TOKEN", "tok")
    monkeypatch.delenv("MCP_MISSING", raising=False)
    value = {"a": "Bearer ${MCP_TOKEN}", "b": ["${MCP_MISSING:-dflt}", "${MCP_MISSING}x", "$MCP_TOKEN"], "c": 3}
    assert expand_vars(value) == {"a": "Bearer tok", "b": ["dflt", "x", "$MCP_TOKEN"], "c": 3}


def test_tool_name():
    assert tool_name("my server", "do.thing") == "mcp__my_server__do_thing"
    long_name = tool_name("s" * 40, "t" * 40)
    assert len(long_name) == 64 and long_name != tool_name("s" * 40, "t" * 41)


def test_iter_sse():
    lines = [": comment", "event: endpoint", "data: /msg", "", 'data: {"a":', "data: 1}", "", "id: 5", "data: tail"]
    assert list(iter_sse(lines)) == [("endpoint", "/msg"), ("message", '{"a":\n1}'), ("message", "tail")]


def test_convert_result():
    result = convert_result({"isError": True, "content": [
        {"type": "text", "text": "hello"},
        {"type": "image", "data": "AAA", "mimeType": "image/jpeg"},
        {"type": "audio", "mimeType": "audio/wav", "data": "x"},
        {"type": "resource", "resource": {"uri": "file:///a.txt", "text": "file text"}},
        {"type": "resource", "resource": {"uri": "file:///b.png", "mimeType": "image/png", "blob": "BBB"}},
        {"type": "resource", "resource": {"uri": "file:///c.bin", "blob": "CCC"}},
        {"type": "resource_link", "uri": "file:///d", "name": "d"},
        "junk"]})
    assert result.is_error and [image.mime for image in result.images] == ["image/jpeg", "image/png"]
    assert result.output == ("hello\n\n[audio content (audio/wav) omitted]\n\n[resource file:///a.txt]\nfile text"
                             "\n\n[binary resource file:///c.bin (unknown type)]\n\n[resource link: d file:///d]")
    assert convert_result({"structuredContent": {"x": 1}}).output == '{\n  "x": 1\n}'
    assert convert_result({}).output == "(no output)"
    assert convert_result({"content": [{"type": "image", "data": "A"}]}).output == "(image output)"


# --------------------------------------------------------------------------- stdio

def test_stdio_server(make_manager, tmp_path, ctx, monkeypatch):
    monkeypatch.setenv("MCP_TEST_TOKEN", "tok-123")
    manager = make_manager({"fake": stdio(env={"FAKE_VAR": "${MCP_TEST_TOKEN}"}, cwd=str(tmp_path))})
    assert manager.start() == []
    server = manager.servers["fake"]
    assert server.server_info["name"] == "fake-mcp" and server.transport.protocol_version == "2025-06-18"
    tools = {tool.name: tool for tool in manager.tools()}
    assert sorted(tools) == sorted(f"mcp__fake__{name}" for name in FAKE_TOOLS)  # both pages listed
    echo = tools["mcp__fake__echo"]
    assert echo.category == "external" and echo.hints == {"readOnlyHint": True} and not echo.trusted
    assert echo.parameters["required"] == ["text"] and "(MCP tool 'echo' from server 'fake')" in echo.description
    assert tools["mcp__fake__add"].description.startswith("Add numbers")
    assert echo.run(ctx, {"text": "hi"}).output == "hi"
    assert tools["mcp__fake__add"].run(ctx, {"a": 2, "b": 3}).output == '{\n  "sum": 5\n}'
    failed = tools["mcp__fake__fail"].run(ctx, {})
    assert failed.is_error and failed.output == "it failed"
    assert tools["mcp__fake__image"].run(ctx, {}).images[0].data == "iVBORw0KGgo="
    root = tmp_path.resolve()
    assert json.loads(tools["mcp__fake__roots"].run(ctx, {}).output) == [{"uri": root.as_uri(), "name": root.name}]
    assert tools["mcp__fake__env"].run(ctx, {"name": "FAKE_VAR"}).output == f"tok-123 in {root}"
    with pytest.raises(ToolError, match="error -32602: Unknown tool: nope"):
        server.call_tool("nope", {})
    assert manager.instructions() == "# MCP server instructions\n\n## MCP server 'fake'\nPrefer the echo tool."
    assert manager.status() == [{"name": "fake", "state": "running", "kind": "stdio", "tools": 9, "error": "",
                                 "trust": False, "server": "fake-mcp"}]
    manager.close()
    assert manager.status()[0]["state"] == "stopped" and manager.tools() == []
    manager.close()  # idempotent


def test_stdio_timeout_crash_and_restart(make_manager):
    manager = make_manager({"fake": stdio(timeout=0.3)})
    assert manager.start() == []
    server = manager.servers["fake"]
    with pytest.raises(ToolError, match="did not answer tools/call within 0.3s"):
        server.call_tool("slow", {"seconds": 1})
    server.config["timeout"] = 10
    assert server.call_tool("echo", {"text": "still alive"}).output == "still alive"
    with pytest.raises(ToolError, match=r"exited \(code 3\).*crashing on purpose"):
        server.call_tool("crash", {})
    assert not server.running
    with pytest.raises(ToolError, match="is not running"):
        server.call_tool("echo", {"text": "x"})
    old_process = server.transport.proc
    assert manager.start() == []  # restarts servers that are not running
    assert server.call_tool("echo", {"text": "back"}).output == "back"
    assert old_process.returncode == 3


def test_config_variants(make_manager):
    manager = make_manager({
        "only": stdio(tools=["echo", "add"], trust=True),
        "off": stdio(enabled=False),
        "broken": {},
        "missing": {"command": "kimi-agent-no-such-binary"},
    })
    errors = manager.start()
    assert len(errors) == 2
    assert any("needs either 'command'" in e for e in errors) and any("Could not start" in e for e in errors)
    assert [tool.name for tool in manager.tools()] == ["mcp__only__echo", "mcp__only__add"]
    assert all(tool.trusted for tool in manager.tools())
    states = {row["name"]: (row["state"], row["kind"]) for row in manager.status()}
    assert states == {"only": ("running", "stdio"), "off": ("disabled", "stdio"), "broken": ("failed", "invalid"),
                      "missing": ("failed", "stdio")}


def test_runtime_uses_mcp_tools_and_refreshes_them(settings, ui):
    settings.mcp_servers = {"fake": stdio(trust=True)}
    replies = [tool_call("mcp__fake__echo", text="ping"), tool_call("mcp__fake__grow"),
               tool_call("mcp__fake__grown"), {"content": "done"}]
    with FakeOpenAI(replies) as fake:
        settings.base_url = fake.base_url
        runtime = Runtime(settings, ui, interactive=False)
        try:
            runtime.start()
            assert "Prefer the echo tool." in runtime.agent.messages[0]["content"]
            assert runtime.agent.run("use the tools") == "done"
        finally:
            runtime.close()
    names = [{tool["function"]["name"] for tool in body["tools"]} for body in fake.requests]
    assert "mcp__fake__echo" in names[0] and "mcp__fake__grown" not in names[1]
    assert "mcp__fake__grown" in names[2]  # refreshed after notifications/tools/list_changed
    results = [m["content"] for m in fake.requests[3]["messages"] if m["role"] == "tool"]
    assert results[:2] == ["ping", "grew"] and "Unknown tool: grown" in results[2]
    assert "tool list was refreshed" in ui.output


# --------------------------------------------------------------------------- Streamable HTTP

class StreamableHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def _send(self, status, body=b"", content_type="application/json", headers=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _record(self, message) -> None:
        self.server.seen.append({"path": self.path, "session": self.headers.get("Mcp-Session-Id"),
                                 "version": self.headers.get("MCP-Protocol-Version"),
                                 "auth": self.headers.get("Authorization"), "message": message})

    def do_DELETE(self) -> None:
        self._record("DELETE")
        self._send(204)

    def do_POST(self) -> None:
        message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._record(message)
        if self.path != "/mcp":
            return self._send(404, b"no such endpoint", "text/plain")
        method, request_id = message.get("method"), message.get("id")
        if request_id is None or method is None:
            return self._send(202)
        if method == "initialize":
            body = {"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "http-fake"}}}
            return self._send(200, json.dumps(body).encode(), headers={"Mcp-Session-Id": "sess-1"})
        if method == "tools/list":
            body = {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [
                {"name": "echo", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}]}}
            return self._send(200, json.dumps(body).encode())
        events = [
            {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}},
            {"jsonrpc": "2.0", "id": 99, "method": "ping"},
            {"jsonrpc": "2.0", "id": request_id, "result": {"content": [
                {"type": "text", "text": "http: " + message["params"]["arguments"]["text"]}]}},
        ]
        payload = "".join(f"event: message\ndata: {json.dumps(event)}\n\n" for event in events).encode()
        return self._send(200, payload, "text/event-stream")


def test_streamable_http(make_manager, ctx, monkeypatch):
    monkeypatch.setenv("MCP_HTTP_TOKEN", "abc")
    server = serve(StreamableHandler)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        manager = make_manager({"web": {"url": f"{base}/mcp", "headers": {"Authorization": "Bearer ${MCP_HTTP_TOKEN}"}},
                                "bad": {"url": f"{base}/nope"}})
        errors = manager.start()
        assert len(errors) == 1 and "'bad'" in errors[0] and "HTTP 404" in errors[0]
        [tool] = manager.tools()
        assert tool.name == "mcp__web__echo" and tool.run(ctx, {"text": "hi"}).output == "http: hi"
        manager.close()
        seen = [s for s in server.seen if s["path"] == "/mcp"]
    finally:
        server.shutdown()
        server.server_close()
    assert seen[0]["message"]["method"] == "initialize" and seen[0]["session"] is None
    assert all(s["auth"] == "Bearer abc" for s in seen)
    later = seen[1:]
    assert all(s["session"] == "sess-1" and s["version"] == "2025-03-26" for s in later)
    messages = [s["message"] for s in later]
    assert {"jsonrpc": "2.0", "id": 99, "result": {}} in messages  # answered the server's ping
    assert messages[-1] == "DELETE"  # session terminated on close


# --------------------------------------------------------------------------- legacy HTTP+SSE

class LegacySseHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b": hello\n\nevent: endpoint\ndata: /messages?session_id=abc\n\n")
        self.wfile.flush()
        while (item := self.server.outbox.get()) is not None:
            self.wfile.write(f"event: message\ndata: {json.dumps(item)}\n\n".encode())
            self.wfile.flush()

    def do_POST(self) -> None:
        message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append((self.path, message))
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.end_headers()
        method, request_id = message.get("method"), message.get("id")
        if request_id is None or method is None:
            return
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "sse"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "echo"}]}
        else:
            result = {"content": [{"type": "text", "text": "sse: " + message["params"]["arguments"]["text"]}]}
        self.server.outbox.put({"jsonrpc": "2.0", "id": request_id, "result": result})


def test_legacy_sse(make_manager, ctx):
    server = serve(LegacySseHandler)
    server.outbox = queue.Queue()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        manager = make_manager({"old": {"url": f"{base}/sse"}})
        assert manager.start() == []
        mcp_server = manager.servers["old"]
        assert mcp_server.kind() == "sse" and mcp_server.transport.endpoint == f"{base}/messages?session_id=abc"
        [tool] = manager.tools()
        assert tool.parameters["type"] == "object" and tool.run(ctx, {"text": "yo"}).output == "sse: yo"
        methods = [message.get("method") for path, message in server.seen if path == "/messages?session_id=abc"]
        assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    finally:
        server.outbox.put(None)
        server.shutdown()
        server.server_close()

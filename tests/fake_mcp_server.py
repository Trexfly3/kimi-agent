"""A tiny stdio MCP server used by the tests (newline-delimited JSON-RPC 2.0)."""

import json
import os
import sys
import time

PNG = "iVBORw0KGgo="
PAGE_1 = [{"name": "echo", "description": "Echo text back",
           "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
           "annotations": {"readOnlyHint": True}}]
PAGE_2 = [
    {"name": "add", "inputSchema": {"type": "object", "properties": {"a": {"type": "number"},
                                                                     "b": {"type": "number"}}},
     "annotations": {"title": "Add numbers"}},
    {"name": "fail", "description": "Always fails"},
    {"name": "image", "description": "Returns an image"},
    {"name": "slow", "description": "Sleeps"},
    {"name": "roots", "description": "Asks the client for its roots"},
    {"name": "env", "description": "Returns an environment variable"},
    {"name": "crash", "description": "Exits the server"},
    {"name": "grow", "description": "Adds a tool and notifies the client"},
]
extra_tools = []


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def read():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def reply(request_id, result):
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def text(value, **extra):
    return {"content": [{"type": "text", "text": value}], **extra}


def call(request_id, name, args):
    if name == "echo":
        reply(request_id, text(args.get("text", "")))
    elif name == "add":
        reply(request_id, {"content": [], "structuredContent": {"sum": args["a"] + args["b"]}})
    elif name == "fail":
        reply(request_id, text("it failed", isError=True))
    elif name == "image":
        reply(request_id, {"content": [{"type": "image", "data": PNG, "mimeType": "image/png"}]})
    elif name == "slow":
        time.sleep(float(args.get("seconds", 5)))
        reply(request_id, text("done"))
    elif name == "roots":
        send({"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list"})
        while True:
            answer = read()
            if answer.get("id") == "srv-1":
                break
        reply(request_id, text(json.dumps(answer["result"]["roots"])))
    elif name == "env":
        reply(request_id, text(os.environ.get(args.get("name", ""), "<unset>") + " in " + os.getcwd()))
    elif name == "crash":
        sys.stderr.write("fatal: crashing on purpose\n")
        sys.stderr.flush()
        sys.exit(3)
    elif name == "grow":
        extra_tools.append({"name": "grown", "description": "Added at runtime"})
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        reply(request_id, text("grew"))
    else:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": f"Unknown tool: {name}"}})


def main():
    print("this line is not JSON and must be ignored", flush=True)
    sys.stderr.write("fake server starting\n")
    sys.stderr.flush()
    while True:
        message = read()
        method, request_id, params = message.get("method"), message.get("id"), message.get("params") or {}
        if request_id is None:
            continue
        if method == "initialize":
            reply(request_id, {"protocolVersion": params.get("protocolVersion"),
                               "capabilities": {"tools": {"listChanged": True}},
                               "serverInfo": {"name": "fake-mcp", "version": "1.0"},
                               "instructions": "Prefer the echo tool."})
        elif method == "tools/list":
            if params.get("cursor") == "page2":
                reply(request_id, {"tools": PAGE_2 + extra_tools})
            else:
                reply(request_id, {"tools": PAGE_1, "nextCursor": "page2"})
        elif method == "tools/call":
            call(request_id, params.get("name"), params.get("arguments") or {})
        elif method == "ping":
            reply(request_id, {})
        else:
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}})


if __name__ == "__main__":
    main()

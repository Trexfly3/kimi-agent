"""Model Context Protocol client: stdio, Streamable HTTP and legacy SSE transports; MCP tools become agent tools."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from . import __version__
from .llm import normalize_schema
from .tools.base import Image, Tool, ToolError, ToolResult

log = logging.getLogger("kimi_agent.mcp")

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
IS_WINDOWS = os.name == "nt"
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class MCPError(Exception):
    pass


def expand_vars(value: Any) -> Any:
    """Expand ${VAR} and ${VAR:-default} in strings, lists and dicts."""
    if isinstance(value, str):
        return _VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [expand_vars(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_vars(v) for k, v in value.items()}
    return value


class _Waiter:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.message: dict[str, Any] | None = None
        self.error: Exception | None = None


class _Transport:
    """Request/response bookkeeping shared by the transports."""

    def __init__(self, name: str, roots: list[dict[str, str]]) -> None:
        self.name = name
        self.roots = roots
        self.protocol_version: str | None = None
        self.tools_changed = False
        self._lock = threading.Lock()
        self._next_id = 1
        self._waiters: dict[int, _Waiter] = {}
        self.closed = False

    def _new_id(self) -> tuple[int, _Waiter]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            waiter = self._waiters[request_id] = _Waiter()
        return request_id, waiter

    def _wait(self, request_id: int, waiter: _Waiter, method: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        try:
            # Short slices keep the wait interruptible by an async KeyboardInterrupt (the GUI's Stop button).
            while not waiter.event.wait(min(0.2, max(deadline - time.monotonic(), 0))):
                if time.monotonic() >= deadline:
                    self._cancel(request_id, "timeout")
                    raise MCPError(f"MCP server '{self.name}' did not answer {method} within {timeout:g}s")
        except KeyboardInterrupt:
            self._cancel(request_id, "interrupted by the user")
            raise
        finally:
            with self._lock:
                self._waiters.pop(request_id, None)
        if waiter.error is not None:
            raise waiter.error
        return unwrap(waiter.message or {}, self.name)

    def _cancel(self, request_id: int, reason: str) -> None:
        self._send_quietly({"jsonrpc": "2.0", "method": "notifications/cancelled",
                            "params": {"requestId": request_id, "reason": reason}})

    def _dispatch(self, message: Any) -> None:
        """Handle one incoming JSON-RPC message (or batch)."""
        if isinstance(message, list):
            for item in message:
                self._dispatch(item)
            return
        if not isinstance(message, dict):
            return
        if "method" in message:
            if "id" in message:
                response = self._server_request(message)
                self._send_quietly(response)
            elif message["method"] == "notifications/tools/list_changed":
                self.tools_changed = True
            return
        request_id = message.get("id")
        with self._lock:
            waiter = self._waiters.get(request_id) if isinstance(request_id, int) else None
        if waiter is not None:
            waiter.message = message
            waiter.event.set()

    def _server_request(self, message: dict[str, Any]) -> dict[str, Any]:
        method = message.get("method")
        if method == "ping":
            return {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        if method == "roots/list":
            return {"jsonrpc": "2.0", "id": message["id"], "result": {"roots": self.roots}}
        return {"jsonrpc": "2.0", "id": message["id"],
                "error": {"code": -32601, "message": f"Method not supported by client: {method}"}}

    def _fail_all(self, error: Exception) -> None:
        with self._lock:
            waiters = list(self._waiters.values())
        for waiter in waiters:
            waiter.error = error
            waiter.event.set()

    def _send_quietly(self, message: dict[str, Any]) -> None:
        try:
            self.send(message)
        except Exception as exc:
            log.debug("[%s] send failed: %s", self.name, exc)

    # implemented by subclasses
    def send(self, message: dict[str, Any]) -> None:
        raise NotImplementedError

    def request(self, method: str, params: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        if self.closed:
            raise MCPError(f"MCP server '{self.name}' is not running")
        request_id, waiter = self._new_id()
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        try:
            self.send(message)
        except MCPError:
            with self._lock:
                self._waiters.pop(request_id, None)
            raise
        return self._wait(request_id, waiter, method, timeout)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        self.closed = True


def unwrap(message: dict[str, Any], name: str) -> dict[str, Any]:
    if "error" in message and message["error"] is not None:
        error = message["error"]
        if isinstance(error, dict):
            detail = error.get("message", "unknown error")
            if error.get("data"):
                detail += f" ({json.dumps(error['data'], ensure_ascii=False)[:500]})"
            raise MCPError(f"MCP server '{name}' error {error.get('code', '')}: {detail}")
        raise MCPError(f"MCP server '{name}' error: {error}")
    result = message.get("result")
    return result if isinstance(result, dict) else {}


class StdioTransport(_Transport):
    def __init__(self, name: str, command: str, args: list[str], env: dict[str, str], cwd: Path,
                 log_path: Path, roots: list[dict[str, str]]) -> None:
        super().__init__(name, roots)
        resolved = shutil.which(command) or command
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path = log_path
        self._log = open(log_path, "ab")  # noqa: SIM115 - lives as long as the process
        full_env = {**os.environ, **{k: str(v) for k, v in env.items()}}
        kwargs: dict[str, Any] = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                                       | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            kwargs["start_new_session"] = True  # Ctrl+C in the terminal must not kill MCP servers
        try:
            self.proc = subprocess.Popen([resolved, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=self._log, env=full_env, cwd=str(cwd), **kwargs)
        except OSError as exc:
            self._log.close()
            raise MCPError(f"Could not start MCP server '{name}' ({command}): {exc}") from exc
        self._write_lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, name=f"mcp-{name}", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                log.debug("[%s] non-JSON output: %s", self.name, line[:200])
                continue
            self._dispatch(message)
        self.closed = True
        try:
            code = self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            code = None
        self._fail_all(MCPError(f"MCP server '{self.name}' exited (code {code}). "
                                f"See {self.log_path}: {self.log_tail()}"))

    def log_tail(self, limit: int = 600) -> str:
        try:
            data = self.log_path.read_bytes()[-limit:]
        except OSError:
            return ""
        return " ".join(data.decode("utf-8", errors="replace").split())

    def send(self, message: dict[str, Any]) -> None:
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise MCPError(f"MCP server '{self.name}' is not accepting input: {exc}") from exc

    def close(self) -> None:
        super().close()
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        try:
            self._log.close()
        except OSError:
            pass


def iter_sse(lines: Iterable[str]) -> Iterator[tuple[str, str]]:
    """Parse Server-Sent Events into (event, data) pairs."""
    event, data = "", []
    for line in lines:
        line = line.rstrip("\r")
        if not line:
            if data:
                yield event or "message", "\n".join(data)
            event, data = "", []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)
    if data:
        yield event or "message", "\n".join(data)


class HttpTransport(_Transport):
    """Streamable HTTP transport (MCP 2025-03-26+): POST requests, JSON or SSE responses."""

    def __init__(self, name: str, url: str, headers: dict[str, str], roots: list[dict[str, str]],
                 timeout: float) -> None:
        super().__init__(name, roots)
        self.url = url
        self.client = httpx.Client(headers=headers, follow_redirects=True, timeout=httpx.Timeout(timeout, connect=20))
        self.session_id: str | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def send(self, message: dict[str, Any]) -> None:
        try:
            response = self.client.post(self.url, json=message, headers=self._headers(), timeout=30)
        except httpx.HTTPError as exc:
            raise MCPError(f"MCP server '{self.name}' unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise MCPError(f"MCP server '{self.name}' returned HTTP {response.status_code}: {response.text[:300]}")

    def request(self, method: str, params: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        if self.closed:
            raise MCPError(f"MCP server '{self.name}' is not running")
        request_id, waiter = self._new_id()
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        try:
            with self.client.stream("POST", self.url, json=message, headers=self._headers(),
                                    timeout=httpx.Timeout(timeout, connect=20)) as response:
                session_id = response.headers.get("mcp-session-id")
                if session_id:
                    self.session_id = session_id
                if response.status_code >= 400:
                    body = response.read().decode("utf-8", errors="replace")[:500]
                    raise MCPError(f"MCP server '{self.name}' returned HTTP {response.status_code} for {method}: "
                                   f"{body}")
                if "text/event-stream" in response.headers.get("content-type", ""):
                    for _event, data in iter_sse(response.iter_lines()):
                        try:
                            self._dispatch(json.loads(data))
                        except ValueError:
                            continue
                        if waiter.event.is_set():
                            break
                else:
                    body = response.read()
                    if body.strip():
                        self._dispatch(json.loads(body))
        except httpx.HTTPError as exc:
            raise MCPError(f"MCP request {method} to '{self.name}' failed: {exc}") from exc
        except ValueError as exc:
            raise MCPError(f"MCP server '{self.name}' sent invalid JSON: {exc}") from exc
        finally:
            with self._lock:
                self._waiters.pop(request_id, None)
        if not waiter.event.is_set():
            raise MCPError(f"MCP server '{self.name}' sent no response to {method}")
        return unwrap(waiter.message or {}, self.name)

    def close(self) -> None:
        super().close()
        if self.session_id:
            try:
                self.client.delete(self.url, headers=self._headers(), timeout=5)
            except httpx.HTTPError:
                pass
        self.client.close()


class SseTransport(_Transport):
    """Legacy HTTP+SSE transport (MCP 2024-11-05): GET an event stream, POST messages to its endpoint."""

    def __init__(self, name: str, url: str, headers: dict[str, str], roots: list[dict[str, str]],
                 timeout: float) -> None:
        super().__init__(name, roots)
        self.url = url
        self.client = httpx.Client(headers=headers, follow_redirects=True, timeout=httpx.Timeout(None, connect=20))
        self.endpoint: str | None = None
        self._ready = threading.Event()
        self._error: MCPError | None = None
        threading.Thread(target=self._listen, name=f"mcp-sse-{name}", daemon=True).start()
        if not self._ready.wait(timeout):
            self.close()
            raise MCPError(f"MCP server '{name}' did not send an SSE endpoint within {timeout:g}s")
        if self.endpoint is None:
            raise self._error or MCPError(f"MCP server '{name}' closed the SSE stream")

    def _listen(self) -> None:
        try:
            with self.client.stream("GET", self.url, headers={"Accept": "text/event-stream"}) as response:
                if response.status_code >= 400:
                    raise MCPError(f"MCP server '{self.name}' returned HTTP {response.status_code} for the SSE stream")
                for event, data in iter_sse(response.iter_lines()):
                    if event == "endpoint":
                        self.endpoint = urljoin(self.url, data.strip())
                        self._ready.set()
                    else:
                        try:
                            self._dispatch(json.loads(data))
                        except ValueError:
                            continue
        except MCPError as exc:
            self._error = exc
        except Exception as exc:
            if not self.closed:
                self._error = MCPError(f"SSE connection to MCP server '{self.name}' failed: {exc}")
        finally:
            self.closed = True
            self._ready.set()
            self._fail_all(self._error or MCPError(f"SSE connection to MCP server '{self.name}' closed"))

    def send(self, message: dict[str, Any]) -> None:
        if not self.endpoint:
            raise MCPError(f"MCP server '{self.name}' has no message endpoint")
        try:
            response = self.client.post(self.endpoint, json=message, timeout=30)
        except httpx.HTTPError as exc:
            raise MCPError(f"MCP server '{self.name}' unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise MCPError(f"MCP server '{self.name}' returned HTTP {response.status_code}: {response.text[:300]}")

    def close(self) -> None:
        super().close()
        self.client.close()


# --------------------------------------------------------------------------- servers and tools

def convert_result(result: dict[str, Any]) -> ToolResult:
    texts: list[str] = []
    images: list[Image] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            texts.append(str(item.get("text", "")))
        elif kind == "image" and item.get("data"):
            images.append(Image(item["data"], item.get("mimeType") or "image/png"))
        elif kind == "audio":
            texts.append(f"[audio content ({item.get('mimeType', 'audio')}) omitted]")
        elif kind == "resource":
            resource = item.get("resource") or {}
            uri = resource.get("uri", "")
            mime = resource.get("mimeType") or ""
            if "text" in resource:
                texts.append(f"[resource {uri}]\n{resource['text']}")
            elif resource.get("blob") and mime.startswith("image/"):
                images.append(Image(resource["blob"], mime))
            else:
                texts.append(f"[binary resource {uri} ({mime or 'unknown type'})]")
        elif kind == "resource_link":
            texts.append(f"[resource link: {item.get('name') or ''} {item.get('uri', '')}]".replace("  ", " "))
    if not texts and result.get("structuredContent") is not None:
        texts.append(json.dumps(result["structuredContent"], indent=2, ensure_ascii=False))
    output = "\n\n".join(t for t in texts if t) or ("(image output)" if images else "(no output)")
    return ToolResult(output, images, is_error=bool(result.get("isError")))


_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


def tool_name(server: str, name: str) -> str:
    full = _NAME_RE.sub("_", f"mcp__{server}__{name}")
    if len(full) > 64:
        full = full[:57] + "_" + hashlib.sha1(full.encode()).hexdigest()[:6]
    return full


class MCPServer:
    def __init__(self, name: str, config: dict[str, Any], cwd: Path, log_dir: Path) -> None:
        self.name = name
        self.config = expand_vars(dict(config))
        self.cwd = cwd
        self.log_dir = log_dir
        self.transport: _Transport | None = None
        self.tools: list[dict[str, Any]] = []
        self.error = ""
        self.server_info: dict[str, Any] = {}
        self.instructions = ""

    @property
    def enabled(self) -> bool:
        return self.config.get("enabled", True) is not False

    @property
    def trust(self) -> bool:
        return bool(self.config.get("trust", False))

    @property
    def startup_timeout(self) -> float:
        return float(self.config.get("startup_timeout", 60))

    @property
    def timeout(self) -> float:
        return float(self.config.get("timeout", 300))

    @property
    def running(self) -> bool:
        return self.transport is not None and not self.transport.closed

    def kind(self) -> str:
        explicit = str(self.config.get("type") or self.config.get("transport") or "").lower().replace("_", "-")
        if explicit in ("stdio", "sse", "http"):
            return explicit
        if explicit == "streamable-http":
            return "http"
        if self.config.get("command"):
            return "stdio"
        url = str(self.config.get("url") or "")
        if url:
            return "sse" if url.rstrip("/").endswith("/sse") else "http"
        raise MCPError(f"MCP server '{self.name}' needs either 'command' (stdio) or 'url' (HTTP)")

    def _make_transport(self) -> _Transport:
        root = self.cwd.resolve()
        roots = [{"uri": root.as_uri(), "name": root.name or str(root)}]
        kind = self.kind()
        if kind == "stdio":
            cwd = Path(self.config.get("cwd") or self.cwd).expanduser()
            args = [str(a) for a in self.config.get("args") or []]
            env = dict(self.config.get("env") or {})
            log_path = self.log_dir / f"mcp-{_NAME_RE.sub('_', self.name)}.log"
            return StdioTransport(self.name, str(self.config["command"]), args, env, cwd, log_path, roots)
        headers = {str(k): str(v) for k, v in (self.config.get("headers") or {}).items()}
        if kind == "sse":
            return SseTransport(self.name, str(self.config["url"]), headers, roots, self.startup_timeout)
        return HttpTransport(self.name, str(self.config["url"]), headers, roots, self.startup_timeout)

    def start(self) -> None:
        self.close()  # a previous transport (e.g. of a crashed server) must not leak its process and log file
        self.error = ""
        self.tools = []
        try:
            self.transport = self._make_transport()
            result = self.transport.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"roots": {"listChanged": False}},
                "clientInfo": {"name": "kimi-agent", "version": __version__},
            }, self.startup_timeout)
            version = str(result.get("protocolVersion") or PROTOCOL_VERSION)
            if version not in SUPPORTED_VERSIONS:
                log.warning("MCP server '%s' uses protocol version %s", self.name, version)
            self.transport.protocol_version = version
            self.server_info = result.get("serverInfo") or {}
            self.instructions = str(result.get("instructions") or "")
            self.transport.notify("notifications/initialized")
            self.tools = self.list_tools() if "tools" in (result.get("capabilities") or {"tools": {}}) else []
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            self.close()
            raise MCPError(self.error) from exc

    def list_tools(self) -> list[dict[str, Any]]:
        assert self.transport is not None
        tools: list[dict[str, Any]] = []
        cursor = None
        for _page in range(100):
            result = self.transport.request("tools/list", {"cursor": cursor} if cursor else {}, self.startup_timeout)
            tools.extend(t for t in result.get("tools") or [] if isinstance(t, dict) and t.get("name"))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        allowed = self.config.get("tools")
        if allowed:
            tools = [t for t in tools if t["name"] in set(allowed)]
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if not self.running or self.transport is None:
            raise ToolError(f"MCP server '{self.name}' is not running" + (f": {self.error}" if self.error else ""))
        try:
            result = self.transport.request("tools/call", {"name": name, "arguments": arguments}, self.timeout)
        except MCPError as exc:
            raise ToolError(str(exc)) from exc
        return convert_result(result)

    def make_tools(self) -> list[Tool]:
        return [self._make_tool(spec) for spec in self.tools]

    def _make_tool(self, spec: dict[str, Any]) -> Tool:
        annotations = spec.get("annotations") if isinstance(spec.get("annotations"), dict) else {}
        remote = str(spec["name"])
        description = str(spec.get("description") or annotations.get("title") or spec.get("title") or remote).strip()
        if len(description) > 4_000:
            description = description[:4_000] + "…"
        parameters = normalize_schema(spec.get("inputSchema") or {"type": "object", "properties": {}})

        def call(_ctx: Any, /, **arguments: Any) -> ToolResult:
            return self.call_tool(remote, arguments)

        return Tool(name=tool_name(self.name, remote),
                    description=f"{description}\n\n(MCP tool '{remote}' from server '{self.name}')",
                    parameters=parameters, func=call, category="external", trusted=self.trust, strict_args=False,
                    hints=dict(annotations))

    def close(self) -> None:
        transport, self.transport = self.transport, None
        if transport is not None:
            try:
                transport.close()
            except Exception as exc:
                log.debug("closing MCP server %s: %s", self.name, exc)


class MCPManager:
    def __init__(self, configs: dict[str, Any], cwd: Path, data_dir: Path) -> None:
        self.servers: dict[str, MCPServer] = {
            name: MCPServer(name, cfg if isinstance(cfg, dict) else {}, cwd, Path(data_dir) / "logs")
            for name, cfg in (configs or {}).items()
        }

    def start(self, names: Iterable[str] | None = None) -> list[str]:
        """Start enabled servers in parallel. Returns error messages."""
        wanted = set(names) if names is not None else None
        targets = [s for s in self.servers.values()
                   if s.enabled and not s.running and (wanted is None or s.name in wanted)]
        errors: list[str] = []
        if not targets:
            return errors
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            futures = [(server, pool.submit(server.start)) for server in targets]
            for server, future in futures:
                try:
                    future.result()
                except Exception as exc:
                    errors.append(f"MCP server '{server.name}' failed to start: {exc}")
        return errors

    def tools(self) -> list[Tool]:
        result: list[Tool] = []
        for server in self.servers.values():
            if server.running:
                result.extend(server.make_tools())
        return result

    def status(self) -> list[dict[str, Any]]:
        rows = []
        for server in self.servers.values():
            if not server.enabled:
                state = "disabled"
            elif server.running:
                state = "running"
            elif server.error:
                state = "failed"
            else:
                state = "stopped"
            try:
                kind = server.kind()
            except MCPError:
                kind = "invalid"
            rows.append({"name": server.name, "state": state, "kind": kind, "tools": len(server.tools),
                         "error": server.error, "trust": server.trust,
                         "server": server.server_info.get("name", "")})
        return rows

    def refresh_changed(self) -> bool:
        """Re-list the tools of servers that sent notifications/tools/list_changed. True if any were refreshed."""
        changed = False
        for server in self.servers.values():
            transport = server.transport
            if not server.running or transport is None or not transport.tools_changed:
                continue
            transport.tools_changed = False
            try:
                server.tools = server.list_tools()
            except MCPError as exc:
                log.warning("could not refresh the tools of MCP server %s: %s", server.name, exc)
                continue
            changed = True
        return changed

    def instructions(self) -> str:
        parts = [f"## MCP server '{s.name}'\n{s.instructions.strip()[:3_000]}"
                 for s in self.servers.values() if s.running and s.instructions.strip()]
        return ("# MCP server instructions\n\n" + "\n\n".join(parts)) if parts else ""

    def close(self) -> None:
        for server in self.servers.values():
            server.close()

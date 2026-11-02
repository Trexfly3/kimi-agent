"""Local HTTP server for the desktop app: static files, a JSON API and a server-sent event stream.

Only 127.0.0.1 is bound. A window gets in by opening a single-use launch link (``/auth?code=…``), which works only
when the browser itself opens it, not a web page. The page it returns sets an HttpOnly cookie and hands the window a
key, which the window keeps in sessionStorage. Browsers also send the cookie to other web apps on 127.0.0.1 (cookies
ignore ports), but no other port can read the key.

Every API call needs both, plus a matching Host header (DNS rebinding). The key travels in a custom header that
cross-site pages cannot send without a CORS preflight (CSRF). On Linux, where other users can read the launch link
from the browser's command line, connections from their programs are refused.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import socket
import struct
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .app import ApiError, GuiApp
from .launcher import launcher_mac
from .render import pygments_css

log = logging.getLogger("kimi_agent.gui.server")

STATIC_DIR = Path(__file__).parent / "static"
STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
                ".html": "text/html; charset=utf-8"}
PREFERRED_PORT = 41753
MAX_JSON_BYTES = 2 * 1024 * 1024
PING_SECONDS = 15.0
NONCE_RE = re.compile(r"[0-9a-f]{32}")
KEY_IN_URL_RE = re.compile(r"([?&]key=)[^&\s\"]*")
CHECK_PEER = sys.platform.startswith("linux")
ON_WSL = CHECK_PEER and "microsoft" in os.uname().release.lower()
GONE = -1  # peer_uid: the client end is closing or closed
NOT_SIGNED_IN_API = "Not signed in. Reopen Kimi Agent from its shortcut."
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "connect-src 'self'; font-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; "
       "object-src 'none'")
NOTICE_PAGE = """<!doctype html><meta charset="utf-8"><title>Kimi Agent</title>
<style>body{font:16px/1.5 system-ui,sans-serif;background:#faf9f5;color:#1f1e1d;display:grid;place-items:center;
height:100vh;margin:0}main{max-width:30rem;padding:2rem}code{background:#f0eee6;padding:.1em .3em;border-radius:4px}
</style><main><h1>Kimi Agent</h1><p>__MESSAGE__</p></main>"""
NOT_SIGNED_IN = NOTICE_PAGE.replace("__MESSAGE__", "This window is not connected. Open Kimi Agent from its desktop "
                                    "shortcut, or run <code>kimi-agent --gui</code> in a terminal.")
OPENED_BY_PAGE = NOTICE_PAGE.replace("__MESSAGE__", "This sign-in link was opened by a web page, so it was ignored. "
                                     "Paste it into the address bar instead, or open Kimi Agent from its desktop "
                                     "shortcut.")
WRONG_HOST = NOTICE_PAGE.replace("__MESSAGE__", "Kimi Agent only answers at <code>http://127.0.0.1:__PORT__</code>, "
                                 "not at <code>localhost</code> or another name. Open it from its desktop shortcut.")


def _proc_net_address(host: str, port: int, family: int) -> str:
    """An address as /proc/net/tcp{,6} shows it: the address as 32-bit words in host byte order, then the port."""
    packed = socket.inet_pton(family, host)
    return "".join(f"{word:08X}" for word in struct.unpack(f"={len(packed) // 4}I", packed)) + f":{port:04X}"


def peer_uid(client: tuple[str, int], server: tuple[str, int]) -> int | None:
    """Linux: the user ID that owns the client end of a TCP connection to this server.

    GONE if that end is closing or closed: the kernel lists such sockets as root's, or not at all, so a client could
    otherwise send a request and hang up before the check. None if /proc doesn't show this machine's connections
    (on WSL, Windows programs connecting in have no Linux socket).
    """
    tables = (("/proc/net/tcp", socket.AF_INET, client[0], server[0]),
              ("/proc/net/tcp6", socket.AF_INET6, f"::ffff:{client[0]}", f"::ffff:{server[0]}"))  # IPv4-mapped
    closing = listed = False
    for path, family, client_host, server_host in tables:
        try:
            ours = _proc_net_address(server_host, server[1], family)
            theirs = _proc_net_address(client_host, client[1], family)
            with open(path, encoding="ascii", errors="replace") as table:
                next(table, None)
                for line in table:
                    # sl, local, remote, state, queues, timer, retransmits, uid, ...; state 01 is ESTABLISHED
                    fields = line.split()
                    if len(fields) < 8:
                        continue
                    if fields[1] == ours:  # our listening socket or our end: /proc does show our connections
                        listed = True
                    elif (fields[1], fields[2]) == (theirs, ours):
                        if fields[3] == "01":
                            return int(fields[7])
                        closing = True
        except (OSError, ValueError):
            continue
    return GONE if closing or (listed and not ON_WSL) else None


def still_open(sock: socket.socket) -> bool:
    """Linux: whether our end of a connection is still ESTABLISHED, i.e. the client hasn't hung up."""
    try:
        return sock.getsockopt(socket.IPPROTO_TCP, getattr(socket, "TCP_INFO", 11), 1)[:1] == b"\x01"
    except OSError:
        return True


class GuiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows SO_REUSEADDR would let another process share the port
    request_queue_size = 64

    def __init__(self, app: GuiApp, port: int | None = None) -> None:
        self.app = app
        self.secret = secrets.token_urlsafe(32)  # lets another launcher ask for a new window (or to quit)
        self.token = secrets.token_urlsafe(32)  # the session cookie
        self.key = secrets.token_urlsafe(32)  # each window keeps it in sessionStorage, which other ports can't read
        self.codes: dict[str, float] = {}
        self.codes_lock = threading.Lock()
        self.stopping = threading.Event()
        self._css: bytes | None = None
        candidates = [port] if port is not None else [PREFERRED_PORT, 0]
        error: OSError | None = None
        for candidate in candidates:
            try:
                super().__init__(("127.0.0.1", candidate), Handler)
                break
            except OSError as exc:
                error = exc
        else:
            raise error or OSError("could not bind a port")
        self.port = int(self.server_address[1])
        self.cookie_name = f"kimi_agent_{self.port}"
        # Not localhost: browsers try ::1 first, where another user could serve that origin.
        self.hosts = {f"127.0.0.1:{self.port}"}
        self.origins = {f"http://{h}" for h in self.hosts}

    def verify_request(self, request: Any, client_address: Any) -> bool:
        """On Linux, refuse other users' programs: they can see launch links in the browser's command line."""
        if not CHECK_PEER:
            return True
        try:
            owner = peer_uid(client_address[:2], request.getsockname()[:2])
        except OSError:
            return False
        # Checked after the lookup, which a client that hung up meanwhile may have passed as root's.
        if owner == GONE or not still_open(request):
            log.info("refused a connection that was closed before it could be checked")
            return False
        if owner is None or owner in (os.getuid(), 0):  # root can read our files anyway
            return True
        log.warning("refused a connection from a program of another user (uid %d)", owner)
        return False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def launch_url(self, ttl: float = 120.0) -> str:
        """A single-use link that signs a window in."""
        code = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self.codes_lock:
            self.codes = {c: t for c, t in self.codes.items() if t > now}
            self.codes[code] = now + ttl
        return f"{self.url}auth?code={code}"

    def redeem(self, code: str) -> bool:
        with self.codes_lock:
            expires = self.codes.pop(code, None)
        return expires is not None and expires > time.monotonic()

    def css(self) -> bytes:
        if self._css is None:
            self._css = pygments_css().encode("utf-8")
        return self._css

    def request_shutdown(self) -> None:
        if not self.stopping.is_set():
            self.stopping.set()
            self.app.bus.close_all()
            threading.Thread(target=self.shutdown, name="kimi-agent-shutdown", daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server: GuiServer
    protocol_version = "HTTP/1.0"
    server_version = "KimiAgent"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s %s", self.address_string(), KEY_IN_URL_RE.sub(r"\1…", format % args))

    # ------------------------------------------------------------------ helpers
    def _headers(self, status: int, content_type: str, length: int | None = None, *,
                 cache: str = "no-store", extra: dict[str, str] | None = None) -> None:
        if any("\r" in value or "\n" in value for value in (content_type, *(extra or {}).values())):
            raise ValueError("line break in a response header")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _send(self, status: int, body: bytes, content_type: str, **kwargs: Any) -> None:
        self._headers(status, content_type, len(body), **kwargs)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _check_host(self) -> bool:
        """False, after sending a notice, unless the request is addressed to 127.0.0.1. Stops DNS rebinding."""
        if self.headers.get("Host", "") in self.server.hosts:
            return True
        self._send(421, WRONG_HOST.replace("__PORT__", str(self.server.port)).encode(), "text/html; charset=utf-8")
        return False

    def _signed_in(self) -> bool:
        # Parsed by hand: SimpleCookie drops the whole header if any cookie in it is unusual, and the browser sends
        # the cookies of every other web app on 127.0.0.1 along with ours.
        expected = self.server.token.encode()
        for header in self.headers.get_all("Cookie") or []:
            for part in header.split(";"):
                name, sep, value = part.partition("=")
                if sep and name.strip() == self.server.cookie_name and hmac.compare_digest(
                        value.strip().encode(), expected):
                    return True
        return False

    def _key_ok(self, key: str | None) -> bool:
        return hmac.compare_digest((key or "").encode(), self.server.key.encode())

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin in self.server.origins

    def _body(self, limit: int = MAX_JSON_BYTES) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ApiError(400, "Bad Content-Length.") from exc
        if length < 0 or length > limit:
            raise ApiError(413, "The request is too large.")
        chunks, remaining = [], length
        while remaining:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                raise ApiError(400, "The request body ended early.")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _json_body(self) -> dict[str, Any]:
        raw = self._body()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ApiError(400, "Invalid JSON.") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "Expected a JSON object.")
        return data

    # ------------------------------------------------------------------ GET
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._check_host():
            return
        parsed = urllib.parse.urlsplit(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/static/"):
                self._static(path[len("/static/"):])
            elif path == "/auth":
                self._auth(query.get("code", [""])[0])
            elif not self._signed_in():
                if path.startswith("/api/"):
                    self._error(401, NOT_SIGNED_IN_API)
                else:
                    self._send(401, NOT_SIGNED_IN.encode(), "text/html; charset=utf-8")
            elif path == "/" or path == "/index.html":
                self._index()
            elif path.startswith("/api/image/"):  # for <img>, which can't send the key; image IDs are unguessable
                self._image(path[len("/api/image/"):])
            elif not self._key_ok(query.get("key", [""])[0] if path == "/api/events"  # EventSource can't either
                                  else self.headers.get("X-Kimi-Agent")):
                self._error(401, NOT_SIGNED_IN_API)
            elif path == "/api/events":
                self._events()
            else:
                self.server.app.touch()
                self._api_get(path, query)
        except ApiError as exc:
            self._error(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            log.exception("GET %s failed", path)
            self._error(500, f"Internal error: {exc}")

    def _static(self, name: str) -> None:
        if name == "pygments.css":
            self._send(200, self.server.css(), STATIC_TYPES[".css"], cache="no-cache")
            return
        target = (STATIC_DIR / name).resolve()
        if (not name or "/" in name or "\\" in name or target.parent != STATIC_DIR.resolve()
                or target.suffix not in STATIC_TYPES or not target.is_file() or target.name == "index.html"):
            self._send(404, b"Not found", "text/plain")
            return
        self._send(200, target.read_bytes(), STATIC_TYPES[target.suffix], cache="no-cache")

    def _auth(self, code: str) -> None:
        # Only the browser itself may open a launch link (Sec-Fetch-Site: none), not a web page: other users can read
        # the link from the browser's command line, and a page of theirs could open it in this user's browser.
        by_page = self.headers.get("Sec-Fetch-Site", "none") != "none"
        if by_page or not self.server.redeem(code):
            if self._signed_in():  # e.g. the window was reloaded on the launch link
                self._send(303, b"", "text/plain", extra={"Location": "/"})
            elif by_page:
                self._send(403, OPENED_BY_PAGE.encode(), "text/html; charset=utf-8")
            else:
                self._send(401, NOT_SIGNED_IN.encode(), "text/html; charset=utf-8")
            return
        cookie = f"{self.server.cookie_name}={self.server.token}; Path=/; HttpOnly; SameSite=Strict"
        self._index(key=self.server.key, extra={"Set-Cookie": cookie})

    def _index(self, key: str = "", extra: dict[str, str] | None = None) -> None:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        theme = self.server.app.prefs.get("theme", "system")
        html = html.replace("__THEME__", theme if theme in ("light", "dark", "system") else "system")
        html = html.replace("__KEY__", key)  # only in the page a launch link returns; the window then keeps it
        self._send(200, html.encode("utf-8"), STATIC_TYPES[".html"], extra=extra)

    def _image(self, image_id: str) -> None:
        image = self.server.app.images.get(image_id)
        if image is None:
            self._send(404, b"Not found", "text/plain")
        else:
            self._send(200, image[1], image[0], cache="private, max-age=86400, immutable")

    def _events(self) -> None:
        app = self.server.app
        sub = app.bus.subscribe()
        try:
            self._headers(200, "text/event-stream; charset=utf-8", extra={"X-Accel-Buffering": "no"})
            self.wfile.write(b"retry: 1500\n\n")
            self.wfile.flush()
            resync, last_write = True, time.monotonic()
            while not self.server.stopping.is_set():
                if resync:
                    events, resync = app.bus.snapshot(sub), False
                else:
                    resync, events = sub.wait(PING_SECONDS)
                    if sub.closed:
                        break
                    if resync:
                        continue
                if events:
                    self.wfile.write("".join(f"data: {e}\n\n" for e in events).encode("utf-8"))
                    self.wfile.flush()
                    last_write = time.monotonic()
                elif time.monotonic() - last_write >= PING_SECONDS:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_write = time.monotonic()
        except OSError:
            pass
        finally:
            app.bus.unsubscribe(sub)
            app.touch()

    def _api_get(self, path: str, query: dict[str, list[str]]) -> None:
        app = self.server.app
        arg = lambda name: query.get(name, [""])[0]
        if path == "/api/state":
            self._json(app.state())
        elif path == "/api/sessions":
            self._json({"sessions": app.list_sessions()})
        elif path == "/api/settings":
            self._json(app.settings_payload())
        elif path == "/api/models":
            self._json(app.models())
        elif path == "/api/browse":
            self._json(app.browse(arg("path") or None))
        elif path == "/api/export":
            filename, text = app.export_session(arg("id") or None)
            disposition = f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}"
            self._send(200, text.encode("utf-8"), "text/markdown; charset=utf-8",
                       extra={"Content-Disposition": disposition})
        else:
            self._error(404, "Not found.")

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:
        if not self._check_host():
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path in ("/api/launch", "/api/quit"):
                self._from_launcher(path)
                return
            if not self._signed_in() or not self._key_ok(self.headers.get("X-Kimi-Agent")):
                self._error(401, NOT_SIGNED_IN_API)
                return
            if not self._origin_ok():
                self._error(403, "Forbidden.")
                return
            self.server.app.touch()
            self._api_post(path)
        except ApiError as exc:
            self._error(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            log.exception("POST %s failed", path)
            self._error(500, f"Internal error: {exc}")

    def _from_launcher(self, path: str) -> None:
        """Requests from another launcher process. Both sides prove that they know the lock file's secret."""
        scheme, _, credentials = self.headers.get("Authorization", "").partition(" ")
        nonce, _, mac = credentials.partition(".")
        expected = launcher_mac(self.server.secret, "launcher", path, nonce)
        if (scheme != "KimiAgent" or not NONCE_RE.fullmatch(nonce) or self.headers.get("Origin")
                or not hmac.compare_digest(mac.encode(), expected.encode())):
            self._error(403, "Forbidden.")
            return
        self._body()
        proof = launcher_mac(self.server.secret, "server", path, nonce)
        if path == "/api/quit":
            self._json({"ok": True, "proof": proof})
            self.server.request_shutdown()
            return
        self._json({"url": self.server.launch_url(), "proof": proof})

    def _api_post(self, path: str) -> None:
        app = self.server.app
        if path == "/api/upload":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            data = self._body(limit=50 * 1024 * 1024)
            self._json(app.upload(query.get("name", ["file"])[0], data))
            return
        body = self._json_body()
        result: Any = None
        if path == "/api/send":
            attachments = body.get("attachments") if isinstance(body.get("attachments"), list) else []
            app.send(str(body.get("text") or ""), attachments)
        elif path == "/api/stop":
            result = {"stopped": app.stop()}
        elif path == "/api/respond":
            app.respond(body.get("id"), body.get("answer"))
        elif path == "/api/new":
            app.new_chat()
        elif path == "/api/open":
            app.open_session(body.get("id"))
        elif path == "/api/delete":
            app.delete_session(body.get("id"))
        elif path == "/api/rename":
            app.rename_session(body.get("id"), body.get("title"))
        elif path == "/api/undo":
            result = app.undo()
        elif path == "/api/retry":
            app.retry()
        elif path == "/api/compact":
            app.compact(str(body.get("focus") or ""))
        elif path == "/api/settings":
            result = app.update_settings(body)
        elif path == "/api/workdir":
            app.set_workdir(body.get("path"))
        elif path == "/api/mkdir":
            result = app.make_folder(body.get("parent"), body.get("name"))
        elif path == "/api/shutdown":
            self._json({"ok": True})
            self.server.request_shutdown()
            return
        else:
            raise ApiError(HTTPStatus.NOT_FOUND, "Not found.")
        self._json(result if result is not None else {"ok": True})

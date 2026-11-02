"""Desktop app: the local server and its API, transcript replay, the launcher and the shortcuts."""

from __future__ import annotations

import base64
import http.server
import json
import logging
import os
import plistlib
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakeserver import FakeOpenAI, tool_call

import kimi_agent.gui.server as gui_server
from kimi_agent import __version__
from kimi_agent.cli import main as cli_main
from kimi_agent.gui import launcher, shortcuts
from kimi_agent.gui.app import GuiApp
from kimi_agent.gui.render import render_markdown
from kimi_agent.gui.server import GuiServer

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
       b"\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")
SLOW_COMMAND = f'"{sys.executable}" -c "import time; time.sleep(30)"'


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    logger = logging.getLogger("kimi_agent")
    saved = logger.handlers[:], logger.level, logger.propagate
    yield
    for handler in logger.handlers:
        if handler not in saved[0]:
            handler.close()
    logger.handlers[:] = saved[0]
    logger.setLevel(saved[1])
    logger.propagate = saved[2]


def reply(body: dict[str, Any], fake: Any) -> dict[str, Any]:
    last = body["messages"][-1]
    text = last["content"] if isinstance(last.get("content"), str) else json.dumps(last.get("content"))
    if last["role"] == "tool":
        return {"content": "Done."}
    if "hello" in text:
        return {"reasoning": "The user greets me.",
                "content": "Hello! I'm **Kimi Agent**.\n\n```python\nprint('hi')\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |"}
    if "file" in text:
        return tool_call("write_file", path="notes.txt", content="hi\n")
    if "slow" in text:
        return tool_call("run_command", command=SLOW_COMMAND)
    if "question" in text:
        return tool_call("ask_user", question="Which color?", choices=["red", "blue"])
    return {"content": "OK."}


def window_key(page: httpx.Response) -> str:
    """The key that the page of a launch link hands its window."""
    assert page.status_code == 200, page.status_code
    match = re.search(r'<meta name="kimi-agent-key" content="([^"]+)">', page.text)
    assert match, "no key in the page"
    return match.group(1)


class Gui:
    """A running app + server, a signed-in client and the events of one window."""

    def __init__(self, app: GuiApp, server: GuiServer, fake: FakeOpenAI) -> None:
        self.app, self.server, self.fake = app, server, fake
        self.base = server.url.rstrip("/")
        self.client = self.new_client()
        self.key = window_key(self.client.get(self.launch_path()))
        self.client.headers["X-Kimi-Agent"] = self.key
        self.events: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def new_client(self, **kwargs: Any) -> httpx.Client:
        return httpx.Client(base_url=self.base, trust_env=False, timeout=30, **kwargs)

    def launch_path(self) -> str:
        return self.server.launch_url().removeprefix(self.base)

    def post(self, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        return self.client.post(path, json=body or {})

    def listen(self) -> None:
        """Follow the event stream like a window does."""
        def read() -> None:
            try:
                with self.new_client(cookies=self.client.cookies) as client, \
                        client.stream("GET", "/api/events", params={"key": self.key}, timeout=None) as response:
                    for line in response.iter_lines():
                        if line.startswith("data: "):
                            with self.lock:
                                self.events.append(json.loads(line[6:]))
            except httpx.HTTPError:
                pass

        threading.Thread(target=read, daemon=True).start()
        self.wait_for(lambda e: e["type"] == "reset", what="the first snapshot")

    def wait_for(self, pred: Callable[[dict[str, Any]], bool], timeout: float = 20, what: str = "an event"
                 ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                for event in self.events:
                    if pred(event):
                        return event
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}; last events: {self.events[-5:]}")

    def wait_idle(self, timeout: float = 20) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.client.get("/api/state").json()
            if state["ready"] and not state["running"]:
                return state
            time.sleep(0.05)
        raise AssertionError("the agent is still busy")

    def prompt(self, kind: str, exclude: tuple[str, ...] = ()) -> dict[str, Any]:
        return self.wait_for(lambda e: e["type"] == "prompt" and e["kind"] == kind and e["resolved"] is None
                             and e["id"] not in exclude, what=f"a {kind} prompt")


def _overrides(fake: FakeOpenAI, workdir: Path) -> dict[str, Any]:
    return {"provider": "custom", "base_url": fake.base_url, "model": "fake-model", "api_key": "test-key",
            "workdir": str(workdir), "max_retries": 0, "web": False}


@pytest.fixture
def gui(home: Path, workdir: Path) -> Iterator[Gui]:
    with FakeOpenAI(handler=reply) as fake:
        app = GuiApp(_overrides(fake, workdir), use_mcp=False)
        server = GuiServer(app, port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
        app.start()
        harness = Gui(app, server, fake)
        try:
            harness.wait_idle()
            yield harness
        finally:
            server.request_shutdown()
            thread.join(10)
            app.close()
            server.server_close()
            harness.client.close()


# ---------------------------------------------------------------------- server and API
def test_requests_need_a_signed_in_window(gui: Gui) -> None:
    anon = gui.new_client()
    assert anon.get("/").status_code == 401
    assert anon.get("/api/state").status_code == 401
    assert anon.post("/api/send", json={"text": "x"}, headers={"X-Kimi-Agent": gui.key}).status_code == 401
    launch = gui.launch_path()
    page = anon.get(launch)  # the link signs this client in and hands its window the key...
    assert window_key(page) == gui.server.key and "HttpOnly" in page.headers["Set-Cookie"]
    assert gui.new_client().get(launch).status_code == 401  # ... and works only once
    assert anon.get(launch).status_code == 303  # reloading the link in a signed-in window is fine
    assert anon.get("/").status_code == 200 and gui.server.key not in anon.get("/").text

    page = gui.client.get("/")
    assert page.status_code == 200 and 'data-theme-pref="system"' in page.text
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    for name in ("app.js", "app.css", "theme.js", "icon.svg", "icon.png", "pygments.css"):
        assert gui.client.get(f"/static/{name}").status_code == 200, name
    for name in ("index.html", "../app.py", "%2e%2e/app.py", "icon.icns"):
        assert gui.client.get(f"/static/{name}").status_code == 404, name

    # cross-site requests: no custom header, a foreign Origin, a foreign Host (DNS rebinding)
    with gui.new_client(cookies=gui.client.cookies) as plain:
        assert plain.post("/api/new").status_code == 401
        assert plain.get("/api/state", headers={"Host": "evil.example"}).status_code == 421
    assert gui.client.post("/api/new", headers={"Origin": "http://evil.example"}).status_code == 403
    assert gui.client.post("/api/new", headers={"Origin": gui.base}).status_code == 200


def test_web_pages_cannot_use_launch_links(gui: Gui) -> None:
    # Other users can read launch links from the browser's command line. A page of theirs in this user's browser
    # must not be able to use one, for example at http://localhost:<port>, which they could serve from [::1].
    launch, port = gui.launch_path(), gui.server.port
    anon = gui.new_client()
    wrong_host = anon.get(launch, headers={"Host": f"localhost:{port}"})
    assert wrong_host.status_code == 421 and f"http://127.0.0.1:{port}" in wrong_host.text
    assert gui.client.get("/api/state", headers={"Host": f"localhost:{port}"}).status_code == 421
    assert gui.client.post("/api/new", headers={"Origin": f"http://localhost:{port}"}).status_code == 403
    for site in ("same-origin", "same-site", "cross-site"):  # fetch(), a link or a redirect from a page
        page = anon.get(launch, headers={"Sec-Fetch-Site": site, "Sec-Fetch-Mode": "navigate"})
        assert page.status_code == 403 and "opened by a web page" in page.text and gui.server.key not in page.text
    with gui.new_client(cookies=gui.client.cookies) as window:  # a signed-in window is just sent to the app
        assert window.get(launch, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 303
    # The browser opening the link itself (the app's window, the address bar) still works, and the code wasn't used up.
    page = anon.get(launch, headers={"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"})
    assert window_key(page) == gui.server.key


def test_the_cookie_alone_is_not_enough(gui: Gui) -> None:
    # Browsers also send the cookie to other web apps on 127.0.0.1, which may belong to another user of the computer.
    approval = gui.app.settings.approval
    with gui.new_client(cookies=gui.client.cookies) as other_app:
        for headers in ({}, {"X-Kimi-Agent": "1"}, {"X-Kimi-Agent": gui.key[:-1]}):
            assert other_app.get("/api/state", headers=headers).status_code == 401
            assert other_app.get("/api/export", params={"id": "current"}, headers=headers).status_code == 401
            assert other_app.post("/api/settings", json={"approval": "yolo"}, headers=headers).status_code == 401
        assert other_app.get("/api/events").status_code == 401
        assert other_app.get("/api/events", params={"key": gui.key[:-1]}).status_code == 401
        assert other_app.get("/api/state", params={"key": gui.key}).status_code == 401  # only EventSource needs that
        image = gui.app.images.add(PNG, "image/png")  # <img> can't send the key either; the IDs are unguessable
        assert other_app.get(image).status_code == 200
    assert gui.app.settings.approval == approval
    assert gui.client.get("/api/state").status_code == 200


def test_another_launcher_needs_the_lock_file_secret(gui: Gui) -> None:
    nonce = "ab" * 16

    def auth(path: str, secret: str = gui.server.secret, nonce: str = nonce) -> dict[str, str]:
        return {"Authorization": f"KimiAgent {nonce}.{launcher.launcher_mac(secret, 'launcher', path, nonce)}"}

    with gui.new_client() as launcher_client:
        assert launcher_client.post("/api/launch").status_code == 403
        assert launcher_client.post("/api/launch", headers=auth("/api/launch", secret="nope")).status_code == 403
        assert launcher_client.post("/api/quit", headers=auth("/api/launch")).status_code == 403  # another path
        assert launcher_client.post("/api/launch", headers=auth("/api/launch", nonce="short")).status_code == 403
        assert launcher_client.post("/api/launch", headers={**auth("/api/launch"), "Origin": gui.base}).status_code == 403
        reply = launcher_client.post("/api/launch", headers=auth("/api/launch")).json()
    assert reply["proof"] == launcher.launcher_mac(gui.server.secret, "server", "/api/launch", nonce)
    assert "/auth?code=" in reply["url"]
    with gui.new_client() as window:
        window.headers["X-Kimi-Agent"] = window_key(window.get(reply["url"].removeprefix(gui.base)))
        assert window.get("/api/state").status_code == 200
    assert not gui.server.stopping.is_set()


def test_unusual_cookies_of_other_local_apps_are_ignored(gui: Gui) -> None:
    mine = f"{gui.server.cookie_name}={gui.server.token}"
    with gui.new_client(headers={"X-Kimi-Agent": gui.key}) as window:  # SimpleCookie would reject these headers
        for junk in ("username=John Doe", "flag", 'prefs={"a":1}', "path=/", "x=a\\b", f"{gui.server.cookie_name}=old"):
            for header in (f"{junk}; {mine}", f"{mine}; {junk}"):
                assert window.get("/api/state", headers={"Cookie": header}).status_code == 200, header
        assert window.get("/api/state", headers={"Cookie": f"flag; {gui.server.cookie_name}=x"}).status_code == 401
        assert window.get("/api/state", headers={"Cookie": f"{gui.server.cookie_name}"}).status_code == 401


def test_image_types_come_from_an_allowlist(gui: Gui) -> None:
    injected = gui.app.images.add(PNG, "image/png\r\nX-Injected: 1\r\n\r\n<script>alert(1)</script>")
    response = gui.client.get(injected)
    assert response.status_code == 200 and "x-injected" not in response.headers
    assert response.headers["content-type"] == "application/octet-stream"
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert gui.client.get(gui.app.images.add(PNG, "IMAGE/PNG")).headers["content-type"] == "image/png"
    svg = gui.app.images.add_data_url("data:image/svg+xml;base64," + base64.b64encode(b"<svg/>").decode())
    assert svg and gui.client.get(svg).headers["content-type"] == "application/octet-stream"


@pytest.mark.skipif(os.name == "nt", reason="POSIX user IDs")
def test_connections_from_other_users_are_refused(gui: Gui, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gui_server, "CHECK_PEER", True)
    for owner in (os.getuid() + 1, gui_server.GONE):  # another user, or a client that hung up before the check
        monkeypatch.setattr(gui_server, "peer_uid", lambda client, server, owner=owner: owner)
        with pytest.raises(httpx.TransportError):
            gui.client.get("/api/state")
    for owner in (None, 0, os.getuid()):  # unknown, root (can read our files anyway) or us
        monkeypatch.setattr(gui_server, "peer_uid", lambda client, server, owner=owner: owner)
        assert gui.client.get("/api/state").status_code == 200


@pytest.mark.skipif(not Path("/proc/net/tcp").is_file(), reason="Linux /proc/net/tcp")
def test_peer_uid_reads_the_owner_of_a_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    hex_ip = "0100007F" if sys.byteorder == "little" else "7F000001"
    assert gui_server._proc_net_address("127.0.0.1", 41753, socket.AF_INET) == f"{hex_ip}:A319"
    monkeypatch.setattr(gui_server, "ON_WSL", False)
    with socket.create_server(("127.0.0.1", 0)) as listener:
        for hang_up in ("close", "reset"):
            client = socket.create_connection(listener.getsockname())
            conn, address = listener.accept()
            with conn:
                assert address == client.getsockname()
                assert gui_server.peer_uid(address, conn.getsockname()) == os.getuid()
                assert gui_server.still_open(conn)
                if hang_up == "reset":
                    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                client.close()  # the kernel then lists the client end as root's, or (reset) not at all
                assert gui_server.peer_uid(address, conn.getsockname()) == gui_server.GONE, hang_up
                deadline = time.monotonic() + 5
                while gui_server.still_open(conn) and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert not gui_server.still_open(conn), hang_up
        assert gui_server.peer_uid(("127.0.0.1", 1), listener.getsockname()) == gui_server.GONE
        monkeypatch.setattr(gui_server, "ON_WSL", True)  # Windows programs connecting into WSL aren't listed
        assert gui_server.peer_uid(("127.0.0.1", 1), listener.getsockname()) is None
    assert gui_server.peer_uid(("127.0.0.1", 1), ("127.0.0.1", 2)) is None  # /proc shows nothing of ours


@pytest.mark.skipif(not Path("/proc/net/tcp").is_file(), reason="Linux /proc/net/tcp")
@pytest.mark.parametrize("hang_up", ["close", "reset"])
def test_requests_of_clients_that_hung_up_are_dropped(gui: Gui, monkeypatch: pytest.MonkeyPatch, hang_up: str) -> None:
    # Another user could send a launch link read from the browser's command line, then hang up before the check.
    monkeypatch.setattr(gui_server, "ON_WSL", False)
    server = GuiServer(gui.app, port=0)
    try:
        for check, survives in ((True, True), (False, False)):
            monkeypatch.setattr(gui_server, "CHECK_PEER", check)
            code = server.launch_url().split("code=", 1)[1]
            client = socket.create_connection(server.server_address)
            client.sendall(f"GET /auth?code={code} HTTP/1.0\r\nHost: 127.0.0.1:{server.port}\r\n\r\n".encode())
            if hang_up == "reset":
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            client.close()
            server.handle_request()  # accepted only now, as by a busy server
            deadline = time.monotonic() + 5
            while not survives and code in server.codes and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (code in server.codes) is survives, check
    finally:
        server.server_close()


def test_chat_approvals_questions_and_stop(gui: Gui, workdir: Path) -> None:
    gui.listen()

    assert gui.post("/api/send", {"text": "hello there"}).json() == {"ok": True}
    answer = gui.wait_for(lambda e: e["type"] == "assistant" and not e["streaming"] and "Kimi Agent" in e["text"],
                          what="the answer")
    assert "<strong>Kimi Agent</strong>" in answer["html"] and "code-block" in answer["html"]
    assert "<table" in answer["html"]
    thought = gui.wait_for(lambda e: e["type"] == "reasoning" and e["done"], what="the reasoning")
    with gui.lock:  # live updates carry only what is new
        deltas = [e.get("delta", "") for e in gui.events if e["type"] == "reasoning" and e["id"] == thought["id"]]
    assert "".join(deltas) == "The user greets me."
    gui.wait_for(lambda e: e["type"] == "user" and e["text"] == "hello there", what="the user message")
    assert gui.wait_idle()["title"]

    # approve a change
    assert gui.post("/api/settings", {"approval": "ask"}).json()["approval"] == "ask"
    gui.post("/api/send", {"text": "make a file"})
    first = gui.prompt("approval")
    assert first["name"] == "write_file"
    assert gui.client.get("/api/state").json()["attention"] is True
    assert gui.post("/api/respond", {"id": first["id"], "answer": {"answer": "bogus"}}).status_code == 400
    assert gui.post("/api/respond", {"id": first["id"], "answer": {"answer": "yes"}}).json() == {"ok": True}
    gui.wait_for(lambda e: e["type"] == "tool" and e["name"] == "write_file" and e["status"] == "done",
                 what="the finished tool")
    gui.wait_idle()
    assert (workdir / "notes.txt").read_text() == "hi\n"

    # deny one, with feedback for the model
    (workdir / "notes.txt").unlink()
    gui.post("/api/send", {"text": "make a file again"})
    second = gui.prompt("approval", exclude=(first["id"],))
    gui.post("/api/respond", {"id": second["id"], "answer": {"answer": "no", "feedback": "use notes.md"}})
    gui.wait_for(lambda e: e["type"] == "tool" and e["status"] == "denied", what="the denied tool")
    gui.wait_idle()
    assert not (workdir / "notes.txt").exists()
    assert "use notes.md" in gui.fake.requests[-1]["messages"][-1]["content"]

    # a question for the user
    gui.post("/api/settings", {"approval": "yolo"})
    gui.post("/api/send", {"text": "a question please"})
    question = gui.prompt("question")
    assert question["choices"] == ["red", "blue"]
    gui.post("/api/respond", {"id": question["id"], "answer": {"answer": "blue"}})
    gui.wait_idle()
    assert "blue" in gui.fake.requests[-1]["messages"][-1]["content"]

    # stop a long-running command
    gui.post("/api/send", {"text": "slow please"})
    gui.wait_for(lambda e: e["type"] == "tool" and e["name"] == "run_command" and e["status"] == "running",
                 what="the running command")
    started = time.monotonic()
    assert gui.post("/api/stop").json() == {"stopped": True}
    gui.wait_for(lambda e: e["type"] == "notice" and e["level"] == "stopped", what="the stopped notice")
    gui.wait_idle()
    assert time.monotonic() - started < 10
    assert gui.post("/api/stop").json() == {"stopped": False}


def test_sessions_undo_retry_and_replay(gui: Gui) -> None:
    gui.listen()
    gui.post("/api/send", {"text": "a question please"})
    gui.post("/api/respond", {"id": gui.prompt("question")["id"], "answer": {"answer": "blue"}})
    gui.wait_idle()
    gui.post("/api/send", {"text": "hello"})
    gui.wait_idle()
    gui.post("/api/send", {"text": "one more thing"})
    gui.wait_idle()

    sessions = gui.client.get("/api/sessions").json()["sessions"]
    assert len(sessions) == 1
    session_id = sessions[0]["id"]
    gui.post("/api/rename", {"id": session_id, "title": "Renamed chat"})
    assert gui.client.get("/api/state").json()["title"] == "Renamed chat"
    exported = gui.client.get("/api/export", params={"id": "current"})
    assert exported.status_code == 200 and "one more thing" in exported.text
    assert "attachment" in exported.headers["Content-Disposition"]

    assert gui.post("/api/undo").json()["text"] == "one more thing"
    gui.post("/api/new")
    gui.wait_for(lambda e: e["type"] == "reset" and e["count"] == 0, what="an empty chat")
    state = gui.client.get("/api/state").json()
    assert state["session_id"] != session_id and not state["can_undo"]

    with gui.lock:
        gui.events.clear()
    gui.post("/api/open", {"id": session_id})
    reset = gui.wait_for(lambda e: e["type"] == "reset", what="the reopened chat")
    deadline = time.monotonic() + 10
    while len(gui.events) <= reset["count"] and time.monotonic() < deadline:
        time.sleep(0.05)
    with gui.lock:
        replayed = gui.events[gui.events.index(reset) + 1:][:reset["count"]]
    assert [e["text"] for e in replayed if e["type"] == "user"] == ["a question please", "hello"]
    assert any(e["type"] == "prompt" and e["resolved"] == {"answer": "blue"} for e in replayed)
    assert replayed[-1]["type"] == "assistant" and "Kimi Agent" in replayed[-1]["text"]
    assert gui.client.get("/api/state").json()["title"] == "Renamed chat"

    requests = len(gui.fake.requests)
    assert gui.post("/api/retry").status_code == 200  # runs "hello" again
    gui.wait_idle()
    assert len(gui.fake.requests) == requests + 1
    assert gui.fake.requests[-1]["messages"][-1]["content"] == "hello"
    assert len([m for m in gui.fake.requests[-1]["messages"] if m["role"] == "user"]) == 2

    gui.post("/api/delete", {"id": session_id})
    assert session_id not in [s["id"] for s in gui.client.get("/api/sessions").json()["sessions"]]


def test_settings_and_api_key(gui: Gui, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.delenv("OPENAI_API_KEY")  # the app sets it; this restores "unset" afterwards
    settings = gui.client.get("/api/settings").json()
    assert settings["provider"] == "custom" and settings["version"] == __version__
    assert {"kimi", "openrouter", "ollama"} <= {p["name"] for p in settings["providers"]}

    assert gui.post("/api/settings", {"theme": "dark"}).json()["theme"] == "dark"
    assert 'data-theme-pref="dark"' in gui.client.get("/").text
    assert json.loads((home / "gui.json").read_text())["theme"] == "dark"
    assert gui.post("/api/settings", {"theme": "neon"}).status_code == 400
    assert gui.post("/api/settings", {"approval": "sometimes"}).status_code == 400
    assert gui.post("/api/settings", {"provider": "nope"}).status_code == 400

    assert gui.post("/api/settings", {"api_key": "has space"}).status_code == 400
    saved = gui.post("/api/settings", {"api_key": "sk-test-abcdef1234567890"}).json()
    assert saved["key_hint"] == "…7890" and saved["has_key"]
    env_file = home / ".env"
    assert "OPENAI_API_KEY=sk-test-abcdef1234567890" in env_file.read_text()
    if os.name != "nt":
        assert env_file.stat().st_mode & 0o777 == 0o600
    assert gui.app.settings.api_key == "sk-test-abcdef1234567890"

    assert gui.client.get("/api/models").json()["models"] == ["fake-model", "fake-model-large"]


def test_folders_and_uploads(gui: Gui, workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user = tmp_path / "user"
    (user / "Desktop").mkdir(parents=True)
    (user / "OneDrive" / "Downloads").mkdir(parents=True)  # Windows may move known folders to OneDrive
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("USERPROFILE", str(user))
    (workdir / "b-dir").mkdir()
    (workdir / "A-dir").mkdir()
    (workdir / ".hidden").mkdir()
    (workdir / "file.txt").write_text("x")

    listing = gui.client.get("/api/browse", params={"path": str(workdir)}).json()
    assert listing["path"] == str(workdir) and listing["parent"] == str(workdir.parent)
    assert [d["name"] for d in listing["dirs"]] == ["A-dir", "b-dir"]
    assert listing["places"] == [{"name": "Desktop", "path": str(user / "Desktop")},
                                 {"name": "Downloads", "path": str(user / "OneDrive" / "Downloads")}]
    assert gui.client.get("/api/browse", params={"path": str(workdir / "file.txt")}).status_code == 404

    made = gui.post("/api/mkdir", {"parent": str(workdir), "name": "new folder"}).json()
    assert Path(made["path"]).is_dir()
    assert gui.post("/api/mkdir", {"parent": str(workdir), "name": "new folder"}).status_code == 409
    for bad in ("../x", "..", "a/b", ""):
        assert gui.post("/api/mkdir", {"parent": str(workdir), "name": bad}).status_code == 400, bad
    assert gui.post("/api/workdir", {"path": made["path"]}).json() == {"ok": True}
    state = gui.client.get("/api/state").json()
    assert state["workdir"] == made["path"] and made["path"] in state["recent_folders"]

    uploaded = gui.client.post("/api/upload", params={"name": "dot.png"}, content=PNG,
                               headers={"Content-Type": "application/octet-stream"}).json()
    assert uploaded["name"] == "dot.png" and uploaded["size"] == len(PNG)
    assert gui.client.get(uploaded["image"]).content == PNG
    gui.post("/api/send", {"text": "what is this", "attachments": [{"path": uploaded["path"], "name": "dot.png"}]})
    gui.wait_idle()
    assert "dot.png" in json.dumps(gui.fake.requests[-1]["messages"][-1])
    outside = str(workdir / "file.txt")
    assert gui.post("/api/send", {"text": "x", "attachments": [{"path": outside}]}).status_code == 400


# ---------------------------------------------------------------------- transcript replay
def _call(call_id: str, name: str, **args: Any) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_replay_rebuilds_plans_questions_and_unanswered_messages(home: Path, workdir: Path) -> None:
    app = GuiApp({"provider": "custom", "base_url": "http://127.0.0.1:9/v1", "model": "m"}, use_mcp=False)
    messages = [
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": "first", "_turn": True},
        {"role": "user", "content": "plan it", "_turn": True},
        {"role": "assistant", "content": "", "reasoning_content": "Thinking.", "tool_calls": [
            _call("c1", "update_plan", explanation="Two steps.", steps=[{"step": "One", "status": "completed"}, "Two"]),
            _call("c2", "ask_user", question="Which **color**?", choices=["red", "blue"]),
            _call("c3", "read_file", path="missing.txt"),
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "Plan updated."},
        {"role": "tool", "tool_call_id": "c2", "content": "The user answered: blue"},
        {"role": "tool", "tool_call_id": "c3", "content": "Error: missing.txt does not exist"},
        {"role": "assistant", "content": "All **done**."},
        {"role": "user", "content": "Summary of earlier messages", "_summary": True},
        {"role": "user", "content": "and then?", "_turn": True},
    ]
    items = app.messages_to_events(messages)
    kinds = [(i["type"], i.get("name") or i.get("kind") or i.get("text", "")[:40]) for i in items]
    assert kinds == [
        ("user", "first"),
        ("notice", "This message didn't get a reply."),
        ("user", "plan it"),
        ("reasoning", "Thinking."),
        ("tool", "update_plan"),
        ("tool", "ask_user"),
        ("tool", "read_file"),
        ("plan", ""),
        ("prompt", "question"),
        ("assistant", "All **done**."),
        ("notice", "Earlier messages were summarized to fit "),
        ("user", "and then?"),
        ("notice", "This message didn't get a reply."),
    ]
    plan = next(i for i in items if i["type"] == "plan")
    assert plan["explanation"] == "Two steps."
    assert plan["steps"] == [{"step": "One", "status": "done"}, {"step": "Two", "status": "pending"}]
    question = next(i for i in items if i["type"] == "prompt")
    assert question["resolved"] == {"answer": "blue"} and "<strong>color</strong>" in question["question"]
    failed = next(i for i in items if i.get("name") == "read_file")
    assert failed["status"] == "error" and failed["hidden"] is False
    assert all("_args" not in i for i in items)
    assert len({i["id"] for i in items}) == len(items)


def test_replay_survives_malformed_tool_calls(home: Path, workdir: Path) -> None:
    app = GuiApp({"provider": "custom", "base_url": "http://127.0.0.1:9/v1", "model": "m"}, use_mcp=False)
    items = app.messages_to_events([
        {"role": "user", "content": "go", "_turn": True},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "x", "function": {"name": "update_plan", "arguments": '{"steps": "not a list"}'}},
            {"id": "y", "function": {"name": "run_command", "arguments": "{not json"}},
        ]},
        {"role": "tool", "tool_call_id": "x", "content": "Plan updated."},
        {"role": "tool", "tool_call_id": "unknown", "content": "ignored"},
    ])
    assert [i["type"] for i in items] == ["user", "tool", "tool", "plan"]
    assert next(i for i in items if i["type"] == "plan")["steps"] == []
    broken = next(i for i in items if i.get("name") == "run_command")
    assert broken["status"] == "stopped" and broken["args"] == "{not json"


# ---------------------------------------------------------------------- rendering
def test_markdown_is_rendered_safely() -> None:
    html = render_markdown("<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n"
                           "[ok](https://example.com) [bad](javascript:alert(1))\n\n"
                           "![remote](https://example.com/a.png) ![inline](data:image/png;base64,AAAA)")
    assert "<script" not in html and "&lt;script&gt;" in html
    assert "<img src=x" not in html
    assert '<a href="https://example.com" target="_blank" rel="noopener noreferrer">ok</a>' in html
    assert 'href="javascript:' not in html
    assert '<img src="https://' not in html and 'class="image-link"' in html  # no remote images (tracking)
    assert '<img src="data:image/png;base64,AAAA" alt="inline">' in html
    code = render_markdown("```python\nx = '<b>'\n```")
    assert "code-block" in code and "<b>" not in code
    assert render_markdown("") == ""


# ---------------------------------------------------------------------- launcher
def test_find_browser_and_open_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIMI_AGENT_BROWSER", "/opt/browser/chrome")
    assert launcher.find_browser() == ["/opt/browser/chrome"]
    spawned: list[list[str]] = []
    monkeypatch.setattr(launcher, "_spawn", spawned.append)
    assert launcher.open_window("http://127.0.0.1:1/auth?code=x") is True
    assert spawned == [["/opt/browser/chrome", "--app=http://127.0.0.1:1/auth?code=x", "--new-window",
                        f"--window-size={launcher.WINDOW_SIZE}"]]

    monkeypatch.setattr(launcher, "find_browser", lambda: None)
    opened: list[str] = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)
    assert launcher.open_window("http://x/") is True and opened == ["http://x/"]


def test_errors_go_to_the_terminal_when_there_is_one(monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(launcher, "_has_console", lambda: True)
    launcher.show_error("something broke")
    assert "kimi-agent: something broke" in capsys.readouterr().err
    assert launcher._applescript_string('say "hi" \\o/') == '"say \\"hi\\" \\\\o/"'


@pytest.mark.skipif(os.name == "nt", reason="POSIX paths")
def test_extend_path_adds_user_tool_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    launcher._extend_path()
    parts = os.environ["PATH"].split(os.pathsep)
    assert parts[:2] == ["/usr/bin", "/bin"] and str(tmp_path / ".local" / "bin") in parts
    before = os.environ["PATH"]
    launcher._extend_path()
    assert os.environ["PATH"] == before


def test_second_launch_hands_off_and_quit_closes_it(home: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))  # restored afterwards; run_gui may extend it
    lock = home / "gui.lock.json"
    with FakeOpenAI(handler=reply) as fake:
        overrides = {**_overrides(fake, workdir), "workdir": None}
        result: dict[str, int] = {}
        thread = threading.Thread(daemon=True, target=lambda: result.update(
            code=launcher.run_gui(overrides, open_browser=False, use_mcp=False, default_workdir=str(workdir))))
        thread.start()
        deadline = time.monotonic() + 20
        while not lock.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        info = json.loads(lock.read_text())
        assert info["pid"] == os.getpid() and info["version"] == __version__
        if os.name != "nt":
            assert lock.stat().st_mode & 0o777 == 0o600

        assert launcher.run_gui(overrides, open_browser=False, use_mcp=False) == 0
        out = capsys.readouterr().out
        assert "Open this link in your browser" in out  # the first instance
        handed_off = out.split("already running", 1)[1]
        link = re.search(r"http://127\.0\.0\.1:\d+/auth\?code=\S+", handed_off)
        assert link and f":{info['port']}/" in link.group(0)
        with httpx.Client(trust_env=False) as window:
            window.headers["X-Kimi-Agent"] = window_key(window.get(link.group(0)))
            assert window.get(f"http://127.0.0.1:{info['port']}/api/state").json()["workdir"] == str(workdir)

        assert launcher._ask_running({**info, "secret": "wrong"}, "/api/quit") is None
        assert thread.is_alive()
        assert launcher.quit_running(home) is True
        thread.join(15)
        assert not thread.is_alive() and result["code"] == 0
        assert not lock.exists()
        assert launcher.quit_running(home) is False


def test_handoff_uses_the_browser_from_env_files(home: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setenv("KIMI_AGENT_BROWSER", "unset")
    home.mkdir(parents=True, exist_ok=True)
    (home / "gui.lock.json").write_text(json.dumps({"pid": os.getpid(), "port": 9, "secret": "s"}))
    link = "http://127.0.0.1:9/auth?code=c"
    monkeypatch.setattr(launcher, "_ask_running", lambda info, path: {"url": link} if path == "/api/launch" else None)
    spawned: list[list[str]] = []
    monkeypatch.setattr(launcher, "_spawn", spawned.append)

    (home / ".env").write_text("KIMI_AGENT_BROWSER=/opt/home/browser\n")
    (workdir / ".env").write_text("KIMI_AGENT_BROWSER=/opt/project/browser\n")
    for expected in ("/opt/project/browser", "/opt/home/browser"):  # the folder's .env wins, as in load_settings
        monkeypatch.delenv("KIMI_AGENT_BROWSER")
        assert launcher.run_gui({}, use_mcp=False) == 0  # already running: no settings are loaded
        assert spawned.pop()[:2] == [expected, f"--app={link}"]
        (workdir / ".env").unlink(missing_ok=True)


def test_launcher_trusts_only_a_server_that_knows_the_secret(home: Path) -> None:
    seen: list[str] = []

    class Impostor(http.server.BaseHTTPRequestHandler):  # took over the port after a crash
        def do_POST(self) -> None:
            seen.append(self.headers.get("Authorization", ""))
            body = json.dumps({"ok": True, "url": "http://evil.example/", "proof": "0" * 64}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    secret = "lock-file-secret"
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Impostor) as impostor:
        threading.Thread(target=impostor.serve_forever, daemon=True).start()
        try:
            home.mkdir(parents=True, exist_ok=True)
            lock = home / "gui.lock.json"
            lock.write_text(json.dumps({"pid": os.getpid(), "port": impostor.server_address[1], "secret": secret}))
            assert launcher._request_window(lock) is None
            assert launcher.quit_running(home) is False
        finally:
            impostor.shutdown()
    assert len(seen) == 2 and not any(secret in header for header in seen)


def test_quit_gui_flag_when_nothing_runs(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["--quit-gui"]) == 0
    assert "not running" in capsys.readouterr().out
    assert launcher.main(["--quit-gui"]) == 0
    assert "not running" in capsys.readouterr().out


def test_stale_lock_files_are_ignored(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    lock = home / "gui.lock.json"
    lock.write_text(json.dumps({"pid": 999999, "port": 9, "secret": "s"}))  # nothing listens on port 9
    assert launcher._request_window(lock) is None
    assert launcher.quit_running(home) is False
    lock.write_text("not json")
    assert launcher._read_lock(lock) is None


def test_gui_module_runs() -> None:
    out = subprocess.run([sys.executable, "-m", "kimi_agent.gui", "--version"], capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0 and __version__ in out.stdout


# ---------------------------------------------------------------------- shortcuts
def test_desktop_entry_quotes_the_command() -> None:
    assert shortcuts._exec_arg("/usr/bin/kimi-agent-gui") == "/usr/bin/kimi-agent-gui"
    assert shortcuts._exec_arg("/home/me/my apps/kimi") == '"/home/me/my apps/kimi"'
    assert shortcuts._exec_arg("100%") == "100%%"
    assert shortcuts._exec_arg("a$b") == '"a\\\\$b"'  # \$ inside quotes, then the file format escapes the \
    assert shortcuts._exec_arg("C:\\x") == '"C:\\\\\\\\x"'
    entry = shortcuts.desktop_entry(["/opt/kimi agent/python", "-m", "kimi_agent.gui"])
    lines = entry.splitlines()
    assert lines[0] == "[Desktop Entry]" and entry.endswith("\n")
    assert 'Exec="/opt/kimi agent/python" -m kimi_agent.gui' in lines
    assert "Name=Kimi Agent" in lines and "Terminal=false" in lines
    icon = next(line for line in lines if line.startswith("Icon="))
    assert Path(icon[5:]).is_file()


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="freedesktop.org menus are for Linux and BSD")
def test_linux_shortcuts_install_and_remove(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    user = tmp_path / "user"
    (user / "Desktop").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("XDG_DATA_HOME", str(user / "share"))
    monkeypatch.setattr(shortcuts.shutil, "which", lambda name: None)  # no gio, xdg-user-dir, update-desktop-database
    monkeypatch.setattr(shortcuts, "launcher_command", lambda: ["/opt/kimi agent/bin/kimi-agent-gui"])

    assert cli_main(["--install-shortcuts"]) == 0
    menu = user / "share" / "applications" / "kimi-agent.desktop"
    desktop = user / "Desktop" / "kimi-agent.desktop"
    for path in (menu, desktop):
        assert path.read_text() == shortcuts.desktop_entry(["/opt/kimi agent/bin/kimi-agent-gui"])
        assert path.stat().st_mode & 0o777 == 0o755
    assert f"Added {menu}" in capsys.readouterr().out

    assert cli_main(["--remove-shortcuts"]) == 0
    assert not menu.exists() and not desktop.exists()
    assert f"Removed {desktop}" in capsys.readouterr().out
    assert cli_main(["--remove-shortcuts"]) == 0


def test_macos_app_bundle(tmp_path: Path) -> None:
    command = ["/Users/me/.local/bin/kimi-agent-gui"]
    for _ in range(2):  # installing again replaces the bundle
        app = shortcuts.install_macos_app(tmp_path / "Applications", command)
    contents = app / "Contents"
    assert app.name == "Kimi Agent.app"
    script = (contents / "MacOS" / "kimi-agent").read_text()
    assert script == '#!/bin/sh\n/usr/bin/nohup /Users/me/.local/bin/kimi-agent-gui "$@" >/dev/null 2>&1 &\n'
    if os.name != "nt":
        assert (contents / "MacOS" / "kimi-agent").stat().st_mode & 0o111
    info = plistlib.loads((contents / "Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "kimi-agent" and info["CFBundleIconFile"] == "icon"
    assert info["CFBundleShortVersionString"] == __version__ and info["LSUIElement"] is True
    assert (contents / "Resources" / "icon.icns").read_bytes()[:4] == b"icns"
    assert shortcuts._remove([app]) == [app] and not app.exists()


def test_windows_shortcuts_run_powershell(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="C:\\Start\\Kimi Agent.lnk\r\nC:\\Desk\\Kimi Agent.lnk\r\n",
                                           stderr="")

    monkeypatch.setattr(shortcuts.subprocess, "run", run)
    command = ["C:\\Program Files\\Python\\pythonw.exe", "-m", "kimi_agent.gui"]
    paths = shortcuts._windows("install", command, True)
    assert [str(p) for p in paths] == ["C:\\Start\\Kimi Agent.lnk", "C:\\Desk\\Kimi Agent.lnk"]
    argv, kwargs = calls[0]
    assert argv[0] == "powershell.exe" and argv[-2] == "-EncodedCommand"
    script = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert "CreateShortcut" in script and "GetFolderPath('Programs')" in script
    env = kwargs["env"]
    assert (env["KA_MODE"], env["KA_DESKTOP"], env["KA_NAME"]) == ("install", "1", "Kimi Agent")
    assert env["KA_TARGET"] == command[0] and env["KA_ARGS"] == "-m kimi_agent.gui"
    assert env["KA_ICON"].endswith("icon.ico,0")

    monkeypatch.setattr(shortcuts.subprocess, "run",
                        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, stdout="", stderr="no COM"))
    with pytest.raises(RuntimeError, match="no COM"):
        shortcuts._windows("remove", [], True)


def test_launcher_command_prefers_the_gui_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / ("kimi-agent-gui.exe" if os.name == "nt" else "kimi-agent-gui")
    script.write_text("")
    monkeypatch.setattr(shortcuts.sysconfig, "get_path", lambda name: str(tmp_path))
    assert shortcuts.launcher_command() == [str(script.absolute())]
    script.unlink()
    monkeypatch.setattr(shortcuts.shutil, "which", lambda name: None)
    command = shortcuts.launcher_command()
    assert command[1:] == ["-m", "kimi_agent.gui"] and Path(command[0]).is_file()

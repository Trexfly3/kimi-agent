"""The desktop app: owns the Runtime, runs requests on a worker thread and implements the HTTP API."""

from __future__ import annotations

import ctypes
import dataclasses
import datetime as dt
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import __version__
from ..agent import INTERRUPTED, SKIPPED
from ..attachments import expand_mentions
from ..config import (
    APPROVAL_MODES,
    PROVIDERS,
    ConfigError,
    Settings,
    default_data_dir,
    finalize,
    load_settings,
    missing_api_key_message,
)
from ..llm import LLMError, content_text
from ..runtime import Runtime
from ..session import ATTACHMENT_RE, export_markdown
from ..tools.base import ToolError, one_line
from ..tools.interaction import normalize_plan
from .render import render_markdown
from .webui import HIDDEN_TOOLS, MAX_ARGS_CHARS, MAX_OUTPUT_CHARS, EventBus, ImageStore, Prompt, WebUI, clip

log = logging.getLogger("kimi_agent.gui")

PREF_SETTINGS = ("provider", "model", "base_url", "approval", "web", "computer_use", "reasoning_effort")
CONNECTION_SETTINGS = ("provider", "model", "base_url", "web", "computer_use", "reasoning_effort")
EFFORTS = ("low", "medium", "high", "max")
THEMES = ("system", "light", "dark")
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_PREVIEW_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp", ".bmp": "image/bmp"}
SESSION_ID_RE = re.compile(r"[\w.-]{1,120}")
TOOL_IMAGES_NOTE = "Image output of the tool call(s) above"
KEY_URLS = {
    "kimi": "https://platform.moonshot.ai/console/api-keys",
    "kimi-cn": "https://platform.moonshot.cn/console/api-keys",
    "openrouter": "https://openrouter.ai/keys",
    "huggingface": "https://huggingface.co/settings/tokens",
    "groq": "https://console.groq.com/keys",
    "together": "https://api.together.ai/settings/api-keys",
    "fireworks": "https://fireworks.ai/account/api-keys",
    "deepseek": "https://platform.deepseek.com/api_keys",
    "ollama": "https://ollama.com/download",
    "lmstudio": "https://lmstudio.ai",
}


class ApiError(Exception):
    """An error reported to the browser with an HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _async_raise(ident: int, exc: type[BaseException] | None) -> int:
    """Raise ``exc`` in another thread at its next bytecode (None clears a pending one)."""
    return ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(ident),
                                                      ctypes.py_object(exc) if exc is not None else None)


def _write_private(path: Path, text: str) -> None:
    """Atomically write a file only the current user can read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _tool_status(output: str) -> str:
    if output in (INTERRUPTED, SKIPPED):
        return "stopped"
    if output.startswith("The user denied"):
        return "denied"
    if output.startswith(("Error", "Blocked by", "Not permitted")):
        return "error"
    return "done"


class GuiApp:
    """State shared by every browser window: one conversation, one agent, many viewers."""

    def __init__(self, overrides: dict[str, Any] | None = None, config_file: str | None = None, *,
                 use_mcp: bool = True, default_workdir: str | None = None) -> None:
        self.cli_overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
        self.config_file = config_file
        self.use_mcp = use_mcp
        self.default_workdir = default_workdir
        self.data_dir = Path(self.cli_overrides.get("data_dir") or default_data_dir()).expanduser()
        self.prefs_path = self.data_dir / "gui.json"
        self.prefs: dict[str, Any] = self._load_prefs()
        self.lock = threading.RLock()
        self.bus = EventBus()
        self.bus.state_provider = self.state
        self.images = ImageStore()
        self.ui = WebUI(self)
        self.prompts: dict[str, Prompt] = {}
        self.active_tools: dict[str, dict[str, Any]] = {}
        self.uploads: dict[str, str | None] = {}
        self.runtime: Runtime | None = None
        self.ready = False
        self.running = False
        self.stop_requested = False
        self.status = ""
        self.start_error = ""
        self.config_error = ""
        self.warnings: list[str] = []
        self.context_tokens = 0
        self.last_activity = time.monotonic()
        self._worker: threading.Thread | None = None
        self._run_ident: int | None = None
        self._interrupt_lock = threading.Lock()
        self._session_cache: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
        self._computer: tuple[bool, str] | None = None
        self.settings = self._load_settings()

    # ------------------------------------------------------------------ preferences and settings
    def _load_prefs(self) -> dict[str, Any]:
        try:
            data = json.loads(self.prefs_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_prefs(self) -> None:
        try:
            _write_private(self.prefs_path, json.dumps(self.prefs, indent=2))
        except OSError as exc:
            log.warning("could not save GUI preferences: %s", exc)

    def _overrides(self, prefs: dict[str, Any] | None = None) -> dict[str, Any]:
        prefs = self.prefs if prefs is None else prefs
        out: dict[str, Any] = {k: prefs[k] for k in PREF_SETTINGS if prefs.get(k) not in (None, "")}
        workdir = prefs.get("workdir") or self.default_workdir
        if workdir and Path(workdir).is_dir():
            out["workdir"] = workdir
        out.update(self.cli_overrides)
        return out

    def _load_settings(self) -> Settings:
        try:
            settings, self.warnings = load_settings(self._overrides(), self.config_file)
            return settings
        except ConfigError as exc:
            self.config_error = str(exc)
            log.warning("configuration error: %s", exc)
        try:  # the saved GUI choices may be stale: try without them
            settings, self.warnings = load_settings(dict(self.cli_overrides), self.config_file)
            return settings
        except ConfigError:
            settings = Settings(data_dir=str(self.data_dir), workdir=self.default_workdir or str(Path.home()))
            finalize(settings)
            return settings

    def _preset(self, provider: str) -> Any:
        return dataclasses.replace(self.settings, provider=provider).preset

    def _computer_available(self) -> tuple[bool, str]:
        if self._computer is None:
            try:
                from ..tools.computer import computer_available

                self._computer = computer_available()
            except Exception as exc:  # pragma: no cover - depends on the desktop environment
                self._computer = (False, str(exc))
        return self._computer

    def settings_payload(self) -> dict[str, Any]:
        s = self.settings
        names = list(PROVIDERS) + [n for n in s.providers if n not in PROVIDERS]
        providers = []
        for name in names:
            p = self._preset(name)
            has_key = (not p.needs_key or any(os.environ.get(e) for e in p.key_envs)
                       or (name == s.provider and s.api_key not in ("", "not-needed")))
            providers.append({"name": name, "description": p.description, "needs_key": p.needs_key,
                              "key_env": p.key_envs[0] if p.key_envs else "", "has_key": bool(has_key),
                              "model": p.model, "base_url": p.base_url, "key_url": KEY_URLS.get(name, "")})
        key = "" if s.api_key in ("", "not-needed") else s.api_key
        ok, reason = self._computer_available()
        return {
            "provider": s.provider, "model": s.model, "base_url": s.base_url, "approval": s.approval,
            "web": s.web, "computer_use": s.computer_use, "reasoning_effort": s.reasoning_effort or "",
            "theme": self.prefs.get("theme", "system"), "key_hint": f"…{key[-4:]}" if len(key) >= 12 else "",
            "has_key": bool(key) or not s.preset.needs_key, "providers": providers,
            "computer": {"available": ok, "reason": reason}, "version": __version__,
            "config_error": self.config_error,
            "paths": {"data": str(s.data_path), "config": str(s.data_path / "config.toml"),
                      "env": str(s.data_path / ".env"), "logs": str(s.data_path / "logs")},
        }

    def _save_key(self, provider: str, key: str) -> None:
        preset = self._preset(provider)
        if not preset.key_envs:
            raise ApiError(400, f"The {provider} provider does not use an API key.")
        if any(c.isspace() for c in key) or any(c in key for c in "\"'#\\") or len(key) > 1000:
            raise ApiError(400, "That does not look like an API key.")
        env = preset.key_envs[0]
        path = self.settings.data_path / ".env"
        pattern = re.compile(rf"^\s*(export\s+)?{re.escape(env)}\s*=")
        try:
            lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        except OSError:
            lines = []
        lines = [line for line in lines if not pattern.match(line)] + [f"{env}={key}"]
        try:
            _write_private(path, "\n".join(lines) + "\n")
        except OSError as exc:
            raise ApiError(500, f"Could not save the key to {path}: {exc}") from exc
        os.environ[env] = key

    def update_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        if "theme" in body:
            if body["theme"] not in THEMES:
                raise ApiError(400, "Unknown theme.")
            self.prefs["theme"] = body["theme"]
        if "approval" in body:
            if body["approval"] not in APPROVAL_MODES:
                raise ApiError(400, "Unknown approval mode.")
            self.prefs["approval"] = body["approval"]
            self.cli_overrides.pop("approval", None)
            self.settings.approval = body["approval"]
        connection = {k: body[k] for k in CONNECTION_SETTINGS if k in body}
        api_key = str(body.get("api_key") or "").strip()
        if connection or api_key:
            self._update_connection(connection, api_key)
        self._save_prefs()
        self._send_state()
        return self.settings_payload()

    def _update_connection(self, connection: dict[str, Any], api_key: str) -> None:
        with self.lock:
            if self.running:
                raise ApiError(409, "Stop the current task before changing these settings.")
        prefs = dict(self.prefs)
        provider = str(connection.get("provider") or self.settings.provider)
        if provider not in PROVIDERS and provider not in self.settings.providers:
            raise ApiError(400, f"Unknown provider {provider!r}.")
        changed = provider != self.settings.provider
        if changed:
            prefs["model"] = prefs["base_url"] = ""
            for key in ("model", "base_url", "api_key", "context_window", "vision"):
                self.cli_overrides.pop(key, None)
        prefs["provider"] = provider
        for key in ("model", "base_url"):
            if key in connection:
                prefs[key] = str(connection[key] or "").strip()
        for key in ("web", "computer_use"):
            if key in connection:
                prefs[key] = bool(connection[key])
        if "reasoning_effort" in connection:
            effort = str(connection["reasoning_effort"] or "").lower()
            if effort not in ("", *EFFORTS):
                raise ApiError(400, f"Unknown reasoning effort {effort!r}.")
            prefs["reasoning_effort"] = effort
        for key in connection:
            self.cli_overrides.pop(key, None)
        if api_key:
            self._save_key(provider, api_key)
            self.cli_overrides.pop("api_key", None)
        overrides = self._overrides(prefs)
        overrides["workdir"] = str(self.runtime.ctx.cwd) if self.runtime else self.settings.workdir
        try:
            new, self.warnings = load_settings(overrides, self.config_file)
        except ConfigError as exc:
            raise ApiError(400, str(exc)) from exc
        self.prefs = prefs
        self.config_error = ""
        self._apply_settings(new)

    def _apply_settings(self, new: Settings) -> None:
        for f in dataclasses.fields(Settings):
            setattr(self.settings, f.name, getattr(new, f.name))
        runtime = self.runtime
        if runtime is None or not self.ready:
            return
        runtime.llm.reconfigure()
        if runtime.agent is not None:
            runtime.agent.context.window_limit = 0
            runtime.rebuild_tools()
        self._update_context()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        threading.Thread(target=self._start_runtime, name="kimi-agent-start", daemon=True).start()

    def _start_runtime(self) -> None:
        self.set_status("Starting…")
        try:
            for warning in self.warnings:
                self.ui.warn(warning)
            runtime = Runtime(self.settings, self.ui, interactive=True, use_mcp=self.use_mcp)
            self.runtime = runtime
            runtime.start()
            self._update_context()
            self.ready = True
        except Exception as exc:
            log.exception("could not start the agent")
            self.start_error = f"Could not start the agent: {exc}"
        finally:
            self.status = ""
            self._send_state()

    def close(self) -> None:
        self.stop()
        if self._worker is not None:
            self._worker.join(timeout=5)
        if self.runtime is not None:
            try:
                self.runtime.close()
            except Exception:
                log.exception("error while closing the runtime")
        self.bus.send({"type": "shutdown"})
        self.bus.close_all()

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    # ------------------------------------------------------------------ state
    def state(self) -> dict[str, Any]:
        """Everything the window header, composer and sidebar need. Lock-free (called under the bus lock)."""
        s = self.settings
        runtime = self.runtime
        agent = runtime.agent if runtime is not None else None
        return {
            "ready": self.ready, "running": self.running, "status": self.status, "attention": bool(self.prompts),
            "session_id": runtime.session.id if runtime else "", "title": runtime.session.title if runtime else "",
            "workdir": str(runtime.ctx.cwd) if runtime else s.workdir,
            "provider": s.provider, "model": s.model, "approval": s.approval, "effort": s.reasoning_effort or "",
            "vision": bool(s.vision), "theme": self.prefs.get("theme", "system"),
            "needs_setup": missing_api_key_message(s) is not None,
            "config_error": self.config_error, "start_error": self.start_error,
            "context_tokens": self.context_tokens,
            "context_window": agent.context.window if agent is not None else s.context_window,
            "usage": dataclasses.asdict(agent.usage) if agent is not None else {},
            "can_undo": agent is not None and any(m.get("_turn") for m in agent.messages),
            "recent_folders": list(self.prefs.get("recent_folders") or [])[:8],
            "version": __version__, "home": str(Path.home()),
        }

    def _send_state(self) -> None:
        self.bus.send({"type": "state", "state": self.state()})

    def set_status(self, text: str) -> None:
        if self.stop_requested and text:
            text = "Stopping…"
        if text != self.status:
            self.status = text
            self.bus.send({"type": "status", "status": text})

    def _update_context(self) -> None:
        agent = self.runtime.agent if self.runtime is not None else None
        if agent is not None:
            try:
                self.context_tokens = agent.context_tokens()
            except Exception:  # an estimate only
                log.debug("context estimate failed", exc_info=True)

    def _require_idle(self) -> Runtime:
        if not self.ready or self.runtime is None or self.runtime.agent is None:
            raise ApiError(409, self.start_error or "Kimi Agent is still starting.")
        if self.running:
            raise ApiError(409, "Stop the current task first.")
        return self.runtime

    # ------------------------------------------------------------------ prompts
    def ask(self, item: dict[str, Any], choices: tuple[str, ...] = ()) -> Any:
        """Show a question card and block the worker until a window answers it (or the run stops)."""
        prompt = Prompt(item["id"], item["kind"], choices)
        with self.lock:
            if self.stop_requested:
                raise KeyboardInterrupt
            self.prompts[prompt.id] = prompt
        self.bus.put(item | {"resolved": None})
        self._send_state()
        try:
            return prompt.wait()
        finally:
            with self.lock:
                self.prompts.pop(prompt.id, None)
            resolved = {"cancelled": True} if prompt.cancelled else dict(prompt.answer or {})
            self.bus.put(item | {"resolved": resolved})
            self._send_state()

    def respond(self, prompt_id: str, answer: Any) -> None:
        with self.lock:
            prompt = self.prompts.get(str(prompt_id))
        if prompt is None:
            raise ApiError(409, "That question is no longer waiting for an answer.")
        raw = answer.get("answer") if isinstance(answer, dict) else answer
        if prompt.kind == "approval":
            if raw not in prompt.choices:
                raise ApiError(400, "Invalid answer.")
            feedback = str(answer.get("feedback") or "")[:4000] if isinstance(answer, dict) else ""
            prompt.resolve({"answer": raw, "feedback": feedback})
        elif prompt.kind == "question":
            prompt.resolve({"answer": str(raw or "")[:20_000]})
        else:
            prompt.resolve({"answer": raw in (True, "yes", "true", 1)})

    # ------------------------------------------------------------------ running requests
    def send(self, text: str, attachments: list[dict[str, Any]] | None = None) -> None:
        text = str(text or "").strip()
        files = self._checked_uploads(attachments or [])
        if not text and not files:
            raise ApiError(400, "The message is empty.")
        runtime = self._claim()
        try:
            agent = runtime.agent
            assert agent is not None
            mentions = "\n\n".join(f'@"{f["path"]}"' for f in files)
            content, notes = expand_mentions(f"{text}\n\n{mentions}".strip(), runtime.ctx, bool(self.settings.vision))
            for note in notes:
                if note.startswith(("skipped", "could not")):
                    self.ui.warn(f"Attachment {note}")
            meta: dict[str, Any] = {"_display": text, "_attachments": [{"name": f["name"], "path": f["path"]}
                                                                         for f in files]}
            last = agent.messages[-1] if len(agent.messages) > 1 else {}
            if last.get("role") == "user" and last.get("_turn"):  # the agent merges an unanswered request
                meta["_display"] = f"{self._display_text(last)}\n\n{text}".strip()
                meta["_attachments"] = list(last.get("_attachments") or []) + meta["_attachments"]
            if not runtime.session.title:
                runtime.session.title = one_line(text or ", ".join(f["name"] for f in files), 70)
            self.bus.put({"type": "user", "id": self.bus.new_id("u"), "text": text,
                          "files": [{"name": f["name"], "image": self.uploads.get(f["path"])} for f in files]})
        except BaseException:
            self._release()
            raise
        self._launch(lambda: agent.run(content, meta), "Thinking…")

    def retry(self) -> None:
        """Run the last request again (after an error), reverting its file changes first."""
        runtime = self._claim()
        try:
            agent = runtime.agent
            assert agent is not None
            message = agent.pop_turn()
            if message is None:
                raise ApiError(400, "Nothing to retry.")
            self._revert_files(message)
            meta = {k: v for k, v in message.items() if k in ("_display", "_attachments")}
            self.bus.reset(self.messages_to_events(agent.messages))
            self.bus.put(self._user_item(message))
        except BaseException:
            self._release()
            raise
        self._launch(lambda: agent.run(message.get("content"), meta), "Thinking…")

    def compact(self, focus: str = "") -> None:
        runtime = self._claim()
        agent = runtime.agent
        assert agent is not None

        def job() -> None:
            if not agent.compact(str(focus or "")[:2000]):
                self.ui.info("Nothing to compact yet.")

        self._launch(job, "Compacting…")

    def _claim(self) -> Runtime:
        with self.lock:
            runtime = self._require_idle()
            self.running = True
            self.stop_requested = False
        return runtime

    def _release(self) -> None:
        with self.lock:
            self.running = False
        self._send_state()

    def _launch(self, job: Callable[[], Any], status: str) -> None:
        self.status = status
        self._send_state()
        self._worker = threading.Thread(target=self._work, args=(job,), name="kimi-agent-run", daemon=True)
        self._worker.start()

    def _work(self, job: Callable[[], Any]) -> None:
        ident = threading.get_ident()
        try:
            try:
                with self._interrupt_lock:
                    self._run_ident = ident
                job()
            except KeyboardInterrupt:
                self.ui._notice("stopped", "Stopped. Send a message to continue, or undo to remove the request.")
            except LLMError as exc:
                self.ui._notice("error", str(exc))
            except Exception as exc:
                log.exception("agent run failed")
                self.ui.error(f"Unexpected error: {exc!r} (details in {self.settings.data_path / 'logs'})")
            finally:
                while True:  # disarm: no interrupt may land after this point
                    try:
                        with self._interrupt_lock:
                            self._run_ident = None
                        _async_raise(ident, None)
                        break
                    except KeyboardInterrupt:
                        continue
                self._finish_run()
        except KeyboardInterrupt:  # a second stop request that arrived while handling the first
            log.debug("late interrupt ignored")

    def _finish_run(self) -> None:
        try:
            self.ui.end_response()
            stale, self.active_tools = list(self.active_tools.values()), {}
            for item in stale:
                self.bus.put(item | {"status": "stopped"})
            if self.runtime is not None:
                try:
                    self.runtime.save_session()
                except OSError as exc:
                    self.ui.warn(f"Could not save the conversation: {exc}")
            self._update_context()
        except Exception:
            log.exception("cleanup after a run failed")
        finally:
            with self.lock:
                self.running = False
                self.stop_requested = False
            self.status = ""
            self.bus.send({"type": "sessions"})
            self._send_state()

    def stop(self) -> bool:
        with self.lock:
            if not self.running:
                return False
            self.stop_requested = True
            prompts = list(self.prompts.values())
        self.set_status("Stopping…")
        for prompt in prompts:
            prompt.cancel()
        if not prompts:
            with self._interrupt_lock:
                if self._run_ident is not None:
                    _async_raise(self._run_ident, KeyboardInterrupt)
        return True

    # ------------------------------------------------------------------ conversations
    def _sessions_dir(self) -> Path:
        return self.runtime.sessions.directory if self.runtime else self.settings.data_path / "sessions"

    def list_sessions(self, limit: int = 300) -> list[dict[str, Any]]:
        entries = []
        try:
            for path in self._sessions_dir().glob("*.json"):
                try:
                    st = path.stat()
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, path))
        except OSError:
            return []
        entries.sort(key=lambda e: e[0], reverse=True)
        out = []
        for mtime, size, path in entries[:limit]:
            key = (mtime, size)
            cached = self._session_cache.get(path.name)
            if cached is None or cached[0] != key:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                summary = {"id": str(data.get("id") or path.stem), "title": str(data.get("title") or "Untitled"),
                           "updated": str(data.get("updated") or ""), "cwd": str(data.get("cwd") or "")}
                cached = (key, summary)
                self._session_cache[path.name] = cached
            out.append(cached[1])
        return out

    @staticmethod
    def _check_id(session_id: Any) -> str:
        session_id = str(session_id or "")
        if not SESSION_ID_RE.fullmatch(session_id) or session_id.startswith("."):
            raise ApiError(400, "Invalid conversation id.")
        return session_id

    def new_chat(self) -> None:
        runtime = self._require_idle()
        runtime.new_session()
        self._update_context()
        self.bus.reset([])
        self.bus.send({"type": "sessions"})

    def open_session(self, session_id: Any) -> None:
        runtime = self._require_idle()
        session_id = self._check_id(session_id)
        if session_id == runtime.session.id:
            return
        try:
            session = runtime.sessions.load(session_id)
        except (OSError, ValueError) as exc:
            raise ApiError(404, f"Could not open the conversation: {exc}") from exc
        missing = ""
        if session.cwd and session.cwd != str(runtime.ctx.cwd):
            if Path(session.cwd).is_dir():
                runtime.change_directory(session.cwd)
                self._remember_folder(str(runtime.ctx.cwd))
            else:
                missing = session.cwd
        runtime.resume(session.id)
        self._update_context()
        self.bus.reset(self.messages_to_events(runtime.agent.messages if runtime.agent else []))
        if missing:
            self.ui.warn(f"This conversation was started in {missing}, which no longer exists. "
                         f"Working in {runtime.ctx.cwd} instead.")
        self.bus.send({"type": "sessions"})

    def delete_session(self, session_id: Any) -> None:
        session_id = self._check_id(session_id)
        runtime = self.runtime
        if runtime is not None and session_id == runtime.session.id:
            runtime = self._require_idle()
            runtime.sessions.delete(session_id)
            agent = runtime.agent
            assert agent is not None
            agent.reset()
            agent.usage = type(agent.usage)()
            runtime.session = runtime._new_session()
            self._update_context()
            self.bus.reset([])
        else:
            (self._sessions_dir() / f"{session_id}.json").unlink(missing_ok=True)
        self.bus.send({"type": "sessions"})

    def rename_session(self, session_id: Any, title: Any) -> None:
        session_id = self._check_id(session_id)
        title = one_line(str(title or ""), 120)
        if not title:
            raise ApiError(400, "The title is empty.")
        runtime = self.runtime
        if runtime is not None and session_id == runtime.session.id:
            runtime.session.title = title
            if not self.running:
                runtime.save_session()
            self._send_state()
        else:
            path = self._sessions_dir() / f"{session_id}.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                data["title"] = title
                _write_private(path, json.dumps(data, ensure_ascii=False, indent=1))
            except (OSError, ValueError) as exc:
                raise ApiError(404, f"Could not rename the conversation: {exc}") from exc
        self.bus.send({"type": "sessions"})

    def export_session(self, session_id: Any) -> tuple[str, str]:
        runtime = self.runtime
        if runtime is None or runtime.agent is None:
            raise ApiError(409, "Kimi Agent is still starting.")
        if session_id in (None, "", "current") or session_id == runtime.session.id:
            title, messages = runtime.session.title or "Conversation", runtime.agent.messages
        else:
            try:
                session = runtime.sessions.load(self._check_id(session_id))
            except (OSError, ValueError) as exc:
                raise ApiError(404, str(exc)) from exc
            title, messages = session.title or "Conversation", session.messages
        name = re.sub(r"[^\w\- ]+", "", title).strip()[:60] or "conversation"
        return f"{name}.md", export_markdown(messages, title)

    def undo(self) -> dict[str, Any]:
        runtime = self._require_idle()
        agent = runtime.agent
        assert agent is not None
        message = agent.pop_turn()
        if message is None:
            raise ApiError(400, "Nothing to undo.")
        restored = self._revert_files(message)
        if any(m.get("role") == "user" for m in agent.messages):
            runtime.save_session()
        else:
            runtime.sessions.delete(runtime.session.id)
            runtime.session.title = ""
        self._update_context()
        self.bus.reset(self.messages_to_events(agent.messages))
        self.bus.send({"type": "sessions"})
        files = [{"name": a.get("name", ""), "path": a.get("path", ""), "image": self.uploads.get(a.get("path", ""))}
                 for a in message.get("_attachments") or [] if Path(str(a.get("path", ""))).is_file()]
        return {"text": self._display_text(message), "files": files, "restored": restored}

    def _revert_files(self, message: dict[str, Any]) -> list[str]:
        turn = message.get("_file_turn")
        history = self.runtime.ctx.file_history if self.runtime else None
        if turn is None or history is None:
            return []
        return history.undo_turn(int(turn))

    # ------------------------------------------------------------------ folders and files
    def set_workdir(self, path: Any) -> None:
        runtime = self._require_idle()
        try:
            target = runtime.change_directory(str(path or ""))
        except (OSError, ValueError) as exc:
            raise ApiError(400, str(exc)) from exc
        self.cli_overrides.pop("workdir", None)
        self._remember_folder(str(target))
        self._update_context()
        self._send_state()

    def _remember_folder(self, path: str) -> None:
        recent = [p for p in self.prefs.get("recent_folders") or [] if p != path and isinstance(p, str)]
        self.prefs["recent_folders"] = [path, *recent][:8]
        self.prefs["workdir"] = path
        self._save_prefs()

    def browse(self, path: Any = None) -> dict[str, Any]:
        base = Path(str(path)).expanduser() if path else (self.runtime.ctx.cwd if self.runtime else Path.home())
        try:
            base = base.resolve()
        except OSError as exc:
            raise ApiError(400, str(exc)) from exc
        if not base.is_dir():
            raise ApiError(404, f"Not a folder: {base}")
        dirs: list[str] = []
        error = ""
        try:
            with os.scandir(base) as entries:
                for entry in entries:
                    try:
                        if not entry.name.startswith(".") and entry.is_dir():
                            dirs.append(entry.name)
                    except OSError:
                        continue
        except OSError as exc:
            error = exc.strerror or str(exc)
        dirs.sort(key=str.lower)
        return {"path": str(base), "parent": str(base.parent) if base.parent != base else None,
                "dirs": [{"name": d, "path": str(base / d)} for d in dirs[:2000]], "error": error,
                "home": str(Path.home()), "places": _known_folders(), "roots": _drives(), "sep": os.sep}

    def make_folder(self, parent: Any, name: Any) -> dict[str, Any]:
        name = str(name or "").strip()
        if not name or name in (".", "..") or any(c in name for c in '/\\:*?"<>|') or len(name) > 200:
            raise ApiError(400, "Invalid folder name.")
        target = Path(str(parent or "")).expanduser() / name
        try:
            target.mkdir(parents=False, exist_ok=False)
        except FileExistsError as exc:
            raise ApiError(409, f"{target} already exists.") from exc
        except OSError as exc:
            raise ApiError(400, f"Could not create {target}: {exc.strerror or exc}") from exc
        return {"path": str(target.resolve())}

    def upload(self, name: str, data: bytes) -> dict[str, Any]:
        if len(data) > MAX_UPLOAD_BYTES:
            raise ApiError(413, "Files larger than 50 MB cannot be attached.")
        display = Path(str(name or "file").replace("\\", "/")).name[:200] or "file"
        safe = re.sub(r"[^\w.\-() ]+", "_", display).strip(" .")[-120:] or "file"
        folder = self.settings.data_path / "uploads" / dt.date.today().isoformat()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{secrets.token_hex(4)}-{safe}"
            path.write_bytes(data)
        except OSError as exc:
            raise ApiError(500, f"Could not save the file: {exc}") from exc
        mime = IMAGE_TYPES.get(path.suffix.lower())
        url = self.images.add(data, mime) if mime and len(data) <= MAX_PREVIEW_IMAGE_BYTES else None
        self.uploads[str(path)] = url
        return {"path": str(path), "name": display, "size": len(data), "image": url}

    def _checked_uploads(self, attachments: list[dict[str, Any]]) -> list[dict[str, str]]:
        root = (self.settings.data_path / "uploads").resolve()
        files = []
        for item in attachments[:20]:
            raw = str(item.get("path", "")) if isinstance(item, dict) else ""
            try:
                path = Path(raw).resolve()
                path.relative_to(root)
            except (OSError, ValueError) as exc:
                raise ApiError(400, "Unknown attachment.") from exc
            if not path.is_file():
                raise ApiError(400, f"The attachment {path.name} no longer exists.")
            files.append({"path": str(path), "name": str(item.get("name") or path.name)[:200]})
        return files

    def models(self) -> dict[str, Any]:
        runtime = self.runtime
        if runtime is None or not self.ready:
            return {"models": [], "error": "Kimi Agent is still starting."}
        try:
            client = runtime.llm.client.with_options(timeout=15, max_retries=0)
            return {"models": sorted({m.id for m in client.models.list()})[:1000]}
        except Exception as exc:
            return {"models": [], "error": one_line(str(exc), 200)}

    # ------------------------------------------------------------------ transcript replay
    @staticmethod
    def _display_text(message: dict[str, Any]) -> str:
        if isinstance(message.get("_display"), str):
            return message["_display"]
        return ATTACHMENT_RE.sub("", content_text(message.get("content"))).strip()

    def _image_urls(self, content: Any) -> list[str]:
        urls = []
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = self.images.add_data_url(str((part.get("image_url") or {}).get("url", "")))
                if url:
                    urls.append(url)
        return urls

    def _user_item(self, message: dict[str, Any]) -> dict[str, Any]:
        files = []
        attachments = message.get("_attachments")
        for att in attachments or []:
            path = str(att.get("path", ""))
            url = self.uploads.get(path)
            mime = IMAGE_TYPES.get(Path(path).suffix.lower())
            if url is None and mime and Path(path).is_file():
                try:
                    if Path(path).stat().st_size <= MAX_PREVIEW_IMAGE_BYTES:
                        url = self.images.add(Path(path).read_bytes(), mime)
                        self.uploads[path] = url
                except OSError:
                    url = None
            files.append({"name": str(att.get("name", "")), "image": url})
        if attachments is None:
            files = [{"name": "image", "image": url} for url in self._image_urls(message.get("content"))]
        return {"type": "user", "id": self.bus.new_id("u"), "text": self._display_text(message), "files": files}

    def messages_to_events(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rebuild transcript items from a conversation (saved sessions, undo)."""
        items: list[dict[str, Any]] = []
        calls: dict[str, dict[str, Any]] = {}
        last_tool: dict[str, Any] | None = None
        registry = self.runtime.registry if self.runtime else None
        new_id = self.bus.new_id
        unanswered = False
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role == "user":
                text = content_text(content)
                if message.get("_summary"):
                    items.append({"type": "notice", "id": new_id("n"), "level": "info",
                                  "text": "Earlier messages were summarized to fit the context window."})
                    continue
                if text.startswith(TOOL_IMAGES_NOTE):
                    urls = self._image_urls(content)
                    if last_tool is not None and urls:
                        last_tool["images"] = (last_tool.get("images") or []) + urls[:4]
                if message.get("_turn"):
                    if unanswered:
                        items.append(_no_reply_notice(new_id("n")))
                    items.append(self._user_item(message))
                    unanswered = True
                continue
            if role == "assistant":
                unanswered = False
                reasoning = message.get("reasoning_content") or message.get("reasoning")
                if isinstance(reasoning, str) and reasoning.strip():
                    items.append({"type": "reasoning", "id": new_id("r"), "text": reasoning, "done": True,
                                  "seconds": None})
                text = content_text(content)
                if text.strip():
                    items.append({"type": "assistant", "id": new_id("a"), "html": render_markdown(text),
                                  "text": text, "streaming": False})
                for call in message.get("tool_calls") or []:
                    item = self._replay_tool(call, registry)
                    calls[str(call.get("id"))] = item
                    items.append(item)
                    last_tool = item
                continue
            if role == "tool":
                item = calls.get(str(message.get("tool_call_id")))
                if item is None:
                    continue
                output = content_text(content)
                status = _tool_status(output)
                item.update(output=clip(output, MAX_OUTPUT_CHARS), status=status)
                if status in ("error", "denied"):
                    item["hidden"] = False
                if item["name"] == "ask_user" and isinstance(item.get("_args"), dict) and status == "done":
                    answer = output.removeprefix("The user answered: ")
                    items.append({"type": "prompt", "id": new_id("p"), "kind": "question",
                                  "question": render_markdown(str(item["_args"].get("question", ""))),
                                  "choices": [], "resolved": {"answer": answer}})
                if item["name"] == "update_plan" and isinstance(item.get("_args"), dict) and status == "done":
                    try:
                        steps = normalize_plan(item["_args"].get("steps") or [])
                    except ToolError:
                        steps = []
                    items = [i for i in items if i.get("id") != "plan"]
                    items.append({"type": "plan", "id": "plan", "explanation": str(item["_args"].get("explanation") or ""),
                                  "steps": steps})
        if unanswered:
            items.append(_no_reply_notice(new_id("n")))
        for item in items:
            item.pop("_args", None)
        return items

    def _replay_tool(self, call: dict[str, Any], registry: Any) -> dict[str, Any]:
        fn = call.get("function") or {}
        name = str(fn.get("name") or "tool")
        raw = str(fn.get("arguments") or "")
        try:
            args = json.loads(raw) if raw.strip() else {}
        except ValueError:
            args = None
        tool = registry.get(name) if registry is not None else None
        if tool is not None and isinstance(args, dict):
            try:
                summary = tool.summary(args)
            except Exception:
                summary = one_line(raw, 200)
        else:
            summary = one_line(raw, 200)
        pretty = json.dumps(args, indent=2, ensure_ascii=False) if isinstance(args, dict) else raw
        item: dict[str, Any] = {"type": "tool", "id": self.bus.new_id("t"), "name": name,
                                "category": getattr(tool, "category", ""), "summary": clip(summary, 400),
                                "args": clip(pretty, MAX_ARGS_CHARS), "status": "stopped", "output": "",
                                "hidden": name in HIDDEN_TOOLS, "_args": args}
        if name == "run_command" and isinstance(args, dict):
            item["command"] = clip(str(args.get("command", "")), MAX_ARGS_CHARS)
        return item


def _no_reply_notice(item_id: str) -> dict[str, Any]:
    return {"type": "notice", "id": item_id, "level": "info", "text": "This message didn't get a reply."}


def _known_folders() -> list[dict[str, str]]:
    """Desktop, Documents and Downloads, where they exist (Windows may keep them in OneDrive)."""
    home = Path.home()
    found = []
    for name in ("Desktop", "Documents", "Downloads"):
        for candidate in (home / name, home / "OneDrive" / name):
            if candidate.is_dir():
                found.append({"name": name, "path": str(candidate)})
                break
    return found


def _drives() -> list[str]:
    if os.name != "nt":
        return []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
    except Exception:
        return []
    return [f"{chr(65 + i)}:\\" for i in range(26) if mask & (1 << i)]

"""Runtime: builds and owns the components of a session (model client, tools, approvals, MCP, persistence)."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .agent import Agent
from .config import ConfigError, finalize, missing_api_key_message
from .llm import LLMClient
from .mcp import MCPManager
from .plugins import load_plugins
from .prompts import build_system_prompt
from .safety import ApprovalPolicy
from .session import Session, SessionStore, new_session_id
from .tools import ToolRegistry, build_registry
from .tools.base import Tool, ToolContext
from .tools.files import FileHistory
from .tools.memory import MemoryStore
from .tools.python_exec import PythonSession
from .tools.shell import ProcessManager

log = logging.getLogger("kimi_agent.runtime")


class Runtime:
    def __init__(self, settings: Any, ui: Any, *, interactive: bool, use_mcp: bool = True,
                 tools: Iterable[Tool] = ()) -> None:
        self.settings = settings
        self.custom_tools: list[Tool] = list(tools)
        self._plugins_loaded = False
        self.ui = ui
        self.interactive = interactive
        self.llm = LLMClient(settings)
        self.memory = MemoryStore(settings.data_path / "memory.md") if settings.memory else None
        self.processes = ProcessManager()
        self.python = PythonSession(settings.python or None)
        self.file_history = FileHistory()
        self.sessions = SessionStore(settings.data_path / "sessions")
        self.policy = ApprovalPolicy(settings)
        self.mcp = (MCPManager(settings.mcp_servers, Path(settings.workdir), settings.data_path)
                    if use_mcp and settings.mcp_servers else None)
        root = Path(settings.workdir)
        self.ctx = ToolContext(settings=settings, cwd=root, root=root, ui=ui, interactive=interactive,
                               file_history=self.file_history, processes=self.processes, python=self.python,
                               memory=self.memory)
        self.registry = ToolRegistry()
        self.agent: Agent | None = None
        self.session = self._new_session()

    # -- lifecycle
    def __enter__(self) -> Runtime:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> Agent:
        if self.settings.plugins and not self._plugins_loaded:
            self._plugins_loaded = True
            found, errors = load_plugins(self.settings.plugins, [Path(self.settings.workdir), self.settings.data_path])
            self.custom_tools += found
            for error in errors:
                self.ui.warn(error)
        if self.mcp is not None:
            with self.ui.spinner("Starting MCP servers…"):
                errors = self.mcp.start()
            for error in errors:
                self.ui.warn(error)
        self.registry = self._build_registry()
        self.agent = Agent(llm=self.llm, registry=self.registry, ctx=self.ctx, policy=self.policy, ui=self.ui,
                           settings=self.settings, make_prompt=self.make_prompt, before_step=self._before_step)
        return self.agent

    def _before_step(self) -> None:
        if self.mcp is not None and self.mcp.refresh_changed():
            self.rebuild_tools()
            self.ui.info("An MCP server changed its tools; the tool list was refreshed.")

    def _build_registry(self) -> ToolRegistry:
        registry, notes = build_registry(self.settings, [*self.custom_tools, *(self.mcp.tools() if self.mcp else [])])
        for note in notes:
            self.ui.warn(note)
        return registry

    def rebuild_tools(self) -> None:
        self.registry = self._build_registry()
        if self.agent is not None:
            self.agent.registry = self.registry
            self.ctx.spawn_subagent = self.agent._spawn_subagent if "delegate_task" in self.registry else None
            self.agent.refresh_system_prompt()

    def make_prompt(self, ctx: ToolContext, registry: ToolRegistry, subagent: bool) -> str:
        memory_text = self.memory.render() if self.memory is not None else ""
        extra = self.mcp.instructions() if self.mcp is not None else ""
        return build_system_prompt(self.settings, ctx.cwd, registry.names(), memory_text=memory_text,
                                   subagent=subagent, extra=extra)

    def close(self) -> None:
        try:
            self.save_session()
        except OSError as exc:
            log.warning("could not save session: %s", exc)
        self.processes.stop_all()
        self.python.close()
        if self.mcp is not None:
            self.mcp.close()

    # -- sessions
    def _new_session(self) -> Session:
        return Session(id=new_session_id(), cwd=str(self.ctx.cwd) if hasattr(self, "ctx") else self.settings.workdir,
                       provider=self.settings.provider, model=self.settings.model)

    def save_session(self) -> Path | None:
        if self.agent is None:
            return None
        messages = [m for m in self.agent.messages if m.get("role") != "system"]
        if not any(m.get("role") == "user" for m in messages):
            return None
        self.session.messages = messages
        self.session.plan = list(self.ctx.plan)
        self.session.provider = self.settings.provider
        self.session.model = self.settings.model
        self.session.cwd = str(self.ctx.cwd)
        self.session.usage = dataclasses.asdict(self.agent.usage)
        return self.sessions.save(self.session)

    def new_session(self) -> None:
        self.save_session()
        assert self.agent is not None
        self.agent.reset()
        self.agent.usage = type(self.agent.usage)()
        self.session = self._new_session()

    def resume(self, session_id: str | None = None) -> Session:
        """Load a saved session (latest for this directory when no id is given)."""
        assert self.agent is not None
        if session_id:
            session = self.sessions.load(session_id)
        else:
            session = self.sessions.latest(cwd=str(self.ctx.cwd)) or self.sessions.latest()
            if session is None:
                raise FileNotFoundError("No saved sessions yet.")
        self.save_session()
        self.session = session
        self.agent.load_messages(session.messages)
        self.ctx.plan[:] = session.plan or []
        usage = type(self.agent.usage)()
        for key, value in (session.usage or {}).items():
            if hasattr(usage, key):
                setattr(usage, key, int(value))
        self.agent.usage = usage
        if session.cwd and session.cwd != str(self.ctx.cwd):
            self.ui.warn(f"This session was started in {session.cwd}; the working directory is {self.ctx.cwd}.")
        return session

    # -- switching
    def change_directory(self, path: str) -> Path:
        target = self.ctx.resolve(path)
        if not target.is_dir():
            raise NotADirectoryError(f"Not a directory: {target}")
        target = target.resolve()
        self.ctx.cwd = target
        self.ctx.root = target
        self.settings.workdir = str(target)
        if self.agent is not None:
            self.agent.refresh_system_prompt()
        return target

    def set_model(self, model: str) -> None:
        self.settings.model = model
        self.llm.reconfigure()
        if self.agent is not None:
            self.agent.refresh_system_prompt()

    def set_provider(self, name: str, model: str | None = None) -> None:
        saved = {f: getattr(self.settings, f) for f in ("provider", "base_url", "model", "api_key",
                                                        "context_window", "vision")}
        self.settings.provider = name
        self.settings.base_url = ""
        self.settings.model = model or ""
        self.settings.api_key = ""
        self.settings.context_window = 0
        self.settings.vision = None
        try:
            finalize(self.settings)
        except ConfigError:
            for key, value in saved.items():
                setattr(self.settings, key, value)
            raise
        message = missing_api_key_message(self.settings)
        if message:
            self.ui.warn(message)
        self.llm.reconfigure()
        if self.agent is not None:
            self.agent.context.window_limit = 0
            self.rebuild_tools()

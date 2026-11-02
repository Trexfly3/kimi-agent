from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent))

from kimi_agent.config import Settings, finalize
from kimi_agent.tools.base import ToolContext
from kimi_agent.tools.files import FileHistory
from kimi_agent.tools.shell import ProcessManager
from kimi_agent.ui import UI

ENV_PREFIXES = ("KIMI_", "MOONSHOT_", "OPENAI_", "OPENROUTER_", "TAVILY_", "BRAVE_", "SEARXNG_")


class FakeUI(UI):
    """Records output in memory and answers prompts from a script."""

    def __init__(self, answers: list[Any] | None = None) -> None:
        super().__init__(Console(file=io.StringIO(), width=160, force_terminal=False, color_system=None),
                         stream=False)
        self.answers = list(answers or [])
        self.confirmations: list[tuple[str, str, str]] = []
        self.questions: list[str] = []
        self.plans: list[list[dict[str, Any]]] = []

    def _answer(self, default: Any) -> Any:
        return self.answers.pop(0) if self.answers else default

    def confirm_tool(self, tool: Any, args: dict[str, Any], decision: Any, preview: Any) -> tuple[str, str]:
        self.confirmations.append((tool.name, decision.action, decision.risk))
        answer = self._answer(("no", ""))
        return answer if isinstance(answer, tuple) else (answer, "")

    def ask_user(self, question: str, choices: list[str]) -> str:
        self.questions.append(question)
        return str(self._answer(""))

    def confirm(self, question: str, default: bool = False) -> bool:
        self.questions.append(question)
        return bool(self._answer(default))

    def show_plan(self, plan: list[dict[str, Any]], explanation: str = "") -> None:
        self.plans.append(list(plan))
        super().show_plan(plan, explanation)

    def child(self, prefix: str) -> UI:
        return self

    @property
    def output(self) -> str:
        return self.console.file.getvalue()  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in list(os.environ):
        if key.startswith(ENV_PREFIXES) or key in ("HF_TOKEN", "GROQ_API_KEY", "GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("KIMI_AGENT_HOME", str(home))
    return home


@pytest.fixture
def home(clean_env: Path) -> Path:
    return clean_env


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    monkeypatch.chdir(path)
    return path.resolve()


@pytest.fixture
def settings(home: Path, workdir: Path) -> Settings:
    s = Settings(provider="custom", base_url="http://127.0.0.1:9/v1", model="fake-model", api_key="test-key",
                 workdir=str(workdir), data_dir=str(home), max_retries=0, web=False, request_timeout=20)
    finalize(s)
    return s


@pytest.fixture
def ui() -> FakeUI:
    return FakeUI()


@pytest.fixture
def ctx(settings: Settings, workdir: Path, ui: FakeUI) -> ToolContext:
    context = ToolContext(settings=settings, cwd=workdir, root=workdir, ui=ui, interactive=False,
                          file_history=FileHistory(), processes=ProcessManager())
    yield context
    context.processes.stop_all()

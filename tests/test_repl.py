from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from fakeserver import FakeOpenAI, tool_call
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from kimi_agent.llm import LLMError
from kimi_agent.repl import EXIT, Repl, ReplCompleter, resolve_command
from kimi_agent.runtime import Runtime


@pytest.fixture
def env(settings, ui, workdir):
    with FakeOpenAI() as fake:
        settings.base_url = fake.base_url
        runtime = Runtime(settings, ui, interactive=True, use_mcp=False)
        runtime.llm._sleep = lambda attempt, exc: None
        runtime.start()
        try:
            yield SimpleNamespace(repl=Repl(runtime), fake=fake, ui=ui, runtime=runtime, settings=settings)
        finally:
            runtime.close()


def complete(repl, text):
    return [c.text for c in ReplCompleter(repl).get_completions(Document(text), CompleteEvent())]


def test_resolve_command():
    assert resolve_command("help")[0].name == "help" and resolve_command("?")[0].name == "help"
    assert resolve_command("q")[0].name == "exit" and resolve_command("COMP")[0].name == "compact"
    assert resolve_command("mod") == (None, ["model", "models"])
    assert resolve_command("nonsense") == (None, [])


def test_informational_commands(env):
    repl, ui = env.repl, env.ui
    assert repl.handle("/exit") is EXIT and repl.handle("/q") is EXIT and repl.handle("   ") is None
    for line in ("/help", "/tools", "/zzz", "/mod x", "/plan", "/mcp", "/ps", "/compact", "/undo", "/export",
                 "/system", "/tokens", "/sessions", "/config", "/copy"):
        repl.handle(line)
    out = ui.output
    for expected in ("/model [name]", "Esc+Enter", "read_file", "Unknown command /zzz",
                     "Ambiguous command /mod: /model, /models", "No plan yet", "No MCP servers",
                     "No background processes", "Nothing to compact yet", "Nothing to undo",
                     "Nothing to export yet", "System prompt", "compacts at 80%", "No saved sessions yet",
                     "fake-model", "Nothing to copy yet"):
        assert expected in out, expected
    assert "test-key" not in out  # the API key is masked by /config
    assert env.fake.requests == []


def test_settings_commands(env):
    repl, ui, settings = env.repl, env.ui, env.settings
    repl.handle("/approval yolo")
    assert settings.approval == "yolo" and "without asking" in ui.output
    repl.handle("/approval sometimes")
    assert "Unknown approval mode" in ui.output and settings.approval == "yolo"
    repl.handle("/approval")
    assert "approval mode: yolo" in ui.output
    repl.handle("/model other-model")
    assert settings.model == "other-model" and "other-model" in repl.agent.messages[0]["content"]
    repl.handle("/effort high")
    assert settings.reasoning_effort == "high"
    repl.handle("/effort off")
    assert settings.reasoning_effort is None
    repl.handle("/reasoning on")
    assert settings.show_reasoning and ui.show_reasoning
    repl.handle("/reasoning")
    assert not settings.show_reasoning
    repl.handle("/models")
    assert repl.models == ["fake-model", "fake-model-large"] and "fake-model-large" in ui.output
    repl.handle("/provider")
    assert "openrouter" in ui.output
    repl.handle("/provider nope")
    assert "Unknown provider 'nope'" in ui.output
    repl.handle("/provider ollama llama3.3")
    assert (settings.provider, settings.model, settings.base_url) == ("ollama", "llama3.3",
                                                                      "http://localhost:11434/v1")
    repl.handle("/provider custom")  # needs a base URL: refused and the previous settings are kept
    assert "/provider: No base URL configured" in ui.output
    assert (settings.provider, settings.model) == ("ollama", "llama3.3")


def test_memory_and_cd(env, workdir):
    repl, ui = env.repl, env.ui
    repl.handle("/memory add Prefers tabs over spaces")
    assert "Prefers tabs over spaces" in repl.agent.messages[0]["content"]
    repl.handle("/memory add Prefers tabs over spaces")
    assert "Already remembered" in ui.output
    repl.handle("/memory")
    assert "1. Prefers tabs over spaces" in ui.output
    repl.handle("/memory forget tabs")
    assert "Forgot: Prefers tabs over spaces" in ui.output
    assert "Prefers tabs" not in repl.agent.messages[0]["content"]
    repl.handle("/memory add something")
    ui.answers.append(True)
    repl.handle("/memory clear")
    assert env.runtime.memory.facts() == []
    repl.handle("/memory bogus")
    assert "Usage: /memory" in ui.output
    (workdir / "sub").mkdir()
    repl.handle("/cd sub")
    assert env.runtime.ctx.cwd == workdir / "sub"
    assert f"Working directory: {workdir / 'sub'}" in repl.agent.messages[0]["content"]
    repl.handle("/cd missing-dir")
    assert "Not a directory" in ui.output


def test_conversation_shell_export_retry_undo(env, workdir, capsys):
    repl, ui, fake = env.repl, env.ui, env.fake
    repl.handle("!echo hi from shell")
    assert len(repl.pending_shell) == 1 and "hi from shell" in ui.output
    fake.replies += [tool_call("write_file", path="notes.md", content="# Notes\n"), {"content": "Wrote notes.md."}]
    repl.handle("Summarize the output into notes.md")
    sent = fake.requests[0]["messages"][-1]["content"]
    assert sent.startswith("<shell_command>\n$ echo hi from shell\nhi from shell\n(exit code 0)\n</shell_command>")
    assert sent.endswith("Summarize the output into notes.md") and repl.pending_shell == []
    assert repl.last_answer == "Wrote notes.md." and (workdir / "notes.md").exists()
    assert env.runtime.sessions.path(env.runtime.session.id).exists()  # saved after every request

    repl.handle("/copy")
    assert base64.b64encode(b"Wrote notes.md.").decode() in capsys.readouterr().out

    repl.handle("/export out.md")
    exported = (workdir / "out.md").read_text()
    assert exported.startswith("# Summarize") and "Wrote notes.md." in exported
    assert "**Tool call:** `write_file`" in exported

    fake.replies.append({"content": "Retried answer."})
    repl.handle("/retry")
    assert not (workdir / "notes.md").exists()  # the retried request's file change was reverted first
    assert repl.last_answer == "Retried answer."
    assert fake.requests[-1]["messages"][-1]["content"] == sent

    repl.handle("/undo")
    assert [m["role"] for m in repl.agent.messages] == ["system"] and repl.last_answer == ""


def test_absolute_path_is_a_message(env):
    env.fake.replies.append({"content": "It is the hosts file."})
    env.repl.handle("/etc/hosts what is this file?")
    assert env.fake.requests[0]["messages"][-1]["content"] == "/etc/hosts what is this file?"


def test_mentions_are_attached(env, workdir):
    (workdir / "data.csv").write_text("a,b\n1,2\n")
    env.fake.replies.append({"content": "Two columns."})
    env.repl.handle("What is in @data.csv?")
    assert '<file path="data.csv">\na,b\n1,2\n</file>' in env.fake.requests[0]["messages"][-1]["content"]
    assert "attached data.csv" in env.ui.output


def test_sessions_new_and_resume(env):
    repl, ui, fake = env.repl, env.ui, env.fake
    fake.replies += [{"content": "First answer."}, {"content": "Second answer."}]
    repl.handle("first question")
    first_id = env.runtime.session.id
    repl.handle("/new")
    assert env.runtime.session.id != first_id and len(repl.agent.messages) == 1
    repl.handle("second question")
    repl.handle("/sessions")
    assert "first question" in ui.output and "second question" in ui.output
    repl.handle(f"/resume {first_id}")
    assert repl.last_answer == "First answer." and "Resumed session" in ui.output
    assert [m["content"] for m in repl.agent.messages[1:]] == ["first question", "First answer."]
    ui.answers.append("")  # cancel the picker
    repl.handle("/resume")
    assert env.runtime.session.id == first_id
    repl.handle("/resume nope")
    assert "No session matching 'nope'" in ui.output


def test_agent_errors_are_reported(env, monkeypatch):
    def fail(content):
        raise LLMError("rate limited by provider")

    def interrupt(content):
        raise KeyboardInterrupt
    monkeypatch.setattr(env.runtime.agent, "run", fail)
    env.repl.handle("hello")
    monkeypatch.setattr(env.runtime.agent, "run", interrupt)
    env.repl.handle("hello")
    assert "rate limited by provider" in env.ui.output and "Interrupted. Send a message" in env.ui.output


def test_completions(env, workdir):
    repl = env.repl
    assert complete(repl, "/mod") == ["/model", "/models"]
    assert complete(repl, "/qu") == ["/quit"]
    assert complete(repl, "/approval y") == ["yolo"] and complete(repl, "/effort h") == ["high"]
    (workdir / "src" / "deep").mkdir(parents=True)
    (workdir / "src" / "deep" / "target_file.py").write_text("")
    (workdir / "notes file.txt").write_text("")
    (workdir / "node_modules").mkdir()
    assert complete(repl, "/cd s") == ["src/"] and complete(repl, "/cd ") == ["src/"]
    assert complete(repl, "look at @src/de") == ["deep/"]
    assert complete(repl, "look at @notes") == ['"notes file.txt"']
    assert complete(repl, "look at @target") == ["src/deep/target_file.py"]
    assert complete(repl, "no mention here") == []


def test_run_loop(env, monkeypatch, capsys):
    repl, ui = env.repl, env.ui
    env.fake.replies.append({"content": "Hello there!"})
    actions = iter(["", "/approval ask", KeyboardInterrupt, "/tokens", KeyboardInterrupt, KeyboardInterrupt])

    def read():
        action = next(actions)
        if action is KeyboardInterrupt:
            raise KeyboardInterrupt
        return action
    monkeypatch.setattr(repl, "read", read)
    assert repl.run(initial="hi") == 0
    out = ui.output
    assert "Hello there!" in capsys.readouterr().out  # the UI is not streaming, so the answer goes to stdout
    assert "Kimi Agent" in out and env.settings.approval == "ask"
    assert "Press Ctrl+C again" in out and f"kimi-agent -r {env.runtime.session.id}" in out
    assert any("fake-model" in text for _, text in repl._toolbar())

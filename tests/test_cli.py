from __future__ import annotations

import io
import json
import logging
import sys

import pytest
from fakeserver import FakeOpenAI, tool_call

from kimi_agent import __version__
from kimi_agent.cli import build_parser, main, overrides_from_args, read_stdin_prompt
from kimi_agent.repl import Repl


class TTYStdin(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")  # keep rich from wrapping messages
    logger = logging.getLogger("kimi_agent")
    saved = logger.handlers[:], logger.level, logger.propagate
    yield
    for handler in logger.handlers:
        if handler not in saved[0]:
            handler.close()
    logger.handlers[:] = saved[0]
    logger.setLevel(saved[1])
    logger.propagate = saved[2]


@pytest.fixture
def stdin(monkeypatch):
    def set_stdin(text: str = "", tty: bool = False) -> None:
        monkeypatch.setattr(sys, "stdin", (TTYStdin if tty else io.StringIO)(text))
    set_stdin()  # an empty pipe by default
    return set_stdin


def run(fake, *args):
    return main(["-p", "custom", "--base-url", fake.base_url, "-m", "fake-model", "--no-mcp", *args])


def json_events(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_parser_and_overrides():
    args = build_parser().parse_args(["-y", "--no-stream", "--computer", "--no-web", "--no-memory", "--show-reasoning",
                                      "--debug", "--vision", "-m", "m1", "--max-steps", "5", "-C", "dir", "do", "it"])
    o = overrides_from_args(args)
    assert o["approval"] == "yolo" and o["stream"] is False and o["computer_use"] and o["web"] is False
    assert o["memory"] is False and o["show_reasoning"] and o["debug"] and o["vision"] is True
    assert (o["model"], o["max_steps"], o["workdir"], args.prompt) == ("m1", 5, "dir", ["do", "it"])
    o = overrides_from_args(build_parser().parse_args(["-a", "ask", "--no-vision"]))
    assert o["approval"] == "ask" and o["vision"] is False and "stream" not in o and o["provider"] is None


def test_read_stdin_prompt(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("  \n"))
    assert read_stdin_prompt("task") == "task"
    monkeypatch.setattr(sys, "stdin", io.StringIO("log line\n"))
    assert read_stdin_prompt("explain") == "explain\n\n<stdin>\nlog line\n</stdin>"
    monkeypatch.setattr(sys, "stdin", io.StringIO("just a task\n"))
    assert read_stdin_prompt("") == "just a task\n"


def test_informational_options(home, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0 and __version__ in capsys.readouterr().out
    assert main(["--list-providers"]) == 0
    out = capsys.readouterr().out
    assert all(name in out for name in ("kimi", "openrouter", "ollama", "MOONSHOT_API_KEY", "(--base-url)"))
    assert main(["--init-config"]) == 0 and "[mcp_servers.playwright]" in (home / "config.toml").read_text()
    assert main(["--init-config"]) == 1 and "already exists" in capsys.readouterr().err
    assert main(["--list-sessions"]) == 0 and "No saved sessions." in capsys.readouterr().out


def test_usage_errors(stdin, capsys, workdir):
    assert main(["--config", "missing.toml", "hi"]) == 2
    assert "kimi-agent: Config file" in capsys.readouterr().err
    assert main(["-p", "ollama", "-C", "nowhere", "hi"]) == 2
    assert "nowhere does not exist" in capsys.readouterr().err
    assert main(["-p", "kimi", "hello"]) == 2
    assert "No API key found for provider 'kimi'. Set KIMI_API_KEY or MOONSHOT_API_KEY" in capsys.readouterr().err
    assert main(["-p", "ollama"]) == 2
    assert "no prompt given" in capsys.readouterr().err
    assert main(["-p", "ollama", "-i", "hello"]) == 2
    assert "interactive mode needs a terminal" in capsys.readouterr().err
    stdin(tty=True)
    assert main(["-p", "ollama", "--json"]) == 2
    assert "--json needs a prompt" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["--approval", "sometimes"])


def test_one_shot_run(stdin, capsys, workdir, home):
    with FakeOpenAI([tool_call("write_file", path="hello.txt", content="hi\n"),
                     {"content": "Created hello.txt."}]) as fake:
        assert run(fake, "create", "hello.txt") == 0
        assert fake.requests[0]["messages"][-1]["content"] == "create hello.txt"
    captured = capsys.readouterr()
    assert (workdir / "hello.txt").read_text() == "hi\n"
    assert captured.out == "Created hello.txt.\n"  # only the answer goes to stdout when it is piped
    assert "write_file" in captured.err and "in 200 · out 20" in captured.err
    assert len(list((home / "sessions").glob("*.json"))) == 1
    assert (home / "logs" / "kimi-agent.log").exists()


def test_non_interactive_runs_cannot_approve(stdin, workdir):
    (workdir / "build").mkdir()
    with FakeOpenAI([tool_call("run_command", command="rm -rf build"), {"content": "I could not delete it."}]) as fake:
        assert run(fake, "clean up") == 0
        result = fake.requests[1]["messages"][-1]
    assert (workdir / "build").exists()
    assert result["role"] == "tool" and result["content"].startswith("Not permitted: run_command needs the user's approval")


def test_json_output(stdin, capsys, workdir):
    with FakeOpenAI([tool_call("write_file", path="a.txt", content="x"), {"content": "Done."}]) as fake:
        assert run(fake, "--json", "make a.txt") == 0
    events = json_events(capsys.readouterr().out)
    calls = [e for e in events if e["type"] == "tool_call"]
    assert calls == [{"type": "tool_call", "name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}]
    assert any(e["type"] == "tool_result" and e["name"] == "write_file" for e in events)
    final = events[-1]
    assert final["type"] == "final" and final["content"] == "Done." and final["usage"]["prompt_tokens"] == 200
    assert final["session"] and (workdir / "a.txt").read_text() == "x"


def test_json_output_on_api_error(stdin, capsys, workdir, monkeypatch):
    monkeypatch.setenv("KIMI_AGENT_MAX_RETRIES", "0")
    with FakeOpenAI([{"status": 400, "error": "model not found"}]) as fake:
        assert run(fake, "--json", "hi") == 1
    events = json_events(capsys.readouterr().out)
    assert any(e["type"] == "error" and "model not found" in e["message"] for e in events)
    assert events[-1]["type"] == "final" and "model not found" in events[-1]["error"]


def test_stdin_prompt(stdin, workdir):
    stdin("Traceback: boom\n")
    with FakeOpenAI([{"content": "It crashed."}]) as fake:
        assert run(fake, "explain") == 0
        assert fake.requests[0]["messages"][-1]["content"] == "explain\n\n<stdin>\nTraceback: boom\n</stdin>"
    stdin("summarize the readme\n")
    with FakeOpenAI([{"content": "ok"}]) as fake:
        assert run(fake) == 0
        assert fake.requests[0]["messages"][-1]["content"] == "summarize the readme"


def test_continue_and_resume(stdin, capsys, workdir):
    with FakeOpenAI([{"content": "The answer is 4."}, {"content": "You asked about 2+2."}]) as fake:
        assert run(fake, "what is 2+2?") == 0
        assert run(fake, "-c", "what did I ask?") == 0
        sent = [m["content"] for m in fake.requests[1]["messages"][1:]]
        assert sent == ["what is 2+2?", "The answer is 4.", "what did I ask?"]
        assert run(fake, "-r", "nope", "hi") == 1
    assert "No session matching 'nope'" in capsys.readouterr().err
    assert main(["--list-sessions"]) == 0
    assert capsys.readouterr().out.count("what is 2+2?") == 1  # -c continued the same session


def test_working_directory_and_debug_log(stdin, workdir, home):
    (workdir / "sub").mkdir()
    with FakeOpenAI([tool_call("write_file", path="x.txt", content="1"), {"content": "ok"}]) as fake:
        assert run(fake, "-C", "sub", "--debug", "make x") == 0
    assert (workdir / "sub" / "x.txt").exists() and not (workdir / "x.txt").exists()
    log = (home / "logs" / "kimi-agent.log").read_text()
    assert "DEBUG kimi_agent.llm: request:" in log and "test-key" not in log


def test_interactive_session(stdin, capsys, workdir, monkeypatch):
    stdin(tty=True)
    lines = iter(["/approval ask", "second message"])

    def read(self):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None
    monkeypatch.setattr(Repl, "read", read)
    with FakeOpenAI([{"content": "Hi!"}, {"content": "Second answer."}]) as fake:
        assert run(fake, "-i", "hello") == 0
        assert [r["messages"][-1]["content"] for r in fake.requests] == ["hello", "second message"]
    captured = capsys.readouterr()
    assert "Hi!" in captured.out and "Second answer." in captured.out
    assert "Approval mode: ask" in captured.err and "Resume it with: kimi-agent -r" in captured.err


def test_interrupt_during_startup(stdin, capsys, workdir, monkeypatch):
    from kimi_agent.runtime import Runtime

    def interrupted(self):
        raise KeyboardInterrupt
    monkeypatch.setattr(Runtime, "start", interrupted)
    with FakeOpenAI() as fake:
        assert run(fake, "hi") == 130
    assert "Interrupted." in capsys.readouterr().err

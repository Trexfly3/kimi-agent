from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from fakeserver import FakeOpenAI, tool_call

import kimi_agent
from kimi_agent.config import ConfigError, tomllib
from kimi_agent.plugins import load_plugins
from kimi_agent.runtime import Runtime
from kimi_agent.tools.base import Tool, ToolContext, ToolError, tool

PLUGIN = '''
from kimi_agent import tool

@tool
def shout(text: str) -> str:
    """Upper-case a text.

    Args:
        text: The text to shout.
    """
    return text.upper() + "!"

@tool(category="write")
def note(ctx, line: str) -> str:
    """Append a line to notes.txt in the working directory."""
    with open(ctx.resolve("notes.txt"), "a") as f:
        f.write(line + "\\n")
    return "noted"
'''


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def test_tool_decorator_forms(ctx):
    @tool
    def add(a: int, b: int = 2) -> int:
        """Add numbers."""
        return a + b

    @tool(name="where", category="internal")
    def where_am_i(context: ToolContext) -> str:
        return str(context.cwd)

    assert isinstance(add, Tool) and add.name == "add" and add.description == "Add numbers."
    assert add.parameters["required"] == ["a"] and add.run(ctx, {"a": 1}).output == "3"
    assert where_am_i.name == "where" and where_am_i.parameters["properties"] == {}
    assert where_am_i.run(ctx, {}).output == str(ctx.cwd)
    with pytest.raises(ValueError, match="unknown tool category"):
        tool(category="dangerous")


def test_load_plugins(workdir, home, monkeypatch):
    write(workdir / "local_tools.py", PLUGIN)
    write(home / "plugins" / "listed.py", '''
        from kimi_agent import tool

        @tool
        def one() -> str:
            return "1"

        @tool
        def hidden() -> str:
            return "not exported"

        TOOLS = [one]
    ''')
    write(workdir / "broken.py", "def oops(:\n")
    write(workdir / "empty.py", "X = 1\n")
    write(workdir / "bad_list.py", "TOOLS = ['not a tool']\n")
    write(workdir / "pkg" / "mytools.py", "from kimi_agent import tool\n\n@tool\ndef pkg_tool() -> str:\n    return 'pkg'\n")
    monkeypatch.syspath_prepend(str(workdir / "pkg"))
    specs = ["local_tools.py", "plugins/listed.py", "broken.py", "empty.py", "bad_list.py", "missing.py", "mytools",
             "no_such_module"]
    tools, errors = load_plugins(specs, [workdir, home])
    assert [t.name for t in tools] == ["shout", "note", "one", "pkg_tool"]
    assert len(errors) == 5
    assert errors[0].startswith("Plugin broken.py failed to load: SyntaxError")
    assert errors[1] == "Plugin empty.py defines no tools (decorate functions with @tool)."
    assert "TOOLS may only contain @tool functions" in errors[2] and "FileNotFoundError" in errors[3]
    assert "ModuleNotFoundError" in errors[4]
    again, _ = load_plugins([str(workdir / "local_tools.py")])
    assert again[0] is tools[0]  # loading the same file twice reuses the module


def test_runtime_custom_tools_and_plugins(settings, ui, workdir):
    @tool
    def secret_number() -> int:
        """Return the secret number."""
        return 42

    @tool
    def read_file(path: str) -> str:  # clashes with the builtin
        return "x"

    write(workdir / "my_plugin.py", PLUGIN)
    settings.plugins = ["my_plugin.py", "broken_missing.py"]
    replies = [tool_call("secret_number"), tool_call("shout", text="hi"), tool_call("note", line="remember"),
               {"content": "The number is 42."}]
    with FakeOpenAI(replies) as fake:
        settings.base_url = fake.base_url
        with Runtime(settings, ui, interactive=False, use_mcp=False, tools=[secret_number, read_file]) as runtime:
            runtime.start()
            assert {"secret_number", "shout", "note"} <= set(runtime.registry.names())
            runtime.rebuild_tools()  # custom tools survive a rebuild (e.g. after /provider or an MCP refresh)
            assert "secret_number" in runtime.registry.names()
            assert runtime.agent.run("what is the secret number?") == "The number is 42."
        results = [m["content"] for m in fake.requests[-1]["messages"] if m["role"] == "tool"]
    assert results == ["42", "HI!", "noted"] and (workdir / "notes.txt").read_text() == "remember\n"
    assert "Tool name conflict: read_file (skipped)" in ui.output
    assert "Plugin broken_missing.py failed to load: FileNotFoundError" in ui.output
    schemas = {t["function"]["name"]: t["function"] for t in fake.requests[0]["tools"]}
    assert schemas["shout"]["parameters"]["properties"]["text"]["description"] == "The text to shout."


def test_python_api(home, workdir, monkeypatch):
    @tool
    def word_count(text: str) -> int:
        """Count the words in a text."""
        return len(text.split())

    with FakeOpenAI([tool_call("word_count", text="to be or not to be"), {"content": "Six words."}]) as fake:
        common = {"provider": "custom", "base_url": fake.base_url, "model": "fake-model", "use_mcp": False}
        assert kimi_agent.run("count the words", tools=[word_count], **common) == "Six words."
        assert fake.requests[1]["messages"][-1]["content"] == "6"
        with kimi_agent.create_runtime(**common) as runtime:
            assert isinstance(runtime, kimi_agent.Runtime) and runtime.agent is not None
    assert len(list((home / "sessions").glob("*.json"))) == 1
    with pytest.raises(TypeError, match="unknown setting.*modle"):
        kimi_agent.create_runtime(modle="x")
    with pytest.raises(ConfigError, match="No API key found"):
        kimi_agent.create_runtime(provider="kimi")
    with pytest.raises(AttributeError):
        kimi_agent.nonexistent  # noqa: B018


EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_example_plugin(ctx, workdir):
    tools, errors = load_plugins([str(EXAMPLES / "plugin_project_stats.py")], [workdir])
    assert errors == []
    by_name = {t.name: t for t in tools}
    assert sorted(by_name) == ["add_todo", "project_stats"]
    assert (by_name["project_stats"].category, by_name["add_todo"].category) == ("read", "write")

    write(workdir / "src" / "a.py", "x = 1\ny = 2\n")
    write(workdir / "src" / "b.py", "z = 3\n")
    write(workdir / "README", "hello\n")
    write(workdir / ".git" / "config.py", "ignored\n" * 50)
    output = by_name["project_stats"].run(ctx, {}).output
    assert output.splitlines() == [f"{'.py':10} {2:6,} files {3:10,} lines", f"{'(none)':10} {1:6,} files {1:10,} lines"]
    with pytest.raises(ToolError, match="Not a directory"):
        by_name["project_stats"].run(ctx, {"path": "README"})

    ctx.file_history.begin_turn()
    assert by_name["add_todo"].run(ctx, {"text": "check\n  the   stats"}).output == "Added to TODO.md."
    assert (workdir / "TODO.md").read_text() == "- [ ] check the stats\n"
    ctx.file_history.undo_last_turn()
    assert not (workdir / "TODO.md").exists()


def test_examples_compile():
    for path in EXAMPLES.glob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    for path in EXAMPLES.glob("*.toml"):
        tomllib.loads(path.read_text(encoding="utf-8"))

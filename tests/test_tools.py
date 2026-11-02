from __future__ import annotations

import sys
import time

import pytest

from kimi_agent.tools import build_registry
from kimi_agent.tools.base import ToolError, prepare_arguments, truncate_middle
from kimi_agent.tools.files import (
    FileHistory,
    apply_edit,
    delete_path,
    edit_file,
    find_files,
    glob_to_regex,
    is_binary,
    list_directory,
    read_file,
    search_files,
    write_file,
)
from kimi_agent.tools.interaction import ask_user, update_plan
from kimi_agent.tools.memory import MemoryStore
from kimi_agent.tools.python_exec import PythonSession, run_python
from kimi_agent.tools.shell import (
    ProcessManager,
    list_processes,
    process_input,
    process_output,
    run_command,
    start_process,
    stop_process,
)


def run(tool, ctx, **args):
    return tool.run(ctx, prepare_arguments(tool, args))


# --------------------------------------------------------------------------- schemas

def test_registry_schemas_are_valid(settings):
    registry, notes = build_registry(settings, [])
    names = registry.names()
    assert {"read_file", "write_file", "edit_file", "run_command", "run_python", "update_plan"} <= set(names)
    assert "web_fetch" not in names  # web disabled in the test settings
    for schema in registry.schemas():
        fn = schema["function"]
        assert fn["description"], fn["name"]
        assert fn["parameters"]["type"] == "object"
        for prop in fn["parameters"].get("properties", {}).values():
            assert "description" in prop or "type" in prop


def test_disabled_tools_glob(settings):
    settings.disabled_tools = ["delete_*", "run_python"]
    registry, _ = build_registry(settings, [])
    assert "delete_path" not in registry and "run_python" not in registry and "read_file" in registry


def test_prepare_arguments_coerces_and_validates():
    args = prepare_arguments(read_file, {"path": "a.txt", "offset": "5"})
    assert args == {"path": "a.txt", "offset": 5}
    with pytest.raises(ToolError):
        prepare_arguments(read_file, {})
    assert prepare_arguments(read_file, {"path": "a", "bogus": 1, "limit": None}) == {"path": "a"}


# --------------------------------------------------------------------------- files

def test_write_read_edit_roundtrip(ctx, workdir):
    out = run(write_file, ctx, path="src/hello.py", content="def hello():\n    return 'hi'\n")
    assert "Created" in out.output
    assert (workdir / "src/hello.py").read_text() == "def hello():\n    return 'hi'\n"
    read = run(read_file, ctx, path="src/hello.py")
    assert "return 'hi'" in read.output and "1" in read.output
    edited = run(edit_file, ctx, path="src/hello.py", old_string="'hi'", new_string="'hello'")
    assert "Edited" in edited.output
    assert "'hello'" in (workdir / "src/hello.py").read_text()


def test_edit_errors(ctx, workdir):
    (workdir / "a.txt").write_text("x = 1\nx = 1\n")
    with pytest.raises(ToolError, match="(?i)not found|could not find"):
        run(edit_file, ctx, path="a.txt", old_string="y = 2", new_string="y = 3")
    with pytest.raises(ToolError):  # ambiguous: two matches without replace_all
        run(edit_file, ctx, path="a.txt", old_string="x = 1", new_string="x = 2")
    run(edit_file, ctx, path="a.txt", old_string="x = 1", new_string="x = 2", replace_all=True)
    assert (workdir / "a.txt").read_text() == "x = 2\nx = 2\n"


def test_apply_edit_preserves_crlf():
    text = "line one\r\nline two\r\n"
    new, count, _, _ = apply_edit(text, "line two", "line 2", False)
    assert count == 1 and new == "line one\r\nline 2\r\n"


def test_read_file_offsets_and_missing(ctx, workdir):
    (workdir / "big.txt").write_text("".join(f"line {i}\n" for i in range(1, 101)))
    out = run(read_file, ctx, path="big.txt", offset=50, limit=3).output
    assert "line 50" in out and "line 52" in out and "line 53" not in out
    with pytest.raises(ToolError, match="not found"):
        run(read_file, ctx, path="nope.txt")


def test_list_find_search(ctx, workdir):
    (workdir / "pkg").mkdir()
    (workdir / "pkg" / "mod.py").write_text("import os\nTODO = 'fix me'\n")
    (workdir / "node_modules").mkdir()
    (workdir / "node_modules" / "junk.py").write_text("TODO\n")
    listing = run(list_directory, ctx).output
    assert "pkg/" in listing and "mod.py" in listing and "junk.py" not in listing
    assert "pkg/mod.py" in run(find_files, ctx, pattern="**/*.py").output
    found = run(search_files, ctx, pattern="TODO").output
    assert "mod.py" in found and "junk.py" not in found
    assert "junk.py" in run(search_files, ctx, pattern="TODO", path="node_modules").output  # explicit target
    assert "mod.py" in run(search_files, ctx, pattern="todo", glob="*.py", ignore_case=True).output


def test_glob_to_regex():
    assert glob_to_regex("**/*.py").match("a/b/c.py")
    assert glob_to_regex("*.py").match("c.py")
    assert not glob_to_regex("*.py").match("a/c.py")
    assert glob_to_regex("src/**/test_*.py").match("src/x/y/test_a.py")


def test_is_binary():
    assert is_binary(b"\x00\x01\x02binary")
    assert not is_binary("héllo wörld\n".encode())


def test_delete_and_undo(ctx, workdir):
    history: FileHistory = ctx.file_history
    (workdir / "keep.txt").write_text("original")
    history.begin_turn()
    run(write_file, ctx, path="keep.txt", content="changed")
    run(write_file, ctx, path="new.txt", content="brand new")
    assert (workdir / "keep.txt").read_text() == "changed"
    messages = history.undo_turn(history.turn)
    assert (workdir / "keep.txt").read_text() == "original"
    assert not (workdir / "new.txt").exists()
    assert any("restored" in m for m in messages) and any("removed" in m for m in messages)

    history.begin_turn()
    run(delete_path, ctx, path="keep.txt")
    assert not (workdir / "keep.txt").exists()
    history.undo_last_turn()
    assert (workdir / "keep.txt").read_text() == "original"


def test_delete_refuses_non_empty_dir(ctx, workdir):
    (workdir / "d").mkdir()
    (workdir / "d" / "f").write_text("x")
    with pytest.raises(ToolError, match="recursive"):
        run(delete_path, ctx, path="d")
    run(delete_path, ctx, path="d", recursive=True)
    assert not (workdir / "d").exists()


# --------------------------------------------------------------------------- shell

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
def test_run_command_output_exit_code_and_cwd(ctx, workdir):
    (workdir / "sub").mkdir()
    out = run(run_command, ctx, command="echo hello && exit 3").output
    assert "hello" in out and "3" in out
    run(run_command, ctx, command="cd sub")
    assert ctx.cwd == workdir / "sub"
    assert "sub" in run(run_command, ctx, command="pwd").output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
def test_run_command_timeout(ctx):
    started = time.monotonic()
    out = run(run_command, ctx, command="sleep 30", timeout=1).output
    assert time.monotonic() - started < 10
    assert "timed out" in out.lower()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
def test_background_processes(ctx):
    started = run(start_process, ctx, command="for i in 1 2 3; do echo tick$i; sleep 0.2; done; cat",
                  wait_seconds=1).output
    assert "p1" in started and "tick1" in started
    sent = run(process_input, ctx, process_id="p1", text="echo-back").output
    assert sent.startswith("Sent input to p1")
    if "echo-back" not in sent:  # slow machine: the echo arrived after process_input returned
        sent += run(process_output, ctx, process_id="p1", wait_seconds=3, wait_for="echo-back").output
    assert "echo-back" in sent
    assert "p1" in run(list_processes, ctx).output
    stopped = run(stop_process, ctx, process_id="p1").output
    assert "p1" in stopped
    assert not ctx.processes.get("p1").running


def test_process_manager_unknown_id():
    with pytest.raises(ToolError, match="No background process"):
        ProcessManager().get("p99")


# --------------------------------------------------------------------------- python

def test_python_session_persists_state(ctx):
    ctx.python = PythonSession()
    try:
        run(run_python, ctx, code="x = 21")
        out = run(run_python, ctx, code="print(x * 2)\nx * 3").output
        assert "42" in out and "63" in out
        err = run(run_python, ctx, code="1/0").output
        assert "ZeroDivisionError" in err
        assert "21" in run(run_python, ctx, code="x").output  # state survives errors
        reset = run(run_python, ctx, code="'x' in globals()", reset=True).output
        assert "False" in reset
    finally:
        ctx.python.close()


def test_python_timeout_restarts(ctx):
    ctx.python = PythonSession()
    try:
        out = run(run_python, ctx, code="import time\ntime.sleep(30)", timeout=1).output
        assert "timed out" in out.lower() or "timeout" in out.lower()
        assert "ok" in run(run_python, ctx, code="print('ok')").output
    finally:
        ctx.python.close()


# --------------------------------------------------------------------------- interaction & memory

def test_update_plan_and_ask_user(ctx, ui):
    run(update_plan, ctx, steps=[{"step": "Read code", "status": "done"}, {"step": "Fix bug", "status": "in_progress"}])
    assert ctx.plan[1]["status"] == "in_progress" and ui.plans
    ui.answers = ["blue"]
    assert "not available" in run(ask_user, ctx, question="Favourite colour?").output  # non-interactive
    ctx.interactive = True
    assert "blue" in run(ask_user, ctx, question="Favourite colour?", choices=["red", "blue"]).output


def test_memory_store(tmp_path):
    store = MemoryStore(tmp_path / "memory.md")
    assert store.add("User prefers   tabs") and not store.add("user prefers tabs")
    store.add("Project uses pytest")
    assert store.facts() == ["User prefers tabs", "Project uses pytest"]
    assert store.remove("pytest") == ["Project uses pytest"]
    assert "- User prefers tabs" in store.render()
    store.clear()
    assert store.facts() == []


def test_truncate_middle():
    text = "a" * 1000 + "MIDDLE" + "b" * 1000
    out = truncate_middle(text, 200)
    assert len(out) < 400 and out.startswith("a") and out.endswith("b")
    assert truncate_middle("short", 200) == "short"

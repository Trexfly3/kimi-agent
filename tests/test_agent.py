from __future__ import annotations

import json

import pytest
from fakeserver import FakeOpenAI, tool_call

from kimi_agent.agent import EMPTY_NUDGE, INTERRUPTED, SKIPPED, STEP_LIMIT_NOTE
from kimi_agent.context import SUMMARY_SYSTEM, SUMMARY_TAG
from kimi_agent.runtime import Runtime


def make_runtime(settings, ui, fake, interactive=False) -> Runtime:
    settings.base_url = fake.base_url
    runtime = Runtime(settings, ui, interactive=interactive, use_mcp=False)
    runtime.llm._sleep = lambda attempt, exc: None
    runtime.start()
    return runtime


def tool_messages(body):
    return [m for m in body["messages"] if m["role"] == "tool"]


def is_summary_request(body):
    return body["messages"][0]["content"] == SUMMARY_SYSTEM


@pytest.fixture
def runtimes():
    created = []
    yield created
    for runtime in created:
        runtime.close()


def test_tool_loop_writes_file(settings, ui, workdir, runtimes):
    replies = [tool_call("write_file", path="hello.txt", content="hi there\n"), {"content": "Created hello.txt."}]
    with FakeOpenAI(replies) as fake:
        runtimes.append(rt := make_runtime(settings, ui, fake))
        answer = rt.agent.run("Create hello.txt")
        second = fake.requests[1]
    assert answer == "Created hello.txt."
    assert (workdir / "hello.txt").read_text() == "hi there\n"
    assert [m["role"] for m in rt.agent.messages] == ["system", "user", "assistant", "tool", "assistant"]
    assert "Created" in tool_messages(second)[0]["content"]
    assert second["messages"][2]["tool_calls"][0]["function"]["name"] == "write_file"
    assert rt.agent.usage.prompt_tokens == 200
    assert "write_file" in ui.output and "hello.txt" in ui.output


def test_non_interactive_needs_approval(settings, ui, runtimes):
    settings.approval = "ask"
    replies = [tool_call("run_command", command="ls"), {"content": "Could not run it."}]
    with FakeOpenAI(replies) as fake:
        runtimes.append(rt := make_runtime(settings, ui, fake))
        rt.agent.run("list files")
        result = tool_messages(fake.requests[1])[0]["content"]
    assert result.startswith("Not permitted") and not ui.confirmations


def test_interactive_approval_flow(settings, ui, workdir, runtimes):
    settings.approval = "ask"
    ui.answers = ["always", ("no", "use python instead")]
    replies = [tool_call("run_command", command="echo first > a.txt"),
               tool_call("run_command", command="echo second > b.txt"),
               tool_call("run_python", code="print(1)"),
               {"content": "ok"}]
    with FakeOpenAI(replies) as fake:
        runtimes.append(rt := make_runtime(settings, ui, fake, interactive=True))
        assert rt.agent.run("do it") == "ok"
        results = [tool_messages(fake.requests[i])[-1]["content"] for i in (1, 2, 3)]
    assert (workdir / "a.txt").exists() and (workdir / "b.txt").exists()
    assert [c[0] for c in ui.confirmations] == ["run_command", "run_python"]  # "always" covered the 2nd echo
    assert results[2] == "The user denied this tool call and said: use python instead"


def test_denial_without_feedback_stops_the_turn(settings, ui, runtimes):
    settings.approval = "ask"
    ui.answers = ["no"]
    reply = {"tool_calls": [{"name": "run_command", "arguments": {"command": "make"}},
                            {"name": "read_file", "arguments": {"path": "x"}}]}
    with FakeOpenAI([reply]) as fake:
        runtimes.append(rt := make_runtime(settings, ui, fake, interactive=True))
        assert rt.agent.run("build") == ""
        assert len(fake.requests) == 1
    tools = [m for m in rt.agent.messages if m["role"] == "tool"]
    assert tools[0]["content"].startswith("The user denied") and tools[1]["content"] == SKIPPED


def test_deny_list_blocks(settings, ui, runtimes):
    settings.approval = "yolo"
    settings.deny_commands = [r"git\s+push"]
    with FakeOpenAI([tool_call("run_command", command="git push"), {"content": "blocked"}]) as fake:
        runtimes.append(rt := make_runtime(settings, ui, fake))
        rt.agent.run("push")
        assert tool_messages(fake.requests[1])[0]["content"].startswith("Blocked by the user's safety configuration")


def test_bad_tool_calls_are_reported_to_the_model(settings, ui, runtimes):
    replies = [{"tool_calls": [{"name": "no_such_tool", "arguments": {}},
                               {"name": "read_file", "arguments": {}},
                               {"name": "read_file", "arguments": '{"path": '}]},
               {"content": "sorry"}]
    with FakeOpenAI(replies) as fake:
        runtimes.append(rt := make_runtime(settings, ui, fake))
        rt.agent.run("go")
        results = [m["content"] for m in tool_messages(fake.requests[1])]
    assert "there is no tool named 'no_such_tool'" in results[0]
    assert "Missing required argument(s): path" in results[1]
    assert "not valid JSON" in results[2]


def test_tool_errors_and_crashes(settings, ui, runtimes):
    with FakeOpenAI([tool_call("read_file", path="missing.txt"), {"content": "done"}]) as fake:
        runtimes.append(rt := make_runtime(settings, ui, fake))
        rt.agent.run("read")
        assert tool_messages(fake.requests[1])[0]["content"].startswith("Error: File not found")


def test_step_limit(settings, ui, runtimes):
    settings.max_steps = 2

    def handler(body, fake):
        if STEP_LIMIT_NOTE in json.dumps(body["messages"][-1]):
            return {"content": "Summary of progress."}
        return tool_call("list_directory")
    with FakeOpenAI(handler=handler) as fake:
        runtimes.append(rt := make_runtime(settings, ui, fake))
        assert rt.agent.run("loop forever") == "Summary of progress."
        assert len(fake.requests) == 3
    assert "Step limit reached" in ui.output


def test_repetition_hint(settings, ui, runtimes):
    replies = [tool_call("list_directory")] * 3 + [{"content": "done"}]
    with FakeOpenAI(replies) as fake:
        runtimes.append(rt := make_runtime(settings, ui, fake))
        rt.agent.run("go")
        assert "exact list_directory call 3 times" in tool_messages(fake.requests[3])[-1]["content"]


def test_empty_response_is_nudged_once(settings, ui, runtimes):
    with FakeOpenAI([{"content": ""}, {"content": "All done."}]) as fake:
        runtimes.append(rt := make_runtime(settings, ui, fake))
        assert rt.agent.run("hello") == "All done."
        assert EMPTY_NUDGE in fake.requests[1]["messages"][-1]["content"]


def test_subagent_delegation(settings, ui, runtimes):
    def handler(body, fake):
        system = body["messages"][0]["content"]
        if "Sub-agent mode" in system:
            assert not any(t["function"]["name"] == "delegate_task" for t in body.get("tools", []))
            return {"content": "Child report: found 3 files."}
        if not tool_messages(body):
            return tool_call("delegate_task", task="Count the files", context="in src/")
        return {"content": "Parent: " + tool_messages(body)[0]["content"]}
    with FakeOpenAI(handler=handler) as fake:
        runtimes.append(rt := make_runtime(settings, ui, fake))
        answer = rt.agent.run("how many files?")
        child_request = fake.requests[1]
    assert answer == "Parent: Child report: found 3 files."
    assert "Count the files" in child_request["messages"][1]["content"]
    assert "# Context\nin src/" in child_request["messages"][1]["content"]
    assert rt.agent.usage.prompt_tokens == 300  # child usage is added to the parent's


def test_manual_compaction(settings, ui, runtimes):
    def handler(body, fake):
        if is_summary_request(body):
            assert "[USER]\nfirst question" in body["messages"][1]["content"]
            return {"content": "The user asked two questions; both answered."}
        realistic = {"prompt_tokens": len(json.dumps(body)) // 3, "completion_tokens": 1}  # for calibration
        return {"content": "answer", "usage": realistic}
    with FakeOpenAI(handler=handler) as fake:
        runtimes.append(rt := make_runtime(settings, ui, fake))
        settings.context_window = 100_000
        rt.agent.run("first question " + "x" * 6000)
        rt.agent.run("second question")
        assert not any(is_summary_request(b) for b in fake.requests)
        settings.context_window = 4_000  # small window => only the last turn is kept verbatim
        assert rt.agent.compact(focus="questions")
        [summary_request] = [b for b in fake.requests if is_summary_request(b)]
    assert "Pay special attention to: questions" in summary_request["messages"][1]["content"]
    messages = rt.agent.messages
    assert messages[0]["role"] == "system" and messages[1].get("_summary")
    assert f"<{SUMMARY_TAG}>" in messages[1]["content"]
    assert messages[-1]["content"] == "answer" and messages[-2]["content"] == "second question"


def test_overflow_recovers_by_compacting(settings, ui, runtimes):
    state = {"overflowed": False}

    def handler(body, fake):
        if is_summary_request(body):
            return {"content": "Earlier work summarized."}
        if not state["overflowed"]:
            state["overflowed"] = True
            return {"status": 400, "error": "This model's maximum context length is 32768 tokens"}
        return {"content": "recovered"}
    with FakeOpenAI(handler=handler) as fake:
        runtimes.append(rt := make_runtime(settings, ui, fake))
        settings.context_window = 100_000
        rt.agent.messages += [{"role": "user", "content": "old " * 4000, "_turn": True},
                              {"role": "assistant", "content": "old answer " * 1000}]
        assert rt.agent.run("new question") == "recovered"
        assert sum(map(is_summary_request, fake.requests)) == 1
    assert any(m.get("_summary") for m in rt.agent.messages)
    assert "compacting" in ui.output
    assert 2_048 <= rt.agent.context.window_limit < 100_000  # learned from the overflow error


def test_small_window_warning_and_no_pointless_compaction(settings, ui, runtimes):
    settings.context_window = 4_000  # smaller than the tool definitions alone
    with FakeOpenAI([{"content": "a"}, {"content": "b"}]) as fake:
        runtimes.append(rt := make_runtime(settings, ui, fake))
        rt.agent.run("one")
        rt.agent.run("two")
        assert not any(is_summary_request(b) for b in fake.requests)
    assert ui.output.count("leaving little room") == 1


def test_interrupt_repairs_tool_results(settings, ui, runtimes):
    with FakeOpenAI([]) as fake:
        runtimes.append(rt := make_runtime(settings, ui, fake))
    agent = rt.agent
    agent.messages += [
        {"role": "user", "content": "go", "_turn": True},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "name": "read_file", "content": "done"}]
    agent._repair_tool_results()
    assert agent.messages[-1] == {"role": "tool", "tool_call_id": "b", "name": "read_file", "content": INTERRUPTED}
    agent._repair_tool_results()
    assert len(agent.messages) == 5  # idempotent


def test_pop_turn_and_reset(settings, ui, runtimes):
    with FakeOpenAI([{"content": "one"}, {"content": "two"}]) as fake:
        runtimes.append(rt := make_runtime(settings, ui, fake))
        rt.agent.run("first")
        rt.agent.run("second")
    assert rt.agent.pop_turn()["content"] == "second"
    assert [m.get("content") for m in rt.agent.messages[1:]] == ["first", "one"]
    rt.agent.reset()
    assert len(rt.agent.messages) == 1 and rt.agent.pop_turn() is None

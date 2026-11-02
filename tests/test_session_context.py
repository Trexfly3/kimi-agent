from __future__ import annotations

import json
import time

import pytest

from kimi_agent.context import ContextManager
from kimi_agent.session import Session, SessionStore, export_markdown, make_title, new_session_id

# --------------------------------------------------------------------------- sessions

def sample_messages():
    return [
        {"role": "user", "content": "  Fix the   failing test in utils.py please ", "_turn": True},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "utils.py"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "1\tdef f(): pass"},
        {"role": "user", "content": [{"type": "text", "text": "see screenshot"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
         "_turn": True},
        {"role": "assistant", "content": "Fixed it."},
    ]


def test_save_load_list(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    assert store.list() == [] and store.latest() is None
    first = Session(id="20240101-000000-aaaaaa", cwd="/proj/a", messages=sample_messages())
    path = store.save(first)
    assert path.is_file() and first.title == "Fix the failing test in utils.py please"
    saved = json.loads(path.read_text())
    assert saved["messages"][3]["content"] == "see screenshot\n[image not saved in session]"
    time.sleep(0.01)
    second = Session(id="20240102-000000-bbbbbb", cwd="/proj/b", messages=[{"role": "user", "content": "hi",
                                                                           "_turn": True}])
    store.save(second)
    loaded = store.load("20240101-000000-aaaaaa")
    assert loaded.turns == 2 and loaded.cwd == "/proj/a"
    assert store.load("20240102").id == second.id  # unique prefix
    with pytest.raises(ValueError, match="ambiguous"):
        store.load("2024010")
    with pytest.raises(FileNotFoundError):
        store.load("1999")
    assert [s.id for s in store.list()] == [second.id, first.id]
    assert store.latest(cwd="/proj/a").id == first.id
    store.delete(first.id)
    assert [s.id for s in store.list()] == [second.id]


def test_corrupt_session_files_are_skipped(tmp_path):
    store = SessionStore(tmp_path)
    (tmp_path / "broken.json").write_text("{not json")
    store.save(Session(id="ok", messages=[{"role": "user", "content": "x", "_turn": True}]))
    assert [s.id for s in store.list()] == ["ok"]


def test_titles_and_ids():
    assert make_title([{"role": "user", "content": "x" * 100, "_turn": True}], limit=10) == "x" * 9 + "…"
    assert make_title([]) == "(empty)"
    attached = ('<shell_command>\n$ ls\na.py\n(exit code 0)\n</shell_command>\n\nExplain @a.py\n\n'
                '<file path="a.py">\nprint(1)\n</file>')
    assert make_title([{"role": "user", "content": attached, "_turn": True}]) == "Explain @a.py"
    assert new_session_id() != new_session_id()


def test_export_markdown():
    md = export_markdown(sample_messages(), title="Demo")
    assert md.startswith("# Demo")
    assert "## User\n\nFix the" in md and "**Tool call:** `read_file`" in md
    assert "**Result (read_file):**" in md and "## Agent\n\nFixed it." in md and "[image]" in md


# --------------------------------------------------------------------------- context management

class DummySettings:
    context_window = 10_000
    compact_threshold = 0.8


class FakeLLM:
    def __init__(self, reply="SUMMARY"):
        self.reply = reply
        self.requests = []

    def complete(self, messages, tools=None, **kwargs):
        from kimi_agent.llm import LLMResponse
        self.requests.append(messages)
        return LLMResponse(content=self.reply)


def conversation(turns, size):
    messages = [{"role": "system", "content": "sys"}]
    for i in range(turns):
        messages += [
            {"role": "user", "content": f"question {i} " + "q" * size, "_turn": True},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "name": "read_file", "content": "t" * size},
            {"role": "assistant", "content": f"answer {i}"},
        ]
    return messages


def test_prune_shortens_old_tool_outputs_and_images():
    messages = conversation(4, 10_000)
    image_msg = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:x"}}]}
    messages[1:1] = [dict(image_msg) for _ in range(5)]
    pruned = ContextManager.prune(messages, keep_recent=4, tool_limit=500)
    old_tools = [m for m in pruned[:-4] if m["role"] == "tool"]
    assert all(len(m["content"]) < 1_000 for m in old_tools)
    assert len([m for m in pruned if m["role"] == "tool" and len(m["content"]) == 10_000]) == 1  # recent kept
    assert sum(1 for m in pruned if isinstance(m["content"], list)) == 3  # newest images kept
    assert all(len(m["content"]) == 10_000 for m in messages if m["role"] == "tool")


def test_split_never_orphans_tool_results():
    manager = ContextManager(DummySettings(), FakeLLM())
    messages = conversation(6, 2_000)
    for budget in range(0, 6_000, 97):
        split = manager._split_index(messages, budget)
        assert 1 <= split < len(messages)
        assert messages[split]["role"] != "tool"


def test_maybe_compact_summarizes_head():
    llm = FakeLLM("The user asked six questions.")
    manager = ContextManager(DummySettings(), llm)
    messages = conversation(6, 3_000)
    assert manager.needs_compaction(messages)
    result = manager.maybe_compact(messages, extra="Current plan:\n- [pending] x")
    assert result is not None and manager.compactions == 1
    assert result[0]["role"] == "system" and result[1]["_summary"]
    assert "The user asked six questions." in result[1]["content"] and "Current plan" in result[1]["content"]
    assert result[2]["role"] in ("assistant", "user")
    assert not manager.needs_compaction(result)
    request = llm.requests[0][1]["content"]
    assert "[USER]\nquestion 0" in request and "-> called read_file" in request


def test_maybe_compact_noop_when_small():
    manager = ContextManager(DummySettings(), FakeLLM())
    assert manager.maybe_compact(conversation(1, 10)) is None


def test_failed_summary_drops_history_with_note():
    from kimi_agent.llm import LLMError

    class FailingLLM(FakeLLM):
        def complete(self, messages, tools=None, **kwargs):
            raise LLMError("boom")
    manager = ContextManager(DummySettings(), FailingLLM())
    result = manager.maybe_compact(conversation(6, 3_000))
    assert result is not None and "could not be summarized" in result[1]["content"]


def test_calibration_and_overflow():
    from kimi_agent.llm import Usage
    manager = ContextManager(DummySettings(), FakeLLM())
    messages = conversation(2, 5_000)
    manager.calibrate(messages, None, Usage(prompt_tokens=5_000))
    assert 0.2 < manager.tokens_per_char < 0.3
    manager.on_overflow(messages)
    assert manager.window_limit and manager.window < 10_000

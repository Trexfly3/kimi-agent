from __future__ import annotations

import json

import pytest

from kimi_agent.llm import (
    StreamFilter,
    Usage,
    extract_tool_calls,
    kimi_tool_name,
    normalize_schema,
    parse_arguments,
    prepare_native,
    split_think,
    strip_images,
    to_text_mode,
)


@pytest.mark.parametrize("raw, expected", [
    ('{"path": "a.txt"}', {"path": "a.txt"}),
    ('```json\n{"path": "a.txt"}\n```', {"path": "a.txt"}),
    ('{"path": "a.txt",}', {"path": "a.txt"}),
    ("{'path': 'a.txt', 'recursive': True}", {"path": "a.txt", "recursive": True}),
    ('"{\\"path\\": \\"a.txt\\"}"', {"path": "a.txt"}),
    ('{"path": "a.txt"} trailing junk', {"path": "a.txt"}),
    ('{"code": "print(1)\nprint(2)"}', {"code": "print(1)\nprint(2)"}),  # raw newline inside a string
    ("", {}), ("null", {}), (None, {}), ({"a": 1}, {"a": 1}),
])
def test_parse_arguments_lenient(raw, expected):
    args, error = parse_arguments(raw)
    assert error is None and args == expected


@pytest.mark.parametrize("raw", ['{"path": ', "[1, 2]", "not json at all"])
def test_parse_arguments_errors(raw):
    args, error = parse_arguments(raw)
    assert args is None and error


def test_extract_tool_call_blocks():
    text = ('Let me look.\n<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call>\n'
            '<tool_call>{"name": "list_directory", "arguments": {}}</tool_call>')
    clean, calls = extract_tool_calls(text)
    assert clean == "Let me look."
    assert calls == [("read_file", {"path": "a.py"}, None), ("list_directory", {}, None)]


def test_extract_unterminated_tool_call():
    clean, calls = extract_tool_calls('<tool_call>{"name": "x", "arguments": {"a": 1}}')
    assert calls == [("x", {"a": 1}, None)] and clean == ""


def test_extract_kimi_native_markers():
    text = ("I'll check.<|tool_calls_section_begin|><|tool_call_begin|>functions.read_file:0"
            "<|tool_call_argument_begin|>{\"path\": \"a.py\"}<|tool_call_end|><|tool_calls_section_end|>")
    clean, calls = extract_tool_calls(text)
    assert clean == "I'll check."
    assert calls == [("read_file", '{"path": "a.py"}', "functions.read_file:0")]
    assert kimi_tool_name("functions.web_search:12") == "web_search"


def test_extract_function_xml():
    text = ("<function=edit_file>\n<parameter=path>\na.py\n</parameter>\n<parameter=replace_all>\ntrue\n"
            "</parameter>\n<parameter=old_string>\nx = 1\n</parameter>\n</function>")
    clean, calls = extract_tool_calls(text)
    assert calls == [("edit_file", {"path": "a.py", "replace_all": True, "old_string": "x = 1"}, None)]
    assert clean == ""
    clean, calls = extract_tool_calls("<tool_call>\n" + text + "\n</tool_call>")
    assert calls[0][0] == "edit_file"


def test_extract_bare_json_only_for_known_tools():
    text = 'Sure:\n```json\n{"name": "read_file", "arguments": {"path": "x"}}\n```'
    assert extract_tool_calls(text, {"read_file"}, allow_bare_json=False)[1] == []
    clean, calls = extract_tool_calls(text, {"read_file"}, allow_bare_json=True)
    assert calls == [("read_file", {"path": "x"}, None)] and clean == "Sure:"
    assert extract_tool_calls(text, {"other"}, allow_bare_json=True)[1] == []
    _, calls = extract_tool_calls('{"name": "read_file", "parameters": {"path": "y"}}', {"read_file"}, True)
    assert calls == [("read_file", {"path": "y"}, None)]


def test_split_think():
    assert split_think("<think>hmm</think>\nAnswer") == ("Answer", "hmm")
    assert split_think("reasoning only</think>Answer") == ("Answer", "reasoning only")
    assert split_think("plain") == ("plain", "")
    assert split_think("<think>unfinished") == ("", "unfinished")


def feed_all(pieces, size=None):
    content, reasoning = [], []
    sf = StreamFilter(content.append, reasoning.append)
    for piece in pieces:
        sf.feed(piece)
    sf.flush()
    return "".join(content), "".join(reasoning)


def chunks(text, size):
    return [text[i:i + size] for i in range(0, len(text), size)]


@pytest.mark.parametrize("size", [1, 2, 3, 7, 100])
def test_stream_filter_think_and_tool_markup(size):
    text = '<think>step one\nstep two</think>\nHello world <tool_call>{"name": "x"}</tool_call> tail'
    content, reasoning = feed_all(chunks(text, size))
    assert reasoning == "step one\nstep two"
    assert content == "Hello world "


@pytest.mark.parametrize("size", [1, 4, 100])
def test_stream_filter_plain_text_with_angle_brackets(size):
    text = "a < b and <tool or <function but not markup; x <think> y"
    content, reasoning = feed_all(chunks(text, size))
    assert content == text and reasoning == ""


def test_stream_filter_kimi_markers():
    content, _ = feed_all(["Checking", "<|tool_calls", "_section_begin|><|tool_call_begin|>functions.x:0"])
    assert content == "Checking"


def test_normalize_schema_inlines_refs_and_types():
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "$defs": {"Point": {"type": "object", "properties": {"x": {"type": "number"}}}},
        "properties": {
            "where": {"$ref": "#/$defs/Point", "description": "loc"},
            "mode": {"enum": ["a", "b"]},
            "anything": {"description": "untyped"},
            "self": {"$ref": "#/$defs/Missing"},
        },
        "required": ["where"],
    }
    out = normalize_schema(schema)
    assert "$schema" not in out and "$defs" not in out
    assert out["properties"]["where"] == {"type": "object", "properties": {"x": {"type": "number"}},
                                          "description": "loc"}
    assert out["properties"]["mode"]["type"] == "string"
    assert out["properties"]["anything"]["type"] == "string"
    assert out["properties"]["self"] == {"type": "object"}
    assert out["required"] == ["where"]
    assert normalize_schema(None) == {"type": "object", "properties": {}}
    assert normalize_schema({"properties": {"a": {"type": "string"}}})["type"] == "object"


def test_recursive_ref_terminates():
    schema = {"type": "object", "definitions": {"Node": {"type": "object", "properties": {
        "child": {"$ref": "#/definitions/Node"}}}}, "properties": {"root": {"$ref": "#/definitions/Node"}}}
    out = normalize_schema(schema)
    assert json.dumps(out)  # no infinite recursion


SCHEMAS = [{"type": "function", "function": {"name": "read_file", "description": "Read",
                                             "parameters": {"type": "object", "properties": {}}}}]


def test_to_text_mode_conversion():
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "read a.py"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "1\tprint('hi')"},
        {"role": "user", "content": "thanks"},
    ]
    out = to_text_mode(messages, SCHEMAS)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "user"]
    assert "<tool_call>" in out[0]["content"] and '"read_file"' in out[0]["content"]
    assert json.loads(out[2]["content"].split("<tool_call>\n")[1].split("\n</tool_call>")[0]) == {
        "name": "read_file", "arguments": {"path": "a.py"}}
    assert out[3]["content"].startswith('<tool_result name="read_file" id="c1">')
    assert out[3]["content"].endswith("thanks")
    assert to_text_mode([{"role": "user", "content": "hi"}], SCHEMAS)[0]["role"] == "system"


def test_prepare_native():
    messages = [{"role": "assistant", "content": "", "reasoning_content": "r",
                 "tool_calls": [{"id": "c", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c", "name": "x", "content": "ok"}]
    kimi = prepare_native(messages, kimi=True, keep_reasoning=True)
    assert "content" not in kimi[0] and kimi[0]["reasoning_content"] == "r" and kimi[1]["name"] == "x"
    other = prepare_native(messages, kimi=False, keep_reasoning=False)
    assert other[0]["content"] == "" and "reasoning_content" not in other[0] and "name" not in other[1]
    assert messages[0]["reasoning_content"] == "r"  # inputs are not mutated


def test_strip_images_and_usage():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "look"},
                                         {"type": "image_url", "image_url": {"url": "data:..."}}]}]
    assert strip_images(msgs, "[img]")[0]["content"] == "look\n[img]"
    usage = Usage.from_any({"prompt_tokens": 10, "completion_tokens": 5,
                            "prompt_tokens_details": {"cached_tokens": 4},
                            "completion_tokens_details": {"reasoning_tokens": 2}})
    assert (usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens, usage.reasoning_tokens) == (10, 5, 4, 2)
    total = Usage()
    total.add(usage)
    total.add(None)
    assert total.total_tokens == 15

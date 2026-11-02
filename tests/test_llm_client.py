from __future__ import annotations

import json

import pytest
from fakeserver import FakeOpenAI

from kimi_agent.llm import ContextOverflowError, LLMClient, LLMError

TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "Read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
USER = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi", "_turn": True}]


def client_for(settings, fake) -> LLMClient:
    settings.base_url = fake.base_url
    client = LLMClient(settings)
    client._sleep = lambda attempt, exc: None  # no real backoff delays in tests
    return client


def collect(client, messages=USER, tools=TOOLS):
    content, reasoning = [], []
    response = client.complete(messages, tools, on_content=content.append, on_reasoning=reasoning.append)
    return response, "".join(content), "".join(reasoning)


def test_streaming_content_reasoning_tool_calls_and_usage(settings):
    reply = {"reasoning": "I should read the file.", "content": "Reading it now.",
             "tool_calls": [{"id": "call_a", "name": "read_file", "arguments": {"path": "a.py"}},
                            {"id": "call_b", "name": "read_file", "arguments": {"path": "b.py"}}],
             "usage": {"prompt_tokens": 50, "completion_tokens": 7, "total_tokens": 57,
                       "prompt_tokens_details": {"cached_tokens": 32}}}
    with FakeOpenAI([reply]) as fake:
        response, content, reasoning = collect(client_for(settings, fake))
        body = fake.requests[0]
        headers = fake.headers[0]
    assert content == "Reading it now." and reasoning == "I should read the file."
    assert response.content == "Reading it now." and response.reasoning == reasoning
    assert response.reasoning_key == "reasoning_content"
    assert [(c.id, c.name, json.loads(c.arguments)) for c in response.tool_calls] == [
        ("call_a", "read_file", {"path": "a.py"}), ("call_b", "read_file", {"path": "b.py"})]
    assert response.finish_reason == "tool_calls"
    assert (response.usage.prompt_tokens, response.usage.cached_tokens) == (50, 32)
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["model"] == "fake-model" and body["tools"][0]["function"]["name"] == "read_file"
    assert all("_turn" not in m for m in body["messages"])
    assert headers.get("authorization") == "Bearer test-key"
    message = response.assistant_message()
    assert message["reasoning_content"] == reasoning and len(message["tool_calls"]) == 2


def test_non_streaming(settings):
    settings.stream = False
    reply = {"content": "<think>pondering</think>Done.", "tool_calls": [{"name": "read_file", "arguments": "{'path': 'x'}"}]}
    with FakeOpenAI([reply]) as fake:
        response, content, reasoning = collect(client_for(settings, fake))
        assert "stream" not in fake.requests[0] or not fake.requests[0]["stream"]
    assert response.content == "Done." and response.reasoning == "pondering"
    assert content == "Done." and reasoning == "pondering"
    assert json.loads(response.tool_calls[0].arguments) == {"path": "x"}  # python-style args repaired


def test_request_options(settings):
    settings.temperature, settings.top_p, settings.max_tokens = 0.3, 0.9, 1234
    settings.reasoning_effort = "high"
    settings.extra_body = {"thinking": {"type": "enabled"}}
    settings.extra_headers = {"X-Test": "yes"}
    with FakeOpenAI([{"content": "ok"}]) as fake:
        collect(client_for(settings, fake))
        body, headers = fake.requests[0], fake.headers[0]
    assert (body["temperature"], body["top_p"], body["max_tokens"]) == (0.3, 0.9, 1234)
    assert body["reasoning_effort"] == "high" and body["thinking"] == {"type": "enabled"}
    assert headers.get("x-test") == "yes"


def test_kimi_specifics(settings):
    settings.provider = "kimi"
    settings.max_tokens = 99
    messages = USER + [
        {"role": "assistant", "content": "", "reasoning_content": "think",
         "tool_calls": [{"id": "functions.read_file:0", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "functions.read_file:0", "name": "read_file", "content": "x"}]
    with FakeOpenAI([{"content": "ok"}]) as fake:
        client = client_for(settings, fake)
        assert client.is_kimi
        collect(client, messages)
        body = fake.requests[0]
    assert body["max_completion_tokens"] == 99 and "max_tokens" not in body
    assistant = body["messages"][2]
    assert "content" not in assistant and assistant["reasoning_content"] == "think"
    assert body["messages"][3]["name"] == "read_file"


def test_stream_options_rejected_falls_back(settings):
    def handler(body, fake):
        if "stream_options" in body:
            return {"status": 400, "error": "Unknown parameter: 'stream_options'"}
        return {"content": "fine"}
    with FakeOpenAI(handler=handler) as fake:
        client = client_for(settings, fake)
        response, _, _ = collect(client)
        assert response.content == "fine" and len(fake.requests) == 2 and not client.stream_options_ok
        collect(client)
        assert len(fake.requests) == 3  # remembered: no extra failing request


def test_tools_unsupported_switches_to_text_mode(settings):
    def handler(body, fake):
        if "tools" in body:
            return {"status": 400, "error": "registry.ollama.ai/library/gemma:2b does not support tools"}
        return {"content": 'Let me read it.\n<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n'
                           "</tool_call>"}
    with FakeOpenAI(handler=handler, chunk_size=3) as fake:
        client = client_for(settings, fake)
        response, content, _ = collect(client)
        retry = fake.requests[1]
    assert client.text_mode and any("text-based tool calls" in n for n in client.drain_notices())
    assert response.content == "Let me read it."
    assert content.strip() == "Let me read it."  # tool markup never reaches the user
    assert response.tool_calls[0].name == "read_file"
    assert json.loads(response.tool_calls[0].arguments) == {"path": "a.py"}
    assert "tools" not in retry and retry["stop"] == ["<tool_result"]
    assert "<tool_call>" in retry["messages"][0]["content"]


def test_text_mode_tool_results_roundtrip(settings):
    settings.tool_mode = "text"
    messages = USER + [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "hello"}]
    with FakeOpenAI([{"content": "The file says hello."}]) as fake:
        response, _, _ = collect(client_for(settings, fake), messages)
        sent = fake.requests[0]["messages"]
    assert response.content == "The file says hello." and not response.tool_calls
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert "<tool_result" in sent[-1]["content"]


def test_unparsed_kimi_markup_in_content(settings):
    content = ("Checking.<|tool_calls_section_begin|><|tool_call_begin|>functions.read_file:0"
               '<|tool_call_argument_begin|>{"path": "z.py"}<|tool_call_end|><|tool_calls_section_end|>')
    with FakeOpenAI([{"content": content}], chunk_size=4) as fake:
        response, streamed, _ = collect(client_for(settings, fake))
    assert streamed == "Checking." and response.content == "Checking."
    assert response.tool_calls[0].id == "functions.read_file:0"
    assert json.loads(response.tool_calls[0].arguments) == {"path": "z.py"}


def test_invalid_tool_arguments_are_flagged(settings):
    reply = {"tool_calls": [{"name": "read_file", "arguments": '{"path": "a.py", "content": "unterminated'}],
             "finish_reason": "length"}
    with FakeOpenAI([reply]) as fake:
        response, _, _ = collect(client_for(settings, fake))
    call = response.tool_calls[0]
    assert call.parse_error and call.arguments == "{}" and "unterminated" in call.raw_arguments
    assert response.finish_reason == "length"


@pytest.mark.parametrize("error", [
    "This model's maximum context length is 8192 tokens. However, you requested 9000 tokens",
    "Input is too long for requested model.", "prompt is too long: 210000 tokens > 200000 maximum",
])
def test_context_overflow(settings, error):
    with FakeOpenAI([{"status": 400, "error": error}]) as fake, pytest.raises(ContextOverflowError):
        collect(client_for(settings, fake))


def test_413_is_overflow(settings):
    with FakeOpenAI([{"status": 413, "error": "Payload Too Large"}]) as fake, pytest.raises(ContextOverflowError):
        collect(client_for(settings, fake))


@pytest.mark.parametrize("status, needle", [(401, "Authentication failed"), (404, "model name or base URL"),
                                            (403, "Access denied"), (400, "API error 400")])
def test_friendly_errors(settings, status, needle):
    with FakeOpenAI([{"status": status, "error": "nope"}]) as fake, pytest.raises(LLMError, match=needle) as info:
        collect(client_for(settings, fake))
    assert not isinstance(info.value, ContextOverflowError) and "nope" in str(info.value)


def test_retries_transient_errors(settings):
    settings.max_retries = 2
    replies = [{"status": 503, "error": "overloaded"}, {"status": 429, "error": "slow down"}, {"content": "finally"}]
    with FakeOpenAI(replies) as fake:
        response, _, _ = collect(client_for(settings, fake))
        assert response.content == "finally" and len(fake.requests) == 3


def test_retries_exhausted(settings):
    settings.max_retries = 1
    with FakeOpenAI([{"status": 503, "error": "overloaded"}] * 3) as fake:
        with pytest.raises(LLMError, match="503"):
            collect(client_for(settings, fake))
        assert len(fake.requests) == 2


def test_quota_errors_are_not_retried(settings):
    settings.max_retries = 3
    with FakeOpenAI([{"status": 429, "error": "You exceeded your current quota, please check your billing"}]) as fake:
        with pytest.raises(LLMError, match="quota"):
            collect(client_for(settings, fake))
        assert len(fake.requests) == 1


def test_reasoning_effort_and_images_fallbacks(settings):
    settings.reasoning_effort = "low"
    settings.vision = True
    messages = [{"role": "user", "content": [{"type": "text", "text": "what is this?"},
                                             {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]

    def handler(body, fake):
        if "reasoning_effort" in body:
            return {"status": 400, "error": "Unrecognized request argument supplied: reasoning_effort"}
        if isinstance(body["messages"][0]["content"], list):
            return {"status": 400, "error": "Image input is not supported by this model"}
        return {"content": "a cat"}
    with FakeOpenAI(handler=handler) as fake:
        client = client_for(settings, fake)
        response, _, _ = collect(client, messages, tools=[])
        assert response.content == "a cat" and len(fake.requests) == 3
        assert "[image omitted" in fake.requests[-1]["messages"][0]["content"]
    assert not client.send_reasoning_effort and not client.images_ok


def test_fixed_sampling_fallback(settings):
    settings.temperature, settings.top_p = 0.3, 0.9

    def handler(body, fake):
        if "temperature" in body or "top_p" in body:
            return {"status": 400, "error": "invalid temperature: only 1 is allowed for this model"}
        return {"content": "ok"}
    with FakeOpenAI(handler=handler) as fake:
        client = client_for(settings, fake)
        response, _, _ = collect(client)
        assert response.content == "ok" and len(fake.requests) == 2
    assert not client.send_sampling
    assert client.drain_notices() == ["The model rejected temperature and top_p; the model's defaults will be used."]


def test_vision_disabled_strips_images(settings):
    settings.vision = False
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"},
                                             {"type": "image_url", "image_url": {"url": "data:image/png;base64,A"}}]}]
    with FakeOpenAI([{"content": "ok"}]) as fake:
        collect(client_for(settings, fake), messages, tools=[])
        assert isinstance(fake.requests[0]["messages"][0]["content"], str)


def test_connection_error(settings):
    settings.base_url = "http://127.0.0.1:9/v1"
    with pytest.raises(LLMError, match="Connection to http://127.0.0.1:9/v1 failed"):
        LLMClient(settings).complete(USER)


def test_list_models(settings):
    with FakeOpenAI(models=("b-model", "a-model")) as fake:
        assert client_for(settings, fake).list_models() == ["a-model", "b-model"]


def test_reconfigure_resets_capabilities(settings):
    client = LLMClient(settings)
    client.text_mode = True
    client.stream_options_ok = False
    _ = client.client
    client.reconfigure()
    assert not client.text_mode and client.stream_options_ok and client._client is None

"""Wire-shape tests for the real providers, offline: a mock HTTP transport captures the request body."""
import json

import pytest

from providers import from_openai_message, load_env, redact, to_openai_messages, to_openai_tools

MSGS = [
    {"role": "user", "content": [{"type": "text", "text": "start"}]},
    {"role": "assistant", "model": "claude-opus-5-5", "content": [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "listing"},
        {"type": "tool_use", "id": "toolu_1", "name": "list_files", "input": {}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.md (10 bytes)"},
                                 {"type": "text", "text": "[fingerprint check, turn 2] ..."}]},
]
TOOLS = [{"name": "list_files", "description": "List", "input_schema": {"type": "object", "properties": {}}}]


def test_openai_translation_roundtrip():
    out = to_openai_messages("sys", MSGS)
    assert out[0] == {"role": "system", "content": "sys"}
    assert out[1] == {"role": "user", "content": "start"}
    assert out[2]["role"] == "assistant" and out[2]["content"] == "listing"
    assert out[2]["tool_calls"][0]["function"] == {"name": "list_files", "arguments": "{}"}
    assert out[3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "a.md (10 bytes)"}
    assert out[4]["role"] == "user" and "fingerprint" in out[4]["content"]
    assert "thinking" not in json.dumps(out)
    blocks = from_openai_message({"content": "hi", "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": '{"path": "a.md"}'}}]})
    assert blocks == [{"type": "text", "text": "hi"}, {"type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": "a.md"}}]
    assert to_openai_tools(TOOLS)[0]["function"]["parameters"] == TOOLS[0]["input_schema"]


def test_anthropic_wire_shape():
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    from providers import AnthropicProvider
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": "ok"}, {"type": "tool_use", "id": "toolu_2", "name": "read_file", "input": {"path": "a.md"}}],
            "stop_reason": "tool_use", "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 7, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 3}})

    http = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler))
    p = AnthropicProvider("claude-sonnet-5-5", effort="medium", http_client=http, api_key="test-key", max_retries=0)
    resp = p.complete("sys", MSGS, TOOLS, max_tokens=512, tool_choice="none")
    body = captured[0]
    assert body["model"] == "claude-sonnet-5-5" and body["system"] == "sys" and body["max_tokens"] == 512
    assert body["tools"][0]["name"] == "list_files" and body["tool_choice"] == {"type": "none"}
    assert body["output_config"] == {"effort": "medium"} and body["cache_control"] == {"type": "ephemeral"}
    assert "thinking" not in body  # omitted -> provider default
    # the other model's thinking block is stripped; visible history is byte-identical
    sent_assistant = body["messages"][1]["content"]
    assert [b["type"] for b in sent_assistant] == ["text", "tool_use"]
    assert body["messages"][2]["content"][0]["type"] == "tool_result"
    assert "model" not in body["messages"][1]
    assert resp.model == "claude-sonnet-5-5" and resp.stop_reason == "tool_use"
    assert resp.content[1]["name"] == "read_file" and resp.usage["cache_read_input_tokens"] == 5
    # same model -> thinking block replayed
    p2 = AnthropicProvider("claude-opus-5-5", http_client=http, api_key="test-key", max_retries=0)
    p2.complete("sys", MSGS, TOOLS)
    assert [b["type"] for b in captured[1]["messages"][1]["content"]] == ["thinking", "text", "tool_use"]


def test_env_loader_and_redaction(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nANTHROPIC_API_KEY=sk-ant-abcdefghijklmnop\nOPENROUTER_API_KEY='sk-or-zzzzzzzzzzzz'\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert load_env(env) == ["ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"]
    import os
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-zzzzzzzzzzzz"
    assert redact("key sk-ant-abcdefghijklmnop here") == "key <redacted-key> here"
    assert load_env(tmp_path / "missing.env") == []

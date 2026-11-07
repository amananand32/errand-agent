"""Grok / Groq adapter: message conversion and a full agent turn against a stubbed HTTP API."""
import json

import pytest

from errand_agent.llm import openai_compat as oc
from errand_agent.mocks.service import ScenarioConfig
from evals.harness import Env


def test_message_conversion_round_trip():
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "checking"},
                                              {"type": "tool_use", "id": "c1", "name": "list_bookings", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "{\"a\":1}",
                                          "is_error": True}]}]
    out = oc.to_openai_messages("SYS", msgs)
    assert out[0] == {"role": "system", "content": "SYS"}
    assert out[2]["tool_calls"][0]["function"]["name"] == "list_bookings"
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "ERROR: {\"a\":1}"}
    blocks = oc.from_openai_message({"content": None, "tool_calls": [
        {"id": "x", "function": {"name": "search_providers", "arguments": "{\"area\": \"HSR Layout\"}"}},
        {"id": "y", "function": {"name": "get_provider", "arguments": "not json"}}]})
    assert blocks[0]["input"] == {"area": "HSR Layout"} and "_unparseable_arguments" in blocks[1]["input"]


def test_missing_key_is_a_clear_config_error(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(oc.ModelConfigError, match="GROQ_API_KEY"):
        oc.OpenAICompatModel("groq")


def test_agent_books_through_groq_adapter(monkeypatch):
    """Scripted HTTP responses in OpenAI format drive a real agent turn end to end."""
    monkeypatch.setenv("GROQ_API_KEY", "test")
    m = oc.OpenAICompatModel("groq")
    sent = []

    def call(name, args, cid):
        return {"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}],
            "usage": {"prompt_tokens": 1500, "completion_tokens": 30}}

    req = "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500."
    script = iter([
        call("update_request", {"intent": "book", "service": "plumbing", "date_from": "2026-11-09",
                                "date_to": "2026-11-15", "earliest_time": "08:00", "latest_time": "12:00",
                                "budget_max": 1500, "evidence": "plumber for my kitchen tap"}, "c1"),
        call("search_providers", {"service": "plumbing", "area": "Indiranagar", "date_from": "2026-11-09",
                                  "date_to": "2026-11-15"}, "c2"),
        None,  # filled dynamically: propose the top ranked option
        {"choices": [{"finish_reason": "stop", "message": {"content": "Here is the best option."}}],
         "usage": {"prompt_tokens": 1900, "completion_tokens": 12}},
    ])

    def fake_post(payload):
        sent.append(payload)
        nxt = next(script)
        if nxt is None:
            ranked = json.loads(payload["messages"][-1]["content"])["ranked_options"]
            return call("propose_booking", {"provider_id": ranked[0]["provider_id"],
                                            "slot": ranked[0]["earliest_fitting_slot"]}, "c3")
        return nxt

    monkeypatch.setattr(m, "_post", fake_post)
    env = Env("groq_adapter", config=ScenarioConfig(book_outcomes=["success"]))
    env.agent.model = m
    r1 = env.say(req)
    assert r1.outcome == "proposed" and "**Confirm booking**" in r1.reply
    assert sent[0]["tools"][0]["type"] == "function" and sent[0]["model"] == "openai/gpt-oss-120b"
    assert sent[1]["messages"][-1]["role"] == "tool"          # tool result fed back in OpenAI format
    r2 = env.say("yes")                                         # approval is deterministic, no model call
    assert r2.outcome == "booked" and len(sent) == 4
    mc = env.ev("model_call")[0]
    assert mc["tokens_in"] == 1500 and mc["est_cost_usd"] == 0.0 and mc["token_counts_estimated"] is False


def test_model_not_found_falls_back_to_an_available_model(monkeypatch, capsys):
    """Reported by a user: Groq returned 404 model_not_found for the configured model."""
    monkeypatch.setenv("GROQ_API_KEY", "test")
    m = oc.OpenAICompatModel("groq", model="llama-3.3-70b-versatile")
    calls = []

    def fake_post(payload):
        calls.append(payload["model"])
        if payload["model"] == "llama-3.3-70b-versatile":
            raise oc.ModelNotFound('HTTP 404: {"error":{"code":"model_not_found"}}')
        return {"choices": [{"finish_reason": "stop", "message": {"content": "hi"}}], "usage": {}}

    monkeypatch.setattr(m, "_post", fake_post)
    monkeypatch.setattr(m, "list_models", lambda: ["openai/gpt-oss-20b", "whisper-large-v3"])
    r = m.complete("sys", [{"role": "user", "content": "hi"}], [])
    assert calls == ["llama-3.3-70b-versatile", "openai/gpt-oss-20b"] and m.name == "openai/gpt-oss-20b"
    assert r.text == "hi" and "switching to 'openai/gpt-oss-20b'" in capsys.readouterr().out


def test_model_not_found_without_alternative_is_a_clear_config_error(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    m = oc.OpenAICompatModel("groq")
    monkeypatch.setattr(m, "_post", lambda p: (_ for _ in ()).throw(oc.ModelNotFound("HTTP 404 model_not_found")))
    monkeypatch.setattr(m, "list_models", lambda: ["whisper-large-v3"])
    with pytest.raises(oc.ModelConfigError, match="check_llm --model groq --list"):
        m.complete("sys", [{"role": "user", "content": "hi"}], [])

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import deepseek_infra.infra.agent_runtime.multi_agent as multi_agent
import deepseek_infra.infra.gateway.deepseek_client as deepseek_client
import deepseek_infra.infra.observability.observability as observability


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _spans_by_name(trace: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(span["name"]): span for span in trace.get("spans", [])}


def test_prepare_deepseek_call_emits_context_subtree(tmp_settings) -> None:
    trace_id = observability.start_trace(kind="chat", title="trace tree")
    deepseek_client.prepare_deepseek_call(
        {"apiKey": "k", "model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "hi"}]},
        stream=False,
        trace_id=trace_id,
        parent_span_id="",
    )
    trace = observability.get_trace(trace_id)
    assert trace is not None
    spans = _spans_by_name(trace)
    assert "context.build" in spans
    assert "memory.retrieve" in spans
    # memory.retrieve is a child of context.build.
    assert spans["memory.retrieve"]["parentSpanId"] == spans["context.build"]["spanId"]
    # context.build hangs off the run root (no parent span).
    assert spans["context.build"]["parentSpanId"] == ""


def test_execute_agent_tier_nests_llm_span_under_agent_span(tmp_settings) -> None:
    trace_id = observability.start_trace(kind="agent", title="agent tree")
    payload = {"apiKey": "k", "model": "deepseek-v4-pro", "traceId": trace_id, "messages": [{"role": "user", "content": "q"}]}

    def fake_run_agent(_payload: dict[str, Any], *, agent_id: str, task: str, parent_span_id: str = "", **_kwargs: Any) -> dict[str, Any]:
        span = observability.start_span(trace_id, name="llm.deepseek", kind="deepseek_api", parent_span_id=parent_span_id)
        span.finish(status="ok", usage={"total_tokens": 7})
        return {"id": agent_id, "name": agent_id, "summary": "s", "usage": {"total_tokens": 7}}

    with patch.object(multi_agent, "run_agent", fake_run_agent):
        multi_agent.execute_agent_tier(
            payload,
            [{"id": "researcher", "task": "find facts"}],
            prior_outputs=[],
            search_budget=multi_agent.new_agent_search_budget(),
            emit_event=lambda _event: None,
        )

    trace = observability.get_trace(trace_id)
    assert trace is not None
    spans = _spans_by_name(trace)
    assert "agent.researcher" in spans
    assert "llm.deepseek" in spans
    assert spans["llm.deepseek"]["parentSpanId"] == spans["agent.researcher"]["spanId"]
    assert spans["agent.researcher"]["parentSpanId"] == ""


def test_call_deepseek_parents_spans_under_passed_span(tmp_settings) -> None:
    trace_id = observability.start_trace(kind="agent", title="call tree")
    agent_span = observability.start_span(trace_id, name="agent.researcher", kind="agent")
    agent_span.finish(status="ok")

    response = {"id": "r", "model": "deepseek-v4-pro", "choices": [{"message": {"content": "answer"}}], "usage": {}}
    with patch("urllib.request.urlopen", return_value=FakeResponse(json.dumps(response).encode("utf-8"))):
        deepseek_client.call_deepseek(
            {"apiKey": "k", "model": "expert", "traceId": trace_id, "messages": [{"role": "user", "content": "q"}]},
            budget_key="researcher",
            parent_span_id=agent_span.span_id,
        )

    trace = observability.get_trace(trace_id)
    assert trace is not None
    spans = trace["spans"]
    deepseek_span = next(span for span in spans if span["kind"] == "deepseek_api")
    semantic_span = next(span for span in spans if span["kind"] == "semantic_cache")
    context_span = next(span for span in spans if span["name"] == "context.build")
    assert deepseek_span["parentSpanId"] == agent_span.span_id
    assert semantic_span["parentSpanId"] == agent_span.span_id
    assert context_span["parentSpanId"] == agent_span.span_id


def test_plain_chat_keeps_top_level_spans(tmp_settings) -> None:
    # Without a parent span (plain chat), the LLM span stays a direct child of the
    # run (parentSpanId == "") — unchanged from before, just with a context subtree added.
    response = {"id": "r", "model": "deepseek-v4-pro", "choices": [{"message": {"content": "answer"}}], "usage": {}}
    payload = {"apiKey": "k", "model": "expert", "messages": [{"role": "user", "content": "q"}]}
    with patch("urllib.request.urlopen", return_value=FakeResponse(json.dumps(response).encode("utf-8"))):
        result = deepseek_client.call_deepseek(payload)
    trace_id = result["diagnostics"]["traceId"]
    trace = observability.get_trace(trace_id)
    assert trace is not None
    deepseek_span = next(span for span in trace["spans"] if span["kind"] == "deepseek_api")
    assert deepseek_span["parentSpanId"] == ""

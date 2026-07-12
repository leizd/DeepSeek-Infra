from __future__ import annotations

import io
import threading
import urllib.error
from datetime import datetime
from email.message import Message
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.gateway import deepseek_client as client
from deepseek_infra.infra.gateway.edge_inference import EdgeRouteDecision


def _payload() -> dict[str, Any]:
    return {"apiKey": "test", "messages": [{"role": "user", "content": "hello"}]}


def _route() -> EdgeRouteDecision:
    return EdgeRouteDecision(True, "local_forced", "local", "stub", {"modelName": "edge-test"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"error":{"message":"quota exhausted"}}', "quota exhausted"),
        ('{"error":{"type":"content_filter"}}', "content_filter"),
        ('{"message":"try later"}', "try later"),
        ('{"type":"overloaded"}', "overloaded"),
        ('{"other":true}', '{"other":true}'),
        ("", "Upstream stream error"),
    ],
)
def test_sse_error_message_handles_malformed_and_structured_errors(raw: str, expected: str) -> None:
    assert client.sse_error_message(raw) == expected


def test_chat_message_normalization_rejects_malformed_entries() -> None:
    messages: list[Any] = [
        None,
        {"role": "system", "content": "ignored"},
        {"role": "tool", "content": "missing id"},
        {"role": "tool", "tool_call_id": "call-1", "content": " ok "},
        {"role": "assistant", "content": "", "tool_calls": [{"name": "fetch_url", "arguments": {"url": "x"}}]},
        {"role": "user", "content": "", "attachments": [{"imageData": "data:image/png;base64," + "a" * 40}]},
    ]

    normalized = client.normalize_chat_messages(messages)

    assert normalized[0] == {"role": "tool", "tool_call_id": "call-1", "content": "ok"}
    assert normalized[1]["tool_calls"][0]["function"]["name"] == "fetch_url"
    assert normalized[2]["content"][0]["type"] == "image_url"


def test_tool_call_normalization_and_delta_merge_cover_invalid_shapes() -> None:
    assert client.normalize_tool_calls("bad") == []
    assert client.normalize_tool_calls([None, {"function": {}}, {"function": {"name": " do-work ", "arguments": {"b": 2, "a": 1}}}], stable_ids=True, canonical_arguments=True) == [
        {
            "id": "call_3_do_work",
            "type": "function",
            "function": {"name": "do-work", "arguments": '{"a":1,"b":2}'},
        }
    ]
    assert client.canonical_tool_arguments("not json ") == "not json"

    accumulator: dict[int, dict[str, Any]] = {}
    client.merge_stream_tool_call_deltas(accumulator, "bad")
    client.merge_stream_tool_call_deltas(
        accumulator,
        [None, {"index": "bad", "id": "call-x", "type": "function", "function": {"name": "fetch", "arguments": '{"a":'}},
         {"index": 0, "function": {"arguments": "1}"}}],
    )
    assert accumulator[0]["id"] == "call-x"
    assert accumulator[0]["function"] == {"name": "fetch", "arguments": '{"a":1}'}


@pytest.mark.parametrize(
    ("tool", "result", "needle"),
    [
        ("create_mindmap", {"downloadUrl": "/api/download?id=" + "a" * 32, "nodeCount": "bad", "outline": [{"label": "Root"}]}, "Mind map SVG"),
        ("create_document", {"downloadUrl": "/api/download?id=" + "b" * 32, "sectionCount": "bad", "format": "pdf"}, "PDF document"),
        ("create_pptx", {"downloadUrl": "/api/download?id=" + "c" * 32, "slideCount": "bad", "outline": [None, {"title": "Intro"}]}, "PPT generated"),
    ],
)
def test_artifact_links_tolerate_invalid_metadata(tool: str, result: dict[str, Any], needle: str) -> None:
    assert needle in client.artifact_link_text(tool, result, base_url="http://127.0.0.1:8000")


def test_download_url_helpers_reject_invalid_base_and_preserve_external_values() -> None:
    assert client.pptx_download_base_url({"localBaseUrl": "not-a-url"}) == ""
    assert client.pptx_download_base_url({"localBaseUrl": "ftp://host/x"}) == ""
    assert client.pptx_download_base_url({"localBaseUrl": "https://host:8443/path"}) == "https://host:8443"
    assert client.absolute_download_url("https://other.test/file.zip", base_url="https://local") == "https://other.test/file.zip"
    assert client.absolute_download_url("https://other.test/api/download?id=1", base_url="https://local") == "https://local/api/download?id=1"


@pytest.mark.parametrize(("score", "expected"), [("none", 1.0), ("2.5", 1.0), ("-0.2", 0.2), ("0.37", 0.37)])
def test_judge_score_is_safe_and_clamped(score: str, expected: float) -> None:
    assert client._parse_judge_score(score) == expected


def test_edge_fallback_swallows_routing_errors_and_rejects_complex_route() -> None:
    with patch.object(client, "select_edge_route", side_effect=AppError("bad route", code=ErrorCode.INVALID_PAYLOAD)):
        assert client.edge_fallback_route(_payload()) is None
    rejected = EdgeRouteDecision(False, "complex_task_cloud", "auto", "stub", {})
    with patch.object(client, "select_edge_route", return_value=rejected):
        assert client.edge_fallback_route(_payload()) is None


def test_stream_edge_inference_reports_app_and_internal_errors() -> None:
    events: list[dict[str, Any]] = []
    with patch.object(client.edge_manager, "stream", side_effect=AppError("model unavailable", code=ErrorCode.UPSTREAM_FAILURE)):
        client.stream_edge_inference(_payload(), events.append, _route())
    assert events[-1]["code"] == ErrorCode.UPSTREAM_FAILURE.value

    events.clear()
    with patch.object(client.edge_manager, "stream", side_effect=RuntimeError("backend crashed")):
        client.stream_edge_inference(_payload(), events.append, _route())
    assert events[-1] == {"type": "error", "error": "Edge inference error", "code": ErrorCode.INTERNAL.value}


def test_stream_edge_inference_cancellation_is_not_reported_as_failure() -> None:
    events: list[dict[str, Any]] = []
    cancelled = threading.Event()
    cancelled.set()
    with patch.object(client.edge_manager, "stream", return_value=iter(["ignored"])):
        client.stream_edge_inference(_payload(), events.append, _route(), cancel_event=cancelled)
    assert not any(event.get("type") == "error" for event in events)


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (AppError("bad payload", code=ErrorCode.INVALID_PAYLOAD), ErrorCode.INVALID_PAYLOAD.value),
        (urllib.error.URLError("timed out"), ErrorCode.UPSTREAM_TIMEOUT.value),
        (TimeoutError(), ErrorCode.UPSTREAM_TIMEOUT.value),
        (OSError("connection reset"), ErrorCode.UPSTREAM_FAILURE.value),
        (RuntimeError("bug"), ErrorCode.INTERNAL.value),
    ],
)
def test_stream_deepseek_maps_preflight_failures_to_stable_error_codes(exc: Exception, code: str) -> None:
    events: list[dict[str, Any]] = []
    with (
        patch.object(client, "edge_route_for_payload", side_effect=exc),
        patch.object(client, "edge_fallback_route", return_value=None),
    ):
        client.stream_deepseek(_payload(), events.append)
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == code


def test_stream_deepseek_maps_http_content_filter_error() -> None:
    events: list[dict[str, Any]] = []
    error = urllib.error.HTTPError("https://api.test", 400, "bad", Message(), io.BytesIO(b'{"error":{"message":"content risk"}}'))
    with patch.object(client, "edge_route_for_payload", side_effect=error):
        client.stream_deepseek(_payload(), events.append)
    assert events[-1]["code"] == ErrorCode.UPSTREAM_CONTENT_RISK.value


def test_stream_emit_disconnect_cancels_edge_request() -> None:
    cancelled = threading.Event()

    def disconnected(_: dict[str, Any]) -> None:
        raise BrokenPipeError

    with (
        patch.object(client, "edge_route_for_payload", return_value=_route()),
        patch.object(client.edge_manager, "stream", return_value=iter(["text"])),
    ):
        client.stream_deepseek(_payload(), disconnected, cancel_event=cancelled)
    assert cancelled.is_set()


class _Response:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.data


def _cloud_route() -> EdgeRouteDecision:
    return EdgeRouteDecision(False, "cloud", "auto", "deepseek", {})


def test_call_deepseek_returns_semantic_cache_hit_without_network() -> None:
    cached = {"id": "cached", "model": "deepseek-v4-pro", "content": "cached answer", "reasoning": "why", "usage": {"total_tokens": 4}}
    lookup = SimpleNamespace(hit=True, result=cached, diagnostics={"checked": True, "hit": True})
    with (
        patch.object(client, "edge_route_for_payload", return_value=_cloud_route()),
        patch.object(client, "semantic_cache_lookup", return_value=lookup),
        patch("urllib.request.urlopen") as urlopen,
    ):
        result = client.call_deepseek(_payload())
    urlopen.assert_not_called()
    assert result["content"] == "cached answer"
    assert result["diagnostics"]["semanticCache"]["hit"] is True


def test_stream_deepseek_replays_semantic_cache_reasoning_and_content() -> None:
    cached = {"id": "cached", "model": "deepseek-v4-pro", "content": "cached answer", "reasoning": "why", "usage": {"total_tokens": 4}}
    lookup = SimpleNamespace(hit=True, result=cached, diagnostics={"checked": True, "hit": True})
    events: list[dict[str, Any]] = []
    with (
        patch.object(client, "edge_route_for_payload", return_value=_cloud_route()),
        patch.object(client, "semantic_cache_lookup", return_value=lookup),
        patch("urllib.request.urlopen") as urlopen,
    ):
        client.stream_deepseek(_payload(), events.append)
    urlopen.assert_not_called()
    assert [event["type"] for event in events if event["type"] in {"reasoning", "content", "done"}] == ["reasoning", "content", "done"]
    assert events[-1]["content"] == "cached answer"


def test_call_deepseek_maps_http_and_url_failures() -> None:
    http_error = urllib.error.HTTPError("https://api.test", 429, "bad", Message(), io.BytesIO(b'{"error":{"message":"content risk"}}'))
    with (
        patch.object(client, "edge_route_for_payload", return_value=_cloud_route()),
        patch.object(client, "open_with_resiliency", side_effect=http_error),
        pytest.raises(AppError) as caught,
    ):
        client.call_deepseek(_payload())
    assert caught.value.code == ErrorCode.UPSTREAM_CONTENT_RISK

    with (
        patch.object(client, "edge_route_for_payload", return_value=_cloud_route()),
        patch.object(client, "edge_fallback_route", return_value=None),
        patch.object(client, "open_with_resiliency", side_effect=urllib.error.URLError("timed out")),
        pytest.raises(AppError) as caught,
    ):
        client.call_deepseek(_payload())
    assert caught.value.code == ErrorCode.UPSTREAM_TIMEOUT


def test_call_deepseek_rejects_invalid_upstream_response_shape() -> None:
    with (
        patch.object(client, "edge_route_for_payload", return_value=_cloud_route()),
        patch.object(client, "open_with_resiliency", return_value=_Response(b'{"choices":[]}')),
        pytest.raises(AppError),
    ):
        client.call_deepseek(_payload())


def test_call_edge_inference_finishes_error_trace_before_reraising() -> None:
    with patch.object(client.edge_manager, "complete", side_effect=RuntimeError("model load failed")), pytest.raises(RuntimeError):
        client.call_edge_inference(_payload(), _route())


def test_budget_and_final_answer_boundaries() -> None:
    body = {"messages": [], "tool_choice": "auto"}
    forced = client.force_final_answer_without_tools(body)
    assert "tool_choice" not in forced
    assert forced["messages"][-1]["content"] == client.TOOL_BUDGET_EXHAUSTED_PROMPT

    tokens = client.TokenBudget(total_limit=0, per_agent_limit=0)
    tokens.record(7, "worker")
    assert tokens.exhausted() is False
    assert tokens.agent_exhausted("worker") is False


def test_request_validation_and_flash_fallback_boundaries() -> None:
    with pytest.raises(AppError, match="Unsupported model"):
        client.validate_deepseek_payload({**_payload(), "model": "unknown-model"})

    payload = {
        **_payload(),
        "model": "deepseek-v4-flash",
        "temperature": "invalid",
        "contextSummaryGeneration": object(),
        "toolsEnabled": False,
    }
    prepared = client.build_deepseek_request(payload, stream=False)
    assert prepared.body["temperature"] == 1.0
    assert prepared.diagnostics["contextSummaryGeneration"] == 0


def test_tool_filter_aliases_and_disabled_forcing(monkeypatch: pytest.MonkeyPatch) -> None:
    definitions = [
        {"function": {"name": "create_pptx"}},
        {"function": {"name": "web_search"}},
        {"function": {"name": "other"}},
    ]
    monkeypatch.setattr(client, "agent_tool_definitions", lambda: definitions)
    assert client.tools_for_payload({"allowedTools": ["other"]}) == [definitions[2]]
    assert client.should_force_create_pptx({"messages": [{"role": "user", "content": "create a ppt presentation"}]})
    assert client.has_create_pptx_tool(definitions)
    assert client.forced_artifact_tool_name({"toolsEnabled": False}, definitions) == ""


def test_dynamic_context_naive_time_memory_notice_and_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "UTC time:" in client.format_current_time_context(datetime(2026, 7, 12, 8, 0, 0))
    monkeypatch.setattr(client, "format_memory_notice", lambda notice: f"notice:{notice}")
    context = client.build_dynamic_turn_context({}, {"notice": "saved"}, tools_enabled=False)
    assert "notice:saved" in context
    messages = [{"role": "assistant", "content": "answer"}]
    copied = client.append_context_to_latest_user(messages, "context")
    assert copied[-1] == {"role": "system", "content": "context"}
    plain_copy = client.append_context_to_latest_user(messages, "")
    assert plain_copy == messages and plain_copy is not messages


def test_artifact_corruption_invalid_urls_and_generation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "messages": [
            {"role": "tool", "content": "not-json"},
            {"role": "tool", "content": "[]"},
            {"role": "tool", "content": '{"ok":false}'},
        ]
    }
    assert client.terminal_artifact_result_from_messages(body) is None
    assert client.pptx_download_base_url({"localBaseUrl": "http://[::1"}) == ""
    assert client.absolute_download_url("http://[::1/api/download?id=bad", base_url="https://local") == "http://[::1/api/download?id=bad"

    payload = {"messages": [{"role": "user", "content": "create a ppt presentation"}]}
    monkeypatch.setattr(client, "create_presentation_from_text", lambda *_: (_ for _ in ()).throw(AppError("failed")))
    assert client.ensure_pptx_response(payload, "draft", {}) == ("draft", False)


def test_ollama_draft_and_judge_failure_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = client.model_router.CascadePlan(
        enabled=True,
        draft_model="ollama/test",
        refine_model="deepseek-reasoner",
        draft_provider="ollama",
        judge=True,
        judge_model="deepseek-v4-flash",
        judge_threshold=0.5,
        min_chars=1,
    )
    provider = SimpleNamespace(chat=lambda payload: (_ for _ in ()).throw(RuntimeError("offline")))
    from deepseek_infra.infra.gateway.providers import registry

    monkeypatch.setattr(registry, "resolve_provider", lambda _: provider)
    draft = client._call_ollama_draft(_payload(), plan)
    assert draft["_draftError"] == "offline"
    monkeypatch.setattr(client, "call_deepseek", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("judge down")))
    assert client.judge_draft(_payload(), "candidate", plan) == 1.0


def test_image_parts_dynamic_context_and_edge_validation_empty_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    assert client._image_content_parts({"attachments": "bad"}) == []
    assert client._image_content_parts({"attachments": [None, {"imageData": "data:image/png;base64,short"}]}) == []
    assert client._has_image_content([{"content": "text"}]) is False
    monkeypatch.setattr(client, "search_tool_enabled", lambda _payload: False)
    monkeypatch.setattr(client, "presentation_intent_requested", lambda _payload: False)
    assert client.CURRENT_TIME_CONTEXT_HEADER in client.build_dynamic_turn_context({}, {}, tools_enabled=False)
    with pytest.raises(AppError):
        client.validate_edge_payload({"messages": []})
    route = EdgeRouteDecision(True, "local_forced", "local", "stub", {})
    monkeypatch.setattr(client, "select_edge_route", lambda *_args, **_kwargs: route)
    monkeypatch.setattr(client, "validate_edge_payload", lambda _payload: None)
    assert client.preflight_chat_payload(_payload()) is None


def test_edge_messages_system_filter_and_stream_empty_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "prepare_memory_state", lambda _payload: {})
    monkeypatch.setattr(client, "build_dynamic_turn_context", lambda *_args, **_kwargs: "context")
    messages, diagnostics = client.build_edge_messages(
        {
            **_payload(),
            "systemPrompt": "system",
            "messages": [
                {"role": "system", "content": "inner"},
                {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "x", "arguments": "{}"}}]},
                {"role": "user", "content": "hello"},
            ],
        }
    )
    assert messages[0] == {"role": "system", "content": "system"}
    assert all(not message.get("tool_calls") for message in messages)
    assert diagnostics["dynamicContextChars"] == len("context")
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(client.edge_manager, "stream", lambda *_args, **_kwargs: iter(["", None, "answer"]))
    client.stream_edge_inference(_payload(), events.append, _route())
    assert [event["type"] for event in events].count("content") == 1


def test_search_if_needed_disabled_missing_query_cached_and_error_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.search_if_needed({}) is None
    monkeypatch.setattr(client, "forced_search_mode", lambda _payload: False)
    assert client.search_if_needed({"searchEnabled": True}) is None
    monkeypatch.setattr(client, "forced_search_mode", lambda _payload: True)
    monkeypatch.setattr(client, "latest_user_query", lambda _payload: "")
    with pytest.raises(AppError):
        client.search_if_needed({"searchEnabled": True})

    notes: list[str] = []
    monkeypatch.setattr(client, "latest_user_query", lambda _payload: "query")
    monkeypatch.setattr(client, "search_queries_for", lambda _query: ["first query"])
    monkeypatch.setattr(client, "search_multiple", lambda *_args, **_kwargs: {"results": [{"url": "x"}], "cached": True})
    client.search_if_needed({"searchEnabled": True}, system_note_callback=notes.append)
    assert len(notes) == 2
    notes.clear()
    monkeypatch.setattr(client, "search_multiple", lambda *_args, **_kwargs: {"status": "error", "rounds": [None, {"error": "offline"}]})
    client.search_if_needed({"searchEnabled": True}, system_note_callback=notes.append)
    assert "offline" in notes[-1]


def test_web_search_turn_initial_round_and_progress_corruption(monkeypatch: pytest.MonkeyPatch) -> None:
    progress: list[dict[str, Any]] = []
    initial = {"rounds": [None, {"round": "bad"}, {"round": 0}, {"round": 2, "query": " cached ", "results": []}]}
    callback, current = client.web_search_callback_for_turn(_payload(), initial, progress_callback=progress.append, turn_limit=0)
    limited = callback("new", "general")
    assert limited["status"] == "error"
    assert current() is not None


def test_stream_deepseek_url_fallback_and_created_trace_finishing(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, Any]] = []
    trace = SimpleNamespace(trace_id="trace", created=True)
    fallback = EdgeRouteDecision(True, "cloud_unavailable_simple_local", "local", "stub", {})
    monkeypatch.setattr(client, "ensure_trace", lambda *_args, **_kwargs: trace)
    monkeypatch.setattr(client, "edge_route_for_payload", lambda _payload: (_ for _ in ()).throw(urllib.error.URLError("offline")))
    monkeypatch.setattr(client, "edge_fallback_route", lambda _payload: fallback)
    monkeypatch.setattr(client, "stream_edge_inference", lambda _payload, emit, *_args, **_kwargs: emit({"type": "done"}))
    finished: list[dict[str, Any]] = []
    monkeypatch.setattr(client, "finish_trace", lambda *_args, **kwargs: finished.append(kwargs))
    client.stream_deepseek(_payload(), events.append)
    assert events[-1]["type"] == "done"
    assert finished == []


def test_sse_error_fallback_for_array_and_empty_message() -> None:
    assert client.sse_error_message("[]") == "[]"
    assert client.sse_error_message('{"error":{},"message":"top"}') == "top"

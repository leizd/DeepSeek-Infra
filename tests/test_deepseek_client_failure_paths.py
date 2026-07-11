from __future__ import annotations

import io
import threading
import urllib.error
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

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from unittest.mock import patch

import pytest

import deepseek_infra.infra.gateway.deepseek_client as deepseek_client
import deepseek_infra.infra.gateway.resiliency as resiliency
from deepseek_infra.infra.gateway.context_manager import stable_json_dumps
from deepseek_infra.infra.gateway.deepseek_client import build_deepseek_request, call_deepseek, stream_deepseek
from deepseek_infra.infra.gateway.edge_inference import EdgeRouteDecision


def test_stable_json_dumps_sorts_keys_without_spacing() -> None:
    assert stable_json_dumps({"b": 1, "a": {"d": 2, "c": 3}}) == '{"a":{"c":3,"d":2},"b":1}'


def test_context_manager_sorts_tools_and_reports_diagnostics() -> None:
    prepared = build_deepseek_request(
        {"apiKey": "test", "model": "expert", "messages": [{"role": "user", "content": "hi"}]},
        stream=False,
    )

    tool_names = [tool["function"]["name"] for tool in prepared.body["tools"]]
    assert tool_names == sorted(tool_names)
    assert prepared.diagnostics["contextManager"]["enabled"] is True
    assert prepared.diagnostics["contextManager"]["toolOrder"] == tool_names


def test_context_manager_applies_sliding_window_only_with_summary() -> None:
    messages = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"message-{index}"}
        for index in range(50)
    ]

    prepared = build_deepseek_request(
        {
            "apiKey": "test",
            "model": "expert",
            "toolsEnabled": False,
            "contextSummary": "Earlier conversation summary.",
            "messages": messages,
        },
        stream=False,
    )

    context_diag = prepared.diagnostics["contextManager"]
    assert context_diag["slidingWindowApplied"] is True
    assert context_diag["droppedMessages"] > 0
    assert len(prepared.body["messages"]) <= 36
    assert prepared.body["messages"][-1]["role"] == "system"


def test_open_with_resiliency_retries_urlerror_and_records_queue(tmp_settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resiliency, "GATEWAY_REQUEST_QUEUE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(resiliency, "GATEWAY_REQUEST_QUEUE_INITIAL_BACKOFF_SECONDS", 0.0)
    attempts: list[dict[str, Any]] = []
    calls = {"count": 0}

    def fake_urlopen(request: urllib.request.Request, timeout: int | float) -> FakeResponse:
        calls["count"] += 1
        if calls["count"] == 1:
            raise urllib.error.URLError("offline")
        return FakeResponse(b"ok")

    monkeypatch.setattr(resiliency.urllib.request, "urlopen", fake_urlopen)

    request = urllib.request.Request("https://api.example.test", data=b"{}")
    response = resiliency.open_with_resiliency(
        request,
        timeout=1,
        kind="test",
        payload={"fingerprint": "retry-test"},
        diagnostics_callback=attempts.append,
    )

    assert response.read() == b"ok"
    assert calls["count"] == 2
    assert attempts[-1]["status"] == "succeeded"
    assert attempts[-1]["retryCount"] == 1
    status = resiliency.request_queue_status()
    assert status["counts"]["succeeded"] == 1


def test_open_with_resiliency_marks_failed_after_max_attempts(tmp_settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resiliency, "GATEWAY_REQUEST_QUEUE_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(resiliency, "GATEWAY_REQUEST_QUEUE_INITIAL_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(resiliency.urllib.request, "urlopen", lambda request, timeout: (_ for _ in ()).throw(urllib.error.URLError("offline")))

    with pytest.raises(urllib.error.URLError):
        resiliency.open_with_resiliency(
            urllib.request.Request("https://api.example.test", data=b"{}"),
            timeout=1,
            kind="test",
            payload={"fingerprint": "fail-test"},
        )

    status = resiliency.request_queue_status()
    assert status["counts"]["failed"] == 1


def test_call_deepseek_retries_transient_network_failure(tmp_settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resiliency, "GATEWAY_REQUEST_QUEUE_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(resiliency, "GATEWAY_REQUEST_QUEUE_INITIAL_BACKOFF_SECONDS", 0.0)
    cloud_route = EdgeRouteDecision(False, "complex_task_cloud", "auto", "llama_cpp", {"modelName": "local"})
    response = {
        "id": "retry-ok",
        "model": "deepseek-v4-pro",
        "choices": [{"message": {"content": "answer"}}],
        "usage": {"prompt_cache_hit_tokens": 10, "prompt_cache_miss_tokens": 5},
    }

    with (
        patch.object(deepseek_client, "edge_route_for_payload", return_value=cloud_route),
        patch("urllib.request.urlopen", side_effect=[urllib.error.URLError("offline"), FakeResponse(json.dumps(response).encode("utf-8"))]),
    ):
        result = call_deepseek(
            {
                "apiKey": "test",
                "model": "expert",
                "toolsEnabled": False,
                "messages": [{"role": "user", "content": "hard question"}],
            }
        )

    assert result["content"] == "answer"
    assert result["diagnostics"]["gatewayResiliency"]["retryCount"] == 1
    assert result["diagnostics"]["gatewayResiliency"]["lastStatus"] == "succeeded"


def test_stream_deepseek_retries_before_first_chunk(tmp_settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resiliency, "GATEWAY_REQUEST_QUEUE_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(resiliency, "GATEWAY_REQUEST_QUEUE_INITIAL_BACKOFF_SECONDS", 0.0)
    cloud_route = EdgeRouteDecision(False, "complex_task_cloud", "auto", "llama_cpp", {"modelName": "local"})
    chunk = {
        "id": "stream-retry-ok",
        "model": "deepseek-v4-pro",
        "choices": [{"delta": {"content": "stream answer"}}],
        "usage": {"prompt_cache_hit_tokens": 1, "prompt_cache_miss_tokens": 1},
    }
    events: list[dict[str, Any]] = []

    with (
        patch.object(deepseek_client, "edge_route_for_payload", return_value=cloud_route),
        patch(
            "urllib.request.urlopen",
            side_effect=[
                urllib.error.URLError("offline"),
                FakeStream([f"data: {json.dumps(chunk)}\n".encode("utf-8"), b"data: [DONE]\n"]),
            ],
        ),
    ):
        stream_deepseek(
            {
                "apiKey": "test",
                "model": "expert",
                "toolsEnabled": False,
                "messages": [{"role": "user", "content": "hard question"}],
            },
            events.append,
        )

    done = [event for event in events if event.get("type") == "done"][0]
    notes = [event for event in events if event.get("type") == "system_note"]
    assert done["content"] == "stream answer"
    assert done["diagnostics"]["gatewayResiliency"]["retryCount"] == 1
    assert any("Gateway queue" in str(note.get("text")) for note in notes)


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return self.data


class FakeStream:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines

    def __enter__(self) -> "FakeStream":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def __iter__(self) -> object:
        return iter(self.lines)

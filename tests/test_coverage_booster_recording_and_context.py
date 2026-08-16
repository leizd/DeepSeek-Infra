"""Targeted test coverage boosters for agent_recording and context_manager."""

from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.infra.evaluation import agent_recording
from deepseek_infra.infra.gateway import context_manager


def test_agent_recording_normalization(tmp_settings: Path) -> None:
    # 1. strip_volatile
    nested = {
        "runId": "run_123",
        "timestamp": "2026-08-15T00:00:00Z",
        "data": {
            "traceId": "tr_1",
            "value": 42,
            "items": [{"spanId": "sp_1", "text": "hello"}],
        },
    }
    cleaned = agent_recording.strip_volatile(nested)
    assert "runId" not in cleaned
    assert "timestamp" not in cleaned
    assert cleaned["data"]["value"] == 42
    assert "spanId" not in cleaned["data"]["items"][0]
    assert cleaned["data"]["items"][0]["text"] == "hello"

    # 2. Number parsing helpers
    assert agent_recording._as_float("3.14") == 3.14
    assert agent_recording._as_float("invalid", default=1.5) == 1.5
    assert agent_recording._as_int("100") == 100
    assert agent_recording._as_int("invalid", default=5) == 5

    # 3. Tool normalization
    assert agent_recording.normalize_tools(None) == []
    assert agent_recording.normalize_tools("single_tool") == ["single_tool"]
    assert agent_recording.normalize_tools([{"name": "t1"}, {"tool": "t2"}, {"toolName": "t3"}]) == ["t1", "t2", "t3"]

    # 4. Usage and trace normalization
    usage = agent_recording.normalize_usage({"prompt_tokens": 10, "completion_tokens": 20, "costUsd": 0.005})
    assert usage["inputTokens"] == 10
    assert usage["outputTokens"] == 20
    assert usage["estimatedCostUsd"] == 0.005

    trace = agent_recording.normalize_trace({"agent_count": 2, "retry_count": 1})
    assert trace["agentCount"] == 2
    assert trace["retryCount"] == 1

    # 5. Prediction normalization and error handling
    with pytest.raises(agent_recording.AgentRecordingError):
        agent_recording.normalize_prediction("not_a_dict")  # type: ignore

    with pytest.raises(agent_recording.AgentRecordingError):
        agent_recording.normalize_prediction({})  # missing id

    pred_ok = agent_recording.normalize_prediction({"id": "pred_1", "status": "completed", "final": "Answer"})
    assert pred_ok["id"] == "pred_1"
    assert pred_ok["failed"] is False

    pred_failed = agent_recording.normalize_prediction({"id": "pred_2", "failed": True})
    assert pred_failed["status"] == "failed"
    assert pred_failed["failed"] is True

    # 6. Golden task validation
    agent_recording.validate_golden_tasks([{"id": "task_1"}, {"id": "task_2"}])

    with pytest.raises(agent_recording.AgentRecordingError):
        agent_recording.validate_golden_tasks(["not_a_dict"])  # type: ignore

    with pytest.raises(agent_recording.AgentRecordingError):
        agent_recording.validate_golden_tasks([{"id": ""}])

    with pytest.raises(agent_recording.AgentRecordingError):
        agent_recording.validate_golden_tasks([{"id": "t1"}, {"id": "t1"}])


def test_context_manager_helpers(tmp_settings: Path) -> None:
    # 1. stable_json_dumps
    json_str = context_manager.stable_json_dumps({"b": 2, "a": 1})
    assert json_str == '{"a":1,"b":2}'

    # 2. Tool helpers
    assert context_manager.tool_sort_key("not_a_dict") == ("~", "")
    assert context_manager.tool_name({"function": "not_a_dict"}) == ""
    assert context_manager.tool_name({"function": {"name": "my_tool"}}) == "my_tool"

    # 3. Body management
    body = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "You are an assistant"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "system", "content": "Dynamic context"},
        ],
        "tools": [
            {"type": "function", "function": {"name": "tool_z"}},
            {"type": "function", "function": {"name": "tool_a"}},
        ],
    }
    managed, diag = context_manager.manage_request_body(body, allow_sliding_window=False)
    assert diag["enabled"] is True
    assert diag["toolOrder"] == ["tool_a", "tool_z"]
    assert diag["hasFrontSystemPrompt"] is True
    assert diag["hasTrailingDynamicContext"] is True

    # 4. Sliding window
    managed_sw, diag_sw = context_manager.manage_request_body(body, allow_sliding_window=True)
    assert diag_sw["slidingWindowAllowed"] is True

from __future__ import annotations

import pytest

from deepseek_infra.infra.evaluation import agent_recording


def test_normalize_prediction_strips_volatile_fields_and_maps_aliases() -> None:
    prediction = {
        "id": "agent_001",
        "runId": "run-volatile",
        "timestamp": "2026-06-27T00:00:00Z",
        "toolCalls": [{"name": "search_files", "spanId": "span-volatile"}, {"name": "python_eval"}, {"name": "python_eval"}],
        "final": "bug found",
        "status": "completed",
        "latency_ms": "1234",
        "usage": {"inputTokens": "100", "outputTokens": "20", "estimatedCostUsd": "0.0012"},
        "trace": {"traceId": "trace-volatile", "agentCount": 4, "retryCount": 1, "toolErrorCount": 0},
    }

    normalized = agent_recording.normalize_prediction(prediction)

    assert normalized["id"] == "agent_001"
    assert normalized["tools"] == ["search_files", "python_eval"]
    assert normalized["final"] == "bug found"
    assert normalized["answer"] == "bug found"
    assert normalized["failed"] is False
    assert normalized["latencyMs"] == 1234.0
    assert normalized["usage"]["prompt_tokens"] == 100
    assert normalized["usage"]["completion_tokens"] == 20
    assert normalized["usage"]["estimatedCostUsd"] == 0.0012
    assert normalized["trace"] == {"agentCount": 4, "retryCount": 1, "toolErrorCount": 0}
    assert "runId" not in normalized
    assert "traceId" not in str(normalized)
    assert "spanId" not in str(normalized)


def test_normalize_prediction_requires_id() -> None:
    with pytest.raises(agent_recording.AgentRecordingError):
        agent_recording.normalize_prediction({"final": "x"})


def test_validate_golden_tasks_rejects_missing_or_duplicate_ids() -> None:
    with pytest.raises(agent_recording.AgentRecordingError):
        agent_recording.validate_golden_tasks([{"task": "missing id"}])
    with pytest.raises(agent_recording.AgentRecordingError):
        agent_recording.validate_golden_tasks([{"id": "a"}, {"id": "a"}])

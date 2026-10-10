"""Normalize recorded Agent predictions for stable offline replay."""

from __future__ import annotations

from typing import Any

VOLATILE_KEYS = {
    "runId",
    "traceId",
    "spanId",
    "eventId",
    "requestId",
    "createdAt",
    "startedAt",
    "completedAt",
    "updatedAt",
    "timestamp",
    "ts",
}

SUCCEEDED_STATUSES = {"succeeded", "success", "completed", "complete", "ok", "passed", "pass"}
FAILED_STATUSES = {"failed", "failure", "error", "errored", "timeout", "canceled", "cancelled"}


class AgentRecordingError(ValueError):
    """Raised when a recorded prediction cannot be replayed deterministically."""


def strip_volatile(value: Any) -> Any:
    """Recursively drop fields that change on every Agent run."""
    if isinstance(value, dict):
        return {str(key): strip_volatile(item) for key, item in value.items() if str(key) not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [strip_volatile(item) for item in value]
    return value


def _as_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, *, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _first_int(data: dict[str, Any], *keys: str) -> int:
    for key in keys:
        if key in data:
            return _as_int(data.get(key))
    return 0


def _first_float(data: dict[str, Any], *keys: str) -> float:
    for key in keys:
        if key in data:
            return _as_float(data.get(key))
    return 0.0


def normalize_tools(value: Any) -> list[str]:
    """Return tool names in replay order while preserving first occurrence only."""
    raw_items: list[Any]
    if isinstance(value, list):
        raw_items = value
    elif value:
        raw_items = [value]
    else:
        raw_items = []

    tools: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if isinstance(item, dict):
            name = _as_str(item.get("name") or item.get("tool") or item.get("toolName"))
        else:
            name = _as_str(item)
        if name and name not in seen:
            tools.append(name)
            seen.add(name)
    return tools


def normalize_usage(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    input_tokens = _first_int(data, "inputTokens", "prompt_tokens", "promptTokens", "input_tokens")
    output_tokens = _first_int(data, "outputTokens", "completion_tokens", "completionTokens", "output_tokens")
    estimated_cost = _first_float(data, "estimatedCostUsd", "estimated_cost_usd", "costUsd")
    normalized: dict[str, Any] = {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
    }
    if estimated_cost:
        normalized["estimatedCostUsd"] = round(estimated_cost, 6)
    return normalized


def normalize_trace(value: Any) -> dict[str, int]:
    data = strip_volatile(value if isinstance(value, dict) else {})
    if not isinstance(data, dict):
        return {"agentCount": 0, "retryCount": 0, "toolErrorCount": 0}
    return {
        "agentCount": _first_int(data, "agentCount", "agent_count"),
        "retryCount": _first_int(data, "retryCount", "retry_count"),
        "toolErrorCount": _first_int(data, "toolErrorCount", "tool_error_count"),
    }


def normalize_prediction(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise AgentRecordingError("prediction must be an object")
    cleaned = strip_volatile(row)
    if not isinstance(cleaned, dict):
        raise AgentRecordingError("prediction must be an object")

    prediction_id = _as_str(cleaned.get("id"))
    if not prediction_id:
        raise AgentRecordingError("prediction id is required")

    final = _as_str(cleaned.get("final") or cleaned.get("answer") or cleaned.get("content"))
    status = _as_str(cleaned.get("status")).lower()
    failed = bool(cleaned.get("failed"))
    if not status:
        status = "failed" if failed else "succeeded"
    if status in FAILED_STATUSES:
        failed = True
    elif status in SUCCEEDED_STATUSES:
        failed = False

    tools = normalize_tools(cleaned.get("tools") or cleaned.get("toolCalls") or cleaned.get("tool_calls"))
    normalized = {
        "id": prediction_id,
        "task": _as_str(cleaned.get("task")),
        "model": _as_str(cleaned.get("model")),
        "tools": tools,
        "final": final,
        "answer": final,
        "content": final,
        "status": status,
        "failed": failed,
        "latencyMs": _as_float(cleaned.get("latencyMs") or cleaned.get("latency_ms")),
        "usage": normalize_usage(cleaned.get("usage")),
        "trace": normalize_trace(cleaned.get("trace")),
    }
    return normalized


def normalize_predictions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_prediction(row) for row in rows]


def validate_golden_tasks(rows: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise AgentRecordingError(f"golden row {index} must be an object")
        task_id = _as_str(row.get("id"))
        if not task_id:
            raise AgentRecordingError(f"golden row {index} is missing id")
        if task_id in seen:
            raise AgentRecordingError(f"duplicate golden id: {task_id}")
        seen.add(task_id)

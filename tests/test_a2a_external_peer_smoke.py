from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest


def _load_smoke() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke_a2a_external_peer.py"
    spec = importlib.util.spec_from_file_location("smoke_a2a_external_peer_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agent_card_missing_fields_fails() -> None:
    smoke = _load_smoke()
    with pytest.raises(smoke.SmokeFailure, match="missing url"):
        smoke.validate_agent_card({"name": "peer", "protocolVersion": "0.3.0", "skills": [{"id": "x"}]})


def test_message_send_without_task_id_fails() -> None:
    smoke = _load_smoke()
    with pytest.raises(smoke.SmokeFailure, match="task id"):
        smoke.task_id_from_result({"kind": "task", "status": {"state": "working"}})


def test_stream_without_final_status_fails() -> None:
    smoke = _load_smoke()
    events = [
        {"jsonrpc": "2.0", "result": {"id": "task_1", "kind": "task"}},
        {"jsonrpc": "2.0", "result": {"kind": "artifact-update", "taskId": "task_1", "chunkIndex": 0, "final": True}},
    ]
    with pytest.raises(smoke.SmokeFailure, match="final status-update"):
        smoke.summarize_stream_events(events)


def test_artifact_chunk_order_must_be_sequential() -> None:
    smoke = _load_smoke()
    events = [
        {"jsonrpc": "2.0", "result": {"id": "task_1", "kind": "task"}},
        {"jsonrpc": "2.0", "result": {"kind": "artifact-update", "taskId": "task_1", "chunkIndex": 1, "final": True}},
        {"jsonrpc": "2.0", "result": {"kind": "status-update", "taskId": "task_1", "status": {"state": "completed"}, "final": True}},
    ]
    with pytest.raises(smoke.SmokeFailure, match="sequential"):
        smoke.summarize_stream_events(events)


def test_tasks_cancel_requires_canceling_or_canceled_state() -> None:
    smoke = _load_smoke()
    with pytest.raises(smoke.SmokeFailure, match="unexpected state"):
        smoke.validate_cancel_result({"status": {"state": "completed"}})


def test_valid_stream_summary_returns_artifact_and_final_status() -> None:
    smoke = _load_smoke()
    events = [
        {"jsonrpc": "2.0", "result": {"id": "task_1", "kind": "task"}},
        {"jsonrpc": "2.0", "result": {"kind": "artifact-update", "taskId": "task_1", "chunkIndex": 0, "final": False}},
        {"jsonrpc": "2.0", "result": {"kind": "artifact-update", "taskId": "task_1", "chunkIndex": 1, "final": True}},
        {"jsonrpc": "2.0", "result": {"kind": "status-update", "taskId": "task_1", "status": {"state": "completed"}, "final": True}},
    ]
    summary = smoke.summarize_stream_events(events)
    assert summary["artifactUpdates"] == 2
    assert summary["chunkIndices"] == [0, 1]
    assert summary["finalState"] == "completed"

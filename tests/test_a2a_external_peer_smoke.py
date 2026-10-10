from __future__ import annotations

import importlib.util
import json
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


def test_cli_writes_third_party_json_and_markdown(monkeypatch: Any, tmp_path: Path) -> None:
    smoke = _load_smoke()
    out = tmp_path / "a2a-third-party-peer.json"
    markdown = tmp_path / "a2a-third-party-peer.md"
    steps = [
        {"name": "a2a.agent_card", "status": "pass", "detail": "ok"},
        {"name": "a2a.message_send", "status": "pass", "detail": "ok"},
        {"name": "a2a.tasks_get", "status": "pass", "detail": "ok"},
        {"name": "a2a.message_stream", "status": "pass", "detail": "ok"},
        {"name": "a2a.artifact_chunks", "status": "pass", "detail": "ok"},
        {"name": "a2a.sse_final_event", "status": "pass", "detail": "ok"},
        {"name": "a2a.tasks_list", "status": "pass", "detail": "ok"},
        {"name": "a2a.tasks_cancel", "status": "pass", "detail": "ok"},
    ]
    peer = {
        "name": "Third Party Demo Peer",
        "url": "http://third-party.local",
        "endpoint": "http://third-party.local/a2a/agents/demo",
        "protocolVersion": "0.3.0",
    }

    monkeypatch.setattr(smoke, "run_smoke", lambda *args, **kwargs: (steps, peer))
    monkeypatch.setattr(smoke, "app_version", lambda: "2.4.6")
    monkeypatch.setattr(smoke, "git_value", lambda *args: "abc1234" if args[:2] == ("rev-parse", "--short") else "")
    monkeypatch.setattr(smoke, "utc_now", lambda: "2026-06-28T00:00:00Z")
    monkeypatch.setattr(smoke, "build_environment", lambda: {"os": "Windows", "python": "3.13", "ci": False})

    assert smoke.main(["--peer-url", "http://third-party.local", "--peer-type", "third-party", "--out", str(out), "--markdown", str(markdown)]) == 0

    evidence = json.loads(out.read_text(encoding="utf-8"))
    assert evidence["schemaVersion"] == "a2a-third-party-peer-evidence.v1"
    assert evidence["version"] == "2.4.6"
    assert evidence["peerType"] == "third-party"
    assert evidence["checks"]["agentCard"] == "PASS"
    assert "A2A Third-Party Peer Evidence" in markdown.read_text(encoding="utf-8")

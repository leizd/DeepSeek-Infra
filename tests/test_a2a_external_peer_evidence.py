from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


def _load_smoke() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke_a2a_external_peer.py"
    spec = importlib.util.spec_from_file_location("smoke_a2a_external_peer_evidence_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _passing_steps() -> list[dict[str, Any]]:
    return [
        {"name": "a2a.agent_card", "status": "pass", "detail": "ok"},
        {"name": "a2a.message_send", "status": "pass", "detail": "ok"},
        {"name": "a2a.tasks_get", "status": "pass", "detail": "ok"},
        {"name": "a2a.message_stream", "status": "pass", "detail": "ok"},
        {"name": "a2a.artifact_chunks", "status": "pass", "detail": "ok"},
        {"name": "a2a.sse_final_event", "status": "pass", "detail": "ok"},
        {"name": "a2a.tasks_list", "status": "pass", "detail": "ok"},
        {"name": "a2a.tasks_cancel", "status": "pass", "detail": "ok"},
    ]


def test_a2a_external_evidence_passes_with_all_required_checks() -> None:
    smoke = _load_smoke()
    evidence = smoke.build_evidence(
        _passing_steps(),
        peer={"name": "External Peer", "url": "http://127.0.0.1:8002", "endpoint": "http://127.0.0.1:8002/a2a/agents/x", "protocolVersion": "0.3.0"},
        peer_type="independent-process",
    )

    assert evidence["schemaVersion"] == "a2a-external-peer-evidence.v1"
    assert evidence["status"] == "PASS"
    assert evidence["checks"] == {name: "pass" for name in smoke.REQUIRED_CHECKS}
    assert evidence["peer"]["type"] == "independent-process"


def test_a2a_external_evidence_fails_when_required_check_is_missing() -> None:
    smoke = _load_smoke()
    steps = [step for step in _passing_steps() if step["name"] != "a2a.artifact_chunks"]
    evidence = smoke.build_evidence(
        steps,
        peer={"name": "External Peer", "url": "http://127.0.0.1:8002", "endpoint": "http://127.0.0.1:8002/a2a/agents/x"},
        peer_type="independent-process",
    )

    assert evidence["status"] == "FAIL"
    assert evidence["checks"]["artifactChunks"] == "fail"


def test_a2a_external_evidence_schema_tracks_required_checks() -> None:
    schema = json.loads(Path("evals/schemas/a2a_external_peer_evidence.schema.json").read_text(encoding="utf-8"))
    required_checks = schema["properties"]["checks"]["required"]

    assert required_checks == [
        "agentCard",
        "messageSend",
        "messageStream",
        "tasksGet",
        "tasksCancel",
        "tasksList",
        "artifactChunks",
        "sseFinalEvent",
    ]

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_smoke():
    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke_mcp_headless_bridge.py"
    spec = importlib.util.spec_from_file_location("smoke_mcp_headless_bridge_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _passing_steps() -> list[dict[str, object]]:
    return [
        {"name": "server.healthz", "status": "pass"},
        {"name": "bridge.start", "status": "pass"},
        {"name": "mcp.initialize", "status": "pass"},
        {"name": "mcp.tools_list", "status": "pass"},
        {"name": "mcp.tools_call", "status": "pass"},
        {"name": "mcp.policy_denial", "status": "pass"},
    ]


def test_headless_mcp_bridge_evidence_passes_without_token_leak() -> None:
    smoke = _load_smoke()
    evidence = smoke.build_evidence(_passing_steps(), mcp_url="http://127.0.0.1:8000/mcp", auth_mode="bearer")

    assert evidence["schemaVersion"] == "headless-mcp-bridge-evidence.v1"
    assert evidence["status"] == "PASS"
    assert evidence["transport"]["auth"] == "bearer"
    assert "Authorization" not in str(evidence)
    assert "tools/call:data_transform" in evidence["covers"]
    assert "commit" in evidence
    assert "environment" in evidence
    assert set(evidence["environment"].keys()) == {"os", "python", "ci"}


def test_headless_mcp_bridge_evidence_fails_when_a_step_fails() -> None:
    smoke = _load_smoke()
    steps = _passing_steps()
    steps.append({"name": "headless_mcp_bridge", "status": "fail", "detail": "boom"})

    evidence = smoke.build_evidence(steps, mcp_url="http://127.0.0.1:8000/mcp", auth_mode="disabled")

    assert evidence["status"] == "FAIL"

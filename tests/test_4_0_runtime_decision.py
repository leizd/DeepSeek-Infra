from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from scripts import check_4_0_rc_readiness as readiness


ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_PATH = ROOT / "release/4_0_rc_requirements.json"
DECISION_PATH = ROOT / "release/4_0_runtime_decision.json"
ARCHITECTURE_IDS = {
    "rust_default_on_decision",
    "rust_sidecar_default_deployment_decision",
    "python_fallback_lifecycle_decision",
    "gateway_streaming_rust_path",
    "mcp_real_tool_bridge",
}


def _requirements(decision_path: Path = DECISION_PATH) -> dict[str, Any]:
    requirements = json.loads(REQUIREMENTS_PATH.read_text(encoding="utf-8"))
    for item in requirements["requirements"]:
        if item["id"] in ARCHITECTURE_IDS:
            item["decision_file"] = str(decision_path)
    return requirements


def _decision() -> dict[str, Any]:
    return json.loads(DECISION_PATH.read_text(encoding="utf-8"))


def _write_decision(tmp_path: Path, mutate: Any) -> Path:
    decision = copy.deepcopy(_decision())
    mutate(decision)
    path = tmp_path / "4_0_runtime_decision.json"
    path.write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
    return path


def _architecture_blockers(report: dict[str, Any]) -> set[str]:
    return ARCHITECTURE_IDS.intersection(report["blocker_ids"])


def test_approved_decision_resolves_all_architecture_blockers() -> None:
    report = readiness.evaluate_readiness(ROOT, _requirements())

    assert _architecture_blockers(report) == set()
    assert report["blocker_ids"] == ["python_measured_coverage"]


def test_empty_default_on_set_is_valid() -> None:
    report = readiness.evaluate_readiness(ROOT, _requirements())
    result = next(item for item in report["results"] if item["id"] == "rust_default_on_decision")

    assert _decision()["rust_default_on_components"] == []
    requirement = next(item for item in _requirements()["requirements"] if item["id"] == "rust_default_on_decision")
    assert requirement["allow_empty"] is True
    assert result["passed"] is True
    assert result["observed"] == []


def test_missing_approver_remains_blocked(tmp_path: Path) -> None:
    path = _write_decision(tmp_path, lambda decision: decision.__setitem__("approved_by", []))
    report = readiness.evaluate_readiness(ROOT, _requirements(path))

    assert _architecture_blockers(report) == ARCHITECTURE_IDS


def test_missing_fallback_lifecycle_remains_blocked(tmp_path: Path) -> None:
    def remove_lifecycle(decision: dict[str, Any]) -> None:
        del decision["python_fallback"]["removal_not_before"]

    path = _write_decision(tmp_path, remove_lifecycle)
    report = readiness.evaluate_readiness(ROOT, _requirements(path))

    assert _architecture_blockers(report) == {"python_fallback_lifecycle_decision"}


def test_invalid_streaming_owner_is_rejected(tmp_path: Path) -> None:
    path = _write_decision(tmp_path, lambda decision: decision.__setitem__("gateway_streaming_owner", "shared"))
    report = readiness.evaluate_readiness(ROOT, _requirements(path))

    assert _architecture_blockers(report) == {"gateway_streaming_rust_path"}


def test_invalid_mcp_execution_owner_is_rejected(tmp_path: Path) -> None:
    path = _write_decision(tmp_path, lambda decision: decision.__setitem__("mcp_tool_execution_owner", "shared"))
    report = readiness.evaluate_readiness(ROOT, _requirements(path))

    assert _architecture_blockers(report) == {"mcp_real_tool_bridge"}


def test_decision_file_does_not_change_runtime_defaults() -> None:
    decision = _decision()
    env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert decision["rust_default_on_components"] == []
    assert decision["default_sidecar_deployment"] is False
    for component in ("GATEWAY", "MCP", "POLICY", "RAG"):
        assert f"DEEPSEEK_RUST_{component}=0" in env_text
    assert "rust-gateway:" not in compose_text
    assert "DEEPSEEK_RUST_" not in compose_text


def test_coverage_blocker_remains_after_architecture_approval() -> None:
    report = readiness.evaluate_readiness(ROOT, _requirements())

    assert report["blocker_ids"] == ["python_measured_coverage"]
    assert report["ready"] is False


def test_strict_mode_still_fails_only_for_coverage() -> None:
    report = readiness.evaluate_readiness(ROOT, _requirements())

    assert report["blocker_ids"] == ["python_measured_coverage"]
    assert readiness.main(["--root", str(ROOT), "--strict"]) == 1

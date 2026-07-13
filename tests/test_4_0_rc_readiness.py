from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts import check_4_0_rc_readiness as readiness


ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_PATH = ROOT / "release/4_0_rc_requirements.json"


def _requirements() -> dict[str, Any]:
    return json.loads(REQUIREMENTS_PATH.read_text(encoding="utf-8"))


def _items_by_id() -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in _requirements()["requirements"]}


def _run_checker(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "scripts/check_4_0_rc_readiness.py", *args]
    return subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False)


def test_requirements_manifest_has_owned_classified_entries() -> None:
    data = _requirements()

    assert data["target_version"] == "4.0.0-rc.1"
    assert data["baseline_version"] == "3.4.0"
    assert len(data["requirements"]) >= 15
    for item in data["requirements"]:
        assert item["id"]
        assert item["owner"]
        assert item["category"] in {"quality_blocker", "decision_blocker", "advisory"}
        assert isinstance(item["blocking"], bool)
        assert item["evidence"]
        assert item["description"]


def test_rc_coverage_target_matches_promoted_current_gate() -> None:
    items = _items_by_id()

    assert items["python_coverage_gate"]["required"] == 95.0
    assert items["python_measured_coverage"]["required"] == 95.0
    assert items["python_measured_coverage"]["observed"] == 95.33
    assert items["python_measured_coverage"]["blocking"] is True


def test_current_readiness_report_is_ready() -> None:
    report = readiness.evaluate_readiness(ROOT, _requirements())

    assert report["ready"] is True
    assert report["blocker_ids"] == []
    assert report["summary"]["advisories"] >= 1

    rendered = readiness.render_report(report)
    assert "PASS   Python measured coverage: 95.33% >= 95.00%" in rendered
    assert "Decision: READY FOR 4.0.0-rc.1" in rendered


def test_coverage_override_keeps_readiness_green() -> None:
    report = readiness.evaluate_readiness(ROOT, _requirements(), coverage_override=95.0)

    assert report["blocker_ids"] == []
    assert report["ready"] is True


def test_report_only_writes_json_without_failing(tmp_path: Path) -> None:
    report_path = tmp_path / "rc-readiness.json"

    completed = _run_checker("--report-only", "--json-out", str(report_path))

    assert completed.returncode == 0, completed.stderr
    assert "Decision: READY FOR 4.0.0-rc.1" in completed.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ready"] is True
    assert report["target_version"] == "4.0.0-rc.1"


def test_strict_mode_passes_when_blockers_are_resolved() -> None:
    completed = _run_checker("--strict")

    assert completed.returncode == 0
    assert "Decision: READY FOR 4.0.0-rc.1" in completed.stdout


def test_live_ci_results_override_recorded_baseline() -> None:
    env = os.environ.copy()
    required_jobs = _items_by_id()["all_ci_jobs_green"]["required_jobs"]
    for job in required_jobs:
        env[readiness._ci_env_name(job)] = "success"
    env[readiness._ci_env_name("rag-parity")] = "failure"

    completed = _run_checker("--report-only", env=env)

    assert completed.returncode == 0
    assert "BLOCK  All required CI jobs green" in completed.stdout


def test_workflow_uses_report_only_and_release_branch_strict_modes() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "rc-readiness:" in workflow
    assert "needs['rag-parity'].result" in workflow
    assert "needs['hybrid-runtime-e2e'].result" in workflow
    assert "release/*|rc/*) mode=--strict" in workflow
    assert "mode=--report-only" in workflow
    assert "artifacts/4-0-rc-readiness.json" in workflow


def test_readiness_document_records_component_recommendations() -> None:
    document = (ROOT / "docs/4_0_RC_READINESS.md").read_text(encoding="utf-8")

    assert "READY FOR 4.0.0-rc.1" in document
    assert "Gateway | Models and non-streaming chat delegation" in document
    assert "MCP | JSON-RPC initialize" in document
    assert "Policy | Stable deny/audit contract" in document
    assert "RAG | Deterministic hot-path parity at 38/38" in document
    assert "does not modify `.env.example`" in document
    assert "ADR-0040" in document

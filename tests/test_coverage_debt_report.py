from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import report_coverage_debt as debt


ROOT = Path(__file__).resolve().parents[1]


def _coverage(*, branch: bool = True) -> dict[str, Any]:
    def entry(statements: int, missing: int, branches: int = 0, missing_branches: int = 0) -> dict[str, Any]:
        return {
            "summary": {
                "num_statements": statements,
                "missing_lines": missing,
                "num_branches": branches,
                "missing_branches": missing_branches,
            }
        }

    return {
        "meta": {"format": 3, "version": "7.13.5", "timestamp": "2026-07-11T00:00:00Z", "branch_coverage": branch},
        "files": {
            "deepseek_infra\\infra\\browser\\controller.py": entry(100, 40, 20, 8),
            "deepseek_infra/infra/automation/runner.py": entry(50, 10, 10, 2),
            "deepseek_infra/core/utils.py": entry(25, 1),
        },
        "totals": {"num_statements": 175, "missing_lines": 51, "num_branches": 30, "missing_branches": 10},
    }


def test_build_report_records_statement_branch_and_risk_debt() -> None:
    report = debt.build_report(_coverage(), source="coverage.json")

    assert report["branch_coverage_enabled"] is True
    assert report["totals"] == {
        "statements": 175,
        "covered_statements": 124,
        "missing_statements": 51,
        "coverage_percent": 70.86,
        "branches": 30,
        "covered_branches": 20,
        "missing_branches": 10,
        "branch_coverage_percent": 66.67,
    }
    browser, automation, utils = report["modules"]
    assert (browser["module"], browser["risk"], browser["missing_statements"]) == ("infra/browser/controller.py", "HIGH", 40)
    assert (automation["risk"], automation["branch_coverage_percent"]) == ("MEDIUM", 80.0)
    assert utils["risk"] == "LOW"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("deepseek_infra/infra/tool_runtime/ocr.py", "HIGH"),
        ("deepseek_infra/launcher/credentials.py", "HIGH"),
        ("deepseek_infra/infra/gateway/edge_inference.py", "MEDIUM"),
        ("deepseek_infra/infra/skills/runner.py", "MEDIUM"),
        ("deepseek_infra/core/utils.py", "LOW"),
    ],
)
def test_risk_category_depends_on_responsibility_not_percentage(path: str, expected: str) -> None:
    risk, reason = debt.classify_risk(path)

    assert risk == expected
    assert reason


def test_render_report_includes_missing_lines_and_branches() -> None:
    rendered = debt.render_report(debt.build_report(_coverage(), source="coverage.json"), limit=2)

    assert "Coverage debt" in rendered
    assert "HIGH   infra/browser/controller.py" in rendered
    assert "40 lines missing, 8 branches missing" in rendered
    assert "Measured statement coverage: 70.86%" in rendered
    assert "Measured branch coverage: 66.67%" in rendered
    assert "core/utils.py" not in rendered


def test_branch_fields_are_optional_for_old_coverage_json() -> None:
    coverage = _coverage(branch=False)
    for payload in coverage["files"].values():
        payload["summary"].pop("num_branches")
        payload["summary"].pop("missing_branches")
    coverage["totals"].pop("num_branches")
    coverage["totals"].pop("missing_branches")

    report = debt.build_report(coverage, source="coverage.json")

    assert report["branch_coverage_enabled"] is False
    assert report["totals"]["branches"] == 0
    assert report["totals"]["branch_coverage_percent"] is None


def test_cli_writes_machine_readable_report(tmp_path: Path) -> None:
    coverage_path = tmp_path / "coverage.json"
    output_path = tmp_path / "coverage-debt.json"
    coverage_path.write_text(json.dumps(_coverage()), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "scripts/report_coverage_debt.py", "--coverage", str(coverage_path), "--json-out", str(output_path), "--limit", "1"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Coverage debt" in completed.stdout
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert len(report["modules"]) == 3


def test_cli_rejects_invalid_coverage_payload(tmp_path: Path) -> None:
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text("[]", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "scripts/report_coverage_debt.py", "--coverage", str(coverage_path), "--json-out", str(tmp_path / "out.json")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "expected a JSON object" in completed.stderr

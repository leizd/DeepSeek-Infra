#!/usr/bin/env python3
"""Offline smoke for the Skill Eval Dashboard / Skill Quality Loop."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.core.config import APP_VERSION  # noqa: E402
from deepseek_infra.infra.skills import eval as skill_eval  # noqa: E402
from deepseek_infra.infra.skills import evidence  # noqa: E402
from scripts.smoke_skills import patch_runtime  # noqa: E402


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _contains_all(text: str, needles: tuple[str, ...]) -> bool:
    return all(needle in text for needle in needles)


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_checks(*, version: str, report_out: Path) -> tuple[dict[str, str], dict[str, Any]]:
    index = _read("static/index.html")
    skills_js = _read("static/modules/skills.js")
    styles = _read("static/styles.css")
    routes = _read("deepseek_infra/web/routes/skills.py")
    runner = _read("evals/runners/run_skill_eval.py")
    ci = _read(".github/workflows/ci.yml")

    checks: dict[str, str] = {}
    details: dict[str, Any] = {}

    with tempfile.TemporaryDirectory(prefix="deepseek-skill-eval-dashboard-", ignore_cleanup_errors=True) as tmp:
        patch_runtime(Path(tmp))
        report = skill_eval.build_skill_eval_report(version=version)
    _write_report(report_out, report)

    checks["evalDashboardEntrypoint"] = "PASS" if _contains_all(
        index + skills_js,
        (
            'id="skillEvalButton"',
            'id="skillEvalHost"',
            "Skill Eval Dashboard",
            "openEvalHost",
            "runSkillEval",
        ),
    ) else "FAIL"

    checks["evalCaseBuilder"] = "PASS" if _contains_all(
        index + skills_js,
        (
            'id="skillEvalCaseForm"',
            "saveSkillEvalCase",
            "create_eval_case",
            "expectedKeywords",
            "requiredOutputPaths",
            "expectedArtifactTypes",
        ),
    ) else "FAIL"

    checks["skillEvalApiActions"] = "PASS" if _contains_all(
        routes,
        (
            'action == "eval_report"',
            'action == "list_eval_cases"',
            'action == "create_eval_case"',
            'action == "delete_eval_case"',
        ),
    ) else "FAIL"

    checks["skillEvalReport"] = "PASS" if report.get("status") == "PASS" and report.get("summary", {}).get("caseCount", 0) >= 4 else "FAIL"
    checks["packLevelEval"] = "PASS" if report.get("checks", {}).get("packLevelEval") == "PASS" and report.get("packResults") else "FAIL"
    checks["regressionCompare"] = "PASS" if report.get("checks", {}).get("regressionCompare") == "PASS" else "FAIL"

    checks["evalExportActions"] = "PASS" if _contains_all(
        skills_js,
        (
            "exportSkillEvalJson",
            "exportSkillEvalMarkdown",
            "copySkillEvalSummary",
            "skillEvalMarkdown",
        ),
    ) else "FAIL"

    checks["skillEvalStyles"] = "PASS" if _contains_all(
        styles,
        (
            ".skill-eval-host",
            ".skill-eval-summary",
            ".skill-eval-case-form",
            ".skill-eval-row",
        ),
    ) else "FAIL"

    asset_paths = (
        "docs/assets/skill-eval-dashboard.png",
        "docs/assets/skill-eval-case-builder.png",
    )
    checks["skillEvalAssets"] = "PASS" if all((REPO_ROOT / path).is_file() for path in asset_paths) else "FAIL"

    checks["skillEvalRunner"] = "PASS" if _contains_all(
        runner,
        (
            "build_skill_eval_report",
            "--baseline",
            "--scope",
            "--pack-id",
        ),
    ) else "FAIL"

    syntax = subprocess.run(["node", "--check", "static/modules/skills.js"], cwd=REPO_ROOT, capture_output=True, text=True)
    checks["skillEvalJsSyntax"] = "PASS" if syntax.returncode == 0 else "FAIL"

    checks["ciReleaseGate"] = "PASS" if "smoke_skill_eval_dashboard.py" in ci and "run_skill_eval.py" in ci else "FAIL"

    details["report"] = {
        "path": str(report_out),
        "summary": report.get("summary", {}),
        "checks": report.get("checks", {}),
    }
    details["assets"] = list(asset_paths)
    details["skillEvalJsSyntax"] = {"returnCode": syntax.returncode, "stderr": syntax.stderr.strip()}
    return checks, details


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline Skill Eval Dashboard smoke")
    parser.add_argument("--offline", action="store_true", help="Kept for release-smoke symmetry; this smoke is always offline.")
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--out", default=str(REPO_ROOT / "docs" / "evidence" / f"skill-eval-dashboard-v{APP_VERSION}.json"))
    parser.add_argument("--report-out", default=str(REPO_ROOT / "evals" / "reports" / f"skills-v{APP_VERSION}.json"))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    checks, details = run_checks(version=args.version, report_out=Path(args.report_out))
    payload = evidence.release_evidence_payload(checks=checks, version=args.version, details=details)
    evidence.write_release_evidence(Path(args.out), payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for name, status in checks.items():
            print(f"[{status}] {name}")
        print(f"Skill Eval Dashboard smoke summary: {payload['status']} -> {args.out}")
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

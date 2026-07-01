#!/usr/bin/env python3
"""Offline smoke for Skill Run Analytics and Usage History."""

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
from deepseek_infra.core.errors import AppError  # noqa: E402
from deepseek_infra.infra.data import projects  # noqa: E402
from deepseek_infra.infra.skills import analytics, evidence  # noqa: E402
from deepseek_infra.infra.skills.runner import run_skill  # noqa: E402
from scripts.smoke_skills import patch_runtime  # noqa: E402


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _contains_all(text: str, needles: tuple[str, ...]) -> bool:
    return all(needle in text for needle in needles)


def run_checks(runtime_root: Path) -> tuple[dict[str, str], dict[str, Any]]:
    patch_runtime(runtime_root)
    checks: dict[str, str] = {}
    details: dict[str, Any] = {}

    project = projects.create_project("Skill Analytics Smoke Project")
    result = run_skill(
        "skill_research_brief",
        {"topic": "Skill Run Analytics", "depth": "quick"},
        project_id=project["id"],
        offline=True,
        persist=True,
    )
    runs = analytics.list_runs(skill_id="skill_research_brief", limit=20)
    run_record = analytics.get_run(str(result["skillRunId"]))
    summary = analytics.analytics_summary(scope="skill", skill_id="skill_research_brief")
    project_runs = projects.list_project_skill_runs(project["id"])
    project_summary = analytics.analytics_summary(scope="project", project_id=project["id"])

    checks["skillRunHistory"] = "PASS" if any(item["skillRunId"] == result["skillRunId"] for item in runs) else "FAIL"
    checks["runMetadataPersist"] = "PASS" if run_record["skillVersion"] and run_record["latencyMs"] >= 0 and run_record["offline"] is True else "FAIL"
    checks["analyticsSummary"] = "PASS" if summary["totalRuns"] >= 1 and summary["successRate"] > 0 and summary["p90LatencyMs"] >= 0 else "FAIL"
    checks["projectRunHistory"] = "PASS" if project_runs and project_runs[0]["skillRunId"] == result["skillRunId"] else "FAIL"
    checks["traceLink"] = "PASS" if bool(run_record["links"].get("trace")) else "FAIL"
    checks["artifactLink"] = "PASS" if run_record["artifactCount"] >= 1 and bool(run_record["links"].get("artifacts")) else "FAIL"
    checks["projectAnalytics"] = "PASS" if project_summary["projectId"] == project["id"] and project_summary["projectBindingRuns"] >= 1 else "FAIL"

    try:
        run_skill("skill_research_brief", {}, offline=True, persist=True)
    except AppError:
        pass
    failed_runs = analytics.list_runs(status="failed", limit=20)
    checks["failureDiagnostics"] = "PASS" if failed_runs and failed_runs[0]["failureCategory"] == "schema_validation_failed" and failed_runs[0]["diagnosticSuggestion"] else "FAIL"

    cleanup = analytics.cleanup_runs(status="failed")
    checks["retentionCleanup"] = "PASS" if cleanup["deleted"] >= 1 and not analytics.list_runs(status="failed", limit=1) else "FAIL"
    redacted = analytics.redact_run(str(result["skillRunId"]))
    checks["privacyRedaction"] = "PASS" if redacted["run"]["redacted"] and redacted["run"]["inputSummary"] == "[redacted]" else "FAIL"

    routes = _read("deepseek_infra/web/routes/skills.py") + _read("deepseek_infra/web/routes/workspace.py")
    index = _read("static/index.html")
    skills_js = _read("static/modules/skills.js")
    styles = _read("static/styles.css")
    ci = _read(".github/workflows/ci.yml")

    checks["analyticsApiActions"] = "PASS" if _contains_all(
        routes,
        (
            'action == "list_runs"',
            'action == "get_run"',
            'action == "delete_run"',
            'action == "analytics_summary"',
            "skill-analytics",
        ),
    ) else "FAIL"
    checks["analyticsUi"] = "PASS" if _contains_all(
        index + skills_js + styles,
        (
            'id="skillRunsButton"',
            'id="skillRunsHost"',
            'id="skillRunsSummary"',
            'id="skillRunsList"',
            "openRunsHost",
            "loadRunsDashboard",
            "cleanupFailedRuns",
            ".skill-runs-host",
            ".skill-run-analytics-row",
        ),
    ) else "FAIL"
    asset_paths = ("docs/assets/skill-runs.png", "docs/assets/skill-analytics.png")
    checks["analyticsAssets"] = "PASS" if all((REPO_ROOT / path).is_file() for path in asset_paths) else "FAIL"
    syntax = subprocess.run(["node", "--check", "static/modules/skills.js"], cwd=REPO_ROOT, capture_output=True, text=True)
    checks["analyticsJsSyntax"] = "PASS" if syntax.returncode == 0 else "FAIL"
    checks["ciReleaseGate"] = "PASS" if "smoke_skill_analytics.py" in ci else "FAIL"

    details["run"] = run_record
    details["summary"] = summary
    details["projectSummary"] = project_summary
    details["cleanup"] = cleanup
    details["assets"] = list(asset_paths)
    details["analyticsJsSyntax"] = {"returnCode": syntax.returncode, "stderr": syntax.stderr.strip()}
    return checks, details


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline Skill Analytics smoke")
    parser.add_argument("--offline", action="store_true", help="Kept for release-smoke symmetry; this smoke is always offline.")
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--out", default=str(REPO_ROOT / "docs" / "evidence" / f"skill-analytics-v{APP_VERSION}.json"))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="deepseek-skill-analytics-", ignore_cleanup_errors=True) as tmp:
        checks, details = run_checks(Path(tmp))
    payload = evidence.release_evidence_payload(checks=checks, version=args.version, details=details)
    evidence.write_release_evidence(Path(args.out), payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for name, status in checks.items():
            print(f"[{status}] {name}")
        print(f"Skill Analytics smoke summary: {payload['status']} -> {args.out}")
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

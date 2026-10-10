#!/usr/bin/env python3
"""Offline Automation Runtime smoke."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.core.config import APP_VERSION  # noqa: E402
from scripts.smoke_browser import configure_browser_runtime  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "automation"


def configure_automation_runtime(root: Path) -> None:
    from deepseek_infra.core import config
    from deepseek_infra.infra.automation import history as automation_history
    from deepseek_infra.infra.automation import registry as automation_registry
    from deepseek_infra.infra.observability import observability
    from deepseek_infra.infra.skills import evidence as skill_evidence
    from deepseek_infra.infra.skills import registry as skill_registry
    from deepseek_infra.infra.tool_runtime import generated_files
    from deepseek_infra.infra.workspace import artifacts as workspace_artifacts
    from deepseek_infra.infra.workspace import exports as workspace_exports
    from deepseek_infra.infra.workspace import saved_items as workspace_saved_items

    configure_browser_runtime(root)
    automation_dir = root / ".automation"
    skills_dir = root / ".skills"
    traces_dir = root / ".traces"
    config.AUTOMATION_DIR = automation_dir
    config.AUTOMATION_ENABLED = True
    config.AUTOMATION_ALLOW_BROWSER = False
    config.AUTOMATION_MAX_RUNS_PER_DAY = 50
    config.AUTOMATION_MIN_INTERVAL_SECONDS = 1
    config.AUTOMATION_REQUIRE_CONFIRM_FOR_BROWSER_WRITE = True
    config.AUTOMATION_RUN_TIMEOUT_SECONDS = 1_800
    config.SKILLS_DIR = skills_dir
    config.TRACE_DIR = traces_dir
    config.TRACE_DB = traces_dir / "traces.sqlite3"
    automation_registry.AUTOMATION_DIR = automation_dir
    automation_history.AUTOMATION_DIR = automation_dir
    skill_registry.SKILLS_DIR = skills_dir
    skill_evidence.GENERATED_DIR = config.GENERATED_DIR
    generated_files.GENERATED_DIR = config.GENERATED_DIR
    observability.TRACE_DIR = traces_dir
    observability.TRACE_DB = traces_dir / "traces.sqlite3"
    workspace_artifacts.legacy_projects.PROJECTS_DIR = config.PROJECTS_DIR
    workspace_saved_items.legacy_projects.PROJECTS_DIR = config.PROJECTS_DIR
    workspace_exports.legacy_projects.PROJECTS_DIR = config.PROJECTS_DIR


def run_automation_smoke(root: Path) -> tuple[dict[str, str], dict[str, Any]]:
    from deepseek_infra.infra.automation import actions as automation_actions
    from deepseek_infra.infra.automation import evidence, history, registry, runner, scheduler, triggers
    from deepseek_infra.infra.workspace import artifacts, projects, saved_items

    checks = {
        "automationCreate": "FAIL",
        "manualRun": "FAIL",
        "scheduleTrigger": "FAIL",
        "eventTrigger": "FAIL",
        "runSkillAction": "FAIL",
        "browserReadOnlyAction": "FAIL",
        "projectExportAction": "FAIL",
        "unsafeActionBlocked": "FAIL",
        "runHistory": "FAIL",
        "traceLinked": "FAIL",
        "artifactOutput": "FAIL",
        "templates": "FAIL",
        "evidenceGenerated": "FAIL",
        "browserCheckChanged": "FAIL",
        "browserCheckUnchanged": "FAIL",
        "fixturePathBlocked": "FAIL",
        "cronStepRange": "FAIL",
        "maxRunsPerDay": "FAIL",
        "retryBackoff": "FAIL",
        "timeoutEvidence": "FAIL",
        "rerun": "FAIL",
        "templateCreate": "FAIL",
    }
    details: dict[str, Any] = {"runtimeRoot": str(root), "fixtures": str(FIXTURES)}
    project = projects.create_project("Automation Smoke", description="Automation Runtime")
    project_id = str(project["projectId"])

    manual = registry.create_automation(
        {
            "projectId": project_id,
            "name": "Manual project summary",
            "trigger": {"type": "manual"},
            "condition": {"type": "always"},
            "action": {"type": "project_summary"},
            "output": {"saveToProject": True, "createArtifact": True, "artifactType": "markdown"},
            "policy": {"maxRunsPerDay": 10, "allowBrowser": False, "allowNetwork": False},
        }
    )
    checks["automationCreate"] = "PASS" if manual["automationId"] and registry.get_automation(manual["automationId"]) else "FAIL"
    manual_run = runner.run_once(manual["automationId"])
    checks["manualRun"] = "PASS" if manual_run["status"] == "success" else "FAIL"
    checks["artifactOutput"] = "PASS" if manual_run["outputs"]["artifactIds"] and artifacts.list_artifacts(project_id) else "FAIL"
    checks["traceLinked"] = "PASS" if manual_run.get("traceId") else "FAIL"

    scheduled = registry.create_automation(
        {
            "projectId": project_id,
            "name": "Scheduled saved item",
            "trigger": {"type": "schedule", "cron": "15 10 * * *"},
            "action": {"type": "save_item", "title": "Scheduled item", "content": "scheduled automation output"},
            "policy": {"maxRunsPerDay": 10, "allowBrowser": False, "allowNetwork": False},
        }
    )
    schedule_result = scheduler.run_due(now=datetime(2026, 7, 4, 10, 15, tzinfo=timezone.utc))
    checks["scheduleTrigger"] = "PASS" if any(run.get("automationId") == scheduled["automationId"] and run.get("status") == "success" for run in schedule_result["runs"]) else "FAIL"

    event_auto = registry.create_automation(
        {
            "projectId": project_id,
            "name": "Saved item event",
            "trigger": {"type": "event", "event": "saved_item.created"},
            "condition": {"type": "new_saved_items", "newSavedItems": True},
            "action": {"type": "save_item", "title": "Event item", "content": "event automation output"},
            "policy": {"maxRunsPerDay": 10, "allowBrowser": False, "allowNetwork": False},
        }
    )
    event_result = scheduler.run_due(event={"event": "saved_item.created", "projectId": project_id})
    checks["eventTrigger"] = "PASS" if any(run.get("automationId") == event_auto["automationId"] and run.get("status") == "success" for run in event_result["runs"]) else "FAIL"

    skill_auto = registry.create_automation(
        {
            "projectId": project_id,
            "name": "Skill action",
            "trigger": {"type": "manual"},
            "action": {"type": "run_skill", "skillId": "skill_research_brief", "input": {"topic": "Automation Runtime", "depth": "quick"}, "offline": True},
            "policy": {"maxRunsPerDay": 10, "allowBrowser": False, "allowNetwork": False},
        }
    )
    skill_run = runner.run_once(skill_auto["automationId"])
    checks["runSkillAction"] = "PASS" if skill_run["status"] == "success" and skill_run["outputs"]["artifactIds"] else "FAIL"

    browser_auto = registry.create_automation(
        {
            "projectId": project_id,
            "name": "Browser snapshot",
            "trigger": {"type": "manual"},
            "action": {"type": "browser_snapshot", "url": (FIXTURES / "webpage_v1.html").as_uri(), "selector": "#content"},
            "policy": {"maxRunsPerDay": 10, "allowBrowser": True, "browserMode": "read_only", "allowNetwork": False},
        }
    )
    browser_run = runner.run_once(browser_auto["automationId"])
    checks["browserReadOnlyAction"] = (
        "PASS" if browser_run["status"] == "success" and browser_run["outputs"]["mediaIds"] and "browserSessionClosed" in browser_run["logs"] else "FAIL"
    )

    browser_check = registry.create_automation(
        {
            "projectId": project_id,
            "name": "Browser check fixture",
            "trigger": {"type": "manual"},
            "action": {"type": "browser_check", "fixturePath": str(FIXTURES / "webpage_v1.html")},
            "policy": {"maxRunsPerDay": 10, "allowBrowser": True, "browserMode": "read_only", "allowNetwork": False},
        }
    )
    check_first = runner.run_once(browser_check["automationId"])
    check_second = runner.run_once(browser_check["automationId"])
    registry.update_automation(browser_check["automationId"], {"action": {"type": "browser_check", "fixturePath": str(FIXTURES / "webpage_v2.html")}})
    check_third = runner.run_once(browser_check["automationId"])
    checks["browserCheckChanged"] = "PASS" if check_first["status"] == "success" and check_third["status"] == "success" else "FAIL"
    checks["browserCheckUnchanged"] = "PASS" if check_second["status"] == "skipped" and check_second["skippedReason"] == "url_unchanged" else "FAIL"

    blocked_fixture = registry.create_automation(
        {
            "projectId": project_id,
            "name": "Blocked fixture path",
            "trigger": {"type": "manual"},
            "action": {"type": "browser_check", "fixturePath": str(REPO_ROOT / "README.md")},
            "policy": {"maxRunsPerDay": 10, "allowBrowser": True, "browserMode": "read_only", "allowNetwork": False},
        }
    )
    blocked_fixture_run = runner.run_once(blocked_fixture["automationId"])
    checks["fixturePathBlocked"] = "PASS" if blocked_fixture_run["status"] == "failed" and "fixturePath is outside allowed" in blocked_fixture_run["error"] else "FAIL"

    monday = datetime(2026, 7, 6, 9, 10, tzinfo=timezone.utc)
    checks["cronStepRange"] = "PASS" if triggers.cron_matches("*/5 9-17 * * 1-5", monday) and not triggers.cron_matches("*/5 9-17 * * 1-5", monday.replace(minute=11)) else "FAIL"

    limited = registry.create_automation(
        {
            "projectId": project_id,
            "name": "Daily limited",
            "trigger": {"type": "manual"},
            "action": {"type": "save_item", "title": "Limited", "content": "limited"},
            "policy": {"maxRunsPerDay": 1, "allowBrowser": False, "allowNetwork": False},
        }
    )
    limit_now = datetime(2026, 7, 7, 8, 0, tzinfo=timezone.utc)
    limit_first = runner.run_once(limited["automationId"], now=limit_now)
    limit_second = runner.run_once(limited["automationId"], now=limit_now)
    checks["maxRunsPerDay"] = "PASS" if limit_first["status"] == "success" and limit_second["status"] == "skipped" and "max_runs_per_day_exceeded" in limit_second["skippedReason"] else "FAIL"

    retry_auto = registry.create_automation(
        {
            "projectId": project_id,
            "name": "Retry action",
            "trigger": {"type": "manual"},
            "action": {"type": "save_item", "title": "Retry", "content": "retry"},
            "policy": {"maxRunsPerDay": 10, "allowBrowser": False, "allowNetwork": False, "retry": {"maxAttempts": 2, "backoffSeconds": 2}},
        }
    )
    original_run_action = automation_actions.run_action
    original_sleep = runner.time.sleep
    retry_calls = 0
    slept: list[int] = []

    def flaky_action(automation: dict[str, Any], *, run_id: str, trigger: dict[str, Any], event: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal retry_calls
        retry_calls += 1
        if retry_calls == 1:
            raise RuntimeError("transient failure")
        return original_run_action(automation, run_id=run_id, trigger=trigger, event=event)

    try:
        automation_actions.run_action = flaky_action
        runner.time.sleep = lambda seconds: slept.append(int(seconds))
        retry_run = runner.run_once(retry_auto["automationId"])
    finally:
        automation_actions.run_action = original_run_action
        runner.time.sleep = original_sleep
    checks["retryBackoff"] = (
        "PASS"
        if retry_run["status"] == "success"
        and retry_run["attempts"] == 2
        and slept == [2]
        and retry_run["evidence"]["runtime"]["attemptErrors"]
        else "FAIL"
    )
    rerun_result = runner.rerun(retry_run["runId"])
    checks["rerun"] = "PASS" if rerun_result["status"] == "success" and rerun_result["trigger"].get("rerunOf") == retry_run["runId"] else "FAIL"

    timeout_auto = registry.create_automation(
        {
            "projectId": project_id,
            "name": "Timeout action",
            "trigger": {"type": "manual"},
            "action": {"type": "save_item", "title": "Timeout", "content": "timeout"},
            "policy": {"maxRunsPerDay": 10, "allowBrowser": False, "allowNetwork": False, "timeoutSeconds": 1, "retry": {"maxAttempts": 1}},
        }
    )
    original_monotonic = runner.time.monotonic
    monotonic_values = iter([0.0, 0.0, 0.0, 2.0, 2.0, 2.0, 2.0])
    try:
        runner.time.monotonic = lambda: next(monotonic_values)
        timeout_run = runner.run_once(timeout_auto["automationId"])
    finally:
        runner.time.monotonic = original_monotonic
    checks["timeoutEvidence"] = (
        "PASS"
        if timeout_run["status"] == "failed"
        and "exceeded timeout" in timeout_run["error"]
        and timeout_run["evidence"]["runtime"]["timeoutCheckedAtMs"] > 0
        else "FAIL"
    )

    template_auto = registry.create_from_template("daily_project_summary", project_id=project_id, overrides={"name": "Smoke template", "trigger": {"type": "manual"}})
    checks["templateCreate"] = "PASS" if template_auto["name"] == "Smoke template" and template_auto["action"]["type"] == "project_summary" else "FAIL"

    export_auto = registry.create_automation(
        {
            "projectId": project_id,
            "name": "Project export",
            "trigger": {"type": "manual"},
            "action": {"type": "export_project", "format": "zip"},
            "policy": {"maxRunsPerDay": 10, "allowBrowser": False, "allowNetwork": False},
        }
    )
    export_run = runner.run_once(export_auto["automationId"])
    checks["projectExportAction"] = "PASS" if export_run["status"] == "success" and export_run["outputs"]["exportIds"] else "FAIL"

    unsafe_auto = registry.create_automation(
        {
            "projectId": project_id,
            "name": "Unsafe browser",
            "trigger": {"type": "manual"},
            "action": {"type": "browser_snapshot", "url": "http://127.0.0.1:9/private"},
            "policy": {"maxRunsPerDay": 10, "allowBrowser": True, "browserMode": "read_only", "allowNetwork": True},
        }
    )
    unsafe_run = runner.run_once(unsafe_auto["automationId"])
    checks["unsafeActionBlocked"] = "PASS" if unsafe_run["status"] == "skipped" and "unsafe_url" in unsafe_run["skippedReason"] else "FAIL"

    runs = history.list_runs(project_id=project_id, limit=100)
    checks["runHistory"] = "PASS" if len(runs) >= 7 and {run["status"] for run in runs} >= {"success", "skipped"} else "FAIL"
    checks["templates"] = "PASS" if len(registry.list_templates()) >= 6 else "FAIL"
    checks["evidenceGenerated"] = "PASS" if evidence.automation_evidence_payload("test", checks={"x": "PASS"})["status"] == "PASS" else "FAIL"
    details.update(
        {
            "projectId": project_id,
            "automationIds": [item["automationId"] for item in registry.list_automations(project_id=project_id)],
            "runIds": [run["runId"] for run in runs],
            "savedItemCount": len(saved_items.list_saved_items(project_id)),
            "artifactCount": len(artifacts.list_artifacts(project_id)),
        }
    )
    return checks, details


def build_evidence(version: str, checks: dict[str, str], details: dict[str, Any]) -> dict[str, Any]:
    from deepseek_infra.infra.automation.evidence import automation_evidence_payload

    return automation_evidence_payload(version, checks=checks, details=details)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline Automation Runtime smoke")
    parser.add_argument("--offline", action="store_true", help="Kept for release-smoke symmetry; this smoke is always offline.")
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "evidence" / f"automation-v{APP_VERSION}.json")
    parser.add_argument("--json", action="store_true", help="Print evidence JSON to stdout.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="deepseek-automation-smoke-", ignore_cleanup_errors=True) as tmp:
        os.environ["DEEPSEEK_INFRA_ROOT"] = tmp
        runtime_root = Path(tmp)
        configure_automation_runtime(runtime_root)
        checks, details = run_automation_smoke(runtime_root)
        evidence = build_evidence(args.version, checks, details)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if evidence["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

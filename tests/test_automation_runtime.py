from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from deepseek_infra.core import config
from deepseek_infra.infra.automation import history, registry, runner, scheduler
from deepseek_infra.infra.workspace import artifacts, projects, saved_items


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "automation"


def test_automation_crud_manual_run_history_and_trace(tmp_settings: Path) -> None:
    project = projects.create_project("Automation Project")
    automation = registry.create_automation(
        {
            "projectId": project["projectId"],
            "name": "Daily summary",
            "trigger": {"type": "manual"},
            "condition": {"type": "always"},
            "action": {"type": "project_summary"},
            "output": {"saveToProject": True, "createArtifact": True, "artifactType": "markdown"},
            "policy": {"maxRunsPerDay": 5, "allowBrowser": False, "allowNetwork": False},
        }
    )

    updated = registry.update_automation(automation["automationId"], {"description": "updated"})
    disabled = registry.set_automation_enabled(automation["automationId"], False)
    enabled = registry.set_automation_enabled(automation["automationId"], True)
    run = runner.run_once(automation["automationId"])
    runs = history.list_runs(automation_id=automation["automationId"])

    assert updated["description"] == "updated"
    assert disabled["enabled"] is False
    assert enabled["enabled"] is True
    assert run["status"] == "success"
    assert run["outputs"]["artifactIds"]
    assert run["outputs"]["savedItemIds"]
    assert run["traceId"]
    assert runs[0]["runId"] == run["runId"]
    assert artifacts.list_artifacts(project["projectId"])
    assert saved_items.list_saved_items(project["projectId"])

    assert registry.delete_automation(automation["automationId"]) == 1


def test_automation_schedule_event_and_policy_block(tmp_settings: Path) -> None:
    project = projects.create_project("Automation Triggers")
    scheduled = registry.create_automation(
        {
            "projectId": project["projectId"],
            "name": "Scheduled",
            "trigger": {"type": "schedule", "cron": "30 9 * * *"},
            "action": {"type": "save_item", "title": "scheduled", "content": "scheduled"},
            "policy": {"maxRunsPerDay": 5, "allowBrowser": False, "allowNetwork": False},
        }
    )
    event_auto = registry.create_automation(
        {
            "projectId": project["projectId"],
            "name": "Event",
            "trigger": {"type": "event", "event": "saved_item.created"},
            "condition": {"type": "new_saved_items", "newSavedItems": True},
            "action": {"type": "save_item", "title": "event", "content": "event"},
            "policy": {"maxRunsPerDay": 5, "allowBrowser": False, "allowNetwork": False},
        }
    )
    unsafe = registry.create_automation(
        {
            "projectId": project["projectId"],
            "name": "Unsafe",
            "trigger": {"type": "manual"},
            "action": {"type": "browser_snapshot", "url": "http://127.0.0.1:9/private"},
            "policy": {"maxRunsPerDay": 5, "allowBrowser": True, "allowNetwork": True},
        }
    )

    schedule_result = scheduler.run_due(now=datetime(2026, 7, 4, 9, 30, tzinfo=timezone.utc))
    event_result = scheduler.run_due(event={"event": "saved_item.created", "projectId": project["projectId"]})
    unsafe_run = runner.run_once(unsafe["automationId"])

    assert any(run["automationId"] == scheduled["automationId"] and run["status"] == "success" for run in schedule_result["runs"])
    assert any(run["automationId"] == event_auto["automationId"] and run["status"] == "success" for run in event_result["runs"])
    assert unsafe_run["status"] == "skipped"
    assert "unsafe_url" in unsafe_run["skippedReason"]


def test_automation_browser_snapshot_uses_browser_safety(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "BROWSER_CONTROL_ENABLED", True)
    monkeypatch.setattr(config, "BROWSER_REQUIRE_CONFIRM", True)
    monkeypatch.setattr(config, "BROWSER_ALLOW_PRIVATE_HOSTS", False)
    project = projects.create_project("Automation Browser")
    automation = registry.create_automation(
        {
            "projectId": project["projectId"],
            "name": "Browser snapshot",
            "trigger": {"type": "manual"},
            "action": {"type": "browser_snapshot", "url": (FIXTURES / "webpage_v1.html").as_uri(), "selector": "#content"},
            "policy": {"maxRunsPerDay": 5, "allowBrowser": True, "browserMode": "read_only", "allowNetwork": False},
        }
    )

    run = runner.run_once(automation["automationId"])

    assert run["status"] == "success"
    assert run["outputs"]["mediaIds"]
    assert run["evidence"]["policy"]["verdict"] == "allow"

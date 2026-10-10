from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core import config
from deepseek_infra.infra.automation import actions as automation_actions
from deepseek_infra.infra.automation import history, registry, runner, scheduler, triggers
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
    second_schedule_result = scheduler.run_due(now=datetime(2026, 7, 4, 9, 30, tzinfo=timezone.utc))
    event_result = scheduler.run_due(event={"event": "saved_item.created", "projectId": project["projectId"]})
    unsafe_run = runner.run_once(unsafe["automationId"])

    assert any(run["automationId"] == scheduled["automationId"] and run["status"] == "success" for run in schedule_result["runs"])
    assert not any(run["automationId"] == scheduled["automationId"] for run in second_schedule_result["runs"])
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


def test_automation_browser_check_fixture_bounds_and_change_detection(tmp_settings: Path) -> None:
    project = projects.create_project("Automation Browser Check")
    automation = registry.create_automation(
        {
            "projectId": project["projectId"],
            "name": "Fixture check",
            "trigger": {"type": "manual"},
            "action": {"type": "browser_check", "fixturePath": str(FIXTURES / "webpage_v1.html")},
            "policy": {"maxRunsPerDay": 5, "allowBrowser": True, "browserMode": "read_only", "allowNetwork": False},
        }
    )

    first = runner.run_once(automation["automationId"])
    second = runner.run_once(automation["automationId"])
    registry.update_automation(
        automation["automationId"],
        {"action": {"type": "browser_check", "fixturePath": str(FIXTURES / "webpage_v2.html")}},
    )
    third = runner.run_once(automation["automationId"])
    blocked = registry.create_automation(
        {
            "projectId": project["projectId"],
            "name": "Blocked fixture",
            "trigger": {"type": "manual"},
            "action": {"type": "browser_check", "fixturePath": str(Path(__file__))},
            "policy": {"maxRunsPerDay": 5, "allowBrowser": True, "browserMode": "read_only", "allowNetwork": False},
        }
    )
    blocked_run = runner.run_once(blocked["automationId"])

    assert first["status"] == "success"
    assert first["outputs"]["savedItemIds"]
    assert second["status"] == "skipped"
    assert second["skippedReason"] == "url_unchanged"
    assert third["status"] == "success"
    assert third["outputs"]["savedItemIds"]
    assert blocked_run["status"] == "failed"
    assert "fixturePath is outside allowed" in blocked_run["error"]


def test_automation_browser_snapshot_closes_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.browser import actions as browser_actions

    calls: list[str] = []

    def fake_execute(payload: dict[str, object]) -> dict[str, object]:
        action = str(payload.get("action") or "")
        calls.append(action)
        if action == "open_url":
            return {"ok": True, "session": {"browserSessionId": "browser_test_session"}}
        if action == "read_page":
            return {"ok": True, "result": {"text": "hello", "snapshot": {"mediaId": "media_page"}}}
        if action == "screenshot":
            return {"ok": True, "result": {"screenshot": {"mediaId": "media_shot"}}}
        if action == "close_session":
            return {"ok": True, "result": {"closed": True}}
        raise AssertionError(f"unexpected browser action {action}")

    monkeypatch.setattr(browser_actions, "execute_browser_action", fake_execute)

    result = automation_actions.browser_snapshot_action(
        {
            "automationId": "auto_browser_close",
            "action": {"type": "browser_snapshot", "url": "file:///tmp/page.html", "screenshot": True},
            "policy": {"allowBrowser": True},
        }
    )

    assert calls == ["open_url", "read_page", "screenshot", "close_session"]
    assert result["outputs"]["mediaIds"] == ["media_page", "media_shot"]
    assert "browserSessionClosed" in result["logs"]


def test_automation_cron_supports_steps_ranges_and_sunday_alias() -> None:
    monday = datetime(2026, 7, 6, 9, 10, tzinfo=timezone.utc)
    sunday = datetime(2026, 7, 5, 9, 0, tzinfo=timezone.utc)

    assert triggers.cron_matches("*/5 9-17 * * 1-5", monday)
    assert not triggers.cron_matches("*/5 9-17 * * 1-5", monday.replace(minute=11))
    assert not triggers.cron_matches("*/5 9-17 * * 1-5", monday.replace(hour=18))
    assert triggers.cron_matches("0 9 * * 7", sunday)
    assert triggers.cron_matches("0 9 * * 0", sunday)


def test_automation_policy_daily_limit_uses_supplied_now(tmp_settings: Path) -> None:
    project = projects.create_project("Automation Daily Limit")
    automation = registry.create_automation(
        {
            "projectId": project["projectId"],
            "name": "Limited",
            "trigger": {"type": "manual"},
            "action": {"type": "save_item", "title": "limited", "content": "daily"},
            "policy": {"maxRunsPerDay": 1, "allowBrowser": False, "allowNetwork": False},
        }
    )
    day_one = datetime(2026, 1, 2, 8, 0, tzinfo=timezone.utc)
    day_two = datetime(2026, 1, 3, 8, 0, tzinfo=timezone.utc)

    first = runner.run_once(automation["automationId"], now=day_one)
    second = runner.run_once(automation["automationId"], now=day_one)
    third = runner.run_once(automation["automationId"], now=day_two)

    assert first["status"] == "success"
    assert first["startedAt"].startswith("2026-01-02")
    assert second["status"] == "skipped"
    assert "max_runs_per_day_exceeded" in second["skippedReason"]
    assert third["status"] == "success"
    assert third["startedAt"].startswith("2026-01-03")


def test_automation_retry_backoff_rerun_and_template_creation(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = projects.create_project("Automation Retry")
    automation = registry.create_automation(
        {
            "projectId": project["projectId"],
            "name": "Flaky",
            "trigger": {"type": "manual"},
            "action": {"type": "save_item", "title": "retry", "content": "retry"},
            "policy": {"maxRunsPerDay": 5, "allowBrowser": False, "allowNetwork": False, "retry": {"maxAttempts": 2, "backoffSeconds": 2}},
        }
    )
    original_run_action = automation_actions.run_action
    calls: list[int] = []
    slept: list[int] = []

    def flaky_action(
        automation_payload: dict[str, Any],
        *,
        run_id: str,
        trigger: dict[str, Any],
        event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient failure")
        return original_run_action(automation_payload, run_id=run_id, trigger=trigger, event=event)

    monkeypatch.setattr(automation_actions, "run_action", flaky_action)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: slept.append(int(seconds)))

    run = runner.run_once(automation["automationId"])
    rerun = runner.rerun(run["runId"])
    template = registry.create_from_template(
        "daily_project_summary",
        project_id=project["projectId"],
        overrides={"name": "Template summary", "trigger": {"type": "manual"}},
    )

    assert run["status"] == "success"
    assert run["attempts"] == 2
    assert slept == [2]
    assert run["evidence"]["runtime"]["backoffSeconds"] == 2
    assert run["evidence"]["runtime"]["attemptErrors"][0]["error"] == "transient failure"
    assert rerun["status"] == "success"
    assert rerun["trigger"]["rerunOf"] == run["runId"]
    assert template["name"] == "Template summary"
    assert template["action"]["type"] == "project_summary"


def test_automation_timeout_failure_records_runtime_evidence(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = projects.create_project("Automation Timeout")
    automation = registry.create_automation(
        {
            "projectId": project["projectId"],
            "name": "Timeout",
            "trigger": {"type": "manual"},
            "action": {"type": "save_item", "title": "timeout", "content": "timeout"},
            "policy": {"maxRunsPerDay": 5, "allowBrowser": False, "allowNetwork": False, "timeoutSeconds": 1, "retry": {"maxAttempts": 1}},
        }
    )
    monotonic_values = iter([0.0, 0.0, 0.0, 2.0, 2.0, 2.0, 2.0])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(monotonic_values))

    run = runner.run_once(automation["automationId"])

    assert run["status"] == "failed"
    assert "exceeded timeout" in run["error"]
    assert run["evidence"]["runtime"]["timeoutSeconds"] == 1
    assert run["evidence"]["runtime"]["timeoutCheckedAtMs"] > 0
    assert "exceeded timeout" in run["evidence"]["runtime"]["attemptErrors"][0]["error"]

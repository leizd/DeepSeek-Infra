"""In-process scheduler helpers for Automation Runtime."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from deepseek_infra.infra.automation import registry, runner, triggers


def due_automations(*, now: datetime | None = None, event: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    due: list[dict[str, Any]] = []
    for automation in registry.list_automations(include_disabled=False):
        raw_trigger = automation.get("trigger")
        trigger: dict[str, Any] = raw_trigger if isinstance(raw_trigger, dict) else {}
        if str(trigger.get("type") or "manual") == "manual":
            continue
        matched, _ = triggers.trigger_matches(automation, trigger=trigger, event=event, now=now)
        if matched:
            due.append(automation)
    return due


def run_due(*, now: datetime | None = None, event: dict[str, Any] | None = None, confirmed: bool = False) -> dict[str, Any]:
    runs = []
    for automation in due_automations(now=now, event=event):
        trigger = automation.get("trigger") if isinstance(automation.get("trigger"), dict) else {"type": "manual"}
        runs.append(runner.run_once(str(automation["automationId"]), trigger=trigger, event=event, now=now, confirmed=confirmed))
    return {
        "ok": True,
        "dueCount": len(runs),
        "runs": runs,
        "summary": {
            "success": sum(1 for run in runs if run.get("status") == "success"),
            "failed": sum(1 for run in runs if run.get("status") == "failed"),
            "skipped": sum(1 for run in runs if run.get("status") == "skipped"),
            "requiresConfirmation": sum(1 for run in runs if run.get("status") == "requires_confirmation"),
        },
    }


def simulate_trigger(
    automation_id: str,
    *,
    trigger: dict[str, Any] | None = None,
    event: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    automation = registry.get_automation(automation_id)
    trigger_data = trigger if isinstance(trigger, dict) else automation.get("trigger")
    matched, reason = triggers.trigger_matches(automation, trigger=trigger_data if isinstance(trigger_data, dict) else {}, event=event, now=now)
    condition, condition_reason = triggers.condition_matches(automation, event=event)
    return {
        "ok": True,
        "automationId": automation_id,
        "triggerMatched": matched,
        "triggerReason": reason,
        "conditionMatched": condition,
        "conditionReason": condition_reason,
        "wouldRun": matched and condition,
    }

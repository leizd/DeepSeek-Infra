"""Trigger and condition helpers for Automation Runtime."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from deepseek_infra.core.config import AUTOMATION_MIN_INTERVAL_SECONDS
from deepseek_infra.infra.automation import history


def trigger_matches(
    automation: dict[str, Any],
    *,
    trigger: dict[str, Any] | None = None,
    event: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    actual = trigger if isinstance(trigger, dict) else automation.get("trigger")
    trigger_data = actual if isinstance(actual, dict) else {}
    trigger_type = str(trigger_data.get("type") or "manual").strip().lower()
    current = _utc(now)
    if trigger_type == "manual":
        return True, ""
    if trigger_type == "event":
        event_data = event if isinstance(event, dict) else {}
        expected = str(trigger_data.get("event") or "").strip().lower()
        actual_event = str(event_data.get("event") or event_data.get("type") or "").strip().lower()
        return (bool(expected and expected == actual_event), "event_not_matched" if expected != actual_event else "")
    if trigger_type == "interval":
        last = history.latest_run(str(automation.get("automationId") or ""), statuses={"success", "failed"})
        interval = max(AUTOMATION_MIN_INTERVAL_SECONDS, _safe_int(trigger_data.get("intervalSeconds"), default=AUTOMATION_MIN_INTERVAL_SECONDS))
        if not last:
            return True, ""
        elapsed_ms = int(current.timestamp() * 1000) - int(last.get("startedAtMs") or 0)
        if elapsed_ms >= interval * 1000:
            return True, ""
        return False, "interval_not_due"
    if trigger_type == "schedule":
        cron = str(trigger_data.get("cron") or "").strip()
        if not cron_matches(cron, current):
            return False, "schedule_not_due"
        last = history.latest_run(str(automation.get("automationId") or ""), statuses={"success", "failed", "skipped"})
        if not last:
            return True, ""
        last_dt = datetime.fromtimestamp(int(last.get("startedAtMs") or 0) / 1000, tz=timezone.utc)
        if last_dt.date() == current.date() and _same_cron_minute(last_dt, current):
            return False, "schedule_already_ran"
        return True, ""
    return False, "unsupported_trigger"


def condition_matches(
    automation: dict[str, Any],
    *,
    event: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    raw_condition = automation.get("condition")
    condition: dict[str, Any] = raw_condition if isinstance(raw_condition, dict) else {}
    condition_type = str(condition.get("type") or "always").strip().lower()
    if condition_type == "always":
        return True, ""
    if condition_type == "project_changed" or condition.get("projectChanged"):
        return project_changed(automation)
    if condition_type == "media_ready" or condition.get("newMediaReady"):
        return event_flag(event, "media.ready", "media_not_ready")
    if condition_type == "new_saved_items" or condition.get("newSavedItems"):
        return event_flag(event, "saved_item.created", "saved_item_not_created")
    if condition_type == "artifact_created" or condition.get("artifactCreated"):
        return event_flag(event, "artifact.created", "artifact_not_created")
    if condition_type == "url_changed" or condition.get("urlChanged"):
        return True, ""
    return False, "condition_not_met"


def project_changed(automation: dict[str, Any]) -> tuple[bool, str]:
    project_id = str(automation.get("projectId") or "")
    if not project_id:
        return True, ""
    try:
        from deepseek_infra.infra.workspace.projects import get_project

        project = get_project(project_id)
    except Exception:
        return False, "project_not_found"
    last = history.latest_run(str(automation.get("automationId") or ""), statuses={"success"})
    if not last:
        return True, ""
    updated_at = int(project.get("updatedAtMs") or 0)
    last_finished = int(last.get("finishedAtMs") or last.get("startedAtMs") or 0)
    return (updated_at > last_finished, "project_unchanged" if updated_at <= last_finished else "")


def event_flag(event: dict[str, Any] | None, expected: str, reason: str) -> tuple[bool, str]:
    data = event if isinstance(event, dict) else {}
    actual = str(data.get("event") or data.get("type") or "").strip().lower()
    return (actual == expected, "" if actual == expected else reason)


def cron_matches(cron: str, now: datetime | None = None) -> bool:
    parts = str(cron or "").split()
    if len(parts) != 5:
        return False
    current = _utc(now)
    # Cron uses 0/7 for Sunday; Python weekday uses 0 for Monday.
    cron_weekday = (current.weekday() + 1) % 7
    fields = (
        (parts[0], current.minute, 0, 59),
        (parts[1], current.hour, 0, 23),
        (parts[2], current.day, 1, 31),
        (parts[3], current.month, 1, 12),
        (parts[4], cron_weekday, 0, 7),
    )
    return all(_cron_field_matches(part, value, minimum, maximum) for part, value, minimum, maximum in fields)


def _cron_field_matches(field: str, value: int, minimum: int, maximum: int) -> bool:
    raw = str(field or "").strip()
    if raw == "*":
        return True
    for part in raw.split(","):
        if _cron_part_matches(part.strip(), value, minimum, maximum):
            return True
    return False


def _cron_part_matches(part: str, value: int, minimum: int, maximum: int) -> bool:
    if not part:
        return False
    base = part
    step = 1
    if "/" in part:
        base, raw_step = part.split("/", 1)
        try:
            step = int(raw_step)
        except ValueError:
            return False
        if step <= 0:
            return False
    if base == "*":
        start, end = minimum, maximum
    elif "-" in base:
        raw_start, raw_end = base.split("-", 1)
        try:
            start, end = int(raw_start), int(raw_end)
        except ValueError:
            return False
        if start > end:
            return False
    else:
        try:
            start = end = int(base)
        except ValueError:
            return False
    if start < minimum or end > maximum:
        return False
    if value == 0 and maximum == 7 and start <= 7 <= end and (7 - start) % step == 0:
        return True
    return start <= value <= end and (value - start) % step == 0


def _same_cron_minute(first: datetime, second: datetime) -> bool:
    return first.year == second.year and first.month == second.month and first.day == second.day and first.hour == second.hour and first.minute == second.minute


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(tz=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default

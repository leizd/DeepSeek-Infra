"""Automation run history store."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core.config import AUTOMATION_DIR
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.automation import schema
from deepseek_infra.infra.workspace.schema import read_json_file, timestamp_ms_to_iso, validate_project_id, write_json_atomic

STORE_NAME = "history.json"
MAX_RUNS = 2_000


def list_runs(
    *,
    automation_id: str = "",
    project_id: str = "",
    status: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    safe_automation_id = schema.validate_automation_id(automation_id) if automation_id else ""
    safe_project_id = validate_project_id(project_id) if project_id else ""
    safe_status = str(status or "").strip().lower()
    runs = _load_runs()
    if safe_automation_id:
        runs = [run for run in runs if run.get("automationId") == safe_automation_id]
    if safe_project_id:
        runs = [run for run in runs if run.get("projectId") == safe_project_id]
    if safe_status:
        runs = [run for run in runs if run.get("status") == safe_status]
    safe_limit = max(0, min(int(limit or 100), MAX_RUNS))
    sorted_runs = sorted(runs, key=lambda item: int(item.get("startedAtMs") or 0), reverse=True)
    return sorted_runs[:safe_limit] if safe_limit else sorted_runs


def get_run(run_id: str) -> dict[str, Any]:
    safe_id = schema.validate_run_id(run_id)
    for run in _load_runs():
        if run.get("runId") == safe_id:
            return run
    raise AppError("Automation run not found", code=ErrorCode.NOT_FOUND, status=404)


def latest_run(automation_id: str, *, statuses: set[str] | None = None) -> dict[str, Any] | None:
    safe_id = schema.validate_automation_id(automation_id)
    allowed = statuses or set()
    for run in list_runs(automation_id=safe_id, limit=MAX_RUNS):
        if allowed and str(run.get("status") or "") not in allowed:
            continue
        return run
    return None


def runs_today(automation_id: str, *, now: datetime | None = None) -> int:
    safe_id = schema.validate_automation_id(automation_id)
    current = now or datetime.now(tz=timezone.utc)
    start = datetime(current.year, current.month, current.day, tzinfo=timezone.utc).timestamp() * 1000
    end = start + 86_400_000
    total = 0
    for run in _load_runs():
        if run.get("automationId") != safe_id:
            continue
        started = int(run.get("startedAtMs") or 0)
        if start <= started < end and run.get("status") not in {"skipped", "requires_confirmation"}:
            total += 1
    return total


def record_run(record: dict[str, Any]) -> dict[str, Any]:
    run = normalize_run_record(record)
    runs = [item for item in _load_runs() if item.get("runId") != run["runId"]]
    runs.append(run)
    _write_runs(runs)
    return run


def normalize_run_record(record: dict[str, Any]) -> dict[str, Any]:
    data = record if isinstance(record, dict) else {}
    run_id = str(data.get("runId") or "").strip() or schema.new_run_id()
    run_id = schema.validate_run_id(run_id)
    automation_id = schema.validate_automation_id(str(data.get("automationId") or ""))
    project_id = str(data.get("projectId") or "").strip()
    safe_project_id = validate_project_id(project_id) if project_id else ""
    status = str(data.get("status") or "failed").strip().lower()
    if status not in schema.RUN_STATUSES:
        status = "failed"
    started_at_ms = _safe_int(data.get("startedAtMs"), default=_now_ms())
    finished_at_ms = _safe_int(data.get("finishedAtMs"), default=started_at_ms)
    duration_ms = max(0, _safe_int(data.get("durationMs"), default=finished_at_ms - started_at_ms))
    return {
        "runId": run_id,
        "automationId": automation_id,
        "projectId": safe_project_id,
        "status": status,
        "startedAt": str(data.get("startedAt") or timestamp_ms_to_iso(started_at_ms)),
        "finishedAt": str(data.get("finishedAt") or timestamp_ms_to_iso(finished_at_ms)),
        "startedAtMs": started_at_ms,
        "finishedAtMs": finished_at_ms,
        "durationMs": duration_ms,
        "trigger": data.get("trigger") if isinstance(data.get("trigger"), dict) else {"type": "manual"},
        "outputs": schema.public_run_outputs(data.get("outputs")),
        "traceId": str(data.get("traceId") or "")[:120],
        "attempts": max(1, _safe_int(data.get("attempts"), default=1)),
        "error": str(data.get("error") or "")[:4_000],
        "skippedReason": str(data.get("skippedReason") or "")[:1_000],
        "logs": _string_list(data.get("logs"), limit=200),
        "evidence": data.get("evidence") if isinstance(data.get("evidence"), dict) else {},
    }


def store_path() -> Path:
    return AUTOMATION_DIR / STORE_NAME


def _load_runs() -> list[dict[str, Any]]:
    data = read_json_file(store_path(), default={"runs": []})
    raw_runs = data.get("runs")
    if not isinstance(raw_runs, list):
        return []
    runs: list[dict[str, Any]] = []
    for raw in raw_runs:
        if not isinstance(raw, dict):
            continue
        try:
            runs.append(normalize_run_record(raw))
        except AppError:
            continue
    return runs[-MAX_RUNS:]


def _write_runs(runs: list[dict[str, Any]]) -> None:
    write_json_atomic(store_path(), {"runs": runs[-MAX_RUNS:]})


def _string_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item or "")[:1_000] for item in value[:limit]]


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default

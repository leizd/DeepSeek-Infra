"""Local Skill run history, analytics, diagnostics, and retention helpers."""

from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import utc_now_iso
from deepseek_infra.infra.skills import registry

RUN_SCHEMA_VERSION = "skill-run.v1"
DEFAULT_RETENTION = 500
SUMMARY_LIMIT = 600

FAILURE_SUGGESTIONS = {
    "schema_validation_failed": "Review the Skill input/output schema and make sure required fields are present.",
    "tool_policy_denied": "Review allowedTools and the Tool Policy audit before granting additional capability.",
    "artifact_policy_failed": "Check artifactPolicy types and whether the run produced persistable content.",
    "project_binding_failed": "Verify the project exists and projectBinding.enabled is true for this Skill.",
    "llm_api_error": "Retry offline or check API key, model route, and upstream diagnostics.",
    "timeout": "Reduce input size or retry with a simpler Skill run.",
    "user_cancelled": "Run was cancelled before completion.",
    "unknown_error": "Inspect the linked trace and run metadata.",
}


def runs_dir() -> Path:
    return registry.SKILLS_DIR / "runs"


def runs_path() -> Path:
    return runs_dir() / "runs.jsonl"


def record_success(
    *,
    skill: dict[str, Any],
    result: dict[str, Any],
    offline: bool,
    model: str = "",
    retention: int = DEFAULT_RETENTION,
) -> dict[str, Any]:
    output = _dict(result.get("output"))
    artifacts = _dict_list(result.get("artifacts"))
    saved_items = _dict_list(result.get("savedItems"))
    record = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "skillRunId": str(result.get("skillRunId") or ""),
        "skillId": str(result.get("skillId") or skill.get("skillId") or ""),
        "skillVersion": str(skill.get("version") or ""),
        "packId": _pack_for_skill(str(skill.get("skillId") or result.get("skillId") or "")),
        "projectId": str(result.get("projectId") or ""),
        "status": str(result.get("status") or "completed"),
        "startedAt": str(result.get("startedAt") or ""),
        "completedAt": str(result.get("completedAt") or utc_now_iso()),
        "latencyMs": _latency_ms(str(result.get("startedAt") or ""), str(result.get("completedAt") or "")),
        "offline": bool(offline),
        "model": str(model or output.get("model") or ""),
        "inputSummary": summarize_payload(result.get("input")),
        "outputSummary": summarize_payload(output.get("content") or output),
        "artifactIds": _artifact_ids(artifacts),
        "savedItemIds": _saved_item_ids(saved_items),
        "artifactCount": len(artifacts),
        "savedItemCount": len(saved_items),
        "errorReason": "",
        "failureCategory": "",
        "diagnosticSuggestion": "",
        "traceId": str(result.get("traceId") or ""),
        "redacted": False,
    }
    return append_run(record, retention=retention)


def record_failure(
    *,
    skill: dict[str, Any],
    run_id: str,
    input_data: Any,
    project_id: str = "",
    started_at: str = "",
    trace_id: str = "",
    error: BaseException | str = "",
    offline: bool = False,
    model: str = "",
    category: str = "",
    retention: int = DEFAULT_RETENTION,
) -> dict[str, Any]:
    completed_at = utc_now_iso()
    reason = str(error or "")
    failure_category = category or classify_failure(reason)
    record = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "skillRunId": run_id,
        "skillId": str(skill.get("skillId") or ""),
        "skillVersion": str(skill.get("version") or ""),
        "packId": _pack_for_skill(str(skill.get("skillId") or "")),
        "projectId": str(project_id or ""),
        "status": "failed",
        "startedAt": started_at or completed_at,
        "completedAt": completed_at,
        "latencyMs": _latency_ms(started_at or completed_at, completed_at),
        "offline": bool(offline),
        "model": str(model or ""),
        "inputSummary": summarize_payload(input_data),
        "outputSummary": "",
        "artifactIds": [],
        "savedItemIds": [],
        "artifactCount": 0,
        "savedItemCount": 0,
        "errorReason": reason[:1200],
        "failureCategory": failure_category,
        "diagnosticSuggestion": FAILURE_SUGGESTIONS.get(failure_category, FAILURE_SUGGESTIONS["unknown_error"]),
        "traceId": str(trace_id or ""),
        "redacted": False,
    }
    return append_run(record, retention=retention)


def append_run(record: dict[str, Any], *, retention: int = DEFAULT_RETENTION) -> dict[str, Any]:
    normalized = normalize_run(record)
    runs = [item for item in list_runs(limit=0, include_redacted=True) if item.get("skillRunId") != normalized["skillRunId"]]
    runs.insert(0, normalized)
    _write_runs(runs[: max(1, int(retention or DEFAULT_RETENTION))])
    return normalized


def list_runs(
    *,
    skill_id: str = "",
    pack_id: str = "",
    project_id: str = "",
    status: str = "",
    limit: int = 50,
    include_redacted: bool = True,
) -> list[dict[str, Any]]:
    runs = _read_runs()
    result: list[dict[str, Any]] = []
    for run in runs:
        if skill_id and run.get("skillId") != skill_id:
            continue
        if pack_id and run.get("packId") != pack_id:
            continue
        if project_id and run.get("projectId") != project_id:
            continue
        if status and run.get("status") != status:
            continue
        if not include_redacted and run.get("redacted"):
            continue
        result.append(run)
    if limit and int(limit) > 0:
        return result[: min(int(limit), DEFAULT_RETENTION)]
    return result


def get_run(skill_run_id: str) -> dict[str, Any]:
    run_id = str(skill_run_id or "").strip()
    for run in _read_runs():
        if run.get("skillRunId") == run_id:
            return run
    raise AppError("Skill run not found", code=ErrorCode.NOT_FOUND, status=404)


def delete_run(skill_run_id: str) -> dict[str, Any]:
    run_id = str(skill_run_id or "").strip()
    runs = _read_runs()
    kept = [run for run in runs if run.get("skillRunId") != run_id]
    _write_runs(kept)
    return {"ok": True, "deleted": len(runs) - len(kept), "skillRunId": run_id}


def cleanup_runs(*, status: str = "", skill_id: str = "", pack_id: str = "", project_id: str = "", keep_recent: int = 0) -> dict[str, Any]:
    runs = _read_runs()
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    matched = 0
    for run in runs:
        if _matches(run, status=status, skill_id=skill_id, pack_id=pack_id, project_id=project_id):
            matched += 1
            if keep_recent and matched <= keep_recent:
                kept.append(run)
            else:
                removed.append(run)
            continue
        kept.append(run)
    _write_runs(kept)
    return {"ok": True, "deleted": len(removed), "remaining": len(kept), "scope": {"status": status, "skillId": skill_id, "packId": pack_id, "projectId": project_id}}


def redact_run(skill_run_id: str) -> dict[str, Any]:
    run_id = str(skill_run_id or "").strip()
    runs = _read_runs()
    redacted: dict[str, Any] | None = None
    for index, run in enumerate(runs):
        if run.get("skillRunId") != run_id:
            continue
        redacted = {**run, "inputSummary": "[redacted]", "outputSummary": "[redacted]", "errorReason": "[redacted]", "redacted": True}
        runs[index] = normalize_run(redacted)
        break
    if redacted is None:
        raise AppError("Skill run not found", code=ErrorCode.NOT_FOUND, status=404)
    _write_runs(runs)
    return {"ok": True, "run": redacted}


def analytics_summary(
    *,
    scope: str = "all",
    skill_id: str = "",
    pack_id: str = "",
    project_id: str = "",
    days: int = 7,
) -> dict[str, Any]:
    scoped_skill = skill_id if scope == "skill" else ""
    scoped_pack = pack_id if scope == "pack" else ""
    scoped_project = project_id if scope == "project" else ""
    runs = list_runs(skill_id=scoped_skill, pack_id=scoped_pack, project_id=scoped_project, limit=0)
    total = len(runs)
    completed = [run for run in runs if run.get("status") == "completed"]
    failed = [run for run in runs if run.get("status") == "failed"]
    latencies = [int(run.get("latencyMs") or 0) for run in completed if int(run.get("latencyMs") or 0) >= 0]
    total_artifacts = sum(int(run.get("artifactCount") or 0) for run in runs)
    total_saved = sum(int(run.get("savedItemCount") or 0) for run in runs)
    result = {
        "scope": scope,
        "skillId": skill_id,
        "packId": pack_id,
        "projectId": project_id,
        "totalRuns": total,
        "successRuns": len(completed),
        "failedRuns": len(failed),
        "successRate": round(len(completed) / total, 4) if total else 0.0,
        "failureRate": round(len(failed) / total, 4) if total else 0.0,
        "averageLatencyMs": round(statistics.fmean(latencies), 2) if latencies else 0,
        "p50LatencyMs": _percentile(latencies, 50),
        "p90LatencyMs": _percentile(latencies, 90),
        "artifactCount": total_artifacts,
        "savedItemCount": total_saved,
        "projectBindingRuns": sum(1 for run in runs if run.get("projectId")),
        "topSkills": _top_counts(run.get("skillId") for run in runs),
        "topPacks": _top_counts(run.get("packId") for run in runs if run.get("packId")),
        "failureCategories": _top_counts(run.get("failureCategory") for run in failed if run.get("failureCategory")),
        "recentTrend": _recent_trend(runs, days=max(1, min(int(days or 7), 30))),
        "recentRuns": runs[:10],
        "generatedAt": utc_now_iso(),
    }
    return result


def project_run_record(record: dict[str, Any], *, input_data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "skillRunId": record.get("skillRunId"),
        "skillId": record.get("skillId"),
        "skillVersion": record.get("skillVersion"),
        "packId": record.get("packId"),
        "status": record.get("status"),
        "projectId": record.get("projectId"),
        "input": input_data if isinstance(input_data, dict) else {},
        "inputSummary": record.get("inputSummary"),
        "outputSummary": record.get("outputSummary"),
        "artifactIds": record.get("artifactIds") if isinstance(record.get("artifactIds"), list) else [],
        "savedItemIds": record.get("savedItemIds") if isinstance(record.get("savedItemIds"), list) else [],
        "artifactCount": record.get("artifactCount"),
        "savedItemCount": record.get("savedItemCount"),
        "traceId": record.get("traceId"),
        "startedAt": record.get("startedAt"),
        "completedAt": record.get("completedAt"),
        "latencyMs": record.get("latencyMs"),
        "offline": record.get("offline"),
        "model": record.get("model"),
        "errorReason": record.get("errorReason"),
        "failureCategory": record.get("failureCategory"),
        "diagnosticSuggestion": record.get("diagnosticSuggestion"),
    }


def normalize_run(record: dict[str, Any]) -> dict[str, Any]:
    run_id = str(record.get("skillRunId") or record.get("runId") or "").strip()
    if not run_id:
        raise AppError("skillRunId is required", code=ErrorCode.INVALID_PAYLOAD)
    trace_id = str(record.get("traceId") or "")[:80]
    project_id = str(record.get("projectId") or "")[:80]
    artifact_ids = _strings(record.get("artifactIds"))[:40]
    saved_item_ids = _strings(record.get("savedItemIds"))[:40]
    return {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "skillRunId": run_id[:80],
        "skillId": str(record.get("skillId") or "")[:80],
        "skillVersion": str(record.get("skillVersion") or "")[:40],
        "packId": str(record.get("packId") or "")[:80],
        "projectId": project_id,
        "status": str(record.get("status") or "completed")[:40],
        "startedAt": str(record.get("startedAt") or ""),
        "completedAt": str(record.get("completedAt") or ""),
        "latencyMs": max(0, int(record.get("latencyMs") or 0)),
        "offline": bool(record.get("offline")),
        "model": str(record.get("model") or "")[:120],
        "inputSummary": str(record.get("inputSummary") or "")[:SUMMARY_LIMIT],
        "outputSummary": str(record.get("outputSummary") or "")[:SUMMARY_LIMIT],
        "artifactIds": artifact_ids,
        "savedItemIds": saved_item_ids,
        "artifactCount": _safe_int(record.get("artifactCount"), default=len(artifact_ids)),
        "savedItemCount": _safe_int(record.get("savedItemCount"), default=len(saved_item_ids)),
        "errorReason": str(record.get("errorReason") or "")[:1200],
        "failureCategory": str(record.get("failureCategory") or "")[:80],
        "diagnosticSuggestion": str(record.get("diagnosticSuggestion") or "")[:240],
        "traceId": trace_id,
        "links": _links(trace_id=trace_id, project_id=project_id, artifact_ids=artifact_ids, saved_item_ids=saved_item_ids),
        "redacted": bool(record.get("redacted")),
    }


def summarize_payload(value: Any, *, limit: int = SUMMARY_LIMIT) -> str:
    if isinstance(value, str):
        text = value
    elif isinstance(value, dict):
        parts = []
        for key, item in list(value.items())[:8]:
            parts.append(f"{key}={_short_value(item)}")
        text = ", ".join(parts) if parts else "{}"
    elif isinstance(value, list):
        text = f"{len(value)} items"
    else:
        text = str(value or "")
    text = " ".join(text.split())
    return text[:limit]


def classify_failure(message: str) -> str:
    lowered = str(message or "").lower()
    if "schema" in lowered or "validation" in lowered or "required" in lowered:
        return "schema_validation_failed"
    if "tool policy" in lowered or "permission" in lowered or "denied" in lowered or "forbidden" in lowered or "not allowed" in lowered:
        return "tool_policy_denied"
    if "artifact" in lowered:
        return "artifact_policy_failed"
    if "project" in lowered:
        return "project_binding_failed"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if "cancel" in lowered:
        return "user_cancelled"
    if "api" in lowered or "llm" in lowered or "deepseek" in lowered or "upstream" in lowered:
        return "llm_api_error"
    return "unknown_error"


def _read_runs() -> list[dict[str, Any]]:
    path = runs_path()
    if not path.exists():
        return []
    runs: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            try:
                runs.append(normalize_run(data))
            except AppError:
                continue
    return runs


def _write_runs(runs: list[dict[str, Any]]) -> None:
    runs_dir().mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(normalize_run(run), ensure_ascii=False, sort_keys=True) + "\n" for run in runs)
    runs_path().write_text(text, encoding="utf-8")


def _matches(run: dict[str, Any], *, status: str, skill_id: str, pack_id: str, project_id: str) -> bool:
    return (
        (not status or run.get("status") == status)
        and (not skill_id or run.get("skillId") == skill_id)
        and (not pack_id or run.get("packId") == pack_id)
        and (not project_id or run.get("projectId") == project_id)
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _artifact_ids(items: list[dict[str, Any]]) -> list[str]:
    return _strings([item.get("artifactId") or item.get("id") for item in items])


def _saved_item_ids(items: list[dict[str, Any]]) -> list[str]:
    return _strings([item.get("id") or item.get("savedItemId") for item in items])


def _pack_for_skill(skill_id: str) -> str:
    if not skill_id:
        return ""
    for pack in registry.list_packs(include_builtin=True):
        for entry in pack.get("skills") or []:
            if isinstance(entry, dict) and entry.get("skillId") == skill_id:
                return str(pack.get("packId") or "")
    return ""


def _latency_ms(started_at: str, completed_at: str) -> int:
    try:
        started = datetime.fromisoformat(started_at)
        completed = datetime.fromisoformat(completed_at)
    except ValueError:
        return 0
    return max(0, int((completed - started).total_seconds() * 1000))


def _links(*, trace_id: str, project_id: str, artifact_ids: list[str], saved_item_ids: list[str]) -> dict[str, Any]:
    links: dict[str, Any] = {}
    if trace_id:
        links["trace"] = f"/api/traces/{trace_id}"
    if project_id:
        links["projectRuns"] = f"/api/workspace/projects/{project_id}/skill-runs"
        links["projectAnalytics"] = f"/api/workspace/projects/{project_id}/skill-analytics"
        links["savedItems"] = f"/api/workspace/projects/{project_id}/saved-items" if saved_item_ids else ""
        links["artifacts"] = f"/api/workspace/projects/{project_id}/artifacts" if artifact_ids else ""
    return links


def _short_value(value: Any) -> str:
    if isinstance(value, str):
        return value[:80]
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    if isinstance(value, list):
        return f"{len(value)} items"
    if isinstance(value, dict):
        return f"{len(value)} fields"
    return str(value)[:80]


def _top_counts(values: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    counter = Counter(str(value or "") for value in values if str(value or ""))
    return [{"id": key, "count": count} for key, count in counter.most_common(limit)]


def _percentile(values: list[int], pct: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * pct / 100))
    return ordered[max(0, min(index, len(ordered) - 1))]


def _recent_trend(runs: list[dict[str, Any]], *, days: int) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).date()
    buckets: dict[str, dict[str, Any]] = {
        (now - timedelta(days=offset)).isoformat(): {"date": (now - timedelta(days=offset)).isoformat(), "runs": 0, "failed": 0}
        for offset in range(days)
    }
    for run in runs:
        raw = str(run.get("completedAt") or run.get("startedAt") or "")
        try:
            day = datetime.fromisoformat(raw).date().isoformat()
        except ValueError:
            continue
        if day not in buckets:
            continue
        buckets[day]["runs"] += 1
        if run.get("status") == "failed":
            buckets[day]["failed"] += 1
    return [buckets[key] for key in sorted(buckets)]


def _safe_int(value: Any, *, default: int = 0) -> int:
    if value is None:
        return max(0, default)
    try:
        return max(0, int(str(value)))
    except (TypeError, ValueError):
        return max(0, default)

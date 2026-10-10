"""Authoritative terminal effect telemetry for fair-service settlement (4.7.6 Gate D)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any


class EffectTelemetryUnavailable(RuntimeError):
    """A terminal Action cannot be bound to durable effect telemetry."""


def _canonical_digest(payload: Any) -> str:
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _non_negative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise EffectTelemetryUnavailable(f"invalid {field}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise EffectTelemetryUnavailable(f"invalid {field}") from exc
    if parsed < 0:
        raise EffectTelemetryUnavailable(f"invalid {field}")
    return parsed


def _non_negative_float(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        raise EffectTelemetryUnavailable(f"invalid {field}")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise EffectTelemetryUnavailable(f"invalid {field}") from exc
    if parsed < 0:
        raise EffectTelemetryUnavailable(f"invalid {field}")
    return parsed


def _elapsed_ms(started_at: Any, completed_at: Any) -> float:
    try:
        start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(completed_at).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise EffectTelemetryUnavailable("effect duration is unavailable") from exc
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise EffectTelemetryUnavailable("effect duration is unavailable")
    return (end - start).total_seconds() * 1000.0


def _validate_scope(action: dict[str, Any], effect: dict[str, Any]) -> None:
    raw_params = action.get("parameters")
    params = raw_params if isinstance(raw_params, dict) else {}
    for action_field, effect_field in (("policyId", "policyId"), ("backupId", "backupId")):
        expected = str(params.get(action_field) or "")
        observed = str(effect.get(effect_field) or "")
        if expected and observed != expected:
            raise EffectTelemetryUnavailable(f"effect {effect_field} does not match Action")


def _repair_telemetry(action: dict[str, Any], handle: dict[str, Any]) -> dict[str, Any]:
    from deepseek_infra.infra.workspace import backup_replication, backup_transfer_budget

    repair_id = str(handle.get("repairId") or "")
    job = backup_replication.read_repair_job(repair_id) if repair_id else None
    if not isinstance(job, dict) or str(job.get("repairId") or "") != repair_id:
        raise EffectTelemetryUnavailable("repair effect is unavailable")
    if str(job.get("resilienceActionId") or "") != str(action.get("actionId") or ""):
        raise EffectTelemetryUnavailable("repair effect is not bound to Action")
    if str(job.get("phase") or "") != "healthy":
        raise EffectTelemetryUnavailable("repair effect is not terminal-success")
    _validate_scope(action, job)
    raw_traffic_class = job.get("trafficClass")
    if raw_traffic_class is None:
        raise EffectTelemetryUnavailable("repair traffic class is unavailable")
    try:
        traffic_class = backup_transfer_budget.TrafficClass(int(raw_traffic_class)).name
    except (TypeError, ValueError):
        raise EffectTelemetryUnavailable("repair traffic class is unavailable") from None
    return {
        "effectType": "repair",
        "transferId": repair_id,
        "actualBytesTransferred": _non_negative_int(job.get("bytesRepaired"), field="bytesRepaired"),
        "actualDurationMs": _non_negative_float(job.get("durationMs"), field="durationMs"),
        "trafficClass": traffic_class,
        "source": "repair-job-ledger",
        "sourceDigest": _canonical_digest(job),
    }


def _rebalance_telemetry(action: dict[str, Any], handle: dict[str, Any]) -> dict[str, Any]:
    from deepseek_infra.infra.workspace import backup_replication, backup_transfer_budget

    job_id = str(handle.get("jobId") or "")
    job = backup_replication.read_rebalance_job(job_id) if job_id else None
    if not isinstance(job, dict) or str(job.get("jobId") or "") != job_id:
        raise EffectTelemetryUnavailable("rebalance effect is unavailable")
    if str(job.get("resilienceActionId") or "") != str(action.get("actionId") or ""):
        raise EffectTelemetryUnavailable("rebalance effect is not bound to Action")
    if str(job.get("phase") or "") != "complete":
        raise EffectTelemetryUnavailable("rebalance effect is not terminal-success")
    _validate_scope(action, job)
    duration = job.get("durationMs")
    if duration is None:
        duration = _elapsed_ms(job.get("createdAt"), job.get("updatedAt"))
    return {
        "effectType": "rebalance",
        "transferId": job_id,
        "actualBytesTransferred": _non_negative_int(job.get("bytesTransferred"), field="bytesTransferred"),
        "actualDurationMs": _non_negative_float(duration, field="durationMs"),
        "trafficClass": backup_transfer_budget.TrafficClass.P5_REBALANCE_DRAIN.name,
        "source": "rebalance-job-ledger",
        "sourceDigest": _canonical_digest(job),
    }


def _drill_telemetry(action: dict[str, Any], handle: dict[str, Any]) -> dict[str, Any]:
    from deepseek_infra.infra.workspace import backup_dr_readiness, backup_transfer_budget

    action_id = str(action.get("actionId") or "")
    record = backup_dr_readiness.get_dr_drill_by_resilience_action_id(action_id)
    if not isinstance(record, dict) or str(record.get("resilienceActionId") or "") != action_id:
        raise EffectTelemetryUnavailable("DR drill effect is unavailable")
    drill_id = str(record.get("drillId") or "")
    bound_drill_id = str(handle.get("drillId") or drill_id)
    if not drill_id or bound_drill_id != drill_id or str(record.get("result") or "") != "success":
        raise EffectTelemetryUnavailable("DR drill effect is not terminal-success")
    return {
        "effectType": "drill",
        "transferId": drill_id,
        "actualBytesTransferred": _non_negative_int(record.get("bytesRestored"), field="bytesRestored"),
        "actualDurationMs": _non_negative_float(float(record.get("rtoSeconds") or 0.0) * 1000.0, field="durationMs"),
        "trafficClass": backup_transfer_budget.TrafficClass.P4_SCRUB_DRILL.name,
        "source": "dr-drill-ledger",
        "sourceDigest": _canonical_digest(record),
    }


def read_terminal_effect_telemetry(action: dict[str, Any]) -> dict[str, Any]:
    """Resolve observed bytes/duration/class from the durable effect named by a terminal Action."""
    action_id = str(action.get("actionId") or "")
    if not action_id or str(action.get("state") or "") != "SUCCEEDED":
        raise EffectTelemetryUnavailable("Action is not terminal-success")
    if not isinstance(action.get("verificationResult"), dict):
        raise EffectTelemetryUnavailable("Action outcome is not verified")
    execution_epoch = _non_negative_int(action.get("executionEpoch"), field="executionEpoch")
    if execution_epoch < 1:
        raise EffectTelemetryUnavailable("Action execution epoch is unavailable")
    handle = action.get("effectHandle")
    if not isinstance(handle, dict):
        raise EffectTelemetryUnavailable("Action effect handle is unavailable")
    kind = str(handle.get("kind") or "").lower()
    if kind == "repair":
        effect = _repair_telemetry(action, handle)
    elif kind == "rebalance":
        effect = _rebalance_telemetry(action, handle)
    elif kind == "drill":
        effect = _drill_telemetry(action, handle)
    else:
        raise EffectTelemetryUnavailable("unsupported Action effect handle")
    telemetry: dict[str, Any] = {
        "actionId": action_id,
        "actionExecutionEpoch": execution_epoch,
        "effectHandle": handle,
        "outcome": "SUCCEEDED",
        **effect,
    }
    telemetry["telemetryDigest"] = _canonical_digest(telemetry)
    return telemetry

"""Production target-probe sampler feeding capacity history and forecast calibration."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Sequence

from deepseek_infra.infra.workspace import (
    backup_control,
    backup_targets,
    resilience_capacity_history,
    resilience_forecast_registry,
)


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _capacity_int(probe: dict[str, Any], field: str) -> int:
    value = probe.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"target capacity probe did not provide {field}")
    return value


def _policy_targets(policy: dict[str, Any]) -> set[str]:
    target_ids = {
        str(policy.get("targetId") or ""),
        str(policy.get("primaryTargetId") or ""),
    }
    replication = policy.get("replication")
    if isinstance(replication, dict):
        targets = replication.get("targets")
        if isinstance(targets, list):
            for item in targets:
                if isinstance(item, dict):
                    target_ids.add(str(item.get("targetId") or ""))
    target_ids.discard("")
    return target_ids


def _active_policy_count(target_id: str) -> int:
    count = 0
    for policy in backup_control.list_policies():
        if policy.get("enabled") is False:
            continue
        if target_id in _policy_targets(policy):
            count += 1
    return count


def sample_target_capacity(
    target_id: str,
    *,
    now: datetime | None = None,
    horizons: Sequence[int] = (30, 90),
) -> dict[str, Any]:
    """Probe one real target and advance the durable forecast lifecycle."""
    tid = str(target_id or "").strip()
    if not tid:
        raise ValueError("targetId is required")
    try:
        probe = backup_targets.probe_target_capacity(tid)
        identity = backup_targets.read_target_capacity_identity(tid)
        total = _capacity_int(probe, "totalBytes")
        used = _capacity_int(probe, "usedBytes")
        free = _capacity_int(probe, "freeBytes")
        probe_source = str(probe.get("source") or "").strip()
        incarnation = str(identity.get("targetIncarnation") or "").strip()
        if not probe_source or probe_source == "unknown":
            raise ValueError("target capacity probe source is unavailable")
        if not incarnation:
            raise ValueError("target incarnation is unavailable")
        if total <= 0 or used > total or free > total or used + free > total:
            raise ValueError("target capacity probe returned inconsistent byte totals")
    except Exception as exc:
        return {
            "status": "UNAVAILABLE",
            "targetId": tid,
            "reason": str(exc),
            "probe": locals().get("probe", {}),
            "observation": None,
            "forecastPipeline": {"backtests": [], "forecasts": []},
        }

    observed_at = now or _parse_iso(probe.get("observedAt")) or datetime.now(tz=timezone.utc)
    provider = str(identity.get("provider") or identity.get("kind") or "target").strip().lower()
    source = f"{provider}-probe"
    revision_binding = {
        "targetId": tid,
        "targetIncarnation": incarnation,
        "kind": str(identity.get("kind") or ""),
        "provider": provider,
        "quotaBytes": identity.get("quotaBytes"),
        "totalBytes": total,
        "probeSource": probe_source,
    }
    capacity_revision = _digest(revision_binding)
    provenance = {
        "source": source,
        "probeSource": probe_source,
        "targetIncarnation": incarnation,
        "capacityRevision": capacity_revision,
        "observedAt": _utc_iso(observed_at),
        "identityDigest": str(identity.get("identityDigest") or ""),
        "probeDigest": _digest(probe),
        "flowCounterSource": "not-exposed-by-target-capacity-probe",
    }
    observation = resilience_capacity_history.record_capacity_observation(
        tid,
        used_bytes=used,
        free_bytes=free,
        total_bytes=total,
        observed_at=observed_at,
        active_policies=_active_policy_count(tid),
        source=source,
        probe_source=probe_source,
        target_incarnation=incarnation,
        capacity_revision=capacity_revision,
        provenance=provenance,
    )
    forecast_pipeline = resilience_forecast_registry.process_capacity_observation(observation, horizons=horizons)
    return {
        "status": "RECORDED",
        "targetId": tid,
        "probe": probe,
        "observation": observation,
        "forecastPipeline": forecast_pipeline,
    }


def sample_fleet_capacity(
    target_ids: Sequence[str],
    *,
    now: datetime | None = None,
    horizons: Sequence[int] = (30, 90),
) -> dict[str, Any]:
    samples = [sample_target_capacity(target_id, now=now, horizons=horizons) for target_id in target_ids]
    return {
        "samples": samples,
        "recorded": sum(1 for item in samples if item["status"] == "RECORDED"),
        "unavailable": sum(1 for item in samples if item["status"] != "RECORDED"),
        "observedAt": _utc_iso(now),
    }

"""Frozen backup run plans for schedule slots (4.4.7).

The first attempt of a schedule slot freezes contributor scope, recipients,
target head and snapshot kind. Later scheduler retries reuse the same plan and
the verified spool ciphertext instead of re-snapshotting and re-encrypting.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode

RUN_PLAN_DIR = config.ROOT / ".backup-run-plans"
RUN_PLAN_SCHEMA_VERSION = 3


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stable_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def plan_path(policy_id: str, slot_digest: str) -> Path:
    return RUN_PLAN_DIR / policy_id / f"{slot_digest}.json"


def compute_run_plan_digest(body: dict[str, Any]) -> str:
    material = {
        key: value
        for key, value in body.items()
        if key not in {"runPlanDigest", "createdAt", "backupId", "placementJournal", "placementGeneration"}
    }
    return hashlib.sha256(_stable_json(material)).hexdigest()


def policy_digest(policy: dict[str, Any]) -> str:
    material = {
        "policyId": policy.get("policyId"),
        "scope": policy.get("scope"),
        "protection": policy.get("protection"),
        "targetId": policy.get("targetId"),
        "primaryTargetId": policy.get("primaryTargetId"),
        "frontendMirror": policy.get("frontendMirror"),
        "retentionPolicyId": policy.get("retentionPolicyId"),
        "incremental": policy.get("incremental"),
        "replication": policy.get("replication"),
        "recoveryObjectives": policy.get("recoveryObjectives"),
    }
    return hashlib.sha256(_stable_json(material)).hexdigest()


def recipient_set_digest(policy: dict[str, Any]) -> str:
    recipients = list((policy.get("protection") or {}).get("recipients") or [])
    normalized = sorted(str(item) for item in recipients)
    return hashlib.sha256(_stable_json(normalized)).hexdigest()


def freeze_run_plan(
    *,
    policy: dict[str, Any],
    schedule_slot: str,
    slot_digest: str,
    contributor_plan: list[dict[str, Any]] | dict[str, Any],
    target_id: str,
    target_head_hash: str = "",
    snapshot_kind: str = "full",
    lineage_id: str | None = None,
    parent_backup_id: str | None = None,
    base_backup_id: str | None = None,
    chain_depth: int = 0,
    parent_commit_hash: str | None = None,
    parent_receipt_digest: str | None = None,
    force_full_reason: str | None = None,
    frontend_generation_id: str | None = None,
    backup_id: str | None = None,
    configured_primary_target_id: str | None = None,
    selected_write_target_id: str | None = None,
    candidate_target_ids: list[str] | None = None,
    failover_reason: str | None = None,
    is_failover: bool = False,
) -> dict[str, Any]:
    """Return existing plan for the slot or create and persist a new frozen plan.

    Once frozen, retries never re-select the parent, snapshot kind, or backupId.
    """
    existing = read_run_plan(str(policy.get("policyId") or ""), slot_digest)
    if existing is not None:
        return existing
    import uuid

    primary_id = configured_primary_target_id or target_id
    write_id = selected_write_target_id or target_id
    is_failover_bool = bool(is_failover or write_id != primary_id)

    body: dict[str, Any] = {
        "schemaVersion": RUN_PLAN_SCHEMA_VERSION,
        "policyId": str(policy.get("policyId") or ""),
        "scheduleSlot": schedule_slot,
        "slotDigest": slot_digest,
        "policyDigest": policy_digest(policy),
        "targetId": write_id,
        "configuredPrimaryTargetId": primary_id,
        "selectedWriteTargetId": write_id,
        "candidateTargetIds": candidate_target_ids or [primary_id, write_id] if is_failover_bool else [primary_id],
        "failoverReason": failover_reason if is_failover_bool else None,
        "isFailover": is_failover_bool,
        "placementGeneration": 1,
        "placementJournal": [],
        "targetHeadHash": target_head_hash or ("0" * 64),
        "contributorPlanDigest": hashlib.sha256(_stable_json(contributor_plan)).hexdigest(),
        "recipientSetDigest": recipient_set_digest(policy),
        "frontendGenerationId": frontend_generation_id,
        "snapshotKind": snapshot_kind if snapshot_kind in {"full", "incremental"} else "full",
        "plannedSnapshotKind": (
            "adaptive"
            if str((policy.get("incremental") or {}).get("mode") or "off") != "off" and snapshot_kind == "incremental"
            else (snapshot_kind if snapshot_kind in {"full", "incremental"} else "full")
        ),
        "resolvedSnapshotKind": snapshot_kind if snapshot_kind in {"full", "incremental"} else "full",
        "resolutionReason": force_full_reason,
        "lineageId": lineage_id,
        "parentBackupId": parent_backup_id,
        "baseBackupId": base_backup_id,
        "chainDepth": int(chain_depth),
        "parentCommitHash": parent_commit_hash,
        "parentReceiptDigest": parent_receipt_digest,
        "forceFullReason": force_full_reason,
        "backupId": backup_id or f"backup_{uuid.uuid4().hex[:16]}",
        "createdAt": _utc_iso(),
    }
    body["runPlanDigest"] = compute_run_plan_digest(body)
    path = plan_path(body["policyId"], slot_digest)
    _atomic_write_json(path, body)
    return body


def transition_run_plan_target(
    policy_id: str,
    slot_digest: str,
    *,
    new_target_id: str,
    reason: str,
) -> dict[str, Any]:
    """Transition write target of an existing run plan while preserving backupId, objectSet and encryption."""
    plan = read_run_plan(policy_id, slot_digest)
    if plan is None:
        raise AppError("backup run plan is unavailable", code=ErrorCode.NOT_FOUND, status=404)

    journal = list(plan.get("placementJournal") or [])
    current_target = plan.get("selectedWriteTargetId") or plan.get("targetId")
    journal.append({
        "fromTargetId": current_target,
        "toTargetId": new_target_id,
        "reason": reason,
        "transitionedAt": _utc_iso(),
    })

    plan["selectedWriteTargetId"] = new_target_id
    plan["targetId"] = new_target_id
    plan["isFailover"] = (new_target_id != plan.get("configuredPrimaryTargetId"))
    plan["failoverReason"] = reason if plan["isFailover"] else None
    plan["placementGeneration"] = int(plan.get("placementGeneration") or 1) + 1
    plan["placementJournal"] = journal
    plan["runPlanDigest"] = compute_run_plan_digest(plan)
    _atomic_write_json(plan_path(policy_id, slot_digest), plan)
    return plan


def resolve_adaptive_plan(
    policy_id: str,
    slot_digest: str,
    *,
    resolved_snapshot_kind: str,
    reason: str,
) -> dict[str, Any]:
    """Durably resolve an adaptive plan once; retries observe the same result."""
    if resolved_snapshot_kind not in {"full", "incremental"}:
        raise AppError("invalid adaptive snapshot resolution", code=ErrorCode.INVALID_PAYLOAD)
    plan = read_run_plan(policy_id, slot_digest)
    if plan is None:
        raise AppError("backup run plan is unavailable", code=ErrorCode.NOT_FOUND, status=404)
    if plan.get("plannedSnapshotKind") != "adaptive":
        if plan.get("resolvedSnapshotKind") != resolved_snapshot_kind:
            raise AppError("backup run plan is not adaptive", code=ErrorCode.INVALID_REQUEST, status=409)
        return plan
    if plan.get("resolutionReason") and plan.get("resolvedSnapshotKind") != resolved_snapshot_kind:
        raise AppError("adaptive backup resolution already frozen", code=ErrorCode.INVALID_REQUEST, status=409)
    plan["resolvedSnapshotKind"] = resolved_snapshot_kind
    plan["snapshotKind"] = resolved_snapshot_kind
    plan["resolutionReason"] = reason
    if resolved_snapshot_kind == "full":
        plan["forceFullReason"] = reason
        plan["lineageId"] = None
        plan["parentBackupId"] = None
        plan["baseBackupId"] = None
        plan["chainDepth"] = 0
    plan["runPlanDigest"] = compute_run_plan_digest(plan)
    _atomic_write_json(plan_path(policy_id, slot_digest), plan)
    return plan


def read_run_plan(policy_id: str, slot_digest: str) -> dict[str, Any] | None:
    path = plan_path(policy_id, slot_digest)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):  # pragma: no cover
        return None
    if not isinstance(data, dict) or not data.get("runPlanDigest"):  # pragma: no cover
        return None
    expected = compute_run_plan_digest(data)
    if str(data.get("runPlanDigest")) != expected:
        raise AppError("backup run plan digest mismatch", code=ErrorCode.INVALID_REQUEST, status=409)
    return data


def clear_run_plan(policy_id: str, slot_digest: str) -> None:
    path = plan_path(policy_id, slot_digest)
    path.unlink(missing_ok=True)
    parent = path.parent
    if parent.is_dir() and not any(parent.iterdir()):  # pragma: no cover
        parent.rmdir()

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
RUN_PLAN_SCHEMA_VERSION = 1


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
    material = {key: value for key, value in body.items() if key not in {"runPlanDigest", "createdAt", "backupId"}}
    return hashlib.sha256(_stable_json(material)).hexdigest()


def policy_digest(policy: dict[str, Any]) -> str:
    material = {
        "policyId": policy.get("policyId"),
        "scope": policy.get("scope"),
        "protection": policy.get("protection"),
        "targetId": policy.get("targetId"),
        "frontendMirror": policy.get("frontendMirror"),
        "retentionPolicyId": policy.get("retentionPolicyId"),
        "incremental": policy.get("incremental"),
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
    parent_backup_id: str | None = None,
    base_backup_id: str | None = None,
    frontend_generation_id: str | None = None,
    backup_id: str | None = None,
) -> dict[str, Any]:
    """Return existing plan for the slot or create and persist a new frozen plan."""
    existing = read_run_plan(str(policy.get("policyId") or ""), slot_digest)
    if existing is not None:
        return existing
    import uuid

    body: dict[str, Any] = {
        "schemaVersion": RUN_PLAN_SCHEMA_VERSION,
        "policyId": str(policy.get("policyId") or ""),
        "scheduleSlot": schedule_slot,
        "slotDigest": slot_digest,
        "policyDigest": policy_digest(policy),
        "targetId": target_id,
        "targetHeadHash": target_head_hash or ("0" * 64),
        "contributorPlanDigest": hashlib.sha256(_stable_json(contributor_plan)).hexdigest(),
        "recipientSetDigest": recipient_set_digest(policy),
        "frontendGenerationId": frontend_generation_id,
        "snapshotKind": snapshot_kind if snapshot_kind in {"full", "incremental"} else "full",
        "parentBackupId": parent_backup_id,
        "baseBackupId": base_backup_id,
        "backupId": backup_id or f"backup_{uuid.uuid4().hex[:16]}",
        "createdAt": _utc_iso(),
    }
    body["runPlanDigest"] = compute_run_plan_digest(body)
    path = plan_path(body["policyId"], slot_digest)
    _atomic_write_json(path, body)
    return body


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

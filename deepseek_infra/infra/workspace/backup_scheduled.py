"""Scheduled backup package construction (4.4.5).

Builds a full workspace backup for a durable policy without any browser or
user secret in the loop: the contributor plan is frozen, the workspace is
quiesced through the mutation gate, the sealed frontend mirror is included as
an inner age file, and the outer ciphertext is produced and verified with an
ephemeral recipient that is destroyed immediately after the round trip.
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_mirror, backup_unattended, backups, mutation_gate


@dataclass(frozen=True, slots=True)
class ScheduledBackupPackage:
    backup_id: str
    filename: str
    path: Path
    size: int
    ciphertext_sha256: str
    manifest_digest: str
    coverage_digest: str
    creation_verified: bool
    frontend: dict[str, Any]
    coverage: dict[str, Any]
    manifest: dict[str, Any]


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _age_seconds(iso_timestamp: Any, *, now: datetime | None = None) -> int:
    try:
        acknowledged = datetime.fromisoformat(str(iso_timestamp or "").replace("Z", "+00:00"))
    except ValueError:
        return -1
    current = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    return max(0, int((current - acknowledged).total_seconds()))


def _section(policy: dict[str, Any], name: str) -> dict[str, Any]:
    value = policy.get(name)
    return value if isinstance(value, dict) else {}


def mirror_coverage(policy: dict[str, Any], *, now: datetime | None = None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Resolve the sealed mirror for a policy run.

    Returns ``(mirror_metadata, coverage_frontend)``. A ``required`` mirror that
    is not current blocks the run; ``best-effort`` records the gap instead.
    """
    mirror_cfg = _section(policy, "frontendMirror")
    mode = str(mirror_cfg.get("mode") or "best-effort")
    if mode == "excluded":
        return None, {"mode": "excluded", "status": "excluded"}
    profile_id = mirror_cfg.get("profileId")
    recipients = _section(policy, "protection").get("recipients") or []
    max_age = int(mirror_cfg.get("maxAgeSeconds") or 3600)
    status = backup_mirror.mirror_status(str(profile_id) if profile_id else None, recipients=recipients, max_age_seconds=max_age, now=now)
    freshness = str(status.get("status") or "missing")
    if freshness == "current":
        metadata = status["mirror"]
        coverage = {
            "mode": "sealed-mirror",
            "profileId": metadata.get("profileId"),
            "sourceEpoch": metadata.get("sourceEpoch"),
            "envelopeDigest": metadata.get("envelopeDigest"),
            "mirrorCreatedAt": metadata.get("createdAt"),
            "ageSeconds": _age_seconds(metadata.get("acknowledgedAt"), now=now),
            "recipientSetDigest": metadata.get("recipientSetDigest"),
            "status": "current",
        }
        return metadata, coverage
    if mode == "required":
        raise AppError(
            f"blocked-frontend-mirror: {freshness}",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    coverage = {"mode": "sealed-mirror", "status": freshness}
    if profile_id:
        coverage["profileId"] = str(profile_id)
    return None, coverage


def _context_from_policy(policy: dict[str, Any]) -> backups.BackupContext:
    scope = _section(policy, "scope")
    return backups.BackupContext(
        mode=str(scope.get("mode") or "full"),
        project_ids=tuple(str(item) for item in scope.get("projectIds") or []),
        include_history=bool(scope.get("includeHistory", True)),
        include_drafts=False,
        include_rebuildable_indexes=False,
        coverage_policy="best-effort" if scope.get("coveragePolicy") == "best-effort" else "strict",
        include_external_state=bool(scope.get("includeExternalState", True)),
    )


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise AppError("Scheduled backup cancelled", code=ErrorCode.INVALID_REQUEST, status=499)


def _build_candidate(
    run_dir: Path,
    backup_id: str,
    policy: dict[str, Any],
    context: backups.BackupContext,
    plan: dict[str, Any],
    mirror_metadata: dict[str, Any] | None,
    coverage_frontend: dict[str, Any],
    *,
    schedule_slot: str,
    cancel_event: threading.Event | None,
) -> ScheduledBackupPackage:
    staging = run_dir / "staging"
    verification_dir = run_dir / "verification"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        _raise_if_cancelled(cancel_event)
        contributions = []
        for contributor in backups._contributors_from_plan(plan):
            _raise_if_cancelled(cancel_event)
            contributions.append(contributor.snapshot(staging, context))
        files = [entry for contribution in contributions for entry in contribution.files]
        frontend_manifest: dict[str, Any] | None = None
        if mirror_metadata is not None:
            policy_recipients = tuple(str(item) for item in (policy.get("protection") or {}).get("recipients") or [])
            ciphertext, metadata_path, _ = backup_mirror.mirror_files(str(mirror_metadata["profileId"]), recipients=policy_recipients)
            frontend_dir = staging / "frontend"
            frontend_dir.mkdir(parents=True, exist_ok=True)
            sealed_target = frontend_dir / "sealed-state.age"
            sealed_meta_target = frontend_dir / "sealed-state.meta.json"
            shutil.copyfile(ciphertext, sealed_target)
            shutil.copyfile(metadata_path, sealed_meta_target)
            files.append({"path": "frontend/sealed-state.age", "size": sealed_target.stat().st_size, "sha256": backups._sha256_file(sealed_target)})
            files.append({"path": "frontend/sealed-state.meta.json", "size": sealed_meta_target.stat().st_size, "sha256": backups._sha256_file(sealed_meta_target)})
            frontend_manifest = {
                "schemaVersion": 1,
                "mode": "sealed-mirror",
                "profileId": mirror_metadata.get("profileId"),
                "sourceEpoch": mirror_metadata.get("sourceEpoch"),
                "envelopeDigest": mirror_metadata.get("envelopeDigest"),
                "conversations": mirror_metadata.get("conversations", 0),
                "conflicts": mirror_metadata.get("conflicts", 0),
                "recipientSetDigest": mirror_metadata.get("recipientSetDigest"),
                "mirrorCreatedAt": mirror_metadata.get("createdAt"),
            }
        source_schemas = {item.contributor_id: item.schema_version for item in contributions}
        migration_path = staging / "migration" / "source-schemas.json"
        migration_path.parent.mkdir(parents=True, exist_ok=True)
        migration_path.write_bytes(backups._stable_json(source_schemas))
        raw_migration = migration_path.read_bytes()
        files.append({"path": "migration/source-schemas.json", "size": len(raw_migration), "sha256": hashlib.sha256(raw_migration).hexdigest()})
        files.sort(key=lambda entry: str(entry["path"]).casefold())
        coverage = backups._attest_coverage(plan, contributions)
        coverage["frontend"] = coverage_frontend
        if coverage_frontend.get("status") not in {"current", "excluded"}:
            coverage["complete"] = False
        manifest: dict[str, Any] = {
            "schemaVersion": backups.BACKUP_SCHEMA,
            "purpose": backups.PACKAGE_PURPOSE,
            "backupId": backup_id,
            "source": {
                "version": config.APP_VERSION,
                "revision": backups._build_revision(),
                "platform": platform.platform(),
                "createdAt": _utc_iso(),
            },
            "scope": {
                "mode": context.mode,
                "projectIds": list(context.project_ids),
                "includeHistory": context.include_history,
                "includeDrafts": False,
            },
            "scheduled": {
                "policyId": str(policy.get("policyId") or ""),
                "scheduleSlot": schedule_slot,
            },
            "contributors": [
                {
                    "id": item.contributor_id,
                    "schemaVersion": item.schema_version,
                    "records": item.records,
                    "bytes": item.bytes,
                    "digest": item.digest,
                    "restorePolicy": item.restore_policy,
                }
                for item in contributions
            ],
            "coverage": coverage,
            "exclusions": backups._exclusions(context),
            "files": files,
            "encrypted": True,
        }
        if frontend_manifest:
            manifest["frontend"] = frontend_manifest
        manifest_bytes = backups._stable_json(manifest)
        (staging / "manifest.json").write_bytes(manifest_bytes)
        checksums = [f"{item['sha256']}  {item['path']}" for item in files]
        checksums.append(f"{hashlib.sha256(manifest_bytes).hexdigest()}  manifest.json")
        (staging / "checksums.sha256").write_text("\n".join(checksums) + "\n", encoding="utf-8", newline="\n")
        filename = f"deepseek-infra-backup-{time.strftime('%Y%m%d')}-{backup_id[-8:]}.dsibackup.age"
        target = run_dir / filename
        expect_sealed = mirror_metadata is not None

        def _verify(decrypted: Path) -> None:
            verified = backups._safe_extract_and_verify(decrypted, verification_dir)
            if expect_sealed and not (verification_dir / "frontend" / "sealed-state.age").is_file():
                raise AppError("Scheduled backup verification lost the sealed frontend mirror", code=ErrorCode.INTERNAL, status=500)
            if (verified.get("coverage") or {}).get("frontend") != coverage_frontend:
                raise AppError("Scheduled backup coverage verification failed", code=ErrorCode.INTERNAL, status=500)

        _raise_if_cancelled(cancel_event)
        encryption = backup_unattended.encrypt_unattended(
            target,
            lambda output: backups._write_zip_tree(staging, output),
            recipients=tuple(str(item) for item in (policy.get("protection") or {}).get("recipients") or []),
            verify=_verify,
            cancel_event=cancel_event,
        )
        return ScheduledBackupPackage(
            backup_id=backup_id,
            filename=filename,
            path=target,
            size=encryption.size,
            ciphertext_sha256=encryption.ciphertext_sha256,
            manifest_digest=hashlib.sha256(manifest_bytes).hexdigest(),
            coverage_digest=hashlib.sha256(backups._stable_json(coverage)).hexdigest(),
            creation_verified=encryption.creation_verified,
            frontend=coverage_frontend,
            coverage=coverage,
            manifest=manifest,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(verification_dir, ignore_errors=True)


def build_scheduled_backup(
    policy: dict[str, Any],
    *,
    run_id: str,
    staging_root: Path,
    schedule_slot: str = "",
    cancel_event: threading.Event | None = None,
    backup_id: str | None = None,
    contributor_plan: Any | None = None,
    snapshot_kind: str = "full",
    parent_backup_id: str | None = None,
    base_backup_id: str | None = None,
) -> ScheduledBackupPackage:
    """Build and verify a scheduled backup package under ``staging_root``.

    The workspace is quiesced through the mutation gate; if it keeps changing
    across three attempts the run fails with 409 so the scheduler can retry.
    When ``backup_id`` / ``contributor_plan`` are supplied (frozen run plan),
    retries keep the same identity instead of minting a new package id.
    """
    context = _context_from_policy(policy)
    plan = contributor_plan if contributor_plan is not None else backups._contributor_plan(context)
    mirror_metadata, coverage_frontend = mirror_coverage(policy)
    resolved_backup_id = backup_id or f"backup_{uuid.uuid4().hex[:16]}"
    _ = (snapshot_kind, parent_backup_id, base_backup_id)  # reserved for incremental builder path
    run_dir = staging_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    gate_root = backups.BACKUP_DIR.parent
    recipients = (policy.get("protection") or {}).get("recipients") or []
    if not recipients:
        raise AppError("Scheduled backup policy has no recipients", code=ErrorCode.INVALID_PAYLOAD)
    last_error: AppError | None = None
    for _attempt in range(1, 4):
        _raise_if_cancelled(cancel_event)
        with mutation_gate.exclusive_gate(gate_root):
            mutation_gate.assert_mutation_allowed(root=gate_root)
            start_generation = mutation_gate.read_generation(gate_root)
            for contributor in backups._contributors_from_plan(plan):
                contributor.flush(context)
        try:
            candidate = _build_candidate(
                run_dir,
                resolved_backup_id,
                policy,
                context,
                plan,
                mirror_metadata,
                coverage_frontend,
                schedule_slot=schedule_slot,
                cancel_event=cancel_event,
            )
        except AppError as exc:
            last_error = exc
            break
        with mutation_gate.exclusive_gate(gate_root):
            end_generation = mutation_gate.read_generation(gate_root)
        if start_generation == end_generation:
            return candidate
        candidate.path.unlink(missing_ok=True)
    if last_error is not None:
        raise last_error
    raise AppError(
        "Workspace changed repeatedly during scheduled backup; the scheduler will retry",
        code=ErrorCode.INVALID_REQUEST,
        status=409,
    )

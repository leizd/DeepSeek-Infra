"""Scheduled backup package construction (4.4.12).

Builds a full workspace backup for a durable policy without any browser or
user secret in the loop: the contributor plan is frozen, the workspace is
quiesced through the mutation gate, the sealed frontend mirror is included as
an inner age file, and the outer ciphertext is produced and verified with an
ephemeral recipient that is destroyed immediately after the round trip.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, cast

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_chunk_engine,
    backup_incremental,
    backup_mirror,
    backup_pack,
    backup_unattended,
    backups,
    mutation_gate,
)


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
    chunk_records: tuple[Any, ...] = ()
    effective_files: tuple[Any, ...] = ()
    savings: dict[str, Any] | None = None


class DeltaCostExceeded(Exception):
    """The exact incremental ZIP exceeded its frozen adaptive-full budget."""

    def __init__(self, *, byte_limit: int, attempted_size: int) -> None:
        self.byte_limit = byte_limit
        self.attempted_size = attempted_size
        super().__init__(f"delta archive exceeded {byte_limit} byte adaptive limit")


class ThresholdWriter:
    """Seekable binary writer that bounds the maximum file extent."""

    def __init__(self, output: BinaryIO, *, byte_limit: int) -> None:
        if byte_limit < 0:
            raise ValueError("byte_limit must be non-negative")
        self._output = output
        self.byte_limit = byte_limit

    def write(self, data: bytes) -> int:
        attempted_size = self._output.tell() + len(data)
        if attempted_size > self.byte_limit:
            raise DeltaCostExceeded(byte_limit=self.byte_limit, attempted_size=attempted_size)
        return self._output.write(data)

    def tell(self) -> int:
        return self._output.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._output.seek(offset, whence)

    def flush(self) -> None:
        self._output.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._output, name)


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
    checkpoint = getattr(cancel_event, "backup_checkpoint", None)
    if callable(checkpoint):
        checkpoint()
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
    snapshot_kind: str = "full",
    parent_backup_id: str | None = None,
    base_backup_id: str | None = None,
    lineage_id: str | None = None,
    chain_depth: int = 0,
    adaptive_max_delta_ratio: float | None = None,
) -> ScheduledBackupPackage:
    staging = run_dir / "staging"
    verification_dir = run_dir / "verification"
    plaintext_archive = run_dir / f".{backup_id}.candidate.delta.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    plaintext_archive.unlink(missing_ok=True)
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
            files.append({"contributorId": "frontend", "path": "frontend/sealed-state.age", "size": sealed_target.stat().st_size, "sha256": backups._sha256_file(sealed_target)})
            files.append({"contributorId": "frontend", "path": "frontend/sealed-state.meta.json", "size": sealed_meta_target.stat().st_size, "sha256": backups._sha256_file(sealed_meta_target)})
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
        files.append({"contributorId": "migration", "path": "migration/source-schemas.json", "size": len(raw_migration), "sha256": hashlib.sha256(raw_migration).hexdigest()})
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
        # Incremental: build a delta payload containing only changed files, plus
        # an operations manifest, and attest the effective tree Merkle root.
        snapshot_meta: dict[str, Any] = {"kind": "full"}
        incremental_cfg = _section(policy, "incremental")
        large_file_mode = str(incremental_cfg.get("largeFileMode") or "cdc")
        large_file_threshold = int(incremental_cfg.get("largeFileThresholdBytes") or (16 * 1024 * 1024))
        scan_workers = int(incremental_cfg.get("scanWorkers") or 1)
        max_in_flight_bytes = int(incremental_cfg.get("maxInFlightBytes") or (64 * 1024 * 1024))
        current_chunk_records: list[backup_incremental.ChunkRecord] = []
        effective_index_records = [
            backup_incremental.FileRecord(
                contributor_id=str(item.get("contributorId") or ""),
                logical_path=str(item["path"]),
                size=int(item["size"]),
                sha256=str(item["sha256"]),
            )
            for item in files
        ]
        if snapshot_kind == "incremental" and parent_backup_id:
            target_id = str(policy.get("targetId") or "managed-local")
            policy_id = str(policy.get("policyId") or "")
            parent_files = []
            parent_index_available = True
            try:
                parent_files = backup_incremental.load_snapshot_files(target_id, policy_id, parent_backup_id)
            except Exception:
                parent_files = []
                parent_index_available = False
            successful = {
                str(item.get("id") or item.get("contributorId") or item.get("contributor_id") or "")
                for item in plan.get("contributors") or []
            }
            records = [
                backup_incremental.FileRecord(
                    contributor_id=str(item.get("contributorId") or ""),
                    logical_path=str(item["path"]),
                    size=int(item["size"]),
                    sha256=str(item["sha256"]),
                )
                for item in files
            ]
            delta = backup_incremental.diff_trees(parent_files, records, successful_contributors=successful or {c.contributor_id for c in parent_files})
            effective_index_records = backup_incremental.effective_current(
                parent_files,
                records,
                successful_contributors=successful or {item.contributor_id for item in records},
            )
            (staging / "delta").mkdir(exist_ok=True)
            payload_dir = staging / "payload" / "files"
            payload_files: list[dict[str, Any]] = []
            payload_by_digest: dict[tuple[str, int], dict[str, str]] = {}
            pack_writer: backup_pack.PackWriter | None = None
            logical_payload_blobs = 0
            raw_payload_bytes = 0
            standalone_blobs = 0
            same_file_chunks_reused = 0
            cross_file_chunks_reused = 0
            whole_files_parent_reused = 0
            parent_reuse_bytes = 0
            lookup_metrics = {"bloomNegatives": 0, "bloomPositives": 0, "exactHits": 0, "falsePositives": 0}
            for put in delta["put"]:
                parent_file = None
                if parent_index_available:
                    try:
                        parent_file = backup_incremental.lookup_parent_file_by_digest(
                            target_id,
                            policy_id,
                            parent_backup_id,
                            sha256=str(put.get("sha256") or ""),
                            size=int(put.get("size") or 0),
                            exclude_path=str(put.get("path") or ""),
                        )
                    except Exception:
                        parent_index_available = False
                if parent_file is not None:
                    put["storage"] = "parent-file"
                    put["parentContributorId"] = parent_file.contributor_id
                    put["parentPath"] = parent_file.logical_path
                    put.pop("payloadRef", None)
                    whole_files_parent_reused += 1
                    parent_reuse_bytes += int(put.get("size") or 0)
            large_paths = [
                staging / str(put["path"])
                for put in delta["put"]
                if large_file_mode == "cdc"
                and str(put.get("storage") or "whole") != "parent-file"
                and int(put.get("size") or 0) >= large_file_threshold
            ]
            scans, scan_telemetry = backup_chunk_engine.scan_files_bounded(
                large_paths,
                workers=scan_workers,
                max_in_flight_bytes=max_in_flight_bytes,
                cancel_event=cancel_event,
                checkpoint=getattr(cancel_event, "backup_checkpoint", None),
            ) if large_paths else ({}, {"engine": {"preferred": "none", "rustFiles": 0, "pythonFallbackFiles": 0, "fallbackReasons": {}, "degraded": False}, "files": 0, "logicalBytes": 0, "scanSeconds": 0.0, "throughputBytesPerSecond": 0, "workers": scan_workers, "maxInFlightBytes": max_in_flight_bytes})
            for put in delta["put"]:
                if str(put.get("storage") or "") == "parent-file":
                    continue
                src = staging / str(put["path"])
                if not src.is_file():
                    continue
                blob_sha = str(put.get("sha256") or "")
                is_large = int(put.get("size") or 0) >= large_file_threshold
                if large_file_mode == "cdc" and is_large:
                    contributor_id = str(put.get("contributorId") or "")
                    logical_path = str(put["path"])
                    try:
                        parent_chunks = backup_incremental.load_snapshot_chunks_for_file(
                            target_id,
                            policy_id,
                            parent_backup_id,
                            contributor_id,
                            logical_path,
                        )
                    except Exception:
                        parent_chunks = []
                    scan = scans[src]
                    if scan.sha256 != blob_sha or scan.size != int(put.get("size") or 0):
                        raise AppError("Workspace file changed during CDC scan", code=ErrorCode.INVALID_REQUEST, status=409)
                    current_chunks = [
                        backup_incremental.ChunkRecord(
                            contributor_id,
                            logical_path,
                            index,
                            int(item["offset"]),
                            int(item["length"]),
                            str(item["sha256"]),
                        )
                        for index, item in enumerate(scan.chunks)
                    ]
                    if parent_index_available:
                        try:
                            parent_locations, current_lookup = backup_incremental.lookup_parent_chunks_accelerated(
                                target_id,
                                policy_id,
                                parent_backup_id,
                                [(item.chunk_sha256, item.length) for item in current_chunks],
                                preferred_file=(contributor_id, logical_path),
                            )
                        except Exception:
                            parent_index_available = False
                            parent_locations, current_lookup = {}, {
                                "bloomNegatives": 0,
                                "bloomPositives": 0,
                                "exactHits": 0,
                                "falsePositives": 0,
                            }
                    else:
                        parent_locations, current_lookup = {}, {
                            "bloomNegatives": 0,
                            "bloomPositives": 0,
                            "exactHits": 0,
                            "falsePositives": 0,
                        }
                    for parent_chunk in parent_chunks:
                        parent_locations.setdefault(
                            (parent_chunk.chunk_sha256, parent_chunk.length),
                            backup_incremental.ParentChunkLocation(
                                contributor_id,
                                logical_path,
                                parent_chunk.chunk_ordinal,
                                parent_chunk.offset,
                                parent_chunk.length,
                                parent_chunk.chunk_sha256,
                            ),
                        )
                    for key in lookup_metrics:
                        lookup_metrics[key] += int(current_lookup.get(key) or 0)
                    described = backup_incremental.cdc_delta_for_file(
                        contributor_id=contributor_id,
                        logical_path=logical_path,
                        file_size=int(put.get("size") or 0),
                        parent_chunks=parent_chunks,
                        current_chunks=current_chunks,
                        parent_locations=parent_locations,
                    )
                    for chunk in described:
                        if chunk.get("source") != "parent-range":
                            continue
                        if (
                            str(chunk.get("parentContributorId") or "") == contributor_id
                            and str(chunk.get("parentPath") or "") == logical_path
                        ):
                            same_file_chunks_reused += 1
                        else:
                            cross_file_chunks_reused += 1
                        parent_reuse_bytes += int(chunk.get("length") or 0)
                    with src.open("rb") as handle:
                        for index, chunk in enumerate(described):
                            if chunk["source"] != "payload":
                                continue
                            logical_payload_blobs += 1
                            chunk_sha = str(chunk.get("sha256") or "")
                            record = current_chunks[index]
                            payload_key = (chunk_sha, record.length)
                            existing_ref = payload_by_digest.get(payload_key)
                            if existing_ref is not None:
                                chunk["payloadRef"] = existing_ref
                                continue
                            handle.seek(record.offset)
                            if pack_writer is None:
                                pack_writer = backup_pack.PackWriter(staging)
                            ref = pack_writer.append(
                                handle,
                                expected_length=record.length,
                                expected_sha256=chunk_sha,
                            )
                            raw_payload_bytes += record.length
                            payload_by_digest[payload_key] = ref
                            chunk["payloadRef"] = ref
                    put["storage"] = "cdc"
                    put["chunks"] = described
                    put.pop("payloadRef", None)
                    current_chunk_records.extend(current_chunks)
                    continue
                logical_payload_blobs += 1
                payload_size = int(put.get("size") or 0)
                payload_key = (blob_sha, payload_size)
                existing = payload_by_digest.get(payload_key)
                if existing is not None:
                    put["payloadRef"] = existing
                    continue
                if payload_size <= backup_pack.WHOLE_FILE_PACK_THRESHOLD:
                    if pack_writer is None:
                        pack_writer = backup_pack.PackWriter(staging)
                    with src.open("rb") as source:
                        ref = pack_writer.append(source, expected_length=payload_size, expected_sha256=blob_sha)
                else:
                    payload_dir.mkdir(parents=True, exist_ok=True)
                    dest = payload_dir / f"{standalone_blobs:06d}"
                    shutil.copyfile(src, dest)
                    relative = f"payload/files/{dest.name}"
                    payload_files.append({"path": relative, "size": dest.stat().st_size, "sha256": blob_sha})
                    ref = {"kind": "standalone", "path": relative}
                    standalone_blobs += 1
                raw_payload_bytes += payload_size
                payload_by_digest[payload_key] = ref
                put["payloadRef"] = ref
            pack_files = pack_writer.delta_files() if pack_writer is not None else []
            # Serialize the delta manifest only after every payload reference is
            # allocated so operations.json carries the final payloadRef paths.
            operations = backups._stable_json(delta)
            (staging / "delta" / "operations.json").write_bytes(operations)
            # Auxiliary files declared for verification but not restorable.
            manifest["deltaFiles"] = [
                {"path": "delta/operations.json", "size": len(operations), "sha256": hashlib.sha256(operations).hexdigest()},
                *payload_files,
                *pack_files,
            ]
            # True delta storage: drop the full workspace copies so the archive
            # carries only the changed payloads plus the operations manifest.
            if (staging / "payload").is_dir():
                for entry in list((staging / "payload").iterdir()):
                    if entry.name not in {"files", "packs"}:
                        shutil.rmtree(entry, ignore_errors=True)
                for retained in (staging / "payload" / "files", staging / "payload" / "packs"):
                    if retained.is_dir() and not any(retained.iterdir()):
                        retained.rmdir()
            for auxiliary_dir in ("migration", "frontend"):
                shutil.rmtree(staging / auxiliary_dir, ignore_errors=True)
            snapshot_meta = {
                "format": "incremental-v5",
                "chunkProtocol": backup_incremental.CURRENT_CDC_PROTOCOL,
                "kind": "incremental",
                "lineageId": lineage_id or parent_backup_id,
                "parentBackupId": parent_backup_id,
                "baseBackupId": base_backup_id or parent_backup_id,
                "chainDepth": int(chain_depth or 1),
                "merkleAlgorithm": backup_incremental.MERKLE_ALGORITHM,
                "parentRootDigest": delta["parentRootDigest"],
                "rootDigest": delta["rootDigest"],
            }
            manifest["snapshot"] = snapshot_meta
            manifest["snapshotKind"] = "incremental"
            logical_changed_bytes = sum(int(item.get("size") or 0) for item in delta["put"])
            physical_payload_bytes = raw_payload_bytes
            payload_chunks_written = sum(1 for put in delta["put"] for chunk in (put.get("chunks") or []) if chunk.get("source") == "payload")
            savings = {
                "logicalBytes": sum(int(item.size) for item in records),
                "logicalChangedBytes": logical_changed_bytes,
                "physicalPayloadBytes": physical_payload_bytes,
                "reusedBytes": max(0, logical_changed_bytes - physical_payload_bytes),
                "savedBytes": max(0, logical_changed_bytes - physical_payload_bytes),
                "savedRatio": round(1.0 - (physical_payload_bytes / logical_changed_bytes), 4) if logical_changed_bytes else 0.0,
            }
            dedup = {
                "logicalChangedBytes": logical_changed_bytes,
                "sameFileChunksReused": same_file_chunks_reused,
                "crossFileChunksReused": cross_file_chunks_reused,
                "wholeFilesParentReused": whole_files_parent_reused,
                "payloadChunksWritten": payload_chunks_written,
                "parentReuseBytes": parent_reuse_bytes,
                "payloadBytes": physical_payload_bytes,
                "dedupRatio": round(parent_reuse_bytes / logical_changed_bytes, 4) if logical_changed_bytes else 0.0,
            }
            pack_index = pack_writer.finalize() if pack_writer is not None else {"packs": [], "entries": {}}
            pack_count = len(pack_index["packs"])
            packed_blobs = len(pack_index["entries"])
            packing = {
                "logicalPayloadBlobs": logical_payload_blobs,
                "standaloneBlobs": standalone_blobs,
                "packCount": pack_count,
                "packedBlobs": packed_blobs,
                "rawPayloadBytes": raw_payload_bytes,
                "packBytes": sum(int(item.get("size") or 0) for item in pack_index["packs"]),
                "filesystemEntriesAvoided": max(0, packed_blobs - pack_count),
                "zipEntriesAvoided": max(0, packed_blobs - pack_count),
            }
            manifest["incrementalSavings"] = savings
            manifest["dedup"] = dedup
            manifest["packing"] = packing
            manifest["incrementalPerformance"] = {
                **scan_telemetry,
                **savings,
                "dedup": dedup,
                "lookup": lookup_metrics,
                "packing": packing,
            }
        elif snapshot_kind == "full" and large_file_mode == "cdc":
            # Full snapshots also chunk large files so the first incremental can
            # reuse parent chunks instead of re-uploading the whole file.
            full_chunk_records: list[backup_incremental.ChunkRecord] = []
            large_entries = [
                item for item in files
                if int(item.get("size") or 0) >= large_file_threshold and (staging / str(item["path"])).is_file()
            ]
            scans, scan_telemetry = backup_chunk_engine.scan_files_bounded(
                [staging / str(item["path"]) for item in large_entries],
                workers=scan_workers,
                max_in_flight_bytes=max_in_flight_bytes,
                cancel_event=cancel_event,
                checkpoint=getattr(cancel_event, "backup_checkpoint", None),
            ) if large_entries else ({}, {"engine": {"preferred": "none", "rustFiles": 0, "pythonFallbackFiles": 0, "fallbackReasons": {}, "degraded": False}, "files": 0, "logicalBytes": 0, "scanSeconds": 0.0, "throughputBytesPerSecond": 0, "workers": scan_workers, "maxInFlightBytes": max_in_flight_bytes})
            for file_entry in large_entries:
                staging_path = staging / str(file_entry["path"])
                contributor_id = str(file_entry.get("contributorId") or "")
                scan = scans[staging_path]
                if scan.sha256 != str(file_entry.get("sha256") or ""):
                    raise AppError("Workspace file changed during CDC scan", code=ErrorCode.INVALID_REQUEST, status=409)
                full_chunk_records.extend(
                    backup_incremental.ChunkRecord(
                        contributor_id,
                        str(file_entry["path"]),
                        index,
                        int(item["offset"]),
                        int(item["length"]),
                        str(item["sha256"]),
                    )
                    for index, item in enumerate(scan.chunks)
                )
            current_chunk_records = full_chunk_records
            manifest["chunkProtocol"] = backup_incremental.CURRENT_CDC_PROTOCOL
            manifest["incrementalPerformance"] = scan_telemetry
        manifest_bytes = backups._stable_json(manifest)
        (staging / "manifest.json").write_bytes(manifest_bytes)
        # Incremental packages checksum only the physically-present payload.
        delta_files = manifest.get("deltaFiles")
        checksummed = delta_files if snapshot_kind == "incremental" and isinstance(delta_files, list) else files
        checksums = [f"{item['sha256']}  {item['path']}" for item in checksummed]
        checksums.append(f"{hashlib.sha256(manifest_bytes).hexdigest()}  manifest.json")
        (staging / "checksums.sha256").write_text("\n".join(checksums) + "\n", encoding="utf-8", newline="\n")
        filename = f"deepseek-infra-backup-{time.strftime('%Y%m%d')}-{backup_id[-8:]}.dsibackup.age"
        target = run_dir / filename
        expect_sealed = mirror_metadata is not None

        def _verify(decrypted: Path) -> None:
            verified = backups._safe_extract_and_verify(decrypted, verification_dir)
            # Incremental packages inherit the sealed mirror from the parent when
            # it is unchanged, so presence is only asserted for full packages.
            if expect_sealed and snapshot_kind != "incremental" and not (verification_dir / "frontend" / "sealed-state.age").is_file():
                raise AppError("Scheduled backup verification lost the sealed frontend mirror", code=ErrorCode.INTERNAL, status=500)
            if (verified.get("coverage") or {}).get("frontend") != coverage_frontend:
                raise AppError("Scheduled backup coverage verification failed", code=ErrorCode.INTERNAL, status=500)

        _raise_if_cancelled(cancel_event)
        plaintext_archive_bytes = 0

        def _stream_plaintext(output: Any) -> None:
            if plaintext_archive.is_file():
                with plaintext_archive.open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            else:
                backups._write_zip_tree(staging, output)

        if snapshot_kind == "incremental":
            # Keep exact container-cost accounting without retaining the delta
            # archive in RAM. The verified temporary archive is streamed into
            # Age and removed whether encryption succeeds or fails.
            with plaintext_archive.open("w+b") as archive_output:
                logical_bytes = int((manifest.get("incrementalSavings") or {}).get("logicalBytes") or 0)
                byte_limit = (
                    int(logical_bytes * adaptive_max_delta_ratio)
                    if adaptive_max_delta_ratio is not None and logical_bytes > 0
                    else None
                )
                bounded_output = (
                    ThresholdWriter(archive_output, byte_limit=byte_limit)
                    if byte_limit is not None
                    else archive_output
                )
                backups._write_zip_tree(staging, cast(BinaryIO, bounded_output))
                bounded_output.flush()
                os.fsync(archive_output.fileno())
            plaintext_archive_bytes = plaintext_archive.stat().st_size
        encryption = backup_unattended.encrypt_unattended(
            target,
            _stream_plaintext,
            recipients=tuple(str(item) for item in (policy.get("protection") or {}).get("recipients") or []),
            verify=_verify,
            cancel_event=cancel_event,
        )
        final_savings: dict[str, Any] | None = None
        if snapshot_kind == "incremental":
            final_savings = dict(manifest.get("incrementalSavings") or {})
            final_savings["physicalDeltaBytes"] = sum(int(item.get("size") or 0) for item in (manifest.get("deltaFiles") or []))
            final_savings["unencryptedArchiveBytes"] = plaintext_archive_bytes
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
            chunk_records=tuple(current_chunk_records),
            effective_files=tuple(effective_index_records),
            savings=final_savings,
        )
    finally:
        plaintext_archive.unlink(missing_ok=True)
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
    lineage_id: str | None = None,
    chain_depth: int = 0,
    adaptive_max_delta_ratio: float | None = None,
) -> ScheduledBackupPackage:
    """Build and verify a scheduled backup package under ``staging_root``.

    The workspace is quiesced through the mutation gate; if it keeps changing
    across three attempts the run fails with 409 so the scheduler can retry.
    When ``backup_id`` / ``contributor_plan`` are supplied (frozen run plan),
    retries keep the same identity instead of minting a new package id.

    With ``snapshot_kind="incremental"`` the candidate builds a delta payload
    (changed files + operations) attested by a Merkle root over the effective
    tree; unchanged files are inherited from ``parent_backup_id``.
    """
    context = _context_from_policy(policy)
    plan = contributor_plan if contributor_plan is not None else backups._contributor_plan(context)
    mirror_metadata, coverage_frontend = mirror_coverage(policy)
    resolved_backup_id = backup_id or f"backup_{uuid.uuid4().hex[:16]}"
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
                snapshot_kind=snapshot_kind,
                parent_backup_id=parent_backup_id,
                base_backup_id=base_backup_id,
                lineage_id=lineage_id,
                chain_depth=chain_depth,
                adaptive_max_delta_ratio=adaptive_max_delta_ratio,
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

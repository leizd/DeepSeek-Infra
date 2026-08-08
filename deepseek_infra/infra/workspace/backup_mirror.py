"""Sealed frontend replica mirror as immutable generations (4.4.5).

The mirror lets an unattended scheduler include browser session state without
the server ever holding it in plaintext at rest. Every upload produces an
immutable generation under ``generations/<generationId>/`` — one ciphertext
per recipient variant plus a ``metadata.json`` descriptor — verified with a
full decrypt round trip and fsynced before the ``HEAD.json`` pointer is CAS
updated to it. Readers resolve HEAD once and copy from that single immutable
generation, so a backup can never mix ciphertext and metadata from two
different generations. Metadata never contains conversation titles, message
bodies, drafts, writer ids, leases or recovery identities. Legacy 4.4.4
mirrors (``frontend-state.age`` + ``frontend-state.meta.json``) stay readable
until the first new generation replaces them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_policies, backup_unattended, backups, mutation_gate

MIRROR_METADATA_SCHEMA_VERSION = 2
BACKUP_MIRROR_DIR = config.ROOT / ".backup-mirror"
MIRROR_CIPHERTEXT_NAME = "frontend-state.age"
MIRROR_METADATA_NAME = "frontend-state.meta.json"
PREVIOUS_DIR_NAME = "previous"
GENERATIONS_DIR_NAME = "generations"
HEAD_NAME = "HEAD.json"
GENERATION_METADATA_NAME = "metadata.json"

MIRROR_STATUSES = ("current", "stale", "missing", "epoch-mismatch", "recipient-mismatch", "excluded")

_MAX_ENVELOPE_BYTES = 64 * 1024 * 1024
_GENERATION_ID = re.compile(r"^gen_[0-9a-f]{8,32}$")
_VARIANT_FILENAME = re.compile(r"^state\.[0-9a-f]{4,32}\.age$")

_profile_locks: dict[str, threading.Lock] = {}
_profile_locks_guard = threading.Lock()


def _profile_lock(profile: str) -> threading.Lock:
    with _profile_locks_guard:
        return _profile_locks.setdefault(profile, threading.Lock())


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _now_iso(now: datetime | None = None) -> str:
    value = now or datetime.now(tz=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _profile_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 64 or not all(char.isalnum() or char in "._-" for char in text):
        raise AppError("Invalid backup mirror profile id", code=ErrorCode.INVALID_PAYLOAD)
    return text


def _profile_dir(profile_id: str) -> Path:
    return BACKUP_MIRROR_DIR / profile_id


def _metadata_path(profile_id: str) -> Path:
    return _profile_dir(profile_id) / MIRROR_METADATA_NAME


def _ciphertext_path(profile_id: str) -> Path:
    return _profile_dir(profile_id) / MIRROR_CIPHERTEXT_NAME


def _head_path(profile_id: str) -> Path:
    return _profile_dir(profile_id) / HEAD_NAME


def _gate_root() -> Path:
    return BACKUP_MIRROR_DIR.parent


def _parse_iso(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AppError(f"Mirror field {name} is required", code=ErrorCode.INVALID_PAYLOAD)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppError(f"Mirror field {name} must be an ISO-8601 timestamp", code=ErrorCode.INVALID_PAYLOAD) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _generation_id(value: Any) -> str | None:
    text = str(value or "")
    return text if _GENERATION_ID.match(text) else None


def _generation_dir(profile_id: str, generation_id: str) -> Path:
    return _profile_dir(profile_id) / GENERATIONS_DIR_NAME / generation_id


def _read_legacy_metadata(profile_id: str) -> dict[str, Any] | None:
    path = _metadata_path(profile_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError("Backup mirror metadata is unreadable", code=ErrorCode.INTERNAL, status=500) from exc
    return data if isinstance(data, dict) else None


def _read_head(profile_id: str) -> dict[str, Any] | None:
    path = _head_path(profile_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_generation_metadata(profile_id: str, generation_id: str) -> dict[str, Any] | None:
    resolved = _generation_id(generation_id)
    if resolved is None:
        return None
    path = _generation_dir(profile_id, resolved) / GENERATION_METADATA_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _head_metadata(profile_id: str) -> dict[str, Any] | None:
    head = _read_head(profile_id)
    if head is not None:
        metadata = _read_generation_metadata(profile_id, str(head.get("generationId") or ""))
        if metadata is not None:
            return metadata
    return _read_legacy_metadata(profile_id)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _fsync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_legacy(profile_id: str) -> None:
    profile_dir = _profile_dir(profile_id)
    for name in (MIRROR_CIPHERTEXT_NAME, MIRROR_METADATA_NAME):
        (profile_dir / name).unlink(missing_ok=True)
    shutil.rmtree(profile_dir / PREVIOUS_DIR_NAME, ignore_errors=True)


def _prune_generations(profile_id: str, *, keep: set[str]) -> None:
    generations = _profile_dir(profile_id) / GENERATIONS_DIR_NAME
    if not generations.is_dir():
        return
    for entry in generations.iterdir():
        if entry.is_dir() and entry.name not in keep:
            shutil.rmtree(entry, ignore_errors=True)


def _variant_groups(recipients: tuple[str, ...] | list[str] | None) -> list[list[str]]:
    """Recipient groups to seal: explicit set, or one exact group per enabled policy.

    Groups are never merged across policies — each policy gets a variant sealed
    to exactly its own recipient set, so distinct recovery keys neither share
    mirror decryption ability nor trip ``recipient-mismatch``.
    """
    if recipients is not None:
        return [backup_policies.normalize_recipients(list(recipients))]
    groups: dict[str, list[str]] = {}
    for policy in backup_policies.enabled_policies():
        group = backup_policies.normalize_recipients(list((policy.get("protection") or {}).get("recipients") or []))
        if group:
            groups.setdefault(backup_policies.recipient_set_digest(group), group)
    if not groups:
        raise AppError("Backup mirror requires at least one recipient", code=ErrorCode.INVALID_PAYLOAD)
    return [groups[digest] for digest in sorted(groups)]


def put_frontend_mirror(
    profile_id: str,
    envelope: dict[str, Any],
    *,
    source_epoch: str,
    recipients: tuple[str, ...] | list[str] | None = None,
    acknowledged_at: str | None = None,
    client_replica_id: str = "",
    client_sequence: int = 0,
    expected_head_generation_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    profile = _profile_id(profile_id)
    if mutation_gate.read_fence(root=_gate_root()) is not None:
        raise AppError(
            "Backup mirror updates are fenced while a workspace restore is in progress",
            code=ErrorCode.INVALID_REQUEST,
            status=423,
        )
    groups = _variant_groups(recipients)
    group_digests = {backup_policies.recipient_set_digest(group) for group in groups}
    epoch = str(source_epoch or "").strip()
    if not epoch or len(epoch) > 120 or "/" in epoch or "\\" in epoch:
        raise AppError("Mirror sourceEpoch is required", code=ErrorCode.INVALID_PAYLOAD)
    if not isinstance(envelope, dict):
        raise AppError("Mirror envelope must be an object", code=ErrorCode.INVALID_PAYLOAD)
    envelope_bytes = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(envelope_bytes) > _MAX_ENVELOPE_BYTES:
        raise AppError("Mirror envelope is too large", code=ErrorCode.REQUEST_TOO_LARGE, status=413)
    backups._validate_frontend_envelope(envelope)
    ack = _parse_iso(acknowledged_at, "acknowledgedAt") if acknowledged_at else _now_iso(now)
    replica = str(client_replica_id or "")[:64]
    try:
        sequence = max(0, int(client_sequence or 0))
    except (TypeError, ValueError):
        raise AppError("Mirror clientSequence must be a non-negative integer", code=ErrorCode.INVALID_PAYLOAD)

    with _profile_lock(profile):
        head = _read_head(profile)
        existing = _head_metadata(profile)
        indexes: dict[str, int] = {}
        if head is not None:
            raw_indexes = head.get("epochIndexes")
            if isinstance(raw_indexes, dict):
                for key, value in raw_indexes.items():
                    try:
                        indexes[str(key)] = int(value)
                    except (TypeError, ValueError):
                        continue
        accepted_sequence = _int_or(head.get("acceptedSequence") if head is not None else None, -1)
        accepted_index = _int_or(head.get("acceptedEpochIndex") if head is not None else None, 0)
        accepted_epoch = str(head.get("acceptedEpoch") or "") if head is not None else ""
        if not accepted_epoch and existing is not None:
            accepted_epoch = str(existing.get("sourceEpoch") or "")
        has_state = head is not None or existing is not None
        if has_state:
            same_envelope = existing is not None and existing.get("envelopeDigest") == envelope.get("digest")
            existing_digests = {str(variant.get("recipientSetDigest") or "") for variant in (existing or {}).get("recipientVariants") or []}
            if not existing_digests and existing is not None:
                existing_digests = {str(existing.get("recipientSetDigest") or "")}
            same_recipients = existing_digests == group_digests
            if existing is not None and epoch == accepted_epoch and same_envelope and same_recipients:
                return {**existing, "idempotent": True}
            if epoch != accepted_epoch and epoch in indexes and indexes[epoch] <= accepted_index:
                raise AppError("mirror-stale-epoch: epoch was superseded on the server; resync before uploading", code=ErrorCode.INVALID_REQUEST, status=409)
            if sequence <= accepted_sequence:
                raise AppError("mirror-stale-sequence: clientSequence must increase monotonically per profile", code=ErrorCode.INVALID_REQUEST, status=409)
        head_generation_id = str(head.get("generationId") or "") if head is not None else ""
        if expected_head_generation_id is not None and str(expected_head_generation_id) != head_generation_id:
            raise AppError("mirror-head-conflict: mirror head changed since the client's snapshot", code=ErrorCode.INVALID_REQUEST, status=409)

        generation_id = f"gen_{uuid.uuid4().hex[:24]}"
        generation_dir = _generation_dir(profile, generation_id)
        generation_dir.mkdir(parents=True, exist_ok=True)
        expected_plaintext = envelope_bytes

        def _write_plaintext(output: Any) -> None:
            output.write(expected_plaintext)

        def _verify(decrypted: Path) -> None:
            if decrypted.read_bytes() != expected_plaintext:
                raise AppError("Mirror round-trip verification failed", code=ErrorCode.INTERNAL, status=500)

        variants: list[dict[str, Any]] = []
        tmp_files: list[Path] = []
        try:
            for group in groups:
                group_digest = backup_policies.recipient_set_digest(group)
                variant_filename = f"state.{group_digest[:16]}.age"
                tmp_ciphertext = generation_dir / f".{variant_filename}.{os.getpid()}.tmp"
                tmp_files.append(tmp_ciphertext)
                encryption = backup_unattended.encrypt_unattended(
                    tmp_ciphertext,
                    _write_plaintext,
                    recipients=group,
                    verify=_verify,
                )
                os.replace(tmp_ciphertext, generation_dir / variant_filename)
                variants.append(
                    {
                        "recipientSetDigest": group_digest,
                        "ciphertextSha256": encryption.ciphertext_sha256,
                        "filename": variant_filename,
                        "creationVerified": encryption.creation_verified,
                    }
                )
            variants.sort(key=lambda item: str(item["recipientSetDigest"]))
            first = variants[0]
            metadata: dict[str, Any] = {
                "schemaVersion": MIRROR_METADATA_SCHEMA_VERSION,
                "profileId": profile,
                "generationId": generation_id,
                "parentGenerationId": head_generation_id or None,
                "sourceEpoch": epoch,
                "clientReplicaId": replica,
                "clientSequence": sequence,
                "envelopeDigest": str(envelope.get("digest") or ""),
                "recipientVariants": variants,
                "recipientSetDigest": first["recipientSetDigest"],
                "conversations": len(envelope.get("conversations") or []),
                "conflicts": len(envelope.get("conflicts") or []),
                "createdAt": _now_iso(now),
                "acknowledgedAt": ack,
                "ciphertextSha256": first["ciphertextSha256"],
                "creationVerified": bool(first["creationVerified"]),
            }
            _atomic_write(generation_dir / GENERATION_METADATA_NAME, (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
            _fsync_dir(generation_dir)
        finally:
            for tmp_file in tmp_files:
                tmp_file.unlink(missing_ok=True)
        epoch_index = indexes.get(epoch, max(indexes.values(), default=0) + 1)
        indexes[epoch] = epoch_index
        head_payload = {
            "schemaVersion": 2,
            "generationId": generation_id,
            "updatedAt": _now_iso(now),
            "acceptedEpoch": epoch,
            "acceptedEpochIndex": epoch_index,
            "acceptedSequence": sequence,
            "epochIndexes": indexes,
        }
        _atomic_write(_head_path(profile), (json.dumps(head_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        _fsync_dir(_profile_dir(profile))
        _remove_legacy(profile)
        _prune_generations(profile, keep={generation_id, head_generation_id})
    return metadata


def list_mirrors() -> list[dict[str, Any]]:
    if not BACKUP_MIRROR_DIR.is_dir():
        return []
    mirrors: list[dict[str, Any]] = []
    for path in sorted(BACKUP_MIRROR_DIR.iterdir()):
        if not path.is_dir() or path.name == PREVIOUS_DIR_NAME:
            continue
        try:
            metadata = _head_metadata(path.name)
        except AppError:
            continue
        if metadata is not None:
            mirrors.append(metadata)
    return mirrors


def latest_mirror() -> dict[str, Any] | None:
    mirrors = list_mirrors()
    if not mirrors:
        return None
    return max(mirrors, key=lambda item: str(item.get("acknowledgedAt") or ""))


def _recipients_match(metadata: dict[str, Any], recipients: tuple[str, ...] | list[str]) -> bool:
    digest = backup_policies.recipient_set_digest(list(recipients))
    variants = metadata.get("recipientVariants") or []
    if variants:
        return any(str(variant.get("recipientSetDigest") or "") == digest for variant in variants if isinstance(variant, dict))
    return str(metadata.get("recipientSetDigest") or "") == digest


def mirror_status(
    profile_id: str | None,
    *,
    recipients: tuple[str, ...] | list[str] | None = None,
    max_age_seconds: int | None = None,
    expected_epoch: str | None = None,
    excluded: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    if excluded:
        return {"status": "excluded"}
    metadata = _head_metadata(_profile_id(profile_id)) if profile_id else latest_mirror()
    if metadata is None:
        return {"status": "missing", "profileId": profile_id}
    status = "current"
    if expected_epoch and str(metadata.get("sourceEpoch") or "") != str(expected_epoch):
        status = "epoch-mismatch"
    elif recipients is not None and not _recipients_match(metadata, recipients):
        status = "recipient-mismatch"
    elif max_age_seconds is not None:
        try:
            acknowledged = datetime.fromisoformat(str(metadata.get("acknowledgedAt") or "").replace("Z", "+00:00"))
        except ValueError:
            acknowledged = None
        current = now or datetime.now(tz=timezone.utc)
        age = (current.astimezone(timezone.utc) - acknowledged).total_seconds() if acknowledged else float("inf")
        if age > max_age_seconds:
            status = "stale"
    return {"status": status, "mirror": metadata}


def mirror_files(profile_id: str, *, recipients: tuple[str, ...] | list[str] | None = None) -> tuple[Path, Path, dict[str, Any]]:
    profile = _profile_id(profile_id)
    head = _read_head(profile)
    if head is not None:
        generation_id = _generation_id(str(head.get("generationId") or ""))
        if generation_id is not None:
            metadata = _read_generation_metadata(profile, generation_id)
            if metadata is not None:
                variants = [variant for variant in (metadata.get("recipientVariants") or []) if isinstance(variant, dict)]
                variant: dict[str, Any] | None = None
                if recipients is not None:
                    digest = backup_policies.recipient_set_digest(list(recipients))
                    variant = next((item for item in variants if str(item.get("recipientSetDigest") or "") == digest), None)
                    if variant is None:
                        raise AppError("Backup mirror has no variant sealed to this recipient set", code=ErrorCode.NOT_FOUND, status=404)
                elif variants:
                    variant = variants[0]
                if variant is None:
                    raise AppError("Mirror generation carries no recipient variants", code=ErrorCode.INTERNAL, status=500)
                filename = str(variant.get("filename") or "")
                if not _VARIANT_FILENAME.match(filename):
                    raise AppError("Mirror generation carries an invalid variant filename", code=ErrorCode.INTERNAL, status=500)
                ciphertext = _generation_dir(profile, generation_id) / filename
                if not ciphertext.is_file():
                    raise AppError("Backup mirror generation ciphertext is missing", code=ErrorCode.NOT_FOUND, status=404)
                expected = str(variant.get("ciphertextSha256") or "")
                if expected and backup_unattended.sha256_file(ciphertext) != expected:
                    raise AppError("mirror-generation-corrupt: ciphertext no longer matches its generation", code=ErrorCode.INVALID_REQUEST, status=409)
                return ciphertext, _generation_dir(profile, generation_id) / GENERATION_METADATA_NAME, metadata
    metadata = _read_legacy_metadata(profile)
    ciphertext = _ciphertext_path(profile)
    if metadata is None or not ciphertext.is_file():
        raise AppError("Backup mirror not found", code=ErrorCode.NOT_FOUND, status=404)
    expected = str(metadata.get("ciphertextSha256") or "")
    if expected and backup_unattended.sha256_file(ciphertext) != expected:
        raise AppError("mirror-generation-corrupt: ciphertext no longer matches its metadata", code=ErrorCode.INVALID_REQUEST, status=409)
    return ciphertext, _metadata_path(profile), metadata

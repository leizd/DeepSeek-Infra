"""Sealed frontend replica mirror (4.4.4).

The mirror lets an unattended scheduler include browser session state without
the server ever holding it in plaintext at rest. The browser uploads its
frontend envelope; the server re-verifies the envelope digest, encrypts it
immediately to the policy's public age recipients (plus an ephemeral
verification recipient that is destroyed), and atomically replaces the sealed
mirror. Metadata never contains conversation titles, message bodies, drafts,
writer ids, leases or recovery identities.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_policies, backup_unattended, backups, mutation_gate

MIRROR_METADATA_SCHEMA_VERSION = 1
BACKUP_MIRROR_DIR = config.ROOT / ".backup-mirror"
MIRROR_CIPHERTEXT_NAME = "frontend-state.age"
MIRROR_METADATA_NAME = "frontend-state.meta.json"
PREVIOUS_DIR_NAME = "previous"

MIRROR_STATUSES = ("current", "stale", "missing", "epoch-mismatch", "recipient-mismatch", "excluded")

_MAX_ENVELOPE_BYTES = 64 * 1024 * 1024


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


def _read_metadata(profile_id: str) -> dict[str, Any] | None:
    path = _metadata_path(profile_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError("Backup mirror metadata is unreadable", code=ErrorCode.INTERNAL, status=500) from exc
    return data if isinstance(data, dict) else None


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


def put_frontend_mirror(
    profile_id: str,
    envelope: dict[str, Any],
    *,
    source_epoch: str,
    recipients: tuple[str, ...] | list[str] | None = None,
    acknowledged_at: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    profile = _profile_id(profile_id)
    if mutation_gate.read_fence(root=_gate_root()) is not None:
        raise AppError(
            "Backup mirror updates are fenced while a workspace restore is in progress",
            code=ErrorCode.INVALID_REQUEST,
            status=423,
        )
    resolved_recipients = backup_policies.normalize_recipients(list(recipients) if recipients is not None else list(backup_policies.active_recipients()))
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

    existing = _read_metadata(profile)
    recipient_digest = backup_policies.recipient_set_digest(resolved_recipients)
    if existing is not None:
        same_envelope = existing.get("envelopeDigest") == envelope.get("digest")
        same_recipients = existing.get("recipientSetDigest") == recipient_digest
        if same_envelope and same_recipients:
            return {**existing, "idempotent": True}
        if str(ack) < str(existing.get("acknowledgedAt") or ""):
            raise AppError("Stale mirror upload from an older tab or generation", code=ErrorCode.INVALID_REQUEST, status=409)

    profile_dir = _profile_dir(profile_id)
    profile_dir.mkdir(parents=True, exist_ok=True)
    tmp_ciphertext = profile_dir / f".{MIRROR_CIPHERTEXT_NAME}.{os.getpid()}.tmp"
    expected_plaintext = envelope_bytes

    def _write_plaintext(output: Any) -> None:
        output.write(expected_plaintext)

    def _verify(decrypted: Path) -> None:
        if decrypted.read_bytes() != expected_plaintext:
            raise AppError("Mirror round-trip verification failed", code=ErrorCode.INTERNAL, status=500)

    try:
        encryption = backup_unattended.encrypt_unattended(
            tmp_ciphertext,
            _write_plaintext,
            recipients=resolved_recipients,
            verify=_verify,
        )
        if existing is not None:
            previous_dir = profile_dir / PREVIOUS_DIR_NAME
            previous_dir.mkdir(parents=True, exist_ok=True)
            for name in (MIRROR_CIPHERTEXT_NAME, MIRROR_METADATA_NAME):
                current = profile_dir / name
                if current.is_file():
                    os.replace(current, previous_dir / name)
        metadata = {
            "schemaVersion": MIRROR_METADATA_SCHEMA_VERSION,
            "profileId": profile,
            "sourceEpoch": epoch,
            "envelopeDigest": str(envelope.get("digest") or ""),
            "recipientSetDigest": recipient_digest,
            "conversations": len(envelope.get("conversations") or []),
            "conflicts": len(envelope.get("conflicts") or []),
            "createdAt": _now_iso(now),
            "acknowledgedAt": ack,
            "ciphertextSha256": encryption.ciphertext_sha256,
            "creationVerified": encryption.creation_verified,
        }
        os.replace(tmp_ciphertext, _ciphertext_path(profile))
        _atomic_write(_metadata_path(profile), (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        _fsync_dir(profile_dir)
    finally:
        tmp_ciphertext.unlink(missing_ok=True)
    return metadata


def list_mirrors() -> list[dict[str, Any]]:
    if not BACKUP_MIRROR_DIR.is_dir():
        return []
    mirrors: list[dict[str, Any]] = []
    for path in sorted(BACKUP_MIRROR_DIR.iterdir()):
        if not path.is_dir() or path.name == PREVIOUS_DIR_NAME:
            continue
        try:
            metadata = _read_metadata(path.name)
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
    metadata = _read_metadata(_profile_id(profile_id)) if profile_id else latest_mirror()
    if metadata is None:
        return {"status": "missing", "profileId": profile_id}
    status = "current"
    if expected_epoch and str(metadata.get("sourceEpoch") or "") != str(expected_epoch):
        status = "epoch-mismatch"
    elif recipients is not None and backup_policies.recipient_set_digest(list(recipients)) != str(metadata.get("recipientSetDigest") or ""):
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


def mirror_files(profile_id: str) -> tuple[Path, Path, dict[str, Any]]:
    profile = _profile_id(profile_id)
    metadata = _read_metadata(profile)
    ciphertext = _ciphertext_path(profile)
    if metadata is None or not ciphertext.is_file():
        raise AppError("Backup mirror not found", code=ErrorCode.NOT_FOUND, status=404)
    return ciphertext, _metadata_path(profile), metadata

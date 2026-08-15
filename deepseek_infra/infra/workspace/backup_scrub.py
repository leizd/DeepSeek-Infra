"""Ciphertext scrubbing and restore drills (4.4.5).

Scrubs re-read ciphertext and verify size, SHA-256 and age header validity
without any Recovery Identity — they never claim the content was unlocked.
User restore drills unlock with the real Recovery Identity, run the full
inspect pipeline (including the sealed frontend mirror), record
``userUnlockVerifiedAt`` and then destroy the identity copy.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_catalog, backup_crypto, backup_object_set, backup_publish, backup_targets, backup_unattended, backups

UNLOCK_VERIFICATION_WARNING_DAYS = 30


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _catalog_record(root: Path, backup_id: str) -> dict[str, Any]:
    record = backup_catalog.catalog_state(root).get(backup_id)
    if record is None:
        raise AppError("Backup not found in catalog", code=ErrorCode.NOT_FOUND, status=404)
    return record


def _ciphertext_path(root: Path, record: dict[str, Any]) -> Path:
    candidates = backup_publish.backup_file_candidates(root, record)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if candidates:
        return candidates[0]
    return root / "backups" / str(record.get("filename") or "")


def _ciphertext_members(root: Path, record: dict[str, Any]) -> list[tuple[Path, str, int]]:
    inventory = backup_object_set.committed_object_inventory(record)
    if str(record.get("storageProtocol") or "") == backup_object_set.OBJECT_SET_V1:
        return [
            (backup_publish.object_path(root, str(item["digest"])), str(item["digest"]), int(item["size"]))
            for item in inventory
        ]
    if not inventory:
        return []
    item = inventory[0]
    return [(_ciphertext_path(root, record), str(item["digest"]), int(item["size"]))]


def scrub_backup(root: Path, backup_id: str, *, target_id: str | None = None) -> dict[str, Any]:
    """Re-verify every committed ciphertext object without unlocking it."""
    record = _catalog_record(root, backup_id)
    checks: dict[str, str] = {}
    ok = True

    def _check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        checks[name] = "PASS" if passed else f"FAIL: {detail}"
        ok = ok and passed

    try:
        members = _ciphertext_members(root, record)
    except AppError as exc:
        members = []
        _check("exists", False, str(exc)[:80])
    if "exists" not in checks:
        missing = [path.name for path, _, _ in members if not path.is_file()]
        _check("exists", bool(members) and not missing, f"missing {', '.join(missing[:3])}")
    if members and all(path.is_file() for path, _, _ in members):
        reparsed = [path.name for path, _, _ in members if backup_targets._is_reparse_point(path)]
        _check("not-symlink", not reparsed, f"reparse point {', '.join(reparsed[:3])}")
        wrong_sizes = [path.name for path, _, size in members if path.stat().st_size != size]
        _check("size", not wrong_sizes, f"size mismatch {', '.join(wrong_sizes[:3])}")
        wrong_digests = [path.name for path, digest, _ in members if backup_unattended.sha256_file(path) != digest]
        _check("sha256", not wrong_digests, f"digest mismatch {', '.join(wrong_digests[:3])}")
        try:
            invalid_headers = [path.name for path, _, _ in members if not bool(backup_crypto.inspect_header(path).get("age"))]
            _check("age-header", not invalid_headers, f"invalid header {', '.join(invalid_headers[:3])}")
        except AppError as exc:
            _check("age-header", False, str(exc)[:80])
    if target_id and target_id != "managed-local":
        try:
            backup_targets.verify_target_ready(target_id)
            _check("target-marker", True)
        except AppError as exc:
            _check("target-marker", False, str(exc)[:80])
    backup_catalog.record_scrub(root, backup_id, ok=ok, detail="; ".join(f"{k}={v}" for k, v in checks.items() if v != "PASS"))
    effective_target_id = str(target_id or "managed-local")
    try:
        from deepseek_infra.infra.workspace import backup_dr_ledger
        backup_dr_ledger.record_scrub_evidence(
            target_id=effective_target_id,
            backup_id=backup_id,
            observed_at=_utc_iso(),
            result="success" if ok else "failed",
            details={"checks": checks, "objectsScrubbed": len(members)},
        )
    except Exception:
        pass
    return {"backupId": backup_id, "ok": ok, "checks": checks, "objectsScrubbed": len(members), "scrubbedAt": _utc_iso()}


def scrub_all(root: Path, *, target_id: str | None = None) -> dict[str, Any]:
    results = []
    for record in backup_catalog.list_backups(root):
        results.append(scrub_backup(root, str(record["backupId"]), target_id=target_id))
    return {"scrubbed": len(results), "ok": all(item["ok"] for item in results), "results": results}


def _unlock_object_set(
    root: Path,
    record: dict[str, Any],
    identity: bytearray,
    *,
    staged: Path,
) -> tuple[dict[str, Any], str]:
    inventory = backup_object_set.committed_object_inventory(record)
    committed = {str(item["digest"]): item for item in inventory}
    control_digest = str(record.get("controlObjectDigest") or "")
    if control_digest not in committed:
        raise AppError("Object-set control ciphertext is not committed", code=ErrorCode.INVALID_PAYLOAD)
    for digest, item in committed.items():
        source = backup_publish.object_path(root, digest)
        if not source.is_file():
            raise AppError("Backup object-set member is missing", code=ErrorCode.NOT_FOUND, status=404)
        if source.stat().st_size != int(item["size"]) or backup_unattended.sha256_file(source) != digest:
            raise AppError("Backup object-set member no longer matches its receipt", code=ErrorCode.INVALID_REQUEST, status=409)

    extracted = staged / "extracted"
    control_plaintext = staged / "unlocked-control.zip"
    backup_crypto.decrypt_file(
        backup_publish.object_path(root, control_digest),
        control_plaintext,
        kind="age-identity",
        secret=identity,
    )
    manifest = backups.extract_archive_metadata(control_plaintext, extracted)
    backup_object_set.verify_control_metadata(extracted)
    manifest_digest = backups._sha256_file(extracted / "manifest.json")
    try:
        payload_index = json.loads((extracted / "payload-index.json").read_text(encoding="utf-8"))
        component_map = json.loads((extracted / "component-map.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError("Object-set control metadata is invalid", code=ErrorCode.INVALID_PAYLOAD) from exc
    descriptors = payload_index.get("payloadComponents") if isinstance(payload_index, dict) else None
    components = component_map.get("components") if isinstance(component_map, dict) else None
    if not isinstance(descriptors, dict) or not isinstance(components, dict) or set(descriptors) != set(components):
        raise AppError("Object-set component inventory is invalid", code=ErrorCode.INVALID_PAYLOAD)
    payload_digests: set[str] = set()
    for component_id in sorted(descriptors):
        descriptor = descriptors[component_id]
        expected_paths = components[component_id]
        if not isinstance(descriptor, dict) or not isinstance(expected_paths, list) or any(not isinstance(path, str) for path in expected_paths):
            raise AppError("Object-set component metadata is invalid", code=ErrorCode.INVALID_PAYLOAD)
        digest = str(descriptor.get("ciphertextDigest") or "")
        committed_item = committed.get(digest)
        if committed_item is None or int(committed_item["size"]) != int(descriptor.get("ciphertextSize") or -1) or digest in payload_digests:
            raise AppError("Object-set payload is not exactly committed", code=ErrorCode.INVALID_PAYLOAD)
        payload_digests.add(digest)
        plaintext = staged / f"unlocked-{component_id}.zip"
        backup_crypto.decrypt_file(
            backup_publish.object_path(root, digest),
            plaintext,
            kind="age-identity",
            secret=identity,
        )
        if (
            plaintext.stat().st_size != int(descriptor.get("plaintextSize") or -1)
            or backup_unattended.sha256_file(plaintext) != str(descriptor.get("plaintextSha256") or "")
        ):
            raise AppError("Object-set payload plaintext commitment mismatch", code=ErrorCode.INVALID_PAYLOAD)
        backup_object_set.extract_component_archive(plaintext, extracted, expected_paths)
        backup_unattended.scrub_plaintext_file(plaintext)
    if payload_digests != (set(committed) - {control_digest}):
        raise AppError("Object-set receipt contains a foreign payload", code=ErrorCode.INVALID_PAYLOAD)
    (extracted / "payload-index.json").unlink()
    (extracted / "component-map.json").unlink()
    manifest = backups._verify_manifest_tree(extracted)
    backup_unattended.scrub_plaintext_file(control_plaintext)
    return manifest, manifest_digest


def verify_unlock_drill(
    root: Path,
    backup_id: str,
    identity: bytearray,
    *,
    staged_root: Path,
) -> dict[str, Any]:
    """Full read-only restore drill with the user's real Recovery Identity.

    Unlocks, inspects, validates the sealed frontend mirror when present, never
    applies anything, records ``userUnlockVerifiedAt`` and destroys plaintext.
    """
    record = _catalog_record(root, backup_id)
    staged = staged_root / f"drill_{backup_id}"
    extracted = staged / "extracted"
    decrypted = staged / "unlocked.dsibackup"
    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True)
    frontend: dict[str, Any] | None = None
    try:
        if str(record.get("storageProtocol") or "") == backup_object_set.OBJECT_SET_V1:
            manifest, manifest_digest = _unlock_object_set(root, record, identity, staged=staged)
        else:
            source = _ciphertext_path(root, record)
            if not source.is_file():
                raise AppError("Backup file is missing", code=ErrorCode.NOT_FOUND, status=404)
            if backup_unattended.sha256_file(source) != str(record.get("ciphertextSha256") or ""):
                raise AppError("Backup ciphertext no longer matches its receipt", code=ErrorCode.INVALID_REQUEST, status=409)
            backup_crypto.decrypt_file(source, decrypted, kind="age-identity", secret=identity)
            manifest = backups._safe_extract_and_verify(decrypted, extracted)
            manifest_digest = backups._sha256_file(extracted / "manifest.json")
            if manifest_digest != str(record.get("manifestDigest") or ""):
                raise AppError("Backup manifest digest does not match its receipt", code=ErrorCode.INVALID_PAYLOAD)
        sealed = extracted / "frontend" / "sealed-state.age"
        if sealed.is_file():
            frontend = backups._unlock_sealed_frontend(staged, "age-identity", identity)
            if frontend is None:
                raise AppError("Sealed frontend mirror could not be unlocked", code=ErrorCode.INVALID_PAYLOAD)
        backup_catalog.record_unlock_verification(root, backup_id)
        return {
            "backupId": backup_id,
            "ok": True,
            "manifestDigest": manifest_digest,
            "contributors": len(manifest.get("contributors") or []),
            "sealedFrontend": frontend,
            "userUnlockVerifiedAt": _utc_iso(),
        }
    finally:
        if decrypted.exists():
            backup_unattended.scrub_plaintext_file(decrypted)
        for plaintext in staged.glob("unlocked-*.zip"):
            backup_unattended.scrub_plaintext_file(plaintext)
        verified_frontend = staged / "verified" / "frontend" / "state.json"
        if verified_frontend.exists():
            backup_unattended.scrub_plaintext_file(verified_frontend)
        shutil.rmtree(staged, ignore_errors=True)


def backup_health(root: Path, *, now: datetime | None = None, warn_after_days: int = UNLOCK_VERIFICATION_WARNING_DAYS) -> dict[str, Any]:
    """Health summary: scrub status and unlock-drill freshness per backup."""
    current = now or datetime.now(tz=timezone.utc)
    entries = []
    worst = "ok"
    for record in backup_catalog.list_backups(root):
        issues = []
        if record.get("scrubOk") is False:
            issues.append("scrub-failed")
        verified = record.get("userUnlockVerifiedAt")
        if not verified:
            issues.append("unlock-verification-missing")
        else:
            try:
                verified_at = datetime.fromisoformat(str(verified).replace("Z", "+00:00"))
                if current - verified_at > timedelta(days=warn_after_days):
                    issues.append("unlock-verification-overdue")
            except ValueError:
                issues.append("unlock-verification-missing")
        status = "ok"
        if "scrub-failed" in issues:
            status = "error"
            worst = "error"
        elif issues:
            status = "warning"
            if worst != "error":
                worst = "warning"
        entries.append(
            {
                "backupId": record["backupId"],
                "status": status,
                "issues": issues,
                "creationVerified": bool(record.get("creationVerified")),
                "ciphertextScrubbedAt": record.get("ciphertextScrubbedAt"),
                "userUnlockVerifiedAt": verified,
            }
        )
    return {"status": worst, "backups": entries, "evaluatedAt": _utc_iso(current)}

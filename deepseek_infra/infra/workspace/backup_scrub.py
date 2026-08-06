"""Ciphertext scrubbing and restore drills (4.4.4).

Scrubs re-read ciphertext and verify size, SHA-256 and age header validity
without any Recovery Identity — they never claim the content was unlocked.
User restore drills unlock with the real Recovery Identity, run the full
inspect pipeline (including the sealed frontend mirror), record
``userUnlockVerifiedAt`` and then destroy the identity copy.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_catalog, backup_crypto, backup_targets, backup_unattended, backups

UNLOCK_VERIFICATION_WARNING_DAYS = 30


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _catalog_record(root: Path, backup_id: str) -> dict[str, Any]:
    record = backup_catalog.catalog_state(root).get(backup_id)
    if record is None:
        raise AppError("Backup not found in catalog", code=ErrorCode.NOT_FOUND, status=404)
    return record


def scrub_backup(root: Path, backup_id: str, *, target_id: str | None = None) -> dict[str, Any]:
    """Re-verify one ciphertext against its receipt without unlocking it."""
    record = _catalog_record(root, backup_id)
    filename = str(record.get("filename") or "")
    path = root / "backups" / filename
    checks: dict[str, str] = {}
    ok = True

    def _check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        checks[name] = "PASS" if passed else f"FAIL: {detail}"
        ok = ok and passed

    _check("exists", path.is_file(), "missing")
    if path.is_file():
        _check("not-symlink", not backup_targets._is_reparse_point(path), "reparse point")
        _check("size", path.stat().st_size == int(record.get("size") or -1), "size mismatch")
        digest = backup_unattended.sha256_file(path)
        _check("sha256", digest == str(record.get("ciphertextSha256") or ""), "digest mismatch")
        try:
            header = backup_crypto.inspect_header(path)
            _check("age-header", bool(header.get("age")), "invalid header")
        except AppError as exc:
            _check("age-header", False, str(exc)[:80])
    if target_id and target_id != "managed-local":
        try:
            backup_targets.verify_target_ready(target_id)
            _check("target-marker", True)
        except AppError as exc:
            _check("target-marker", False, str(exc)[:80])
    backup_catalog.record_scrub(root, backup_id, ok=ok, detail="; ".join(f"{k}={v}" for k, v in checks.items() if v != "PASS"))
    return {"backupId": backup_id, "ok": ok, "checks": checks, "scrubbedAt": _utc_iso()}


def scrub_all(root: Path, *, target_id: str | None = None) -> dict[str, Any]:
    results = []
    for record in backup_catalog.list_backups(root):
        results.append(scrub_backup(root, str(record["backupId"]), target_id=target_id))
    return {"scrubbed": len(results), "ok": all(item["ok"] for item in results), "results": results}


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
    filename = str(record.get("filename") or "")
    source = root / "backups" / filename
    if not source.is_file():
        raise AppError("Backup file is missing", code=ErrorCode.NOT_FOUND, status=404)
    if backup_unattended.sha256_file(source) != str(record.get("ciphertextSha256") or ""):
        raise AppError("Backup ciphertext no longer matches its receipt", code=ErrorCode.INVALID_REQUEST, status=409)
    staged = staged_root / f"drill_{backup_id}"
    extracted = staged / "extracted"
    decrypted = staged / "unlocked.dsibackup"
    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True)
    frontend: dict[str, Any] | None = None
    try:
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

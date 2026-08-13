"""Manual isolated Recovery Drill orchestration (4.5.0)."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_crypto,
    backup_object_set,
    backup_recovery_job,
    backup_remote_restore,
    backup_unattended,
    backup_verified_plan,
    backups,
)

SCHEMA_VERSION = 1
RESULT_NAME = "drill-result.json"
CLAIM_NAME = "drill-running.json"
DISALLOWED_PHASES = frozenset(
    {"preparing", "prepared", "committing", "complete", "aborting", "aborted", "rolled-back", "failed", "recovery-required"}
)
PLAINTEXT_FILES = (
    "plan.json",
    backup_verified_plan.PLAN_NAME,
)
PLAINTEXT_PATTERNS = (
    "control-decrypted-*.zip",
    "payload-decrypted-*.zip",
    "decrypted-*.dsibackup",
    "preview-decrypted-*.dsibackup",
    "unlocked.dsibackup",
)
PLAINTEXT_DIR_PATTERNS = (
    "extracted",
    "verified",
    "staged",
    "rollback",
    "metadata-*",
    "object-layer-*",
    "extracted-*",
    "projected-*",
)


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _root(restore_id: str) -> Path:
    if not restore_id.startswith("restore_") or not restore_id[8:].isalnum():
        raise AppError("Invalid restore id", code=ErrorCode.INVALID_PAYLOAD)
    root = backups.RESTORE_DIR / restore_id
    if not root.is_dir():
        raise AppError("Remote restore session not found", code=ErrorCode.NOT_FOUND, status=404)
    return root


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError("Recovery Drill metadata is unavailable", code=ErrorCode.INVALID_PAYLOAD) from exc
    if not isinstance(raw, dict):
        raise AppError("Recovery Drill metadata is unavailable", code=ErrorCode.INVALID_PAYLOAD)
    return raw


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _claim(root: Path, restore_id: str) -> dict[str, Any] | None:
    session_path = root / "remote-fetch.json"
    with backup_recovery_job.session_lock(session_path):
        result_path = root / RESULT_NAME
        if result_path.is_file():
            return _read_json(result_path)
        claim_path = root / CLAIM_NAME
        session = backup_remote_restore.read_restore_session(restore_id)
        if session is None:
            raise AppError("Remote restore session not found", code=ErrorCode.NOT_FOUND, status=404)
        phase = str(session.get("phase") or "")
        if (root / "transaction.json").is_file() or phase in DISALLOWED_PHASES:
            raise AppError("Recovery Drill requires a pre-commit Recovery Job", code=ErrorCode.INVALID_REQUEST, status=409)
        if not backup_crypto.has_secret(restore_id):
            raise AppError("Recovery Drill requires an available unlock secret", code=ErrorCode.INVALID_REQUEST, status=409)
        _atomic_write(
            claim_path,
            {"schemaVersion": SCHEMA_VERSION, "restoreId": restore_id, "startedAt": _utc_iso()},
        )
    return None


@contextmanager
def _exclusive_drill_lock(root: Path) -> Iterator[None]:
    path = root / ".drill.lock"
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                getattr(msvcrt, "locking")(handle.fileno(), getattr(msvcrt, "LK_NBLCK"), 1)
            else:
                import fcntl

                getattr(fcntl, "flock")(handle.fileno(), getattr(fcntl, "LOCK_EX") | getattr(fcntl, "LOCK_NB"))
        except OSError as exc:
            raise AppError("Recovery Drill is already running", code=ErrorCode.INVALID_REQUEST, status=409) from exc
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                getattr(msvcrt, "locking")(handle.fileno(), getattr(msvcrt, "LK_UNLCK"), 1)
            else:
                import fcntl

                getattr(fcntl, "flock")(handle.fileno(), getattr(fcntl, "LOCK_UN"))


def _scrub_directory(path: Path) -> None:
    if not path.is_dir():
        return
    for file_path in sorted((item for item in path.rglob("*") if item.is_file()), key=lambda item: len(item.parts), reverse=True):
        backup_unattended.scrub_plaintext_file(file_path)
    shutil.rmtree(path, ignore_errors=False)


def _scrub_plaintext(root: Path) -> None:
    for name in PLAINTEXT_FILES:
        backup_unattended.scrub_plaintext_file(root / name)
    for pattern in PLAINTEXT_PATTERNS:
        for path in root.glob(pattern):
            if path.is_file():
                backup_unattended.scrub_plaintext_file(path)
    seen: set[Path] = set()
    for pattern in PLAINTEXT_DIR_PATTERNS:
        for path in root.glob(pattern):
            if path.is_dir() and path not in seen:
                seen.add(path)
                _scrub_directory(path)


def _plaintext_remains(root: Path) -> bool:
    if any((root / name).exists() for name in PLAINTEXT_FILES):
        return True
    if any(path.exists() for pattern in PLAINTEXT_PATTERNS for path in root.glob(pattern)):
        return True
    return any(path.exists() for pattern in PLAINTEXT_DIR_PATTERNS for path in root.glob(pattern))


def _work(session: dict[str, Any], materialized: dict[str, Any], inspected: dict[str, Any]) -> dict[str, int]:
    members = backup_remote_restore.restore_members(session)
    components = 0
    ciphertext_bytes = 0
    for member in members:
        control = member.get("control")
        if isinstance(control, dict):
            components += 1
            ciphertext_bytes += max(0, int(control.get("expectedBytes") or 0))
        elif member.get("expectedBytes") is not None:
            components += 1
            ciphertext_bytes += max(0, int(member.get("expectedBytes") or 0))
        for component in member.get("requiredComponents") or []:
            if isinstance(component, dict):
                components += 1
                ciphertext_bytes += max(0, int(component.get("expectedBytes") or 0))
    manifest = materialized.get("manifest")
    files = manifest.get("files") if isinstance(manifest, dict) else None
    logical_bytes = sum(max(0, int(item.get("size") or 0)) for item in files or [] if isinstance(item, dict))
    operations = inspected.get("operations")
    verified_contributors = len([item for item in operations or [] if isinstance(item, dict)])
    if verified_contributors == 0 and isinstance(manifest, dict):
        verified_contributors = len([item for item in manifest.get("contributors") or [] if isinstance(item, dict)])
    return {
        "chainLength": len(members),
        "components": components,
        "ciphertextBytes": ciphertext_bytes,
        "logicalBytes": logical_bytes,
        "verifiedContributors": verified_contributors,
    }


def _inspect_materialized(
    restore_id: str,
    materialized: dict[str, Any],
    *,
    kind: str,
) -> dict[str, Any]:
    tree = Path(str(materialized.get("tree") or ""))
    expected_tree = (_root(restore_id) / "extracted").resolve()
    if tree.resolve() != expected_tree:
        raise AppError("Recovery Drill materialized outside its isolated root", code=ErrorCode.INVALID_REQUEST, status=409)
    manifest = materialized.get("manifest")
    projection = materialized.get("projection")
    protection = "passphrase" if kind == "passphrase" else "age-recipient"
    if isinstance(projection, dict) and isinstance(manifest, dict):
        return backups.inspect_projected_restore_tree(
            restore_id,
            tree,
            protection=protection,
            ciphertext_sha256=None,
            projection=projection,
            manifest=manifest,
        )
    return backups.inspect_verified_restore_tree(restore_id, tree, protection=protection, ciphertext_sha256=None)


def _run_recovery_drill_locked(restore_id: str, *, client: Any | None = None) -> dict[str, Any]:
    root = _root(restore_id)
    existing = _claim(root, restore_id)
    if existing is not None:
        return existing
    started = time.monotonic()
    started_at = _utc_iso()
    session = backup_remote_restore.read_restore_session(restore_id)
    assert session is not None
    known_work = _work(session, {}, {})
    result: dict[str, Any] | None = None
    failure: BaseException | None = None
    try:
        if str(session.get("storageProtocol") or "") == backup_object_set.OBJECT_SET_V1:
            backup_remote_restore.preflight_restore_session(restore_id, client=client)
        fetched = backup_remote_restore.fetch_restore_session(restore_id, client=client)
        if str(fetched.get("phase") or "") not in {"fetched", "chain-fetched", "components-fetched"}:
            raise AppError("Recovery Drill fetch did not reach a verified terminal phase", code=ErrorCode.INVALID_REQUEST, status=409)
        kind, secret = backup_crypto.consume_secret(restore_id)
        try:
            materialized = backup_remote_restore.materialize_restore_session(
                restore_id,
                kind=kind,
                secret=secret,
                client=client,
                _drill=True,
            )
        finally:
            secret[:] = b"\x00" * len(secret)
        session = backup_remote_restore.read_restore_session(restore_id) or session
        inspected = _inspect_materialized(restore_id, materialized, kind=kind)
        if inspected.get("compatible") is False:
            raise AppError("Recovery Drill contributor validation is incompatible", code=ErrorCode.INVALID_REQUEST, status=409)
        result = {
            "schemaVersion": SCHEMA_VERSION,
            "restoreId": restore_id,
            "result": "success",
            "startedAt": started_at,
            "completedAt": _utc_iso(),
            "durationMs": max(0, int((time.monotonic() - started) * 1_000)),
            **_work(session, materialized, inspected),
        }
    except Exception as exc:
        failure = exc
        result = {
            "schemaVersion": SCHEMA_VERSION,
            "restoreId": restore_id,
            "result": "failed",
            "failureCode": "drill-validation-failed",
            "startedAt": started_at,
            "completedAt": _utc_iso(),
            "durationMs": max(0, int((time.monotonic() - started) * 1_000)),
            **known_work,
        }
    finally:
        backup_crypto.clear_secret(restore_id)
        cleanup_failed = False
        try:
            _scrub_plaintext(root)
        except OSError:
            cleanup_failed = True
        cleanup_failed = cleanup_failed or _plaintext_remains(root)
        try:
            backup_remote_restore._release_session_holds(session)
        except Exception:
            cleanup_failed = True
        if cleanup_failed:
            result = {
                "schemaVersion": SCHEMA_VERSION,
                "restoreId": restore_id,
                "result": "failed",
                "failureCode": "drill-cleanup-failed",
                "startedAt": started_at,
                "completedAt": _utc_iso(),
                "durationMs": max(0, int((time.monotonic() - started) * 1_000)),
                **known_work,
            }
            if failure is None:
                failure = AppError("Recovery Drill cleanup failed", code=ErrorCode.INTERNAL, status=500)
        assert result is not None
        _atomic_write(root / RESULT_NAME, result)
        current = backup_remote_restore.read_restore_session(restore_id) or session
        current["drillOnly"] = True
        current["drillResult"] = str(result["result"])
        current["phase"] = "complete" if result["result"] == "success" else "failed"
        current["updatedAt"] = str(result["completedAt"])
        backup_remote_restore._atomic_write_json(root / "remote-fetch.json", current)
        (root / CLAIM_NAME).unlink(missing_ok=True)
    if failure is not None:
        raise AppError("Recovery Drill failed", code=ErrorCode.INVALID_REQUEST, status=409) from failure
    return result


def run_recovery_drill(restore_id: str, *, client: Any | None = None) -> dict[str, Any]:
    """Execute production recovery through validation without federated prepare."""
    root = _root(restore_id)
    with _exclusive_drill_lock(root):
        return _run_recovery_drill_locked(restore_id, client=client)


def get_recovery_drill(restore_id: str) -> dict[str, Any]:
    root = _root(restore_id)
    path = root / RESULT_NAME
    if not path.is_file():
        raise AppError("Recovery Drill result not found", code=ErrorCode.NOT_FOUND, status=404)
    return _read_json(path)

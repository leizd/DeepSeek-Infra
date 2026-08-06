from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_catalog, backup_crypto, backup_mirror, backup_policies, backup_scheduled, backup_scrub


UTC = timezone.utc
RECIPIENT_A = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"


@pytest.fixture
def stub_crypto(monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = b"age-encryption.org/v1\n"

    def encrypt_stream(target: Path, write_plaintext: object, *, mode: str, secret: object = None, recipients: tuple[str, ...] = (), cancel_event: object = None) -> None:
        import io

        buffer = io.BytesIO()
        write_plaintext(buffer)  # type: ignore[operator]
        target.write_bytes(prefix + bytes(buffer.getbuffer())[::-1])

    def decrypt_file(source: Path, target: Path, *, kind: str, secret: bytearray, cancel_event: object = None) -> None:
        raw = source.read_bytes()
        assert raw.startswith(prefix)
        target.write_bytes(raw[len(prefix):][::-1])

    monkeypatch.setattr(backup_crypto, "encrypt_stream", encrypt_stream)
    monkeypatch.setattr(backup_crypto, "decrypt_file", decrypt_file)
    monkeypatch.setattr(backup_crypto, "generate_identity", lambda: {"identity": "AGE-SECRET-KEY-1EPH", "recipient": "age1eph"})
    monkeypatch.setattr(backup_crypto, "inspect_header", lambda _path: {"age": True})


def _envelope() -> dict[str, object]:
    body: dict[str, object] = {
        "schemaVersion": 1,
        "sourceVersion": config.APP_VERSION,
        "createdAt": 1,
        "conversations": [{"conversationId": "c1", "headRevision": "r1", "checkpoint": {"messages": []}}],
        "conflicts": [],
    }
    body["digest"] = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return body


def _build(tmp_settings: Path, *, with_mirror: bool = True) -> tuple[Path, dict[str, object]]:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('{"items":[]}', encoding="utf-8")
    policy = backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": "nightly",
            "enabled": True,
            "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
            "scope": {"mode": "full", "includeExternalState": False, "coveragePolicy": "best-effort"},
            "protection": {"mode": "age-recipient", "recipients": [RECIPIENT_A]},
            "targetId": "managed-local",
        }
    )
    if with_mirror:
        backup_mirror.put_frontend_mirror("mirror_default", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A])
    package = backup_scheduled.build_scheduled_backup(policy, run_id="run_scrub", staging_root=tmp_settings / ".staging")
    root = tmp_settings / "target"
    for name in ("backups", "receipts", "catalog", ".partial", ".trash"):
        (root / name).mkdir(parents=True, exist_ok=True)
    final = root / "backups" / package.filename
    final.write_bytes(package.path.read_bytes())
    receipt = {
        "schemaVersion": 1,
        "backupId": package.backup_id,
        "runId": "run_scrub",
        "policyId": policy["policyId"],
        "targetId": "managed-local",
        "scheduleSlot": "slot",
        "filename": package.filename,
        "size": package.size,
        "ciphertextSha256": package.ciphertext_sha256,
        "manifestDigest": package.manifest_digest,
        "coverageDigest": package.coverage_digest,
        "creationVerified": package.creation_verified,
        "createdAt": "2026-06-01T00:00:00Z",
        "pinned": False,
    }
    backup_catalog.append_receipt(root, receipt)
    return root, {"backupId": package.backup_id, "filename": package.filename}


def test_scrub_ok(tmp_settings: Path, stub_crypto: None) -> None:
    root, info = _build(tmp_settings)
    result = backup_scrub.scrub_backup(root, str(info["backupId"]))
    assert result["ok"] is True
    assert set(result["checks"]) == {"exists", "not-symlink", "size", "sha256", "age-header"}
    record = backup_catalog.catalog_state(root)[str(info["backupId"])]
    assert record["scrubOk"] is True
    assert record["ciphertextScrubbedAt"]


def test_scrub_detects_tamper_and_missing(tmp_settings: Path, stub_crypto: None) -> None:
    root, info = _build(tmp_settings)
    path = root / "backups" / str(info["filename"])
    path.write_bytes(path.read_bytes() + b"x")
    result = backup_scrub.scrub_backup(root, str(info["backupId"]))
    assert result["ok"] is False
    assert "FAIL" in result["checks"]["size"] or "FAIL" in result["checks"]["sha256"]
    path.unlink()
    missing = backup_scrub.scrub_backup(root, str(info["backupId"]))
    assert missing["ok"] is False
    assert "FAIL" in missing["checks"]["exists"]
    with pytest.raises(AppError):
        backup_scrub.scrub_backup(root, "backup_missing")


def test_verify_unlock_drill_records_timestamp(tmp_settings: Path, stub_crypto: None) -> None:
    root, info = _build(tmp_settings)
    result = backup_scrub.verify_unlock_drill(root, str(info["backupId"]), bytearray(b"AGE-SECRET-KEY-1USER"), staged_root=tmp_settings / ".drill")
    assert result["ok"] is True
    assert result["sealedFrontend"] is not None
    assert result["sealedFrontend"]["conversations"] == 1
    record = backup_catalog.catalog_state(root)[str(info["backupId"])]
    assert record["userUnlockVerifiedAt"]
    assert not (tmp_settings / ".drill").exists() or not list((tmp_settings / ".drill").rglob("*.dsibackup"))


def test_verify_unlock_drill_rejects_missing_and_mismatched(tmp_settings: Path, stub_crypto: None) -> None:
    root, info = _build(tmp_settings)
    path = root / "backups" / str(info["filename"])
    path.write_bytes(b"age-encryption.org/v1\n" + b"garbage")
    with pytest.raises(AppError, match="receipt"):
        backup_scrub.verify_unlock_drill(root, str(info["backupId"]), bytearray(b"x"), staged_root=tmp_settings / ".drill")
    with pytest.raises(AppError):
        backup_scrub.verify_unlock_drill(root, "backup_missing", bytearray(b"x"), staged_root=tmp_settings / ".drill")


def test_backup_health_warning_and_error(tmp_settings: Path, stub_crypto: None) -> None:
    root, info = _build(tmp_settings)
    now = datetime(2026, 8, 1, tzinfo=UTC)
    health = backup_scrub.backup_health(root, now=now)
    assert health["status"] == "warning"
    assert health["backups"][0]["issues"] == ["unlock-verification-missing"]
    backup_scrub.verify_unlock_drill(root, str(info["backupId"]), bytearray(b"u"), staged_root=tmp_settings / ".drill")
    health = backup_scrub.backup_health(root, now=now)
    assert health["backups"][0]["issues"] == []
    path = root / "backups" / str(info["filename"])
    path.write_bytes(path.read_bytes() + b"x")
    backup_scrub.scrub_backup(root, str(info["backupId"]))
    health = backup_scrub.backup_health(root, now=now)
    assert health["status"] == "error"
    assert "scrub-failed" in health["backups"][0]["issues"]


def test_backup_health_overdue_verification(tmp_settings: Path, stub_crypto: None) -> None:
    root, info = _build(tmp_settings)
    backup_scrub.verify_unlock_drill(root, str(info["backupId"]), bytearray(b"u"), staged_root=tmp_settings / ".drill")
    future = datetime.now(tz=UTC) + timedelta(days=31)
    health = backup_scrub.backup_health(root, now=future)
    assert health["backups"][0]["issues"] == ["unlock-verification-overdue"]
    assert health["status"] == "warning"


def test_scrub_all(tmp_settings: Path, stub_crypto: None) -> None:
    root, info = _build(tmp_settings)
    result = backup_scrub.scrub_all(root)
    assert result["scrubbed"] == 1
    assert result["ok"] is True

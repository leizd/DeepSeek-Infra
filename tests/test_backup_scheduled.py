from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_crypto, backup_mirror, backup_policies, backup_scheduled, backups


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
    monkeypatch.setattr(backup_crypto, "capabilities", lambda: {"encryptedBackupAvailable": True, "formats": ["age-v1"], "protectionModes": ["passphrase", "age-recipient"]})


def _policy(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "name": "nightly",
        "enabled": True,
        "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
        "scope": {"mode": "full", "includeExternalState": False, "coveragePolicy": "best-effort"},
        "protection": {"mode": "age-recipient", "recipients": [RECIPIENT_A]},
        "targetId": "managed-local",
    }
    payload.update(overrides)
    return backup_policies.create_policy(payload)


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


def _fresh_ack() -> str:
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _seed_workspace(tmp_settings: Path) -> None:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('{"items":[{"id":"m1","text":"remember"}]}', encoding="utf-8")


def _decrypt(package: Path, target: Path) -> None:
    prefix = b"age-encryption.org/v1\n"
    raw = package.read_bytes()
    assert raw.startswith(prefix)
    target.write_bytes(raw[len(prefix):][::-1])


def test_scheduled_backup_includes_sealed_mirror(tmp_settings: Path, stub_crypto: None) -> None:
    _seed_workspace(tmp_settings)
    policy = _policy()
    backup_mirror.put_frontend_mirror("mirror_default", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at=_fresh_ack())
    package = backup_scheduled.build_scheduled_backup(policy, run_id="run_1", staging_root=tmp_settings / ".staging", schedule_slot="2026-01-02T03:00@UTC")
    assert package.path.is_file()
    assert package.filename.endswith(".dsibackup.age")
    assert package.creation_verified is True
    assert package.ciphertext_sha256 == hashlib.sha256(package.path.read_bytes()).hexdigest()
    assert package.frontend["status"] == "current"
    assert package.frontend["mode"] == "sealed-mirror"
    manifest = package.manifest
    assert manifest["scheduled"] == {"policyId": policy["policyId"], "scheduleSlot": "2026-01-02T03:00@UTC"}
    assert manifest["frontend"]["mode"] == "sealed-mirror"
    assert manifest["frontend"]["sourceEpoch"] == "epoch-1"
    assert manifest["coverage"]["frontend"]["status"] == "current"
    assert manifest["coverage"]["complete"] is True
    plain = tmp_settings / ".staging" / "plain.zip"
    _decrypt(package.path, plain)
    with zipfile.ZipFile(plain) as archive:
        names = set(archive.namelist())
        assert "frontend/sealed-state.age" in names
        assert "frontend/sealed-state.meta.json" in names
        assert "frontend/state.json" not in names
        inner = archive.read("frontend/sealed-state.age")
        assert inner.startswith(b"age-encryption.org/v1")
        assert b"c1" not in inner


def test_scheduled_backup_best_effort_without_mirror(tmp_settings: Path, stub_crypto: None) -> None:
    _seed_workspace(tmp_settings)
    policy = _policy()
    package = backup_scheduled.build_scheduled_backup(policy, run_id="run_2", staging_root=tmp_settings / ".staging")
    assert package.frontend["status"] == "missing"
    assert package.coverage["complete"] is False
    assert "frontend" not in package.manifest
    assert package.creation_verified is True


def test_scheduled_backup_required_mirror_blocks(tmp_settings: Path, stub_crypto: None) -> None:
    _seed_workspace(tmp_settings)
    policy = _policy(frontendMirror={"mode": "required", "maxAgeSeconds": 3600})
    with pytest.raises(AppError) as blocked:
        backup_scheduled.build_scheduled_backup(policy, run_id="run_3", staging_root=tmp_settings / ".staging")
    assert blocked.value.status == 409
    assert "blocked-frontend-mirror" in str(blocked.value)


def test_scheduled_backup_excluded_mirror(tmp_settings: Path, stub_crypto: None) -> None:
    _seed_workspace(tmp_settings)
    policy = _policy(frontendMirror={"mode": "excluded"})
    package = backup_scheduled.build_scheduled_backup(policy, run_id="run_4", staging_root=tmp_settings / ".staging")
    assert package.frontend == {"mode": "excluded", "status": "excluded"}
    assert package.coverage["complete"] is True


def test_scheduled_backup_stale_mirror_blocks_strict(tmp_settings: Path, stub_crypto: None) -> None:
    _seed_workspace(tmp_settings)
    backup_mirror.put_frontend_mirror("mirror_default", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2020-01-01T00:00:00Z")
    policy = _policy(frontendMirror={"mode": "required", "maxAgeSeconds": 60})
    with pytest.raises(AppError, match="blocked-frontend-mirror: stale"):
        backup_scheduled.build_scheduled_backup(policy, run_id="run_5", staging_root=tmp_settings / ".staging")


def test_scheduled_backup_restore_unlocks_sealed_frontend(tmp_settings: Path, stub_crypto: None) -> None:
    _seed_workspace(tmp_settings)
    policy = _policy()
    backup_mirror.put_frontend_mirror("mirror_default", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at=_fresh_ack())
    package = backup_scheduled.build_scheduled_backup(policy, run_id="run_6", staging_root=tmp_settings / ".staging")
    locked = backups.inspect_archive(package.path, filename=package.filename)
    assert locked["phase"] == "locked"
    backup_crypto.put_secret_bytes(locked["restoreId"], "age-identity", bytearray(b"AGE-SECRET-KEY-1USER"))
    unlocked = backups.unlock_restore(locked["restoreId"])
    assert unlocked["ok"] is True
    sealed = unlocked.get("sealedFrontend")
    assert sealed is not None
    assert sealed["sourceEpoch"] == "epoch-1"
    assert sealed["conversations"] == 1
    state = backups.get_restore(locked["restoreId"])
    frontend = state.get("frontend")
    assert frontend is not None
    assert frontend["conversations"][0]["conversationId"] == "c1"


def test_scheduled_backup_requires_recipients(tmp_settings: Path, stub_crypto: None) -> None:
    _seed_workspace(tmp_settings)
    policy = _policy()
    policy["protection"]["recipients"] = []  # type: ignore[index]
    with pytest.raises(AppError, match="no recipients"):
        backup_scheduled.build_scheduled_backup(policy, run_id="run_7", staging_root=tmp_settings / ".staging")

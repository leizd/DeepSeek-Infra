from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_publish, backup_scheduler, backup_targets


@pytest.fixture(autouse=True)
def _tempdir_outside_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / ".sys-temp"
    fake_temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


class _Package:
    def __init__(self, path: Path, payload: bytes) -> None:
        self.path = path
        self.backup_id = "backup_test1"
        self.filename = "deepseek-infra-backup-20260101-test1234.dsibackup.age"
        self.size = len(payload)
        self.ciphertext_sha256 = hashlib.sha256(payload).hexdigest()
        self.manifest_digest = "a" * 64
        self.coverage_digest = "b" * 64
        self.creation_verified = True


def _package(tmp_path: Path, payload: bytes = b"ciphertext-bytes") -> _Package:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    path = staging / "package.age"
    path.write_bytes(payload)
    return _Package(path, payload)


def test_publish_managed_local_layout(tmp_settings: Path, tmp_path: Path) -> None:
    package = _package(tmp_path)
    target = backup_publish.resolve_target("managed-local")
    result = backup_publish.publish_backup(target, package, run_id="run_1", policy_id="policy_1", schedule_slot="slot", fencing_token=1)
    root = backup_publish.backups.BACKUP_DIR
    assert result.path == backup_publish.object_path(root, package.ciphertext_sha256)
    assert result.path.read_bytes() == b"ciphertext-bytes"
    assert result.converged is False
    receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
    assert result.receipt_path == root / "receipts" / "backup_test1.json"
    assert receipt["schemaVersion"] == 2
    assert receipt["backupId"] == "backup_test1"
    assert receipt["ciphertextSha256"] == package.ciphertext_sha256
    assert receipt["objectDigest"] == package.ciphertext_sha256
    assert receipt["pinned"] is False
    marker_path = backup_publish.commit_marker_path(root, "policy_1", "slot")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["schemaVersion"] >= 2
    assert marker["runId"] == "run_1"
    assert marker["fencingToken"] == 1
    assert marker["backupId"] == "backup_test1"
    assert marker["objectDigest"] == package.ciphertext_sha256
    assert marker["targetGeneration"] == 1
    assert marker["previousCommitHash"] == backup_publish.GENESIS_COMMIT_HASH
    journal = backup_publish.read_journal(root, "run_1")
    assert journal is not None
    assert journal["phase"] == "committed"
    assert journal["commitHash"] == marker["commitHash"]
    assert not list((root / ".partial").iterdir())
    health = backup_scheduler.target_health()
    assert health and health[0]["status"] == "ok"


def test_publish_filesystem_target(tmp_settings: Path, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    package = _package(tmp_path)
    target = backup_publish.resolve_target(record["targetId"])
    result = backup_publish.publish_backup(target, package, run_id="run_2", policy_id="policy_1", schedule_slot="slot", fencing_token=2)
    assert result.path.is_file()
    assert (directory / "receipts" / "backup_test1.json").is_file()
    assert backup_publish.commit_marker_path(directory, "policy_1", "slot").is_file()
    for name in backup_publish.LAYOUT_DIRS:
        assert (directory / name).is_dir()


def test_publish_rejects_digest_mismatch(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = _package(tmp_path)
    target = backup_publish.resolve_target("managed-local")
    monkeypatch.setattr(backup_publish.backup_unattended, "sha256_file", lambda _path, **_kwargs: "0" * 64)
    with pytest.raises(AppError, match="digest mismatch"):
        backup_publish.publish_backup(target, package, run_id="run_3", policy_id="policy_1", schedule_slot="slot", fencing_token=3)
    root = backup_publish.backups.BACKUP_DIR
    assert not backup_publish.object_path(root, package.ciphertext_sha256).exists()
    assert not backup_publish.commit_marker_path(root, "policy_1", "slot").exists()
    assert not list((root / ".partial").iterdir())


def test_resolve_blocked_filesystem_target(tmp_settings: Path, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    (directory / backup_targets.TARGET_MARKER_NAME).unlink()
    with pytest.raises(AppError, match="blocked-target-unavailable"):
        backup_publish.resolve_target(record["targetId"])


def test_cleanup_partial(tmp_settings: Path, tmp_path: Path) -> None:
    target = backup_publish.resolve_target("managed-local")
    (target.root / ".partial").mkdir(exist_ok=True)
    partial = target.root / ".partial" / "run_x.part"
    partial.write_bytes(b"junk")
    backup_publish.cleanup_partial(target.root, "run_x")
    assert not partial.exists()

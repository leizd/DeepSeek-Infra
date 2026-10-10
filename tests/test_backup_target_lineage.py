from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_publish, backup_retention, backup_targets


NOW = datetime(2026, 6, 2, 4, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _tempdir_outside_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / ".sys-temp"
    fake_temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def _marker_path(directory: Path) -> Path:
    return directory / backup_targets.TARGET_MARKER_NAME


def _read_marker(directory: Path) -> dict[str, object]:
    return json.loads(_marker_path(directory).read_text(encoding="utf-8"))


def _write_marker(directory: Path, **updates: object) -> None:
    marker = _read_marker(directory)
    marker.update(updates)
    _marker_path(directory).write_text(json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_marker_v2_written_at_init(tmp_settings: Path, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    marker = _read_marker(directory)
    assert int(str(marker["schemaVersion"])) >= 2
    assert str(marker["incarnationId"]).startswith("inc_")
    assert marker["ownerInstallationId"]
    assert marker["targetGeneration"] == 0
    assert marker["latestCommitHash"] == backup_targets.TARGET_GENESIS_HASH
    checkpoint = backup_targets._read_checkpoint(str(record["targetId"]))
    assert checkpoint is not None
    assert checkpoint["incarnationId"] == marker["incarnationId"]
    assert checkpoint["lastSeenGeneration"] == 0


def test_rollback_detected_and_adopted(tmp_settings: Path, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    target_id = str(record["targetId"])
    assert backup_targets.verify_target_ready(target_id)
    _write_marker(directory, targetGeneration=5, latestCommitHash="a" * 64)
    assert backup_targets.verify_target_ready(target_id)
    _write_marker(directory, targetGeneration=2, latestCommitHash="b" * 64)
    with pytest.raises(AppError) as exc:
        backup_targets.verify_target_ready(target_id)
    assert "target-rollback-detected" in str(exc.value)
    probe = backup_targets.probe_target(target_id)
    assert probe["status"] == "target-rollback-detected"
    assert probe["ready"] is False
    read_only = backup_targets.verify_target_ready(target_id, write_intent=False)
    assert read_only == directory.resolve()
    adopted = backup_targets.adopt_target_incarnation(target_id)
    assert adopted["adopted"] is True
    assert backup_targets.verify_target_ready(target_id)
    checkpoint = backup_targets._read_checkpoint(target_id)
    assert checkpoint is not None
    assert checkpoint["lastSeenGeneration"] == 2
    assert checkpoint["incarnationId"] == adopted["incarnationId"]


def test_fork_detected_on_same_generation_different_head(tmp_settings: Path, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    target_id = str(record["targetId"])
    _write_marker(directory, targetGeneration=3, latestCommitHash="a" * 64)
    assert backup_targets.verify_target_ready(target_id)
    _write_marker(directory, targetGeneration=3, latestCommitHash="b" * 64)
    with pytest.raises(AppError) as exc:
        backup_targets.verify_target_ready(target_id)
    assert "target-fork-detected" in str(exc.value)


def test_fork_detected_on_incarnation_change(tmp_settings: Path, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    target_id = str(record["targetId"])
    _write_marker(directory, targetGeneration=4, latestCommitHash="a" * 64)
    assert backup_targets.verify_target_ready(target_id)
    _write_marker(directory, incarnationId="inc_foreign12345678", targetGeneration=5, latestCommitHash="b" * 64)
    with pytest.raises(AppError) as exc:
        backup_targets.verify_target_ready(target_id)
    assert "target-fork-detected" in str(exc.value)


def test_clone_detected_at_second_location(tmp_settings: Path, tmp_path: Path) -> None:
    first = tmp_path / "disk-a"
    first.mkdir()
    record = backup_targets.init_target(first)
    clone = tmp_path / "disk-b"
    clone.mkdir()
    (clone / backup_targets.TARGET_MARKER_NAME).write_bytes(_marker_path(first).read_bytes())
    with pytest.raises(AppError) as exc:
        backup_targets.init_target(clone)
    assert "target-clone-detected" in str(exc.value)
    fresh = backup_targets.reinitialize_target(clone, label="clone-as-new")
    assert fresh["targetId"] != record["targetId"]
    marker = _read_marker(clone)
    assert marker["targetId"] == fresh["targetId"]
    assert int(str(marker["schemaVersion"])) >= 2
    assert marker["targetGeneration"] == 0


def test_moved_disk_reregisters_when_old_path_gone(tmp_settings: Path, tmp_path: Path) -> None:
    first = tmp_path / "disk-a"
    first.mkdir()
    record = backup_targets.init_target(first)
    marker_bytes = _marker_path(first).read_bytes()
    import shutil

    shutil.rmtree(first)
    moved = tmp_path / "disk-b"
    moved.mkdir()
    (moved / backup_targets.TARGET_MARKER_NAME).write_bytes(marker_bytes)
    again = backup_targets.init_target(moved)
    assert again["targetId"] == record["targetId"]
    assert Path(str(again["path"])) == moved.resolve()


def test_v1_marker_upgraded_on_write_contact(tmp_settings: Path, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    target_id = str(record["targetId"])
    _marker_path(directory).write_text(json.dumps({"schemaVersion": 1, "targetId": target_id, "targetNonce": record["targetNonce"], "createdAt": "2026-01-01T00:00:00Z"}), encoding="utf-8")
    backup_targets._checkpoint_path(target_id).unlink(missing_ok=True)
    assert backup_targets.verify_target_ready(target_id)
    marker = _read_marker(directory)
    assert int(str(marker["schemaVersion"])) >= 2
    assert str(marker["incarnationId"]).startswith("inc_")
    assert marker["targetGeneration"] == 0
    assert backup_targets.verify_target_ready(target_id)


def test_record_target_head_advances_checkpoint(tmp_settings: Path, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    target_id = str(record["targetId"])
    backup_targets.record_target_head(directory, target_id=target_id, generation=1, commit_hash="c" * 64)
    marker = _read_marker(directory)
    assert marker["targetGeneration"] == 1
    assert marker["latestCommitHash"] == "c" * 64
    checkpoint = backup_targets._read_checkpoint(target_id)
    assert checkpoint is not None
    assert checkpoint["lastSeenGeneration"] == 1
    assert backup_targets.verify_target_ready(target_id)


def test_publish_advances_marker_and_retention_stays_blocked_on_anomaly(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    target_id = str(record["targetId"])

    class _Package:
        def __init__(self, path: Path, payload: bytes) -> None:
            self.path = path
            self.backup_id = "backup_lineage"
            self.filename = "lineage.dsibackup.age"
            self.size = len(payload)
            self.ciphertext_sha256 = "d" * 64
            self.manifest_digest = "a" * 64
            self.coverage_digest = "b" * 64
            self.creation_verified = True

    import hashlib

    payload = b"lineage-ciphertext"
    staging = tmp_path / "pkg"
    staging.mkdir()
    package = _Package(staging / "pkg.age", payload)
    package.path.write_bytes(payload)
    package.ciphertext_sha256 = hashlib.sha256(payload).hexdigest()
    target = backup_publish.resolve_target(target_id)
    result = backup_publish.publish_backup(target, package, run_id="run_l1", policy_id="policy_1", schedule_slot="slot", fencing_token=1)
    marker = _read_marker(directory)
    assert marker["targetGeneration"] == result.commit["targetGeneration"] == 1
    assert marker["latestCommitHash"] == result.commit["commitHash"]
    _write_marker(directory, targetGeneration=0, latestCommitHash=backup_targets.TARGET_GENESIS_HASH)
    with pytest.raises(AppError, match="target-rollback-detected"):
        backup_publish.resolve_target(target_id)
    with pytest.raises(AppError, match="target-rollback-detected"):
        backup_publish.resolve_target(target_id, write_intent=True)
    backup_targets.adopt_target_incarnation(target_id)
    retention = backup_retention.normalize_retention_policy({})
    applied = backup_retention.apply_retention(retention, directory)
    assert applied["trashed"] == []


def test_adopt_requires_valid_marker(tmp_settings: Path, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    target_id = str(record["targetId"])
    _marker_path(directory).unlink()
    with pytest.raises(AppError, match="marker is missing"):
        backup_targets.adopt_target_incarnation(target_id)
    _marker_path(directory).write_text("{nope", encoding="utf-8")
    with pytest.raises(AppError, match="unreadable"):
        backup_targets.adopt_target_incarnation(target_id)
    _marker_path(directory).write_text(json.dumps({"schemaVersion": 2, "targetId": target_id, "targetNonce": "wrong-nonce"}), encoding="utf-8")
    with pytest.raises(AppError, match="replaced"):
        backup_targets.adopt_target_incarnation(target_id)


def test_record_target_head_tolerates_marker_problems(tmp_settings: Path, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    target_id = str(record["targetId"])
    _marker_path(directory).unlink()
    backup_targets.record_target_head(directory, target_id=target_id, generation=9, commit_hash="e" * 64)
    assert not _marker_path(directory).exists()
    _marker_path(directory).write_text("{nope", encoding="utf-8")
    backup_targets.record_target_head(directory, target_id=target_id, generation=9, commit_hash="e" * 64)
    _marker_path(directory).write_text("[]", encoding="utf-8")
    backup_targets.record_target_head(directory, target_id=target_id, generation=9, commit_hash="e" * 64)


def test_first_contact_writes_checkpoint(tmp_settings: Path, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    target_id = str(record["targetId"])
    backup_targets._checkpoint_path(target_id).unlink()
    assert backup_targets.verify_target_ready(target_id)
    checkpoint = backup_targets._read_checkpoint(target_id)
    assert checkpoint is not None
    assert checkpoint["incarnationId"] == _read_marker(directory)["incarnationId"]


def test_reinitialize_edge_paths(tmp_settings: Path, tmp_path: Path) -> None:
    first = tmp_path / "disk-a"
    first.mkdir()
    backup_targets.init_target(first)
    clone = tmp_path / "disk-b"
    clone.mkdir()
    (clone / backup_targets.TARGET_MARKER_NAME).write_text("{nope", encoding="utf-8")
    fresh = backup_targets.reinitialize_target(clone)
    assert _read_marker(clone)["targetId"] == fresh["targetId"]
    inside = first / "nested"
    inside.mkdir()
    with pytest.raises(AppError, match="Unsafe backup target"):
        backup_targets.reinitialize_target(inside)

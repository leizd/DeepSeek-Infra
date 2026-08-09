from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_catalog, backup_retention, backup_scheduler, backups


UTC = timezone.utc


def _policy(**overrides: object) -> dict[str, object]:
    return backup_retention.normalize_retention_policy(overrides)


def _add_backup(root: Path, backup_id: str, *, created: str, size: int = 100, pinned: bool = False, **extra: object) -> None:
    (root / "backups").mkdir(parents=True, exist_ok=True)
    filename = f"{backup_id}.dsibackup.age"
    (root / "backups" / filename).write_bytes(b"x" * size)
    backup_catalog.append_receipt(
        root,
        {
            "schemaVersion": 1,
            "backupId": backup_id,
            "runId": f"run_{backup_id}",
            "policyId": "policy_1",
            "targetId": "managed-local",
            "scheduleSlot": "slot",
            "filename": filename,
            "size": size,
            "ciphertextSha256": "a" * 64,
            "manifestDigest": "b" * 64,
            "coverageDigest": "c" * 64,
            "creationVerified": True,
            "createdAt": created,
            "pinned": pinned,
            **extra,
        },
    )
    if pinned:
        backup_catalog.pin_backup(root, backup_id, True)


def test_retention_policy_crud_and_validation(tmp_settings: Path) -> None:
    default = backup_retention.get_retention_policy("default")
    assert default["keepLast"] == 3
    assert default["trashGraceHours"] == 24
    policy = backup_retention.put_retention_policy("weekly-plus", {"keepLast": 5, "keepWeekly": 12, "maxAgeDays": 90})
    assert policy["keepLast"] == 5
    assert policy["maxAgeDays"] == 90
    loaded = backup_retention.get_retention_policy("weekly-plus")
    assert loaded["keepWeekly"] == 12
    ids = [item["retentionPolicyId"] for item in backup_retention.list_retention_policies()]
    assert "default" in ids and "weekly-plus" in ids
    with pytest.raises(AppError):
        backup_retention.put_retention_policy("bad id!", {})
    with pytest.raises(AppError):
        backup_retention.put_retention_policy("ok", {"keepLast": -1})
    with pytest.raises(AppError):
        backup_retention.put_retention_policy("ok", {"maxAgeDays": 0})
    with pytest.raises(AppError):
        backup_retention.get_retention_policy("missing")


def test_gfs_buckets_in_policy_timezone(tmp_settings: Path, tmp_path: Path) -> None:
    root = tmp_path / "target"
    now = datetime(2026, 6, 15, 4, 30, tzinfo=UTC)
    # 2026-06-14 23:30 UTC is 2026-06-14 19:30 in New York (same local day) but
    # a different UTC day; daily bucketing must use the policy timezone.
    _add_backup(root, "backup_ny_evening", created="2026-06-14T23:30:00Z")
    _add_backup(root, "backup_old", created="2026-05-01T12:00:00Z")
    policy = _policy(keepLast=0, keepHourly=0, keepDaily=1, keepWeekly=0, keepMonthly=0, minimumHealthyCopies=1)
    preview = backup_retention.preview_retention(policy, root, policy_timezone="America/New_York", now=now)
    assert "backup_ny_evening" in preview["keep"]
    assert "backup_old" in preview["trash"]


def test_gfs_keeps_recent_buckets_deterministically(tmp_settings: Path, tmp_path: Path) -> None:
    root = tmp_path / "target"
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    base = now - timedelta(hours=1)
    for index in range(10):
        created = (base - timedelta(hours=index)).isoformat(timespec="seconds").replace("+00:00", "Z")
        _add_backup(root, f"backup_h{index}", created=created)
    policy = _policy(keepLast=2, keepHourly=4, keepDaily=0, keepWeekly=0, keepMonthly=0, minimumHealthyCopies=1)
    preview = backup_retention.preview_retention(policy, root, policy_timezone="UTC", now=now)
    keep = set(preview["keep"])
    assert {"backup_h0", "backup_h1", "backup_h2"} <= keep
    assert "backup_h3" in preview["trash"]
    assert "backup_h9" in preview["trash"]
    again = backup_retention.preview_retention(policy, root, policy_timezone="UTC", now=now)
    assert again["keep"] == preview["keep"]


def test_pinned_latest_and_minimum_healthy_protected(tmp_settings: Path, tmp_path: Path) -> None:
    root = tmp_path / "target"
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    _add_backup(root, "backup_pinned", created="2026-01-01T00:00:00Z", pinned=True)
    _add_backup(root, "backup_mid", created="2026-06-10T00:00:00Z")
    _add_backup(root, "backup_latest", created="2026-06-15T00:00:00Z")
    policy = _policy(keepLast=0, keepHourly=0, keepDaily=0, keepWeekly=0, keepMonthly=0, minimumHealthyCopies=2)
    preview = backup_retention.preview_retention(policy, root, now=now)
    reasons = {item["backupId"]: item["reason"] for item in preview["protected"]}
    assert reasons["backup_pinned"] == "pinned"
    assert reasons["backup_latest"] == "latest-successful-backup"
    assert preview["trash"] == []


def test_restore_referenced_backup_protected(tmp_settings: Path, tmp_path: Path) -> None:
    root = tmp_path / "target"
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    _add_backup(root, "backup_ref", created="2026-01-01T00:00:00Z")
    _add_backup(root, "backup_new", created="2026-06-15T00:00:00Z")
    restore = backups.RESTORE_DIR / "restore_abc"
    restore.mkdir(parents=True)
    (restore / "upload.json").write_text(json.dumps({"filename": "backup_ref.dsibackup.age", "ciphertextSha256": "a" * 64}), encoding="utf-8")
    policy = _policy(keepLast=0, keepHourly=0, keepDaily=0, keepWeekly=0, keepMonthly=0, minimumHealthyCopies=1)
    preview = backup_retention.preview_retention(policy, root, now=now)
    reasons = {item["backupId"]: item["reason"] for item in preview["protected"]}
    assert reasons.get("backup_ref") == "restore-referenced"


def test_apply_and_finalize_two_phase_delete(tmp_settings: Path, tmp_path: Path) -> None:
    root = tmp_path / "target"
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    _add_backup(root, "backup_old", created="2026-01-01T00:00:00Z")
    _add_backup(root, "backup_new", created="2026-06-15T00:00:00Z")
    policy = _policy(keepLast=1, keepHourly=0, keepDaily=0, keepWeekly=0, keepMonthly=0, minimumHealthyCopies=1, trashGraceHours=24)
    applied = backup_retention.apply_retention(policy, root, now=now)
    assert applied["trashed"] == ["backup_old"]
    assert not (root / "backups" / "backup_old.dsibackup.age").exists()
    assert (root / ".trash" / "backup_old" / "backup_old.dsibackup.age").is_file()
    state = backup_catalog.catalog_state(root)
    assert state["backup_old"]["trashed"] is True
    early = backup_retention.finalize_retention(policy, root, now=now + timedelta(hours=2))
    assert early["deleted"] == []
    restored = backup_retention.restore_from_trash(root, "backup_old")
    assert restored["restored"] is True
    assert (root / "backups" / "backup_old.dsibackup.age").is_file()
    reapplied = backup_retention.apply_retention(policy, root, now=now + timedelta(hours=3))
    assert reapplied["trashed"] == ["backup_old"]
    late = backup_retention.finalize_retention(policy, root, now=now + timedelta(hours=30))
    assert late["deleted"] == ["backup_old"]
    assert not (root / ".trash" / "backup_old").exists()
    final_state = backup_catalog.catalog_state(root)
    assert final_state["backup_old"]["deleted"] is True


def test_protected_backup_returns_from_trash_at_finalize(tmp_settings: Path, tmp_path: Path) -> None:
    root = tmp_path / "target"
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    _add_backup(root, "backup_old", created="2026-01-01T00:00:00Z")
    _add_backup(root, "backup_new", created="2026-06-15T00:00:00Z")
    policy = _policy(keepLast=1, keepHourly=0, keepDaily=0, keepWeekly=0, keepMonthly=0, minimumHealthyCopies=1)
    applied = backup_retention.apply_retention(policy, root, now=now)
    assert applied["trashed"] == ["backup_old"]
    backup_catalog.pin_backup(root, "backup_old", True)
    result = backup_retention.finalize_retention(policy, root, now=now + timedelta(days=2))
    assert result["deleted"] == []
    assert (root / "backups" / "backup_old.dsibackup.age").is_file()
    assert backup_catalog.catalog_state(root)["backup_old"]["trashed"] is False


def test_restore_from_trash_requires_trashed_backup(tmp_settings: Path, tmp_path: Path) -> None:
    root = tmp_path / "target"
    _add_backup(root, "backup_a", created="2026-06-15T00:00:00Z")
    with pytest.raises(AppError):
        backup_retention.restore_from_trash(root, "backup_a")


def test_active_run_backup_protected(tmp_settings: Path, tmp_path: Path) -> None:
    root = tmp_path / "target"
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    _add_backup(root, "backup_busy", created="2026-01-01T00:00:00Z")
    _add_backup(root, "backup_new", created="2026-06-15T00:00:00Z")
    with backup_scheduler._connect() as connection:
        connection.execute(
            "INSERT INTO backup_runs(run_id, policy_id, schedule_slot, phase, attempt, backup_id, created_at, updated_at) VALUES ('run_active', 'p', 's', 'publishing', 1, 'backup_busy', '2026-06-15T00:00:00Z', '2026-06-15T00:00:00Z')"
        )
    policy = _policy(keepLast=0, keepHourly=0, keepDaily=0, keepWeekly=0, keepMonthly=0, minimumHealthyCopies=1)
    preview = backup_retention.preview_retention(policy, root, now=now)
    reasons = {item["backupId"]: item["reason"] for item in preview["protected"]}
    assert reasons.get("backup_busy") == "active-run"


def _add_incremental(root: Path, backup_id: str, created: str, parent: str | None) -> None:
    _add_backup(
        root,
        backup_id,
        created=created,
        snapshotKind="full" if parent is None else "incremental",
        parentBackupId=parent,
        baseBackupId="F0",
    )


def test_trashed_descendant_protects_ancestors(tmp_settings: Path, tmp_path: Path) -> None:
    """A trashed-but-recoverable incremental descendant keeps its ancestors."""
    root = tmp_path / "target"
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    _add_incremental(root, "F0", "2026-01-01T00:00:00Z", None)
    _add_incremental(root, "I1", "2026-02-01T00:00:00Z", "F0")
    _add_incremental(root, "I2", "2026-06-15T10:00:00Z", "I1")
    # I1 is trashed but still inside trash grace. I2 (kept) needs I1 which needs
    # F0, so the ancestor chain must walk through the trashed intermediate.
    backup_catalog.record_trash(root, "I1", retention_run_id="rr", at="2026-06-15T11:00:00Z")
    policy = _policy(keepLast=0, keepHourly=0, keepDaily=0, keepWeekly=0, keepMonthly=0, minimumHealthyCopies=1)
    preview = backup_retention.preview_retention(policy, root, now=now)
    assert "I2" in preview["keep"]
    assert "F0" in preview["keep"]
    reasons = {item["backupId"]: item["reason"] for item in preview["protected"]}
    assert reasons.get("F0") == "ancestor-of-kept-snapshot"
    assert reasons.get("I1") == "ancestor-of-kept-snapshot"
    # A trashed descendant itself also protects its ancestors while in grace.
    _add_incremental(root, "I3", "2026-06-15T09:00:00Z", "I2")
    backup_catalog.record_trash(root, "I3", retention_run_id="rr", at="2026-06-15T11:30:00Z")
    preview2 = backup_retention.preview_retention(policy, root, now=now)
    assert "F0" in preview2["keep"]


def test_grace_expired_trash_releases_ancestors(tmp_settings: Path, tmp_path: Path) -> None:
    """A grace-expired trashed descendant no longer protects its ancestors."""
    root = tmp_path / "target"
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    _add_incremental(root, "F0", "2026-01-01T00:00:00Z", None)
    _add_incremental(root, "I1", "2026-02-01T00:00:00Z", "F0")
    _add_incremental(root, "I2", "2026-03-01T00:00:00Z", "I1")
    _add_incremental(root, "I3", "2026-06-14T00:00:00Z", "I2")
    backup_catalog.record_trash(root, "I3", retention_run_id="rr", at="2026-06-10T00:00:00Z")
    backup_catalog.record_trash(root, "I2", retention_run_id="rr", at="2026-06-10T00:00:00Z")
    backup_catalog.record_trash(root, "I1", retention_run_id="rr", at="2026-06-10T00:00:00Z")
    policy = _policy(keepLast=0, keepHourly=0, keepDaily=0, keepWeekly=0, keepMonthly=0, minimumHealthyCopies=1, trashGraceHours=24)
    preview = backup_retention.preview_retention(policy, root, now=now)
    reasons = {item["backupId"]: item["reason"] for item in preview["protected"]}
    # Every descendant is trashed past grace, so none of them protects the
    # ancestors; F0 only stays as the latest visible snapshot.
    assert reasons.get("F0") == "latest-successful-backup"
    assert reasons.get("I2") != "ancestor-of-kept-snapshot"
    assert "I2" not in preview["keep"]

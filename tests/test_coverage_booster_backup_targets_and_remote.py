"""Targeted test coverage boosters for backup targets and reconcile."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_reconcile,
    backup_targets,
)


@pytest.fixture(autouse=True)
def _tempdir_outside_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / ".sys-temp"
    fake_temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def test_backup_targets_lifecycle(tmp_settings: Path, tmp_path: Path) -> None:
    target_dir = tmp_path / "external_disk_target"
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. Initialize target
    rec = backup_targets.init_target(target_dir, label="USB Disk")
    t_id = rec["targetId"]
    assert rec["label"] == "USB Disk"
    assert rec["kind"] == "filesystem"

    # 2. Get target
    fetched = backup_targets.get_target(t_id)
    assert fetched["targetId"] == t_id

    # 3. List targets
    targets = backup_targets.list_targets()
    assert any(t["targetId"] == t_id for t in targets)

    # 4. Re-init existing target (should re-register)
    re_rec = backup_targets.init_target(target_dir, label="Updated USB Disk")
    assert re_rec["targetId"] == t_id

    # 5. Delete target registration
    del_res = backup_targets.delete_target(t_id)
    assert del_res.get("deleted") is True
    assert any(t["targetId"] == t_id for t in backup_targets.list_targets()) is False


def test_backup_reconcile_catalog_checks(tmp_settings: Path, tmp_path: Path) -> None:
    target_dir = tmp_path / "reconcile_test_target"
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. Uncorrupted empty target
    assert backup_reconcile.catalog_corrupt_backup_ids(target_dir) == []
    backup_reconcile.assert_catalog_committed(target_dir)

    # 2. Add a receipt without commit marker
    backup_catalog.append_receipt(
        target_dir,
        {
            "schemaVersion": 2,
            "backupId": "bk_uncommitted_1",
            "createdAt": "2026-08-15T00:00:00Z",
            "filename": "bk_uncommitted_1.zip",
        },
    )

    corrupt = backup_reconcile.catalog_corrupt_backup_ids(target_dir)
    assert "bk_uncommitted_1" in corrupt

    with pytest.raises(AppError) as exc_info:
        backup_reconcile.assert_catalog_committed(target_dir)
    assert "catalog-corrupt" in str(exc_info.value)

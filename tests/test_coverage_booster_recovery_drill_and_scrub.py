"""Targeted test coverage boosters for backup_recovery_drill and plaintext scrubbing."""

from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_recovery_drill, backups


def test_recovery_drill_helpers_and_scrub(tmp_settings: Path) -> None:
    # 1. Invalid restore IDs
    with pytest.raises(AppError):
        backup_recovery_drill._root("invalid_id")

    with pytest.raises(AppError):
        backup_recovery_drill._root("restore_not_existing_123")

    # 2. Setup mock restore dir
    r_dir = backups.RESTORE_DIR / "restore_test123"
    r_dir.mkdir(parents=True, exist_ok=True)
    assert backup_recovery_drill._root("restore_test123") == r_dir

    # 3. Read/write JSON helpers
    json_path = r_dir / "test.json"
    backup_recovery_drill._atomic_write(json_path, {"status": "ok", "count": 42})
    data = backup_recovery_drill._read_json(json_path)
    assert data["status"] == "ok"
    assert data["count"] == 42

    with pytest.raises(AppError):
        (r_dir / "bad.json").write_text("invalid json", encoding="utf-8")
        backup_recovery_drill._read_json(r_dir / "bad.json")

    # 4. Plaintext scrubbing and detection
    extracted_dir = r_dir / "extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    sample_file = extracted_dir / "sample.txt"
    sample_file.write_text("plaintext content", encoding="utf-8")

    plan_file = r_dir / "plan.json"
    plan_file.write_text("{}", encoding="utf-8")

    assert backup_recovery_drill._plaintext_remains(r_dir) is True

    backup_recovery_drill._scrub_plaintext(r_dir)
    assert backup_recovery_drill._plaintext_remains(r_dir) is False

    # 5. Exclusive drill lock context manager
    with backup_recovery_drill._exclusive_drill_lock(r_dir):
        pass

    # 6. Work estimation
    session = {
        "snapshotKind": "full",
        "backupId": "b1",
        "expectedBytes": 700,
    }
    materialized = {
        "manifest": {
            "files": [{"size": 1000}],
            "contributors": [{"name": "db"}],
        }
    }
    inspected = {"operations": [{"op": "check"}]}
    work_res = backup_recovery_drill._work(session, materialized, inspected)
    assert work_res["chainLength"] == 1
    assert work_res["ciphertextBytes"] == 700
    assert work_res["logicalBytes"] == 1000
    assert work_res["verifiedContributors"] == 1

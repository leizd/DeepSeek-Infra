"""Targeted test coverage boosters for backup target publication crash reconciliation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from deepseek_infra.infra.workspace import backup_reconcile, backup_writer_lease


def test_backup_reconcile_helpers(tmp_settings: Path) -> None:
    # 1. UTC ISO helper
    now_dt = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
    assert backup_reconcile._utc_iso(now_dt) == "2026-08-15T12:00:00Z"

    # 2. Receipt files and catalog corrupt checks on empty root
    target_root = tmp_settings / "test_target"
    target_root.mkdir(parents=True, exist_ok=True)

    receipts = backup_reconcile._receipt_files(target_root)
    assert receipts == {}

    corrupt = backup_reconcile.catalog_corrupt_backup_ids(target_root)
    assert corrupt == []

    backup_reconcile.assert_catalog_committed(target_root)

    # 3. Marker validity check
    assert backup_reconcile._marker_valid({}) is False

    # 4. Committed receipt objects check
    assert backup_reconcile._committed_receipt_objects({}, {}) == set()

    # 5. Reconcile target on empty root
    writer = backup_writer_lease.TargetWriterLease(
        target_root,
        target_id="target-1",
        owner_run_id="run-1",
        owner_instance_id="inst-1",
        fencing_token=1,
    )
    report = backup_reconcile.reconcile_target(
        target_root,
        target_id="target-1",
        writer=writer,
        now=now_dt,
    )
    assert report["targetId"] == "target-1"
    assert report["invalidMarkers"] == []
    assert report["catalogCorrupt"] == []

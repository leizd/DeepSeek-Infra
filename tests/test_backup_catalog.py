from __future__ import annotations

import json
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_catalog


def _receipt(backup_id: str, filename: str | None = None, *, policy: str = "policy_1", created: str = "2026-01-01T00:00:00Z") -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "backupId": backup_id,
        "runId": "run_1",
        "policyId": policy,
        "targetId": "managed-local",
        "scheduleSlot": "slot",
        "filename": filename or f"{backup_id}.dsibackup.age",
        "size": 10,
        "ciphertextSha256": "a" * 64,
        "manifestDigest": "b" * 64,
        "coverageDigest": "c" * 64,
        "creationVerified": True,
        "createdAt": created,
        "pinned": False,
    }


def test_append_and_fold_catalog(tmp_path: Path) -> None:
    backup_catalog.append_receipt(tmp_path, _receipt("backup_1"))
    backup_catalog.append_receipt(tmp_path, _receipt("backup_2", created="2026-01-02T00:00:00Z"))
    backup_catalog.pin_backup(tmp_path, "backup_1", True)
    backup_catalog.record_scrub(tmp_path, "backup_1", ok=True)
    backup_catalog.record_unlock_verification(tmp_path, "backup_2")
    state = backup_catalog.catalog_state(tmp_path)
    assert set(state) == {"backup_1", "backup_2"}
    assert state["backup_1"]["pinned"] is True
    assert state["backup_1"]["scrubOk"] is True
    assert state["backup_1"]["ciphertextScrubbedAt"]
    assert state["backup_2"]["userUnlockVerifiedAt"]
    assert backup_catalog.verify_chain(tmp_path) is True


def test_chain_detects_tampering(tmp_path: Path) -> None:
    backup_catalog.append_receipt(tmp_path, _receipt("backup_1"))
    backup_catalog.append_receipt(tmp_path, _receipt("backup_2"))
    path = backup_catalog.catalog_path(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["payload"]["size"] = 999
    lines[0] = json.dumps(first)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert backup_catalog.verify_chain(tmp_path) is False


def test_trash_restore_delete_flow(tmp_path: Path) -> None:
    backup_catalog.append_receipt(tmp_path, _receipt("backup_1"))
    backup_catalog.record_trash(tmp_path, "backup_1", retention_run_id="rr_1")
    assert backup_catalog.catalog_state(tmp_path)["backup_1"]["trashed"] is True
    backup_catalog.record_restore_from_trash(tmp_path, "backup_1")
    assert backup_catalog.catalog_state(tmp_path)["backup_1"]["trashed"] is False
    backup_catalog.record_trash(tmp_path, "backup_1", retention_run_id="rr_2")
    backup_catalog.record_delete(tmp_path, "backup_1", retention_run_id="rr_2")
    record = backup_catalog.catalog_state(tmp_path)["backup_1"]
    assert record["deleted"] is True
    assert backup_catalog.list_backups(tmp_path) == []
    assert len(backup_catalog.list_backups(tmp_path, include_deleted=True)) == 1


def test_list_backups_filters(tmp_path: Path) -> None:
    backup_catalog.append_receipt(tmp_path, _receipt("backup_1", policy="policy_a"))
    backup_catalog.append_receipt(tmp_path, _receipt("backup_2", policy="policy_b", created="2026-01-02T00:00:00Z"))
    assert [item["backupId"] for item in backup_catalog.list_backups(tmp_path, policy_id="policy_a")] == ["backup_1"]
    assert [item["backupId"] for item in backup_catalog.list_backups(tmp_path)] == ["backup_2", "backup_1"]


def test_rebuild_catalog_from_receipts(tmp_path: Path) -> None:
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    for index in range(3):
        (receipts / f"backup_{index}.receipt.json").write_text(json.dumps(_receipt(f"backup_{index}", created=f"2026-01-0{index + 1}T00:00:00Z")), encoding="utf-8")
    (receipts / "broken.receipt.json").write_text("{not json", encoding="utf-8")
    result = backup_catalog.rebuild_catalog_from_receipts(tmp_path)
    assert result["rebuilt"] == 3
    assert result["chainValid"] is True
    state = backup_catalog.catalog_state(tmp_path)
    assert set(state) == {"backup_0", "backup_1", "backup_2"}


def test_orphans_and_missing(tmp_path: Path) -> None:
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    (backups_dir / "known.dsibackup.age").write_bytes(b"x")
    (backups_dir / "orphan.dsibackup.age").write_bytes(b"x")
    backup_catalog.append_receipt(tmp_path, _receipt("backup_known", "known.dsibackup.age"))
    backup_catalog.append_receipt(tmp_path, _receipt("backup_missing", "missing.dsibackup.age"))
    result = backup_catalog.find_orphans_and_missing(tmp_path)
    assert result == {"orphans": ["orphan.dsibackup.age"], "missing": ["missing.dsibackup.age"]}


def test_corrupt_catalog_raises_actionable_error(tmp_path: Path) -> None:
    path = backup_catalog.catalog_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(AppError, match="rebuild"):
        backup_catalog.catalog_state(tmp_path)


def test_append_receipt_requires_identity(tmp_path: Path) -> None:
    with pytest.raises(AppError):
        backup_catalog.append_receipt(tmp_path, {"filename": "f"})

def test_rebuild_catalog_loads_events_and_skips_corrupt(tmp_path: Path) -> None:
    catalog = backup_catalog.catalog_path(tmp_path)
    catalog.parent.mkdir(parents=True)
    catalog.write_text("{bad json\n", encoding="utf-8")
    events = tmp_path / "events" / "pol"
    events.mkdir(parents=True)
    payload = {"backupId": "A"}
    entry_hash = backup_catalog._entry_hash("receipt", payload, backup_catalog.GENESIS_HASH)
    (events / "e1.json").write_text(
        json.dumps({"entryHash": entry_hash, "type": "receipt", "payload": payload, "previousEntryHash": backup_catalog.GENESIS_HASH}), encoding="utf-8"
    )
    (events / "e2.json").write_text("{bad", encoding="utf-8")
    result = backup_catalog.rebuild_catalog_from_receipts(tmp_path)
    state = backup_catalog.catalog_state(tmp_path)
    assert "A" in state
    assert result["chainValid"] is True

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_crypto,
    backup_publish,
    backup_retention,
    backup_scheduler,
    backup_writer_lease,
    backups,
)


NOW = datetime(2026, 6, 2, 4, 0, tzinfo=timezone.utc)


class _Package:
    def __init__(self, path: Path, backup_id: str, payload: bytes) -> None:
        self.path = path
        self.backup_id = backup_id
        self.filename = f"{backup_id}.dsibackup.age"
        self.size = len(payload)
        self.ciphertext_sha256 = hashlib.sha256(payload).hexdigest()
        self.manifest_digest = "a" * 64
        self.coverage_digest = "b" * 64
        self.creation_verified = True


def _publish(root: Path, tmp_path: Path, *, backup_id: str, slot: str, payload: bytes, token: int = 1, catalog: bool = True, creation_verified: bool = True, created: str | None = None) -> dict[str, object]:
    staging = tmp_path / f"pkg-{backup_id}"
    staging.mkdir(exist_ok=True)
    package = _Package(staging / "pkg.age", backup_id, payload)
    package.path.write_bytes(payload)
    target = backup_publish.ResolvedTarget(target_id="managed-local", root=root, managed=True)
    result = backup_publish.publish_backup(target, package, run_id=f"run_{backup_id}", policy_id="policy_1", schedule_slot=slot, fencing_token=token)
    receipt = dict(result.receipt)
    receipt["creationVerified"] = creation_verified
    if created is not None:
        receipt["createdAt"] = created
    if catalog:
        backup_catalog.append_receipt(root, receipt)
    return receipt


def _retention(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"keepLast": 0, "keepHourly": 0, "keepDaily": 0, "keepWeekly": 0, "keepMonthly": 0, "minimumHealthyCopies": 1}
    payload.update(overrides)
    return backup_retention.normalize_retention_policy(payload)


def _writer(root: Path) -> backup_writer_lease.TargetWriterLease:
    lease = backup_writer_lease.TargetWriterLease(
        root,
        target_id="managed-local",
        owner_run_id="run_writer",
        owner_instance_id="w1",
        fencing_token=backup_scheduler.allocate_fencing_token(),
    )
    lease.acquire()
    return lease


def test_apply_rejects_stale_catalog_head(tmp_settings: Path, tmp_path: Path) -> None:
    root = backups.BACKUP_DIR
    _publish(root, tmp_path, backup_id="backup_a", slot="slot-a", payload=b"a", token=1)
    retention = _retention()
    preview = backup_retention.preview_retention(retention, root, now=NOW)
    assert preview["retentionRunId"]
    assert preview["catalogHeadHash"]
    assert preview["policyDigest"]
    _publish(root, tmp_path, backup_id="backup_b", slot="slot-b", payload=b"b", token=2)
    with pytest.raises(AppError) as exc:
        backup_retention.apply_retention(retention, root, preview=preview)
    assert exc.value.status == 409
    assert "catalog head changed" in str(exc.value)


def test_apply_rejects_stale_target_generation(tmp_settings: Path, tmp_path: Path) -> None:
    root = backups.BACKUP_DIR
    _publish(root, tmp_path, backup_id="backup_a", slot="slot-a", payload=b"a", token=1)
    retention = _retention()
    preview = backup_retention.preview_retention(retention, root, now=NOW)
    _publish(root, tmp_path, backup_id="backup_b", slot="slot-b", payload=b"b", token=2, catalog=False)
    with pytest.raises(AppError) as exc:
        backup_retention.apply_retention(retention, root, preview=preview)
    assert "target generation changed" in str(exc.value)


def test_apply_rejects_changed_policy(tmp_settings: Path, tmp_path: Path) -> None:
    root = backups.BACKUP_DIR
    _publish(root, tmp_path, backup_id="backup_a", slot="slot-a", payload=b"a", token=1)
    preview = backup_retention.preview_retention(_retention(), root, now=NOW)
    with pytest.raises(AppError) as exc:
        backup_retention.apply_retention(_retention(keepLast=2), root, preview=preview)
    assert "retention policy changed" in str(exc.value)


def test_apply_rejects_incomplete_preview(tmp_settings: Path, tmp_path: Path) -> None:
    root = backups.BACKUP_DIR
    retention = _retention()
    with pytest.raises(AppError) as exc:
        backup_retention.apply_retention(retention, root, preview={"keep": []})
    assert exc.value.status == 409


def test_fresh_preview_applies_cleanly(tmp_settings: Path, tmp_path: Path) -> None:
    root = backups.BACKUP_DIR
    _publish(root, tmp_path, backup_id="backup_a", slot="slot-a", payload=b"a", token=1)
    retention = _retention()
    preview = backup_retention.preview_retention(retention, root, now=NOW)
    applied = backup_retention.apply_retention(retention, root, preview=preview)
    assert applied["retentionRunId"] == preview["retentionRunId"]
    assert applied["recoveredTrash"] == []


def test_unhealthy_backups_not_counted(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = backups.BACKUP_DIR
    _publish(root, tmp_path, backup_id="backup_new", slot="slot-d", payload=b"new", token=4, created="2026-06-04T00:00:00Z")
    healthy_receipt = _publish(root, tmp_path, backup_id="backup_ok", slot="slot-a", payload=b"ok", token=1, created="2026-06-02T00:00:00Z")
    _publish(root, tmp_path, backup_id="backup_broken", slot="slot-c", payload=b"br", token=3, created="2026-06-01T00:00:00Z")
    _publish(root, tmp_path, backup_id="backup_unverified", slot="slot-b", payload=b"uv", token=2, creation_verified=False, created="2026-05-31T00:00:00Z")
    receipt_path = root / "receipts" / "backup_broken.json"
    receipt_path.write_text(receipt_path.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
    monkeypatch.setattr(backup_crypto, "inspect_header", lambda _path: {"age": True})
    retention = _retention(minimumHealthyCopies=3)
    preview = backup_retention.preview_retention(retention, root, now=NOW)
    protected = {item["backupId"]: item["reason"] for item in preview["protected"]}
    assert protected.get("backup_new") == "latest-successful-backup"
    assert protected.get(str(healthy_receipt["backupId"])) == "minimum-healthy-copies"
    assert "backup_broken" not in protected
    assert "backup_unverified" not in protected
    assert "backup_broken" in preview["trash"]
    assert "backup_unverified" in preview["trash"]


def test_uncommitted_object_not_counted_healthy(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = backups.BACKUP_DIR
    receipt = _publish(root, tmp_path, backup_id="backup_phantom", slot="slot-a", payload=b"ph", token=1)
    for marker in root.joinpath("commits", "policy_1").glob("*.json"):
        marker.unlink()
    monkeypatch.setattr(backup_crypto, "inspect_header", lambda _path: {"age": True})
    preview = backup_retention.preview_retention(_retention(minimumHealthyCopies=1), root, now=NOW)
    healthy_protected = {item["backupId"] for item in preview["protected"] if item["reason"] == "minimum-healthy-copies"}
    assert str(receipt["backupId"]) not in healthy_protected
    with pytest.raises(AppError, match="catalog-corrupt"):
        backup_retention.apply_retention(_retention(), root, now=NOW)


def test_trash_journal_recovery_completes_interrupted_move(tmp_settings: Path, tmp_path: Path) -> None:
    root = backups.BACKUP_DIR
    receipt = _publish(root, tmp_path, backup_id="backup_crash", slot="slot-a", payload=b"crash", token=1)
    backup_id = str(receipt["backupId"])
    digest = str(receipt["objectDigest"])
    destination = root / ".trash" / backup_id
    destination.mkdir(parents=True)
    journal = {
        "schemaVersion": 1,
        "backupId": backup_id,
        "retentionRunId": "rr_crash",
        "filename": str(receipt["filename"]),
        "objectDigest": digest,
        "payloadNames": [f"{digest}.age"],
        "receiptNames": [f"{backup_id}.json"],
        "phase": "intent",
        "recordedAt": "2026-06-02T04:00:00Z",
    }
    (destination / backup_retention.TRASH_JOURNAL_NAME).write_text(json.dumps(journal), encoding="utf-8")
    applied = backup_retention.apply_retention(_retention(), root, now=NOW)
    assert backup_id in applied["recoveredTrash"]
    assert (destination / f"{digest}.age").is_file()
    assert (destination / f"{backup_id}.json").is_file()
    record = backup_catalog.catalog_state(root)[backup_id]
    assert record["trashed"] is True
    assert json.loads((destination / backup_retention.TRASH_JOURNAL_NAME).read_text(encoding="utf-8"))["phase"] == "event-committed"


def test_trash_journal_recovery_after_payload_moved(tmp_settings: Path, tmp_path: Path) -> None:
    root = backups.BACKUP_DIR
    receipt = _publish(root, tmp_path, backup_id="backup_half", slot="slot-a", payload=b"half", token=1)
    backup_id = str(receipt["backupId"])
    digest = str(receipt["objectDigest"])
    destination = root / ".trash" / backup_id
    destination.mkdir(parents=True)
    obj = backup_publish.object_path(root, digest)
    obj.replace(destination / obj.name)
    journal = {
        "schemaVersion": 1,
        "backupId": backup_id,
        "retentionRunId": "rr_half",
        "filename": str(receipt["filename"]),
        "objectDigest": digest,
        "payloadNames": [f"{digest}.age"],
        "receiptNames": [f"{backup_id}.json"],
        "phase": "payload-moved",
        "recordedAt": "2026-06-02T04:00:00Z",
    }
    (destination / backup_retention.TRASH_JOURNAL_NAME).write_text(json.dumps(journal), encoding="utf-8")
    recovered = backup_retention._recover_trash_journals(root, at="2026-06-02T05:00:00Z", writer=None)
    assert recovered == [backup_id]
    assert (destination / f"{backup_id}.json").is_file()
    assert backup_catalog.catalog_state(root)[backup_id]["trashed"] is True


def test_apply_requires_writer_lease(tmp_settings: Path, tmp_path: Path) -> None:
    root = backups.BACKUP_DIR
    receipt = _publish(root, tmp_path, backup_id="backup_a", slot="slot-a", payload=b"a", token=1)
    writer = _writer(root)
    writer.path.unlink()
    with pytest.raises(AppError) as exc:
        backup_retention.apply_retention(_retention(), root, now=NOW, writer=writer)
    assert exc.value.status == 409
    assert backup_publish.object_path(root, str(receipt["objectDigest"])).is_file()
    assert backup_catalog.catalog_state(root)["backup_a"]["trashed"] is False

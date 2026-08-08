from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_crypto,
    backup_policies,
    backup_publish,
    backup_reconcile,
    backup_retention,
    backup_scheduler,
    backup_writer_lease,
    backups,
)


UTC = timezone.utc
RECIPIENT_A = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"
NOW = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)


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


def _policy(tmp_settings: Path, **updates: object) -> dict[str, object]:
    policy = backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": "nightly",
            "enabled": True,
            "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
            "scope": {"mode": "full", "includeExternalState": False, "coveragePolicy": "best-effort"},
            "frontendMirror": {"mode": "best-effort"},
            "protection": {"mode": "age-recipient", "recipients": [RECIPIENT_A]},
            "targetId": "managed-local",
            "retry": {"maxAttempts": 1, "initialBackoffSeconds": 30, "maxBackoffSeconds": 60},
        }
    )
    if updates:
        policy = backup_policies.update_policy(str(policy["policyId"]), updates)
    return policy


def _seed_workspace() -> None:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('{"items":[]}', encoding="utf-8")


def _publish_without_completing(tmp_settings: Path, policy: dict[str, object], run: backup_scheduler.ClaimedRun, *, catalog: bool = True) -> backup_publish.PublishResult:
    package = backup_scheduled_package(policy, run)
    target = backup_publish.resolve_target("managed-local")
    published = backup_publish.publish_backup(target, package, run_id=run.run_id, policy_id=str(policy["policyId"]), schedule_slot=run.schedule_slot, fencing_token=run.fencing_token)
    if catalog:
        backup_catalog.append_receipt(backups.BACKUP_DIR, published.receipt)
    return published


def backup_scheduled_package(policy: dict[str, object], run: backup_scheduler.ClaimedRun) -> object:
    from deepseek_infra.infra.workspace import backup_scheduled

    return backup_scheduled.build_scheduled_backup(policy, run_id=run.run_id, staging_root=backup_scheduler.staging_root(), schedule_slot=run.schedule_slot)


def test_converges_run_with_surviving_marker(tmp_settings: Path, stub_crypto: None) -> None:
    _seed_workspace()
    policy = _policy(tmp_settings)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    backup_scheduler.record_run_phase(run.run_id, "publishing", instance_id="w1", fencing_token=run.fencing_token, now=NOW)
    published = _publish_without_completing(tmp_settings, policy, run)
    assert backup_scheduler.get_run(run.run_id)["phase"] == "publishing"
    reports = backup_reconcile.reconcile_all_targets(instance_id="w2", now=NOW + timedelta(seconds=10))
    report = next(item for item in reports if item["targetId"] == "managed-local")
    assert report["convergedRuns"] == [run.run_id]
    converged = backup_scheduler.get_run(run.run_id)
    assert converged["phase"] == "complete"
    assert converged["backupId"] == str(published.receipt["backupId"])
    assert backup_scheduler.reclaim_abandoned_slots(instance_id="w3", now=NOW + timedelta(seconds=400)) == []


def test_rebuilds_missing_receipt_from_journal(tmp_settings: Path, stub_crypto: None) -> None:
    _seed_workspace()
    policy = _policy(tmp_settings)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    published = _publish_without_completing(tmp_settings, policy, run)
    receipt_path = backups.BACKUP_DIR / "receipts" / f"{published.receipt['backupId']}.json"
    receipt_path.unlink()
    backup_catalog.catalog_path(backups.BACKUP_DIR).unlink()
    report = next(item for item in backup_reconcile.reconcile_all_targets(instance_id="w2", now=NOW) if item["targetId"] == "managed-local")
    assert report["rebuiltReceipts"] == [str(published.receipt["backupId"])]
    rebuilt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert rebuilt["backupId"] == published.receipt["backupId"]
    assert rebuilt["objectDigest"] == published.receipt["objectDigest"]
    assert report["catalogBackfilled"] == [str(published.receipt["backupId"])]
    assert backup_catalog.verify_chain(backups.BACKUP_DIR) is True


def test_backfills_missing_catalog_projection(tmp_settings: Path, stub_crypto: None) -> None:
    _seed_workspace()
    policy = _policy(tmp_settings)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    published = _publish_without_completing(tmp_settings, policy, run, catalog=False)
    assert backup_catalog.catalog_state(backups.BACKUP_DIR) == {}
    report = next(item for item in backup_reconcile.reconcile_all_targets(instance_id="w2", now=NOW) if item["targetId"] == "managed-local")
    assert report["catalogBackfilled"] == [str(published.receipt["backupId"])]
    state = backup_catalog.catalog_state(backups.BACKUP_DIR)
    assert str(published.receipt["backupId"]) in state
    assert backup_catalog.verify_chain(backups.BACKUP_DIR) is True


def _plant_orphan(root: Path, *, digest: str, backup_id: str, age_days: float) -> None:
    obj = backup_publish.object_path(root, digest)
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_bytes(b"orphan-ciphertext")
    receipt = {
        "schemaVersion": 2,
        "backupId": backup_id,
        "runId": f"run_{backup_id}",
        "policyId": "p",
        "targetId": "managed-local",
        "scheduleSlot": "s",
        "filename": f"{backup_id}.age",
        "size": 17,
        "ciphertextSha256": digest,
        "objectDigest": digest,
        "manifestDigest": "b" * 64,
        "coverageDigest": "c" * 64,
        "creationVerified": True,
        "createdAt": "2026-01-01T00:00:00Z",
        "pinned": False,
    }
    (root / "receipts").mkdir(parents=True, exist_ok=True)
    receipt_path = root / "receipts" / f"{backup_id}.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    old = (NOW - timedelta(days=age_days)).timestamp()
    os.utime(obj, (old, old))
    os.utime(receipt_path, (old, old))


def test_orphans_move_after_grace_period(tmp_settings: Path) -> None:
    root = backups.BACKUP_DIR
    _plant_orphan(root, digest="a" * 64, backup_id="backup_old", age_days=2)
    _plant_orphan(root, digest="b" * 64, backup_id="backup_fresh", age_days=0.1)
    report = backup_reconcile.reconcile_target(
        root,
        target_id="managed-local",
        writer=_writer(root),
        now=NOW,
    )
    assert report["orphanedObjects"] == [f"{'a' * 64}.age"]
    assert report["orphanedReceipts"] == ["backup_old.json"]
    assert (root / ".orphaned" / "objects" / f"{'a' * 64}.age").is_file()
    assert (root / ".orphaned" / "receipts" / "backup_old.json").is_file()
    assert backup_publish.object_path(root, "b" * 64).is_file()
    assert (root / "receipts" / "backup_fresh.json").is_file()


def _writer(root: Path) -> backup_writer_lease.TargetWriterLease:
    lease = backup_writer_lease.TargetWriterLease(root, target_id="managed-local", owner_run_id="reconcile_test", owner_instance_id="w2", fencing_token=backup_scheduler.allocate_fencing_token(), clock=lambda: NOW)
    lease.acquire()
    return lease


def test_catalog_corrupt_blocks_retention_until_reconciled(tmp_settings: Path) -> None:
    root = backups.BACKUP_DIR
    digest = "c" * 64
    _plant_orphan(root, digest=digest, backup_id="backup_uncommitted", age_days=2)
    receipt = json.loads((root / "receipts" / "backup_uncommitted.json").read_text(encoding="utf-8"))
    backup_catalog.append_receipt(root, receipt)
    retention = backup_retention.normalize_retention_policy({})
    with pytest.raises(AppError) as exc:
        backup_retention.apply_retention(retention, root, now=NOW)
    assert exc.value.status == 409
    assert "catalog-corrupt" in str(exc.value)
    with pytest.raises(AppError):
        backup_retention.finalize_retention(retention, root, now=NOW)
    writer = _writer(root)
    try:
        report = backup_reconcile.reconcile_target(root, target_id="managed-local", writer=writer, now=NOW + timedelta(days=1))
    finally:
        writer.release()
    assert report["orphanedObjects"] == [f"{digest}.age"]
    assert report["orphanedReceipts"] == ["backup_uncommitted.json"]
    assert backup_catalog.catalog_state(root)["backup_uncommitted"]["trashed"] is False


def test_rebuilds_catalog_projection_from_committed_receipts_only(tmp_settings: Path, stub_crypto: None) -> None:
    _seed_workspace()
    policy = _policy(tmp_settings)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    published = _publish_without_completing(tmp_settings, policy, run)
    _plant_orphan(backups.BACKUP_DIR, digest="d" * 64, backup_id="backup_phantom", age_days=0)
    backup_catalog.catalog_path(backups.BACKUP_DIR).write_text("{corrupt json\n", encoding="utf-8")
    writer = _writer(backups.BACKUP_DIR)
    try:
        report = backup_reconcile.reconcile_target(backups.BACKUP_DIR, target_id="managed-local", writer=writer, now=NOW)
    finally:
        writer.release()
    assert report["catalogRebuilt"] is True
    state = backup_catalog.catalog_state(backups.BACKUP_DIR)
    assert set(state) == {str(published.receipt["backupId"])}
    assert backup_catalog.verify_chain(backups.BACKUP_DIR) is True
    assert report["catalogCorrupt"] == []


def test_backup_worker_reconciles_on_start(tmp_settings: Path, stub_crypto: None) -> None:
    _seed_workspace()
    policy = _policy(tmp_settings)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    _publish_without_completing(tmp_settings, policy, run)
    worker = backup_scheduler.BackupWorker(lambda _run: None, instance_id="w2", tick_seconds=3600)
    worker.start()
    try:
        converged = backup_scheduler.get_run(run.run_id)
        assert converged["phase"] == "complete"
    finally:
        worker.stop()
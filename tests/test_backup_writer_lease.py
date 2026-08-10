from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_crypto,
    backup_executor,
    backup_policies,
    backup_publish,
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


def _lease(root: Path, *, run: str = "run_1", token: int = 1, clock: object = None) -> backup_writer_lease.TargetWriterLease:
    return backup_writer_lease.TargetWriterLease(
        root,
        target_id="managed-local",
        owner_run_id=run,
        owner_instance_id="w1",
        fencing_token=token,
        clock=clock,  # type: ignore[arg-type]
    )


def _seed_lease_file(root: Path, *, token: int, expired: bool, run: str = "run_old") -> None:
    at = NOW - timedelta(seconds=400) if expired else NOW
    payload = {
        "schemaVersion": 1,
        "targetId": "managed-local",
        "ownerRunId": run,
        "ownerInstanceId": "w0",
        "fencingToken": token,
        "acquiredAt": at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "expiresAt": (at + timedelta(seconds=300)).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    path = root / ".target-lock" / "writer.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


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


def test_acquire_release_roundtrip(tmp_path: Path) -> None:
    lease = _lease(tmp_path)
    lease.acquire()
    payload = json.loads(lease.path.read_text(encoding="utf-8"))
    assert payload["schemaVersion"] == 1
    assert payload["targetId"] == "managed-local"
    assert payload["ownerRunId"] == "run_1"
    assert payload["ownerInstanceId"] == "w1"
    assert payload["fencingToken"] == 1
    assert payload["expiresAt"] > payload["acquiredAt"]
    with pytest.raises(AppError) as busy:
        _lease(tmp_path, run="run_2", token=2).acquire()
    assert busy.value.status == 423
    lease.release()
    assert not lease.path.exists()
    _lease(tmp_path, run="run_2", token=2).acquire()


def test_store_acquire_reconciles_large_server_clock_skew(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.workspace import backup_writer_lease
    from deepseek_infra.infra.workspace.backup_target_store import MemoryTargetStore, read_json, writer_lease_key

    clock = lambda: datetime(2026, 6, 2, 4, 0, 0, tzinfo=timezone.utc)  # noqa: E731
    store = MemoryTargetStore()
    lease = backup_writer_lease.TargetWriterLease(
        None,
        store=store,
        target_id="t",
        owner_run_id="run_1",
        owner_instance_id="w1",
        fencing_token=1,
        clock=clock,
    )
    # Simulate a target server clock two hours ahead of the local clock.
    def note_server_date(self_: object, server_date: str | None) -> None:
        del self_, server_date
        lease._server_skew = timedelta(hours=2)

    monkeypatch.setattr(backup_writer_lease.TargetWriterLease, "_note_server_date", note_server_date)
    lease.acquire()
    payload = read_json(store, writer_lease_key())
    assert payload is not None and payload["ownerRunId"] == "run_1"
    # The reconciled lease expires at least a lease period after the skewed now.
    from deepseek_infra.infra.workspace.backup_writer_lease import CLOCK_SKEW_SAFETY_SECONDS

    assert str(payload["expiresAt"]) > (clock() + timedelta(hours=2)).isoformat(timespec="seconds").replace("+00:00", "Z") + "Z"
    assert abs((datetime.fromisoformat(str(payload["expiresAt"]).replace("Z", "+00:00")) - (clock() + timedelta(hours=2))).total_seconds()) > CLOCK_SKEW_SAFETY_SECONDS
    # A follower using the reconciled skew must still own the lease.
    follower = backup_writer_lease.TargetWriterLease(
        None,
        store=store,
        target_id="t",
        owner_run_id="run_1",
        owner_instance_id="w1",
        fencing_token=1,
        clock=clock,
    )
    follower._server_skew = timedelta(hours=2)
    follower.acquired = True
    follower.assert_owned()


def test_expired_lease_preempted_only_by_higher_token(tmp_path: Path) -> None:
    _seed_lease_file(tmp_path, token=3, expired=True)
    lease = _lease(tmp_path, run="run_new", token=5, clock=lambda: NOW)
    lease.acquire()
    assert json.loads(lease.path.read_text(encoding="utf-8"))["ownerRunId"] == "run_new"
    lease.release()
    _seed_lease_file(tmp_path, token=5, expired=True)
    with pytest.raises(AppError) as exc:
        _lease(tmp_path, run="run_stale", token=4, clock=lambda: NOW).acquire()
    assert exc.value.status == 423
    assert "fencing token" in str(exc.value)


def test_active_lease_never_preempted(tmp_path: Path) -> None:
    _seed_lease_file(tmp_path, token=3, expired=False)
    with pytest.raises(AppError) as exc:
        _lease(tmp_path, run="run_new", token=9, clock=lambda: NOW).acquire()
    assert exc.value.status == 423
    assert "busy" in str(exc.value)


def test_renew_and_assert_owned_detects_loss(tmp_path: Path) -> None:
    box = [NOW]

    def clock() -> datetime:
        return box[0]

    lease = _lease(tmp_path, clock=clock)
    lease.acquire()
    initial = str(json.loads(lease.path.read_text(encoding="utf-8"))["expiresAt"])
    box[0] = NOW + timedelta(seconds=60)
    lease.renew()
    renewed = str(json.loads(lease.path.read_text(encoding="utf-8"))["expiresAt"])
    assert renewed > initial
    lease.path.unlink()
    with pytest.raises(AppError) as missing:
        lease.assert_owned()
    assert missing.value.status == 409
    _lease(tmp_path, run="run_thief", token=2).acquire()
    with pytest.raises(AppError) as stolen:
        lease.assert_owned()
    assert stolen.value.status == 409
    assert "lost" in str(stolen.value)


def test_probe_atomic_target_detects_broken_semantics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backup_writer_lease.probe_atomic_target(tmp_path)
    real_open = os.open

    def lax_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if ".probe-" in str(path):
            flags &= ~os.O_EXCL
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backup_writer_lease.os, "open", lax_open)
    with pytest.raises(AppError) as exc:
        backup_writer_lease.probe_atomic_target(tmp_path)
    assert exc.value.status == 503
    assert "unsupported-atomic-target" in str(exc.value)


def test_checkpoint_covers_writer_loss(tmp_settings: Path) -> None:
    policy = _policy(tmp_settings)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    guard = backup_scheduler.RunLeaseGuard(run.run_id, "w1", run.fencing_token, clock=lambda: NOW)
    root = backups.BACKUP_DIR
    writer = _lease(root, run=run.run_id, token=run.fencing_token, clock=lambda: NOW)
    writer.acquire()
    guard.attach_writer(writer)
    guard.checkpoint()
    writer.path.unlink()
    _lease(root, run="run_thief", token=999).acquire()
    with pytest.raises(AppError) as exc:
        guard.checkpoint()
    assert exc.value.status == 409
    assert "writer lease lost" in str(exc.value)


def test_heartbeat_renews_writer_lease(tmp_settings: Path) -> None:
    policy = _policy(tmp_settings)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    box = [NOW]
    guard = backup_scheduler.RunLeaseGuard(run.run_id, "w1", run.fencing_token, heartbeat_seconds=0.05, clock=lambda: box[0])
    root = backups.BACKUP_DIR
    writer = _lease(root, run=run.run_id, token=run.fencing_token, clock=lambda: box[0])
    writer.acquire()
    initial = str(json.loads(writer.path.read_text(encoding="utf-8"))["expiresAt"])
    guard.attach_writer(writer)
    guard.start_heartbeat()
    try:
        for step in range(4):
            time.sleep(0.06)
            box[0] = NOW + timedelta(seconds=10 * (step + 1))
    finally:
        guard.stop()
    renewed = str(json.loads(writer.path.read_text(encoding="utf-8"))["expiresAt"])
    assert renewed > initial
    writer.release()


def test_executor_releases_writer_lock(tmp_settings: Path, stub_crypto: None) -> None:
    _seed_workspace()
    policy = _policy(tmp_settings)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    outcome = backup_executor.execute_run(run, instance_id="w1", now=NOW)
    assert outcome["phase"] == "complete"
    assert not (backups.BACKUP_DIR / ".target-lock" / "writer.json").exists()
    assert backup_publish.read_commit_markers(backups.BACKUP_DIR)


def test_retention_cannot_move_files_after_writer_loss(tmp_settings: Path, stub_crypto: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_workspace()
    policy = _policy(tmp_settings)
    first = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    assert backup_executor.execute_run(first, instance_id="w1", now=NOW)["phase"] == "complete"
    root = backups.BACKUP_DIR
    older = next(iter(backup_catalog.catalog_state(root).values()))
    older_digest = str(older["objectDigest"])
    backup_retention.put_retention_policy("aggressive", {"keepLast": 1, "keepHourly": 0, "keepDaily": 0, "keepWeekly": 0, "keepMonthly": 0, "minimumHealthyCopies": 1})
    policy = _policy(tmp_settings, retentionPolicyId="aggressive")
    later = NOW + timedelta(days=1, minutes=5)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=later)[0]
    original_apply = backup_retention.apply_retention

    def steal_then_apply(*args: object, **kwargs: object) -> object:
        lock = root / ".target-lock" / "writer.json"
        lock.unlink(missing_ok=True)
        _lease(root, run="run_thief", token=999).acquire()
        return original_apply(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backup_executor.backup_retention, "apply_retention", steal_then_apply)
    outcome = backup_executor.execute_run(run, instance_id="w1", now=later)
    assert outcome["phase"] == "abandoned"
    assert "lease" in str(outcome["error"]).casefold()
    assert backup_publish.object_path(root, older_digest).is_file()
    assert not (root / ".trash" / str(older["backupId"])).exists()
    assert backup_catalog.catalog_state(root)[str(older["backupId"])]["trashed"] is False


def test_catalog_mutations_assert_writer_ownership(tmp_path: Path) -> None:
    lease = _lease(tmp_path)
    lease.acquire()
    receipt = {
        "schemaVersion": 2,
        "backupId": "backup_1",
        "runId": "run_1",
        "policyId": "p",
        "targetId": "managed-local",
        "scheduleSlot": "s",
        "filename": "f.age",
        "size": 1,
        "ciphertextSha256": "a" * 64,
        "objectDigest": "a" * 64,
        "manifestDigest": "b" * 64,
        "coverageDigest": "c" * 64,
        "creationVerified": True,
        "createdAt": "2026-01-01T00:00:00Z",
        "pinned": False,
    }
    backup_catalog.append_receipt(tmp_path, receipt, writer=lease)
    backup_catalog.pin_backup(tmp_path, "backup_1", True, writer=lease)
    assert backup_catalog.catalog_state(tmp_path)["backup_1"]["pinned"] is True
    lease.path.unlink()
    with pytest.raises(AppError) as exc:
        backup_catalog.append_receipt(tmp_path, {**receipt, "backupId": "backup_2"}, writer=lease)
    assert exc.value.status == 409
    with pytest.raises(AppError):
        backup_catalog.pin_backup(tmp_path, "backup_1", False, writer=lease)
    with pytest.raises(AppError):
        backup_catalog.rebuild_catalog_from_receipts(tmp_path, writer=lease)

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_catalog, backup_scheduler, backup_writer_lease


def _writer(root: Path, *, run: str = "run_1", token: int | None = None) -> backup_writer_lease.TargetWriterLease:
    lease = backup_writer_lease.TargetWriterLease(
        root,
        target_id="managed-local",
        owner_run_id=run,
        owner_instance_id="w1",
        fencing_token=token if token is not None else backup_scheduler.allocate_fencing_token(),
    )
    lease.acquire()
    return lease


def _receipt(backup_id: str, *, created: str = "2026-01-01T00:00:00Z") -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "backupId": backup_id,
        "runId": f"run_{backup_id}",
        "policyId": "policy_1",
        "targetId": "managed-local",
        "scheduleSlot": "slot",
        "filename": f"{backup_id}.age",
        "size": 10,
        "ciphertextSha256": "a" * 64,
        "objectDigest": "a" * 64,
        "manifestDigest": "b" * 64,
        "coverageDigest": "c" * 64,
        "creationVerified": True,
        "createdAt": created,
        "pinned": False,
    }


def test_appends_write_immutable_event_files(tmp_settings: Path, tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    try:
        entry = backup_catalog.append_receipt(tmp_path, _receipt("backup_1"), writer=writer)
        backup_catalog.pin_backup(tmp_path, "backup_1", True, writer=writer)
        backup_catalog.record_scrub(tmp_path, "backup_1", ok=True, writer=writer)
    finally:
        writer.release()
    digest = str(entry["entryHash"])
    event_path = tmp_path / "events" / digest[:2] / f"{digest}.json"
    assert event_path.is_file()
    event = json.loads(event_path.read_text(encoding="utf-8"))
    assert event["type"] == "receipt"
    assert event["previousEntryHash"] == backup_catalog.GENESIS_HASH
    assert event["targetGeneration"] == 0
    assert event["writerFencingToken"] == writer.fencing_token
    events = list((tmp_path / "events").rglob("*.json"))
    assert len(events) == 3
    line = json.loads(backup_catalog.catalog_path(tmp_path).read_text(encoding="utf-8").splitlines()[0])
    assert line["targetGeneration"] == 0
    assert line["writerFencingToken"] == writer.fencing_token


def test_rebuild_preserves_governance_history(tmp_settings: Path, tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    try:
        backup_catalog.append_receipt(tmp_path, _receipt("backup_1"), writer=writer)
        backup_catalog.pin_backup(tmp_path, "backup_1", True, writer=writer)
        backup_catalog.record_scrub(tmp_path, "backup_1", ok=True, writer=writer)
        backup_catalog.record_unlock_verification(tmp_path, "backup_1", writer=writer)
        backup_catalog.record_trash(tmp_path, "backup_1", retention_run_id="rr_1", writer=writer)
    finally:
        writer.release()
    before = backup_catalog.catalog_state(tmp_path)["backup_1"]
    backup_catalog.catalog_path(tmp_path).write_text("{corrupt\n", encoding="utf-8")
    result = backup_catalog.rebuild_catalog_from_receipts(tmp_path)
    assert result["chainValid"] is True
    after = backup_catalog.catalog_state(tmp_path)["backup_1"]
    assert after["pinned"] == before["pinned"] is True
    assert after["scrubOk"] is True
    assert after["ciphertextScrubbedAt"] == before["ciphertextScrubbedAt"]
    assert after["userUnlockVerifiedAt"] == before["userUnlockVerifiedAt"]
    assert after["trashed"] is True


def test_rebuild_from_events_without_jsonl(tmp_settings: Path, tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    try:
        backup_catalog.append_receipt(tmp_path, _receipt("backup_1"), writer=writer)
        backup_catalog.pin_backup(tmp_path, "backup_1", True, writer=writer)
    finally:
        writer.release()
    backup_catalog.catalog_path(tmp_path).unlink()
    result = backup_catalog.rebuild_catalog_from_receipts(tmp_path)
    assert result["rebuilt"] == 2
    state = backup_catalog.catalog_state(tmp_path)
    assert state["backup_1"]["pinned"] is True
    assert backup_catalog.verify_chain(tmp_path) is True


def test_head_precondition_cas_enforced(tmp_settings: Path, tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    try:
        snapshot = backup_catalog.catalog_precondition(tmp_path)
        backup_catalog.append_receipt(tmp_path, _receipt("backup_1"), writer=writer, precondition=snapshot)
        with pytest.raises(AppError) as exc:
            backup_catalog.append_receipt(tmp_path, _receipt("backup_2"), writer=writer, precondition=snapshot)
        assert exc.value.status == 409
        assert "catalog-head-cas-failed" in str(exc.value)
        fresh = backup_catalog.catalog_precondition(tmp_path)
        backup_catalog.append_receipt(tmp_path, _receipt("backup_2"), writer=writer, precondition=fresh)
    finally:
        writer.release()
    assert set(backup_catalog.catalog_state(tmp_path)) == {"backup_1", "backup_2"}
    assert backup_catalog.verify_chain(tmp_path) is True


def test_generation_precondition_cas_enforced(tmp_settings: Path, tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    try:
        wrong = backup_catalog.CatalogPrecondition(expected_head_hash=None, expected_target_generation=7)
        with pytest.raises(AppError) as exc:
            backup_catalog.append_receipt(tmp_path, _receipt("backup_1"), writer=writer, precondition=wrong)
        assert exc.value.status == 409
        assert "catalog-generation-cas-failed" in str(exc.value)
        snapshot = backup_catalog.catalog_precondition(tmp_path)
        assert snapshot.expected_target_generation == 0
        backup_catalog.append_receipt(tmp_path, _receipt("backup_1"), writer=writer, precondition=snapshot)
    finally:
        writer.release()
    assert backup_catalog.verify_chain(tmp_path) is True


def test_concurrent_appends_serialize_without_fork(tmp_settings: Path, tmp_path: Path) -> None:
    lock = threading.Lock()
    token_box = [100]

    def worker(worker_index: int) -> None:
        for entry_index in range(5):
            while True:
                with lock:
                    token_box[0] += 1
                    token = token_box[0]
                lease = backup_writer_lease.TargetWriterLease(
                    tmp_path,
                    target_id="managed-local",
                    owner_run_id=f"run_{worker_index}_{entry_index}",
                    owner_instance_id=f"w{worker_index}",
                    fencing_token=token,
                )
                try:
                    lease.acquire()
                except AppError as exc:
                    if exc.status == 423:
                        threading.Event().wait(0.01)
                        continue
                    raise
                try:
                    backup_catalog.append_receipt(tmp_path, _receipt(f"backup_{worker_index}_{entry_index}"), writer=lease)
                finally:
                    lease.release()
                break

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)
    for thread in threads:
        assert not thread.is_alive()
    state = backup_catalog.catalog_state(tmp_path)
    assert len(state) == 20
    assert backup_catalog.verify_chain(tmp_path) is True
    entries = backup_catalog._read_entries(tmp_path)
    assert len({entry["previousEntryHash"] for entry in entries}) == len(entries)


def test_rebuild_skips_forked_entries(tmp_settings: Path, tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    try:
        backup_catalog.append_receipt(tmp_path, _receipt("backup_1"), writer=writer)
    finally:
        writer.release()
    fork_payload = dict(_receipt("backup_fork"))
    fork_entry = {
        "schemaVersion": backup_catalog.CATALOG_SCHEMA_VERSION,
        "type": "receipt",
        "payload": fork_payload,
        "previousEntryHash": backup_catalog.GENESIS_HASH,
        "entryHash": backup_catalog._entry_hash("receipt", fork_payload, backup_catalog.GENESIS_HASH),
        "recordedAt": "2026-01-02T00:00:00Z",
        "targetGeneration": 0,
        "writerFencingToken": 0,
    }
    backup_catalog._write_event_file(tmp_path, fork_entry)
    result = backup_catalog.rebuild_catalog_from_receipts(tmp_path)
    assert result["skippedForkEntries"] == 1
    assert result["chainValid"] is True
    state = backup_catalog.catalog_state(tmp_path)
    assert len(state) == 1
    head_after_first = backup_catalog.catalog_precondition(tmp_path).expected_head_hash
    second = backup_catalog.rebuild_catalog_from_receipts(tmp_path)
    assert second["skippedForkEntries"] == 1
    assert backup_catalog.catalog_precondition(tmp_path).expected_head_hash == head_after_first


def test_legacy_jsonl_history_survives_rebuild(tmp_settings: Path, tmp_path: Path) -> None:
    backup_catalog.append_receipt(tmp_path, _receipt("backup_legacy"))
    backup_catalog.pin_backup(tmp_path, "backup_legacy", True)
    events = tmp_path / "events"
    if events.is_dir():
        import shutil

        shutil.rmtree(events)
    backup_catalog.catalog_path(tmp_path).write_text(backup_catalog.catalog_path(tmp_path).read_text(encoding="utf-8"), encoding="utf-8")
    result = backup_catalog.rebuild_catalog_from_receipts(tmp_path)
    assert result["rebuilt"] == 2
    assert backup_catalog.catalog_state(tmp_path)["backup_legacy"]["pinned"] is True


def test_writer_lease_context_manager(tmp_path: Path) -> None:
    lease = backup_writer_lease.TargetWriterLease(
        tmp_path,
        target_id="managed-local",
        owner_run_id="run_ctx",
        owner_instance_id="w1",
        fencing_token=backup_scheduler.allocate_fencing_token(),
    )
    with lease:
        assert lease.path.is_file()
        assert lease.acquired is True
    assert not lease.path.exists()
    assert lease.acquired is False


def test_writer_lease_renew_rejects_loss_and_release_keeps_foreign(tmp_path: Path) -> None:
    lease = _writer(tmp_path)
    lease.path.unlink()
    _writer(tmp_path, run="run_other", token=999)
    with pytest.raises(AppError) as exc:
        lease.renew()
    assert exc.value.status == 409
    lease.release()
    assert lease.path.is_file()
    payload = json.loads(lease.path.read_text(encoding="utf-8"))
    assert payload["ownerRunId"] == "run_other"


def test_catalog_precondition_snapshot(tmp_settings: Path, tmp_path: Path) -> None:
    snapshot = backup_catalog.catalog_precondition(tmp_path)
    assert snapshot.expected_head_hash == backup_catalog.GENESIS_HASH
    assert snapshot.expected_target_generation == 0
    backup_catalog.append_receipt(tmp_path, _receipt("backup_1"))
    after = backup_catalog.catalog_precondition(tmp_path)
    assert after.expected_head_hash != backup_catalog.GENESIS_HASH


def test_retry_permission_exhausts_and_reraises() -> None:
    from deepseek_infra.infra.workspace.backup_writer_lease import _retry_permission

    calls = []

    def always_locked() -> None:
        calls.append(1)
        raise PermissionError("locked")

    with pytest.raises(PermissionError):
        _retry_permission(always_locked, attempts=3, sleep_seconds=0.001)
    assert len(calls) == 3

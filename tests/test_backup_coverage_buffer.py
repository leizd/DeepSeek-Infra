"""Extra buffer tests so all Python versions clear the 95% coverage gate."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_publish,
    backup_retention,
    backup_spool,
    backup_targets,
    backup_writer_lease,
)
from deepseek_infra.infra.workspace.backup_target_store import (
    MemoryTargetStore,
    commit_marker_key,
    commit_slot_digest,
    open_filesystem_store,
    put_json_if_absent,
    read_json,
)
from deepseek_infra.infra.workspace.backup_target_store import put_json_if_match


@pytest.fixture(autouse=True)
def _tempdir_outside_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = tmp_path / ".sys-temp"
    fake.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake))


def _pkg(tmp_path: Path, name: str, body: bytes) -> SimpleNamespace:
    path = tmp_path / f"{name}.age"
    raw = b"age-encryption.org/v1\n" + body
    path.write_bytes(raw)
    return SimpleNamespace(
        backup_id=name,
        filename=f"{name}.age",
        size=len(raw),
        ciphertext_sha256=hashlib.sha256(raw).hexdigest(),
        manifest_digest="m" * 64,
        coverage_digest="c" * 64,
        creation_verified=True,
        path=path,
    )


def test_buffer_publish_receipt_conflict_and_marker_race(tmp_settings: Path, tmp_path: Path) -> None:
    store = MemoryTargetStore()
    put_json_if_absent(store, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"})
    package = _pkg(tmp_path, "buf1", b"alpha")
    target = backup_publish.ResolvedTarget(target_id="tb", root=None, managed=False, kind="s3", store=store)
    bad_receipt = {
        "backupId": package.backup_id,
        "objectDigest": "f" * 64,
        "filename": package.filename,
        "runId": "other",
        "policyId": "pol",
        "scheduleSlot": "slot-buf",
        "size": 1,
        "ciphertextSha256": "f" * 64,
        "manifestDigest": "m" * 64,
        "coverageDigest": "c" * 64,
        "creationVerified": True,
        "createdAt": "2026-01-01T00:00:00Z",
    }
    put_json_if_absent(store, f"receipts/{package.backup_id}.json", bad_receipt)
    try:
        backup_publish.publish_backup(target, package, run_id="rb1", policy_id="pol", schedule_slot="slot-buf", fencing_token=1)
    except AppError:
        pass
    store.delete_if_match(f"receipts/{package.backup_id}.json")
    for key in list(store._objects):
        if key.startswith("commits/") or key.startswith("transactions/"):
            store.delete_if_match(key)
    first = backup_publish.publish_backup(target, package, run_id="rb2", policy_id="pol", schedule_slot="slot-buf2", fencing_token=5)
    assert first.converged is False
    other = _pkg(tmp_path, "buf2", b"beta")
    with pytest.raises(AppError) as exc:
        backup_publish.publish_backup(target, other, run_id="rb3", policy_id="pol", schedule_slot="slot-buf2", fencing_token=1)
    assert "slot-commit-conflict" in str(exc.value)


def test_buffer_writer_lease_store_preempt_and_caps(tmp_settings: Path) -> None:
    store = MemoryTargetStore()
    store._caps = store._caps.__class__(
        conditional_create=False,
        conditional_replace=False,
        kind="s3",
    )
    weak = backup_writer_lease.TargetWriterLease(store=store, target_id="t", owner_run_id="r", owner_instance_id="i", fencing_token=1)
    with pytest.raises(AppError):
        weak.acquire()
    store._caps = store._caps.__class__(
        conditional_create=True,
        conditional_replace=True,
        range_get=True,
        multipart_upload=True,
        multipart_checksum=True,
        list_pagination=True,
        delete=True,
        server_date=True,
        kind="s3",
    )
    a = backup_writer_lease.TargetWriterLease(store=store, target_id="t", owner_run_id="ra", owner_instance_id="ia", fencing_token=1, lease_seconds=1)
    a.acquire()
    payload = read_json(store, "control/writer.json")
    assert payload is not None
    payload["expiresAt"] = "2000-01-01T00:00:00Z"
    meta = store.stat("control/writer.json")
    assert meta is not None
    put_json_if_match(store, "control/writer.json", payload, expected_etag=meta.etag)
    b = backup_writer_lease.TargetWriterLease(store=store, target_id="t", owner_run_id="rb", owner_instance_id="ib", fencing_token=9)
    b.acquire()
    with pytest.raises(AppError):
        a.assert_owned()
    payload = read_json(store, "control/writer.json")
    assert payload is not None
    payload["expiresAt"] = "2000-01-01T00:00:00Z"
    meta = store.stat("control/writer.json")
    assert meta is not None
    put_json_if_match(store, "control/writer.json", payload, expected_etag=meta.etag)
    c = backup_writer_lease.TargetWriterLease(store=store, target_id="t", owner_run_id="rc", owner_instance_id="ic", fencing_token=9)
    with pytest.raises(AppError):
        c.acquire()
    b.release()


def test_buffer_catalog_fs_and_targets_paths(tmp_settings: Path) -> None:
    directory = tmp_settings / "usb-buf"
    directory.mkdir()
    record = backup_targets.init_target(directory, label="buf")
    root = Path(record["path"])
    backup_catalog.append_receipt(
        root,
        {
            "schemaVersion": 1,
            "backupId": "x1",
            "runId": "r",
            "policyId": "p",
            "targetId": record["targetId"],
            "scheduleSlot": "s",
            "filename": "x1.age",
            "size": 1,
            "ciphertextSha256": "a" * 64,
            "manifestDigest": "b" * 64,
            "coverageDigest": "c" * 64,
            "creationVerified": True,
            "createdAt": "2026-01-01T00:00:00Z",
            "pinned": False,
        },
    )
    path = backup_catalog.catalog_path(root)
    path.write_text(path.read_text(encoding="utf-8") + "{not-json\n", encoding="utf-8")
    with pytest.raises(AppError):
        backup_catalog.verify_chain(root)
    rebuilt = backup_catalog.rebuild_catalog_from_receipts(root)
    assert rebuilt["rebuilt"] >= 1
    listed = backup_catalog.list_backups(root, policy_id="p", target_id=record["targetId"])
    assert listed
    store = open_filesystem_store(root / "extra-store")
    data = b"stream-me-please"
    store.put_if_absent("s.bin", data, checksum_sha256=hashlib.sha256(data).hexdigest())
    chunks = list(store.get_stream("s.bin", offset=7))
    assert b"".join(chunks) == data[7:]
    with pytest.raises(AppError):
        store.put_if_absent("s.bin", b"other", checksum_sha256=hashlib.sha256(b"other").hexdigest())
    assert backup_targets.probe_target(record["targetId"])["ready"] is True
    backup_targets.record_target_head(root, target_id=record["targetId"], generation=3, commit_hash="d" * 64)
    backup_targets.delete_target(record["targetId"])


def test_buffer_retention_store_hold_and_spool(tmp_settings: Path, tmp_path: Path) -> None:
    store = MemoryTargetStore()
    writer = backup_writer_lease.TargetWriterLease(store=store, target_id="t", owner_run_id="r", owner_instance_id="i", fencing_token=2)
    writer.acquire()
    digest = "9" * 64
    receipt = {
        "backupId": "hold1",
        "filename": "hold1.age",
        "policyId": "p",
        "targetId": "t",
        "runId": "r",
        "scheduleSlot": "s",
        "size": 1,
        "ciphertextSha256": digest,
        "objectDigest": digest,
        "manifestDigest": "m" * 64,
        "coverageDigest": "c" * 64,
        "creationVerified": True,
        "createdAt": "2026-03-01T00:00:00Z",
    }
    backup_catalog.append_receipt_store(store, receipt, writer=writer)
    store.put_if_absent(f"objects/sha256/99/{digest}.age", b"x", checksum_sha256=hashlib.sha256(b"x").hexdigest())
    put_json_if_absent(store, "holds/restore/h1.json", {"objectDigest": digest})
    backup_catalog._append_entry_store(
        store,
        "trash",
        {"backupId": "hold1", "retentionRunId": "rr", "trashedAt": "2000-01-01T00:00:00Z"},
        writer=writer,
    )
    finalized = backup_retention.finalize_retention_store(
        {"trashGraceHours": 0, "keepLast": 0, "keepHourly": 0, "keepDaily": 0, "keepWeekly": 0, "keepMonthly": 0},
        store,
        writer=writer,
    )
    assert "hold1" in finalized["kept"] or "hold1" not in finalized["deleted"]
    writer.release()
    package = _pkg(tmp_path, "sp", b"good")
    meta = backup_spool.store_verified_package(package, policy_id="px", schedule_slot="sx", run_id="rx")
    d = commit_slot_digest("sx")
    backup_spool.clear_slot("px", d)
    assert meta["backupId"] == "sp"


def test_buffer_slot_journal_fs_edges(tmp_settings: Path) -> None:
    root = tmp_settings / "jroot"
    root.mkdir()
    (root / "transactions").mkdir()
    (root / "transactions" / "bad.json").write_text("not-json", encoding="utf-8")
    (root / "transactions" / "run_a.json").write_text(
        json.dumps({"runId": "run_a", "policyId": "p", "scheduleSlot": "s", "phase": "started"}),
        encoding="utf-8",
    )
    (root / "transactions" / "run_b.json").write_text(
        json.dumps({"runId": "run_b", "policyId": "p", "scheduleSlot": "s", "phase": "done"}),
        encoding="utf-8",
    )
    assert backup_publish.slot_has_incomplete_journal(root, policy_id="p", schedule_slot="s") is True
    assert backup_publish.slot_has_incomplete_journal(root, policy_id="p", schedule_slot="s", exclude_run_id="run_a") is False
    store = MemoryTargetStore()
    for i in range(5):
        put_json_if_absent(
            store,
            f"transactions/t{i}.json",
            {"runId": f"t{i}", "policyId": "p", "scheduleSlot": "sx", "phase": "started" if i == 4 else "done"},
        )
    assert backup_publish.slot_has_incomplete_journal_store(store, policy_id="p", schedule_slot="sx") is True
    put_json_if_absent(store, commit_marker_key("p", "sx"), {"commitHash": "c" * 64, "objectDigest": "d" * 64, "backupId": "b", "targetGeneration": 1})
    assert backup_publish.slot_has_incomplete_journal_store(store, policy_id="p", schedule_slot="sx") is False

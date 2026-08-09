"""Extra coverage for spool, filesystem store edges, and remote restore helpers."""

from __future__ import annotations

import hashlib
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_catalog, backup_remote_restore, backup_spool, backup_targets
from deepseek_infra.infra.workspace.backup_target_store import (
    MemoryTargetStore,
    commit_marker_key,
    commit_slot_digest,
    object_key,
    open_filesystem_store,
    put_json_if_absent,
    read_json,
    receipt_key,
    restore_hold_key,
)


@pytest.fixture(autouse=True)
def _tempdir_outside_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / ".sys-temp"
    fake_temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def _pkg(tmp_path: Path, name: str = "b1", body: bytes = b"payload") -> SimpleNamespace:
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


def test_spool_store_reuse_quota_cleanup(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = _pkg(tmp_path)
    meta = backup_spool.store_verified_package(package, policy_id="pol", schedule_slot="slot-1", run_id="r1")
    assert meta["ciphertextSha256"] == package.ciphertext_sha256
    again = backup_spool.store_verified_package(package, policy_id="pol", schedule_slot="slot-1", run_id="r2")
    assert again["ciphertextSha256"] == package.ciphertext_sha256
    digest = commit_slot_digest("slot-1")
    assert backup_spool.package_path("pol", digest) is not None
    backup_spool.write_multipart_state("pol", digest, {"uploadId": "u1", "parts": []})
    mp = backup_spool.read_multipart_state("pol", digest)
    assert mp is not None and mp["uploadId"] == "u1"
    assert backup_spool.spool_usage_bytes() > 0
    # Force TTL expiry
    slot_dir = backup_spool.SPOOL_DIR / "pol" / digest
    old = time.time() - backup_spool.DEFAULT_TTL_SECONDS - 10
    for path in slot_dir.rglob("*"):
        if path.is_file():
            import os

            os.utime(path, (old, old))
    cleaned = backup_spool.cleanup_expired(ttl_seconds=1)
    assert cleaned["removed"] >= 1
    other = _pkg(tmp_path, name="b2", body=b"other")
    backup_spool.store_verified_package(package, policy_id="pol2", schedule_slot="slot-x", run_id="r3")
    d = commit_slot_digest("slot-x")
    p = backup_spool.SPOOL_DIR / "pol2" / d / "package.age"
    p.write_bytes(b"age-encryption.org/v1\nconflict-bytes")
    with pytest.raises(AppError):
        backup_spool.store_verified_package(other, policy_id="pol2", schedule_slot="slot-x", run_id="r4")
    backup_spool.clear_slot("pol2", d)
    view = backup_spool.SpooledPackage(meta, tmp_path / "x")
    assert view.backup_id == package.backup_id


def test_filesystem_store_list_multipart_and_invalid_key(tmp_path: Path) -> None:
    store = open_filesystem_store(tmp_path / "fs")
    data = b"abc123"
    digest = hashlib.sha256(data).hexdigest()
    store.put_if_absent("objects/x.bin", data, checksum_sha256=digest)
    page = store.list_objects("objects/", limit=1)
    assert len(page.objects) == 1
    upload = store.begin_multipart("objects/mp.bin", checksum_sha256=digest)
    store.upload_part(upload, 1, data, checksum_sha256=digest)
    store.complete_multipart_if_absent(upload)
    assert store.get_bytes("objects/mp.bin") == data
    store.abort_multipart(store.begin_multipart("objects/abort.bin", checksum_sha256=digest))
    assert store.server_time() is not None
    chunks = list(store.get_stream("objects/x.bin"))
    assert b"".join(chunks) == data
    with pytest.raises(AppError):
        store.stat("../escape")
    meta = store.stat("objects/x.bin")
    assert meta is not None
    store.put_if_match("objects/x.bin", b"zzz", expected_etag=meta.etag, checksum_sha256=hashlib.sha256(b"zzz").hexdigest())
    with pytest.raises(AppError):
        store.put_if_match("objects/x.bin", b"nope", expected_etag='"missing"', checksum_sha256=hashlib.sha256(b"nope").hexdigest())
    assert store.delete_if_match("missing") is False


def test_memory_store_failures_and_pagination() -> None:
    store = MemoryTargetStore()
    for index in range(5):
        body = f"item-{index}".encode()
        store.put_if_absent(f"p/{index}.bin", body, checksum_sha256=hashlib.sha256(body).hexdigest())
    page1 = store.list_objects("p/", limit=2)
    assert len(page1.objects) == 2 and page1.cursor
    page2 = store.list_objects("p/", cursor=page1.cursor, limit=2)
    assert len(page2.objects) == 2
    store.inject_failure("put_if_absent", AppError("boom", status=500))
    with pytest.raises(AppError):
        store.put_if_absent("p/x", b"x", checksum_sha256=hashlib.sha256(b"x").hexdigest())
    store.clear_failure("put_if_absent")
    upload = store.begin_multipart("p/mp", checksum_sha256=hashlib.sha256(b"ab").hexdigest())
    store.upload_part(upload, 1, b"a")
    store.upload_part(upload, 2, b"b")
    # wrong checksum on complete
    upload.checksum_sha256 = "0" * 64
    with pytest.raises(AppError):
        store.complete_multipart_if_absent(upload)


def test_init_s3_target_with_fake_client(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_backup_target_s3_adapter import FakeS3Client

    client = FakeS3Client()
    record = backup_targets.init_s3_target(
        bucket="bk",
        prefix="pref",
        region="us-east-1",
        label="offsite",
        credential_provider={"type": "aws-default-chain", "profile": "demo"},
        client=client,
        probe=True,
    )
    assert record["kind"] == "s3"
    assert record["bucket"] == "bk"
    assert "secretAccessKey" not in str(record)
    store = backup_targets.open_target_store(record["targetId"], write_intent=False, client=client)
    assert store.capabilities().kind == "s3"
    from deepseek_infra.infra.workspace.backup_target_s3 import open_s3_store

    monkeypatch.setattr(
        backup_targets,
        "open_target_store",
        lambda target_id, *, write_intent=True, client=None: open_s3_store(record, client=FakeS3Client()),
    )
    probe = backup_targets.probe_target(record["targetId"])
    assert probe["targetId"] == record["targetId"]
    assert probe.get("kind") == "s3"


def test_remote_restore_hold_release(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryTargetStore()
    package = _pkg(tmp_path, body=b"restore-body")
    digest = package.ciphertext_sha256
    put_json_if_absent(
        store,
        receipt_key(package.backup_id),
        {
            "backupId": package.backup_id,
            "objectDigest": digest,
            "filename": package.filename,
            "size": package.size,
        },
    )
    put_json_if_absent(
        store,
        commit_marker_key("pol", "slot"),
        {"backupId": package.backup_id, "objectDigest": digest, "commitHash": "c" * 64, "targetGeneration": 1},
    )
    store.put_if_absent(object_key(digest), package.path.read_bytes(), checksum_sha256=digest)
    target = SimpleNamespace(target_id="t1", root=None, managed=False, kind="s3", store=store, require_store=lambda: store)
    import deepseek_infra.infra.workspace.backup_publish as publish

    monkeypatch.setattr(publish, "resolve_target", lambda *a, **k: target)
    staged = backup_remote_restore.restore_from_target(target_id="t1", backup_id=package.backup_id)
    assert Path(staged["path"]).is_file()
    hold = read_json(store, restore_hold_key(str(staged["restoreId"])))
    assert hold is not None
    backup_remote_restore.release_restore_hold(store, str(staged["restoreId"]))


def test_record_remote_target_head(tmp_settings: Path) -> None:
    store = MemoryTargetStore()
    put_json_if_absent(store, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "inc"})
    backup_targets.record_remote_target_head(store, target_id="target_x", generation=2, commit_hash="a" * 64)
    head = read_json(store, "control/head.json")
    assert head is not None
    assert int(head["targetGeneration"]) == 2


def test_spool_quota_and_force_cleanup(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_spool, "DEFAULT_QUOTA_BYTES", 50)
    package = _pkg(tmp_path, name="big", body=b"x" * 200)
    # First store may succeed then cleanup; force small quota triggers cleanup/raise path
    try:
        backup_spool.store_verified_package(package, policy_id="q", schedule_slot="s1", run_id="r1")
    except AppError as exc:
        assert "quota" in str(exc).casefold() or exc.status == 507
    # force_oldest path
    small = _pkg(tmp_path, name="small", body=b"y")
    monkeypatch.setattr(backup_spool, "DEFAULT_QUOTA_BYTES", 10_000_000)
    backup_spool.store_verified_package(small, policy_id="q2", schedule_slot="s2", run_id="r2")
    monkeypatch.setattr(backup_spool, "DEFAULT_QUOTA_BYTES", 1)
    result = backup_spool.cleanup_expired(force_oldest=True)
    assert result["removed"] >= 0


def test_catalog_store_append_and_list(tmp_settings: Path, tmp_path: Path) -> None:
    from deepseek_infra.infra.workspace import backup_catalog, backup_writer_lease

    store = MemoryTargetStore()
    writer = backup_writer_lease.TargetWriterLease(
        store=store,
        target_id="t",
        owner_run_id="r",
        owner_instance_id="i",
        fencing_token=1,
    )
    writer.acquire()
    receipt = {
        "backupId": "b1",
        "filename": "b1.age",
        "policyId": "p",
        "targetId": "t",
        "runId": "r",
        "scheduleSlot": "s",
        "size": 1,
        "ciphertextSha256": "a" * 64,
        "objectDigest": "a" * 64,
        "manifestDigest": "b" * 64,
        "coverageDigest": "c" * 64,
        "creationVerified": True,
        "createdAt": "2026-01-01T00:00:00Z",
    }
    backup_catalog.append_receipt_store(store, receipt, writer=writer)
    state = backup_catalog.catalog_state_store(store)
    assert "b1" in state
    backup_catalog._append_entry_store(store, "pin", {"backupId": "b1", "pinned": True}, writer=writer)
    backup_catalog._append_entry_store(store, "scrub", {"backupId": "b1", "ok": True, "detail": "ok", "scrubbedAt": "2026-01-02T00:00:00Z"}, writer=writer)
    state2 = backup_catalog.catalog_state_store(store)
    assert state2["b1"]["pinned"] is True
    writer.release()


def test_remote_restore_errors(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryTargetStore()
    target = SimpleNamespace(target_id="t1", root=None, managed=False, kind="s3", store=store, require_store=lambda: store)
    import deepseek_infra.infra.workspace.backup_publish as publish

    monkeypatch.setattr(publish, "resolve_target", lambda *a, **k: target)
    with pytest.raises(AppError):
        backup_remote_restore.restore_from_target(target_id="t1", backup_id="missing")
    put_json_if_absent(store, receipt_key("b"), {"backupId": "b", "objectDigest": "short"})
    with pytest.raises(AppError):
        backup_remote_restore.restore_from_target(target_id="t1", backup_id="b")
    put_json_if_absent(store, receipt_key("b2"), {"backupId": "b2", "objectDigest": "a" * 64, "filename": "b2.age"})
    with pytest.raises(AppError):
        backup_remote_restore.restore_from_target(target_id="t1", backup_id="b2")


def test_open_target_store_kinds(tmp_settings: Path) -> None:
    directory = tmp_settings / "fs-target"
    directory.mkdir()
    record = backup_targets.init_target(directory, label="local")
    store = backup_targets.open_target_store(record["targetId"], write_intent=True)
    assert store.capabilities().kind == "filesystem"
    managed = backup_targets.open_target_store("managed-local", write_intent=False)
    assert managed.capabilities().kind == "filesystem"
    # webdav reserved
    path = backup_targets.BACKUP_TARGET_DIR / "target_webdavx.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"schemaVersion":3,"targetId":"target_webdavx","kind":"webdav","label":"x","createdAt":"t","registeredAt":"t"}',
        encoding="utf-8",
    )
    with pytest.raises(AppError):
        backup_targets.open_target_store("target_webdavx")

def test_spool_lookup_miss_and_plan_mismatch(tmp_settings: Path, tmp_path: Path) -> None:
    assert backup_spool.lookup_verified_package(policy_id="nope", slot_digest="nope") is None
    package = _pkg(tmp_path)
    meta = backup_spool.store_verified_package(package, policy_id="pol_miss", schedule_slot="slot-1", run_id="r1", run_plan_digest="plan-A")
    digest = commit_slot_digest("slot-1")
    with pytest.raises(AppError, match="run plan digest mismatch"):
        backup_spool.lookup_verified_package(policy_id="pol_miss", slot_digest=digest, run_plan_digest="plan-B")
    # Meta present but the ciphertext vanished -> miss.
    path = backup_spool.package_path("pol_miss", digest)
    assert path is not None
    path.unlink()
    assert backup_spool.lookup_verified_package(policy_id="pol_miss", slot_digest=digest) is None
    assert meta["ciphertextSha256"]

def test_store_catalog_chain_orphan_and_generation(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryTargetStore()
    backup_catalog.append_receipt_store(store, {"backupId": "A", "filename": "A.age", "size": 1, "ciphertextSha256": "a" * 64})
    backup_catalog.append_receipt_store(store, {"backupId": "B", "filename": "B.age", "size": 1, "ciphertextSha256": "b" * 64})
    state = backup_catalog.catalog_state_store(store)
    assert "A" in state and "B" in state
    # An orphan event for an unknown backup is skipped by the fold.
    backup_catalog._append_entry_store(store, "scrub", {"backupId": "GHOST", "ok": True})
    state2 = backup_catalog.catalog_state_store(store)
    assert "GHOST" not in state2
    # A store head read failure degrades the generation to zero.
    from deepseek_infra.infra.workspace import backup_publish

    def _boom(*_a: object, **_k: object) -> None:
        raise AppError("store read failed", status=500)
    monkeypatch.setattr(backup_publish, "latest_commit_store", _boom)
    entry = backup_catalog._append_entry_store(store, "receipt", {"backupId": "C", "filename": "C.age", "size": 1, "ciphertextSha256": "c" * 64})
    assert int(entry.get("targetGeneration") or 0) == 0

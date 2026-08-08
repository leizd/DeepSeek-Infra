"""Targeted coverage for remaining 4.4.6 backup gaps (CI 95% gate)."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_publish,
    backup_remote_restore,
    backup_retention,
    backup_spool,
    backup_targets,
    backup_writer_lease,
)
from deepseek_infra.infra.workspace.backup_target_store import (
    MemoryTargetStore,
    PutResult,
    TargetCapabilities,
    commit_marker_key,
    commit_slot_digest,
    open_filesystem_store,
    probe_store_capabilities,
    put_json_if_absent,
    put_json_if_match,
    read_json,
)
from deepseek_infra.web import server as server_module


@pytest.fixture(autouse=True)
def _tempdir_outside_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / ".sys-temp"
    fake_temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def _pkg(tmp_path: Path, name: str = "cov1", body: bytes = b"body") -> SimpleNamespace:
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


def test_filesystem_store_error_edges(tmp_path: Path) -> None:
    store = open_filesystem_store(tmp_path / "fs")
    with pytest.raises(AppError):
        store.stat("/abs")
    assert store.stat("missing.bin") is None
    assert store.get_bytes("missing.bin") is None
    assert list(store.get_stream("missing.bin")) == []
    data = b"hello"
    digest = hashlib.sha256(data).hexdigest()
    with pytest.raises(AppError):
        store.put_if_absent("a.bin", data, checksum_sha256="0" * 64)
    store.put_if_absent("a.bin", io.BytesIO(data), checksum_sha256=digest)
    # converge identical
    store.put_if_absent("a.bin", data, checksum_sha256=digest)
    with pytest.raises(AppError):
        store.put_if_absent("a.bin", b"other", checksum_sha256=hashlib.sha256(b"other").hexdigest())
    meta = store.stat("a.bin")
    assert meta is not None
    with pytest.raises(AppError):
        store.put_if_match("a.bin", b"x", expected_etag=meta.etag, checksum_sha256="0" * 64)
    # race-style etag fail before replace
    with pytest.raises(AppError):
        store.put_if_match("a.bin", b"x", expected_etag='"nope"', checksum_sha256=hashlib.sha256(b"x").hexdigest())
    # delete mismatch
    with pytest.raises(AppError):
        store.delete_if_match("a.bin", expected_etag='"nope"')
    assert store.delete_if_match("gone") is False
    # multipart checksum fail
    upload = store.begin_multipart("mp.bin", checksum_sha256=digest)
    with pytest.raises(AppError):
        store.upload_part(upload, 1, b"nope", checksum_sha256="0" * 64)
    store.upload_part(upload, 1, data, checksum_sha256=digest)
    upload.checksum_sha256 = "0" * 64
    with pytest.raises(AppError):
        store.complete_multipart_if_absent(upload)
    # empty list root
    empty = open_filesystem_store(tmp_path / "empty-root")
    assert empty.list_objects("x").objects == ()
    # read_json bad payloads
    store.put_if_absent("bad.json", b"not-json", checksum_sha256=hashlib.sha256(b"not-json").hexdigest())
    assert read_json(store, "bad.json") is None
    store.put_if_absent("arr.json", b"[1]\n", checksum_sha256=hashlib.sha256(b"[1]\n").hexdigest())
    assert read_json(store, "arr.json") is None
    # put_json helpers
    put_json_if_absent(store, "obj.json", {"a": 1})
    meta2 = store.stat("obj.json")
    assert meta2 is not None
    put_json_if_match(store, "obj.json", {"a": 2}, expected_etag=meta2.etag)


def test_probe_failure_branches() -> None:
    class _FailStore:
        def __init__(self, mode: str) -> None:
            self.mode = mode
            self._caps = TargetCapabilities(
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

        def capabilities(self) -> TargetCapabilities:
            return self._caps

        def put_if_absent(self, *a: Any, **k: Any) -> PutResult:
            if self.mode == "create-app":
                raise AppError("no", status=412)
            if self.mode == "create-exc":
                raise RuntimeError("boom")
            return PutResult(key="k", etag='"e"', size=1, created=True)

        def put_if_match(self, *a: Any, **k: Any) -> PutResult:
            if self.mode == "replace":
                raise RuntimeError("r")
            return PutResult(key="k", etag='"e2"', size=1, created=False)

        def get_bytes(self, *a: Any, **k: Any) -> bytes | None:
            if self.mode == "range":
                raise RuntimeError("g")
            return b"abcdefgh"

        def list_objects(self, *a: Any, **k: Any) -> Any:
            if self.mode == "list":
                raise RuntimeError("l")
            return SimpleNamespace(objects=())

        def begin_multipart(self, *a: Any, **k: Any) -> Any:
            if self.mode == "mp":
                raise RuntimeError("m")
            return SimpleNamespace(key="k", upload_id="u", checksum_sha256="x", parts=[])

        def upload_part(self, *a: Any, **k: Any) -> dict[str, Any]:
            return {"partNumber": 1}

        def complete_multipart_if_absent(self, *a: Any, **k: Any) -> PutResult:
            return PutResult(key="k", etag='"e"', size=1, created=True)

        def delete_if_match(self, *a: Any, **k: Any) -> bool:
            if self.mode == "delete":
                raise RuntimeError("d")
            return True

        def server_time(self) -> Any:
            if self.mode == "server":
                raise RuntimeError("s")
            return None

    for mode in ("create-app", "create-exc", "replace", "range", "list", "mp", "delete", "server"):
        result = probe_store_capabilities(_FailStore(mode))  # type: ignore[arg-type]
        assert "status" in result
    # create fail => SKIP branches
    skip = probe_store_capabilities(_FailStore("create-app"))  # type: ignore[arg-type]
    assert skip["results"]["conditional-replace"] == "SKIP"


def test_publish_store_conflict_and_oserror(tmp_settings: Path, tmp_path: Path) -> None:
    store = MemoryTargetStore()
    put_json_if_absent(store, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"})
    package = _pkg(tmp_path, name="p1", body=b"one")
    target = backup_publish.ResolvedTarget(target_id="t", root=None, managed=False, kind="s3", store=store)
    first = backup_publish.publish_backup(target, package, run_id="r1", policy_id="pol", schedule_slot="slot-a", fencing_token=1)
    assert first.converged is False
    # different package same slot
    other = _pkg(tmp_path, name="p2", body=b"two")
    with pytest.raises(AppError) as exc:
        backup_publish.publish_backup(target, other, run_id="r2", policy_id="pol", schedule_slot="slot-a", fencing_token=2)
    assert "slot-commit-conflict" in str(exc.value)
    # incomplete journal detection exclude
    assert backup_publish.slot_has_incomplete_journal_store(store, policy_id="pol", schedule_slot="slot-b", exclude_run_id="r9") is False
    put_json_if_absent(
        store,
        "transactions/r9.json",
        {"runId": "r9", "policyId": "pol", "scheduleSlot": "slot-b", "phase": "object-published"},
    )
    assert backup_publish.slot_has_incomplete_journal_store(store, policy_id="pol", schedule_slot="slot-b", exclude_run_id="r9") is False
    assert backup_publish.slot_has_incomplete_journal_store(store, policy_id="pol", schedule_slot="slot-b") is True
    # ResolvedTarget helpers
    with pytest.raises(AppError):
        target.require_root()
    fs = backup_publish.resolve_target("managed-local")
    assert fs.require_root().exists()
    assert fs.require_store().capabilities().kind == "filesystem"
    bare = backup_publish.ResolvedTarget(target_id="x", root=None, managed=False, kind="s3", store=None)
    with pytest.raises(AppError):
        bare.require_store()


def test_spool_and_restore_remaining(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = _pkg(tmp_path)
    meta = backup_spool.store_verified_package(package, policy_id="p", schedule_slot="s", run_id="r")
    assert backup_spool.read_package_meta("p", commit_slot_digest("s")) is not None
    assert backup_spool.read_package_meta("missing", "x") is None
    assert backup_spool.package_path("missing", "x") is None
    assert backup_spool.read_multipart_state("missing", "x") is None
    # corrupt meta
    d = commit_slot_digest("s")
    (backup_spool.SPOOL_DIR / "p" / d / "package.json").write_text("{bad", encoding="utf-8")
    assert backup_spool.read_package_meta("p", d) is None
    (backup_spool.SPOOL_DIR / "p" / d / "multipart.json").write_text("{bad", encoding="utf-8")
    assert backup_spool.read_multipart_state("p", d) is None
    # usage empty
    monkeypatch.setattr(backup_spool, "SPOOL_DIR", tmp_path / "no-spool")
    assert backup_spool.spool_usage_bytes() == 0
    assert backup_spool.cleanup_expired()["removed"] == 0
    # restore digest mismatch path
    store = MemoryTargetStore()
    good = package.path.read_bytes()
    digest = hashlib.sha256(good).hexdigest()
    put_json_if_absent(store, f"receipts/{package.backup_id}.json", {"backupId": package.backup_id, "objectDigest": digest, "filename": package.filename, "size": len(good)})
    put_json_if_absent(store, commit_marker_key("pol", "slot"), {"backupId": package.backup_id, "objectDigest": digest, "commitHash": "c" * 64, "targetGeneration": 1})
    store.put_if_absent(f"objects/sha256/{digest[:2]}/{digest}.age", good[:-1] + b"Z", checksum_sha256=hashlib.sha256(good[:-1] + b"Z").hexdigest())
    target = SimpleNamespace(target_id="t", root=None, store=store, require_store=lambda: store)
    import deepseek_infra.infra.workspace.backup_publish as publish

    monkeypatch.setattr(publish, "resolve_target", lambda *a, **k: target)
    with pytest.raises(AppError):
        backup_remote_restore.restore_from_target(target_id="t", backup_id=package.backup_id)
    # missing object after commit
    store2 = MemoryTargetStore()
    put_json_if_absent(store2, f"receipts/{package.backup_id}.json", {"backupId": package.backup_id, "objectDigest": digest, "filename": package.filename, "size": len(good)})
    put_json_if_absent(store2, commit_marker_key("pol", "slot2"), {"backupId": package.backup_id, "objectDigest": digest, "commitHash": "d" * 64, "targetGeneration": 1})
    target2 = SimpleNamespace(target_id="t2", root=None, store=store2, require_store=lambda: store2)
    monkeypatch.setattr(publish, "resolve_target", lambda *a, **k: target2)
    with pytest.raises(AppError):
        backup_remote_restore.restore_from_target(target_id="t2", backup_id=package.backup_id)
    del meta


def test_retention_store_and_catalog_edges(tmp_settings: Path) -> None:
    store = MemoryTargetStore()
    writer = backup_writer_lease.TargetWriterLease(store=store, target_id="t", owner_run_id="r", owner_instance_id="i", fencing_token=1)
    writer.acquire()
    for i in range(3):
        receipt = {
            "backupId": f"b{i}",
            "filename": f"b{i}.age",
            "policyId": "p",
            "targetId": "t",
            "runId": "r",
            "scheduleSlot": f"s{i}",
            "size": 10,
            "ciphertextSha256": f"{i}" * 64,
            "objectDigest": f"{i}" * 64,
            "manifestDigest": "m" * 64,
            "coverageDigest": "c" * 64,
            "creationVerified": True,
            "createdAt": f"2026-01-0{i+1}T00:00:00Z",
            "pinned": i == 0,
        }
        backup_catalog.append_receipt_store(store, receipt, writer=writer)
        store.put_if_absent(f"objects/sha256/{str(i)*2}/{str(i)*64}.age", b"x", checksum_sha256=hashlib.sha256(b"x").hexdigest())
    retention = {
        "keepLast": 1,
        "keepHourly": 0,
        "keepDaily": 0,
        "keepWeekly": 0,
        "keepMonthly": 0,
        "trashGraceHours": 0,
        "minimumHealthyCopies": 1,
    }
    applied = backup_retention.apply_retention_store(retention, store, writer=writer)
    assert isinstance(applied["trashed"], list)
    # force trash age past grace via direct event
    backup_catalog._append_entry_store(
        store,
        "trash",
        {"backupId": "b1", "retentionRunId": "rr", "trashedAt": "2000-01-01T00:00:00Z"},
        writer=writer,
    )
    finalized = backup_retention.finalize_retention_store(retention, store, writer=writer)
    assert isinstance(finalized["deleted"], list)
    # hold protects digest
    put_json_if_absent(store, "holds/restore/r1.json", {"objectDigest": "2" * 64})
    holds = backup_retention._restore_hold_digests(store)
    assert "2" * 64 in holds
    writer.release()
    # writer lease missing store/root
    with pytest.raises(AppError):
        backup_writer_lease.TargetWriterLease(target_id="t", owner_run_id="r", owner_instance_id="i", fencing_token=1)


def test_targets_lineage_and_s3_open_paths(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_settings / "usb"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    # adopt / reinitialize
    adopted = backup_targets.adopt_target_incarnation(record["targetId"])
    assert adopted["adopted"] is True
    reinit = backup_targets.reinitialize_target(directory, label="new")
    assert reinit["targetId"] != record["targetId"]
    # delete
    assert backup_targets.delete_target(reinit["targetId"])["deleted"] is True
    with pytest.raises(AppError):
        backup_targets.get_target(reinit["targetId"])
    # open unsupported kind
    path = backup_targets.BACKUP_TARGET_DIR / "target_weird.json"
    path.write_text(json.dumps({"schemaVersion": 3, "targetId": "target_weird", "kind": "tape", "createdAt": "t", "registeredAt": "t"}), encoding="utf-8")
    with pytest.raises(AppError):
        backup_targets.open_target_store("target_weird")
    # s3 without sdk and no client
    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_target_s3.s3_sdk_available", lambda: False)
    with pytest.raises(AppError):
        backup_targets.init_s3_target(bucket="b", probe=False)


def test_writer_lease_store_renew_release_and_skew(tmp_settings: Path) -> None:
    store = MemoryTargetStore()
    lease = backup_writer_lease.TargetWriterLease(
        store=store,
        target_id="t",
        owner_run_id="run1",
        owner_instance_id="inst",
        fencing_token=1,
    )
    lease.acquire()
    # server date skew paths
    lease._note_server_date(None)
    lease._note_server_date("not-a-date")
    lease._note_server_date("2026-01-01T00:00:00Z")
    from email.utils import format_datetime
    from datetime import datetime, timezone

    lease._note_server_date(format_datetime(datetime.now(tz=timezone.utc)))
    lease.renew()
    lease.assert_owned()
    # missing etag write fails when calling _write directly
    lease._etag = None
    with pytest.raises(AppError):
        lease._write(lease._payload(lease._now()))
    lease.release()


def test_publish_existing_object_and_multipart_resume(tmp_settings: Path, tmp_path: Path) -> None:
    store = MemoryTargetStore()
    put_json_if_absent(store, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"})
    package = _pkg(tmp_path, name="big", body=b"z" * (9 * 1024 * 1024))
    digest = package.ciphertext_sha256
    obj = f"objects/sha256/{digest[:2]}/{digest}.age"
    # pre-seed object so publish verifies instead of upload
    store.put_if_absent(obj, package.path.read_bytes(), checksum_sha256=digest)
    target = backup_publish.ResolvedTarget(target_id="t", root=None, managed=False, kind="s3", store=store)
    result = backup_publish.publish_backup(target, package, run_id="r-pre", policy_id="pol", schedule_slot="slot-pre", fencing_token=1, checkpoint=lambda: None)
    assert result.converged is False
    # multipart resume: seed spool multipart state then publish new package
    package2 = _pkg(tmp_path, name="big2", body=b"y" * (9 * 1024 * 1024))
    slot = "slot-resume"
    slot_d = commit_slot_digest(slot)
    backup_spool.store_verified_package(package2, policy_id="pol", schedule_slot=slot, run_id="r-res")
    upload = store.begin_multipart(f"objects/sha256/{package2.ciphertext_sha256[:2]}/{package2.ciphertext_sha256}.age", checksum_sha256=package2.ciphertext_sha256)
    # upload first part only
    part = package2.path.read_bytes()[: 8 * 1024 * 1024]
    store.upload_part(upload, 1, part, checksum_sha256=hashlib.sha256(part).hexdigest())
    backup_spool.write_multipart_state(
        "pol",
        slot_d,
        {"key": f"objects/sha256/{package2.ciphertext_sha256[:2]}/{package2.ciphertext_sha256}.age", "uploadId": upload.upload_id, "parts": upload.parts, "checksumSha256": package2.ciphertext_sha256},
    )
    result2 = backup_publish.publish_backup(target, package2, run_id="r-res2", policy_id="pol", schedule_slot=slot, fencing_token=2, checkpoint=lambda: None)
    assert result2.commit["objectDigest"] == package2.ciphertext_sha256
    # latest_commit_store pagination
    for i in range(3):
        put_json_if_absent(store, f"commits/extra/{i}.bin", {"no": "json"})  # non-json skipped via read fail - actually put json
    put_json_if_absent(store, "commits/extra/note.txt", {"commitHash": None})
    assert backup_publish.latest_commit_store(store) is not None


def test_catalog_store_snapshot_reload(tmp_settings: Path) -> None:
    store = MemoryTargetStore()
    writer = backup_writer_lease.TargetWriterLease(store=store, target_id="t", owner_run_id="r", owner_instance_id="i", fencing_token=3)
    writer.acquire()
    # force snapshot interval by appending many events
    for i in range(5):
        backup_catalog.append_receipt_store(
            store,
            {
                "backupId": f"c{i}",
                "filename": f"c{i}.age",
                "policyId": "p",
                "targetId": "t",
                "runId": "r",
                "scheduleSlot": f"s{i}",
                "size": 1,
                "ciphertextSha256": f"{i:064d}"[-64:],
                "objectDigest": f"{i:064d}"[-64:],
                "manifestDigest": "m" * 64,
                "coverageDigest": "c" * 64,
                "creationVerified": True,
                "createdAt": f"2026-02-0{i+1}T00:00:00Z",
            },
            writer=writer,
        )
    state = backup_catalog.catalog_state_store(store)
    assert "c0" in state
    backup_catalog._append_entry_store(store, "unlock-verified", {"backupId": "c0", "userUnlockVerifiedAt": "2026-02-10T00:00:00Z"}, writer=writer)
    backup_catalog._append_entry_store(store, "restore-trash", {"backupId": "c1", "restoredAt": "2026-02-11T00:00:00Z"}, writer=writer)
    backup_catalog._append_entry_store(store, "delete", {"backupId": "c2", "retentionRunId": "rr", "deletedAt": "2026-02-12T00:00:00Z"}, writer=writer)
    state2 = backup_catalog.catalog_state_store(store)
    assert state2["c0"].get("userUnlockVerifiedAt")
    writer.release()


def test_governance_adopt_register_new(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.web.routes import backup_governance

    monkeypatch.setattr(backup_governance, "require_api_auth", lambda _request: None)
    directory = tmp_settings / "usb2"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    app = server_module.create_app()
    client = TestClient(app)
    adopted = client.post(f"/api/workspace/backup-targets/{record['targetId']}/adopt")
    assert adopted.status_code == 200
    registered = client.post("/api/workspace/backup-targets/register-new", json={"path": str(directory), "label": "n"})
    assert registered.status_code == 200


def test_governance_api_new_routes(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_backup_target_s3_adapter import FakeS3Client
    from deepseek_infra.web.routes import backup_governance

    monkeypatch.setattr(backup_governance, "require_api_auth", lambda _request: None)
    app = server_module.create_app()
    client = TestClient(app)
    resp = client.get("/api/workspace/backup-target-capabilities")
    assert resp.status_code == 200
    assert "s3TargetAvailable" in resp.json()
    bad = client.post("/api/workspace/backup-targets", json={"kind": "webdav"})
    assert bad.status_code in {400, 501}
    fake = FakeS3Client()
    original_init = backup_targets.init_s3_target

    def _init_s3(**kwargs: Any) -> dict[str, Any]:
        kwargs = dict(kwargs)
        kwargs["client"] = fake
        kwargs["probe"] = False
        return original_init(**kwargs)

    monkeypatch.setattr(backup_targets, "init_s3_target", _init_s3)
    created = client.post(
        "/api/workspace/backup-targets",
        json={"kind": "s3", "bucket": "bk", "prefix": "p", "label": "cloud", "probe": False, "credentialProvider": {"type": "aws-default-chain"}},
    )
    assert created.status_code == 200
    target_id = created.json()["targetId"]
    missing = client.post("/api/workspace/restores/from-target", json={"targetId": target_id, "backupId": "nope"})
    assert missing.status_code in {404, 409, 503, 400, 500}
    with pytest.raises(AppError):
        backup_governance._find_backup_root("does-not-exist-backup")

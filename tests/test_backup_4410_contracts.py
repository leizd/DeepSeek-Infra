from __future__ import annotations

import hashlib
import io
import json
import random
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_chunk_engine,
    backup_crypto,
    backup_incremental,
    backup_incremental_restore,
    backup_policies,
    backup_publish,
    backup_remote_restore,
    backup_run_plan,
    backup_spool,
    backup_target_s3,
    backups,
)
from deepseek_infra.infra.workspace.backup_target_store import MemoryTargetStore, MultipartUpload


def _policy() -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "policyId": "policy_streaming",
        "name": "streaming",
        "targetId": "managed-local",
        "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
        "scope": {"mode": "full"},
        "protection": {"mode": "age-recipient", "recipients": ["age1" + "q" * 58]},
        "incremental": {"mode": "file-delta", "scanWorkers": 2, "maxInFlightBytes": 8 * 1024 * 1024},
    }


def test_cdc_v3_is_explicit_and_normalized() -> None:
    data = random.Random(1).randbytes(24 * 1024 * 1024)
    chunks = backup_incremental.chunk_stream(
        io.BytesIO(data),
        file_size=len(data),
        protocol=backup_incremental.CDC_ALGORITHM_V3,
    )
    mean = sum(int(item["length"]) for item in chunks) / len(chunks)
    assert 1.5 * 1024 * 1024 <= mean <= 2.75 * 1024 * 1024
    assert backup_incremental.CURRENT_CDC_PROTOCOL == "fastcdc-gear-v3"
    legacy = backup_incremental.chunk_stream(io.BytesIO(data[: 2 * 1024 * 1024]), file_size=2 * 1024 * 1024)
    assert legacy
    split = backup_incremental.chunk_stream(
        io.BytesIO(b"x" * (2 * 1024 * 1024)),
        file_size=2 * 1024 * 1024,
        protocol=backup_incremental.CDC_ALGORITHM_V3,
    )
    assert len(split) == 2
    with pytest.raises(AppError, match="Unsupported CDC"):
        backup_incremental.chunk_stream(io.BytesIO(b"data"), file_size=4, protocol="future")
    with pytest.raises(AppError, match="non-covering"):
        backup_incremental.chunk_stream(io.BytesIO(b"data"), file_size=5, protocol=backup_incremental.CDC_ALGORITHM_V3)


def test_protocol_upgrade_forces_full(tmp_settings: Path) -> None:
    files = [backup_incremental.FileRecord("memory", "payload/memory/a", 1, "a" * 64)]
    policy = _policy()
    backup_incremental.record_committed_snapshot(
        target_id="managed-local",
        policy_id="policy_streaming",
        backup_id="legacy449",
        parent_backup_id=None,
        base_backup_id="legacy449",
        chain_depth=0,
        root_digest=backup_incremental.snapshot_root(files),
        files=files,
        scope_digest=backup_incremental.scope_digest(policy),
        recipient_set_digest=backup_incremental.recipient_set_digest(policy),
        schema_digest=backup_incremental.schema_digest({}),
        chunk_protocol=backup_incremental.CDC_ALGORITHM_V2,
    )
    selected = backup_incremental.select_snapshot_plan(
        policy=policy,
        target_id="managed-local",
        policy_id="policy_streaming",
        index_available=True,
        contributor_schemas={},
    )
    assert selected[0] == "full"
    assert selected[6] == "chunk-protocol-upgrade"


def test_parent_chunk_lookup_is_file_indexed(tmp_settings: Path) -> None:
    chunks = [
        backup_incremental.ChunkRecord("memory", "payload/memory/a", 0, 0, 2, "a" * 64),
        backup_incremental.ChunkRecord("projects", "payload/projects/b", 0, 0, 3, "b" * 64),
    ]
    backup_incremental.record_snapshot_chunks(target_id="t", policy_id="p", backup_id="b", chunks=chunks)
    loaded = backup_incremental.load_snapshot_chunks_for_file("t", "p", "b", "projects", "payload/projects/b")
    assert loaded == [chunks[1]]
    with backup_incremental._connect() as connection:
        indexes = {str(row[1]) for row in connection.execute("PRAGMA index_list(snapshot_chunks)")}
    assert "idx_snapshot_chunks_file" in indexes


def test_streaming_restore_never_reads_parent_or_payload_whole(tmp_path: Path) -> None:
    output = tmp_path / "tree"
    package = tmp_path / "package"
    target = output / "payload" / "memory" / "large.bin"
    payload = package / "payload" / "files" / "000000"
    target.parent.mkdir(parents=True)
    payload.parent.mkdir(parents=True)
    parent = random.Random(3).randbytes(4 * 1024 * 1024)
    target.write_bytes(parent)
    replacement = b"changed"
    payload.write_bytes(replacement)
    put = {
        "path": "payload/memory/large.bin",
        "storage": "cdc",
        "chunks": [
            {
                "source": "payload",
                "payloadRef": "payload/files/000000",
                "length": len(replacement),
                "sha256": hashlib.sha256(replacement).hexdigest(),
            }
        ],
        "sha256": hashlib.sha256(replacement).hexdigest(),
    }
    with patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read forbidden")):
        backup_incremental_restore._materialize_cdc(
            output,
            package,
            target,
            put,
            chunk_protocol=backup_incremental.CDC_ALGORITHM_V2,
        )
    assert target.read_bytes() == replacement
    assert backup_incremental_restore.COPY_BUFFER_BYTES == 1024 * 1024


def test_python_rust_chunk_parity_when_helper_is_available(tmp_path: Path) -> None:
    helper = backup_chunk_engine.native_helper_path()
    if helper is None:
        pytest.skip("native deepseek-backup helper is built in the Rust CI job")
    path = tmp_path / "parity.bin"
    path.write_bytes(random.Random(9).randbytes(18 * 1024 * 1024))
    python = backup_chunk_engine.PythonChunkEngine().scan_file(path)
    rust = backup_chunk_engine.RustChunkEngine(helper).scan_file(path)
    assert (python.size, python.sha256, python.protocol, python.chunks) == (rust.size, rust.sha256, rust.protocol, rust.chunks)


def test_chunk_engine_fallback_and_bounded_parallel_scan(tmp_path: Path) -> None:
    class BrokenNative:
        def scan_file(self, path: Path, *, protocol: str) -> backup_chunk_engine.FileChunkScan:
            del path, protocol
            raise AppError("native failed", code=ErrorCode.INTERNAL)

    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"a" * 1024)
    second.write_bytes(b"b" * 2048)
    fallback = backup_chunk_engine.FallbackChunkEngine(BrokenNative())  # type: ignore[arg-type]
    assert fallback.scan_file(first).engine == "python"
    checkpoints: list[bool] = []
    scans, telemetry = backup_chunk_engine.scan_files_bounded(
        [first, second],
        workers=2,
        max_in_flight_bytes=1024,
        engine=backup_chunk_engine.PythonChunkEngine(),
        checkpoint=lambda: checkpoints.append(True),
    )
    assert set(scans) == {first, second}
    assert telemetry["files"] == 2 and telemetry["maxInFlightBytes"] == 1024
    assert len(checkpoints) == 2


def test_native_chunk_engine_contract_and_helper_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / ("deepseek-backup.exe" if sys.platform == "win32" else "deepseek-backup")
    explicit.write_bytes(b"helper")
    monkeypatch.setenv("DEEPSEEK_BACKUP_CHUNK_HELPER", str(explicit))
    assert backup_chunk_engine.native_helper_path() == explicit.resolve()

    monkeypatch.delenv("DEEPSEEK_BACKUP_CHUNK_HELPER")
    bundle = tmp_path / "bundle"
    bundled = bundle / "bin" / explicit.name
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"helper")
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    assert backup_chunk_engine.native_helper_path() == bundled.resolve()
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    discovered = tmp_path / "path-helper"
    discovered.write_bytes(b"helper")
    monkeypatch.setattr(backup_chunk_engine, "__file__", str(tmp_path / "a" / "b" / "c" / "mod.py"))
    monkeypatch.setattr(shutil, "which", lambda _name: str(discovered))
    assert backup_chunk_engine.native_helper_path() == discovered.resolve()

    path = tmp_path / "data.bin"
    path.write_bytes(b"data")
    payload = {
        "size": 4,
        "sha256": hashlib.sha256(b"data").hexdigest(),
        "protocol": backup_incremental.CDC_ALGORITHM_V3,
        "chunks": [{"offset": 0, "length": 4, "sha256": hashlib.sha256(b"data").hexdigest()}],
    }
    engine = backup_chunk_engine.RustChunkEngine(explicit)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload)))
    result = engine.scan_file(path)
    assert result.engine == "rust" and result.chunks[0]["length"] == 4

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=3, stdout=""))
    with pytest.raises(AppError, match="native backup chunk helper failed"):
        engine.scan_file(path)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="{}"))
    with pytest.raises(AppError, match="invalid output"):
        engine.scan_file(path)


def test_chunk_scan_cancellation_and_default_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_chunk_engine, "native_helper_path", lambda: None)
    assert isinstance(backup_chunk_engine.default_chunk_engine(), backup_chunk_engine.FallbackChunkEngine)
    cancelled = threading.Event()
    cancelled.set()
    path = tmp_path / "cancel.bin"
    path.write_bytes(b"x")
    with pytest.raises(AppError, match="cancelled"):
        backup_chunk_engine.scan_files_bounded([path], workers=0, max_in_flight_bytes=0, cancel_event=cancelled)

    budget = backup_chunk_engine._ByteBudget(1)
    held = budget.acquire(1, None)
    try:
        with pytest.raises(AppError, match="cancelled"):
            budget.acquire(1, cancelled)
    finally:
        budget.release(held)


def test_adaptive_delta_resolution_is_frozen(tmp_settings: Path) -> None:
    policy = backup_policies.normalize_policy(_policy())
    plan = backup_run_plan.freeze_run_plan(
        policy=policy,
        schedule_slot="2026-08-09T03:00@UTC",
        slot_digest="a" * 64,
        contributor_plan={"contributors": []},
        target_id="managed-local",
        snapshot_kind="incremental",
        parent_backup_id="F0",
        base_backup_id="F0",
        lineage_id="F0",
        chain_depth=1,
    )
    assert plan["plannedSnapshotKind"] == "adaptive"
    resolved = backup_run_plan.resolve_adaptive_plan(
        str(policy["policyId"]),
        "a" * 64,
        resolved_snapshot_kind="full",
        reason="delta-ratio",
    )
    assert resolved["resolvedSnapshotKind"] == "full"
    assert resolved["resolutionReason"] == "delta-ratio"
    assert backup_run_plan.read_run_plan(str(policy["policyId"]), "a" * 64) == resolved


def test_adaptive_plan_rejects_invalid_and_conflicting_resolutions(tmp_settings: Path) -> None:
    with pytest.raises(AppError, match="invalid adaptive"):
        backup_run_plan.resolve_adaptive_plan("missing", "0" * 64, resolved_snapshot_kind="other", reason="bad")
    with pytest.raises(AppError, match="unavailable"):
        backup_run_plan.resolve_adaptive_plan("missing", "0" * 64, resolved_snapshot_kind="full", reason="bad")

    full_policy = {**_policy(), "policyId": "policy_full", "incremental": {"mode": "off"}}
    full = backup_run_plan.freeze_run_plan(
        policy=full_policy,
        schedule_slot="slot",
        slot_digest="b" * 64,
        contributor_plan=[],
        target_id="managed-local",
        snapshot_kind="full",
    )
    assert backup_run_plan.resolve_adaptive_plan("policy_full", "b" * 64, resolved_snapshot_kind="full", reason="same") == full
    with pytest.raises(AppError, match="not adaptive"):
        backup_run_plan.resolve_adaptive_plan("policy_full", "b" * 64, resolved_snapshot_kind="incremental", reason="bad")

    adaptive_policy = {**_policy(), "policyId": "policy_incremental"}
    backup_run_plan.freeze_run_plan(
        policy=adaptive_policy,
        schedule_slot="slot",
        slot_digest="c" * 64,
        contributor_plan=[],
        target_id="managed-local",
        snapshot_kind="incremental",
    )
    resolved = backup_run_plan.resolve_adaptive_plan(
        "policy_incremental", "c" * 64, resolved_snapshot_kind="incremental", reason="delta-ratio-within-limit"
    )
    assert resolved["snapshotKind"] == "incremental"
    with pytest.raises(AppError, match="already frozen"):
        backup_run_plan.resolve_adaptive_plan("policy_incremental", "c" * 64, resolved_snapshot_kind="full", reason="changed")

    plan_path = backup_run_plan.plan_path("policy_full", "b" * 64)
    damaged = json.loads(plan_path.read_text(encoding="utf-8"))
    damaged["targetId"] = "tampered"
    plan_path.write_text(json.dumps(damaged), encoding="utf-8")
    with pytest.raises(AppError, match="digest mismatch"):
        backup_run_plan.read_run_plan("policy_full", "b" * 64)


def test_public_materialize_route(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from deepseek_infra.web import server as server_module
    from deepseek_infra.web.routes import backup_governance

    monkeypatch.setattr(backup_governance, "require_api_auth", lambda _request: None)
    client = TestClient(server_module.create_app())
    prepared = {"ok": True, "restoreId": "restore_public", "phase": "prepared", "materializedTreeVerified": True}
    with patch.object(backup_remote_restore, "materialize_federated_restore", return_value=prepared) as materialize:
        response = client.post(
            "/api/workspace/restores/restore_public/materialize",
            json={"mode": "merge", "previousEpoch": "old", "targetEpoch": "new", "ownerDocumentId": "tab"},
        )
    assert response.status_code == 200 and response.json()["phase"] == "prepared"
    assert materialize.call_args.kwargs["target_epoch"] == "new"


def test_materialize_feeds_existing_federated_restore(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    restore_id = "restore_closed_loop"
    root = backups.RESTORE_DIR / restore_id
    root.mkdir(parents=True)
    backup_remote_restore._atomic_write_json(
        root / "remote-fetch.json",
        {
            "schemaVersion": 2,
            "restoreId": restore_id,
            "snapshotKind": "incremental",
            "phase": "chain-fetched",
            "chain": [{"backupId": "F0", "objectDigest": "a" * 64}, {"backupId": "I1", "objectDigest": "b" * 64}],
        },
    )
    backup_crypto.put_secret(restore_id, "age-identity", "AGE-SECRET-KEY-TEST")
    tree = tmp_path / "verified"
    tree.mkdir()
    inspected: list[Path] = []
    monkeypatch.setattr(
        backup_remote_restore,
        "materialize_restore_session",
        lambda *args, **kwargs: {"restoreId": restore_id, "phase": "materialized", "tree": str(tree)},
    )
    def inspect_tree(_restore_id: str, path: Path, **kwargs: Any) -> dict[str, Any]:
        del _restore_id, kwargs
        inspected.append(path)
        return {"ok": True}

    monkeypatch.setattr(backups, "inspect_verified_restore_tree", inspect_tree)
    monkeypatch.setattr(
        backups,
        "prepare_restore",
        lambda *args, **kwargs: {"ok": True, "restoreId": restore_id, "phase": "backend-staged", "serverTransactionDigest": "c" * 64},
    )
    result = backup_remote_restore.materialize_federated_restore(restore_id, previous_epoch="old", target_epoch="new")
    assert result["phase"] == "prepared" and result["materializedTreeVerified"] is True
    assert inspected == [tree]
    assert backup_remote_restore.read_restore_session(restore_id)["phase"] == "prepared"  # type: ignore[index]


def test_materialize_federated_restore_error_and_retry_phases(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AppError, match="not found"):
        backup_remote_restore.materialize_federated_restore("restore_missing")

    invalid_id = "restore_not_fetched"
    backup_remote_restore._atomic_write_json(
        backups.RESTORE_DIR / invalid_id / "remote-fetch.json",
        {"schemaVersion": 2, "restoreId": invalid_id, "phase": "fetching-chain"},
    )
    with pytest.raises(AppError, match="not finished fetching"):
        backup_remote_restore.materialize_federated_restore(invalid_id)

    prepared_id = "restore_already_prepared"
    backup_remote_restore._atomic_write_json(
        backups.RESTORE_DIR / prepared_id / "remote-fetch.json",
        {"schemaVersion": 2, "restoreId": prepared_id, "phase": "prepared"},
    )
    monkeypatch.setattr(backups, "get_restore", lambda _restore_id: {"restoreId": prepared_id, "ok": True})
    retried = backup_remote_restore.materialize_federated_restore(prepared_id)
    assert retried["phase"] == "prepared" and retried["materializedTreeVerified"] is True

    for restore_id, recovery_required in (("restore_failed", False), ("restore_recovery_required", True)):
        root = backups.RESTORE_DIR / restore_id
        backup_remote_restore._atomic_write_json(
            root / "remote-fetch.json",
            {"schemaVersion": 2, "restoreId": restore_id, "phase": "chain-fetched", "chain": []},
        )
        if recovery_required:
            (root / "transaction.json").write_text("{}", encoding="utf-8")
        backup_crypto.put_secret(restore_id, "age-identity", "AGE-SECRET-KEY-TEST")
        monkeypatch.setattr(backup_remote_restore, "materialize_restore_session", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError, match="boom"):
            backup_remote_restore.materialize_federated_restore(restore_id)
        session = backup_remote_restore.read_restore_session(restore_id)
        assert session is not None and session["phase"] == ("recovery-required" if recovery_required else "failed")

    backup_remote_restore.advance_federated_phase("restore_absent", "complete")


def test_multipart_upload_is_parallel_resumable_and_fenced(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class ConcurrentStore(MemoryTargetStore):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.maximum = 0
            self.lock = threading.Lock()

        def upload_part(self, upload: Any, part_number: int, data: bytes, *, checksum_sha256: str | None = None) -> dict[str, Any]:
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            time.sleep(0.01)
            try:
                return super().upload_part(upload, part_number, data, checksum_sha256=checksum_sha256)
            finally:
                with self.lock:
                    self.active -= 1

    monkeypatch.setattr(backup_spool, "SPOOL_DIR", tmp_settings / ".backup-spool")
    path = tmp_path / "package.age"
    path.write_bytes(b"x" * (33 * 1024 * 1024))
    package = SimpleNamespace(path=path, size=path.stat().st_size, ciphertext_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    store = ConcurrentStore()
    checkpoints: list[bool] = []
    backup_publish._upload_object_resumable(
        store,
        package,
        obj_key="objects/package.age",
        policy_id="p",
        slot_digest="s",
        checkpoint=lambda: checkpoints.append(True),
    )
    assert store.maximum > 1
    state = backup_spool.read_multipart_state("p", "s")
    assert state is not None and state["partSize"] == 16 * 1024 * 1024 and state["completedParts"] == 3
    assert store.stat("objects/package.age") is not None and checkpoints

    stale_path = tmp_path / "stale.age"
    stale_path.write_bytes(b"y" * (17 * 1024 * 1024))
    stale = SimpleNamespace(path=stale_path, size=stale_path.stat().st_size, ciphertext_sha256=hashlib.sha256(stale_path.read_bytes()).hexdigest())
    calls = 0

    def fence() -> None:
        nonlocal calls
        calls += 1
        if calls >= 4:
            raise AppError("stale worker", code=ErrorCode.INVALID_REQUEST, status=409)

    with pytest.raises(AppError, match="stale worker"):
        backup_publish._upload_object_resumable(
            ConcurrentStore(),
            stale,
            obj_key="objects/stale.age",
            policy_id="p2",
            slot_digest="s2",
            checkpoint=fence,
        )


def test_multipart_reconciles_legacy_journal_and_handles_list_errors(
    tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backup_spool, "SPOOL_DIR", tmp_settings / ".backup-spool")
    path = tmp_path / "legacy.age"
    path.write_bytes(b"a" * (9 * 1024 * 1024))
    package = SimpleNamespace(path=path, size=path.stat().st_size, ciphertext_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    store = MemoryTargetStore()
    upload = store.begin_multipart("objects/legacy.age", checksum_sha256=package.ciphertext_sha256)
    first = path.read_bytes()[: 8 * 1024 * 1024]
    store.upload_part(upload, 1, first, checksum_sha256=hashlib.sha256(first).hexdigest())
    backup_spool.write_multipart_state(
        "legacy-policy",
        "legacy-slot",
        {"key": upload.key, "uploadId": upload.upload_id, "parts": [], "checksumSha256": package.ciphertext_sha256},
    )
    backup_publish._upload_object_resumable(
        store,
        package,
        obj_key=upload.key,
        policy_id="legacy-policy",
        slot_digest="legacy-slot",
    )
    state = backup_spool.read_multipart_state("legacy-policy", "legacy-slot")
    assert state is not None and state["partSize"] == 8 * 1024 * 1024 and state["completedParts"] == 2

    class UnavailablePartsStore(MemoryTargetStore):
        def list_multipart_parts(self, upload: MultipartUpload) -> list[dict[str, Any]]:
            del upload
            raise AppError("list unavailable", code=ErrorCode.INTERNAL)

    retry_path = tmp_path / "retry.age"
    retry_path.write_bytes(b"retry")
    retry_package = SimpleNamespace(
        path=retry_path,
        size=retry_path.stat().st_size,
        ciphertext_sha256=hashlib.sha256(retry_path.read_bytes()).hexdigest(),
    )
    backup_publish._upload_object_resumable(
        UnavailablePartsStore(), retry_package, obj_key="objects/retry.age", policy_id="retry", slot_digest="retry"
    )

    broken = UnavailablePartsStore()
    broken_upload = broken.begin_multipart("objects/broken.age", checksum_sha256=retry_package.ciphertext_sha256)
    backup_spool.write_multipart_state(
        "broken",
        "broken",
        {
            "key": broken_upload.key,
            "uploadId": broken_upload.upload_id,
            "parts": [{"partNumber": 1, "etag": "known", "size": 1}],
            "checksumSha256": retry_package.ciphertext_sha256,
            "partSize": 16 * 1024 * 1024,
        },
    )
    with pytest.raises(AppError, match="list unavailable"):
        backup_publish._upload_object_resumable(
            broken, retry_package, obj_key=broken_upload.key, policy_id="broken", slot_digest="broken"
        )

    truncated = SimpleNamespace(path=tmp_path / "empty.age", size=1, ciphertext_sha256=hashlib.sha256(b"").hexdigest())
    truncated.path.write_bytes(b"")
    with pytest.raises(AppError, match="truncated"):
        backup_publish._upload_object_resumable(
            MemoryTargetStore(), truncated, obj_key="objects/empty.age", policy_id="empty", slot_digest="empty"
        )


def test_s3_list_parts_paginates_and_normalizes() -> None:
    class ListPartsClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def list_parts(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            if "PartNumberMarker" not in kwargs:
                return {
                    "Parts": [{"PartNumber": 1, "ETag": '"first"', "Size": 5, "ChecksumSHA256": "checksum"}],
                    "IsTruncated": True,
                    "NextPartNumberMarker": 1,
                }
            return {"Parts": [{"PartNumber": 2, "ETag": '"second"', "Size": 7}], "IsTruncated": False}

    client = ListPartsClient()
    store = backup_target_s3.S3TargetStore(bucket="bucket", prefix="prefix", client=client)
    upload = MultipartUpload(key="objects/value", upload_id="upload-1", checksum_sha256="a" * 64)
    parts = store.list_multipart_parts(upload)
    assert parts == [
        {"partNumber": 1, "etag": '\"first\"', "size": 5, "checksumSHA256": "checksum"},
        {"partNumber": 2, "etag": '\"second\"', "size": 7},
    ]
    assert client.calls[1]["PartNumberMarker"] == 1

    class BrokenListClient:
        def list_parts(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            raise RuntimeError("unavailable")

    broken = backup_target_s3.S3TargetStore(bucket="bucket", client=BrokenListClient())
    with pytest.raises(AppError, match="list-parts"):
        broken.list_multipart_parts(upload)

    class NoSuchUploadError(RuntimeError):
        response = {
            "Error": {"Code": "NoSuchUpload"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }

    class MissingUploadClient:
        def list_parts(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            raise NoSuchUploadError("gone")

    missing = backup_target_s3.S3TargetStore(bucket="bucket", client=MissingUploadClient())
    with pytest.raises(AppError, match="multipart-upload-not-found") as missing_exc:
        missing.list_multipart_parts(upload)
    assert missing_exc.value.status == 404

    class NoDateClient:
        def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return {"ResponseMetadata": {"HTTPHeaders": {}}}

    assert backup_target_s3.S3TargetStore(bucket="bucket", client=NoDateClient()).server_time() is None


def test_publish_store_projection_and_incomplete_journal_branches(tmp_path: Path) -> None:
    def put_json(store: MemoryTargetStore, key: str, value: Any) -> None:
        store.put_if_absent(key, (json.dumps(value, sort_keys=True) + "\n").encode(), content_type="application/json")

    class PagedStore(MemoryTargetStore):
        def list_objects(self, prefix: str, *, cursor: str | None = None, limit: int = 1000) -> Any:
            del limit
            return super().list_objects(prefix, cursor=cursor, limit=1)

    store = PagedStore()
    assert backup_publish.latest_commit_store(store) is None
    store.put_if_absent("commits/ignore.txt", b"ignored")
    put_json(store, "commits/one.json", {"commitHash": "one", "targetGeneration": 1})
    put_json(store, "commits/no-hash.json", {"targetGeneration": 2})
    put_json(store, "commits/three.json", {"commitHash": "three", "targetGeneration": 3})
    assert backup_publish.latest_commit_store(store)["commitHash"] == "three"  # type: ignore[index]

    journal_store = MemoryTargetStore()
    journal = {"runId": "run-journal", "phase": "started"}
    backup_publish._replace_journal(journal_store, journal)
    journal["phase"] = "object-published"
    backup_publish._replace_journal(journal_store, journal)
    stored_journal = json.loads(journal_store.get_bytes("transactions/run-journal.json") or b"{}")
    assert stored_journal["phase"] == "object-published"

    root = tmp_path / "fs-target"
    backup_publish._write_journal(
        root,
        {"runId": "excluded", "policyId": "policy", "scheduleSlot": "slot", "phase": "started"},
    )
    backup_publish._write_journal(
        root,
        {"runId": "wrong", "policyId": "other", "scheduleSlot": "slot", "phase": "started"},
    )
    backup_publish._write_journal(
        root,
        {"runId": "done", "policyId": "policy", "scheduleSlot": "slot", "phase": "complete"},
    )
    (root / "transactions" / "bad.json").write_text("{", encoding="utf-8")
    assert not backup_publish.slot_has_incomplete_journal(root, policy_id="policy", schedule_slot="slot", exclude_run_id="excluded")
    backup_publish._write_journal(
        root,
        {"runId": "pending", "policyId": "policy", "scheduleSlot": "slot", "phase": "receipt-published"},
    )
    assert backup_publish.slot_has_incomplete_journal(root, policy_id="policy", schedule_slot="slot", exclude_run_id="excluded")
    marker = backup_publish.commit_marker_path(root, "policy", "slot")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    assert not backup_publish.slot_has_incomplete_journal(root, policy_id="policy", schedule_slot="slot")

    remote = PagedStore()
    remote.put_if_absent("transactions/ignore.txt", b"ignored")
    remote.put_if_absent("transactions/null.json", b"null")
    put_json(remote, "transactions/excluded.json", {"runId": "excluded", "policyId": "policy", "scheduleSlot": "slot", "phase": "started"})
    put_json(remote, "transactions/wrong.json", {"runId": "wrong", "policyId": "other", "scheduleSlot": "slot", "phase": "started"})
    put_json(remote, "transactions/done.json", {"runId": "done", "policyId": "policy", "scheduleSlot": "slot", "phase": "complete"})
    assert not backup_publish.slot_has_incomplete_journal_store(
        remote, policy_id="policy", schedule_slot="slot", exclude_run_id="excluded"
    )
    put_json(remote, "transactions/pending.json", {"runId": "pending", "policyId": "policy", "scheduleSlot": "slot", "phase": "started"})
    assert backup_publish.slot_has_incomplete_journal_store(remote, policy_id="policy", schedule_slot="slot", exclude_run_id="excluded")

    committed = MemoryTargetStore()
    from deepseek_infra.infra.workspace.backup_target_store import commit_marker_key

    put_json(committed, commit_marker_key("policy", "slot"), {"commitHash": "committed"})
    assert not backup_publish.slot_has_incomplete_journal_store(committed, policy_id="policy", schedule_slot="slot")

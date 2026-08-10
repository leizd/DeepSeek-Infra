"""End-to-end projected remote restore through the federated commit (4.4.13)."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_crypto,
    backup_incremental,
    backup_pack,
    backup_projection,
    backup_publish,
    backup_remote_restore,
    backups,
)
from deepseek_infra.infra.workspace.backup_projection import RestoreSelection

RECIPIENT_A = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def stub_crypto(monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = b"age-encryption.org/v1\n"

    def encrypt_stream(target: Path, write_plaintext: object, *, mode: str, secret: object = None, recipients: tuple[str, ...] = (), cancel_event: object = None) -> None:
        buffer = io.BytesIO()
        write_plaintext(buffer)  # type: ignore[operator]
        target.write_bytes(prefix + bytes(buffer.getbuffer())[::-1])

    def decrypt_file(source: Path, target: Path, *, kind: str, secret: bytearray, cancel_event: object = None) -> None:
        raw = source.read_bytes()
        assert raw.startswith(prefix)
        target.write_bytes(raw[len(prefix) :][::-1])

    monkeypatch.setattr(backup_crypto, "encrypt_stream", encrypt_stream)
    monkeypatch.setattr(backup_crypto, "decrypt_file", decrypt_file)
    monkeypatch.setattr(backup_crypto, "generate_identity", lambda: {"identity": "AGE-SECRET-KEY-1EPH", "recipient": "age1eph"})
    monkeypatch.setattr(backup_crypto, "inspect_header", lambda _path: {"age": True, "passphrase": True})
    monkeypatch.setattr(backup_crypto, "capabilities", lambda: {"encryptedBackupAvailable": True, "formats": ["age-v1"], "protectionModes": ["passphrase", "age-recipient"]})


def _rec(contributor: str, path: str, size: int, sha: str) -> backup_incremental.FileRecord:
    return backup_incremental.FileRecord(contributor, path, size, sha)


class _FakePackage:
    def __init__(self, path: Path, payload: bytes, backup_id: str, manifest: dict[str, Any]) -> None:
        self.path = path
        self.backup_id = backup_id
        self.filename = f"{backup_id}.age"
        self.size = len(payload)
        self.ciphertext_sha256 = hashlib.sha256(payload).hexdigest()
        self.manifest_digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        self.coverage_digest = "c" * 64
        self.creation_verified = True
        self.manifest = manifest


def _build_chain_packages(tmp_path: Path) -> tuple[_FakePackage, _FakePackage]:
    keep = b"keep"
    source = b"source-payload"
    memory = b"mem"
    new_content = b"brand new payload"

    f0_records = [
        _rec("projects", "payload/projects/p1/keep.bin", len(keep), _sha(keep)),
        _rec("projects", "payload/projects/p2/source.bin", len(source), _sha(source)),
        _rec("memory", "payload/memory/memories.json", len(memory), _sha(memory)),
    ]
    final_records = [
        *f0_records,
        _rec("projects", "payload/projects/p1/restored.bin", len(source), _sha(source)),
        _rec("projects", "payload/projects/p1/new.bin", len(new_content), _sha(new_content)),
    ]
    ops = backup_incremental.diff_trees(f0_records, final_records, successful_contributors={"projects", "memory"})
    for put in ops["put"]:
        if put["path"] == "payload/projects/p1/restored.bin":
            put["storage"] = "cdc"
            put["chunks"] = [{"source": "parent-range", "parentPath": "payload/projects/p2/source.bin", "offset": 0, "length": len(source), "sha256": _sha(source)}]
            put.pop("payloadRef", None)
        elif put["path"] == "payload/projects/p1/new.bin":
            put["storage"] = "whole"
            put["payloadRef"] = {"kind": "pack-range", "blobId": "blob_000000"}

    f0_root = tmp_path / "f0"
    for relative, content in (
        ("payload/projects/p1/keep.bin", keep),
        ("payload/projects/p2/source.bin", source),
        ("payload/memory/memories.json", memory),
    ):
        path = f0_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    f0_manifest: dict[str, Any] = {
        "schemaVersion": backups.BACKUP_SCHEMA,
        "purpose": backups.PACKAGE_PURPOSE,
        "backupId": "backup_f0",
        "snapshotKind": "full",
        "source": {"version": config.APP_VERSION, "revision": "rev", "platform": "test", "createdAt": "2026-08-10T00:00:00Z"},
        "scope": {"mode": "full", "projectIds": [], "includeHistory": True, "includeDrafts": False},
        "contributors": [
            {"id": "projects", "schemaVersion": 1, "records": 2, "bytes": 18, "digest": "d" * 64, "restorePolicy": "merge"},
            {"id": "memory", "schemaVersion": 1, "records": 1, "bytes": 3, "digest": "e" * 64, "restorePolicy": "merge"},
        ],
        "coverage": {"planId": "plan_f0", "policy": "strict", "localContributors": ["projects", "memory"], "externalContributors": [], "complete": True},
        "files": [
            {"contributorId": r.contributor_id, "path": r.logical_path, "size": r.size, "sha256": r.sha256} for r in f0_records
        ],
        "snapshot": {"kind": "full", "rootDigest": backup_incremental.snapshot_root(f0_records)},
    }
    (f0_root / "manifest.json").write_text(json.dumps(f0_manifest, sort_keys=True), encoding="utf-8")
    backup_remote_restore._write_checksums(f0_root, f0_manifest)

    i1_root = tmp_path / "i1"
    writer = backup_pack.PackWriter(i1_root)
    writer.append(io.BytesIO(new_content), expected_length=len(new_content), expected_sha256=_sha(new_content))
    writer.finalize()
    (i1_root / "delta").mkdir()
    operations_bytes = json.dumps(ops, sort_keys=True).encode("utf-8")
    (i1_root / "delta/operations.json").write_bytes(operations_bytes)
    i1_manifest: dict[str, Any] = {
        "schemaVersion": backups.BACKUP_SCHEMA,
        "purpose": backups.PACKAGE_PURPOSE,
        "backupId": "backup_i1",
        "snapshotKind": "incremental",
        "source": {"version": config.APP_VERSION, "revision": "rev", "platform": "test", "createdAt": "2026-08-10T01:00:00Z"},
        "scope": {"mode": "full", "projectIds": [], "includeHistory": True, "includeDrafts": False},
        "contributors": [
            {"id": "projects", "schemaVersion": 1, "records": 4, "bytes": 40, "digest": "f" * 64, "restorePolicy": "merge"},
            {"id": "memory", "schemaVersion": 1, "records": 1, "bytes": 3, "digest": "g" * 64, "restorePolicy": "merge"},
        ],
        "coverage": {"planId": "plan_i1", "policy": "strict", "localContributors": ["projects", "memory"], "externalContributors": [], "complete": True},
        "files": [
            {"contributorId": r.contributor_id, "path": r.logical_path, "size": r.size, "sha256": r.sha256} for r in final_records
        ],
        "snapshot": {
            "kind": "incremental",
            "format": "incremental-v5",
            "chunkProtocol": backup_incremental.CURRENT_CDC_PROTOCOL,
            "lineageId": "backup_f0",
            "parentBackupId": "backup_f0",
            "baseBackupId": "backup_f0",
            "chainDepth": 1,
            "rootDigest": backup_incremental.snapshot_root(final_records),
        },
        "deltaFiles": [
            {"path": "delta/operations.json", "size": len(operations_bytes), "sha256": _sha(operations_bytes)},
            {"path": backup_pack.PACK_INDEX_PATH, "size": (i1_root / backup_pack.PACK_INDEX_PATH).stat().st_size, "sha256": _sha((i1_root / backup_pack.PACK_INDEX_PATH).read_bytes())},
            {"path": "payload/packs/0000.pack", "size": (i1_root / "payload/packs/0000.pack").stat().st_size, "sha256": _sha((i1_root / "payload/packs/0000.pack").read_bytes())},
        ],
    }
    (i1_root / "manifest.json").write_text(json.dumps(i1_manifest, sort_keys=True), encoding="utf-8")
    backup_remote_restore._write_checksums(i1_root, i1_manifest)

    prefix = b"age-encryption.org/v1\n"
    f0_buffer = io.BytesIO()
    backups._write_zip_tree(f0_root, f0_buffer)
    i1_buffer = io.BytesIO()
    backups._write_zip_tree(i1_root, i1_buffer)
    f0_ciphertext = prefix + f0_buffer.getvalue()[::-1]
    i1_ciphertext = prefix + i1_buffer.getvalue()[::-1]
    f0_staging = tmp_path / "pkg-f0"
    f0_staging.mkdir(exist_ok=True)
    f0_path = f0_staging / "f0.age"
    f0_path.write_bytes(f0_ciphertext)
    i1_staging = tmp_path / "pkg-i1"
    i1_staging.mkdir(exist_ok=True)
    i1_path = i1_staging / "i1.age"
    i1_path.write_bytes(i1_ciphertext)
    return (
        _FakePackage(f0_path, f0_ciphertext, "backup_f0", f0_manifest),
        _FakePackage(i1_path, i1_ciphertext, "backup_i1", i1_manifest),
    )


def _seed_workspace() -> None:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('{"items":[{"id":"m1","text":"before"}]}', encoding="utf-8")


def _publish_chain(package_f0: _FakePackage, package_i1: _FakePackage) -> None:
    target = backup_publish.resolve_target("managed-local")
    backup_publish.publish_backup(target, package_f0, run_id="run_f0", policy_id="policy_e2e", schedule_slot="slot-f0", fencing_token=1)
    backup_publish.publish_backup(target, package_i1, run_id="run_i1", policy_id="policy_e2e", schedule_slot="slot-i1", fencing_token=2)
    backup_catalog.rebuild_catalog_from_receipts(backups.BACKUP_DIR)


def _restore_to_complete(restore_id: str) -> dict[str, Any]:
    result = backup_remote_restore.fetch_restore_session(restore_id)
    while str(result.get("phase") or "") not in {"fetched", "chain-fetched"}:
        result = backup_remote_restore.fetch_restore_session(restore_id)
    assert str(result.get("phase") or "") in {"fetched", "chain-fetched"}
    backup_crypto.put_secret(restore_id, "passphrase", "hunter2")
    prepared = backup_remote_restore.materialize_federated_restore(restore_id, mode="merge", owner_document_id="server")
    assert prepared["phase"] == "prepared"
    committed = backups.commit_restore(restore_id)
    assert committed["phase"] == "backend-committed"
    completed = backups.complete_restore(restore_id)
    backup_remote_restore.advance_federated_phase(restore_id, "complete")
    return completed


def test_from_target_preview_reports_honest_projection(tmp_settings: Path, stub_crypto: None) -> None:
    _seed_workspace()
    package_f0, package_i1 = _build_chain_packages(tmp_settings)
    _publish_chain(package_f0, package_i1)
    selection = {"contributors": ["projects"], "projectIds": ["p1"]}
    created = backup_remote_restore.create_restore_from_target(
        target_id="managed-local",
        backup_id="backup_i1",
        selection=selection,
    )
    restore_id = str(created["restoreId"])
    result = backup_remote_restore.fetch_restore_session(restore_id)
    while str(result.get("phase") or "") not in {"fetched", "chain-fetched"}:
        result = backup_remote_restore.fetch_restore_session(restore_id)
    # Preview without a secret reports that one is required.
    no_secret = backup_remote_restore.preview_restore_from_target(
        target_id="managed-local",
        backup_id="backup_i1",
        selection=selection,
        restore_id=restore_id,
    )
    assert no_secret["requiresSecret"] is True
    backup_crypto.put_secret(restore_id, "passphrase", "hunter2")
    preview = backup_remote_restore.preview_restore_from_target(
        target_id="managed-local",
        backup_id="backup_i1",
        selection=selection,
        restore_id=restore_id,
    )
    assert preview["phase"] == "preview-planned"
    projection = preview["projection"]
    assert projection["networkSelective"] is False
    assert projection["networkSelectivityReason"] == "whole-age-object"
    assert projection["selected"]["projects"] == 1
    assert projection["bytes"]["selectedLogicalBytes"] > 0
    assert projection["bytes"]["ciphertextDownloadBytes"] == package_f0.size + package_i1.size
    assert projection["requiresFrontendApply"] is False
    # The preview re-put the secret so the federated materialize can proceed.
    completed = _restore_to_complete(restore_id)
    assert completed["phase"] == "complete"


def test_restore_holds_released_on_complete(tmp_settings: Path, stub_crypto: None) -> None:
    _seed_workspace()
    package_f0, package_i1 = _build_chain_packages(tmp_settings)
    _publish_chain(package_f0, package_i1)
    created = backup_remote_restore.create_restore_from_target(
        target_id="managed-local",
        backup_id="backup_i1",
        selection={"contributors": ["projects"], "projectIds": ["p1"]},
    )
    restore_id = str(created["restoreId"])
    session = backup_remote_restore.read_restore_session(restore_id)
    assert session is not None
    hold_keys = list(session.get("holdKeys") or [])
    assert len(hold_keys) == 2
    for key in hold_keys:
        assert (backups.BACKUP_DIR / key).is_file()
    _restore_to_complete(restore_id)
    for key in hold_keys:
        assert not (backups.BACKUP_DIR / key).is_file()


def test_hold_release_respects_recovery_required(tmp_settings: Path) -> None:
    restore_id = "restore_hold_policy"
    hold_keys = [f"holds/restore/{restore_id}:0.json", f"holds/restore/{restore_id}:1.json"]
    session = {
        "schemaVersion": 3,
        "restoreId": restore_id,
        "targetId": "managed-local",
        "backupId": "backup_b",
        "snapshotKind": "incremental",
        "holdKeys": hold_keys,
        "holdKey": None,
        "phase": "preparing",
    }
    backup_remote_restore._atomic_write_json(backup_remote_restore._session_path(restore_id), session)
    for key in hold_keys:
        path = backups.BACKUP_DIR / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schemaVersion":1}', encoding="utf-8")
    backup_remote_restore.advance_federated_phase(restore_id, "recovery-required")
    assert all((backups.BACKUP_DIR / key).is_file() for key in hold_keys)
    backup_remote_restore.advance_federated_phase(restore_id, "complete")
    assert all(not (backups.BACKUP_DIR / key).is_file() for key in hold_keys)


def test_projected_remote_restore_round_trip(tmp_settings: Path, stub_crypto: None) -> None:
    _seed_workspace()
    package_f0, package_i1 = _build_chain_packages(tmp_settings)
    _publish_chain(package_f0, package_i1)
    selection = {"contributors": ["projects"], "projectIds": ["p1"]}
    created = backup_remote_restore.create_restore_from_target(
        target_id="managed-local",
        backup_id="backup_i1",
        selection=selection,
    )
    restore_id = str(created["restoreId"])
    assert created["selectionDigest"] == backup_projection.selection_digest(RestoreSelection(contributors=("projects",), project_ids=("p1",)))
    completed = _restore_to_complete(restore_id)
    assert completed["phase"] == "complete"
    assert completed.get("selectionDigest") == created["selectionDigest"]

    projects_root = config.PROJECTS_DIR
    assert (projects_root / "p1" / "keep.bin").read_bytes() == b"keep"
    assert (projects_root / "p1" / "restored.bin").read_bytes() == b"source-payload"
    assert (projects_root / "p1" / "new.bin").read_bytes() == b"brand new payload"
    # Support file from the unselected project never reaches the workspace.
    assert not (projects_root / "p2").exists()
    # Unselected contributor is never mutated.
    assert (config.MEMORY_FILE).read_text(encoding="utf-8") == '{"items":[{"id":"m1","text":"before"}]}'


def test_full_remote_restore_round_trip_still_works(tmp_settings: Path, stub_crypto: None) -> None:
    _seed_workspace()
    package_f0, package_i1 = _build_chain_packages(tmp_settings)
    _publish_chain(package_f0, package_i1)
    created = backup_remote_restore.create_restore_from_target(target_id="managed-local", backup_id="backup_i1")
    restore_id = str(created["restoreId"])
    completed = _restore_to_complete(restore_id)
    assert completed["phase"] == "complete"
    projects_root = config.PROJECTS_DIR
    assert (projects_root / "p1" / "keep.bin").read_bytes() == b"keep"
    assert (projects_root / "p2" / "source.bin").read_bytes() == b"source-payload"
    assert (projects_root / "p1" / "new.bin").read_bytes() == b"brand new payload"
    assert config.MEMORY_FILE.read_text(encoding="utf-8") == "mem"


def test_projected_restore_rollback_restores_scope(tmp_settings: Path, stub_crypto: None) -> None:
    _seed_workspace()
    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    existing_p1 = config.PROJECTS_DIR / "p1"
    existing_p1.mkdir()
    (existing_p1 / "old.bin").write_bytes(b"old state")
    package_f0, package_i1 = _build_chain_packages(tmp_settings)
    _publish_chain(package_f0, package_i1)
    created = backup_remote_restore.create_restore_from_target(
        target_id="managed-local",
        backup_id="backup_i1",
        selection={"contributors": ["projects"], "projectIds": ["p1"]},
    )
    restore_id = str(created["restoreId"])
    result = backup_remote_restore.fetch_restore_session(restore_id)
    while str(result.get("phase") or "") not in {"fetched", "chain-fetched"}:
        result = backup_remote_restore.fetch_restore_session(restore_id)
    backup_crypto.put_secret(restore_id, "passphrase", "hunter2")
    prepared = backup_remote_restore.materialize_federated_restore(restore_id, mode="merge", owner_document_id="server")
    assert prepared["phase"] == "prepared"
    rolled_back = backups.abort_restore(restore_id)
    assert rolled_back["phase"] == "rolled-back"
    # The pre-existing project state is restored byte-for-byte.
    assert (existing_p1 / "old.bin").read_bytes() == b"old state"
    assert not (existing_p1 / "keep.bin").exists()

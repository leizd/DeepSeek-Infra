from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

import pytest

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_crypto,
    backup_catalog,
    backup_incremental,
    backup_object_set,
    backup_pack,
    backup_publish,
    backup_reconcile,
    backup_remote_restore,
    backup_retention,
    backup_scrub,
    backup_scheduler,
    backup_spool,
    backup_target_store,
    backup_unattended,
    backup_writer_lease,
    backups,
)


@pytest.fixture
def object_set_crypto(monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = b"age-encryption.org/v1\n"
    counter = 0

    def encrypt_stream(
        target: Path,
        write_plaintext: object,
        *,
        mode: str,
        secret: object = None,
        recipients: tuple[str, ...] = (),
        cancel_event: object = None,
    ) -> None:
        nonlocal counter
        del mode, secret, recipients, cancel_event
        plain = io.BytesIO()
        write_plaintext(plain)  # type: ignore[operator]
        counter += 1
        target.write_bytes(prefix + counter.to_bytes(4, "big") + plain.getvalue()[::-1])

    def decrypt_file(
        source: Path,
        target: Path,
        *,
        kind: str,
        secret: bytearray,
        cancel_event: object = None,
    ) -> None:
        del kind, secret, cancel_event
        raw = source.read_bytes()
        assert raw.startswith(prefix)
        target.write_bytes(raw[len(prefix) + 4 :][::-1])

    monkeypatch.setattr(backup_crypto, "encrypt_stream", encrypt_stream)
    monkeypatch.setattr(backup_crypto, "decrypt_file", decrypt_file)
    monkeypatch.setattr(backup_crypto, "generate_identity", lambda: {"identity": "AGE-SECRET-KEY-1EPH", "recipient": "age1eph"})
    monkeypatch.setattr(backup_crypto, "inspect_header", lambda _path: {"age": True})
    monkeypatch.setattr(
        backup_crypto,
        "capabilities",
        lambda: {
            "encryptedBackupAvailable": True,
            "formats": ["age-v1"],
            "protectionModes": ["none", "passphrase", "age-recipient"],
        },
    )


def test_object_set_crypto_fixture_is_independent_of_installed_helper(
    object_set_crypto: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_crypto, "helper_path", lambda: None)
    assert backup_crypto.capabilities()["encryptedBackupAvailable"] is True


def test_restart_probe_counts_each_s3_object_get_once() -> None:
    from deepseek_infra.infra.workspace.backup_target_s3 import S3TargetStore
    from scripts import object_set_restart_probe

    class FakeS3Client:
        def get_object(self, **_kwargs: object) -> dict[str, object]:
            return {"Body": io.BytesIO(b"ciphertext")}

    store = S3TargetStore(bucket="test", client=FakeS3Client())
    object_gets, restore_audit = object_set_restart_probe._install_s3_object_get_audit()
    try:
        assert b"".join(store.get_stream("objects/sha256/ab/ciphertext.age")) == b"ciphertext"
        assert object_gets == ["objects/sha256/ab/ciphertext.age"]
    finally:
        restore_audit()


def _component(tmp_path: Path, component_id: str, payload: bytes, *, control: bool = False) -> backup_object_set.EncryptedComponent:
    path = tmp_path / f"{component_id}.age"
    path.write_bytes(payload)
    return backup_object_set.EncryptedComponent(
        component_id=component_id,
        path=path,
        ciphertext_digest=hashlib.sha256(payload).hexdigest(),
        ciphertext_size=len(payload),
        control=control,
    )


def _full_object_set_package(
    tmp_path: Path,
    *,
    recipients: tuple[str, ...] = ("age1test",),
) -> backup_object_set.ObjectSetPackage:
    staging = tmp_path / "full-object-set-staging"
    contents = {
        "payload/projects/p1/a.bin": b"project-one-snapshot",
        "payload/projects/p2/b.bin": b"project-two-snapshot",
        "payload/memory/memories.json": b"memory-snapshot",
    }
    records = []
    manifest_files = []
    for relative, content in contents.items():
        path = staging / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        contributor = "projects" if "/projects/" in relative else "memory"
        digest = hashlib.sha256(content).hexdigest()
        records.append(backup_incremental.FileRecord(contributor, relative, len(content), digest))
        manifest_files.append(
            {"contributorId": contributor, "path": relative, "size": len(content), "sha256": digest}
        )
    manifest = {
        "schemaVersion": backups.BACKUP_SCHEMA,
        "purpose": backups.PACKAGE_PURPOSE,
        "backupId": "backup_selective_set",
        "snapshotKind": "full",
        "source": {
            "version": config.APP_VERSION,
            "revision": "object-set-test",
            "platform": "test",
            "createdAt": "2026-08-11T00:00:00Z",
        },
        "scope": {"mode": "full", "projectIds": [], "includeHistory": True, "includeDrafts": False},
        "files": manifest_files,
        "contributors": [
            {"id": "projects", "schemaVersion": 1, "restorePolicy": "merge"},
            {"id": "memory", "schemaVersion": 1, "restorePolicy": "merge"},
        ],
        "coverage": {
            "policy": "strict",
            "localContributors": ["projects", "memory"],
            "externalContributors": [],
            "complete": True,
        },
        "snapshot": {"kind": "full", "rootDigest": backup_incremental.snapshot_root(records)},
    }
    (staging / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (staging / "checksums.sha256").write_text("placeholder\n", encoding="utf-8")
    return backup_object_set.build_encrypted_object_set(
        staging,
        tmp_path / "full-object-set-encrypted",
        backup_id="backup_selective_set",
        recipients=recipients,
        manifest=manifest,
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        component_target_bytes=1,
    )


def _incremental_object_set_chain(
    tmp_path: Path,
) -> tuple[backup_object_set.ObjectSetPackage, backup_object_set.ObjectSetPackage]:
    unselected = b"baseline-unselected"
    support = b"cross-project-support"
    added = b"new-project-payload"
    f0_records = [
        backup_incremental.FileRecord(
            "projects",
            "payload/projects/p1/unselected.bin",
            len(unselected),
            hashlib.sha256(unselected).hexdigest(),
        ),
        backup_incremental.FileRecord(
            "projects",
            "payload/projects/p2/support.bin",
            len(support),
            hashlib.sha256(support).hexdigest(),
        ),
    ]
    final_records = [
        *f0_records,
        backup_incremental.FileRecord(
            "projects",
            "payload/projects/p3/from-support.bin",
            len(support),
            hashlib.sha256(support).hexdigest(),
        ),
        backup_incremental.FileRecord(
            "projects",
            "payload/projects/p3/new.bin",
            len(added),
            hashlib.sha256(added).hexdigest(),
        ),
    ]
    common = {
        "schemaVersion": backups.BACKUP_SCHEMA,
        "purpose": backups.PACKAGE_PURPOSE,
        "source": {
            "version": config.APP_VERSION,
            "revision": "object-set-chain-test",
            "platform": "test",
            "createdAt": "2026-08-11T00:00:00Z",
        },
        "scope": {"mode": "full", "projectIds": [], "includeHistory": True, "includeDrafts": False},
        "contributors": [{"id": "projects", "schemaVersion": 1, "restorePolicy": "merge"}],
        "coverage": {
            "policy": "strict",
            "localContributors": ["projects"],
            "externalContributors": [],
            "complete": True,
        },
    }
    f0_staging = tmp_path / "object-set-f0"
    for relative, content in (
        ("payload/projects/p1/unselected.bin", unselected),
        ("payload/projects/p2/support.bin", support),
    ):
        path = f0_staging / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    f0_manifest = {
        **common,
        "backupId": "backup_object_set_f0",
        "snapshotKind": "full",
        "files": [
            {"contributorId": item.contributor_id, "path": item.logical_path, "size": item.size, "sha256": item.sha256}
            for item in f0_records
        ],
        "snapshot": {"kind": "full", "rootDigest": backup_incremental.snapshot_root(f0_records)},
    }
    f0_package = backup_object_set.build_encrypted_object_set(
        f0_staging,
        tmp_path / "object-set-f0-encrypted",
        backup_id="backup_object_set_f0",
        recipients=("age1test",),
        manifest=f0_manifest,
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        component_target_bytes=1,
    )

    operations = backup_incremental.diff_trees(
        f0_records,
        final_records,
        successful_contributors={"projects"},
    )
    for put in operations["put"]:
        if put["path"] == "payload/projects/p3/from-support.bin":
            put["storage"] = "cdc"
            put["chunks"] = [
                {
                    "source": "parent-range",
                    "parentContributorId": "projects",
                    "parentPath": "payload/projects/p2/support.bin",
                    "offset": 0,
                    "length": len(support),
                    "sha256": hashlib.sha256(support).hexdigest(),
                }
            ]
            put.pop("payloadRef", None)
        elif put["path"] == "payload/projects/p3/new.bin":
            put["storage"] = "whole"
            put["payloadRef"] = {"kind": "standalone", "path": "payload/files/000000"}
    i1_staging = tmp_path / "object-set-i1"
    payload_path = i1_staging / "payload/files/000000"
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_bytes(added)
    operations_path = i1_staging / "delta/operations.json"
    operations_path.parent.mkdir(parents=True, exist_ok=True)
    operations_bytes = json.dumps(operations, sort_keys=True, separators=(",", ":")).encode()
    operations_path.write_bytes(operations_bytes)
    i1_manifest = {
        **common,
        "backupId": "backup_object_set_i1",
        "snapshotKind": "incremental",
        "files": [
            {"contributorId": item.contributor_id, "path": item.logical_path, "size": item.size, "sha256": item.sha256}
            for item in final_records
        ],
        "deltaFiles": [
            {
                "path": "delta/operations.json",
                "size": len(operations_bytes),
                "sha256": hashlib.sha256(operations_bytes).hexdigest(),
            },
            {
                "path": "payload/files/000000",
                "size": len(added),
                "sha256": hashlib.sha256(added).hexdigest(),
            },
        ],
        "snapshot": {
            "kind": "incremental",
            "format": "incremental-v6",
            "lineageId": "backup_object_set_f0",
            "parentBackupId": "backup_object_set_f0",
            "baseBackupId": "backup_object_set_f0",
            "chainDepth": 1,
            "parentRootDigest": backup_incremental.snapshot_root(f0_records),
            "rootDigest": backup_incremental.snapshot_root(final_records),
        },
    }
    i1_package = backup_object_set.build_encrypted_object_set(
        i1_staging,
        tmp_path / "object-set-i1-encrypted",
        backup_id="backup_object_set_i1",
        recipients=("age1test",),
        manifest=i1_manifest,
        manifest_digest="c" * 64,
        coverage_digest="d" * 64,
        component_target_bytes=1,
    )
    return f0_package, i1_package


def test_object_set_digest_is_canonical_and_role_blind(tmp_path: Path) -> None:
    control = _component(tmp_path, "control", b"random-control", control=True)
    payload = _component(tmp_path, "p0000", b"random-payload")

    first = backup_object_set.object_set_digest([control, payload])
    second = backup_object_set.object_set_digest([payload, control])

    assert first == second
    assert len(first) == 64
    assert "control" not in backup_object_set.object_set_commitment([control, payload]).decode("ascii")
    assert "p0000" not in backup_object_set.object_set_commitment([control, payload]).decode("ascii")


def test_backup_zip_stream_is_valid_for_process_pipe_output(tmp_path: Path) -> None:
    staging = tmp_path / "streaming-zip"
    staging.mkdir()
    (staging / "entry.bin").write_bytes(b"streamed-through-age-stdin")
    output = io.BytesIO()

    backups._write_zip_tree(staging, output)

    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
        assert archive.read("entry.bin") == b"streamed-through-age-stdin"


def test_object_set_requires_one_control_and_unique_ciphertexts(tmp_path: Path) -> None:
    control = _component(tmp_path, "control", b"same", control=True)
    duplicate = backup_object_set.EncryptedComponent(
        component_id="p0000",
        path=control.path,
        ciphertext_digest=control.ciphertext_digest,
        ciphertext_size=control.ciphertext_size,
    )
    with pytest.raises(AppError, match="duplicate ciphertext"):
        backup_object_set.validate_components([control, duplicate])
    with pytest.raises(AppError, match="exactly one control"):
        backup_object_set.validate_components([duplicate])

    distinct = _component(tmp_path, "distinct", b"distinct")
    duplicate_id = backup_object_set.EncryptedComponent(
        component_id="control",
        path=distinct.path,
        ciphertext_digest=distinct.ciphertext_digest,
        ciphertext_size=distinct.ciphertext_size,
    )
    with pytest.raises(AppError, match="duplicate component ids"):
        backup_object_set.validate_components([control, duplicate_id])
    with pytest.raises(AppError, match="invalid ciphertext digest"):
        backup_object_set.validate_components(
            [
                backup_object_set.EncryptedComponent(
                    component_id="bad-digest",
                    path=distinct.path,
                    ciphertext_digest="not-a-digest",
                    ciphertext_size=distinct.ciphertext_size,
                    control=True,
                )
            ]
        )
    with pytest.raises(AppError, match="invalid ciphertext size"):
        backup_object_set.validate_components(
            [
                backup_object_set.EncryptedComponent(
                    component_id="bad-size",
                    path=distinct.path,
                    ciphertext_digest=distinct.ciphertext_digest,
                    ciphertext_size=-1,
                    control=True,
                )
            ]
        )


def test_object_set_receipt_inventory_fails_closed() -> None:
    digest = "a" * 64
    with pytest.raises(AppError, match="inventory is empty"):
        backup_object_set.object_inventory_digest([])
    for invalid in (
        [{"digest": "short", "size": 1}],
        [{"digest": digest, "size": True}],
        [{"digest": digest, "size": -1}],
        [{"digest": digest, "size": 1}, {"digest": digest, "size": 1}],
    ):
        with pytest.raises(AppError, match="inventory is invalid"):
            backup_object_set.object_inventory_digest(invalid)

    inventory = [{"digest": digest, "size": 1}]
    commitment = backup_object_set.object_inventory_digest(inventory)
    with pytest.raises(AppError, match="inventory is invalid"):
        backup_object_set.committed_object_inventory(
            {"storageProtocol": backup_object_set.OBJECT_SET_V1, "objects": "not-a-list"}
        )
    with pytest.raises(AppError, match="commitment mismatch"):
        backup_object_set.committed_object_inventory(
            {
                "storageProtocol": backup_object_set.OBJECT_SET_V1,
                "objects": inventory,
                "objectSetDigest": "b" * 64,
                "controlObjectDigest": digest,
            }
        )
    with pytest.raises(AppError, match="control object is invalid"):
        backup_object_set.committed_object_inventory(
            {
                "storageProtocol": backup_object_set.OBJECT_SET_V1,
                "objects": inventory,
                "objectSetDigest": commitment,
                "controlObjectDigest": "c" * 64,
            }
        )
    assert backup_object_set.committed_object_inventory({"objectDigest": "invalid"}) == []


def test_remote_object_inventory_contains_ciphertext_facts_only(tmp_path: Path) -> None:
    control = _component(tmp_path, "control", b"control", control=True)
    payload = _component(tmp_path, "p0000", b"payload")

    inventory = backup_object_set.remote_object_inventory([payload, control])

    assert inventory == sorted(inventory, key=lambda item: item["digest"])
    assert set(inventory[0]) == {"digest", "size"}
    assert {item["digest"] for item in inventory} == {control.ciphertext_digest, payload.ciphertext_digest}
    package = backup_object_set.ObjectSetPackage(
        backup_id="backup-path-property",
        components=(control, payload),
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        manifest={"snapshotKind": "full"},
    )
    assert package.path == control.path


def test_object_set_physical_payload_filter_and_unavailable_target(tmp_path: Path) -> None:
    payload = tmp_path / "payload" / "projects" / "p1" / "a.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"payload")
    manifest = {
        "snapshotKind": "full",
        "files": [
            None,
            {"path": "delta/operations.json"},
            {"path": "payload/packs/index.json"},
            {"path": "payload/projects/p1/missing.bin"},
            {"path": "payload/projects/p1/a.bin"},
        ],
    }
    assert backup_object_set._physical_payload_paths(tmp_path, manifest) == ["payload/projects/p1/a.bin"]

    unavailable = backup_publish.ResolvedTarget(target_id="unavailable", root=None, managed=False, store=None)
    with pytest.raises(AppError, match="no local filesystem root"):
        unavailable.require_root()
    with pytest.raises(AppError, match="store is unavailable"):
        unavailable.require_store()


def test_object_set_archives_fail_closed_on_paths_inventory_and_checksums(tmp_path: Path) -> None:
    for unsafe in ("", "../escape", "/absolute", "payload/../escape"):
        with pytest.raises(AppError, match="unsafe path"):
            backup_object_set._safe_relative_path(unsafe)

    with pytest.raises(AppError, match="entry collision"):
        backup_object_set._write_zip_entries(
            io.BytesIO(),
            file_entries={"same.bin": tmp_path / "source.bin"},
            byte_entries={"same.bin": b"collision"},
        )

    archive_path = tmp_path / "component.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("payload/a.bin", b"payload")
    expected_digest = hashlib.sha256(b"payload").hexdigest()
    with pytest.raises(AppError, match="inventory mismatch"):
        backup_object_set._verify_component_archive(
            archive_path,
            {"payload/missing.bin": (7, expected_digest)},
        )
    with pytest.raises(AppError, match="plaintext mismatch"):
        backup_object_set._verify_component_archive(
            archive_path,
            {"payload/a.bin": (7, "0" * 64)},
        )
    with pytest.raises(AppError, match="inventory mismatch"):
        backup_object_set.extract_component_archive(archive_path, tmp_path / "extract-mismatch", ["payload/other.bin"])

    extracted = tmp_path / "extracted"
    backup_object_set.extract_component_archive(archive_path, extracted, ["payload/a.bin"])
    with pytest.raises(AppError, match="overlap"):
        backup_object_set.extract_component_archive(archive_path, extracted, ["payload/a.bin"])

    control = tmp_path / "control"
    control.mkdir()
    with pytest.raises(AppError, match="checksum map is missing"):
        backup_object_set.verify_control_metadata(control)
    checksum_path = control / "checksums.sha256"
    checksum_path.write_text("invalid\n", encoding="utf-8")
    with pytest.raises(AppError, match="checksum map is invalid"):
        backup_object_set.verify_control_metadata(control)
    metadata = control / "manifest.json"
    metadata.write_bytes(b"manifest")
    checksum_path.write_text(f"{hashlib.sha256(b'manifest').hexdigest()}  missing.json\n", encoding="utf-8")
    with pytest.raises(AppError, match="inventory mismatch"):
        backup_object_set.verify_control_metadata(control)
    checksum_path.write_text(f"{'0' * 64}  manifest.json\n", encoding="utf-8")
    with pytest.raises(AppError, match="checksum mismatch"):
        backup_object_set.verify_control_metadata(control)


def test_object_set_payload_groups_respect_boundaries_and_size_limit(tmp_path: Path) -> None:
    paths = [
        "payload/projects/p1/a.bin",
        "payload/projects/p1/b.bin",
        "payload/projects/p2/c.bin",
        "payload/memory/memories.json",
    ]
    for relative in paths:
        target = tmp_path.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"1234")
    with pytest.raises(AppError, match="target must be positive"):
        backup_object_set._payload_component_groups(tmp_path, paths, 0)

    groups = backup_object_set._payload_component_groups(tmp_path, paths, 6)
    assert groups == [
        ["payload/memory/memories.json"],
        ["payload/projects/p1/a.bin"],
        ["payload/projects/p1/b.bin"],
        ["payload/projects/p2/c.bin"],
    ]


def test_object_set_member_validates_receipt_commitment(tmp_path: Path) -> None:
    store = backup_target_store.MemoryTargetStore()
    control = _component(tmp_path, "member-control", b"control", control=True)
    payload = _component(tmp_path, "member-payload", b"payload")
    package = backup_object_set.ObjectSetPackage(
        backup_id="backup_member_validation",
        components=(control, payload),
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        manifest={"snapshotKind": "full"},
    )
    receipt = backup_publish.receipt_for(
        package,
        run_id="run-member-validation",
        policy_id="policy-member-validation",
        target_id="target-member-validation",
        schedule_slot="slot-member-validation",
    )
    for component in package.components:
        store.put_if_absent(
            backup_target_store.object_key(component.ciphertext_digest),
            component.path.read_bytes(),
            checksum_sha256=component.ciphertext_digest,
        )
    member = backup_remote_restore._object_set_member(store, receipt, tmp_path / "restore", 0)
    assert member["objectSetDigest"] == package.object_set_digest
    assert member["control"]["objectDigest"] == control.ciphertext_digest

    with pytest.raises(AppError, match="invalid object set"):
        backup_remote_restore._object_set_member(store, {**receipt, "controlObjectDigest": "invalid"}, tmp_path, 0)
    with pytest.raises(AppError, match="digest mismatch"):
        backup_remote_restore._object_set_member(store, {**receipt, "objectSetDigest": "f" * 64}, tmp_path, 0)
    with pytest.raises(AppError, match="control object is foreign"):
        backup_remote_restore._object_set_member(store, {**receipt, "controlObjectDigest": "e" * 64}, tmp_path, 0)
    store.delete_if_match(backup_target_store.object_key(payload.ciphertext_digest))
    with pytest.raises(AppError, match="component is missing"):
        backup_remote_restore._object_set_member(store, receipt, tmp_path, 0)


def test_object_set_spool_cleanup_and_multipart_state_fail_closed(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del tmp_settings
    assert backup_spool.cleanup_expired(ttl_seconds=0) == {"removed": 0, "freedBytes": 0}
    policy = backup_spool.SPOOL_DIR / "cleanup-policy"
    policy.mkdir(parents=True)
    (backup_spool.SPOOL_DIR / "not-a-policy-dir").write_text("ignored", encoding="utf-8")
    (policy / "not-a-slot-dir").write_text("ignored", encoding="utf-8")
    expired = policy / "expired-slot"
    expired.mkdir()
    (expired / "object-set.json").write_text("{}", encoding="utf-8")
    os.utime(expired / "object-set.json", (1, 1))
    removed = backup_spool.cleanup_expired(ttl_seconds=1)
    assert removed["removed"] == 1
    assert not expired.exists()

    recent = policy / "recent-slot"
    recent.mkdir()
    (recent / "payload.age").write_bytes(b"payload")
    monkeypatch.setattr(backup_spool, "DEFAULT_QUOTA_BYTES", 1)
    forced = backup_spool.cleanup_expired(ttl_seconds=10**12, force_oldest=True)
    assert forced["removed"] == 1
    assert not recent.exists()

    digest = "a" * 64
    backup_spool.write_component_multipart_state("multipart-policy", "b" * 64, digest, {"uploadId": "u"})
    assert backup_spool.read_component_multipart_state("multipart-policy", "b" * 64, digest) == {"uploadId": "u"}
    multipart = backup_spool.SPOOL_DIR / "multipart-policy" / ("b" * 64) / "multipart" / f"{digest}.json"
    multipart.write_text("not-json", encoding="utf-8")
    assert backup_spool.read_component_multipart_state("multipart-policy", "b" * 64, digest) is None
    multipart.write_text("[]", encoding="utf-8")
    assert backup_spool.read_component_multipart_state("multipart-policy", "b" * 64, digest) is None


def test_receipt_v4_commits_exact_ciphertext_set_without_roles(tmp_path: Path) -> None:
    control = _component(tmp_path, "control", b"control", control=True)
    payload = _component(tmp_path, "p0000", b"payload")
    package = backup_object_set.ObjectSetPackage(
        backup_id="backup_object_set",
        components=(control, payload),
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        manifest={"snapshotKind": "full"},
    )

    receipt = backup_publish.receipt_for(
        package,
        run_id="run_object_set",
        policy_id="policy_object_set",
        target_id="target_object_set",
        schedule_slot="slot_object_set",
    )

    assert receipt["schemaVersion"] == 4
    assert receipt["storageProtocol"] == backup_object_set.OBJECT_SET_V1
    assert receipt["controlObjectDigest"] == control.ciphertext_digest
    assert receipt["objectSetDigest"] == package.object_set_digest
    assert receipt["objects"] == backup_object_set.remote_object_inventory(package.components)
    serialized = str(receipt)
    for forbidden in ("componentId", "componentRole", "projectId", "plaintext", "logicalPath", "manifestDigest", "coverageDigest", "filename"):
        assert forbidden not in serialized


def test_build_object_set_independently_encrypts_control_and_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    files = {
        "payload/projects/p1/a.bin": b"same-payload",
        "payload/projects/p2/b.bin": b"same-payload",
    }
    manifest_files = []
    for relative, content in files.items():
        path = staging / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        manifest_files.append(
            {"contributorId": "projects", "path": relative, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        )
    manifest = {
        "schemaVersion": backups.BACKUP_SCHEMA,
        "purpose": backups.PACKAGE_PURPOSE,
        "backupId": "backup_components",
        "snapshotKind": "full",
        "files": manifest_files,
        "contributors": [{"id": "projects"}],
    }
    (staging / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (staging / "checksums.sha256").write_text("placeholder\n", encoding="utf-8")
    encryption_calls: list[Path] = []

    def random_encrypt(
        target: Path,
        write_plaintext: object,
        *,
        recipients: object,
        verify: object,
        cancel_event: object = None,
    ) -> backup_unattended.UnattendedEncryption:
        del recipients, cancel_event
        plain = io.BytesIO()
        write_plaintext(plain)  # type: ignore[operator]
        plaintext = plain.getvalue()
        verification = target.with_suffix(".verify.zip")
        verification.write_bytes(plaintext)
        verify(verification)  # type: ignore[operator]
        verification.unlink()
        encryption_calls.append(target)
        ciphertext = f"random-session-{len(encryption_calls)}\n".encode() + plaintext
        target.write_bytes(ciphertext)
        return backup_unattended.UnattendedEncryption(
            ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest(),
            size=len(ciphertext),
            creation_verified=True,
            recipients=("age1test",),
        )

    monkeypatch.setattr(backup_object_set.backup_unattended, "encrypt_unattended", random_encrypt)
    package = backup_object_set.build_encrypted_object_set(
        staging,
        tmp_path / "encrypted",
        backup_id="backup_components",
        recipients=("age1test",),
        manifest=manifest,
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        component_target_bytes=1,
    )

    assert package.storage_protocol == backup_object_set.OBJECT_SET_V1
    assert len(package.components) == 3
    assert len(encryption_calls) == 3
    assert encryption_calls[-1].name == "control.age"
    assert len({item.ciphertext_digest for item in package.components}) == 3
    control_ciphertext = package.control.path.read_bytes().split(b"\n", 1)[1]
    with zipfile.ZipFile(io.BytesIO(control_ciphertext)) as archive:
        payload_index = json.loads(archive.read("payload-index.json"))
        component_map = json.loads(archive.read("component-map.json"))
    assert set(payload_index["payloadComponents"]) == {"p0000", "p0001"}
    assert set(component_map["paths"]) == set(files)
    assert not list((tmp_path / "encrypted").glob("*.plaintext.tmp"))
    second = backup_object_set.build_encrypted_object_set(
        staging,
        tmp_path / "encrypted-second",
        backup_id="backup_components",
        recipients=("age1test",),
        manifest=manifest,
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        component_target_bytes=1,
    )
    assert [item.ciphertext_digest for item in package.components] != [item.ciphertext_digest for item in second.components]


def test_object_set_spool_reuses_exact_verified_ciphertexts(tmp_settings: Path) -> None:
    control = _component(tmp_settings, "control", b"control", control=True)
    payload = _component(tmp_settings, "p0000", b"payload")
    package = backup_object_set.ObjectSetPackage(
        backup_id="backup_spooled_set",
        components=(control, payload),
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        manifest={"snapshotKind": "full"},
    )

    meta = backup_spool.store_verified_package(
        package,
        policy_id="policy-set",
        schedule_slot="slot-set",
        slot_digest="d" * 64,
        run_id="run-set",
        run_plan_digest="e" * 64,
    )
    loaded = backup_spool.lookup_verified_package(
        policy_id="policy-set",
        slot_digest="d" * 64,
        run_plan_digest="e" * 64,
    )

    assert meta["storageProtocol"] == backup_object_set.OBJECT_SET_V1
    assert loaded is not None
    assert isinstance(loaded, backup_object_set.ObjectSetPackage)
    assert loaded.object_set_digest == package.object_set_digest
    slot = backup_spool.SPOOL_DIR / "policy-set" / ("d" * 64)
    assert (slot / "object-set.json").is_file()
    assert (slot / "control.age").read_bytes() == b"control"
    assert (slot / "payload" / "0000.age").read_bytes() == b"payload"
    serialized = (slot / "object-set.json").read_text(encoding="utf-8")
    for forbidden in ("plaintextSha256", "manifestDigest", "coverageDigest", "projectId", "logicalPath", "recoveryIdentity"):
        assert forbidden not in serialized

    assert backup_spool.store_verified_package(
        package,
        policy_id="policy-set",
        schedule_slot="slot-set",
        slot_digest="d" * 64,
        run_id="run-set-retry",
        run_plan_digest="e" * 64,
    ) == meta
    with pytest.raises(AppError, match="run plan digest mismatch"):
        backup_spool.store_verified_package(
            package,
            policy_id="policy-set",
            schedule_slot="slot-set",
            slot_digest="d" * 64,
            run_id="run-set-other-plan",
            run_plan_digest="f" * 64,
        )
    rival_payload = _component(tmp_settings, "spool-rival", b"spool-rival")
    rival = backup_object_set.ObjectSetPackage(
        backup_id="backup_spooled_rival",
        components=(control, rival_payload),
        manifest_digest="c" * 64,
        coverage_digest="d" * 64,
        manifest={"snapshotKind": "full"},
    )
    with pytest.raises(AppError, match="different object set"):
        backup_spool.store_verified_package(
            rival,
            policy_id="policy-set",
            schedule_slot="slot-set",
            slot_digest="d" * 64,
            run_id="run-set-rival",
        )


def test_object_set_spool_rejects_corrupt_metadata_and_members(tmp_settings: Path) -> None:
    control = _component(tmp_settings, "spool-check-control", b"control", control=True)
    payload = _component(tmp_settings, "spool-check-payload", b"payload")
    package = backup_object_set.ObjectSetPackage(
        backup_id="backup_spool_check",
        components=(control, payload),
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        manifest={"snapshotKind": "full"},
    )
    policy_id = "policy-spool-check"
    slot_digest = "9" * 64
    meta = backup_spool.store_verified_package(
        package,
        policy_id=policy_id,
        schedule_slot="slot-spool-check",
        slot_digest=slot_digest,
        run_id="run-spool-check",
    )

    def changed(mutator: object) -> dict[str, object]:
        value = json.loads(json.dumps(meta))
        mutator(value)  # type: ignore[operator]
        return value

    invalid = [
        changed(lambda value: value.__setitem__("schemaVersion", 0)),
        changed(lambda value: value.__setitem__("objects", "invalid")),
        changed(lambda value: value["objects"].__setitem__(0, "invalid")),
        changed(lambda value: value["objects"][1].__setitem__("path", "../escape.age")),
        changed(lambda value: value["objects"][1].__setitem__("digest", "invalid")),
        changed(lambda value: value.__setitem__("controlObjectDigest", "f" * 64)),
    ]
    assert all(backup_spool._load_spooled_object_set(policy_id, slot_digest, item) is None for item in invalid)

    slot = backup_spool.SPOOL_DIR / policy_id / slot_digest
    control_path = slot / "control.age"
    original = control_path.read_bytes()
    control_path.unlink()
    assert backup_spool._load_spooled_object_set(policy_id, slot_digest, meta) is None
    control_path.write_bytes(original)

    meta_path = slot / "object-set.json"
    meta_path.write_text("not-json", encoding="utf-8")
    assert backup_spool.read_object_set_meta(policy_id, slot_digest) is None
    meta_path.write_text("[]", encoding="utf-8")
    assert backup_spool.read_object_set_meta(policy_id, slot_digest) is None


def test_publish_v4_commits_every_ciphertext_member(tmp_settings: Path) -> None:
    control = _component(tmp_settings, "control-publish", b"control", control=True)
    payload = _component(tmp_settings, "payload-publish", b"payload")
    package = backup_object_set.ObjectSetPackage(
        backup_id="backup_published_set",
        components=(control, payload),
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        manifest={"snapshotKind": "full"},
    )
    target = backup_publish.resolve_target("managed-local")

    published = backup_publish.publish_backup(
        target,
        package,
        run_id="run-published-set",
        policy_id="policy-published-set",
        schedule_slot="slot-published-set",
        fencing_token=1,
    )

    assert published.receipt["schemaVersion"] == 4
    assert published.commit["schemaVersion"] == 4
    assert published.commit["objectSetDigest"] == package.object_set_digest
    assert published.commit["controlObjectDigest"] == control.ciphertext_digest
    assert "objectDigest" not in published.commit
    for component in package.components:
        assert backup_publish.object_path(backups.BACKUP_DIR, component.ciphertext_digest).read_bytes() == component.path.read_bytes()


def test_publish_v4_store_commits_and_converges_exact_ciphertext_set(tmp_settings: Path) -> None:
    store = backup_target_store.MemoryTargetStore()
    target = backup_publish.ResolvedTarget(
        target_id="target-object-set-store",
        root=None,
        managed=False,
        kind="s3",
        store=store,
    )
    control = _component(tmp_settings, "control-store", b"control-store", control=True)
    payload = _component(tmp_settings, "payload-store", b"payload-store")
    package = backup_object_set.ObjectSetPackage(
        backup_id="backup_object_set_store",
        components=(control, payload),
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        manifest={"snapshotKind": "full"},
    )

    first = backup_publish.publish_backup(
        target,
        package,
        run_id="run-object-set-store-1",
        policy_id="policy-object-set-store",
        schedule_slot="slot-object-set-store",
        fencing_token=1,
    )

    assert first.converged is False
    assert first.path is None
    assert first.receipt_path is None
    assert first.commit["schemaVersion"] == 4
    assert first.commit["objectSetDigest"] == package.object_set_digest
    assert first.receipt["objects"] == backup_object_set.remote_object_inventory(package.components)
    assert all(
        store.get_bytes(backup_target_store.object_key(component.ciphertext_digest)) == component.path.read_bytes()
        for component in package.components
    )
    stored_receipt = store.get_bytes(backup_target_store.receipt_key(package.backup_id))
    assert stored_receipt is not None
    for forbidden in (b"manifestDigest", b"coverageDigest", b"filename", b"plaintext"):
        assert forbidden not in stored_receipt

    converged = backup_publish.publish_backup(
        target,
        package,
        run_id="run-object-set-store-2",
        policy_id="policy-object-set-store",
        schedule_slot="slot-object-set-store",
        fencing_token=2,
    )
    assert converged.converged is True
    assert converged.commit == first.commit
    assert converged.receipt == first.receipt

    reused = backup_object_set.ObjectSetPackage(
        backup_id="backup_object_set_store_reused",
        components=package.components,
        manifest_digest="e" * 64,
        coverage_digest="f" * 64,
        manifest={"snapshotKind": "full"},
    )
    reused_result = backup_publish.publish_backup(
        target,
        reused,
        run_id="run-object-set-store-reused",
        policy_id="policy-object-set-store",
        schedule_slot="slot-object-set-store-reused",
        fencing_token=3,
    )
    assert reused_result.converged is False
    assert reused_result.commit["objectSetDigest"] == package.object_set_digest

    rival_payload = _component(tmp_settings, "payload-rival", b"payload-rival")
    rival = backup_object_set.ObjectSetPackage(
        backup_id="backup_object_set_store_rival",
        components=(control, rival_payload),
        manifest_digest="c" * 64,
        coverage_digest="d" * 64,
        manifest={"snapshotKind": "full"},
    )
    with pytest.raises(AppError, match="slot-commit-conflict"):
        backup_publish.publish_backup(
            target,
            rival,
            run_id="run-object-set-store-rival",
            policy_id="policy-object-set-store",
            schedule_slot="slot-object-set-store",
            fencing_token=3,
        )


def test_reconcile_store_rebuilds_object_set_receipt_and_catalog(tmp_settings: Path) -> None:
    store = backup_target_store.MemoryTargetStore()
    target_id = "target-object-set-reconcile"
    target = backup_publish.ResolvedTarget(target_id=target_id, root=None, managed=False, kind="s3", store=store)
    control = _component(tmp_settings, "control-reconcile-store", b"control-reconcile-store", control=True)
    payload = _component(tmp_settings, "payload-reconcile-store", b"payload-reconcile-store")
    package = backup_object_set.ObjectSetPackage(
        backup_id="backup_object_set_reconcile_store",
        components=(control, payload),
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        manifest={"snapshotKind": "full"},
    )
    published = backup_publish.publish_backup(
        target,
        package,
        run_id="run-object-set-reconcile-store",
        policy_id="policy-object-set-reconcile-store",
        schedule_slot="slot-object-set-reconcile-store",
        fencing_token=1,
    )
    assert store.delete_if_match(backup_target_store.receipt_key(package.backup_id)) is True
    assert store.delete_if_match("control/head.json") is True

    writer = backup_writer_lease.TargetWriterLease(
        store=store,
        target_id=target_id,
        owner_run_id="run-reconcile-store",
        owner_instance_id="test",
        fencing_token=2,
    )
    writer.acquire()
    try:
        report = backup_reconcile.reconcile_target_store(
            store,
            target_id=target_id,
            writer=writer,
            now=datetime(2026, 8, 11, tzinfo=timezone.utc),
        )
    finally:
        writer.release()

    assert report["rebuiltReceipts"] == [package.backup_id]
    assert report["catalogBackfills"] == [package.backup_id]
    assert report["headAdvanced"] is True
    assert report["invalidMarkers"] == []
    assert backup_catalog.catalog_state_store(store)[package.backup_id]["objectSetDigest"] == package.object_set_digest
    rebuilt = store.get_bytes(backup_target_store.receipt_key(package.backup_id))
    assert rebuilt is not None
    assert hashlib.sha256(rebuilt).hexdigest() == published.commit["receiptDigest"]


def test_publish_v4_store_converges_when_commit_cas_loses_race(tmp_settings: Path) -> None:
    class CommitRaceStore(backup_target_store.MemoryTargetStore):
        def __init__(self) -> None:
            super().__init__()
            self.raced = False

        def put_if_absent(
            self,
            key: str,
            source: BinaryIO | bytes,
            *,
            checksum_sha256: str | None = None,
            content_type: str = "application/octet-stream",
        ) -> backup_target_store.PutResult:
            result = super().put_if_absent(
                key,
                source,
                checksum_sha256=checksum_sha256,
                content_type=content_type,
            )
            if key.startswith("commits/") and not self.raced:
                self.raced = True
                raise AppError("conditional-create-failed: simulated winner", status=412)
            return result

    store = CommitRaceStore()
    target = backup_publish.ResolvedTarget(target_id="target-commit-race", root=None, managed=False, kind="s3", store=store)
    control = _component(tmp_settings, "control-commit-race", b"control-race", control=True)
    payload = _component(tmp_settings, "payload-commit-race", b"payload-race")
    package = backup_object_set.ObjectSetPackage(
        backup_id="backup_object_set_commit_race",
        components=(control, payload),
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        manifest={"snapshotKind": "full"},
    )

    published = backup_publish.publish_backup(
        target,
        package,
        run_id="run-object-set-commit-race",
        policy_id="policy-object-set-commit-race",
        schedule_slot="slot-object-set-commit-race",
        fencing_token=1,
    )
    assert published.converged is True
    assert published.commit["objectSetDigest"] == package.object_set_digest
    assert store.raced is True


def test_reconcile_store_rejects_invalid_markers_missing_members_and_bad_orphans(tmp_settings: Path) -> None:
    store = backup_target_store.MemoryTargetStore()
    target_id = "target-reconcile-negative"
    target = backup_publish.ResolvedTarget(target_id=target_id, root=None, managed=False, kind="s3", store=store)
    control = _component(tmp_settings, "control-reconcile-negative", b"control-negative", control=True)
    payload = _component(tmp_settings, "payload-reconcile-negative", b"payload-negative")
    package = backup_object_set.ObjectSetPackage(
        backup_id="backup_reconcile_negative",
        components=(control, payload),
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        manifest={"snapshotKind": "full"},
    )
    backup_publish.publish_backup(
        target,
        package,
        run_id="run-reconcile-negative",
        policy_id="policy-reconcile-negative",
        schedule_slot="slot-reconcile-negative",
        fencing_token=1,
    )
    store.delete_if_match(backup_target_store.object_key(payload.ciphertext_digest))
    backup_target_store.put_json_if_absent(
        store,
        "commits/invalid.json",
        {"backupId": "backup-invalid-marker", "commitHash": "invalid"},
    )
    absent_digest = "e" * 64
    absent_inventory = [{"digest": absent_digest, "size": 10}]
    backup_target_store.put_json_if_absent(
        store,
        "transactions/run-absent-orphan.json",
        {
            "runId": "run-absent-orphan",
            "phase": "objects-published",
            "updatedAt": "2000-01-01T00:00:00Z",
            "objects": absent_inventory,
            "objectSetDigest": backup_object_set.object_inventory_digest(absent_inventory),
        },
    )
    backup_target_store.put_json_if_absent(
        store,
        "transactions/run-invalid-orphan.json",
        {
            "runId": "run-invalid-orphan",
            "phase": "objects-published",
            "updatedAt": "2000-01-01T00:00:00Z",
            "objects": [{"digest": "invalid", "size": -1}],
            "objectSetDigest": "f" * 64,
        },
    )
    writer = backup_writer_lease.TargetWriterLease(
        store=store,
        target_id=target_id,
        owner_run_id="run-reconcile-negative-writer",
        owner_instance_id="test",
        fencing_token=2,
    )
    writer.acquire()
    try:
        report = backup_reconcile.reconcile_target_store(
            store,
            target_id=target_id,
            writer=writer,
            now=datetime(2026, 8, 11, tzinfo=timezone.utc),
            orphan_grace_seconds=1,
        )
    finally:
        writer.release()

    assert len(report["invalidMarkers"]) == 2
    assert sorted(report["orphanedTransactions"]) == ["run-absent-orphan", "run-invalid-orphan"]
    assert report["orphanedObjects"] == []


@pytest.mark.parametrize("failure", ["truncated", "digest", "receipt"])
def test_publish_v4_store_fails_closed_on_existing_corruption(tmp_settings: Path, failure: str) -> None:
    store = backup_target_store.MemoryTargetStore()
    target = backup_publish.ResolvedTarget(
        target_id=f"target-object-set-{failure}",
        root=None,
        managed=False,
        kind="s3",
        store=store,
    )
    control = _component(tmp_settings, f"control-{failure}", b"control-bytes", control=True)
    payload = _component(tmp_settings, f"payload-{failure}", b"payload-bytes")
    package = backup_object_set.ObjectSetPackage(
        backup_id=f"backup_object_set_{failure}",
        components=(control, payload),
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        manifest={"snapshotKind": "full"},
    )
    if failure in {"truncated", "digest"}:
        corrupt = b"x" if failure == "truncated" else b"x" * control.ciphertext_size
        store.put_if_absent(
            backup_target_store.object_key(control.ciphertext_digest),
            corrupt,
            checksum_sha256=hashlib.sha256(corrupt).hexdigest(),
        )
    else:
        store.put_if_absent(
            backup_target_store.receipt_key(package.backup_id),
            b"{}\n",
            checksum_sha256=hashlib.sha256(b"{}\n").hexdigest(),
            content_type="application/json",
        )

    expected = "missing or truncated" if failure == "truncated" else "fails its digest" if failure == "digest" else "already exists"
    with pytest.raises(AppError, match=expected):
        backup_publish.publish_backup(
            target,
            package,
            run_id=f"run-object-set-{failure}",
            policy_id=f"policy-object-set-{failure}",
            schedule_slot=f"slot-object-set-{failure}",
            fencing_token=1,
            checkpoint=lambda: None,
        )


@pytest.mark.parametrize("failure", ["missing-source", "corrupt-target", "journal-io"])
def test_publish_v4_filesystem_fails_closed_on_component_or_io_error(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    control = _component(tmp_settings, f"control-local-{failure}", b"control-local", control=True)
    payload = _component(tmp_settings, f"payload-local-{failure}", b"payload-local")
    package = backup_object_set.ObjectSetPackage(
        backup_id=f"backup_local_{failure}",
        components=(control, payload),
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        manifest={"snapshotKind": "full"},
    )
    if failure == "missing-source":
        payload.path.unlink()
        expected = "fails its ciphertext commitment"
    elif failure == "corrupt-target":
        destination = backup_publish.object_path(backups.BACKUP_DIR, payload.ciphertext_digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x" * payload.ciphertext_size)
        expected = "fails its digest"
    else:
        def fail_journal(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk unavailable")

        monkeypatch.setattr(backup_publish, "_write_journal", fail_journal)
        expected = "blocked-target-unavailable"

    with pytest.raises(AppError, match=expected):
        backup_publish.publish_backup(
            backup_publish.resolve_target("managed-local"),
            package,
            run_id=f"run-local-{failure}",
            policy_id=f"policy-local-{failure}",
            schedule_slot=f"slot-local-{failure}",
            fencing_token=1,
        )

    health = backup_scheduler.target_health()
    managed = next(item for item in health if item["targetId"] == "managed-local")
    assert managed["status"] == ("blocked" if failure == "journal-io" else "error")


def test_component_control_validation_rejects_malformed_metadata() -> None:
    control_digest = "a" * 64
    payload_digest = "b" * 64
    path = "payload/projects/p1/a.bin"
    member = {
        "controlObjectDigest": control_digest,
        "objects": [
            {"digest": control_digest, "size": 10},
            {"digest": payload_digest, "size": 20},
        ],
    }
    component_map = {
        "schemaVersion": 1,
        "paths": {path: "p0000"},
        "components": {"p0000": [path]},
    }
    payload_index = {
        "schemaVersion": 1,
        "storageProtocol": backup_object_set.OBJECT_SET_V1,
        "payloadComponents": {
            "p0000": {
                "ciphertextDigest": payload_digest,
                "ciphertextSize": 20,
                "plaintextSize": 5,
                "plaintextSha256": "c" * 64,
            }
        },
    }
    paths, descriptors = backup_remote_restore._validated_component_control(member, component_map, payload_index)
    assert paths == {path: "p0000"}
    assert set(descriptors) == {"p0000"}

    def cloned(value: object) -> object:
        return json.loads(json.dumps(value))

    invalid_cases: list[tuple[object, object, str]] = [
        (None, payload_index, "control metadata"),
        ({**component_map, "paths": []}, payload_index, "component map"),
        (component_map, {**payload_index, "payloadComponents": {}}, "inventory is inconsistent"),
        ({**component_map, "components": {"p0000": []}}, payload_index, "path map is invalid"),
        ({**component_map, "components": {"p0000": [path, path]}}, payload_index, "paths overlap"),
        ({**component_map, "paths": {}}, payload_index, "path map is inconsistent"),
        ({**component_map, "paths": {1: "p0000"}}, payload_index, "path map is invalid"),
        ({**component_map, "paths": {path: "wrong"}}, payload_index, "path map is inconsistent"),
        (component_map, {**payload_index, "payloadComponents": {"p0000": "invalid"}}, "payload descriptor is invalid"),
    ]
    for bad_map, bad_index, message in invalid_cases:
        with pytest.raises(AppError, match=message):
            backup_remote_restore._validated_component_control(member, bad_map, bad_index)

    bad_descriptor_index = cloned(payload_index)
    assert isinstance(bad_descriptor_index, dict)
    bad_descriptor_index["payloadComponents"]["p0000"]["ciphertextDigest"] = "not-a-digest"
    with pytest.raises(AppError, match="foreign component or invalid commitment"):
        backup_remote_restore._validated_component_control(member, component_map, bad_descriptor_index)

    foreign_member = cloned(member)
    assert isinstance(foreign_member, dict)
    foreign_member["objects"].append({"digest": "d" * 64, "size": 30})
    with pytest.raises(AppError, match="foreign component"):
        backup_remote_restore._validated_component_control(foreign_member, component_map, payload_index)


def test_reconcile_validates_exact_object_set_receipt_commitment(tmp_path: Path) -> None:
    control = _component(tmp_path, "reconcile-control", b"control", control=True)
    payload = _component(tmp_path, "reconcile-payload", b"payload")
    package = backup_object_set.ObjectSetPackage(
        backup_id="backup_reconcile_commitment",
        components=(control, payload),
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        manifest={"snapshotKind": "full"},
    )
    receipt = backup_publish.receipt_for(
        package,
        run_id="run-reconcile-commitment",
        policy_id="policy-reconcile-commitment",
        target_id="target-reconcile-commitment",
        schedule_slot="slot-reconcile-commitment",
    )
    receipt_bytes = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
    marker = {
        "schemaVersion": 4,
        "objectSetDigest": package.object_set_digest,
        "controlObjectDigest": control.ciphertext_digest,
        "receiptDigest": receipt_digest,
    }
    expected = {component.ciphertext_digest for component in package.components}
    assert backup_reconcile._committed_receipt_objects(marker, receipt, receipt_digest=receipt_digest) == expected

    for field, value in (
        ("schemaVersion", 3),
        ("receiptDigest", "f" * 64),
        ("objectSetDigest", "e" * 64),
        ("controlObjectDigest", "d" * 64),
    ):
        invalid_marker = {**marker, field: value}
        assert backup_reconcile._committed_receipt_objects(
            invalid_marker,
            receipt,
            receipt_digest=receipt_digest,
        ) == set()
    assert backup_reconcile._committed_receipt_objects(marker, receipt, receipt_digest=None) == set()
    invalid_receipt = {**receipt, "objectSetDigest": "c" * 64}
    assert backup_reconcile._committed_receipt_objects(
        marker,
        invalid_receipt,
        receipt_digest=receipt_digest,
    ) == set()


def test_object_set_fetch_fails_closed_and_resumes_partial_members(tmp_settings: Path) -> None:
    store = backup_target_store.MemoryTargetStore()
    data = b"encrypted-component-bytes"
    digest = hashlib.sha256(data).hexdigest()
    key = backup_target_store.object_key(digest)
    store.put_if_absent(key, data, checksum_sha256=digest)
    meta = store.stat(key)
    assert meta is not None

    def control_session(restore_id: str, control: object) -> dict[str, object]:
        return {
            "restoreId": restore_id,
            "storageProtocol": backup_object_set.OBJECT_SET_V1,
            "controlIndex": 0,
            "chain": [{"backupId": "backup-fetch-unit", "control": control}],
        }

    control = {
        "objectDigest": digest,
        "expectedBytes": len(data),
        "downloadedBytes": 0,
        "remoteETag": meta.etag,
        "remoteVersionId": None,
        "ciphertextPath": str(tmp_settings / "fetch-control.age"),
    }
    with pytest.raises(AppError, match="descriptor is invalid"):
        backup_remote_restore._fetch_object_set_controls(control_session("restore-invalid-control", None), store, max_bytes=None)
    missing = {**control, "objectDigest": "0" * 64}
    with pytest.raises(AppError, match="control object is missing"):
        backup_remote_restore._fetch_object_set_controls(control_session("restore-missing-control", missing), store, max_bytes=None)
    changed = {**control, "remoteETag": '"different"'}
    with pytest.raises(AppError, match="control object changed"):
        backup_remote_restore._fetch_object_set_controls(control_session("restore-changed-control", changed), store, max_bytes=None)

    class VersionedStore(backup_target_store.MemoryTargetStore):
        def stat(self, object_key: str) -> backup_target_store.ObjectMeta | None:
            observed = super().stat(object_key)
            if observed is None:
                return None
            return backup_target_store.ObjectMeta(
                key=observed.key,
                size=observed.size,
                etag=observed.etag,
                sha256=observed.sha256,
                last_modified=observed.last_modified,
                version_id="v2",
            )

    versioned = VersionedStore()
    versioned.put_if_absent(key, data, checksum_sha256=digest)
    version_control = {**control, "remoteETag": "", "remoteVersionId": "v1"}
    with pytest.raises(AppError, match="control object version changed"):
        backup_remote_restore._fetch_object_set_controls(
            control_session("restore-version-control", version_control),
            versioned,
            max_bytes=None,
        )

    resumable = control_session("restore-partial-control", dict(control))
    partial = backup_remote_restore._fetch_object_set_controls(resumable, store, max_bytes=1)
    assert partial["phase"] == "fetching-controls"
    assert partial["downloadedBytes"] == 1
    assert backup_remote_restore._fetch_object_set_controls(resumable, store, max_bytes=None)["phase"] == "controls-fetched"

    component = {
        "objectDigest": digest,
        "expectedBytes": len(data),
        "downloadedBytes": 0,
        "remoteETag": meta.etag,
        "remoteVersionId": None,
        "ciphertextPath": str(tmp_settings / "fetch-payload.age"),
    }
    payload_session: dict[str, object] = {
        "restoreId": "restore-partial-payload",
        "storageProtocol": backup_object_set.OBJECT_SET_V1,
        "componentFetchIndex": 0,
        "chain": [
            {
                "backupId": "backup-fetch-unit",
                "control": {"downloadedBytes": len(data)},
                "requiredComponents": [component],
            }
        ],
    }
    partial_payload = backup_remote_restore._fetch_object_set_components(payload_session, store, max_bytes=1)
    assert partial_payload["phase"] == "fetching-selected-components"
    assert backup_remote_restore._fetch_object_set_components(payload_session, store, max_bytes=None)["phase"] == "components-fetched"

    for field, value, message in (
        ("objectDigest", "f" * 64, "component is missing"),
        ("remoteETag", '"different"', "payload component changed"),
    ):
        invalid_component = {**component, field: value, "ciphertextPath": str(tmp_settings / f"invalid-{field}.age")}
        invalid_session = {
            "restoreId": f"restore-invalid-{field}",
            "storageProtocol": backup_object_set.OBJECT_SET_V1,
            "chain": [{"control": {"downloadedBytes": 0}, "requiredComponents": [invalid_component]}],
        }
        with pytest.raises(AppError, match=message):
            backup_remote_restore._fetch_object_set_components(invalid_session, store, max_bytes=None)

    version_component = {**component, "remoteETag": "", "remoteVersionId": "v1"}
    version_session = {
        "restoreId": "restore-version-payload",
        "storageProtocol": backup_object_set.OBJECT_SET_V1,
        "chain": [{"control": {"downloadedBytes": 0}, "requiredComponents": [version_component]}],
    }
    with pytest.raises(AppError, match="payload component version changed"):
        backup_remote_restore._fetch_object_set_components(version_session, versioned, max_bytes=None)

    with pytest.raises(AppError, match="no valid chain"):
        backup_remote_restore.restore_members({"storageProtocol": backup_object_set.OBJECT_SET_V1, "chain": []})
    with pytest.raises(AppError, match="no valid chain"):
        backup_remote_restore.restore_members({"snapshotKind": "incremental", "chain": []})

    wrong_digest = "1" * 64
    wrong_store = backup_target_store.MemoryTargetStore()
    wrong_store.put_if_absent(backup_target_store.object_key(wrong_digest), data, checksum_sha256=digest)
    wrong_member = {
        "backupId": "backup-wrong-digest",
        "objectDigest": wrong_digest,
        "expectedBytes": len(data),
        "downloadedBytes": 0,
        "ciphertextPath": str(tmp_settings / "wrong-digest.age"),
    }
    with pytest.raises(AppError, match="digest mismatch"):
        backup_remote_restore._download_member(wrong_store, wrong_member, None)


def test_object_set_projection_rejects_invalid_control_session(
    tmp_settings: Path,
    object_set_crypto: None,
) -> None:
    package = _full_object_set_package(tmp_settings)
    backup_publish.publish_backup(
        backup_publish.resolve_target("managed-local"),
        package,
        run_id="run-invalid-control-session",
        policy_id="policy-invalid-control-session",
        schedule_slot="slot-invalid-control-session",
        fencing_token=1,
    )
    created = backup_remote_restore.create_restore_from_target(
        target_id="managed-local",
        backup_id=package.backup_id,
        selection=None,
    )
    restore_id = str(created["restoreId"])
    backup_remote_restore.fetch_restore_session(restore_id)
    original = backup_remote_restore.read_restore_session(restore_id)
    assert original is not None

    projection, network = backup_remote_restore._plan_object_set_projection(
        original,
        kind="passphrase",
        secret=bytearray(b"secret"),
    )
    assert projection is None
    assert network["networkSelective"] is True
    assert network["requiredComponents"] == len([component for component in package.components if not component.control])

    invalid_descriptor = json.loads(json.dumps(original))
    invalid_descriptor["restoreId"] = "restore-invalid-control-descriptor"
    invalid_descriptor["chain"][0]["control"] = None
    with pytest.raises(AppError, match="descriptor is invalid"):
        backup_remote_restore._plan_object_set_projection(
            invalid_descriptor,
            kind="passphrase",
            secret=bytearray(b"secret"),
        )

    unavailable = json.loads(json.dumps(original))
    unavailable["restoreId"] = "restore-unavailable-control"
    unavailable["chain"][0]["control"]["ciphertextPath"] = str(tmp_settings / "missing-control.age")
    with pytest.raises(AppError, match="ciphertext is unavailable"):
        backup_remote_restore._plan_object_set_projection(
            unavailable,
            kind="passphrase",
            secret=bytearray(b"secret"),
        )

    mismatched = json.loads(json.dumps(original))
    mismatched["restoreId"] = "restore-mismatched-control"
    mismatched["chain"][0]["backupId"] = "backup-foreign"
    with pytest.raises(AppError, match="manifest does not match"):
        backup_remote_restore._plan_object_set_projection(
            mismatched,
            kind="age-identity",
            secret=bytearray(b"secret"),
        )

    assert backup_remote_restore.fetch_restore_session(restore_id)["phase"] == "components-fetched"
    materialized = backup_remote_restore.materialize_restore_session(
        restore_id,
        kind="passphrase",
        secret=bytearray(b"secret"),
    )
    assert materialized["snapshotKind"] == "full"
    assert "projection" not in materialized


def test_object_set_materializer_rejects_invalid_component_metadata(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = "invalid-json"

    def fake_plan(
        _session: dict[str, object],
        *,
        kind: str,
        secret: bytearray,
    ) -> tuple[None, dict[str, object]]:
        del kind, secret
        return None, {}

    def fake_decrypt(
        _source: Path,
        target: Path,
        *,
        kind: str,
        secret: bytearray,
        cancel_event: object = None,
    ) -> None:
        del kind, secret, cancel_event
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"plaintext")

    def fake_extract(_archive: Path, destination: Path) -> dict[str, object]:
        destination.mkdir(parents=True, exist_ok=True)
        if case == "invalid-json":
            content = "not-json"
        elif case == "map-type":
            content = "[]"
        elif case == "required":
            content = json.dumps({"components": {}})
        else:
            content = json.dumps({"components": {"p0000": ["payload/a.bin"]}})
        (destination / "component-map.json").write_text(content, encoding="utf-8")
        return {}

    monkeypatch.setattr(backup_remote_restore, "_plan_object_set_projection", fake_plan)
    monkeypatch.setattr(backup_remote_restore.backup_crypto, "decrypt_file", fake_decrypt)
    monkeypatch.setattr(backup_remote_restore.backups, "extract_archive_metadata", fake_extract)
    for case, required, message in (
        ("invalid-json", [], "component map is invalid"),
        ("map-type", [], "component map is invalid"),
        ("required", [None], "required component is invalid"),
        ("path", [{"componentId": "missing"}], "path map is invalid"),
        (
            "plaintext",
            [
                {
                    "componentId": "p0000",
                    "ciphertextPath": str(tmp_settings / "payload.age"),
                    "plaintextSize": 1,
                    "plaintextSha256": "0" * 64,
                }
            ],
            "plaintext commitment mismatch",
        ),
    ):
        session = {
            "restoreId": f"restore-materialize-{case}",
            "storageProtocol": backup_object_set.OBJECT_SET_V1,
            "snapshotKind": "full",
            "phase": "components-fetched",
            "chain": [
                {
                    "backupId": "backup-materialize",
                    "control": {"ciphertextPath": str(tmp_settings / "control.age")},
                    "requiredComponents": required,
                }
            ],
        }
        with pytest.raises(AppError, match=message):
            backup_remote_restore._materialize_object_set_session(
                session,
                kind="passphrase",
                secret=bytearray(b"secret"),
                client=None,
            )


def test_object_set_scrub_and_unlock_drill_verify_every_member(
    tmp_settings: Path,
    object_set_crypto: None,
) -> None:
    package = _full_object_set_package(tmp_settings)
    published = backup_publish.publish_backup(
        backup_publish.resolve_target("managed-local"),
        package,
        run_id="run-scrub-object-set",
        policy_id="policy-scrub-object-set",
        schedule_slot="slot-scrub-object-set",
        fencing_token=1,
    )
    backup_catalog.append_receipt(backups.BACKUP_DIR, published.receipt)

    scrubbed = backup_scrub.scrub_backup(backups.BACKUP_DIR, package.backup_id)
    assert scrubbed["ok"] is True
    drill = backup_scrub.verify_unlock_drill(
        backups.BACKUP_DIR,
        package.backup_id,
        bytearray(b"AGE-SECRET-KEY-1TEST"),
        staged_root=tmp_settings / "drill-object-set",
    )
    assert drill["ok"] is True
    assert drill["contributors"] == 2


@pytest.mark.parametrize("failure", ["missing", "tampered"])
def test_object_set_unlock_drill_rejects_missing_or_tampered_member(
    tmp_settings: Path,
    object_set_crypto: None,
    failure: str,
) -> None:
    package = _full_object_set_package(tmp_settings)
    published = backup_publish.publish_backup(
        backup_publish.resolve_target("managed-local"),
        package,
        run_id=f"run-unlock-{failure}",
        policy_id=f"policy-unlock-{failure}",
        schedule_slot=f"slot-unlock-{failure}",
        fencing_token=1,
    )
    backup_catalog.append_receipt(backups.BACKUP_DIR, published.receipt)
    payload = next(component for component in package.components if not component.control)
    stored = backup_publish.object_path(backups.BACKUP_DIR, payload.ciphertext_digest)
    if failure == "missing":
        stored.unlink()
        expected = "member is missing"
    else:
        stored.write_bytes(b"x" * stored.stat().st_size)
        expected = "no longer matches"
    with pytest.raises(AppError, match=expected):
        backup_scrub.verify_unlock_drill(
            backups.BACKUP_DIR,
            package.backup_id,
            bytearray(b"AGE-SECRET-KEY-1TEST"),
            staged_root=tmp_settings / f"drill-{failure}",
        )


def test_object_set_unlock_rejects_semantically_invalid_control(
    tmp_settings: Path,
    object_set_crypto: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _full_object_set_package(tmp_settings)
    published = backup_publish.publish_backup(
        backup_publish.resolve_target("managed-local"),
        package,
        run_id="run-semantic-control",
        policy_id="policy-semantic-control",
        schedule_slot="slot-semantic-control",
        fencing_token=1,
    )
    source_zip = tmp_settings / "source-control.zip"
    source_meta = tmp_settings / "source-control"
    backup_crypto.decrypt_file(
        package.control.path,
        source_zip,
        kind="age-identity",
        secret=bytearray(b"identity"),
    )
    manifest = backups.extract_archive_metadata(source_zip, source_meta)
    original_index = json.loads((source_meta / "payload-index.json").read_text(encoding="utf-8"))
    original_map = json.loads((source_meta / "component-map.json").read_text(encoding="utf-8"))
    manifest_bytes = (source_meta / "manifest.json").read_bytes()
    case = "inventory"

    def fake_extract(_archive: Path, destination: Path) -> dict[str, object]:
        destination.mkdir(parents=True, exist_ok=True)
        payload_index = json.loads(json.dumps(original_index))
        component_map = json.loads(json.dumps(original_map))
        first_id = sorted(payload_index["payloadComponents"])[0]
        if case == "inventory":
            payload_index["payloadComponents"] = {}
        elif case == "metadata":
            payload_index["payloadComponents"][first_id] = "invalid"
        elif case == "ciphertext":
            payload_index["payloadComponents"][first_id]["ciphertextDigest"] = "f" * 64
        elif case == "plaintext":
            payload_index["payloadComponents"][first_id]["plaintextSha256"] = "0" * 64
        elif case == "foreign":
            payload_index["payloadComponents"] = {first_id: payload_index["payloadComponents"][first_id]}
            component_map["components"] = {first_id: component_map["components"][first_id]}
        (destination / "manifest.json").write_bytes(manifest_bytes)
        (destination / "payload-index.json").write_text(json.dumps(payload_index), encoding="utf-8")
        (destination / "component-map.json").write_text(json.dumps(component_map), encoding="utf-8")
        return manifest

    monkeypatch.setattr(backup_scrub.backups, "extract_archive_metadata", fake_extract)
    monkeypatch.setattr(backup_scrub.backup_object_set, "verify_control_metadata", lambda _destination: None)
    for case, message in (
        ("inventory", "inventory is invalid"),
        ("metadata", "metadata is invalid"),
        ("ciphertext", "not exactly committed"),
        ("plaintext", "plaintext commitment mismatch"),
        ("foreign", "foreign payload"),
    ):
        staged = tmp_settings / f"semantic-{case}"
        staged.mkdir()
        with pytest.raises(AppError, match=message):
            backup_scrub._unlock_object_set(
                backups.BACKUP_DIR,
                published.receipt,
                bytearray(b"identity"),
                staged=staged,
            )


def test_object_set_catalog_scan_reports_corrupt_receipt_and_unreferenced_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipts = tmp_path / "receipts"
    receipts.mkdir(parents=True)
    (receipts / "corrupt.json").write_text("not-json", encoding="utf-8")
    orphan = tmp_path / "objects" / "sha256" / "aa" / f"{'a' * 64}.age"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    records = {
        "backup-invalid-set": {
            "backupId": "backup-invalid-set",
            "storageProtocol": backup_object_set.OBJECT_SET_V1,
            "objects": [],
            "objectSetDigest": "b" * 64,
            "controlObjectDigest": "c" * 64,
        },
        "backup-trashed": {
            "backupId": "backup-trashed",
            "storageProtocol": backup_object_set.OBJECT_SET_V1,
            "trashed": True,
        },
    }
    monkeypatch.setattr(backup_catalog, "catalog_state", lambda _root: records)

    report = backup_catalog.find_orphans_and_missing(tmp_path)

    assert report["orphans"] == [orphan.name]
    assert report["missing"] == ["backup-invalid-set"]


def test_whole_age_lineage_forces_object_set_full_upgrade(tmp_settings: Path) -> None:
    del tmp_settings
    backup_incremental.record_committed_snapshot(
        target_id="target-upgrade",
        policy_id="policy-upgrade",
        backup_id="F0",
        parent_backup_id=None,
        base_backup_id="F0",
        chain_depth=0,
        root_digest=backup_incremental.snapshot_root([]),
        files=[],
        storage_protocol=backup_object_set.WHOLE_AGE_V1,
    )

    selected = backup_incremental.select_snapshot_plan(
        policy={"incremental": {"mode": "file-delta"}},
        target_id="target-upgrade",
        policy_id="policy-upgrade",
        index_available=True,
        contributor_schemas={},
    )

    assert selected[0] == "full"
    assert selected[-1] == "storage-protocol-upgrade"


def test_pack_index_v2_uses_compact_ordinal_ranges(tmp_settings: Path) -> None:
    payload = b"compact-index-payload"
    writer = backup_pack.PackWriter(tmp_settings)
    ref = writer.append(
        io.BytesIO(payload),
        expected_length=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )
    runtime_index = writer.finalize()
    stored_index = json.loads((tmp_settings / backup_pack.PACK_INDEX_PATH).read_text(encoding="utf-8"))

    assert stored_index["schemaVersion"] == 2
    assert stored_index["entries"] == [[0, 0, len(payload)]]
    assert all(len(entry) == 3 for entry in stored_index["entries"])
    assert "sha256" not in str(stored_index["entries"])
    assert runtime_index["entries"][ref["blobId"]]["pack"] == "payload/packs/0000.pack"
    parsed = backup_pack.parse_pack_index(tmp_settings)
    assert parsed["entries"][ref["blobId"]]["length"] == len(payload)


def test_object_set_restore_fetches_control_before_any_payload(tmp_settings: Path) -> None:
    control = _component(tmp_settings, "control-fetch", b"control", control=True)
    payload = _component(tmp_settings, "payload-fetch", b"payload")
    package = backup_object_set.ObjectSetPackage(
        backup_id="backup_fetch_set",
        components=(control, payload),
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        manifest={"snapshotKind": "full"},
    )
    published = backup_publish.publish_backup(
        backup_publish.resolve_target("managed-local"),
        package,
        run_id="run-fetch-set",
        policy_id="policy-fetch-set",
        schedule_slot="slot-fetch-set",
        fencing_token=1,
    )
    assert published.commit["objectSetDigest"] == package.object_set_digest

    created = backup_remote_restore.create_restore_from_target(
        target_id="managed-local",
        backup_id=package.backup_id,
        selection={"contributors": ["projects"], "projectIds": ["p1"]},
    )
    fetched = backup_remote_restore.fetch_restore_session(str(created["restoreId"]))

    assert created["phase"] == "fetching-controls"
    assert fetched["phase"] == "controls-fetched"
    session = backup_remote_restore.read_restore_session(str(created["restoreId"]))
    assert session is not None
    assert Path(session["chain"][0]["control"]["ciphertextPath"]).read_bytes() == b"control"
    assert not list((backups.RESTORE_DIR / str(created["restoreId"])).glob("payload-*.age"))


def test_object_set_restore_fails_closed_when_committed_component_is_missing(
    tmp_settings: Path,
    object_set_crypto: None,
) -> None:
    package = _full_object_set_package(tmp_settings)
    backup_publish.publish_backup(
        backup_publish.resolve_target("managed-local"),
        package,
        run_id="run-missing-set",
        policy_id="policy-missing-set",
        schedule_slot="slot-missing-set",
        fencing_token=1,
    )
    missing = next(item for item in package.components if not item.control)
    backup_publish.object_path(backups.BACKUP_DIR, missing.ciphertext_digest).unlink()

    with pytest.raises(AppError, match="component is missing"):
        backup_remote_restore.create_restore_from_target(
            target_id="managed-local",
            backup_id=package.backup_id,
            selection={"contributors": ["projects"], "projectIds": ["p1"]},
        )


def test_object_set_control_cannot_select_foreign_component(
    tmp_settings: Path,
    object_set_crypto: None,
) -> None:
    package = _full_object_set_package(tmp_settings)
    backup_publish.publish_backup(
        backup_publish.resolve_target("managed-local"),
        package,
        run_id="run-foreign-set",
        policy_id="policy-foreign-set",
        schedule_slot="slot-foreign-set",
        fencing_token=1,
    )
    created = backup_remote_restore.create_restore_from_target(
        target_id="managed-local",
        backup_id=package.backup_id,
        selection={"contributors": ["projects"], "projectIds": ["p1"]},
    )
    restore_id = str(created["restoreId"])
    backup_remote_restore.fetch_restore_session(restore_id)
    session = backup_remote_restore.read_restore_session(restore_id)
    assert session is not None
    member = session["chain"][0]
    selected_digest = next(item.ciphertext_digest for item in package.components if item.component_id == "p0001")
    member["objects"] = [item for item in member["objects"] if item["digest"] != selected_digest]
    backup_remote_restore._atomic_write_json(backup_remote_restore._session_path(restore_id), session)
    backup_crypto.put_secret(restore_id, "passphrase", "test-secret")

    with pytest.raises(AppError, match="foreign component"):
        backup_remote_restore.preview_restore_from_target(
            target_id="managed-local",
            backup_id=package.backup_id,
            selection={"contributors": ["projects"], "projectIds": ["p1"]},
            restore_id=restore_id,
        )


def test_object_set_preview_plans_and_fetches_only_selected_component(
    tmp_settings: Path,
    object_set_crypto: None,
) -> None:
    package = _full_object_set_package(tmp_settings)
    backup_publish.publish_backup(
        backup_publish.resolve_target("managed-local"),
        package,
        run_id="run-selective-set",
        policy_id="policy-selective-set",
        schedule_slot="slot-selective-set",
        fencing_token=1,
    )
    created = backup_remote_restore.create_restore_from_target(
        target_id="managed-local",
        backup_id=package.backup_id,
        selection={"contributors": ["projects"], "projectIds": ["p1"]},
    )
    restore_id = str(created["restoreId"])
    assert backup_remote_restore.fetch_restore_session(restore_id)["phase"] == "controls-fetched"
    backup_crypto.put_secret(restore_id, "passphrase", "test-secret")

    preview = backup_remote_restore.preview_restore_from_target(
        target_id="managed-local",
        backup_id=package.backup_id,
        selection={"contributors": ["projects"], "projectIds": ["p1"]},
        restore_id=restore_id,
    )

    assert preview["phase"] == "preview-planned"
    assert preview["projection"]["networkSelective"] is True
    assert preview["projection"]["requiredComponents"] == 1
    assert preview["projection"]["totalComponents"] == 3
    fetched = backup_remote_restore.fetch_restore_session(restore_id)
    assert fetched["phase"] == "components-fetched"
    session = backup_remote_restore.read_restore_session(restore_id)
    assert session is not None
    required = session["chain"][0]["requiredComponents"]
    assert len(required) == 1 and required[0]["componentId"] == "p0001"
    assert Path(required[0]["ciphertextPath"]).is_file()
    assert not (backups.RESTORE_DIR / restore_id / "payload-0000-p0000.age").exists()
    assert not (backups.RESTORE_DIR / restore_id / "payload-0000-p0002.age").exists()
    materialized = backup_remote_restore.materialize_restore_session(
        restore_id,
        kind="passphrase",
        secret=bytearray(b"test-secret"),
    )
    tree = Path(materialized["tree"])
    assert (tree / "payload/projects/p1/a.bin").read_bytes() == b"project-one-snapshot"
    assert not (tree / "payload/projects/p2").exists()
    assert not (tree / "payload/memory").exists()


def test_object_set_selective_restore_issues_gets_for_required_payload_only(
    tmp_settings: Path,
    object_set_crypto: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _full_object_set_package(tmp_settings)
    backup_publish.publish_backup(
        backup_publish.resolve_target("managed-local"),
        package,
        run_id="run-counted-gets",
        policy_id="policy-counted-gets",
        schedule_slot="slot-counted-gets",
        fencing_token=1,
    )
    get_keys: list[str] = []
    original_get = backup_target_store.FilesystemTargetStore.get_bytes
    original_stream = backup_target_store.FilesystemTargetStore.get_stream

    def counted_get(
        store: backup_target_store.FilesystemTargetStore,
        key: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> bytes | None:
        get_keys.append(key)
        return original_get(store, key, offset=offset, length=length)

    monkeypatch.setattr(backup_target_store.FilesystemTargetStore, "get_bytes", counted_get)

    def counted_stream(
        store: backup_target_store.FilesystemTargetStore,
        key: str,
        *,
        offset: int = 0,
    ) -> object:
        get_keys.append(key)
        return original_stream(store, key, offset=offset)

    monkeypatch.setattr(backup_target_store.FilesystemTargetStore, "get_stream", counted_stream)
    created = backup_remote_restore.create_restore_from_target(
        target_id="managed-local",
        backup_id=package.backup_id,
        selection={"contributors": ["projects"], "projectIds": ["p1"]},
    )
    restore_id = str(created["restoreId"])
    backup_remote_restore.fetch_restore_session(restore_id)
    backup_crypto.put_secret(restore_id, "passphrase", "test-secret")
    backup_remote_restore.preview_restore_from_target(
        target_id="managed-local",
        backup_id=package.backup_id,
        selection={"contributors": ["projects"], "projectIds": ["p1"]},
        restore_id=restore_id,
    )
    backup_remote_restore.fetch_restore_session(restore_id)

    payload_by_id = {item.component_id: item for item in package.components if not item.control}
    assert get_keys.count(backup_target_store.object_key(package.control.ciphertext_digest)) == 1
    assert get_keys.count(backup_target_store.object_key(payload_by_id["p0001"].ciphertext_digest)) == 1
    assert backup_target_store.object_key(payload_by_id["p0000"].ciphertext_digest) not in get_keys
    assert backup_target_store.object_key(payload_by_id["p0002"].ciphertext_digest) not in get_keys


def test_object_set_federated_restore_keeps_unselected_live_contributor(
    tmp_settings: Path,
    object_set_crypto: None,
) -> None:
    package = _full_object_set_package(tmp_settings)
    backup_publish.publish_backup(
        backup_publish.resolve_target("managed-local"),
        package,
        run_id="run-federated-set",
        policy_id="policy-federated-set",
        schedule_slot="slot-federated-set",
        fencing_token=1,
    )
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    live_memory = b'[{"id":"live","text":"live-diverged-after-backup"}]'
    config.MEMORY_FILE.write_bytes(live_memory)
    live_project = config.PROJECTS_DIR / "p1"
    live_project.mkdir(parents=True, exist_ok=True)
    (live_project / "a.bin").write_bytes(b"live-project-diverged-after-backup")
    (live_project / "post-backup.bin").write_bytes(b"must-be-removed")
    created = backup_remote_restore.create_restore_from_target(
        target_id="managed-local",
        backup_id=package.backup_id,
        selection={"contributors": ["projects"], "projectIds": ["p1"]},
    )
    restore_id = str(created["restoreId"])
    backup_remote_restore.fetch_restore_session(restore_id)
    backup_crypto.put_secret(restore_id, "passphrase", "test-secret")

    prepared = backup_remote_restore.materialize_federated_restore(
        restore_id,
        mode="merge",
        owner_document_id="server",
    )
    assert prepared["phase"] == "prepared"
    assert backups.commit_restore(restore_id)["phase"] == "backend-committed"
    assert backups.complete_restore(restore_id)["phase"] == "complete"
    backup_remote_restore.advance_federated_phase(restore_id, "complete")

    assert (config.PROJECTS_DIR / "p1" / "a.bin").read_bytes() == b"project-one-snapshot"
    assert not (config.PROJECTS_DIR / "p1" / "post-backup.bin").exists()
    assert not (config.PROJECTS_DIR / "p2").exists()
    assert config.MEMORY_FILE.read_bytes() == live_memory


def test_incremental_object_set_restores_post_baseline_project_and_support_component(
    tmp_settings: Path,
    object_set_crypto: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    f0_package, i1_package = _incremental_object_set_chain(tmp_settings)
    target = backup_publish.resolve_target("managed-local")
    backup_publish.publish_backup(
        target,
        f0_package,
        run_id="run-object-set-f0",
        policy_id="policy-object-set-chain",
        schedule_slot="slot-object-set-f0",
        fencing_token=1,
    )
    backup_publish.publish_backup(
        target,
        i1_package,
        run_id="run-object-set-i1",
        policy_id="policy-object-set-chain",
        schedule_slot="slot-object-set-i1",
        fencing_token=2,
    )
    backup_catalog.rebuild_catalog_from_receipts(backups.BACKUP_DIR)
    get_keys: list[str] = []
    original_get = backup_target_store.FilesystemTargetStore.get_bytes
    original_stream = backup_target_store.FilesystemTargetStore.get_stream

    def counted_get(
        store: backup_target_store.FilesystemTargetStore,
        key: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> bytes | None:
        get_keys.append(key)
        return original_get(store, key, offset=offset, length=length)

    monkeypatch.setattr(backup_target_store.FilesystemTargetStore, "get_bytes", counted_get)

    def counted_stream(
        store: backup_target_store.FilesystemTargetStore,
        key: str,
        *,
        offset: int = 0,
    ) -> object:
        get_keys.append(key)
        return original_stream(store, key, offset=offset)

    monkeypatch.setattr(backup_target_store.FilesystemTargetStore, "get_stream", counted_stream)
    created = backup_remote_restore.create_restore_from_target(
        target_id="managed-local",
        backup_id=i1_package.backup_id,
        selection={"contributors": ["projects"], "projectIds": ["p3"]},
    )
    restore_id = str(created["restoreId"])
    assert backup_remote_restore.fetch_restore_session(restore_id)["phase"] == "controls-fetched"
    backup_crypto.put_secret(restore_id, "passphrase", "test-secret")
    preview = backup_remote_restore.preview_restore_from_target(
        target_id="managed-local",
        backup_id=i1_package.backup_id,
        selection={"contributors": ["projects"], "projectIds": ["p3"]},
        restore_id=restore_id,
    )
    assert preview["phase"] == "preview-planned"
    assert preview["projection"]["requiredComponents"] == 2
    assert preview["projection"]["selected"]["projects"] == 1
    assert backup_remote_restore.fetch_restore_session(restore_id)["phase"] == "components-fetched"
    materialized = backup_remote_restore.materialize_restore_session(
        restore_id,
        kind="passphrase",
        secret=bytearray(b"test-secret"),
    )

    tree = Path(materialized["tree"])
    assert (tree / "payload/projects/p3/from-support.bin").read_bytes() == b"cross-project-support"
    assert (tree / "payload/projects/p3/new.bin").read_bytes() == b"new-project-payload"
    assert not (tree / "payload/projects/p1").exists()
    assert not (tree / "payload/projects/p2").exists()
    f0_payloads = {item.component_id: item for item in f0_package.components if not item.control}
    i1_payloads = {item.component_id: item for item in i1_package.components if not item.control}
    assert backup_target_store.object_key(f0_payloads["p0000"].ciphertext_digest) not in get_keys
    assert get_keys.count(backup_target_store.object_key(f0_payloads["p0001"].ciphertext_digest)) == 1
    assert get_keys.count(backup_target_store.object_key(i1_payloads["p0000"].ciphertext_digest)) == 1


def _run_restart_probe(tmp_settings: Path, command: dict[str, object]) -> dict[str, object]:
    environment = os.environ.copy()
    environment["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    repository_root = Path(__file__).resolve().parents[1]
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(repository_root) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    completed = subprocess.run(
        [sys.executable, "scripts/object_set_restart_probe.py"],
        cwd=repository_root,
        env=environment,
        input=json.dumps(command),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    value = json.loads(completed.stdout)
    assert isinstance(value, dict)
    return value


@pytest.mark.integration
@pytest.mark.slow
def test_object_set_restore_resumes_across_real_process_exits(tmp_settings: Path) -> None:
    if not bool(backup_crypto.capabilities().get("encryptedBackupAvailable")):
        pytest.skip("real backup crypto helper is unavailable")
    identity = backup_crypto.generate_identity()
    recovery_identity = str(identity["identity"])
    package = _full_object_set_package(tmp_settings, recipients=(str(identity["recipient"]),))
    backup_publish.publish_backup(
        backup_publish.resolve_target("managed-local"),
        package,
        run_id="run-restart-set",
        policy_id="policy-restart-set",
        schedule_slot="slot-restart-set",
        fencing_token=1,
    )
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    live_memory = b'[{"id":"live","text":"live-diverged-after-backup"}]'
    config.MEMORY_FILE.write_bytes(live_memory)
    live_project = config.PROJECTS_DIR / "p1"
    live_project.mkdir(parents=True, exist_ok=True)
    (live_project / "a.bin").write_bytes(b"live-project-diverged-after-backup")
    (live_project / "post-backup.bin").write_bytes(b"must-be-removed")

    process_a = _run_restart_probe(
        tmp_settings,
        {
            "action": "create-partial-fetch",
            "targetId": "managed-local",
            "backupId": package.backup_id,
            "selection": {"contributors": ["projects"], "projectIds": ["p1"]},
        },
    )
    assert process_a["phase"] == "fetching-controls"
    restore_id = str(process_a["restoreId"])
    process_b = _run_restart_probe(
        tmp_settings,
        {
            "action": "resume-and-prepare",
            "restoreId": restore_id,
            "secretKind": "age-identity",
            "secret": recovery_identity,
        },
    )
    assert process_b["phase"] == "prepared"
    process_c = _run_restart_probe(
        tmp_settings,
        {"action": "resume-commit-complete", "restoreId": restore_id},
    )
    assert process_c == {
        "restoreId": restore_id,
        "commitPhase": "backend-committed",
        "phase": "complete",
    }
    assert (config.PROJECTS_DIR / "p1" / "a.bin").read_bytes() == b"project-one-snapshot"
    assert not (config.PROJECTS_DIR / "p1" / "post-backup.bin").exists()
    assert not (config.PROJECTS_DIR / "p2").exists()
    assert config.MEMORY_FILE.read_bytes() == live_memory


def test_object_set_retention_hold_protects_every_ciphertext_member(tmp_settings: Path) -> None:
    store = backup_target_store.MemoryTargetStore()
    control = _component(tmp_settings, "retention-control", b"retention-control", control=True)
    payload = _component(tmp_settings, "retention-payload", b"retention-payload")
    package = backup_object_set.ObjectSetPackage(
        backup_id="backup_retention_set",
        components=(control, payload),
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        manifest={"snapshotKind": "full"},
    )
    receipt = backup_publish.receipt_for(
        package,
        run_id="run-retention-set",
        policy_id="policy-retention-set",
        target_id="target-retention-set",
        schedule_slot="slot-retention-set",
    )
    writer = backup_writer_lease.TargetWriterLease(
        store=store,
        target_id="target-retention-set",
        owner_run_id="run-retention-set",
        owner_instance_id="test",
        fencing_token=1,
    )
    writer.acquire()
    try:
        for component in package.components:
            store.put_if_absent(
                backup_target_store.object_key(component.ciphertext_digest),
                component.path.read_bytes(),
                checksum_sha256=component.ciphertext_digest,
            )
        backup_catalog.append_receipt_store(store, receipt, writer=writer)
        backup_catalog._append_entry_store(
            store,
            "trash",
            {
                "backupId": package.backup_id,
                "retentionRunId": "rr-object-set",
                "trashedAt": "2000-01-01T00:00:00Z",
            },
            writer=writer,
        )
        backup_target_store.put_json_if_absent(
            store,
            "holds/restore/restore-object-set.json",
            {
                "schemaVersion": 2,
                "expiresAt": "2099-01-01T00:00:00Z",
                "objects": backup_object_set.remote_object_inventory(package.components),
            },
        )
        retention = {"trashGraceHours": 1}
        now = datetime(2026, 8, 11, tzinfo=timezone.utc)
        protected = backup_retention.finalize_retention_store(retention, store, writer=writer, now=now)
        assert protected == {"deleted": [], "kept": [package.backup_id], "recoveredTrash": []}
        assert all(store.stat(backup_target_store.object_key(item.ciphertext_digest)) is not None for item in package.components)

        store.delete_if_match("holds/restore/restore-object-set.json")
        collected = backup_retention.finalize_retention_store(retention, store, writer=writer, now=now)
        assert collected["deleted"] == [package.backup_id]
        assert all(store.stat(backup_target_store.object_key(item.ciphertext_digest)) is None for item in package.components)
    finally:
        writer.release()


def test_object_set_orphan_components_are_collected_only_after_grace(tmp_settings: Path) -> None:
    store = backup_target_store.MemoryTargetStore()
    control = _component(tmp_settings, "orphan-control", b"orphan-control", control=True)
    payload = _component(tmp_settings, "orphan-payload", b"orphan-payload")
    package = backup_object_set.ObjectSetPackage(
        backup_id="backup_orphan_set",
        components=(control, payload),
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        manifest={"snapshotKind": "full"},
    )
    for component in package.components:
        store.put_if_absent(
            backup_target_store.object_key(component.ciphertext_digest),
            component.path.read_bytes(),
            checksum_sha256=component.ciphertext_digest,
        )
    backup_target_store.put_json_if_absent(
        store,
        "transactions/run-orphan-set.json",
        {
            "runId": "run-orphan-set",
            "phase": "objects-published",
            "updatedAt": "2000-01-01T00:00:00Z",
            "storageProtocol": backup_object_set.OBJECT_SET_V1,
            "objectSetDigest": package.object_set_digest,
            "controlObjectDigest": package.control.ciphertext_digest,
            "objects": backup_object_set.remote_object_inventory(package.components),
        },
    )
    writer = backup_writer_lease.TargetWriterLease(
        store=store,
        target_id="target-orphan-set",
        owner_run_id="reconcile-orphan-set",
        owner_instance_id="test",
        fencing_token=1,
    )
    writer.acquire()
    try:
        report = backup_reconcile.reconcile_target_store(
            store,
            target_id="target-orphan-set",
            writer=writer,
            now=datetime(2026, 8, 11, tzinfo=timezone.utc),
            orphan_grace_seconds=1,
        )
    finally:
        writer.release()
    expected = sorted(backup_target_store.object_key(item.ciphertext_digest) for item in package.components)
    assert report["orphanedObjects"] == expected
    assert all(store.stat(key) is None for key in expected)

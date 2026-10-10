"""Targeted test coverage boosters for backup_object_set and backup_incremental_restore."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_incremental_restore,
    backup_object_set,
)


def test_backup_object_set_data_structures(tmp_settings: Path) -> None:
    # 1. EncryptedComponent instance
    comp1 = backup_object_set.EncryptedComponent(
        component_id="control",
        path=tmp_settings / "control.dsiob",
        ciphertext_digest="a" * 64,
        ciphertext_size=128,
        control=True,
    )
    comp2 = backup_object_set.EncryptedComponent(
        component_id="payload_0001",
        path=tmp_settings / "payload_0001.dsiob",
        ciphertext_digest="c" * 64,
        ciphertext_size=512,
        control=False,
    )

    # 2. validate_components
    backup_object_set.validate_components([comp1, comp2])

    # 3. Component list helper functions
    inv = backup_object_set.remote_object_inventory([comp1, comp2])
    assert len(inv) == 2

    comm = backup_object_set.object_set_commitment([comp1, comp2])
    assert isinstance(comm, bytes)

    os_dig = backup_object_set.object_set_digest([comp1, comp2])
    assert len(os_dig) == 64

    inv_dig = backup_object_set.object_inventory_digest(inv)
    assert len(inv_dig) == 64

    total_bytes = backup_object_set.total_ciphertext_bytes([comp1, comp2])
    assert total_bytes == 128 + 512

    # 4. ObjectSetPackage container
    pkg = backup_object_set.ObjectSetPackage(
        backup_id="bk_test_obj_set",
        components=(comp1, comp2),
        manifest_digest="m" * 64,
        coverage_digest="c" * 64,
        manifest={"schemaVersion": 1},
    )
    assert pkg.storage_protocol == backup_object_set.OBJECT_SET_V1
    assert pkg.control.component_id == "control"
    assert len(pkg.object_set_digest) == 64
    assert pkg.filename == "bk_test_obj_set.object-set"
    assert pkg.size == 640
    assert len(pkg.ciphertext_sha256) == 64

    # 5. Path helpers
    assert backup_object_set._safe_relative_path("foo/bar.txt") == "foo/bar.txt"
    with pytest.raises(AppError):
        backup_object_set._safe_relative_path("../escape.txt")

    z_info = backup_object_set._zip_info("relative/path.json")
    assert z_info.filename == "relative/path.json"


def test_backup_incremental_restore_helpers(tmp_settings: Path) -> None:
    # 1. Manifest file parsing
    manifest = {
        "files": [
            {"contributorId": "workspace", "path": "file1.txt", "size": 100, "sha256": "1" * 64},
            {"contributorId": "memory", "path": "state.json", "size": 50, "sha256": "2" * 64},
        ]
    }
    records = backup_incremental_restore._manifest_files(manifest)
    assert len(records) == 2

    # 2. File hashing helper
    sample_path = tmp_settings / "hash_sample.txt"
    sample_path.write_text("Materialization test", encoding="utf-8")
    h_val = backup_incremental_restore._sha256_file(sample_path)
    assert len(h_val) == 64

    # 3. FilePayloadSource copy_to
    source_payload = backup_incremental_restore.FilePayloadSource(sample_path)
    out_buf = io.BytesIO()
    source_payload.copy_to(out_buf, expected_sha256=h_val, expected_length=len("Materialization test"))
    assert out_buf.getvalue() == b"Materialization test"

    # 4. Standalone path
    st_path = backup_incremental_restore._standalone_path(tmp_settings, "payload/dir/a.bin")
    assert st_path == tmp_settings / "payload" / "dir" / "a.bin"

    # 5. Chunk protocol detection
    proto = backup_incremental_restore._chunk_protocol({"chunkProtocol": "fastcdc-gear-v3"})
    assert proto == "fastcdc-gear-v3"

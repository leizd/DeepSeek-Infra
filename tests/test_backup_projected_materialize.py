"""Projection-aware materializer contracts (projected-recovery)."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_incremental,
    backup_incremental_restore,
    backup_pack,
)
from deepseek_infra.infra.workspace.backup_projection import ChainPackage, ProjectionPlan, RestoreSelection, plan_projection


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _rec(contributor: str, path: str, size: int, sha: str) -> backup_incremental.FileRecord:
    return backup_incremental.FileRecord(contributor, path, size, sha)


def _build_chain(tmp_path: Path) -> tuple[Path, Path, ProjectionPlan]:
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
    for contributor, relative in (("projects", "payload/projects/p1/keep.bin"), ("projects", "payload/projects/p2/source.bin"), ("memory", "payload/memory/memories.json")):
        content = {"payload/projects/p1/keep.bin": keep, "payload/projects/p2/source.bin": source, "payload/memory/memories.json": memory}[relative]
        path = f0_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    f0_manifest = {
        "schemaVersion": 1,
        "purpose": "deepseek-infra-workspace-backup",
        "snapshotKind": "full",
        "files": [
            {"contributorId": r.contributor_id, "path": r.logical_path, "size": r.size, "sha256": r.sha256} for r in f0_records
        ],
        "snapshot": {"kind": "full", "rootDigest": backup_incremental.snapshot_root(f0_records)},
    }
    (f0_root / "manifest.json").write_text(json.dumps(f0_manifest, sort_keys=True), encoding="utf-8")
    (f0_root / "checksums.sha256").write_text("", encoding="utf-8")

    i1_root = tmp_path / "i1"
    writer = backup_pack.PackWriter(i1_root)
    pack_ref = writer.append(io.BytesIO(new_content), expected_length=len(new_content), expected_sha256=_sha(new_content))
    writer.finalize()
    pack_index = json.loads((i1_root / backup_pack.PACK_INDEX_PATH).read_text(encoding="utf-8"))
    (i1_root / "delta").mkdir()
    (i1_root / "delta/operations.json").write_text(json.dumps(ops, sort_keys=True), encoding="utf-8")
    operations_bytes = (i1_root / "delta/operations.json").read_bytes()
    i1_manifest = {
        "schemaVersion": 1,
        "purpose": "deepseek-infra-workspace-backup",
        "snapshotKind": "incremental",
        "files": [
            {"contributorId": r.contributor_id, "path": r.logical_path, "size": r.size, "sha256": r.sha256} for r in final_records
        ],
        "snapshot": {"kind": "incremental", "format": "incremental-v5", "rootDigest": backup_incremental.snapshot_root(final_records)},
        "deltaFiles": [
            {"path": "delta/operations.json", "size": len(operations_bytes), "sha256": _sha(operations_bytes)},
            {"path": backup_pack.PACK_INDEX_PATH, "size": (i1_root / backup_pack.PACK_INDEX_PATH).stat().st_size, "sha256": _sha((i1_root / backup_pack.PACK_INDEX_PATH).read_bytes())},
            {"path": "payload/packs/0000.pack", "size": (i1_root / "payload/packs/0000.pack").stat().st_size, "sha256": _sha((i1_root / "payload/packs/0000.pack").read_bytes())},
        ],
    }
    (i1_root / "manifest.json").write_text(json.dumps(i1_manifest, sort_keys=True), encoding="utf-8")
    (i1_root / "checksums.sha256").write_text("", encoding="utf-8")

    assert pack_ref == {"kind": "pack-range", "blobId": "blob_000000"}
    baseline = ChainPackage(
        snapshot_kind="full",
        files=tuple(f0_records),
        root_digest=backup_incremental.snapshot_root(f0_records),
        contributor_ids=frozenset({"projects", "memory"}),
    )
    incremental = ChainPackage(
        snapshot_kind="incremental",
        files=tuple(final_records),
        root_digest=backup_incremental.snapshot_root(final_records),
        operations=ops,
        pack_index=pack_index,
    )
    projection = plan_projection(
        RestoreSelection(contributors=("projects",), project_ids=("p1",)),
        [baseline, incremental],
        ciphertext_download_bytes=0,
    )
    return f0_root, i1_root, projection


def test_projected_materialize_outputs_only_selected(tmp_path: Path) -> None:
    f0_root, i1_root, projection = _build_chain(tmp_path)
    output_root = tmp_path / "output"
    backup_incremental_restore.materialize_chain([f0_root, i1_root], output_root, projection=projection)

    assert (output_root / "payload/projects/p1/keep.bin").read_bytes() == b"keep"
    assert (output_root / "payload/projects/p1/restored.bin").read_bytes() == b"source-payload"
    assert (output_root / "payload/projects/p1/new.bin").read_bytes() == b"brand new payload"
    # Support files never reach the final tree.
    assert not (output_root / "payload/projects/p2").exists()
    # Unselected contributors are never mutated.
    assert not (output_root / "payload/memory").exists()
    # No scratch residue.
    assert not [path for path in output_root.iterdir() if path.name.startswith(".projection-support")]


def test_projected_materialize_verifies_full_chain(tmp_path: Path) -> None:
    f0_root, i1_root, projection = _build_chain(tmp_path)
    tampered = tmp_path / "i1-tampered"
    tampered.mkdir()
    for item in i1_root.rglob("*"):
        if item.is_file():
            relative = item.relative_to(i1_root)
            target = tampered / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.read_bytes())
    ops_path = tampered / "delta/operations.json"
    ops = json.loads(ops_path.read_text(encoding="utf-8"))
    ops["rootDigest"] = "0" * 64
    ops_path.write_text(json.dumps(ops, sort_keys=True), encoding="utf-8")
    with pytest.raises(AppError, match="Merkle root mismatch"):
        backup_incremental_restore.materialize_chain([f0_root, tampered], tmp_path / "output-bad", projection=projection)


def test_projected_materialize_fails_on_corrupt_support(tmp_path: Path) -> None:
    f0_root, i1_root, projection = _build_chain(tmp_path)
    (f0_root / "payload/projects/p2/source.bin").write_bytes(b"corrupted-source")
    with pytest.raises(AppError, match="checksum mismatch"):
        backup_incremental_restore.materialize_chain([f0_root, i1_root], tmp_path / "output-corrupt", projection=projection)


def test_projected_materialize_missing_baseline_entry_fails(tmp_path: Path) -> None:
    f0_root, i1_root, projection = _build_chain(tmp_path)
    (f0_root / "payload/projects/p2/source.bin").unlink()
    with pytest.raises(AppError, match="Projection baseline file is missing"):
        backup_incremental_restore.materialize_chain([f0_root, i1_root], tmp_path / "output-missing", projection=projection)


def test_materialize_chain_rejects_corrupt_members(tmp_path: Path) -> None:
    f0_root, i1_root, projection = _build_chain(tmp_path)

    no_manifest = tmp_path / "no-manifest"
    no_manifest.mkdir()
    with pytest.raises(AppError, match="missing manifest.json"):
        backup_incremental_restore.materialize_chain([no_manifest], tmp_path / "o1", projection=projection)

    bad_json = tmp_path / "bad-json"
    bad_json.mkdir()
    (bad_json / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(AppError, match="manifest is invalid"):
        backup_incremental_restore.materialize_chain([bad_json], tmp_path / "o2", projection=projection)

    bad_inventory = tmp_path / "bad-inventory"
    bad_inventory.mkdir()
    (bad_inventory / "manifest.json").write_text('{"files": ["junk"]}', encoding="utf-8")
    with pytest.raises(AppError, match="file inventory is invalid"):
        backup_incremental_restore.materialize_chain([bad_inventory], tmp_path / "o3", projection=projection)

    missing_ops = tmp_path / "missing-ops"
    missing_ops.mkdir()
    (missing_ops / "manifest.json").write_text('{"snapshotKind": "incremental", "files": []}', encoding="utf-8")
    with pytest.raises(AppError, match="missing its operations manifest"):
        backup_incremental_restore.materialize_chain([f0_root, missing_ops], tmp_path / "o4", projection=projection)

    invalid_ops = tmp_path / "invalid-ops"
    invalid_ops.mkdir()
    (invalid_ops / "delta").mkdir()
    (invalid_ops / "delta/operations.json").write_text('{"put": []}', encoding="utf-8")
    (invalid_ops / "manifest.json").write_text('{"snapshotKind": "incremental", "files": []}', encoding="utf-8")
    with pytest.raises(AppError, match="operations manifest is invalid"):
        backup_incremental_restore.materialize_chain([f0_root, invalid_ops], tmp_path / "o5", projection=projection)


def test_projected_materialize_error_branches(tmp_path: Path) -> None:
    # Baseline declared as incremental -> missing full baseline.
    f0_root, i1_root, projection = _build_chain(tmp_path / "case-inc")
    bad_baseline = tmp_path / "case-inc" / "f0-inc"
    shutil.copytree(f0_root, bad_baseline)
    manifest = json.loads((bad_baseline / "manifest.json").read_text(encoding="utf-8"))
    manifest["snapshotKind"] = "incremental"
    (bad_baseline / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    with pytest.raises(AppError, match="missing its full baseline"):
        backup_incremental_restore.materialize_chain([bad_baseline, i1_root], tmp_path / "case-inc/o-inc", projection=projection)

    # Baseline root digest mismatch.
    f0_root, i1_root, projection = _build_chain(tmp_path / "case-root")
    manifest = json.loads((f0_root / "manifest.json").read_text(encoding="utf-8"))
    manifest["snapshot"]["rootDigest"] = "0" * 64
    (f0_root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    with pytest.raises(AppError, match="Full baseline Merkle root mismatch"):
        backup_incremental_restore.materialize_chain([f0_root, i1_root], tmp_path / "case-root/o-root", projection=projection)

    # Parent root mismatch.
    f0_root, i1_root, projection = _build_chain(tmp_path / "case-parent")
    ops = json.loads((i1_root / "delta/operations.json").read_text(encoding="utf-8"))
    ops["parentRootDigest"] = "0" * 64
    (i1_root / "delta/operations.json").write_text(json.dumps(ops, sort_keys=True), encoding="utf-8")
    with pytest.raises(AppError, match="parent root mismatch"):
        backup_incremental_restore.materialize_chain([f0_root, i1_root], tmp_path / "case-parent/o-parent", projection=projection)

    # Non-dict put entry.
    f0_root, i1_root, projection = _build_chain(tmp_path / "case-put")
    ops = json.loads((i1_root / "delta/operations.json").read_text(encoding="utf-8"))
    ops["put"].append("junk")
    (i1_root / "delta/operations.json").write_text(json.dumps(ops, sort_keys=True), encoding="utf-8")
    with pytest.raises(AppError, match="Delta put entry is invalid"):
        backup_incremental_restore.materialize_chain([f0_root, i1_root], tmp_path / "case-put/o-put", projection=projection)

    # Non-dict delete entry.
    f0_root, i1_root, projection = _build_chain(tmp_path / "case-del-junk")
    ops = json.loads((i1_root / "delta/operations.json").read_text(encoding="utf-8"))
    ops["delete"] = ["junk"]
    (i1_root / "delta/operations.json").write_text(json.dumps(ops, sort_keys=True), encoding="utf-8")
    with pytest.raises(AppError, match="Delta delete entry is invalid"):
        backup_incremental_restore.materialize_chain([f0_root, i1_root], tmp_path / "case-del-junk/o-del-junk", projection=projection)

    # A delete that targets a needed file.
    f0_root, i1_root, projection = _build_chain(tmp_path / "case-del")
    ops = json.loads((i1_root / "delta/operations.json").read_text(encoding="utf-8"))
    ops["delete"] = [{"contributorId": "projects", "path": "payload/projects/p1/keep.bin"}]
    (i1_root / "delta/operations.json").write_text(json.dumps(ops, sort_keys=True), encoding="utf-8")
    with pytest.raises(AppError, match="Projection required file is deleted"):
        backup_incremental_restore.materialize_chain([f0_root, i1_root], tmp_path / "case-del/o-del", projection=projection)

    # Delete of an ordinary (non-needed) file path succeeds.
    f0_root, i1_root, projection = _build_chain(tmp_path / "case-del-ok")
    ops = json.loads((i1_root / "delta/operations.json").read_text(encoding="utf-8"))
    ops["delete"] = [{"contributorId": "projects", "path": "payload/memory/memories.json"}]
    (i1_root / "delta/operations.json").write_text(json.dumps(ops, sort_keys=True), encoding="utf-8")
    backup_incremental_restore.materialize_chain([f0_root, i1_root], tmp_path / "case-del-ok/o-del-ok", projection=projection)


def test_pack_handle_cache_rejects_pack_missing_from_index(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    writer = backup_pack.PackWriter(root)
    writer.append(io.BytesIO(b"aaaa"), expected_length=4, expected_sha256=_sha(b"aaaa"))
    writer.finalize()
    cache = backup_incremental_restore.PackHandleCache(root)
    with pytest.raises(AppError, match="missing from the index"):
        cache.handle("payload/packs/9999.pack")


def test_materialize_put_rejects_missing_parent_and_pack_blob(tmp_path: Path) -> None:
    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    package_root = tmp_path / "package"
    package_root.mkdir()
    writer = backup_pack.PackWriter(package_root)
    writer.append(io.BytesIO(b"aaaa"), expected_length=4, expected_sha256=_sha(b"aaaa"))
    writer.finalize()
    output_root = tmp_path / "output"
    output_root.mkdir()
    with pytest.raises(AppError, match="missing parent file"):
        backup_incremental_restore._materialize_put(
            package_root,
            parent_root,
            output_root / "x.bin",
            {"path": "x.bin", "storage": "parent-file", "parentPath": "nope.bin", "size": 1, "sha256": "0" * 64},
            chunk_protocol=backup_incremental.CURRENT_CDC_PROTOCOL,
        )
    with backup_incremental_restore.PackHandleCache(package_root) as cache:
        with pytest.raises(AppError, match="blob is missing"):
            backup_incremental_restore._materialize_put(
                package_root,
                parent_root,
                output_root / "y.bin",
                {"path": "y.bin", "storage": "whole", "payloadRef": {"kind": "pack-range", "blobId": "blob_missing"}, "size": 4, "sha256": _sha(b"aaaa")},
                chunk_protocol=backup_incremental.CURRENT_CDC_PROTOCOL,
                payload_cache=cache,
            )


def test_materialize_put_parent_file_and_standalone(tmp_path: Path) -> None:
    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    (parent_root / "src.bin").write_bytes(b"parent-bytes")
    package_root = tmp_path / "package"
    package_root.mkdir()
    standalone_dir = package_root / "payload" / "files"
    standalone_dir.mkdir(parents=True)
    standalone = b"standalone-bytes"
    (standalone_dir / "000000").write_bytes(standalone)
    output_root = tmp_path / "output"
    output_root.mkdir()
    chunk_protocol = backup_incremental.CURRENT_CDC_PROTOCOL

    parent_target = output_root / "parent-copy.bin"
    backup_incremental_restore._materialize_put(
        package_root,
        parent_root,
        parent_target,
        {
            "path": "parent-copy.bin",
            "storage": "parent-file",
            "parentPath": "src.bin",
            "size": len(b"parent-bytes"),
            "sha256": hashlib.sha256(b"parent-bytes").hexdigest(),
        },
        chunk_protocol=chunk_protocol,
    )
    assert parent_target.read_bytes() == b"parent-bytes"

    standalone_target = output_root / "standalone.bin"
    backup_incremental_restore._materialize_put(
        package_root,
        parent_root,
        standalone_target,
        {
            "path": "standalone.bin",
            "storage": "whole",
            "payloadRef": {"kind": "standalone", "path": "payload/files/000000"},
            "size": len(standalone),
            "sha256": hashlib.sha256(standalone).hexdigest(),
        },
        chunk_protocol=chunk_protocol,
    )
    assert standalone_target.read_bytes() == standalone

    # Unsupported storage fails closed.
    with pytest.raises(AppError, match="Unsupported delta storage"):
        backup_incremental_restore._materialize_put(
            package_root,
            parent_root,
            output_root / "bad.bin",
            {"path": "bad.bin", "storage": "mystery", "size": 1, "sha256": "0" * 64},
            chunk_protocol=chunk_protocol,
        )


def test_pack_handle_cache_verifies_only_used_packs(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    writer = backup_pack.PackWriter(root, target_pack_size=4, max_pack_size=8, alignment=8)
    first = b"aaaa"
    second = b"bbbb"
    first_ref = writer.append(io.BytesIO(first), expected_length=len(first), expected_sha256=_sha(first))
    second_ref = writer.append(io.BytesIO(second), expected_length=len(second), expected_sha256=_sha(second))
    writer.finalize()
    cache = backup_incremental_restore.PackHandleCache(root)
    assert cache.verified_pack_count == 0
    output = io.BytesIO()
    backup_incremental_restore.PackRangePayloadSource(cache, str(first_ref["blobId"])).copy_to(
        output, expected_sha256=_sha(first), expected_length=len(first)
    )
    assert output.getvalue() == first
    assert cache.verified_pack_count == 1
    assert cache.open_handle_count == 1
    second_pack = cache.index["entries"][second_ref["blobId"]]["pack"]
    assert cache.index["entries"][first_ref["blobId"]]["pack"] != second_pack

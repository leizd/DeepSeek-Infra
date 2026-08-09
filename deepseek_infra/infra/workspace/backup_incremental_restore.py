"""Incremental chain materializer (4.4.9).

Applies an ordered chain ``[F0, I1, ..., In]`` of decrypted, extracted backup
packages into a complete workspace tree, verifying the domain-separated Merkle
root at every snapshot transition. Whole-file deltas are restored by copying
payload blobs; content-defined (``fastcdc-gear-v2``) files are reconstructed
from parent ranges plus payload chunks. Any missing member or corrupt chunk
fails closed.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_incremental


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError("Chain package manifest is invalid", code=ErrorCode.INVALID_PAYLOAD) from exc
    if not isinstance(data, dict):
        raise AppError("Chain package manifest is invalid", code=ErrorCode.INVALID_PAYLOAD)
    return data


def _manifest_files(manifest: dict[str, Any]) -> list[backup_incremental.FileRecord]:
    records: list[backup_incremental.FileRecord] = []
    for item in manifest.get("files") or []:
        if not isinstance(item, dict):
            raise AppError("Chain package file inventory is invalid", code=ErrorCode.INVALID_PAYLOAD)
        records.append(
            backup_incremental.FileRecord(
                contributor_id=str(item.get("contributorId") or ""),
                logical_path=str(item["path"]),
                size=int(item.get("size") or 0),
                sha256=str(item.get("sha256") or ""),
            )
        )
    return records


def _read_delta_ops(package_root: Path) -> dict[str, Any]:
    ops_path = package_root / "delta" / "operations.json"
    if not ops_path.is_file():
        raise AppError("Incremental chain member is missing its operations manifest", code=ErrorCode.INVALID_PAYLOAD)
    ops = _read_json(ops_path)
    if "put" not in ops or "delete" not in ops:
        raise AppError("Incremental chain operations manifest is invalid", code=ErrorCode.INVALID_PAYLOAD)
    return ops


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _copy_full_tree(package_root: Path, output_root: Path) -> None:
    for top in ("payload", "migration", "frontend"):
        source = package_root / top
        if source.is_dir():
            shutil.copytree(source, output_root / top, dirs_exist_ok=True)


def _chunk_ranges_for(parent_bytes: bytes) -> list[tuple[int, int]]:
    chunks = backup_incremental.chunk_stream(
        io.BytesIO(parent_bytes),
        file_size=len(parent_bytes),
    )
    return [(int(item["offset"]), int(item["length"])) for item in chunks]


def _parent_bytes(output_root: Path, parent_files: dict[str, bytes], logical_path: str) -> bytes:
    data = parent_files.get(logical_path)
    if data is not None:
        return data
    path = output_root / logical_path
    if not path.is_file():
        raise AppError(f"Delta references a missing parent file: {logical_path}", code=ErrorCode.INVALID_PAYLOAD)
    data = path.read_bytes()
    parent_files[logical_path] = data
    return data


def _materialize_put(package_root: Path, output_root: Path, put: dict[str, Any], parent_files: dict[str, bytes]) -> None:
    target = output_root / str(put["path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    storage = str(put.get("storage") or "whole")
    if storage == "cdc":
        _materialize_cdc(output_root, package_root, target, put, parent_files)
    elif storage == "whole":
        ref = str(put.get("payloadRef") or "")
        source = package_root / ref
        if not source.is_file():
            raise AppError(f"Delta payload blob is missing: {ref}", code=ErrorCode.INVALID_PAYLOAD)
        shutil.copyfile(source, target)
    else:
        raise AppError(f"Unsupported delta storage: {storage}", code=ErrorCode.INVALID_PAYLOAD)
    expected = str(put.get("sha256") or "")
    if expected and _sha256_file(target) != expected:
        raise AppError(f"Delta file failed checksum after restore: {put['path']}", code=ErrorCode.INVALID_PAYLOAD)


def _materialize_cdc(output_root: Path, package_root: Path, target: Path, put: dict[str, Any], parent_files: dict[str, bytes]) -> None:
    logical_path = str(put["path"])
    parent_data = _parent_bytes(output_root, parent_files, logical_path)
    parent_ranges = _chunk_ranges_for(parent_data)
    chunks = put.get("chunks")
    if not isinstance(chunks, list):
        raise AppError(f"Delta CDC file has no chunk list: {logical_path}", code=ErrorCode.INVALID_PAYLOAD)
    with target.open("wb") as output:
        for index, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                raise AppError(f"Delta CDC file has an invalid chunk: {logical_path}", code=ErrorCode.INVALID_PAYLOAD)
            length = int(chunk.get("length") or 0)
            if str(chunk.get("source") or "") == "parent":
                ordinal = int(chunk.get("parentOrdinal") or -1)
                if ordinal < 0 or ordinal >= len(parent_ranges):
                    raise AppError(f"Delta CDC references an invalid parent chunk: {logical_path}", code=ErrorCode.INVALID_PAYLOAD)
                offset, chunk_length = parent_ranges[ordinal]
                if chunk_length != length:
                    raise AppError(f"Delta CDC parent chunk length mismatch: {logical_path}", code=ErrorCode.INVALID_PAYLOAD)
                data = parent_data[offset : offset + chunk_length]
            else:
                ref = str(chunk.get("payloadRef") or "")
                source = package_root / ref
                if not source.is_file():
                    raise AppError(f"Delta CDC payload chunk is missing: {ref}", code=ErrorCode.INVALID_PAYLOAD)
                data = source.read_bytes()
            if len(data) != length:
                raise AppError(f"Delta CDC chunk length mismatch at ordinal {index}: {logical_path}", code=ErrorCode.INVALID_PAYLOAD)
            if str(chunk.get("sha256") or "") and hashlib.sha256(data).hexdigest() != str(chunk["sha256"]):
                raise AppError(f"Delta CDC chunk checksum mismatch at ordinal {index}: {logical_path}", code=ErrorCode.INVALID_PAYLOAD)
            output.write(data)


def materialize_chain(package_roots: list[Path], output_root: Path) -> dict[str, Any]:
    """Reconstruct the workspace tree for an incremental chain.

    ``package_roots`` are ordered decrypted-and-extracted package directories
    starting with the full baseline. Every transition is verified against the
    domain-separated Merkle root; a missing parent, a corrupt payload or a
    root mismatch fails closed. Returns the final package manifest.
    """
    if not package_roots:
        raise AppError("Incremental restore chain is empty", code=ErrorCode.INVALID_PAYLOAD)
    output_root.mkdir(parents=True, exist_ok=True)
    parent_files: dict[str, bytes] = {}
    current: list[backup_incremental.FileRecord] = []
    final_manifest: dict[str, Any] = {}

    for index, package_root in enumerate(package_roots):
        manifest_path = package_root / "manifest.json"
        if not manifest_path.is_file():
            raise AppError("Chain member is missing manifest.json", code=ErrorCode.INVALID_PAYLOAD)
        manifest = _read_json(manifest_path)
        final_manifest = manifest
        files = _manifest_files(manifest)
        if index == 0:
            if str(manifest.get("snapshotKind") or "full") == "incremental" or (package_root / "delta" / "operations.json").is_file():
                raise AppError("Incremental restore chain is missing its full baseline", code=ErrorCode.INVALID_PAYLOAD)
            _copy_full_tree(package_root, output_root)
            current = files
            expected_root = str(((manifest.get("snapshot") or {}).get("rootDigest")) or "")
            if expected_root and backup_incremental.snapshot_root(current) != expected_root:
                raise AppError("Full baseline Merkle root mismatch", code=ErrorCode.INVALID_PAYLOAD)
            continue
        ops = _read_delta_ops(package_root)
        if str(ops.get("parentRootDigest") or "") != backup_incremental.snapshot_root(current):
            raise AppError(f"Incremental chain parent root mismatch at {index}", code=ErrorCode.INVALID_PAYLOAD)
        for delete in ops.get("delete") or []:
            if not isinstance(delete, dict):
                raise AppError("Delta delete entry is invalid", code=ErrorCode.INVALID_PAYLOAD)
            target = output_root / str(delete["path"])
            if target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
        for put in ops.get("put") or []:
            if not isinstance(put, dict):
                raise AppError("Delta put entry is invalid", code=ErrorCode.INVALID_PAYLOAD)
            _materialize_put(package_root, output_root, put, parent_files)
        current = backup_incremental.apply_delta_ops(
            current,
            ops,
            successful_contributors={item.contributor_id for item in current},
        )
        expected_root = str(ops.get("rootDigest") or "")
        if expected_root and backup_incremental.snapshot_root(current) != expected_root:
            raise AppError(f"Incremental chain Merkle root mismatch at {index}", code=ErrorCode.INVALID_PAYLOAD)
        expected = {str(item.logical_path): item.sha256 for item in current}
        for logical_path, sha256 in expected.items():
            path = output_root / logical_path
            if not path.is_file():
                raise AppError(f"Restored tree is missing declared file: {logical_path}", code=ErrorCode.INVALID_PAYLOAD)
            if _sha256_file(path) != sha256:
                raise AppError(f"Restored file failed checksum: {logical_path}", code=ErrorCode.INVALID_PAYLOAD)
    return final_manifest

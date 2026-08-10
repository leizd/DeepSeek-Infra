"""Incremental chain materializer with immutable-parent apply (4.4.11).

Applies an ordered chain ``[F0, I1, ..., In]`` of decrypted, extracted backup
packages into a complete workspace tree, verifying the domain-separated Merkle
root at every snapshot transition. Whole-file deltas are restored by copying
payload blobs; content-defined (``fastcdc-gear-v2``) files are reconstructed
from parent ranges plus payload chunks. Any missing member or corrupt chunk
fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
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


COPY_BUFFER_BYTES = 1024 * 1024


def _chunk_ranges_for(parent_path: Path, protocol: str) -> list[tuple[int, int]]:
    with parent_path.open("rb") as parent:
        chunks = backup_incremental.chunk_stream(
            parent,
            file_size=parent_path.stat().st_size,
            protocol=protocol,
        )
    return [(int(item["offset"]), int(item["length"])) for item in chunks]


def _parent_path(output_root: Path, logical_path: str) -> Path:
    path = output_root / logical_path
    if not path.is_file():
        raise AppError(f"Delta references a missing parent file: {logical_path}", code=ErrorCode.INVALID_PAYLOAD)
    return path


def _materialize_put(package_root: Path, parent_root: Path, target: Path, put: dict[str, Any], *, chunk_protocol: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    storage = str(put.get("storage") or "whole")
    if storage == "cdc":
        _materialize_cdc(parent_root, package_root, target, put, chunk_protocol=chunk_protocol)
    elif storage == "whole":
        ref = str(put.get("payloadRef") or "")
        source = package_root / ref
        if not source.is_file():
            raise AppError(f"Delta payload blob is missing: {ref}", code=ErrorCode.INVALID_PAYLOAD)
        shutil.copyfile(source, target)
    elif storage == "parent-file":
        parent_path = _parent_path(parent_root, str(put.get("parentPath") or ""))
        shutil.copyfile(parent_path, target)
    else:
        raise AppError(f"Unsupported delta storage: {storage}", code=ErrorCode.INVALID_PAYLOAD)
    expected = str(put.get("sha256") or "")
    if expected and _sha256_file(target) != expected:
        raise AppError(f"Delta file failed checksum after restore: {put['path']}", code=ErrorCode.INVALID_PAYLOAD)


def _copy_verified_range(source: Any, output: Any, *, offset: int, length: int, expected_sha256: str, label: str) -> None:
    source.seek(offset)
    remaining = length
    digest = hashlib.sha256()
    while remaining:
        block = source.read(min(COPY_BUFFER_BYTES, remaining))
        if not block:
            raise AppError(f"Delta CDC chunk length mismatch: {label}", code=ErrorCode.INVALID_PAYLOAD)
        output.write(block)
        digest.update(block)
        remaining -= len(block)
    if expected_sha256 and digest.hexdigest() != expected_sha256:
        raise AppError(f"Delta CDC chunk checksum mismatch: {label}", code=ErrorCode.INVALID_PAYLOAD)


def _materialize_cdc(
    parent_root: Path,
    package_root: Path,
    target: Path,
    put: dict[str, Any],
    *,
    chunk_protocol: str,
) -> None:
    logical_path = str(put["path"])
    chunks = put.get("chunks")
    if not isinstance(chunks, list):
        raise AppError(f"Delta CDC file has no chunk list: {logical_path}", code=ErrorCode.INVALID_PAYLOAD)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.restore-tmp")
    try:
        with temporary.open("wb") as output:
            for index, chunk in enumerate(chunks):
                if not isinstance(chunk, dict):
                    raise AppError(f"Delta CDC file has an invalid chunk: {logical_path}", code=ErrorCode.INVALID_PAYLOAD)
                length = int(chunk.get("length") or 0)
                expected = str(chunk.get("sha256") or "")
                label = f"{logical_path}#{index}"
                source_kind = str(chunk.get("source") or "")
                if source_kind == "parent":
                    parent_path = _parent_path(parent_root, logical_path)
                    parent_ranges = _chunk_ranges_for(parent_path, chunk_protocol)
                    raw_ordinal = chunk.get("parentOrdinal")
                    if not isinstance(raw_ordinal, int) or raw_ordinal < 0 or raw_ordinal >= len(parent_ranges):
                        raise AppError(f"Delta CDC references an invalid parent chunk: {logical_path}", code=ErrorCode.INVALID_PAYLOAD)
                    offset, chunk_length = parent_ranges[raw_ordinal]
                    if chunk_length != length:
                        raise AppError(f"Delta CDC parent chunk length mismatch: {logical_path}", code=ErrorCode.INVALID_PAYLOAD)
                    with parent_path.open("rb") as parent:
                        _copy_verified_range(parent, output, offset=offset, length=length, expected_sha256=expected, label=label)
                elif source_kind == "parent-range":
                    parent_path = _parent_path(parent_root, str(chunk.get("parentPath") or ""))
                    raw_offset = chunk.get("offset")
                    if isinstance(raw_offset, bool) or not isinstance(raw_offset, int) or raw_offset < 0 or length <= 0:
                        raise AppError(f"Delta CDC references an invalid parent range: {logical_path}", code=ErrorCode.INVALID_PAYLOAD)
                    offset = raw_offset
                    if offset + length > parent_path.stat().st_size:
                        raise AppError(f"Delta CDC parent range exceeds its file: {logical_path}", code=ErrorCode.INVALID_PAYLOAD)
                    with parent_path.open("rb") as parent:
                        _copy_verified_range(parent, output, offset=offset, length=length, expected_sha256=expected, label=label)
                else:
                    ref = str(chunk.get("payloadRef") or "")
                    source = package_root / ref
                    if not source.is_file():
                        raise AppError(f"Delta CDC payload chunk is missing: {ref}", code=ErrorCode.INVALID_PAYLOAD)
                    with source.open("rb") as payload:
                        _copy_verified_range(payload, output, offset=0, length=length, expected_sha256=expected, label=label)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _chunk_protocol(manifest: dict[str, Any]) -> str:
    raw_snapshot = manifest.get("snapshot")
    snapshot: dict[str, Any] = raw_snapshot if isinstance(raw_snapshot, dict) else {}
    explicit = str(snapshot.get("chunkProtocol") or manifest.get("chunkProtocol") or "")
    if explicit:
        return explicit
    if str(snapshot.get("format") or "") == "incremental-v2":
        return backup_incremental.CDC_ALGORITHM_V2
    return backup_incremental.CDC_ALGORITHM_V2


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
        # The parent snapshot for this layer is the materialized tree after the
        # previous layer; a cross-layer parent cache would be stale.
        if str(ops.get("parentRootDigest") or "") != backup_incremental.snapshot_root(current):
            raise AppError(f"Incremental chain parent root mismatch at {index}", code=ErrorCode.INVALID_PAYLOAD)
        # Prepare every PUT against the immutable parent view before applying a
        # single tombstone. This makes delete+copy, rename and file swaps safe.
        prepared_root = output_root / f".delta-prepared-{uuid.uuid4().hex}"
        prepared_root.mkdir(parents=True, exist_ok=False)
        try:
            for put in ops.get("put") or []:
                if not isinstance(put, dict):
                    raise AppError("Delta put entry is invalid", code=ErrorCode.INVALID_PAYLOAD)
                _materialize_put(
                    package_root,
                    output_root,
                    prepared_root / str(put["path"]),
                    put,
                    chunk_protocol=_chunk_protocol(manifest),
                )
            for delete in ops.get("delete") or []:
                if not isinstance(delete, dict):
                    raise AppError("Delta delete entry is invalid", code=ErrorCode.INVALID_PAYLOAD)
                target = output_root / str(delete["path"])
                if target.is_file():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
            for put in ops.get("put") or []:
                target = output_root / str(put["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(prepared_root / str(put["path"]), target)
        finally:
            shutil.rmtree(prepared_root, ignore_errors=True)
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

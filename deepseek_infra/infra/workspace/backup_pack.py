"""Immutable snapshot-local payload packs for incremental-v5 backups."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from deepseek_infra.core.errors import AppError, ErrorCode

TARGET_PACK_SIZE = 64 * 1024 * 1024
MAX_PACK_SIZE = 72 * 1024 * 1024
PACK_ALIGNMENT = 8
WHOLE_FILE_PACK_THRESHOLD = 16 * 1024 * 1024
COPY_BUFFER_BYTES = 1024 * 1024
PACK_INDEX_PATH = "payload/packs/index.json"


def _is_sha256(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(COPY_BUFFER_BYTES):
            digest.update(block)
    return digest.hexdigest()


def parse_payload_ref(value: Any) -> tuple[str, str]:
    """Return ``(kind, locator)`` for legacy and incremental-v5 references."""
    if isinstance(value, str) and value:
        return "standalone", value
    if not isinstance(value, dict):
        raise AppError("Delta payload reference is missing or invalid", code=ErrorCode.INVALID_PAYLOAD)
    kind = str(value.get("kind") or "")
    if kind == "pack-range":
        blob_id = str(value.get("blobId") or "")
        if blob_id:
            return kind, blob_id
    if kind == "standalone":
        path = str(value.get("path") or "")
        if path:
            return kind, path
    raise AppError("Delta payload reference is missing or invalid", code=ErrorCode.INVALID_PAYLOAD)


def parse_pack_index(root: Path) -> dict[str, Any]:
    """Parse and structurally validate the pack index without reading pack bytes.

    Validates schema, pack paths, blob ranges, integer bounds and overlap. Pack
    digests are verified lazily on first use by :func:`verify_pack`.
    """
    path = root / PACK_INDEX_PATH
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError("Delta pack index is invalid", code=ErrorCode.INVALID_PAYLOAD) from exc
    if not isinstance(index, dict) or index.get("schemaVersion") != 1:
        raise AppError("Delta pack index is invalid", code=ErrorCode.INVALID_PAYLOAD)
    raw_packs = index.get("packs")
    raw_entries = index.get("entries")
    if not isinstance(raw_packs, list) or not isinstance(raw_entries, dict):
        raise AppError("Delta pack index is invalid", code=ErrorCode.INVALID_PAYLOAD)
    packs: dict[str, int] = {}
    for raw in raw_packs:
        if not isinstance(raw, dict):
            raise AppError("Delta pack index is invalid", code=ErrorCode.INVALID_PAYLOAD)
        relative = str(raw.get("path") or "")
        relative_path = PurePosixPath(relative)
        size = raw.get("size")
        digest = str(raw.get("sha256") or "")
        if (
            not relative.startswith("payload/packs/")
            or not relative.endswith(".pack")
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative in packs
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not _is_sha256(digest)
        ):
            raise AppError("Delta pack index is invalid", code=ErrorCode.INVALID_PAYLOAD)
        packs[relative] = size
    ranges: dict[str, list[tuple[int, int]]] = {relative: [] for relative in packs}
    for blob_id, raw in raw_entries.items():
        if not isinstance(blob_id, str) or not blob_id or not isinstance(raw, dict):
            raise AppError("Delta pack entry is invalid", code=ErrorCode.INVALID_PAYLOAD)
        relative = str(raw.get("pack") or "")
        offset = raw.get("offset")
        length = raw.get("length")
        digest = str(raw.get("sha256") or "")
        if (
            relative not in packs
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or offset % PACK_ALIGNMENT != 0
            or isinstance(length, bool)
            or not isinstance(length, int)
            or length < 0
            or offset + length > packs[relative]
            or not _is_sha256(digest)
        ):
            raise AppError("Delta pack entry is invalid", code=ErrorCode.INVALID_PAYLOAD)
        ranges[relative].append((offset, offset + length))
    for pack_ranges in ranges.values():
        previous_end = 0
        for start, end in sorted(pack_ranges):
            if start < previous_end:
                raise AppError("Delta pack entries overlap", code=ErrorCode.INVALID_PAYLOAD)
            previous_end = max(previous_end, end)
    return index


def verify_pack(root: Path, spec: dict[str, Any]) -> None:
    """Verify one pack's size and SHA-256 against its index declaration."""
    relative = str(spec.get("path") or "")
    size = spec.get("size")
    digest = str(spec.get("sha256") or "")
    relative_path = PurePosixPath(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise AppError(f"Delta pack path is invalid: {relative}", code=ErrorCode.INVALID_PAYLOAD)
    pack_path = root.joinpath(*relative_path.parts)
    if (
        not pack_path.is_file()
        or isinstance(size, bool)
        or not isinstance(size, int)
        or pack_path.stat().st_size != size
        or _sha256_file(pack_path) != digest
    ):
        raise AppError(f"Delta pack failed checksum: {relative}", code=ErrorCode.INVALID_PAYLOAD)


def load_pack_index(root: Path) -> dict[str, Any]:
    """Load and fully verify the immutable pack index and every pack digest."""
    index = parse_pack_index(root)
    for raw in index.get("packs") or []:
        verify_pack(root, raw)
    return index


class PackWriter:
    """Append verified streams to a small set of aligned immutable packfiles."""

    def __init__(
        self,
        root: Path,
        *,
        target_pack_size: int = TARGET_PACK_SIZE,
        max_pack_size: int = MAX_PACK_SIZE,
        alignment: int = PACK_ALIGNMENT,
    ) -> None:
        if target_pack_size <= 0 or max_pack_size < target_pack_size or alignment <= 0:
            raise ValueError("invalid pack writer limits")
        self.root = root
        self.pack_dir = root / "payload" / "packs"
        self.pack_dir.mkdir(parents=True, exist_ok=True)
        self.target_pack_size = int(target_pack_size)
        self.max_pack_size = int(max_pack_size)
        self.alignment = int(alignment)
        self._handle: BinaryIO | None = None
        self._path: Path | None = None
        self._pack_number = 0
        self._blob_number = 0
        self._packs: list[dict[str, Any]] = []
        self._entries: dict[str, dict[str, Any]] = {}
        self._created: list[Path] = []
        self._finalized: dict[str, Any] | None = None

    @staticmethod
    def _relative_pack_path(number: int) -> str:
        return f"payload/packs/{number:04d}.pack"

    def _open_pack(self) -> None:
        relative = self._relative_pack_path(self._pack_number)
        self._pack_number += 1
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("w+b")
        self._path = path
        self._created.append(path)

    def _finish_pack(self) -> None:
        handle = self._handle
        path = self._path
        if handle is None or path is None:
            return
        handle.flush()
        os.fsync(handle.fileno())
        size = handle.tell()
        handle.close()
        self._packs.append(
            {
                "path": path.relative_to(self.root).as_posix(),
                "size": size,
                "sha256": _sha256_file(path),
            }
        )
        self._handle = None
        self._path = None

    def _aligned_offset(self) -> int:
        if self._handle is None:
            return 0
        current = self._handle.tell()
        return ((current + self.alignment - 1) // self.alignment) * self.alignment

    def append(self, source: BinaryIO, *, expected_length: int, expected_sha256: str) -> dict[str, str]:
        if self._finalized is not None:
            raise AppError("Payload pack writer is already finalized", code=ErrorCode.INTERNAL, status=500)
        length = int(expected_length)
        if length < 0 or length > self.max_pack_size:
            raise AppError("Payload blob exceeds the maximum pack size", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
        if self._handle is None:
            self._open_pack()
        assert self._handle is not None and self._path is not None
        offset = self._aligned_offset()
        if self._handle.tell() > 0 and (offset + length > self.target_pack_size or offset + length > self.max_pack_size):
            self._finish_pack()
            self._open_pack()
            assert self._handle is not None and self._path is not None
            offset = 0
        if offset + length > self.max_pack_size:
            raise AppError("Payload blob exceeds the maximum pack size", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)

        rollback_offset = self._handle.tell()
        if offset > rollback_offset:
            self._handle.write(b"\0" * (offset - rollback_offset))
        digest = hashlib.sha256()
        remaining = length
        try:
            while remaining:
                block = source.read(min(COPY_BUFFER_BYTES, remaining))
                if not block:
                    raise AppError("Payload blob length mismatch", code=ErrorCode.INVALID_PAYLOAD)
                if len(block) > remaining:
                    block = block[:remaining]
                self._handle.write(block)
                digest.update(block)
                remaining -= len(block)
            actual = digest.hexdigest()
            if actual != expected_sha256:
                raise AppError("Payload blob checksum mismatch", code=ErrorCode.INVALID_PAYLOAD)
        except Exception:
            self._handle.seek(rollback_offset)
            self._handle.truncate()
            raise

        blob_id = f"blob_{self._blob_number:06d}"
        self._blob_number += 1
        self._entries[blob_id] = {
            "pack": self._path.relative_to(self.root).as_posix(),
            "offset": offset,
            "length": length,
            "sha256": expected_sha256,
        }
        return {"kind": "pack-range", "blobId": blob_id}

    def finalize(self) -> dict[str, Any]:
        if self._finalized is not None:
            return self._finalized
        self._finish_pack()
        index: dict[str, Any] = {
            "schemaVersion": 1,
            "packs": self._packs,
            "entries": self._entries,
        }
        path = self.root / PACK_INDEX_PATH
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
        self._created.append(path)
        self._finalized = index
        return index

    def delta_files(self) -> list[dict[str, Any]]:
        self.finalize()
        index_path = self.root / PACK_INDEX_PATH
        return [
            *self._packs,
            {
                "path": PACK_INDEX_PATH,
                "size": index_path.stat().st_size,
                "sha256": _sha256_file(index_path),
            },
        ]

    def abort(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
            self._path = None
        for path in reversed(self._created):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

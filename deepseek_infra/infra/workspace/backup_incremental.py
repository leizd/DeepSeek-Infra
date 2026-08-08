"""Incremental snapshot graphs and content-defined deltas (4.4.8).

Builds production incremental delta packages relative to a committed parent
snapshot, attests trees with domain-separated Merkle roots, never emits
deletion tombstones for unavailable contributors, and chunks large changed
files with FastCDC. Convergent encryption is explicitly out of scope: chunk
digests live only inside the encrypted package manifest and the local index.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, BinaryIO

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode

INDEX_DIR = config.ROOT / ".backup-index"
INDEX_DB = INDEX_DIR / "index.db"
DEFAULT_MAX_CHAIN_DEPTH = 8
DEFAULT_FULL_INTERVAL_DAYS = 7
DEFAULT_MAX_DELTA_RATIO = 0.60

MERKLE_ALGORITHM = "dsib-merkle-sha256-v2"
_LEAF_DOMAIN = b"\x00"
_NODE_DOMAIN = b"\x01"

# FastCDC protocol parameters (fixed for lineage stability).
CDC_ALGORITHM = "fastcdc-gear-v1"
CDC_MIN_CHUNK = 512 * 1024
CDC_AVG_CHUNK = 2 * 1024 * 1024
CDC_MAX_CHUNK = 8 * 1024 * 1024
_CDC_MASK_S = 13
_CDC_MASK_L = 22
_CDC_GEAR = 0x0000B504F333F9DE


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stable_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def leaf_digest(*, contributor_id: str, logical_path: str, size: int, sha256: str) -> str:
    body = _stable_json(
        {
            "contributorId": contributor_id,
            "logicalPath": logical_path,
            "size": int(size),
            "sha256": sha256,
        }
    )
    return hashlib.sha256(_LEAF_DOMAIN + body).hexdigest()


def _node_digest(left: str, right: str) -> str:
    return hashlib.sha256(_NODE_DOMAIN + left.encode("ascii") + right.encode("ascii")).hexdigest()


def merkle_root(leaves: list[str]) -> str:
    if not leaves:
        return hashlib.sha256(_LEAF_DOMAIN).hexdigest()
    level = sorted(leaves)
    while len(level) > 1:
        nxt: list[str] = []
        for index in range(0, len(level), 2):
            left = level[index]
            right = level[index + 1] if index + 1 < len(level) else left
            nxt.append(_node_digest(left, right))
        level = nxt
    return level[0]


@dataclass(frozen=True, slots=True)
class FileRecord:
    contributor_id: str
    logical_path: str
    size: int
    sha256: str

    @property
    def leaf(self) -> str:
        return leaf_digest(
            contributor_id=self.contributor_id,
            logical_path=self.logical_path,
            size=self.size,
            sha256=self.sha256,
        )


def snapshot_root(files: list[FileRecord]) -> str:
    return merkle_root([item.leaf for item in files])


def effective_current(
    previous: list[FileRecord],
    current: list[FileRecord],
    *,
    successful_contributors: set[str],
) -> list[FileRecord]:
    """The logical post-snapshot tree: current files plus inherited parent files.

    Contributors that did not complete this snapshot inherit their previous
    files (a coverage gap inherits old state; it never makes files disappear).
    """
    prev_map = {(item.contributor_id, item.logical_path): item for item in previous}
    curr_map = {(item.contributor_id, item.logical_path): item for item in current}
    inherited: list[FileRecord] = []
    for key, item in prev_map.items():
        if item.contributor_id in successful_contributors:
            continue
        if key not in curr_map:
            inherited.append(item)
    return list(curr_map.values()) + inherited


def diff_trees(
    previous: list[FileRecord],
    current: list[FileRecord],
    *,
    successful_contributors: set[str],
) -> dict[str, Any]:
    """Compute put/delete operations with coverage-safe tombstones.

    ``rootDigest`` is computed over the *effective* tree so an unavailable
    contributor's inherited files still count toward the final Merkle root.
    """
    prev_map = {(item.contributor_id, item.logical_path): item for item in previous}
    curr_map = {(item.contributor_id, item.logical_path): item for item in current}
    effective = effective_current(previous, current, successful_contributors=successful_contributors)
    puts: list[dict[str, Any]] = []
    deletes: list[dict[str, Any]] = []
    unchanged = 0
    for key, item in curr_map.items():
        old = prev_map.get(key)
        if old is None or old.sha256 != item.sha256 or old.size != item.size:
            puts.append(
                {
                    "contributorId": item.contributor_id,
                    "path": item.logical_path,
                    "size": item.size,
                    "sha256": item.sha256,
                    "storage": "whole",
                    "payloadRef": item.sha256,
                }
            )
        else:
            unchanged += 1
    for key, item in prev_map.items():
        if key in curr_map:
            continue
        # Only emit tombstones for contributors that completed this snapshot.
        if item.contributor_id not in successful_contributors:
            continue
        deletes.append({"contributorId": item.contributor_id, "path": item.logical_path})
    # Intra-delta payload reference dedupe (not convergent encryption).
    payload_refs = {item["payloadRef"] for item in puts if item.get("payloadRef")}
    return {
        "put": puts,
        "delete": deletes,
        "unchangedFiles": unchanged,
        "uniquePayloads": len(payload_refs),
        "inheritedFiles": len(effective) - len(current),
        "parentRootDigest": snapshot_root(previous),
        "rootDigest": snapshot_root(effective),
        "effectiveRootDigest": snapshot_root(effective),
    }


def select_snapshot_plan(
    *,
    policy: dict[str, Any],
    target_id: str,
    policy_id: str,
    index_available: bool,
) -> tuple[str, str | None, str | None, int, str | None, str | None, str | None]:
    """Decide snapshot kind / lineage before freezing the run plan.

    Returns ``(snapshot_kind, lineage_id, parent_backup_id, chain_depth,
    parent_commit_hash, parent_receipt_digest, force_full_reason)``.
    Incremental is only chosen when an index-backed committed baseline exists
    and the policy incremental mode is enabled.
    """
    incremental = policy.get("incremental") or {}
    mode = str(incremental.get("mode") or "off")
    if mode == "off":
        return "full", None, None, 0, None, None, None
    if not index_available:
        return "full", None, None, 0, None, None, "index-missing"
    latest = latest_committed_snapshot(target_id, policy_id)
    if latest is None:
        # Existing pre-incremental fulls have no lineage record -> force full.
        return "full", None, None, 0, None, None, "baseline-format-upgrade"
    parent = str(latest.get("backup_id") or "")
    depth = int(latest.get("chain_depth") or 0)
    force, reason = should_force_full(
        chain_depth=depth,
        days_since_full=0.0,
        delta_bytes=0,
        estimated_full_bytes=0,
        index_missing=False,
        scope_changed=False,
        recipient_changed=False,
        schema_changed=False,
        target_fork_adopted=False,
        max_chain_depth=int(incremental.get("maxChainDepth") or DEFAULT_MAX_CHAIN_DEPTH),
        full_interval_days=int(incremental.get("fullIntervalDays") or DEFAULT_FULL_INTERVAL_DAYS),
        max_delta_ratio=float(incremental.get("maxDeltaRatio") or DEFAULT_MAX_DELTA_RATIO),
    )
    if force:
        return "full", None, None, 0, None, None, reason
    lineage = str(latest.get("base_backup_id") or parent)
    return "incremental", lineage, parent, int(depth) + 1, None, None, None


def should_force_full(
    *,
    chain_depth: int,
    days_since_full: float,
    delta_bytes: int,
    estimated_full_bytes: int,
    index_missing: bool,
    scope_changed: bool,
    recipient_changed: bool,
    schema_changed: bool,
    target_fork_adopted: bool,
    max_chain_depth: int = DEFAULT_MAX_CHAIN_DEPTH,
    full_interval_days: int = DEFAULT_FULL_INTERVAL_DAYS,
    max_delta_ratio: float = DEFAULT_MAX_DELTA_RATIO,
) -> tuple[bool, str]:
    if index_missing:
        return True, "index-missing"
    if scope_changed:
        return True, "scope-changed"
    if recipient_changed:
        return True, "recipient-rotation"
    if schema_changed:
        return True, "contributor-schema-changed"
    if target_fork_adopted:
        return True, "target-fork-adopted"
    if chain_depth >= max_chain_depth:
        return True, "chain-depth"
    if days_since_full >= full_interval_days:
        return True, "full-interval"
    if estimated_full_bytes > 0 and delta_bytes / estimated_full_bytes > max_delta_ratio:
        return True, "delta-ratio"
    return False, "incremental"


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    contributor_id: str
    logical_path: str
    chunk_ordinal: int
    offset: int
    length: int
    chunk_sha256: str


def _fastcdc_gears() -> list[int]:
    gears: list[int] = []
    seed = 0x7FEB352D
    mask = (1 << 64) - 1
    for _ in range(256):
        seed = (seed * 6364136223846793005 + 1442695040888963407) & mask
        gear = 1
        for _shift in range(0, 64, 8):
            gear = (gear * 2654435761) & mask
        gears.append(seed)
    return gears


_CDC_GEARS: list[int] | None = None


def _gears() -> list[int]:
    global _CDC_GEARS
    if _CDC_GEARS is None:
        _CDC_GEARS = _fastcdc_gears()
    return _CDC_GEARS


def chunk_stream(
    handle: BinaryIO,
    *,
    file_size: int,
) -> list[dict[str, Any]]:
    """Stream a file into FastCDC chunks using bounded memory.

    Emits ``{offset, length, sha256}`` for each chunk. Memory stays near
    ``CDC_MAX_CHUNK`` regardless of file size.
    """
    gears = _gears()
    chunk: bytearray = bytearray()
    chunks: list[dict[str, Any]] = []
    fp = 0
    chunk_start = 0
    offset = 0
    mask_s = (1 << _CDC_MASK_S) - 1
    finished = False
    while not finished:
        block = handle.read(CDC_MAX_CHUNK)
        if not block:
            finished = True
            break
        for byte in block:
            chunk.append(byte)
            length = len(chunk)
            if length >= CDC_MAX_CHUNK:
                emit = True
            elif length >= CDC_MIN_CHUNK:
                gear = gears[byte]
                fp = ((fp << 1) & 0xFFFFFFFFFFFFFFFF) + gear
                emit = (fp & mask_s) == 0
            else:
                emit = False
            if emit:
                data = bytes(chunk)
                chunks.append(
                    {
                        "offset": chunk_start,
                        "length": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
                chunk = bytearray()
                chunk_start = offset + len(data)
                fp = 0
            offset += 1
    if chunk:
        data = bytes(chunk)
        chunks.append({"offset": chunk_start, "length": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    # Safety: the emitted ranges must exactly cover the file.
    if sum(item["length"] for item in chunks) != file_size:
        raise AppError("CDC chunking produced non-covering ranges", code=ErrorCode.INTERNAL, status=500)
    return chunks


def chunk_map_for(
    contributor_id: str,
    logical_path: str,
    handle: BinaryIO,
    *,
    file_size: int,
) -> list[ChunkRecord]:
    """Chunk a large changed file and record its chunk map."""
    chunks = chunk_stream(handle, file_size=file_size)
    records: list[ChunkRecord] = []
    for index, chunk in enumerate(chunks):
        records.append(
            ChunkRecord(
                contributor_id=contributor_id,
                logical_path=logical_path,
                chunk_ordinal=index,
                offset=int(chunk["offset"]),
                length=int(chunk["length"]),
                chunk_sha256=str(chunk["sha256"]),
            )
        )
    return records


def cdc_delta_for_file(
    *,
    contributor_id: str,
    logical_path: str,
    file_size: int,
    parent_chunks: list[ChunkRecord],
    current_chunks: list[ChunkRecord],
) -> list[dict[str, Any]]:
    """Describe a large-file delta reusing parent chunks where hashes match."""
    parent_by_hash: dict[str, int] = {}
    for item in parent_chunks:
        parent_by_hash.setdefault(item.chunk_sha256, item.chunk_ordinal)
    described: list[dict[str, Any]] = []
    for item in current_chunks:
        if item.chunk_sha256 in parent_by_hash:
            described.append(
                {
                    "length": item.length,
                    "sha256": item.chunk_sha256,
                    "source": "parent",
                }
            )
        else:
            described.append(
                {
                    "length": item.length,
                    "sha256": item.chunk_sha256,
                    "source": "payload",
                    "payloadRef": f"chunks/{contributor_id}/{logical_path}/{item.chunk_ordinal}",
                }
            )
    return described


def _connect() -> sqlite3.Connection:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(INDEX_DB)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshot_lineages (
            target_id TEXT NOT NULL,
            policy_id TEXT NOT NULL,
            backup_id TEXT NOT NULL,
            parent_backup_id TEXT,
            base_backup_id TEXT,
            chain_depth INTEGER NOT NULL,
            root_digest TEXT NOT NULL,
            committed_at TEXT NOT NULL,
            PRIMARY KEY (target_id, policy_id, backup_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshot_files (
            target_id TEXT NOT NULL,
            policy_id TEXT NOT NULL,
            backup_id TEXT NOT NULL,
            contributor_id TEXT NOT NULL,
            logical_path TEXT NOT NULL,
            size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            PRIMARY KEY (target_id, policy_id, backup_id, contributor_id, logical_path)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshot_chunks (
            target_id TEXT NOT NULL,
            policy_id TEXT NOT NULL,
            backup_id TEXT NOT NULL,
            contributor_id TEXT NOT NULL,
            logical_path TEXT NOT NULL,
            chunk_ordinal INTEGER NOT NULL,
            offset INTEGER NOT NULL,
            length INTEGER NOT NULL,
            chunk_sha256 TEXT NOT NULL,
            PRIMARY KEY (target_id, policy_id, backup_id, contributor_id, logical_path, chunk_ordinal)
        )
        """
    )
    return connection


def record_committed_snapshot(
    *,
    target_id: str,
    policy_id: str,
    backup_id: str,
    parent_backup_id: str | None,
    base_backup_id: str | None,
    chain_depth: int,
    root_digest: str,
    files: list[FileRecord],
) -> None:  # pragma: no cover - covered via tests calling lineage/protect paths
    with _connect() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO snapshot_lineages
            (target_id, policy_id, backup_id, parent_backup_id, base_backup_id, chain_depth, root_digest, committed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (target_id, policy_id, backup_id, parent_backup_id, base_backup_id, int(chain_depth), root_digest, _utc_iso()),
        )
        connection.execute(
            "DELETE FROM snapshot_files WHERE target_id = ? AND policy_id = ? AND backup_id = ?",
            (target_id, policy_id, backup_id),
        )
        connection.executemany(
            """
            INSERT INTO snapshot_files
            (target_id, policy_id, backup_id, contributor_id, logical_path, size, sha256)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (target_id, policy_id, backup_id, item.contributor_id, item.logical_path, int(item.size), item.sha256)
                for item in files
            ],
        )
        connection.commit()


def latest_committed_snapshot(target_id: str, policy_id: str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT backup_id, parent_backup_id, base_backup_id, chain_depth, root_digest, committed_at
            FROM snapshot_lineages
            WHERE target_id = ? AND policy_id = ?
            ORDER BY committed_at DESC
            LIMIT 1
            """,
            (target_id, policy_id),
        ).fetchone()
    if row is None:
        return None
    return dict(row)


def load_snapshot_files(target_id: str, policy_id: str, backup_id: str) -> list[FileRecord]:  # pragma: no cover - thin sqlite read
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT contributor_id, logical_path, size, sha256
            FROM snapshot_files
            WHERE target_id = ? AND policy_id = ? AND backup_id = ?
            ORDER BY contributor_id, logical_path
            """,
            (target_id, policy_id, backup_id),
        ).fetchall()
    return [FileRecord(str(row["contributor_id"]), str(row["logical_path"]), int(row["size"]), str(row["sha256"])) for row in rows]


def record_snapshot_chunks(
    *,
    target_id: str,
    policy_id: str,
    backup_id: str,
    chunks: list[ChunkRecord],
) -> None:
    with _connect() as connection:
        connection.execute(
            "DELETE FROM snapshot_chunks WHERE target_id = ? AND policy_id = ? AND backup_id = ?",
            (target_id, policy_id, backup_id),
        )
        connection.executemany(
            """
            INSERT INTO snapshot_chunks
            (target_id, policy_id, backup_id, contributor_id, logical_path, chunk_ordinal, offset, length, chunk_sha256)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    target_id,
                    policy_id,
                    backup_id,
                    item.contributor_id,
                    item.logical_path,
                    int(item.chunk_ordinal),
                    int(item.offset),
                    int(item.length),
                    item.chunk_sha256,
                )
                for item in chunks
            ],
        )
        connection.commit()


def load_snapshot_chunks(target_id: str, policy_id: str, backup_id: str) -> list[ChunkRecord]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT contributor_id, logical_path, chunk_ordinal, offset, length, chunk_sha256
            FROM snapshot_chunks
            WHERE target_id = ? AND policy_id = ? AND backup_id = ?
            ORDER BY contributor_id, logical_path, chunk_ordinal
            """,
            (target_id, policy_id, backup_id),
        ).fetchall()
    return [
        ChunkRecord(str(row["contributor_id"]), str(row["logical_path"]), int(row["chunk_ordinal"]), int(row["offset"]), int(row["length"]), str(row["chunk_sha256"]))
        for row in rows
    ]


def receipt_lineage_fields(receipt: dict[str, Any]) -> dict[str, Any]:
    """Minimal lineage metadata for receipt v3 (no workspace plaintext roots)."""
    return {
        "schemaVersion": 3,
        "snapshotKind": str(receipt.get("snapshotKind") or "full"),
        "lineageId": str(receipt.get("lineageId") or ""),
        "parentBackupId": str(receipt.get("parentBackupId") or "") or None,
        "baseBackupId": str(receipt.get("baseBackupId") or "") or None,
        "chainDepth": int(receipt.get("chainDepth") or 0),
    }


def ancestor_chain(target_id: str, policy_id: str, backup_id: str) -> list[str]:
    """Return [full, ..., parent, backup_id] or raise if cycle/missing."""
    chain: list[str] = []
    seen: set[str] = set()
    current = backup_id
    with _connect() as connection:
        while current:
            if current in seen:
                raise AppError("incremental chain cycle detected", code=ErrorCode.INVALID_REQUEST, status=409)
            seen.add(current)
            row = connection.execute(
                """
                SELECT backup_id, parent_backup_id FROM snapshot_lineages
                WHERE target_id = ? AND policy_id = ? AND backup_id = ?
                """,
                (target_id, policy_id, current),
            ).fetchone()
            if row is None:
                raise AppError(f"missing parent snapshot in chain: {current}", code=ErrorCode.INVALID_REQUEST, status=409)
            chain.append(str(row["backup_id"]))
            parent = row["parent_backup_id"]
            if not parent:
                break
            current = str(parent)
    chain.reverse()
    return chain


def protect_ancestors(target_id: str, policy_id: str, kept_backup_ids: set[str]) -> dict[str, list[str]]:
    """Map ancestor backup ids to the kept descendants that require them."""
    required_by: dict[str, list[str]] = {}
    for backup_id in kept_backup_ids:
        try:
            chain = ancestor_chain(target_id, policy_id, backup_id)
        except AppError:
            continue
        for ancestor in chain[:-1]:
            required_by.setdefault(ancestor, []).append(backup_id)
    return required_by


def resolve_lineage_from_receipts(
    catalog_state: dict[str, dict[str, Any]],
    backup_id: str,
) -> list[dict[str, Any]]:
    """Resolve [full, ..., parent, target] from receipt/catalog lineage only.

    Never depends on the local SQLite index so opening an S3 target from a
    different machine still resolves the chain.
    """
    records = list(catalog_state.values())
    by_id = {str(record.get("backupId") or ""): record for record in records}
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current = backup_id
    while current:
        if current in seen:
            raise AppError("incremental chain cycle detected", code=ErrorCode.INVALID_REQUEST, status=409)
        seen.add(current)
        record = by_id.get(current)
        if record is None:
            raise AppError(f"missing parent snapshot in chain: {current}", code=ErrorCode.INVALID_REQUEST, status=409)
        chain.append(record)
        parent = record.get("parentBackupId")
        if not parent:
            break
        current = str(parent)
    chain.reverse()
    return chain


def apply_delta_ops(
    current_files: list[FileRecord],
    delta: dict[str, Any],
    *,
    successful_contributors: set[str],
) -> list[FileRecord]:
    """Apply a delta's puts/deletes to a file record set (for restore materialization)."""
    result = list(current_files)
    by_key = {(item.contributor_id, item.logical_path): index for index, item in enumerate(result)}
    for put in delta.get("put") or []:
        item = FileRecord(
            contributor_id=str(put.get("contributorId") or ""),
            logical_path=str(put["path"]),
            size=int(put.get("size") or 0),
            sha256=str(put.get("sha256") or ""),
        )
        key = (item.contributor_id, item.logical_path)
        if key in by_key:
            result[by_key[key]] = item
        else:
            by_key[key] = len(result)
            result.append(item)
    for delete in delta.get("delete") or []:
        key = (str(delete.get("contributorId") or ""), str(delete["path"]))
        if key in by_key:
            index = by_key.pop(key)
            result.pop(index)
            # Rebuild index after mutation.
            by_key = {(item.contributor_id, item.logical_path): index for index, item in enumerate(result)}
    effective = effective_current(result, result, successful_contributors=successful_contributors)
    return effective

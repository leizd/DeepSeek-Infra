"""Incremental snapshot graphs and effective dedup indexes (4.4.11).

Builds production incremental delta packages relative to a committed parent
snapshot, attests trees with domain-separated Merkle roots, never emits
deletion tombstones for unavailable contributors, and chunks large changed
files with the pinned ``fastcdc-gear-v2`` protocol. Convergent encryption is
explicitly out of scope: chunk digests live only inside the encrypted package
manifest and the local index.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode

INDEX_DIR = config.ROOT / ".backup-index"
INDEX_DB = INDEX_DIR / "index.db"
BLOOM_BITS_PER_ITEM = 10
BLOOM_HASH_FUNCTIONS = 7
BLOOM_MAGIC = b"DSIBBF1\n"
INDEX_SCHEMA_KEY = "chunk-map-schema"
INDEX_SCHEMA_VERSION = "2"
DEFAULT_MAX_CHAIN_DEPTH = 8
DEFAULT_FULL_INTERVAL_DAYS = 7
DEFAULT_MAX_DELTA_RATIO = 0.60

MERKLE_ALGORITHM = "dsib-merkle-sha256-v2"
_LEAF_DOMAIN = b"\x00"
_NODE_DOMAIN = b"\x01"

# FastCDC protocol parameters (fixed for lineage stability). 4.4.10 introduced v3;
# 4.4.11 keeps v3 while upgrading only the encrypted delta reference format.
# v2 remains a first-class decoder because a committed 4.4.9 lineage may be
# restored indefinitely. The v3 normalization deliberately makes boundaries
# harder before the 2 MiB target and easier afterwards.
CDC_ALGORITHM = "fastcdc-gear-v2"  # public 4.4.9 compatibility alias
CDC_ALGORITHM_V2 = CDC_ALGORITHM
CDC_ALGORITHM_V3 = "fastcdc-gear-v3"
CURRENT_CDC_PROTOCOL = CDC_ALGORITHM_V3
CDC_ALGORITHM_V1 = "fastcdc-gear-v1"
SUPPORTED_CDC_PROTOCOLS = (CDC_ALGORITHM_V2, CDC_ALGORITHM_V3)
CDC_MIN_CHUNK = 512 * 1024
CDC_AVG_CHUNK = 2 * 1024 * 1024
CDC_MAX_CHUNK = 8 * 1024 * 1024
_CDC_V2_MASK_EARLY = 13
_CDC_V2_MASK_LATE = 22
_CDC_V3_MASK_EARLY = 22
_CDC_V3_MASK_LATE = 20


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
    contributor_schemas: dict[str, int] | None = None,
) -> tuple[str, str | None, str | None, int, str | None, str | None, str | None]:
    """Decide snapshot kind / lineage before freezing the run plan.

    Returns ``(snapshot_kind, lineage_id, parent_backup_id, chain_depth,
    parent_commit_hash, parent_receipt_digest, force_full_reason)``.
    Incremental is only chosen when an index-backed committed baseline exists
    and the policy incremental mode is enabled. Force-full conditions are
    evaluated from committed snapshot metadata (scope / recipient / schema
    digests, full-interval age, chain depth), not from the raw policy alone.
    """
    incremental = policy.get("incremental") or {}
    mode = str(incremental.get("mode") or "off")
    if mode == "off":
        return "full", None, None, 0, None, None, None
    if not index_available:
        return "full", None, None, 0, None, None, "index-missing"
    if not index_is_healthy(target_id, policy_id):
        return "full", None, None, 0, None, None, "chunk-index-rebuild-failed"
    latest = latest_committed_snapshot(target_id, policy_id)
    if latest is None:
        # Existing pre-incremental fulls have no lineage record -> force full.
        return "full", None, None, 0, None, None, "baseline-format-upgrade"
    parent = str(latest.get("backup_id") or "")
    depth = int(latest.get("chain_depth") or 0)
    committed_scope = str(latest.get("scope_digest") or "")
    committed_recipients = str(latest.get("recipient_set_digest") or "")
    committed_schema = str(latest.get("schema_digest") or "")
    # Rebuildable pre-4.4.9 test/index rows did not carry a protocol at all;
    # only an explicitly committed v2 lineage proves that an upgrade full is
    # required. Real 4.4.9 executor rows always persisted v2.
    committed_chunk_protocol = str(latest.get("chunk_protocol") or CURRENT_CDC_PROTOCOL)
    if committed_chunk_protocol != CURRENT_CDC_PROTOCOL:
        return "full", None, None, 0, None, None, "chunk-protocol-upgrade"
    now = datetime.now(tz=timezone.utc)
    full_reference = str(latest.get("full_committed_at") or latest.get("committed_at") or "")
    days_since_full = 0.0
    try:
        full_time = datetime.fromisoformat(full_reference.replace("Z", "+00:00"))
        days_since_full = max(0.0, (now - full_time).total_seconds() / 86400.0)
    except ValueError:
        days_since_full = 0.0
    force, reason = should_force_full(
        chain_depth=depth,
        days_since_full=days_since_full,
        delta_bytes=0,
        estimated_full_bytes=int(latest.get("logical_bytes") or 0),
        index_missing=False,
        scope_changed=bool(committed_scope) and committed_scope != scope_digest(policy),
        recipient_changed=bool(committed_recipients) and committed_recipients != recipient_set_digest(policy),
        schema_changed=bool(committed_schema) and committed_schema != schema_digest(contributor_schemas or {}),
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


@dataclass(frozen=True, slots=True)
class ParentChunkLocation:
    contributor_id: str
    logical_path: str
    chunk_ordinal: int
    offset: int
    length: int
    chunk_sha256: str


def chunk_map_id(*, protocol: str, file_size: int, file_sha256: str) -> str:
    """Content-address an immutable local chunk map without exposing it remotely."""
    return hashlib.sha256(f"{protocol}\0{int(file_size)}\0{file_sha256}".encode("ascii")).hexdigest()


def _fastcdc_gears() -> list[int]:
    """Deterministic splitmix64 gear table for fastcdc-gear-v2.

    The table is a fixed pseudorandom sequence: identical byte streams always
    produce identical chunk boundaries, so the protocol stays stable across
    processes, machines and releases.
    """
    gears: list[int] = []
    seed = 0x9E3779B97F4A7C15
    mask = (1 << 64) - 1
    for _ in range(256):
        seed = (seed + 0x9E3779B97F4A7C15) & mask
        z = seed
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & mask
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & mask
        z = z ^ (z >> 31)
        gears.append(z)
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
    protocol: str = CDC_ALGORITHM,
) -> list[dict[str, Any]]:
    """Stream a file into versioned FastCDC chunks using bounded memory.

    Emits ``{offset, length, sha256}`` for each chunk. Boundaries use the short
    mask up to the average size and the long mask beyond it, so the protocol is
    pinned for lineage stability. Memory stays near ``CDC_MAX_CHUNK``.
    """
    if protocol not in SUPPORTED_CDC_PROTOCOLS:
        raise AppError(f"Unsupported CDC chunk protocol: {protocol}", code=ErrorCode.INVALID_PAYLOAD)
    gears = _gears()
    chunk: bytearray = bytearray()
    chunks: list[dict[str, Any]] = []
    fp = 0
    chunk_start = 0
    offset = 0
    if protocol == CDC_ALGORITHM_V2 or file_size <= 16 * 1024 * 1024:
        early_bits, late_bits = _CDC_V2_MASK_EARLY, _CDC_V2_MASK_LATE
    else:
        early_bits, late_bits = _CDC_V3_MASK_EARLY, _CDC_V3_MASK_LATE
    early_mask = (1 << early_bits) - 1
    late_mask = (1 << late_bits) - 1
    mask_64 = (1 << 64) - 1
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
                fp = (((fp << 1) & mask_64) + gears[byte]) & mask_64
                boundary_mask = late_mask if length >= CDC_AVG_CHUNK else early_mask
                emit = (fp & boundary_mask) == 0
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
                chunk_start = offset + 1
            offset += 1
    if chunk:
        data = bytes(chunk)
        if protocol == CDC_ALGORITHM_V3 and not chunks and len(data) >= CDC_AVG_CHUNK:
            midpoint = len(data) // 2
            for relative, part in ((0, data[:midpoint]), (midpoint, data[midpoint:])):
                chunks.append({"offset": chunk_start + relative, "length": len(part), "sha256": hashlib.sha256(part).hexdigest()})
        else:
            chunks.append({"offset": chunk_start, "length": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    # Safety: the emitted ranges must exactly and contiguously cover the file.
    if sum(item["length"] for item in chunks) != file_size:
        raise AppError("CDC chunking produced non-covering ranges", code=ErrorCode.INTERNAL, status=500)
    expected = 0
    for item in chunks:
        if int(item["offset"]) != expected:
            raise AppError("CDC chunking produced non-contiguous ranges", code=ErrorCode.INTERNAL, status=500)
        expected += int(item["length"])
    if expected != file_size:
        raise AppError("CDC chunking produced non-covering ranges", code=ErrorCode.INTERNAL, status=500)
    return chunks


def chunk_map_for(
    contributor_id: str,
    logical_path: str,
    handle: BinaryIO,
    *,
    file_size: int,
    protocol: str = CDC_ALGORITHM,
) -> list[ChunkRecord]:
    """Chunk a large changed file and record its chunk map."""
    chunks = chunk_stream(handle, file_size=file_size, protocol=protocol)
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
    parent_locations: dict[tuple[str, int], ParentChunkLocation] | None = None,
) -> list[dict[str, Any]]:
    """Describe a large-file delta reusing parent chunks where hashes match."""
    parent_by_hash: dict[tuple[str, int], int] = {}
    for item in parent_chunks:
        parent_by_hash.setdefault((item.chunk_sha256, item.length), item.chunk_ordinal)
    described: list[dict[str, Any]] = []
    for item in current_chunks:
        location = (parent_locations or {}).get((item.chunk_sha256, item.length))
        if location is not None:
            described.append(
                {
                    "length": item.length,
                    "sha256": item.chunk_sha256,
                    "source": "parent-range",
                    "parentContributorId": location.contributor_id,
                    "parentPath": location.logical_path,
                    "offset": location.offset,
                }
            )
            continue
        parent_ordinal = parent_by_hash.get((item.chunk_sha256, item.length))
        if parent_ordinal is not None:
            described.append(
                {
                    "length": item.length,
                    "sha256": item.chunk_sha256,
                    "source": "parent",
                    "parentOrdinal": parent_ordinal,
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


_LINEAGE_EXTRA_COLUMNS = (
    ("scope_digest", "TEXT"),
    ("recipient_set_digest", "TEXT"),
    ("schema_digest", "TEXT"),
    ("chunk_protocol", "TEXT"),
    ("full_committed_at", "TEXT"),
    ("logical_bytes", "INTEGER"),
)


def _migrate_lineage_columns(connection: sqlite3.Connection) -> None:
    existing = {str(row[1]) for row in connection.execute("PRAGMA table_info(snapshot_lineages)")}
    for name, column_type in _LINEAGE_EXTRA_COLUMNS:
        if name not in existing:
            connection.execute(f"ALTER TABLE snapshot_lineages ADD COLUMN {name} {column_type}")


def scope_digest(policy: dict[str, Any]) -> str:
    """Deterministic digest of the backup scope relevant to force-full checks."""
    raw_scope = policy.get("scope")
    scope = raw_scope if isinstance(raw_scope, dict) else {}
    body = _stable_json(
        {
            "mode": str(scope.get("mode") or "full"),
            "projectIds": sorted(str(item) for item in (scope.get("projectIds") or [])),
            "includeHistory": bool(scope.get("includeHistory", True)),
            "includeDrafts": bool(scope.get("includeDrafts", False)),
            "includeExternalState": bool(scope.get("includeExternalState", True)),
            "coveragePolicy": str(scope.get("coveragePolicy") or "strict"),
        }
    )
    return hashlib.sha256(body).hexdigest()


def recipient_set_digest(policy: dict[str, Any]) -> str:
    raw_protection = policy.get("protection")
    protection = raw_protection if isinstance(raw_protection, dict) else {}
    recipients = sorted(str(item) for item in (protection.get("recipients") or []))
    return hashlib.sha256(_stable_json(recipients)).hexdigest()


def schema_digest(contributor_schemas: dict[str, int]) -> str:
    body = _stable_json({str(key): int(value) for key, value in sorted(contributor_schemas.items())})
    return hashlib.sha256(body).hexdigest()


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
    _migrate_lineage_columns(connection)
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
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_snapshot_chunks_file
        ON snapshot_chunks (target_id, policy_id, backup_id, contributor_id, logical_path, chunk_ordinal)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chunk_maps (
            chunk_map_id TEXT PRIMARY KEY,
            protocol TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            file_sha256 TEXT NOT NULL,
            chunk_count INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chunk_map_chunks (
            chunk_map_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            offset INTEGER NOT NULL,
            length INTEGER NOT NULL,
            chunk_sha256 TEXT NOT NULL,
            PRIMARY KEY (chunk_map_id, ordinal),
            FOREIGN KEY (chunk_map_id) REFERENCES chunk_maps(chunk_map_id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshot_chunk_refs (
            target_id TEXT NOT NULL,
            policy_id TEXT NOT NULL,
            backup_id TEXT NOT NULL,
            contributor_id TEXT NOT NULL,
            logical_path TEXT NOT NULL,
            chunk_map_id TEXT NOT NULL,
            PRIMARY KEY (target_id, policy_id, backup_id, contributor_id, logical_path),
            FOREIGN KEY (chunk_map_id) REFERENCES chunk_maps(chunk_map_id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chunk_map_hash
        ON chunk_map_chunks (chunk_sha256, length, chunk_map_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_snapshot_chunk_refs_snapshot
        ON snapshot_chunk_refs (target_id, policy_id, backup_id, contributor_id, logical_path)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS index_health (
            target_id TEXT NOT NULL,
            policy_id TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (target_id, policy_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    migrated = connection.execute("SELECT value FROM index_meta WHERE key = ?", (INDEX_SCHEMA_KEY,)).fetchone()
    if migrated is None or str(migrated["value"]) != INDEX_SCHEMA_VERSION:
        _migrate_legacy_chunk_index(connection)
        connection.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)", (INDEX_SCHEMA_KEY, INDEX_SCHEMA_VERSION))
        connection.commit()
    return connection


def _migrate_legacy_chunk_index(connection: sqlite3.Connection) -> None:
    """Idempotently project legacy rows into immutable maps and inherited refs."""
    legacy_count = int(connection.execute("SELECT COUNT(*) FROM snapshot_chunks").fetchone()[0])
    rows = connection.execute(
        """
        SELECT c.target_id, c.policy_id, c.backup_id, c.contributor_id, c.logical_path,
               c.chunk_ordinal, c.offset, c.length, c.chunk_sha256,
               f.size AS file_size, f.sha256 AS file_sha256,
               COALESCE(NULLIF(l.chunk_protocol, ''), ?) AS protocol
        FROM snapshot_chunks c
        JOIN snapshot_files f
          ON f.target_id = c.target_id AND f.policy_id = c.policy_id AND f.backup_id = c.backup_id
         AND f.contributor_id = c.contributor_id AND f.logical_path = c.logical_path
        JOIN snapshot_lineages l
          ON l.target_id = c.target_id AND l.policy_id = c.policy_id AND l.backup_id = c.backup_id
        ORDER BY c.target_id, c.policy_id, c.backup_id, c.contributor_id, c.logical_path, c.chunk_ordinal
        """,
        (CDC_ALGORITHM_V2,),
    ).fetchall()
    migration_broken = len(rows) != legacy_count
    grouped: dict[tuple[str, str, str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = (
            str(row["target_id"]),
            str(row["policy_id"]),
            str(row["backup_id"]),
            str(row["contributor_id"]),
            str(row["logical_path"]),
        )
        grouped.setdefault(key, []).append(row)
    for (target_id, policy_id, backup_id, contributor_id, logical_path), items in grouped.items():
        first = items[0]
        protocol = str(first["protocol"])
        if protocol not in SUPPORTED_CDC_PROTOCOLS:
            migration_broken = True
            continue
        file = FileRecord(contributor_id, logical_path, int(first["file_size"]), str(first["file_sha256"]))
        chunk_records = [
            ChunkRecord(
                contributor_id,
                logical_path,
                int(item["chunk_ordinal"]),
                int(item["offset"]),
                int(item["length"]),
                str(item["chunk_sha256"]),
            )
            for item in items
        ]
        try:
            _validate_chunk_map(file, chunk_records)
        except AppError:
            migration_broken = True
            continue
        map_id = chunk_map_id(protocol=protocol, file_size=file.size, file_sha256=file.sha256)
        connection.execute(
            "INSERT OR IGNORE INTO chunk_maps (chunk_map_id, protocol, file_size, file_sha256, chunk_count) VALUES (?, ?, ?, ?, ?)",
            (map_id, protocol, file.size, file.sha256, len(items)),
        )
        connection.executemany(
            "INSERT OR IGNORE INTO chunk_map_chunks (chunk_map_id, ordinal, offset, length, chunk_sha256) VALUES (?, ?, ?, ?, ?)",
            [(map_id, int(item["chunk_ordinal"]), int(item["offset"]), int(item["length"]), str(item["chunk_sha256"])) for item in items],
        )
        try:
            _assert_stored_chunk_map(connection, map_id=map_id, protocol=protocol, file=file, chunks=chunk_records)
        except AppError:
            migration_broken = True
            continue
        connection.execute(
            "INSERT OR IGNORE INTO snapshot_chunk_refs (target_id, policy_id, backup_id, contributor_id, logical_path, chunk_map_id) VALUES (?, ?, ?, ?, ?, ?)",
            (target_id, policy_id, backup_id, contributor_id, logical_path, map_id),
        )
    # Replay effective refs through immediate parents. Matching file identity is
    # required, so a changed file can never inherit a stale map accidentally.
    lineages = connection.execute(
        "SELECT target_id, policy_id, backup_id, parent_backup_id FROM snapshot_lineages WHERE parent_backup_id IS NOT NULL ORDER BY chain_depth, committed_at, rowid"
    ).fetchall()
    for lineage in lineages:
        parent_exists = connection.execute(
            "SELECT 1 FROM snapshot_lineages WHERE target_id = ? AND policy_id = ? AND backup_id = ?",
            (str(lineage["target_id"]), str(lineage["policy_id"]), str(lineage["parent_backup_id"])),
        ).fetchone()
        if parent_exists is None:
            migration_broken = True
            continue
        connection.execute(
            """
            INSERT OR IGNORE INTO snapshot_chunk_refs
            (target_id, policy_id, backup_id, contributor_id, logical_path, chunk_map_id)
            SELECT ?, ?, ?, child_file.contributor_id, child_file.logical_path, parent_ref.chunk_map_id
            FROM snapshot_chunk_refs parent_ref
            JOIN snapshot_files parent_file
              ON parent_file.target_id = parent_ref.target_id AND parent_file.policy_id = parent_ref.policy_id
             AND parent_file.backup_id = parent_ref.backup_id AND parent_file.contributor_id = parent_ref.contributor_id
             AND parent_file.logical_path = parent_ref.logical_path
            JOIN snapshot_files child_file
              ON child_file.target_id = ? AND child_file.policy_id = ? AND child_file.backup_id = ?
             AND child_file.size = parent_file.size AND child_file.sha256 = parent_file.sha256
            WHERE parent_ref.target_id = ? AND parent_ref.policy_id = ? AND parent_ref.backup_id = ?
            """,
            (
                str(lineage["target_id"]), str(lineage["policy_id"]), str(lineage["backup_id"]),
                str(lineage["target_id"]), str(lineage["policy_id"]), str(lineage["backup_id"]),
                str(lineage["target_id"]), str(lineage["policy_id"]), str(lineage["parent_backup_id"]),
            ),
        )
    if migration_broken:
        scopes = connection.execute(
            "SELECT target_id, policy_id FROM snapshot_lineages UNION SELECT target_id, policy_id FROM snapshot_chunks"
        ).fetchall()
        connection.executemany(
            "INSERT OR REPLACE INTO index_health (target_id, policy_id, status, reason, updated_at) VALUES (?, ?, 'stale', 'chunk-index-rebuild-failed', ?)",
            [(str(row["target_id"]), str(row["policy_id"]), _utc_iso()) for row in scopes],
        )


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
    scope_digest: str = "",
    recipient_set_digest: str = "",
    schema_digest: str = "",
    chunk_protocol: str = "",
    full_committed_at: str | None = None,
    logical_bytes: int = 0,
) -> None:  # pragma: no cover - covered via tests calling lineage/protect paths
    with _connect() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO snapshot_lineages
            (target_id, policy_id, backup_id, parent_backup_id, base_backup_id, chain_depth, root_digest, committed_at,
             scope_digest, recipient_set_digest, schema_digest, chunk_protocol, full_committed_at, logical_bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target_id,
                policy_id,
                backup_id,
                parent_backup_id,
                base_backup_id,
                int(chain_depth),
                root_digest,
                _utc_iso(),
                scope_digest,
                recipient_set_digest,
                schema_digest,
                chunk_protocol,
                full_committed_at,
                int(logical_bytes),
            ),
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


def index_is_healthy(target_id: str, policy_id: str) -> bool:
    if _health_marker_path(target_id, policy_id).exists():
        return False
    with _connect() as connection:
        row = connection.execute(
            "SELECT status FROM index_health WHERE target_id = ? AND policy_id = ?",
            (target_id, policy_id),
        ).fetchone()
    return row is None or str(row["status"]) == "healthy"


def mark_index_stale(target_id: str, policy_id: str, reason: str) -> None:
    marker = _health_marker_path(target_id, policy_id)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(reason + "\n", encoding="utf-8")
        temporary.replace(marker)
    except OSError:
        pass
    try:
        with _connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO index_health (target_id, policy_id, status, reason, updated_at)
                VALUES (?, ?, 'stale', ?, ?)
                """,
                (target_id, policy_id, reason, _utc_iso()),
            )
            connection.commit()
    except sqlite3.Error:
        pass


def _health_marker_path(target_id: str, policy_id: str) -> Path:
    scope = hashlib.sha256(f"{target_id}\0{policy_id}".encode()).hexdigest()[:24]
    return INDEX_DIR / "health" / f"{scope}.stale"


def _validate_chunk_map(file: FileRecord, chunks: list[ChunkRecord]) -> None:
    expected_offset = 0
    for ordinal, item in enumerate(chunks):
        if item.chunk_ordinal != ordinal or item.offset != expected_offset or item.length <= 0:
            raise AppError("Chunk index map is non-contiguous", code=ErrorCode.INTERNAL, status=500)
        if len(item.chunk_sha256) != 64:
            raise AppError("Chunk index map has an invalid digest", code=ErrorCode.INTERNAL, status=500)
        expected_offset += item.length
    if expected_offset != file.size:
        raise AppError("Chunk index map does not cover its file", code=ErrorCode.INTERNAL, status=500)


def _assert_stored_chunk_map(
    connection: sqlite3.Connection,
    *,
    map_id: str,
    protocol: str,
    file: FileRecord,
    chunks: list[ChunkRecord],
) -> None:
    metadata = connection.execute(
        "SELECT protocol, file_size, file_sha256, chunk_count FROM chunk_maps WHERE chunk_map_id = ?",
        (map_id,),
    ).fetchone()
    stored = connection.execute(
        "SELECT ordinal, offset, length, chunk_sha256 FROM chunk_map_chunks WHERE chunk_map_id = ? ORDER BY ordinal",
        (map_id,),
    ).fetchall()
    expected = [(item.chunk_ordinal, item.offset, item.length, item.chunk_sha256) for item in chunks]
    actual = [(int(row["ordinal"]), int(row["offset"]), int(row["length"]), str(row["chunk_sha256"])) for row in stored]
    if (
        metadata is None
        or str(metadata["protocol"]) != protocol
        or int(metadata["file_size"]) != file.size
        or str(metadata["file_sha256"]) != file.sha256
        or int(metadata["chunk_count"]) != len(chunks)
        or actual != expected
    ):
        raise AppError("Immutable chunk map conflicts with stored index data", code=ErrorCode.INTERNAL, status=500)


def commit_snapshot_index(
    *,
    target_id: str,
    policy_id: str,
    backup_id: str,
    parent_backup_id: str | None,
    base_backup_id: str | None,
    chain_depth: int,
    root_digest: str,
    files: list[FileRecord],
    chunks: list[ChunkRecord],
    scope_digest: str = "",
    recipient_set_digest: str = "",
    schema_digest: str = "",
    chunk_protocol: str = CURRENT_CDC_PROTOCOL,
    full_committed_at: str | None = None,
    logical_bytes: int = 0,
) -> None:
    """Atomically commit lineage, effective files, immutable maps and refs."""
    grouped: dict[tuple[str, str], list[ChunkRecord]] = {}
    for item in chunks:
        grouped.setdefault((item.contributor_id, item.logical_path), []).append(item)
    file_by_key = {(item.contributor_id, item.logical_path): item for item in files}
    for key, items in grouped.items():
        file = file_by_key.get(key)
        if file is None:
            raise AppError("Chunk index references an unknown file", code=ErrorCode.INTERNAL, status=500)
        _validate_chunk_map(file, sorted(items, key=lambda item: item.chunk_ordinal))

    try:
        with _connect() as connection:
            connection.commit()
            health = connection.execute(
                "SELECT status FROM index_health WHERE target_id = ? AND policy_id = ?",
                (target_id, policy_id),
            ).fetchone()
            rebuilding = parent_backup_id is None and (
                _health_marker_path(target_id, policy_id).exists()
                or (health is not None and str(health["status"]) != "healthy")
            )
            connection.execute("BEGIN IMMEDIATE")
            if rebuilding:
                connection.execute("DELETE FROM snapshot_chunk_refs WHERE target_id = ? AND policy_id = ?", (target_id, policy_id))
                connection.execute("DELETE FROM snapshot_chunks WHERE target_id = ? AND policy_id = ?", (target_id, policy_id))
                connection.execute("DELETE FROM snapshot_files WHERE target_id = ? AND policy_id = ?", (target_id, policy_id))
                connection.execute("DELETE FROM snapshot_lineages WHERE target_id = ? AND policy_id = ?", (target_id, policy_id))
                connection.execute(
                    "DELETE FROM chunk_map_chunks WHERE NOT EXISTS (SELECT 1 FROM snapshot_chunk_refs r WHERE r.chunk_map_id = chunk_map_chunks.chunk_map_id)"
                )
                connection.execute(
                    "DELETE FROM chunk_maps WHERE NOT EXISTS (SELECT 1 FROM snapshot_chunk_refs r WHERE r.chunk_map_id = chunk_maps.chunk_map_id)"
                )
            connection.execute(
                """
                INSERT OR REPLACE INTO snapshot_lineages
                (target_id, policy_id, backup_id, parent_backup_id, base_backup_id, chain_depth, root_digest, committed_at,
                 scope_digest, recipient_set_digest, schema_digest, chunk_protocol, full_committed_at, logical_bytes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target_id, policy_id, backup_id, parent_backup_id, base_backup_id, int(chain_depth), root_digest, _utc_iso(),
                    scope_digest, recipient_set_digest, schema_digest, chunk_protocol, full_committed_at, int(logical_bytes),
                ),
            )
            connection.execute(
                "DELETE FROM snapshot_files WHERE target_id = ? AND policy_id = ? AND backup_id = ?",
                (target_id, policy_id, backup_id),
            )
            connection.executemany(
                "INSERT INTO snapshot_files (target_id, policy_id, backup_id, contributor_id, logical_path, size, sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(target_id, policy_id, backup_id, item.contributor_id, item.logical_path, int(item.size), item.sha256) for item in files],
            )
            connection.execute(
                "DELETE FROM snapshot_chunk_refs WHERE target_id = ? AND policy_id = ? AND backup_id = ?",
                (target_id, policy_id, backup_id),
            )
            for key, raw_items in grouped.items():
                file = file_by_key[key]
                items = sorted(raw_items, key=lambda item: item.chunk_ordinal)
                map_id = chunk_map_id(protocol=chunk_protocol, file_size=file.size, file_sha256=file.sha256)
                if rebuilding:
                    connection.execute("DELETE FROM chunk_map_chunks WHERE chunk_map_id = ?", (map_id,))
                    connection.execute(
                        "INSERT OR REPLACE INTO chunk_maps (chunk_map_id, protocol, file_size, file_sha256, chunk_count) VALUES (?, ?, ?, ?, ?)",
                        (map_id, chunk_protocol, int(file.size), file.sha256, len(items)),
                    )
                    connection.executemany(
                        "INSERT INTO chunk_map_chunks (chunk_map_id, ordinal, offset, length, chunk_sha256) VALUES (?, ?, ?, ?, ?)",
                        [(map_id, item.chunk_ordinal, item.offset, item.length, item.chunk_sha256) for item in items],
                    )
                else:
                    connection.execute(
                        "INSERT OR IGNORE INTO chunk_maps (chunk_map_id, protocol, file_size, file_sha256, chunk_count) VALUES (?, ?, ?, ?, ?)",
                        (map_id, chunk_protocol, int(file.size), file.sha256, len(items)),
                    )
                    connection.executemany(
                        "INSERT OR IGNORE INTO chunk_map_chunks (chunk_map_id, ordinal, offset, length, chunk_sha256) VALUES (?, ?, ?, ?, ?)",
                        [(map_id, item.chunk_ordinal, item.offset, item.length, item.chunk_sha256) for item in items],
                    )
                _assert_stored_chunk_map(connection, map_id=map_id, protocol=chunk_protocol, file=file, chunks=items)
                connection.execute(
                    "INSERT INTO snapshot_chunk_refs (target_id, policy_id, backup_id, contributor_id, logical_path, chunk_map_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (target_id, policy_id, backup_id, key[0], key[1], map_id),
                )
            if parent_backup_id:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO snapshot_chunk_refs
                    (target_id, policy_id, backup_id, contributor_id, logical_path, chunk_map_id)
                    SELECT ?, ?, ?, child_file.contributor_id, child_file.logical_path, parent_ref.chunk_map_id
                    FROM snapshot_chunk_refs parent_ref
                    JOIN snapshot_files parent_file
                      ON parent_file.target_id = parent_ref.target_id AND parent_file.policy_id = parent_ref.policy_id
                     AND parent_file.backup_id = parent_ref.backup_id AND parent_file.contributor_id = parent_ref.contributor_id
                     AND parent_file.logical_path = parent_ref.logical_path
                    JOIN snapshot_files child_file
                      ON child_file.target_id = ? AND child_file.policy_id = ? AND child_file.backup_id = ?
                     AND child_file.size = parent_file.size AND child_file.sha256 = parent_file.sha256
                    WHERE parent_ref.target_id = ? AND parent_ref.policy_id = ? AND parent_ref.backup_id = ?
                    """,
                    (target_id, policy_id, backup_id, target_id, policy_id, backup_id, target_id, policy_id, parent_backup_id),
                )
            connection.execute(
                "INSERT OR REPLACE INTO index_health (target_id, policy_id, status, reason, updated_at) VALUES (?, ?, 'healthy', '', ?)",
                (target_id, policy_id, _utc_iso()),
            )
            connection.commit()
        try:
            _health_marker_path(target_id, policy_id).unlink(missing_ok=True)
        except OSError:
            pass
    except Exception:
        mark_index_stale(target_id, policy_id, "snapshot-index-commit-failed")
        raise


def latest_committed_snapshot(target_id: str, policy_id: str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT backup_id, parent_backup_id, base_backup_id, chain_depth, root_digest, committed_at,
                   scope_digest, recipient_set_digest, schema_digest, chunk_protocol, full_committed_at, logical_bytes
            FROM snapshot_lineages
            WHERE target_id = ? AND policy_id = ?
            ORDER BY committed_at DESC, rowid DESC
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
        grouped: dict[tuple[str, str], list[ChunkRecord]] = {}
        for item in chunks:
            grouped.setdefault((item.contributor_id, item.logical_path), []).append(item)
        connection.execute(
            "DELETE FROM snapshot_chunk_refs WHERE target_id = ? AND policy_id = ? AND backup_id = ?",
            (target_id, policy_id, backup_id),
        )
        for (contributor_id, logical_path), items in grouped.items():
            file = connection.execute(
                "SELECT size, sha256 FROM snapshot_files WHERE target_id = ? AND policy_id = ? AND backup_id = ? AND contributor_id = ? AND logical_path = ?",
                (target_id, policy_id, backup_id, contributor_id, logical_path),
            ).fetchone()
            lineage = connection.execute(
                "SELECT COALESCE(NULLIF(chunk_protocol, ''), ?) AS protocol FROM snapshot_lineages WHERE target_id = ? AND policy_id = ? AND backup_id = ?",
                (CDC_ALGORITHM_V2, target_id, policy_id, backup_id),
            ).fetchone()
            if file is None or lineage is None:
                continue
            ordered = sorted(items, key=lambda item: item.chunk_ordinal)
            map_id = chunk_map_id(protocol=str(lineage["protocol"]), file_size=int(file["size"]), file_sha256=str(file["sha256"]))
            connection.execute(
                "INSERT OR IGNORE INTO chunk_maps (chunk_map_id, protocol, file_size, file_sha256, chunk_count) VALUES (?, ?, ?, ?, ?)",
                (map_id, str(lineage["protocol"]), int(file["size"]), str(file["sha256"]), len(ordered)),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO chunk_map_chunks (chunk_map_id, ordinal, offset, length, chunk_sha256) VALUES (?, ?, ?, ?, ?)",
                [(map_id, item.chunk_ordinal, item.offset, item.length, item.chunk_sha256) for item in ordered],
            )
            connection.execute(
                "INSERT INTO snapshot_chunk_refs (target_id, policy_id, backup_id, contributor_id, logical_path, chunk_map_id) VALUES (?, ?, ?, ?, ?, ?)",
                (target_id, policy_id, backup_id, contributor_id, logical_path, map_id),
            )
        connection.commit()


def load_snapshot_chunks(target_id: str, policy_id: str, backup_id: str) -> list[ChunkRecord]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT r.contributor_id, r.logical_path, c.ordinal AS chunk_ordinal, c.offset, c.length, c.chunk_sha256
            FROM snapshot_chunk_refs r
            JOIN chunk_map_chunks c ON c.chunk_map_id = r.chunk_map_id
            WHERE r.target_id = ? AND r.policy_id = ? AND r.backup_id = ?
            ORDER BY r.contributor_id, r.logical_path, c.ordinal
            """,
            (target_id, policy_id, backup_id),
        ).fetchall()
        if not rows:
            rows = connection.execute(
                "SELECT contributor_id, logical_path, chunk_ordinal, offset, length, chunk_sha256 FROM snapshot_chunks WHERE target_id = ? AND policy_id = ? AND backup_id = ? ORDER BY contributor_id, logical_path, chunk_ordinal",
                (target_id, policy_id, backup_id),
            ).fetchall()
    return [
        ChunkRecord(str(row["contributor_id"]), str(row["logical_path"]), int(row["chunk_ordinal"]), int(row["offset"]), int(row["length"]), str(row["chunk_sha256"]))
        for row in rows
    ]


def load_snapshot_chunks_for_file(
    target_id: str,
    policy_id: str,
    backup_id: str,
    contributor_id: str,
    logical_path: str,
) -> list[ChunkRecord]:
    """Load one parent file's chunk map through the composite index."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT r.contributor_id, r.logical_path, c.ordinal AS chunk_ordinal, c.offset, c.length, c.chunk_sha256
            FROM snapshot_chunk_refs r
            JOIN chunk_map_chunks c ON c.chunk_map_id = r.chunk_map_id
            WHERE r.target_id = ? AND r.policy_id = ? AND r.backup_id = ?
              AND r.contributor_id = ? AND r.logical_path = ?
            ORDER BY c.ordinal
            """,
            (target_id, policy_id, backup_id, contributor_id, logical_path),
        ).fetchall()
        if not rows:
            rows = connection.execute(
                "SELECT contributor_id, logical_path, chunk_ordinal, offset, length, chunk_sha256 FROM snapshot_chunks WHERE target_id = ? AND policy_id = ? AND backup_id = ? AND contributor_id = ? AND logical_path = ? ORDER BY chunk_ordinal",
                (target_id, policy_id, backup_id, contributor_id, logical_path),
            ).fetchall()
    return [
        ChunkRecord(str(row["contributor_id"]), str(row["logical_path"]), int(row["chunk_ordinal"]), int(row["offset"]), int(row["length"]), str(row["chunk_sha256"]))
        for row in rows
    ]


def load_snapshot_chunk_refs(target_id: str, policy_id: str, backup_id: str) -> dict[tuple[str, str], str]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT contributor_id, logical_path, chunk_map_id FROM snapshot_chunk_refs WHERE target_id = ? AND policy_id = ? AND backup_id = ?",
            (target_id, policy_id, backup_id),
        ).fetchall()
    return {(str(row["contributor_id"]), str(row["logical_path"])): str(row["chunk_map_id"]) for row in rows}


def lookup_parent_file_by_digest(
    target_id: str,
    policy_id: str,
    backup_id: str,
    *,
    sha256: str,
    size: int,
    exclude_path: str = "",
) -> FileRecord | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT contributor_id, logical_path, size, sha256
            FROM snapshot_files
            WHERE target_id = ? AND policy_id = ? AND backup_id = ? AND sha256 = ? AND size = ?
              AND logical_path != ?
            ORDER BY contributor_id, logical_path
            LIMIT 1
            """,
            (target_id, policy_id, backup_id, sha256, int(size), exclude_path),
        ).fetchone()
    if row is None:
        return None
    return FileRecord(str(row["contributor_id"]), str(row["logical_path"]), int(row["size"]), str(row["sha256"]))


def lookup_parent_chunks(
    target_id: str,
    policy_id: str,
    backup_id: str,
    candidates: list[tuple[str, int]],
    *,
    preferred_file: tuple[str, str] | None = None,
    batch_size: int = 256,
) -> dict[tuple[str, int], ParentChunkLocation]:
    """Batch exact chunk lookup scoped strictly to the immediate parent view."""
    unique = list(dict.fromkeys((str(sha), int(length)) for sha, length in candidates))
    found: dict[tuple[str, int], ParentChunkLocation] = {}
    with _connect() as connection:
        for start in range(0, len(unique), max(1, min(512, batch_size))):
            batch = unique[start : start + max(1, min(512, batch_size))]
            if not batch:
                continue
            predicates = " OR ".join("(c.chunk_sha256 = ? AND c.length = ?)" for _ in batch)
            parameters: list[Any] = [target_id, policy_id, backup_id]
            for sha256, length in batch:
                parameters.extend((sha256, length))
            rows = connection.execute(
                f"""
                SELECT r.contributor_id, r.logical_path, c.ordinal, c.offset, c.length, c.chunk_sha256
                FROM snapshot_chunk_refs r
                JOIN chunk_map_chunks c ON c.chunk_map_id = r.chunk_map_id
                WHERE r.target_id = ? AND r.policy_id = ? AND r.backup_id = ? AND ({predicates})
                ORDER BY r.contributor_id, r.logical_path, c.ordinal
                """,
                parameters,
            ).fetchall()
            for row in rows:
                key = (str(row["chunk_sha256"]), int(row["length"]))
                location = ParentChunkLocation(
                    str(row["contributor_id"]), str(row["logical_path"]), int(row["ordinal"]),
                    int(row["offset"]), int(row["length"]), str(row["chunk_sha256"]),
                )
                current = found.get(key)
                if current is None or (
                    preferred_file is not None
                    and (location.contributor_id, location.logical_path) == preferred_file
                    and (current.contributor_id, current.logical_path) != preferred_file
                ):
                    found[key] = location
    return found


class ParentChunkBloom:
    """Local-only probabilistic negative cache; exact SQLite remains authoritative."""

    def __init__(self, bits: bytearray, bit_count: int, hash_count: int = BLOOM_HASH_FUNCTIONS) -> None:
        self.bits = bits
        self.bit_count = bit_count
        self.hash_count = hash_count

    @staticmethod
    def _key(sha256: str, length: int) -> bytes:
        return f"{sha256}:{int(length)}".encode("ascii")

    def _positions(self, sha256: str, length: int) -> list[int]:
        digest = hashlib.sha256(self._key(sha256, length)).digest()
        first = int.from_bytes(digest[:8], "big")
        second = int.from_bytes(digest[8:16], "big") or 1
        return [int((first + index * second) % self.bit_count) for index in range(self.hash_count)]

    def add(self, sha256: str, length: int) -> None:
        for position in self._positions(sha256, length):
            self.bits[position // 8] |= 1 << (position % 8)

    def might_contain(self, sha256: str, length: int) -> bool:
        return all(self.bits[position // 8] & (1 << (position % 8)) for position in self._positions(sha256, length))

    def to_bytes(self) -> bytes:
        header = _stable_json({"bits": self.bit_count, "hashes": self.hash_count}) + b"\n"
        return BLOOM_MAGIC + header + bytes(self.bits)

    @classmethod
    def from_bytes(cls, raw: bytes) -> ParentChunkBloom | None:
        try:
            if not raw.startswith(BLOOM_MAGIC):
                return None
            header_raw, body = raw[len(BLOOM_MAGIC) :].split(b"\n", 1)
            header = json.loads(header_raw)
            bit_count = int(header["bits"])
            hash_count = int(header["hashes"])
            if bit_count <= 0 or hash_count <= 0 or len(body) * 8 < bit_count:
                return None
            return cls(bytearray(body), bit_count, hash_count)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None


def _bloom_path(target_id: str, policy_id: str, backup_id: str) -> Path:
    scope = hashlib.sha256(f"{target_id}\0{policy_id}".encode()).hexdigest()[:24]
    return INDEX_DIR / "bloom" / scope / f"{backup_id}.bf"


def parent_chunk_bloom(target_id: str, policy_id: str, backup_id: str) -> ParentChunkBloom:
    path = _bloom_path(target_id, policy_id, backup_id)
    try:
        loaded = ParentChunkBloom.from_bytes(path.read_bytes())
    except OSError:
        loaded = None
    if loaded is not None:
        return loaded
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT c.chunk_sha256, c.length
            FROM snapshot_chunk_refs r JOIN chunk_map_chunks c ON c.chunk_map_id = r.chunk_map_id
            WHERE r.target_id = ? AND r.policy_id = ? AND r.backup_id = ?
            """,
            (target_id, policy_id, backup_id),
        ).fetchall()
    bit_count = max(8, len(rows) * BLOOM_BITS_PER_ITEM)
    bloom = ParentChunkBloom(bytearray((bit_count + 7) // 8), bit_count)
    for row in rows:
        bloom.add(str(row["chunk_sha256"]), int(row["length"]))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(bloom.to_bytes())
        temporary.replace(path)
    except OSError:
        pass
    return bloom


def lookup_parent_chunks_accelerated(
    target_id: str,
    policy_id: str,
    backup_id: str,
    candidates: list[tuple[str, int]],
    *,
    preferred_file: tuple[str, str] | None = None,
) -> tuple[dict[tuple[str, int], ParentChunkLocation], dict[str, int]]:
    bloom = parent_chunk_bloom(target_id, policy_id, backup_id)
    maybe: list[tuple[str, int]] = []
    negatives = 0
    for candidate in dict.fromkeys(candidates):
        if bloom.might_contain(*candidate):
            maybe.append(candidate)
        else:
            negatives += 1
    exact = (
        lookup_parent_chunks(target_id, policy_id, backup_id, maybe, preferred_file=preferred_file)
        if maybe
        else {}
    )
    return exact, {
        "bloomNegatives": negatives,
        "bloomPositives": len(maybe),
        "exactHits": len(exact),
        "falsePositives": max(0, len(maybe) - len(exact)),
    }


def garbage_collect_chunk_maps(deleted_snapshots: list[tuple[str, str, str]]) -> dict[str, int]:
    """Drop physically deleted snapshot rows and now-unreferenced maps."""
    bloom_paths = [_bloom_path(target_id, policy_id, backup_id) for target_id, policy_id, backup_id in deleted_snapshots]
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for target_id, policy_id, backup_id in deleted_snapshots:
            connection.execute(
                "DELETE FROM snapshot_chunk_refs WHERE target_id = ? AND policy_id = ? AND backup_id = ?",
                (target_id, policy_id, backup_id),
            )
            connection.execute(
                "DELETE FROM snapshot_chunks WHERE target_id = ? AND policy_id = ? AND backup_id = ?",
                (target_id, policy_id, backup_id),
            )
            connection.execute(
                "DELETE FROM snapshot_files WHERE target_id = ? AND policy_id = ? AND backup_id = ?",
                (target_id, policy_id, backup_id),
            )
            connection.execute(
                "DELETE FROM snapshot_lineages WHERE target_id = ? AND policy_id = ? AND backup_id = ?",
                (target_id, policy_id, backup_id),
            )
        before = int(connection.execute("SELECT COUNT(*) FROM chunk_maps").fetchone()[0])
        connection.execute(
            "DELETE FROM chunk_map_chunks WHERE NOT EXISTS (SELECT 1 FROM snapshot_chunk_refs r WHERE r.chunk_map_id = chunk_map_chunks.chunk_map_id)"
        )
        connection.execute("DELETE FROM chunk_maps WHERE NOT EXISTS (SELECT 1 FROM snapshot_chunk_refs r WHERE r.chunk_map_id = chunk_maps.chunk_map_id)")
        after = int(connection.execute("SELECT COUNT(*) FROM chunk_maps").fetchone()[0])
        connection.commit()
    for path in bloom_paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return {"deletedSnapshotRefs": len(deleted_snapshots), "deletedChunkMaps": before - after}


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

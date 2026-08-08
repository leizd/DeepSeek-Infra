"""Incremental snapshot graphs and adaptive full checkpoints (4.4.7).

Builds encrypted delta packages relative to a committed parent snapshot,
attests trees with Merkle roots, and never emits deletion tombstones for
unavailable contributors. Content-defined chunking / convergent encryption
are explicitly out of scope.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode

INDEX_DIR = config.ROOT / ".backup-index"
INDEX_DB = INDEX_DIR / "index.db"
DEFAULT_MAX_CHAIN_DEPTH = 8
DEFAULT_FULL_INTERVAL_DAYS = 7
DEFAULT_MAX_DELTA_RATIO = 0.60


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stable_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def leaf_digest(*, contributor_id: str, logical_path: str, size: int, sha256: str) -> str:
    body = {
        "contributorId": contributor_id,
        "logicalPath": logical_path,
        "size": int(size),
        "sha256": sha256,
    }
    return hashlib.sha256(_stable_json(body)).hexdigest()


def merkle_root(leaves: list[str]) -> str:
    if not leaves:
        return hashlib.sha256(b"").hexdigest()
    level = sorted(leaves)
    while len(level) > 1:
        nxt: list[str] = []
        for index in range(0, len(level), 2):
            left = level[index]
            right = level[index + 1] if index + 1 < len(level) else left
            nxt.append(hashlib.sha256((left + right).encode("ascii")).hexdigest())
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


def diff_trees(
    previous: list[FileRecord],
    current: list[FileRecord],
    *,
    successful_contributors: set[str],
) -> dict[str, Any]:
    """Compute put/delete operations with coverage-safe tombstones."""
    prev_map = {(item.contributor_id, item.logical_path): item for item in previous}
    curr_map = {(item.contributor_id, item.logical_path): item for item in current}
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
    payload_refs = {item["payloadRef"] for item in puts}
    return {
        "put": puts,
        "delete": deletes,
        "unchangedFiles": unchanged,
        "uniquePayloads": len(payload_refs),
        "parentRootDigest": snapshot_root(previous),
        "rootDigest": snapshot_root(current),
    }


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
) -> None:
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


def load_snapshot_files(target_id: str, policy_id: str, backup_id: str) -> list[FileRecord]:
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

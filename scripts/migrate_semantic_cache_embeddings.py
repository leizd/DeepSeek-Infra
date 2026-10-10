#!/usr/bin/env python3
"""Backfill semantic-cache f64le BLOBs in explicit, resumable batches."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.core.config import SEMANTIC_CACHE_DB  # noqa: E402
from deepseek_infra.infra.gateway import semantic_cache  # noqa: E402

SQLITE_HEADER = b"SQLite format 3\x00"
WRITE_COLUMNS = ("embedding_blob", "embedding_dimensions", "embedding_format")


class MigrationError(RuntimeError):
    """Safe CLI failure that never contains cached prompts or embeddings."""


def validate_database_path(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MigrationError("database path does not exist or cannot be resolved") from exc
    if path.is_symlink() or not resolved.is_file():
        raise MigrationError("database path must be a regular, non-symlink file")
    try:
        with resolved.open("rb") as stream:
            header = stream.read(len(SQLITE_HEADER))
    except OSError as exc:
        raise MigrationError("database file cannot be read") from exc
    if header != SQLITE_HEADER:
        raise MigrationError("database file is not a recognized SQLite database")
    return resolved


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        uri_path = quote(path.as_posix(), safe="/:~")
        conn = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection) -> set[str]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (semantic_cache.CACHE_TABLE,),
    ).fetchone()
    if exists is None:
        raise MigrationError("semantic cache table is missing")
    columns = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({semantic_cache.CACHE_TABLE})").fetchall()
    }
    if "embedding" not in columns:
        raise MigrationError("semantic cache embedding column is missing")
    return columns


def _namespace_dimensions(cache_version: Any) -> int | None:
    tail = str(cache_version or "").rsplit(":", 1)[-1]
    try:
        dimensions = int(tail)
    except ValueError:
        return None
    if not 0 < dimensions <= semantic_cache.MAX_EMBEDDING_DIMENSIONS:
        return None
    return dimensions


def _representations(row: sqlite3.Row) -> semantic_cache.NormalizedEmbedding | None:
    values = semantic_cache.decode_embedding(row["embedding"])
    if not values or not all(isinstance(value, float) for value in values):
        return None
    expected_dimensions = _namespace_dimensions(row["cache_version"])
    try:
        return semantic_cache.encode_embedding_representations(
            values,
            expected_dimensions=expected_dimensions,
        )
    except semantic_cache.EmbeddingBlobError:
        return None


def _is_current(row: sqlite3.Row, representations: semantic_cache.NormalizedEmbedding) -> bool:
    if row["embedding_format"] != semantic_cache.EMBEDDING_FORMAT_F64LE_V1:
        return False
    if row["embedding_dimensions"] != representations.dimensions:
        return False
    try:
        decoded = semantic_cache.decode_embedding_blob(
            row["embedding_blob"],
            row["embedding_dimensions"],
            expected_dimensions=representations.dimensions,
        )
    except semantic_cache.EmbeddingBlobError:
        return False
    return tuple(decoded) == representations.values


def _select_sql(columns: set[str]) -> str:
    cache_version = "cache_version" if "cache_version" in columns else "'' AS cache_version"
    embedding_blob = "embedding_blob" if "embedding_blob" in columns else "NULL AS embedding_blob"
    embedding_dimensions = "embedding_dimensions" if "embedding_dimensions" in columns else "0 AS embedding_dimensions"
    embedding_format = "embedding_format" if "embedding_format" in columns else "'' AS embedding_format"
    return (
        f"SELECT rowid AS migration_rowid, embedding, {cache_version}, {embedding_blob}, "
        f"{embedding_dimensions}, {embedding_format} FROM {semantic_cache.CACHE_TABLE} "
        "WHERE rowid > ? ORDER BY rowid LIMIT ?"
    )


def _verify(conn: sqlite3.Connection, *, batch_size: int) -> dict[str, int]:
    columns = _table_columns(conn)
    select_sql = _select_sql(columns)
    checked = valid = legacy = invalid = 0
    last_rowid = 0
    while True:
        rows = conn.execute(select_sql, (last_rowid, batch_size)).fetchall()
        if not rows:
            break
        last_rowid = int(rows[-1]["migration_rowid"])
        for row in rows:
            checked += 1
            representations = _representations(row)
            if representations is None:
                invalid += 1
            elif _is_current(row, representations):
                valid += 1
            else:
                legacy += 1
    return {"checked": checked, "valid": valid, "legacy": legacy, "invalid": invalid}


def migrate_database(
    database: Path,
    *,
    batch_size: int = 100,
    dry_run: bool = True,
    verify: bool = False,
) -> dict[str, Any]:
    if not 1 <= batch_size <= 10_000:
        raise MigrationError("batch size must be between 1 and 10000")
    path = validate_database_path(database)
    conn = _connect(path, read_only=dry_run)
    try:
        columns = _table_columns(conn)
        if not dry_run and not set(WRITE_COLUMNS).issubset(columns):
            semantic_cache.initialize_schema(conn)
            conn.commit()
            columns = _table_columns(conn)
        select_sql = _select_sql(columns)
        report: dict[str, Any] = {
            "database": str(path),
            "dryRun": dry_run,
            "batchSize": batch_size,
            "scanned": 0,
            "migrated": 0,
            "skipped": 0,
            "invalid": 0,
            "failed": 0,
            "wouldMigrate": 0,
        }
        last_rowid = 0
        while True:
            rows = conn.execute(select_sql, (last_rowid, batch_size)).fetchall()
            if not rows:
                break
            last_rowid = int(rows[-1]["migration_rowid"])
            updates: list[tuple[sqlite3.Binary, int, str, int]] = []
            for row in rows:
                report["scanned"] += 1
                representations = _representations(row)
                if representations is None:
                    report["invalid"] += 1
                    continue
                if _is_current(row, representations):
                    report["skipped"] += 1
                    continue
                if dry_run:
                    report["wouldMigrate"] += 1
                    continue
                updates.append(
                    (
                        sqlite3.Binary(representations.blob),
                        representations.dimensions,
                        representations.format,
                        int(row["migration_rowid"]),
                    )
                )
            if updates:
                try:
                    with conn:
                        conn.executemany(
                            f"UPDATE {semantic_cache.CACHE_TABLE} "
                            "SET embedding_blob = ?, embedding_dimensions = ?, embedding_format = ? "
                            "WHERE rowid = ?",
                            updates,
                        )
                except sqlite3.Error:
                    report["failed"] += len(updates)
                else:
                    report["migrated"] += len(updates)
        if verify:
            report["verification"] = _verify(conn, batch_size=batch_size)
        return report
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=SEMANTIC_CACHE_DB)
    parser.add_argument("--batch-size", type=int, default=100)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Inspect only (the default)")
    mode.add_argument("--write", action="store_true", help="Persist BLOB representations in batches")
    parser.add_argument("--verify", action="store_true", help="Verify dual-format rows after scanning or writing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = migrate_database(
            args.database,
            batch_size=args.batch_size,
            dry_run=not args.write,
            verify=args.verify,
        )
    except (MigrationError, sqlite3.Error) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, separators=(",", ":")), file=sys.stderr)
        return 2
    print(json.dumps({"ok": report["failed"] == 0, **report}, ensure_ascii=False, separators=(",", ":")))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

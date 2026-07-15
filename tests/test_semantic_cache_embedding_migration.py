from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from deepseek_infra.infra.gateway import semantic_cache
from scripts import migrate_semantic_cache_embeddings as migration


LEGACY_SCHEMA = """
CREATE TABLE semantic_cache_items (
    cache_id TEXT PRIMARY KEY,
    prompt_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    embedding TEXT NOT NULL,
    response_json TEXT NOT NULL,
    usage_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_hit_at INTEGER NOT NULL,
    hit_count INTEGER NOT NULL,
    cache_version TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'global',
    quality_score REAL NOT NULL DEFAULT 0,
    query_text TEXT NOT NULL DEFAULT ''
)
"""


def _database(path: Path, embeddings: list[str]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(LEGACY_SCHEMA)
        for index, embedding in enumerate(embeddings):
            conn.execute(
                "INSERT INTO semantic_cache_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"cache-{index}",
                    f"hash-{index}",
                    "deepseek-v4-pro",
                    f"prompt-{index}",
                    embedding,
                    json.dumps({"content": f"response-{index}"}),
                    json.dumps({"total_tokens": index}),
                    100 + index,
                    200 + index,
                    300 + index,
                    index,
                    "1:test:2",
                    "global",
                    0.9,
                    f"query-{index}",
                ),
            )


def _columns(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {str(row[1]) for row in conn.execute("PRAGMA table_info(semantic_cache_items)")}


def test_migration_dry_run_does_not_write(tmp_path: Path) -> None:
    database = tmp_path / "cache.sqlite3"
    _database(database, ["[1.0,0.0]"])
    before = database.read_bytes()

    report = migration.migrate_database(database, dry_run=True, batch_size=1, verify=True)

    assert report["scanned"] == 1
    assert report["migrated"] == 0
    assert report["wouldMigrate"] == 1
    assert report["failed"] == 0
    assert report["verification"] == {"checked": 1, "valid": 0, "legacy": 1, "invalid": 0}
    assert database.read_bytes() == before
    assert "embedding_blob" not in _columns(database)


def test_migration_is_batched_resumable_and_preserves_legacy_text(tmp_path: Path) -> None:
    database = tmp_path / "cache.sqlite3"
    _database(database, ["[1.0,0.0]", "[0.25,0.75]", "not-json"])
    with sqlite3.connect(database) as conn:
        before = conn.execute(
            "SELECT cache_id, prompt_text, embedding, response_json, usage_json, created_at, updated_at, "
            "last_hit_at, hit_count, cache_version, scope, quality_score, query_text "
            "FROM semantic_cache_items ORDER BY cache_id"
        ).fetchall()

    first = migration.migrate_database(database, dry_run=False, batch_size=1, verify=True)
    second = migration.migrate_database(database, dry_run=False, batch_size=2, verify=True)

    assert first["scanned"] == 3 and first["migrated"] == 2 and first["invalid"] == 1
    assert first["verification"] == {"checked": 3, "valid": 2, "legacy": 0, "invalid": 1}
    assert second["migrated"] == 0 and second["skipped"] == 2 and second["invalid"] == 1
    with sqlite3.connect(database) as conn:
        after = conn.execute(
            "SELECT cache_id, prompt_text, embedding, response_json, usage_json, created_at, updated_at, "
            "last_hit_at, hit_count, cache_version, scope, quality_score, query_text "
            "FROM semantic_cache_items ORDER BY cache_id"
        ).fetchall()
        rows = conn.execute(
            "SELECT embedding, embedding_blob, embedding_dimensions, embedding_format "
            "FROM semantic_cache_items ORDER BY cache_id"
        ).fetchall()
    assert after == before
    assert rows[0][0] == "[1.0,0.0]" and rows[0][2:] == (2, "f64le-v1")
    assert tuple(semantic_cache.decode_embedding_blob(rows[0][1], 2)) == (1.0, 0.0)
    assert rows[2][0] == "not-json" and rows[2][1:] == (None, 0, "")


def test_migration_skips_previously_migrated_rows_and_repairs_corrupt_blob(tmp_path: Path) -> None:
    database = tmp_path / "cache.sqlite3"
    _database(database, ["[1.0,0.0]", "[0.0,1.0]"])
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        semantic_cache.initialize_schema(conn)
        encoded = semantic_cache.encode_embedding_representations([1.0, 0.0])
        conn.execute(
            "UPDATE semantic_cache_items SET embedding_blob=?, embedding_dimensions=?, embedding_format=? WHERE cache_id='cache-0'",
            (encoded.blob, encoded.dimensions, encoded.format),
        )
        conn.execute(
            "UPDATE semantic_cache_items SET embedding_blob=?, embedding_dimensions=?, embedding_format=? WHERE cache_id='cache-1'",
            (b"short", 2, encoded.format),
        )

    report = migration.migrate_database(database, dry_run=False, batch_size=1, verify=True)

    assert report["skipped"] == 1 and report["migrated"] == 1
    assert report["verification"] == {"checked": 2, "valid": 2, "legacy": 0, "invalid": 0}


def test_migration_cli_defaults_to_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    database = tmp_path / "cache.sqlite3"
    _database(database, ["[1.0,0.0]"])

    assert migration.main(["--database", str(database), "--batch-size", "1"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["dryRun"] is True and payload["wouldMigrate"] == 1
    assert "embedding_blob" not in _columns(database)


def test_migration_rejects_non_sqlite_and_unsafe_paths(tmp_path: Path) -> None:
    bad = tmp_path / "not-a-database.txt"
    bad.write_text("not sqlite and no embeddings", encoding="utf-8")

    with pytest.raises(migration.MigrationError, match="not a recognized SQLite"):
        migration.migrate_database(bad)
    with pytest.raises(migration.MigrationError, match="does not exist"):
        migration.migrate_database(tmp_path / "missing.sqlite3")


def test_migration_output_never_contains_embedding_values(tmp_path: Path) -> None:
    database = tmp_path / "cache.sqlite3"
    _database(database, ["[0.123456,0.987654]"])

    rendered = json.dumps(migration.migrate_database(database, dry_run=True))

    assert "0.123456" not in rendered and "0.987654" not in rendered

from __future__ import annotations

import json
import math
import sqlite3
import struct
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from deepseek_infra.infra.gateway import semantic_cache


def _set_pipeline(monkeypatch: pytest.MonkeyPatch, dimensions: int) -> None:
    monkeypatch.setattr(
        semantic_cache,
        "embedding_pipeline",
        lambda: SimpleNamespace(active_provider="test", dimensions=dimensions, error=""),
    )


def _row(
    *,
    embedding: str = "[1.0,0.0]",
    blob: Any = None,
    dimensions: int = 0,
    embedding_format: str = "",
    prompt_hash: str = "candidate",
    row_id: str = "row",
) -> dict[str, Any]:
    return {
        "cache_id": row_id,
        "id": row_id,
        "updated_at": int(time.time()),
        "prompt_hash": prompt_hash,
        "embedding": embedding,
        "embedding_blob": blob,
        "embedding_dimensions": dimensions,
        "embedding_format": embedding_format,
    }


def _dual_row(values: list[float], *, embedding: str | None = None, row_id: str = "row") -> dict[str, Any]:
    encoded = semantic_cache.encode_embedding_representations(values)
    return _row(
        embedding=embedding or encoded.json_text,
        blob=encoded.blob,
        dimensions=encoded.dimensions,
        embedding_format=encoded.format,
        row_id=row_id,
    )


def test_dual_write_preserves_existing_json_contract_and_rounded_blob_values() -> None:
    encoded = semantic_cache.encode_embedding_representations([0.123456789, -0.0, 1.9999999])

    assert encoded.json_text == "[0.123457,-0.0,2.0]"
    assert tuple(semantic_cache.decode_embedding_blob(encoded.blob, 3)) == tuple(json.loads(encoded.json_text))
    assert encoded.blob == struct.pack("<3d", 0.123457, -0.0, 2.0)
    assert encoded.format == "f64le-v1"


def test_embedding_blob_rejects_bad_dimensions_lengths_and_non_finite_values() -> None:
    assert semantic_cache.validate_embedding_blob(struct.pack("<d", 1.0), 0) is False
    assert semantic_cache.validate_embedding_blob(struct.pack("<d", 1.0) + b"x", 1) is False
    assert semantic_cache.validate_embedding_blob(struct.pack("<d", math.nan), 1) is False
    with pytest.raises(semantic_cache.EmbeddingBlobError, match="embedding_dimension_mismatch"):
        semantic_cache.encode_embedding_representations([1.0], expected_dimensions=2)
    with pytest.raises(semantic_cache.EmbeddingBlobError, match="invalid_embedding_values"):
        semantic_cache.encode_embedding_representations([object()])  # type: ignore[list-item]
    with pytest.raises(semantic_cache.EmbeddingBlobError, match="non_finite_embedding"):
        semantic_cache.encode_embedding_representations([math.inf])
    with pytest.raises(semantic_cache.EmbeddingBlobError, match="invalid_embedding_dimensions"):
        semantic_cache.encode_embedding_representations([])
    with pytest.raises(semantic_cache.EmbeddingBlobError, match="invalid_embedding_values"):
        semantic_cache._f64le_bytes([object()])  # type: ignore[list-item]


def test_new_database_writes_and_updates_both_formats(tmp_settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_pipeline(monkeypatch, 2)
    current = [0.123456789, 0.5]
    monkeypatch.setattr(semantic_cache, "embed_text", lambda _text: list(current))
    payload = {"messages": [{"role": "user", "content": "dual write"}]}
    body = {"model": "deepseek-v4-pro", "messages": payload["messages"]}

    assert semantic_cache.store(payload, body, {"content": "first answer"})["stored"] is True
    current[:] = [0.75, 0.25]
    assert semantic_cache.store(payload, body, {"content": "updated answer"})["stored"] is True

    with sqlite3.connect(semantic_cache.SEMANTIC_CACHE_DB) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT embedding, embedding_blob, embedding_dimensions, embedding_format FROM semantic_cache_items"
        ).fetchone()
    assert row is not None
    assert row["embedding"] == "[0.75,0.25]"
    assert bytes(row["embedding_blob"]) == struct.pack("<2d", 0.75, 0.25)
    assert row["embedding_dimensions"] == 2
    assert row["embedding_format"] == "f64le-v1"


def test_schema_upgrade_is_idempotent_and_does_not_scan_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE semantic_cache_items (
            cache_id TEXT PRIMARY KEY, prompt_hash TEXT NOT NULL, model TEXT NOT NULL,
            prompt_text TEXT NOT NULL, embedding TEXT NOT NULL, response_json TEXT NOT NULL,
            usage_json TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            last_hit_at INTEGER NOT NULL, hit_count INTEGER NOT NULL
        )
        """
    )
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    semantic_cache.initialize_schema(conn)
    semantic_cache.initialize_schema(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(semantic_cache_items)")}
    assert {"embedding_blob", "embedding_dimensions", "embedding_format"} <= columns
    operational = "\n".join(statements).upper()
    assert "SELECT * FROM SEMANTIC_CACHE_ITEMS" not in operational
    assert "UPDATE SEMANTIC_CACHE_ITEMS" not in operational
    conn.close()


def test_legacy_database_remains_readable_and_rollback_reads_new_json(
    tmp_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pipeline(monkeypatch, 2)
    body = {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "legacy exact"}]}
    prompt_text = semantic_cache.prompt_text_for_body(body)
    now = int(time.time())
    semantic_cache.SEMANTIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(semantic_cache.SEMANTIC_CACHE_DB) as conn:
        conn.execute(
            """
            CREATE TABLE semantic_cache_items (
                cache_id TEXT PRIMARY KEY, prompt_hash TEXT NOT NULL, model TEXT NOT NULL,
                prompt_text TEXT NOT NULL, embedding TEXT NOT NULL, response_json TEXT NOT NULL,
                usage_json TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                last_hit_at INTEGER NOT NULL, hit_count INTEGER NOT NULL,
                cache_version TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'global',
                quality_score REAL NOT NULL DEFAULT 0, query_text TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "INSERT INTO semantic_cache_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy",
                semantic_cache.stable_hash(prompt_text),
                "deepseek-v4-pro",
                prompt_text,
                "[1.0,0.0]",
                '{"model":"deepseek-v4-pro","content":"legacy answer","reasoning":""}',
                "{}",
                now,
                now,
                0,
                0,
                semantic_cache.cache_version(),
                "global",
                1.0,
                "",
            ),
        )
    monkeypatch.setattr(semantic_cache, "embed_text", lambda _text: (_ for _ in ()).throw(AssertionError("exact hit embedded")))

    hit = semantic_cache.lookup({}, body)

    assert hit.hit is True and hit.result is not None and hit.result["content"] == "legacy answer"
    with sqlite3.connect(semantic_cache.SEMANTIC_CACHE_DB) as conn:
        row = conn.execute("SELECT embedding FROM semantic_cache_items").fetchone()
    assert row is not None and semantic_cache.decode_embedding(row[0]) == [1.0, 0.0]


def test_mixed_rows_rank_correctly_with_one_direct_binary_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", "binary")
    rows = [_dual_row([0.5, 0.0], row_id="blob"), _row(embedding="[0.9,0.0]", row_id="legacy")]
    diagnostics: dict[str, Any] = {}

    with (
        patch.object(semantic_cache._rust_rag, "rank_vectors_from_blobs", return_value=((1, 0.9), True)) as binary,
        patch.object(semantic_cache._rust_rag, "rank_vectors", side_effect=AssertionError("JSON/list Rust path called")),
    ):
        selected, similarity, backend = semantic_cache.best_candidate(
            [1.0, 0.0],
            rows,
            now=int(time.time()),
            prompt_hash="query",
            exact_only=False,
            storage_diagnostics=diagnostics,
        )

    binary.assert_called_once()
    assert len(binary.call_args.args[1]) == 2
    assert selected is not None
    assert selected["id"] == "legacy" and similarity == 0.9 and backend == "rust"
    assert diagnostics == {
        "embeddingStorage": "mixed",
        "blobCandidates": 1,
        "legacyCandidates": 1,
        "invalidBlobCandidates": 0,
    }


def test_valid_blob_is_preferred_over_divergent_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", "binary")
    row = _dual_row([1.0, 0.0], embedding="[0.0,1.0]")
    diagnostics: dict[str, Any] = {}

    selected, similarity, _backend = semantic_cache.best_candidate(
        [1.0, 0.0],
        [row],
        now=int(time.time()),
        prompt_hash="query",
        exact_only=False,
        storage_diagnostics=diagnostics,
    )

    assert selected is row and similarity == 1.0
    assert diagnostics["embeddingStorage"] == "blob"


@pytest.mark.parametrize(
    ("row", "invalid_count"),
    [
        (_row(), 0),
        (_row(blob=struct.pack("<2d", 1.0, 0.0), dimensions=2, embedding_format="unknown"), 1),
        (_row(blob=struct.pack("<d", 1.0), dimensions=2, embedding_format="f64le-v1"), 1),
        (_row(blob=struct.pack("<2d", 1.0, 0.0), dimensions=1, embedding_format="f64le-v1"), 1),
        (_row(blob=struct.pack("<2d", math.nan, 0.0), dimensions=2, embedding_format="f64le-v1"), 1),
        (_row(blob=struct.pack("<2d", 1.0, 0.0) + b"tail", dimensions=2, embedding_format="f64le-v1"), 1),
        (_row(blob=object(), dimensions=2, embedding_format="f64le-v1"), 1),
    ],
    ids=("missing", "unknown-format", "truncated", "dimension-mismatch", "non-finite", "oversized", "memoryview-failure"),
)
def test_invalid_or_missing_blob_uses_same_row_json(
    monkeypatch: pytest.MonkeyPatch, row: dict[str, Any], invalid_count: int
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", "binary")
    diagnostics: dict[str, Any] = {}

    selected, similarity, backend = semantic_cache.best_candidate(
        [1.0, 0.0],
        [row],
        now=int(time.time()),
        prompt_hash="query",
        exact_only=False,
        storage_diagnostics=diagnostics,
    )

    assert selected is row and similarity == 1.0 and backend == "python"
    assert diagnostics["embeddingStorage"] == "json"
    assert diagnostics["legacyCandidates"] == 1
    assert diagnostics["invalidBlobCandidates"] == invalid_count


def test_corrupt_blob_and_json_are_rejected_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", "binary")
    row = _row(embedding="not-json", blob=b"short", dimensions=2, embedding_format="f64le-v1")
    diagnostics: dict[str, Any] = {}

    selected, similarity, backend = semantic_cache.best_candidate(
        [1.0, 0.0],
        [row],
        now=int(time.time()),
        prompt_hash="query",
        exact_only=False,
        storage_diagnostics=diagnostics,
    )

    assert selected is None and similarity == 0.0 and backend == "python"
    assert diagnostics["invalidBlobCandidates"] == 1


def test_direct_blob_path_does_not_decode_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", "binary")
    row = _dual_row([1.0, 0.0], embedding="sentinel-json-must-not-be-read")

    with patch.object(semantic_cache.json, "loads", side_effect=AssertionError("JSON decoded")):
        selected, similarity, _backend = semantic_cache.best_candidate(
            [1.0, 0.0],
            [row],
            now=int(time.time()),
            prompt_hash="query",
            exact_only=False,
        )

    assert selected is row and similarity == 1.0


def test_exact_match_skips_embedding_and_blob_loading(tmp_settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_pipeline(monkeypatch, 2)
    monkeypatch.setattr(semantic_cache, "embed_text", lambda _text: [1.0, 0.0])
    payload = {"messages": [{"role": "user", "content": "exact first"}]}
    body = {"model": "deepseek-v4-pro", "messages": payload["messages"]}
    assert semantic_cache.store(payload, body, {"content": "exact answer"})["stored"] is True
    monkeypatch.setattr(semantic_cache, "embed_text", lambda _text: (_ for _ in ()).throw(AssertionError("embedded")))
    monkeypatch.setattr(semantic_cache, "candidate_rows", lambda *_args: (_ for _ in ()).throw(AssertionError("BLOB loaded")))

    hit = semantic_cache.lookup(payload, body)

    assert hit.hit is True and hit.diagnostics["rankingBackend"] == "exact"
    assert hit.diagnostics["blobCandidates"] == 0


def test_binary_rust_failure_falls_back_to_blob_python_without_json_rust(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", "binary")
    row = _dual_row([1.0, 0.0])
    with (
        patch.object(semantic_cache._rust_rag, "rank_vectors_from_blobs", return_value=(None, False)) as binary,
        patch.object(semantic_cache._rust_rag, "rank_vectors", side_effect=AssertionError("JSON Rust endpoint called")),
    ):
        selected, similarity, backend = semantic_cache.best_candidate(
            [1.0, 0.0],
            [row],
            now=int(time.time()),
            prompt_hash="query",
            exact_only=False,
        )

    binary.assert_called_once()
    assert selected is row and similarity == 1.0 and backend == "python"


def test_embedding_storage_diagnostics_never_include_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", "binary")
    diagnostics: dict[str, Any] = {}
    semantic_cache.best_candidate(
        [0.123456789, 0.0],
        [_dual_row([0.987654321, 0.0])],
        now=int(time.time()),
        prompt_hash="query",
        exact_only=False,
        storage_diagnostics=diagnostics,
    )

    rendered = json.dumps(diagnostics)
    assert "0.123456789" not in rendered and "0.987654321" not in rendered
    assert set(diagnostics) == {"embeddingStorage", "blobCandidates", "legacyCandidates", "invalidBlobCandidates"}

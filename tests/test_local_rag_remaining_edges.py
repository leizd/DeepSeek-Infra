from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deepseek_infra.infra.rag import local_rag


class _Cursor:
    def __init__(self, rows: list[Any] | None = None, one: Any = None) -> None:
        self.rows = rows or []
        self.one = one

    def fetchall(self) -> list[Any]:
        return self.rows

    def fetchone(self) -> Any:
        return self.one


def test_onnx_pipeline_loads_fake_runtime_and_embeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = tmp_path / "model.onnx"
    tokenizer = tmp_path / "tokenizer.json"
    model.write_bytes(b"model")
    tokenizer.write_text("{}", encoding="utf-8")
    session = object()
    token = object()
    monkeypatch.setattr(local_rag, "LOCAL_RAG_EMBEDDING_PROVIDER", "onnx")
    monkeypatch.setattr(local_rag, "LOCAL_RAG_ONNX_MODEL_PATH", str(model))
    monkeypatch.setattr(local_rag, "LOCAL_RAG_TOKENIZER_PATH", str(tokenizer))
    monkeypatch.setattr(local_rag, "LOCAL_RAG_EMBEDDING_DIMENSIONS", 2)
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(InferenceSession=lambda *_args, **_kwargs: session))
    monkeypatch.setitem(sys.modules, "tokenizers", SimpleNamespace(Tokenizer=SimpleNamespace(from_file=lambda _path: token)))

    pipeline = local_rag.EmbeddingPipeline()
    monkeypatch.setattr(pipeline, "_embed_onnx", lambda _text: [3.0, 4.0])

    assert pipeline.active_provider == "onnx"
    assert pipeline.embed("hello") == [0.6, 0.8]


def test_cosine_similarity_empty_input() -> None:
    assert local_rag.cosine_similarity([], [1.0]) == 0.0


def test_score_chunks_with_rust_ignores_malformed_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"item_id": "item-1", "source_id": "source", "text": "hello", "metadata": "{"}]
    monkeypatch.setattr(local_rag._rust_rag, "rust_rag_enabled", lambda: True)
    monkeypatch.setattr(local_rag._rust_rag, "score_chunks", lambda _query, _chunks: ([('item-1', 0.75)], True))

    scores, used_rust = local_rag._score_chunks_with_rust("hello", rows)  # type: ignore[arg-type]

    assert used_rust is True
    assert scores == [0.75]


def test_load_sqlite_vec_success(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: list[sqlite3.Connection] = []
    monkeypatch.setattr(local_rag, "LOCAL_RAG_BACKEND", "sqlite_vec")
    monkeypatch.setitem(sys.modules, "sqlite_vec", SimpleNamespace(load=loaded.append))
    connection = sqlite3.connect(":memory:")

    assert local_rag.load_sqlite_vec(connection) is True
    assert loaded == [connection]
    connection.close()


def test_initialize_schema_resets_dimension_and_handles_vector_table(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    monkeypatch.setattr(local_rag, "_embedding_pipeline", SimpleNamespace(dimensions=2, active_provider="hash"))
    assert local_rag.initialize_schema(connection, vec_loaded=False) is False
    connection.execute(f"UPDATE {local_rag.META_TABLE} SET value = '999' WHERE key = 'embedding_dimensions'")
    assert local_rag.initialize_schema(connection, vec_loaded=False) is False
    connection.close()

    class Connection:
        def execute(self, _sql: str, _params: object = None) -> _Cursor:
            return _Cursor(one=None)

        def commit(self) -> None:
            return None

    assert local_rag.initialize_schema(Connection(), vec_loaded=True) is True  # type: ignore[arg-type]

    class FailingConnection(Connection):
        def execute(self, sql: str, params: object = None) -> _Cursor:
            if "CREATE VIRTUAL TABLE" in sql:
                raise sqlite3.OperationalError("vec0 unavailable")
            return super().execute(sql, params)

    assert local_rag.initialize_schema(FailingConnection(), vec_loaded=True) is False  # type: ignore[arg-type]


def test_upsert_items_isolates_database_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_rag, "LOCAL_RAG_ENABLED", True)
    monkeypatch.setattr(local_rag, "db_ready", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))

    assert local_rag.upsert_items([{"item_id": "item-1", "text": "hello"}]) == 0
    assert "index write failed" in local_rag._last_error


def test_upsert_item_updates_vector_table_and_isolates_vec_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        def __init__(self, fail_vector: bool = False) -> None:
            self.fail_vector = fail_vector
            self.queries: list[str] = []

        def execute(self, sql: str, _params: object = None) -> _Cursor:
            self.queries.append(sql)
            if self.fail_vector and f"DELETE FROM {local_rag.VECTOR_TABLE}" in sql:
                raise sqlite3.OperationalError("vector write failed")
            return _Cursor()

    item = {"item_id": "item-1", "collection": "files", "source_id": "source", "text": "hello", "embedding": [1.0, 0.0]}
    connection = Connection()
    local_rag.upsert_item(connection, item, vector_table_ready=True)  # type: ignore[arg-type]
    assert any(f"INSERT INTO {local_rag.VECTOR_TABLE}" in query for query in connection.queries)

    failing = Connection(fail_vector=True)
    local_rag.upsert_item(failing, item, vector_table_ready=True)  # type: ignore[arg-type]
    assert "sqlite-vec insert failed" in local_rag._last_error


def test_delete_items_applies_scope_to_vector_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        def __init__(self) -> None:
            self.queries: list[tuple[str, object]] = []

        def execute(self, sql: str, params: object = None) -> _Cursor:
            self.queries.append((sql, params))
            if sql.startswith("SELECT vector_id"):
                return _Cursor(rows=[{"vector_id": 7}])
            return _Cursor()

        def commit(self) -> None:
            return None

        def close(self) -> None:
            return None

    connection = Connection()
    monkeypatch.setattr(local_rag, "LOCAL_RAG_ENABLED", True)
    monkeypatch.setattr(local_rag, "db_ready", lambda: (connection, True))

    assert local_rag.delete_items(collection="memory", scope="global") == 1
    assert any("scope = ?" in sql for sql, _params in connection.queries)
    assert any(f"DELETE FROM {local_rag.VECTOR_TABLE}" in sql for sql, _params in connection.queries)


def test_search_db_uses_vector_filters_and_distance(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {
        "item_id": "item-1",
        "collection": "files",
        "source_id": "source",
        "project_id": "project",
        "chunk_index": 0,
        "name": "Doc",
        "kind": "text",
        "scope": "global",
        "text": "hello",
        "embedding": json.dumps([1.0, 0.0]),
        "metadata": "{}",
    }

    class Connection:
        def execute(self, _sql: str, _params: object = None) -> _Cursor:
            return _Cursor(rows=[{"item_id": "item-1", "distance": 0.5}])

    monkeypatch.setattr(local_rag, "embed_text", lambda _query: [1.0, 0.0])
    monkeypatch.setattr(local_rag, "load_candidate_rows", lambda *_args, **_kwargs: [row])
    monkeypatch.setattr(local_rag, "_score_chunks_with_rust", lambda _query, _rows: ([1.0], True))

    results = local_rag._search_db(
        Connection(),  # type: ignore[arg-type]
        "hello",
        collection="files",
        limit=2,
        source_id="source",
        project_id="project",
        scopes=["global"],
        vector_table_ready=True,
    )

    assert results[0].item_id == "item-1"
    assert results[0].vector_score == 1.0


def test_search_db_isolates_sqlite_vec_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        def execute(self, _sql: str, _params: object = None) -> _Cursor:
            raise sqlite3.OperationalError("vec query failed")

    monkeypatch.setattr(local_rag, "load_candidate_rows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(local_rag, "embed_text", lambda _query: [0.0])
    monkeypatch.setattr(local_rag, "_score_chunks_with_rust", lambda _query, _rows: (None, False))

    assert local_rag._search_db(
        Connection(),  # type: ignore[arg-type]
        "hello",
        collection="files",
        limit=2,
        source_id="",
        project_id=None,
        scopes=None,
        vector_table_ready=True,
    ) == []
    assert "sqlite-vec search failed" in local_rag._last_error


def test_load_candidate_rows_fetches_missing_vector_ids() -> None:
    class Connection:
        def execute(self, sql: str, _params: object = None) -> _Cursor:
            if "item_id IN" in sql:
                return _Cursor(rows=[{"item_id": "item-2"}])
            return _Cursor(rows=[{"item_id": "item-1"}])

    rows = local_rag.load_candidate_rows(
        Connection(),  # type: ignore[arg-type]
        collection="files",
        source_id="",
        project_id=None,
        scopes=None,
        item_ids={"item-1": 0.1, "item-2": 0.2},
    )

    assert [row["item_id"] for row in rows] == ["item-1", "item-2"]


def test_rebuild_media_index_skips_unready_and_empty_media(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.media import indexer, library

    media = [
        {"mediaId": "pending", "status": "processing"},
        {"mediaId": "empty", "status": "ready"},
        {"mediaId": "ready", "status": "ready"},
    ]
    monkeypatch.setattr(library, "list_media", lambda: media)
    monkeypatch.setattr(library, "list_segments", lambda media_id: [] if media_id == "empty" else [{"segmentId": "s1"}])
    monkeypatch.setattr(indexer, "index_media_segments", lambda _media, segments: len(segments))

    assert local_rag.rebuild_media_index() == (1, 1)

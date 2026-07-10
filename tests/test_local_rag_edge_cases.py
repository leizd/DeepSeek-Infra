"""Edge-case tests for local_rag.py to raise coverage."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

import deepseek_infra.infra.rag.local_rag as local_rag
from deepseek_infra.core.config import LOCAL_RAG_DIR


@pytest.fixture(autouse=True)
def _reset_pipeline() -> Iterator[None]:
    local_rag.reset_embedding_pipeline()
    yield
    local_rag.reset_embedding_pipeline()


def test_normalize_vector_invalid_items() -> None:
    result = local_rag.normalize_vector(["abc", 1.0, None], dimensions=3)
    assert len(result) == 3
    assert result[0] == 0.0


def test_bm25_empty_inputs() -> None:
    assert local_rag.bm25_scores([], [["a"]]) == [0.0]
    assert local_rag.bm25_scores(["a"], []) == []


def test_sqlite_vec_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_rag, "LOCAL_RAG_BACKEND", "sqlite_vec")
    monkeypatch.setattr(local_rag, "sqlite_vec_available", lambda: False)
    assert local_rag.load_sqlite_vec(sqlite3.connect(":memory:")) is False


def test_sqlite_vec_load_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    fake = type(sys)("sqlite_vec")
    def fail_load(_conn: sqlite3.Connection) -> None:
        raise Exception("no extension")
    setattr(fake, "load", fail_load)
    monkeypatch.setitem(sys.modules, "sqlite_vec", fake)
    monkeypatch.setattr(local_rag, "LOCAL_RAG_BACKEND", "sqlite_vec")
    assert local_rag.load_sqlite_vec(sqlite3.connect(":memory:")) is False


def test_upsert_items_disabled_or_empty() -> None:
    with patch.object(local_rag, "LOCAL_RAG_ENABLED", False):
        assert local_rag.upsert_items([]) == 0
        assert local_rag.upsert_items([{"item_id": "x"}]) == 0
    assert local_rag.upsert_items([]) == 0


def test_upsert_item_skips_empty_id() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    local_rag.initialize_schema(conn, vec_loaded=False)
    local_rag.upsert_item(conn, {"item_id": ""}, vector_table_ready=False)
    conn.close()


def test_delete_items_with_filters(tmp_settings: Path) -> None:
    cached = {
        "id": "a" * 32,
        "name": "notes.txt",
        "kind": "text",
        "chunks": [{"index": 0, "text": "hello world", "lineStart": 1, "lineEnd": 1}],
    }
    local_rag.index_file_payload(cached)
    deleted = local_rag.delete_items(collection=local_rag.COLLECTION_FILES, source_id="a" * 32, project_id="", scope="")
    assert deleted == 1


def test_delete_items_error_path(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("db fail")

    monkeypatch.setattr(local_rag, "db_ready", fail)
    assert local_rag.delete_items(collection=local_rag.COLLECTION_FILES) == 0


def test_existing_doc_chunks_empty_source() -> None:
    assert local_rag.existing_doc_chunks(local_rag.COLLECTION_FILES, "", "") == {}


def test_existing_doc_chunks_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("db fail")

    monkeypatch.setattr(local_rag, "db_ready", fail)
    assert local_rag.existing_doc_chunks(local_rag.COLLECTION_FILES, "a" * 32, "") == {}


def test_index_file_payload_skips_empty_text_and_malformed_chunks(tmp_settings: Path) -> None:
    cached = {
        "id": "b" * 32,
        "name": "empty.txt",
        "kind": "text",
        "chunks": [
            {"index": 0, "text": "   "},
            "not a dict",
            {"index": 1, "text": "valid chunk"},
        ],
    }
    assert local_rag.index_file_payload(cached) == 1


def test_index_file_payload_no_id() -> None:
    assert local_rag.index_file_payload({"name": "x"}) == 0


def test_index_file_payload_removes_when_no_chunks(tmp_settings: Path) -> None:
    cached = {"id": "c" * 32, "name": "x.txt", "kind": "text", "chunks": []}
    local_rag.index_file_payload(cached)
    assert local_rag.index_file_payload(cached) == 0


def test_index_file_payload_duplicate_chunk_ids(tmp_settings: Path) -> None:
    cached = {
        "id": "d" * 32,
        "name": "dup.txt",
        "kind": "text",
        "chunks": [
            {"index": 0, "text": "first"},
            {"index": 0, "text": "second"},
        ],
    }
    assert local_rag.index_file_payload(cached) == 2


def test_sync_memories_skips_invalid(tmp_settings: Path) -> None:
    memories: list[dict[str, Any]] = [
        cast(dict[str, Any], "not a dict"),
        {"id": "m1", "content": "", "category": "fact"},
        {"id": "", "content": "x"},
    ]
    assert local_rag.sync_memories(memories) == 0


def test_search_empty_query_and_exception() -> None:
    assert local_rag.search_files_index("") == []
    assert local_rag.search_files_index("   ") == []
    with patch.object(local_rag, "db_ready", side_effect=OSError("db fail")):
        assert local_rag.search_files_index("hello") == []


def test_chunk_lineage_without_rust(tmp_settings: Path) -> None:
    cached = {
        "id": "e" * 32,
        "name": "citation.md",
        "kind": "markdown",
        "chunks": [{"index": 0, "text": "line one", "lineStart": 10, "lineEnd": 12}],
    }
    local_rag.index_file_payload(cached)
    results = local_rag.search_files_index("line one")
    assert results
    lineage = local_rag.chunk_lineage(results[0])
    assert "citation" in lineage
    assert "L10-L12" in lineage["citation"]


def test_verify_citation_empty_snippet(tmp_settings: Path) -> None:
    cached = {
        "id": "f" * 32,
        "name": "x.txt",
        "kind": "text",
        "chunks": [{"index": 0, "text": "hello world"}],
    }
    local_rag.index_file_payload(cached)
    item_id = local_rag.file_item_id("f" * 32, "", 0)
    result = local_rag.verify_citation(item_id, "   ")
    assert result["grounded"] is False
    assert result["reason"] == "empty_snippet"


def test_eval_recall_skips_invalid_cases() -> None:
    assert local_rag.evaluate_recall([])["cases"] == 0
    cases: list[dict[str, Any]] = [cast(dict[str, Any], "not a dict")]
    assert local_rag.evaluate_recall(cases)["cases"] == 0
    assert local_rag.evaluate_recall([{"query": "", "relevant": ["x"]}])["cases"] == 0
    assert local_rag.evaluate_recall([{"query": "x", "relevant": []}])["cases"] == 0


def test_status_disabled() -> None:
    with patch.object(local_rag, "LOCAL_RAG_ENABLED", False):
        assert local_rag.status()["enabled"] is False


def test_status_db_error(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_rag, "LOCAL_RAG_ENABLED", True)
    monkeypatch.setattr(local_rag, "LOCAL_RAG_DB", LOCAL_RAG_DIR / "rag.sqlite3")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("db fail")

    monkeypatch.setattr(local_rag, "db_ready", fail)
    payload = local_rag.status()
    assert "db fail" in payload["lastError"]


def test_rebuild_media_index_import_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    original = builtins.__import__

    def blocking_import(name: str, *args: Any, **kwargs: Any) -> object:
        if name.startswith("deepseek_infra.infra.media"):
            raise ImportError("no media module")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking_import)
    assert local_rag.rebuild_media_index() == (0, 0)


def test_read_json_edge_cases(tmp_settings: Path) -> None:
    bad_json = tmp_settings / "bad.json"
    bad_json.write_text("not json", encoding="utf-8")
    assert local_rag.read_json_dict(bad_json) is None
    assert local_rag.read_json_list(bad_json) == []
    not_list = tmp_settings / "not_list.json"
    not_list.write_text("{}", encoding="utf-8")
    assert local_rag.read_json_list(not_list) == []


def test_rebuild_index_reads_disk_files(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file_cache = tmp_settings / ".file-cache"
    memory_dir = tmp_settings / ".memory"
    file_cache.mkdir(parents=True)
    memory_dir.mkdir(parents=True)
    monkeypatch.setattr(local_rag, "FILE_CACHE_DIR", file_cache)
    monkeypatch.setattr(local_rag, "MEMORY_FILE", memory_dir / "memories.json")
    (file_cache / f"{'g' * 32}.json").write_text(
        json.dumps({
            "id": "g" * 32,
            "name": "cjk.txt",
            "kind": "text",
            "chunks": [{"index": 0, "text": "中文测试内容", "lineStart": 1, "lineEnd": 1}],
        }),
        encoding="utf-8",
    )
    (memory_dir / "memories.json").write_text(
        json.dumps([{"id": "m-cjk", "content": "CJK memory", "category": "fact", "scope": "global"}]),
        encoding="utf-8",
    )
    result = local_rag.rebuild_index()
    assert result["ok"] is True
    assert result["files"] == 1
    cjk_results = local_rag.search_files_index("中文测试", limit=2)
    assert cjk_results


def test_score_chunks_with_rust_malformed_metadata(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local_rag.index_file_payload({
        "id": "h" * 32,
        "name": "meta.txt",
        "kind": "text",
        "chunks": [{"index": 0, "text": "metadata test"}],
    })
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _MockResponse(
            200,
            json.dumps({"ranked": [{"id": local_rag.file_item_id("h" * 32, "", 0), "score": 1.0}]}).encode(),
        )
        results = local_rag.search_files_index("metadata", limit=2)
        assert results


def test_vector_distances_missing_rows(tmp_settings: Path) -> None:
    local_rag.index_file_payload({
        "id": "i" * 32,
        "name": "vector.txt",
        "kind": "text",
        "chunks": [{"index": 0, "text": "vector search test"}],
    })
    results = local_rag.search_files_index("vector", limit=2)
    assert results


class _MockResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_MockResponse":
        return self

    def __exit__(self, *args: object) -> None:
        pass

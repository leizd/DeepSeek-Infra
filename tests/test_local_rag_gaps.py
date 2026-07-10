from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import deepseek_infra.core.config as config
import deepseek_infra.infra.rag.local_rag as local_rag
from deepseek_infra.infra.rag.local_rag import RAGSearchResult


@pytest.fixture
def tmp_rag_dir(monkeypatch):
    base = Path("C:/Users/12393/AppData/Local/Temp/opencode")
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base) as tmp_dir:
        root = Path(tmp_dir)
        file_cache_dir = root / ".file-cache"
        local_rag_dir = root / ".local-rag"
        memory_dir = root / ".memory"
        projects_dir = root / ".projects"
        local_rag_dir.mkdir()
        file_cache_dir.mkdir()
        memory_dir.mkdir()
        monkeypatch.setattr(config, "FILE_CACHE_DIR", file_cache_dir)
        monkeypatch.setattr(config, "LOCAL_RAG_DIR", local_rag_dir)
        monkeypatch.setattr(config, "LOCAL_RAG_DB", local_rag_dir / "rag.sqlite3")
        monkeypatch.setattr(config, "MEMORY_FILE", memory_dir / "memories.json")
        monkeypatch.setattr(config, "PROJECTS_DIR", projects_dir)
        monkeypatch.setattr(local_rag, "FILE_CACHE_DIR", file_cache_dir)
        monkeypatch.setattr(local_rag, "LOCAL_RAG_DIR", local_rag_dir)
        monkeypatch.setattr(local_rag, "LOCAL_RAG_DB", local_rag_dir / "rag.sqlite3")
        monkeypatch.setattr(local_rag, "MEMORY_FILE", memory_dir / "memories.json")
        monkeypatch.setattr(local_rag, "PROJECTS_DIR", projects_dir)
        monkeypatch.setattr(local_rag, "LOCAL_RAG_ENABLED", True)
        monkeypatch.setattr(local_rag, "LOCAL_RAG_BACKEND", "python")
        monkeypatch.setattr(local_rag, "LOCAL_RAG_INCREMENTAL", True)
        local_rag.reset_embedding_pipeline()
        yield root
        local_rag.reset_embedding_pipeline()


class TestEmbeddingPipeline:
    def test_onnx_provider_without_model_path(self, tmp_rag_dir, monkeypatch) -> None:
        monkeypatch.setattr(local_rag, "LOCAL_RAG_EMBEDDING_PROVIDER", "onnx")
        monkeypatch.setattr(local_rag, "LOCAL_RAG_ONNX_MODEL_PATH", "")
        monkeypatch.setattr(local_rag, "LOCAL_RAG_TOKENIZER_PATH", "")
        local_rag.reset_embedding_pipeline()
        pipeline = local_rag.embedding_pipeline()
        assert pipeline.active_provider == "hash"
        assert "not configured" in pipeline.error

    def test_onnx_provider_missing_dependencies(self, tmp_rag_dir, monkeypatch) -> None:
        monkeypatch.setattr(local_rag, "LOCAL_RAG_EMBEDDING_PROVIDER", "onnx")
        fake_path = tmp_rag_dir / "model.onnx"
        fake_path.write_text("x")
        fake_tokenizer = tmp_rag_dir / "tokenizer.json"
        fake_tokenizer.write_text("x")
        monkeypatch.setattr(local_rag, "LOCAL_RAG_ONNX_MODEL_PATH", str(fake_path))
        monkeypatch.setattr(local_rag, "LOCAL_RAG_TOKENIZER_PATH", str(fake_tokenizer))
        local_rag.reset_embedding_pipeline()
        with patch.dict("sys.modules", {"numpy": None, "onnxruntime": None, "tokenizers": None}):
            pipeline = local_rag.embedding_pipeline()
        assert pipeline.active_provider == "hash"
        assert "missing" in pipeline.error


class TestVectorHelpers:
    def test_normalize_vector_with_invalid_items(self) -> None:
        result = local_rag.normalize_vector([1.0, "bad", None], dimensions=3)
        assert len(result) == 3

    def test_cosine_similarity_with_invalid_values(self) -> None:
        assert local_rag.cosine_similarity([1.0, "x"], [1.0, 2.0]) == 1.0

    def test_vector_blob(self) -> None:
        blob = local_rag.vector_blob([1.0, 2.0])
        assert isinstance(blob, bytes)
        assert len(blob) == 8

    def test_chunk_hash(self) -> None:
        assert local_rag.chunk_hash("hello")
        assert local_rag.chunk_hash("") != ""

    def test_doc_version(self) -> None:
        assert local_rag.doc_version({0: "a", 1: "b"})


class TestIndexOperations:
    def test_index_file_payload_skips_invalid_chunks(self, tmp_rag_dir) -> None:
        cached = {
            "id": "a" * 32,
            "name": "doc.txt",
            "chunks": [
                "not a dict",
                {"text": "", "index": 1},
                {"text": "valid text", "index": 2},
            ],
        }
        assert local_rag.index_file_payload(cached) == 1

    def test_index_file_payload_removes_when_no_valid_chunks(self, tmp_rag_dir) -> None:
        cached = {"id": "b" * 32, "name": "empty.txt", "chunks": [{"text": ""}]}
        local_rag.index_file_payload(cached)
        assert local_rag.index_file_payload(cached) == 0

    def test_sync_memories_skips_invalid(self, tmp_rag_dir) -> None:
        assert local_rag.sync_memories([{}, {"id": "m1", "content": "hello"}]) == 1

    def test_upsert_items_when_disabled(self, tmp_rag_dir, monkeypatch) -> None:
        monkeypatch.setattr(local_rag, "LOCAL_RAG_ENABLED", False)
        assert local_rag.upsert_items([{"item_id": "x"}]) == 0

    def test_delete_items_when_disabled(self, tmp_rag_dir, monkeypatch) -> None:
        monkeypatch.setattr(local_rag, "LOCAL_RAG_ENABLED", False)
        assert local_rag.delete_items(collection="files") == 0

    def test_delete_items_by_source_and_project(self, tmp_rag_dir) -> None:
        cached = {"id": "c" * 32, "name": "doc.txt", "chunks": [{"text": "chunk", "index": 0}]}
        local_rag.index_file_payload(cached, project_id="proj")
        assert local_rag.delete_items(collection="files", source_id="c" * 32, project_id="proj") == 1

    def test_media_item_id(self) -> None:
        assert local_rag.media_item_id("m1", "s1", 0) == "media:m1:s1:0"

    def test_existing_doc_chunks_with_bad_metadata(self, tmp_rag_dir) -> None:
        cached = {"id": "d" * 32, "name": "doc.txt", "chunks": [{"text": "chunk", "index": 0}]}
        local_rag.index_file_payload(cached)
        conn, _ = local_rag.db_ready()
        conn.execute(f"UPDATE {local_rag.ITEM_TABLE} SET metadata = ? WHERE source_id = ?", ("not json", "d" * 32))
        conn.commit()
        conn.close()
        existing = local_rag.existing_doc_chunks("files", "d" * 32, "")
        assert existing[0]["hash"] == ""


class TestSearch:
    def test_search_by_source_and_project(self, tmp_rag_dir) -> None:
        cached = {"id": "e" * 32, "name": "doc.txt", "chunks": [{"text": "alpha beta gamma", "index": 0}]}
        local_rag.index_file_payload(cached, project_id="proj")
        results = local_rag.search("alpha", collection="files", source_id="e" * 32, project_id="proj")
        assert results

    def test_search_file_chunks(self, tmp_rag_dir) -> None:
        cached = {"id": "f" * 32, "name": "doc.txt", "chunks": [{"text": "hello world", "index": 0}]}
        local_rag.index_file_payload(cached)
        indices = local_rag.search_file_chunks("f" * 32, "", "hello")
        assert indices == [0]

    def test_search_media_index(self, tmp_rag_dir) -> None:
        results = local_rag.search_media_index("query", project_id="proj", media_id="m1")
        assert results == []

    def test_search_with_scopes(self, tmp_rag_dir) -> None:
        local_rag.sync_memories([{"id": "m1", "content": "memory content", "scope": "project:a"}])
        results = local_rag.search_memories_index("memory", scopes=["project:a"])
        assert results

    def test_search_with_empty_query(self, tmp_rag_dir) -> None:
        assert local_rag.search("", collection="files") == []

    def test_search_db_error_isolated(self, tmp_rag_dir, monkeypatch) -> None:
        def boom(*args, **kwargs):
            raise RuntimeError("boom")
        monkeypatch.setattr(local_rag, "db_ready", boom)
        assert local_rag.search("x", collection="files") == []

    def test_chunk_lineage_only_line_start(self) -> None:
        result = RAGSearchResult(
            item_id="i",
            collection="files",
            source_id="s",
            project_id="",
            chunk_index=0,
            name="n",
            kind="text",
            scope="",
            text="t",
            score=0,
            vector_score=0.0,
            keyword_score=0,
            metadata={"lineStart": 5},
        )
        lineage = local_rag.chunk_lineage(result)
        assert "L5" in lineage["citation"]


class TestChunkRetrieval:
    def test_get_chunk_disabled(self, tmp_rag_dir, monkeypatch) -> None:
        monkeypatch.setattr(local_rag, "LOCAL_RAG_ENABLED", False)
        assert local_rag.get_chunk("id") is None

    def test_get_chunk_empty_id(self) -> None:
        assert local_rag.get_chunk("") is None

    def test_get_chunk_error(self, tmp_rag_dir, monkeypatch) -> None:
        def boom(*args, **kwargs):
            raise RuntimeError("boom")
        monkeypatch.setattr(local_rag, "db_ready", boom)
        assert local_rag.get_chunk("id") is None

    def test_parse_embedding_invalid(self) -> None:
        assert local_rag.parse_embedding("not json") == []
        assert local_rag.parse_embedding(json.dumps("not a list")) == [0.0] * local_rag.LOCAL_RAG_EMBEDDING_DIMENSIONS

    def test_row_to_result_with_bad_metadata(self, tmp_rag_dir) -> None:
        cached = {"id": "g" * 32, "name": "doc.txt", "chunks": [{"text": "text", "index": 0}]}
        local_rag.index_file_payload(cached)
        conn, _ = local_rag.db_ready()
        conn.execute(f"UPDATE {local_rag.ITEM_TABLE} SET metadata = ? WHERE source_id = ?", ("not json", "g" * 32))
        conn.commit()
        row = conn.execute(f"SELECT * FROM {local_rag.ITEM_TABLE} WHERE source_id = ?", ("g" * 32,)).fetchone()
        conn.close()
        result = local_rag.row_to_result(row, score=0, vector_score=0.0, keyword_score=0)
        assert result.metadata == {}


class TestVerifyAndEvaluate:
    def test_verify_citation_empty_snippet(self, tmp_rag_dir) -> None:
        cached = {"id": "h" * 32, "name": "doc.txt", "chunks": [{"text": "hello world", "index": 0}]}
        local_rag.index_file_payload(cached)
        item_id = local_rag.file_item_id("h" * 32, "", 0)
        result = local_rag.verify_citation(item_id, "")
        assert result["grounded"] is False
        assert result["reason"] == "empty_snippet"

    def test_verify_citation_partial_grounding(self, tmp_rag_dir) -> None:
        cached = {"id": "i" * 32, "name": "doc.txt", "chunks": [{"text": "hello world", "index": 0}]}
        local_rag.index_file_payload(cached)
        item_id = local_rag.file_item_id("i" * 32, "", 0)
        result = local_rag.verify_citation(item_id, "hello universe", min_coverage=0.5)
        assert result["grounded"] is True


class TestRebuildAndIO:
    def test_rebuild_index_skips_invalid_files(self, tmp_rag_dir) -> None:
        cache_dir = Path(local_rag.FILE_CACHE_DIR)
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{'j' * 32}.json").write_text("not json", encoding="utf-8")
        result = local_rag.rebuild_index()
        assert result["files"] == 0

    def test_iter_cached_file_paths_project_files(self, tmp_rag_dir) -> None:
        project_dir = Path(local_rag.PROJECTS_DIR) / "proj" / "files"
        project_dir.mkdir(parents=True)
        (project_dir / f"{'k' * 32}.json").write_text("{}", encoding="utf-8")
        paths = local_rag.iter_cached_file_paths()
        assert any(project_id == "proj" for _, project_id in paths)

    def test_read_json_dict_invalid(self, tmp_rag_dir) -> None:
        path = tmp_rag_dir / "bad.json"
        path.write_text("not json", encoding="utf-8")
        assert local_rag.read_json_dict(path) is None

    def test_read_json_list_invalid(self, tmp_rag_dir) -> None:
        path = tmp_rag_dir / "bad.json"
        path.write_text("not json", encoding="utf-8")
        assert local_rag.read_json_list(path) == []

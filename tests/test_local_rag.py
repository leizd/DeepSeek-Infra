from __future__ import annotations

import json
from typing import Any

import deepseek_infra.infra.rag.local_rag as local_rag


def test_local_rag_indexes_and_searches_file_chunks(tmp_settings) -> None:
    cached = {
        "id": "a" * 32,
        "name": "guide.txt",
        "kind": "text",
        "chunks": [
            {"index": 0, "text": "alpha beta gamma", "lineStart": 1, "lineEnd": 1},
            {"index": 1, "text": "sqlite vector database for local rag", "lineStart": 2, "lineEnd": 2},
        ],
    }

    indexed = local_rag.index_file_payload(cached)
    results = local_rag.search_files_index("local vector database", limit=3)
    status = local_rag.status()

    assert indexed == 2
    assert results
    assert results[0].source_id == "a" * 32
    assert results[0].chunk_index == 1
    assert status["indexedItems"] >= 2
    assert status["indexedFiles"] >= 1


def test_local_rag_syncs_memory_items(tmp_settings) -> None:
    memories = [
        {
            "id": "m1",
            "content": "Project alpha prefers SQLite local RAG.",
            "category": "project",
            "scope": "project:alpha",
            "source": "manual",
        },
        {
            "id": "m2",
            "content": "Prefers concise answers.",
            "category": "preference",
            "scope": "global",
            "source": "manual",
        },
    ]

    indexed = local_rag.sync_memories(memories)
    hits = local_rag.search_memories_index("SQLite local retrieval", scopes=["project:alpha"], limit=5)

    assert indexed == 2
    assert hits
    assert hits[0].source_id == "m1"
    assert hits[0].scope == "project:alpha"


def test_bm25_scores_rank_term_overlap() -> None:
    scores = local_rag.bm25_scores(
        ["vector", "database"],
        [["alpha", "beta"], ["vector", "database", "store"], ["vector"]],
    )
    assert scores[1] > scores[2] > 0.0
    assert scores[0] == 0.0


def test_chunk_lineage_records_hash_page_and_offsets(tmp_settings) -> None:
    cached = {
        "id": "c" * 32,
        "name": "clock.md",
        "kind": "markdown",
        "chunks": [
            {"index": 0, "text": "intro paragraph about scheduling", "start": 0, "end": 32, "page": 1, "lineStart": 1, "lineEnd": 1},
            {"index": 1, "text": "the CLOCK algorithm approximates LRU using a reference bit", "start": 33, "end": 90, "page": 2, "lineStart": 2, "lineEnd": 2},
        ],
    }
    local_rag.index_file_payload(cached)
    results = local_rag.search_files_index("CLOCK algorithm reference bit", limit=3)
    assert results
    lineage = local_rag.chunk_lineage(results[0])
    assert lineage["docId"] == "c" * 32
    assert lineage["page"] == 2
    assert lineage["startChar"] == 33
    assert lineage["endChar"] == 90
    assert lineage["hash"]
    assert lineage["docVersion"]


def test_incremental_index_skips_unchanged_document(tmp_settings, monkeypatch) -> None:
    cached = {
        "id": "d" * 32,
        "name": "notes.txt",
        "kind": "text",
        "chunks": [{"index": 0, "text": "incremental indexing avoids recompute", "start": 0, "end": 38}],
    }
    assert local_rag.index_file_payload(cached) == 1

    def boom(_text: str) -> list[float]:
        raise AssertionError("unchanged document must not be re-embedded")

    monkeypatch.setattr(local_rag, "embed_text", boom)
    # Re-indexing identical content is a no-op (no embedding work) and reports the current count.
    assert local_rag.index_file_payload(cached) == 1


def test_incremental_index_reuses_embeddings_for_unchanged_chunks(tmp_settings, monkeypatch) -> None:
    cached: dict[str, Any] = {
        "id": "e" * 32,
        "name": "two.txt",
        "kind": "text",
        "chunks": [
            {"index": 0, "text": "first chunk about vectors", "start": 0, "end": 25},
            {"index": 1, "text": "second chunk about sqlite", "start": 26, "end": 51},
        ],
    }
    assert local_rag.index_file_payload(cached) == 2

    calls = {"n": 0}
    original = local_rag.embed_text

    def counting(text: str) -> list[float]:
        calls["n"] += 1
        return original(text)

    monkeypatch.setattr(local_rag, "embed_text", counting)
    cached["chunks"][1] = {"index": 1, "text": "second chunk now about hybrid retrieval", "start": 26, "end": 60}
    assert local_rag.index_file_payload(cached) == 2
    # Only the changed chunk is re-embedded; the unchanged chunk reuses its stored vector.
    assert calls["n"] == 1


def test_verify_citation_grounding(tmp_settings) -> None:
    cached = {
        "id": "f" * 32,
        "name": "grounding.md",
        "kind": "markdown",
        "chunks": [{"index": 0, "text": "The CLOCK algorithm approximates LRU using a reference bit per page.", "start": 0, "end": 66}],
    }
    local_rag.index_file_payload(cached)
    item_id = local_rag.file_item_id("f" * 32, "", 0)

    grounded = local_rag.verify_citation(item_id, "CLOCK algorithm approximates LRU")
    assert grounded["grounded"] is True
    assert grounded["coverage"] == 1.0
    assert grounded["lineage"]["docId"] == "f" * 32

    ungrounded = local_rag.verify_citation(item_id, "quantum teleportation of entangled qubits")
    assert ungrounded["grounded"] is False

    missing = local_rag.verify_citation("file:_:missing:0", "anything")
    assert missing["grounded"] is False
    assert missing["reason"] == "chunk_not_found"


def test_evaluate_recall_at_k(tmp_settings) -> None:
    local_rag.index_file_payload(
        {"id": "a" * 32, "name": "vectors.md", "kind": "markdown", "chunks": [{"index": 0, "text": "sqlite vector database hybrid retrieval", "start": 0, "end": 40}]}
    )
    local_rag.index_file_payload(
        {"id": "b" * 32, "name": "cooking.md", "kind": "markdown", "chunks": [{"index": 0, "text": "a recipe for roasting vegetables in the oven", "start": 0, "end": 44}]}
    )

    report = local_rag.evaluate_recall(
        [{"query": "sqlite vector database", "relevant": ["a" * 32]}],
        k=3,
    )
    assert report["cases"] == 1
    assert report["recallAtK"] == 1.0
    assert report["mrr"] > 0.0
    assert report["details"][0]["hit"] is True


def test_local_rag_rebuild_scans_existing_cache_and_memory(tmp_settings) -> None:
    file_cache = tmp_settings / ".file-cache"
    memory_dir = tmp_settings / ".memory"
    file_cache.mkdir(parents=True)
    memory_dir.mkdir(parents=True)
    (file_cache / f"{'b' * 32}.json").write_text(
        json.dumps(
            {
                "id": "b" * 32,
                "name": "notes.md",
                "kind": "markdown",
                "chunks": [{"index": 0, "text": "chunking vectorization local sqlite", "lineStart": 1, "lineEnd": 1}],
            }
        ),
        encoding="utf-8",
    )
    (memory_dir / "memories.json").write_text(
        json.dumps(
            [
                {
                    "id": "m-local",
                    "content": "User wants 100 percent local RAG.",
                    "category": "project",
                    "scope": "global",
                }
            ]
        ),
        encoding="utf-8",
    )

    result = local_rag.rebuild_index()

    assert result["ok"] is True
    assert result["files"] == 1
    assert result["chunks"] == 1
    assert result["memories"] == 1
    assert local_rag.search_files_index("sqlite vector", limit=2)
    assert local_rag.search_memories_index("local rag", scopes=["global"], limit=2)

"""Tests for Rust RAG opt-in integration."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import deepseek_infra.infra.rag.local_rag as local_rag
from deepseek_infra.infra.rust_core import rag_client


@pytest.fixture(autouse=True)
def _clear_rust_rag_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_RUST_RAG", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_RAG_FALLBACK", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_RAG_TIMEOUT_MS", raising=False)


# --- config / client ---


def test_fallback_enabled_by_default() -> None:
    assert rag_client.fallback_to_python_enabled() is True


def test_rust_rag_disabled_by_default() -> None:
    assert rag_client.rust_rag_enabled() is False


def test_rust_rag_client_disabled_returns_disabled_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "0")
    result = rag_client.normalize_query("hello")
    assert result == (None, False)


# --- query normalization ---


def test_rust_rag_disabled_uses_python_path(
    monkeypatch: pytest.MonkeyPatch, tmp_settings
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "0")
    cached = {
        "id": "x" * 32,
        "name": "notes.txt",
        "kind": "text",
        "chunks": [
            {"index": 0, "text": "rust hot path normalization", "lineStart": 1, "lineEnd": 1}
        ],
    }
    local_rag.index_file_payload(cached)
    with patch.object(local_rag._rust_rag, "score_chunks", return_value=(None, False)) as mock:
        results = local_rag.search_files_index("Rust hot path", limit=3)
        mock.assert_not_called()
    assert results


def test_rust_rag_enabled_normalizes_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    with patch.object(
        local_rag._rust_rag, "normalize_query", return_value=("rust hot path", True)
    ) as mock:
        normalized = local_rag.normalize_search_query("  Rust   HOT  path  ")
        mock.assert_called_once_with("  Rust   HOT  path  ")
    assert normalized == "rust hot path"


def test_rust_rag_preserves_cjk_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    with patch.object(
        local_rag._rust_rag, "normalize_query", return_value=("rust 语言", True)
    ) as mock:
        normalized = local_rag.normalize_search_query("  Rust 语言  ")
        mock.assert_called_once_with("  Rust 语言  ")
    assert normalized == "rust 语言"


# --- chunk scoring ---


def test_rust_rag_enabled_scores_chunks(
    monkeypatch: pytest.MonkeyPatch, tmp_settings
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    cached = {
        "id": "s" * 32,
        "name": "scores.txt",
        "kind": "text",
        "chunks": [
            {"index": 0, "text": "alpha beta", "lineStart": 1, "lineEnd": 1},
            {"index": 1, "text": "exact match phrase", "lineStart": 2, "lineEnd": 2},
        ],
    }
    local_rag.index_file_payload(cached)
    item_id_0 = local_rag.file_item_id("s" * 32, "", 0)
    item_id_1 = local_rag.file_item_id("s" * 32, "", 1)

    with patch.object(
        local_rag._rust_rag,
        "score_chunks",
        return_value=([(item_id_1, 25.0), (item_id_0, 1.0)], True),
    ) as mock:
        results = local_rag.search_files_index("exact match phrase", limit=3)
        mock.assert_called_once()

    assert results
    assert results[0].source_id == "s" * 32
    assert results[0].chunk_index == 1


def test_rust_rag_unreachable_falls_back_to_python(
    monkeypatch: pytest.MonkeyPatch, tmp_settings
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    cached = {
        "id": "f" * 32,
        "name": "fallback.txt",
        "kind": "text",
        "chunks": [
            {"index": 0, "text": "python fallback path", "lineStart": 1, "lineEnd": 1}
        ],
    }
    local_rag.index_file_payload(cached)
    with patch.object(
        local_rag._rust_rag, "score_chunks", return_value=(None, False)
    ) as mock:
        results = local_rag.search_files_index("python fallback", limit=3)
        mock.assert_called_once()
    assert results
    assert results[0].source_id == "f" * 32


# --- citation formatting ---


def test_rust_rag_enabled_formats_citation(
    monkeypatch: pytest.MonkeyPatch, tmp_settings
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    cached = {
        "id": "c" * 32,
        "name": "citation.md",
        "kind": "markdown",
        "chunks": [
            {
                "index": 0,
                "text": "citation formatting hot path",
                "lineStart": 10,
                "lineEnd": 20,
            }
        ],
    }
    local_rag.index_file_payload(cached)
    results = local_rag.search_files_index("citation formatting", limit=3)
    assert results
    with patch.object(
        local_rag._rust_rag, "format_citation", return_value=("rust-citation", True)
    ) as mock:
        lineage = local_rag.chunk_lineage(results[0])
        mock.assert_called_once()
    assert lineage["citation"] == "rust-citation"

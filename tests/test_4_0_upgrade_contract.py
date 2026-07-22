from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from deepseek_infra.infra.gateway import request_preparation, semantic_cache
from deepseek_infra.infra.mcp import protocol_preparation
from deepseek_infra.infra.rag import document_preparation
from deepseek_infra.infra.rust_core import config as rust_config
from deepseek_infra.infra.rust_core import gateway_client, mcp_client, rag_client
from deepseek_infra.infra.rust_core.policy_client import PolicyProxyResult
from deepseek_infra.infra.tool_runtime import tools
from deepseek_infra.web import server


ROOT = Path(__file__).resolve().parents[1]

LEGACY_SCHEMA = """
CREATE TABLE semantic_cache_items (
    cache_id TEXT PRIMARY KEY, prompt_hash TEXT NOT NULL, model TEXT NOT NULL,
    prompt_text TEXT NOT NULL, embedding TEXT NOT NULL, response_json TEXT NOT NULL,
    usage_json TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    last_hit_at INTEGER NOT NULL, hit_count INTEGER NOT NULL,
    cache_version TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'global',
    quality_score REAL NOT NULL DEFAULT 0, query_text TEXT NOT NULL DEFAULT ''
)
"""


def _insert_legacy_row(connection: sqlite3.Connection, cache_id: str = "legacy") -> None:
    connection.execute(
        "INSERT INTO semantic_cache_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            cache_id,
            f"hash-{cache_id}",
            "deepseek-v4-pro",
            "legacy prompt",
            "[1.0,0.0]",
            '{"content":"legacy response"}',
            "{}",
            100,
            200,
            300,
            0,
            "1:test:2",
            "global",
            0.9,
            "legacy prompt",
        ),
    )


def _all_rust_flags_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GATEWAY", "MCP", "POLICY", "RAG"):
        monkeypatch.setenv(f"DEEPSEEK_RUST_{name}", "0")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_DOCUMENT_PREP", "0")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", "json")


def test_upgrade_from_310_needs_no_forced_migration_and_keeps_python_default(
    tmp_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_rust_flags_off(monkeypatch)
    semantic_cache.SEMANTIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(semantic_cache.SEMANTIC_CACHE_DB) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(LEGACY_SCHEMA)
        _insert_legacy_row(connection)
        semantic_cache.initialize_schema(connection)
        legacy = connection.execute("SELECT embedding FROM semantic_cache_items WHERE cache_id='legacy'").fetchone()
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(semantic_cache_items)")}

    assert legacy is not None and json.loads(legacy["embedding"]) == [1.0, 0.0]
    assert {"embedding", "embedding_blob", "embedding_dimensions", "embedding_format"} <= columns
    assert rust_config.load_rust_flags() == rust_config.RustComponentFlags(False, False, False, False)
    assert rust_config.rust_rag_vector_transport() == "json"
    assert server.create_app().version == "4.2.5"

    monkeypatch.setattr(
        semantic_cache,
        "embedding_pipeline",
        lambda: SimpleNamespace(active_provider="test", dimensions=2, error=""),
    )
    monkeypatch.setattr(semantic_cache, "embed_text", lambda _text: [0.25, 0.75])
    payload = {"messages": [{"role": "user", "content": "new record"}]}
    body = {"model": "deepseek-v4-pro", "messages": payload["messages"]}
    assert semantic_cache.store(payload, body, {"content": "new response"})["stored"] is True
    with sqlite3.connect(semantic_cache.SEMANTIC_CACHE_DB) as connection:
        dual = connection.execute(
            "SELECT embedding, embedding_blob, embedding_dimensions, embedding_format "
            "FROM semantic_cache_items WHERE prompt_text LIKE '%new record%'"
        ).fetchone()
    assert dual is not None and dual[0] == "[0.25,0.75]" and bytes(dual[1])
    assert dual[2:] == (2, "f64le-v1")


def test_upgrade_from_rc1_preserves_legacy_flags_sqlite_rows_and_user_directory(
    tmp_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY_FALLBACK", "0")
    monkeypatch.delenv("DEEPSEEK_RUST_POLICY_FAILURE_MODE", raising=False)
    assert rust_config.rust_policy_failure_mode() == "deny"

    semantic_cache.SEMANTIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sentinel = semantic_cache.SEMANTIC_CACHE_DIR / "rc1-user-data.keep"
    sentinel.write_text("preserve", encoding="utf-8")
    with sqlite3.connect(semantic_cache.SEMANTIC_CACHE_DB) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(LEGACY_SCHEMA)
        _insert_legacy_row(connection, "rc1")
        semantic_cache.initialize_schema(connection)
        row = connection.execute("SELECT cache_id, response_json FROM semantic_cache_items WHERE cache_id='rc1'").fetchone()
    assert row is not None and row["cache_id"] == "rc1"
    assert json.loads(row["response_json"])["content"] == "legacy response"
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_402_to_403_adds_only_the_isolated_react_chat_slice() -> None:
    runtime = json.loads((ROOT / "release/4_0_runtime_decision.json").read_text(encoding="utf-8"))
    protocol = json.loads((ROOT / "release/4_0_protocol_contract.json").read_text(encoding="utf-8"))
    notes = (ROOT / "docs/releases/4.0.3.md").read_text(encoding="utf-8")

    assert server.create_app().version == "4.2.5"
    assert runtime["target_version"] == "4.0.0"
    assert runtime["architecture"] == "python_first_hybrid"
    assert runtime["default_sidecar_deployment"] is False
    assert runtime["rust_default_on_components"] == []
    assert protocol["version"] == "4.0.0"
    assert protocol["binary_protocol"]["request_magic"] == "DSVRNK01"
    assert protocol["binary_protocol"]["response_magic"] == "DSVRSP01"
    assert "first complete user workflow" in notes
    assert "Memory-only DeepSeek and Tavily credentials" in notes
    assert "stop-generation" in notes
    assert "`/` does not switch to React" in notes
    assert "Python-first ownership" in notes


def test_402_rows_remain_readable_by_310_style_rollback(tmp_settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _all_rust_flags_off(monkeypatch)
    semantic_cache.SEMANTIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(semantic_cache.SEMANTIC_CACHE_DB) as connection:
        connection.row_factory = sqlite3.Row
        semantic_cache.initialize_schema(connection)
        encoded = semantic_cache.encode_embedding_representations([0.5, 0.5])
        connection.execute(
            """
            INSERT INTO semantic_cache_items (
                cache_id, prompt_hash, model, prompt_text, embedding, embedding_blob,
                embedding_dimensions, embedding_format, response_json, usage_json,
                created_at, updated_at, last_hit_at, hit_count, cache_version, scope,
                quality_score, query_text
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "rc2",
                "hash-rc2",
                "deepseek-v4-pro",
                "rollback prompt",
                encoded.json_text,
                encoded.blob,
                encoded.dimensions,
                encoded.format,
                '{"content":"rc2 response"}',
                "{}",
                100,
                200,
                300,
                0,
                "1:test:2",
                "global",
                0.9,
                "rollback prompt",
            ),
        )
        # This is the legacy 3.10 projection: it does not know or select the BLOB columns.
        legacy = connection.execute(
            "SELECT cache_id, embedding, response_json FROM semantic_cache_items WHERE cache_id='rc2'"
        ).fetchone()
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(semantic_cache_items)")}
    assert legacy is not None and json.loads(legacy["embedding"]) == [0.5, 0.5]
    assert json.loads(legacy["response_json"])["content"] == "rc2 response"
    assert "embedding" in columns and "embedding_blob" in columns
    assert rust_config.load_rust_flags() == rust_config.RustComponentFlags(False, False, False, False)


def test_sidecar_unavailable_falls_back_for_every_delegate_without_data_loss(
    tmp_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_DOCUMENT_PREP", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", "binary")
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY_FAILURE_MODE", "fallback")

    gateway_failure = gateway_client.GatewayProxyResult(False, 0, None, "connection_refused")
    monkeypatch.setattr(gateway_client, "prepare_request_with_rust", lambda _payload: gateway_failure)
    gateway = request_preparation.prepare_request_with_optional_rust(
        {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "hello"}]}
    )
    assert gateway.diagnostics["runtime"] == "python" and gateway.diagnostics["fallback"] is True

    mcp_failure = mcp_client.McpProxyResult(False, 0, None, "connection_refused")
    monkeypatch.setattr(mcp_client, "prepare_mcp_with_rust", lambda _payload: mcp_failure)
    mcp = protocol_preparation.prepare_mcp_protocol_with_optional_rust(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    assert mcp.diagnostics["runtime"] == "python" and mcp.diagnostics["fallback"] is True

    document_failure = rag_client.RagProxyResult(False, 0, None, "connection_refused")
    monkeypatch.setattr(rag_client, "prepare_document", lambda _payload: document_failure)
    document = document_preparation.prepare_rag_document_with_optional_rust(
        {"documentId": "doc-upgrade", "text": "hello\nworld", "chunking": {"chunkChars": 100, "chunkOverlap": 10}}
    )
    assert document.diagnostics["runtime"] == "python" and document.diagnostics["fallback"] is True

    encoded = semantic_cache.encode_embedding_representations([1.0, 0.0])
    row = {
        "id": "row",
        "cache_id": "row",
        "prompt_hash": "candidate",
        "updated_at": int(time.time()),
        "embedding": encoded.json_text,
        "embedding_blob": encoded.blob,
        "embedding_dimensions": encoded.dimensions,
        "embedding_format": encoded.format,
    }
    with (
        patch.object(rag_client, "rank_vectors_from_blobs", return_value=(None, False)) as binary,
        patch.object(rag_client, "rank_vectors", side_effect=AssertionError("binary failure retried JSON Rust")),
    ):
        selected, similarity, backend = semantic_cache.best_candidate(
            [1.0, 0.0], [row], now=int(time.time()), prompt_hash="query", exact_only=False
        )
    binary.assert_called_once()
    assert selected is row and similarity == 1.0 and backend == "python"

    policy_failure = PolicyProxyResult(
        ok=False,
        status=0,
        allowed=False,
        reason="connection refused",
        body={},
        code="policy_backend_unavailable",
    )
    monkeypatch.setattr(tools, "rust_check_capability", lambda *_args, **_kwargs: policy_failure)
    assert tools._evaluate_rust_policy("python_eval", {"expression": "2 + 2"}, None) is None

    semantic_cache.SEMANTIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sentinel = semantic_cache.SEMANTIC_CACHE_DIR / "sidecar-loss.keep"
    sentinel.write_text("user-data", encoding="utf-8")
    assert sentinel.read_text(encoding="utf-8") == "user-data"

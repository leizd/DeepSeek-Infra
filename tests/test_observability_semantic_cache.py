from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import deepseek_infra.infra.gateway.deepseek_client as deepseek_client
from deepseek_infra.infra.gateway.deepseek_client import call_deepseek
from deepseek_infra.infra.observability import observability
from deepseek_infra.infra.gateway import semantic_cache


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return self.data


def response_bytes(content: str = "hello", usage: dict[str, int] | None = None) -> bytes:
    return json.dumps(
        {
            "id": "response-id",
            "model": "deepseek-v4-pro",
            "choices": [{"message": {"content": content}}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    ).encode("utf-8")


def test_trace_store_records_run_and_span(tmp_settings: Any) -> None:
    payload: dict[str, Any] = {}
    context = observability.ensure_trace(payload, kind="chat", title="hello", metadata={"stream": False})
    span = observability.start_span(context.trace_id, name="deepseek", kind="deepseek_api", input_data={"prompt": "hello"})
    span.finish(output_data={"content": "world"}, usage={"total_tokens": 12})
    observability.finish_trace(context.trace_id, metadata={"model": "deepseek-v4-pro"})

    trace = observability.get_trace(context.trace_id)

    assert trace is not None
    assert trace["traceId"] == context.trace_id
    assert trace["status"] == "completed"
    assert trace["summary"]["spanCount"] == 1
    assert trace["summary"]["totalTokens"] == 12
    assert trace["spans"][0]["name"] == "deepseek"
    assert observability.trace_status()["traceCount"] == 1


def test_semantic_cache_store_and_lookup(tmp_settings: Any) -> None:
    payload = {"messages": [{"role": "user", "content": "summarize alpha"}], "toolsEnabled": False}
    body = {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "summarize alpha"}]}

    missed = semantic_cache.lookup(payload, body)
    stored = semantic_cache.store(payload, body, {"model": "deepseek-v4-pro", "content": "alpha summary", "usage": {"total_tokens": 9}})
    hit = semantic_cache.lookup(payload, body)

    assert missed.hit is False
    assert missed.diagnostics["checked"] is True
    assert stored["stored"] is True
    assert hit.hit is True
    assert hit.result is not None
    assert hit.result["content"] == "alpha summary"
    assert hit.diagnostics["hit"] is True


def test_semantic_cache_uses_rust_vector_ranking(tmp_settings: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    monkeypatch.setattr(semantic_cache, "embed_text", lambda _text: [1.0, 0.0])
    first = {"messages": [{"role": "user", "content": "first prompt"}], "toolsEnabled": False}
    second = {"messages": [{"role": "user", "content": "second prompt"}], "toolsEnabled": False}
    first_body = {"model": "deepseek-v4-pro", "messages": first["messages"]}
    second_body = {"model": "deepseek-v4-pro", "messages": second["messages"]}
    assert semantic_cache.store(first, first_body, {"model": "deepseek-v4-pro", "content": "rust ranked"})["stored"] is True

    with patch.object(semantic_cache._rust_rag, "rank_vectors", return_value=((0, 1.0), True)) as rank:
        hit = semantic_cache.lookup(second, second_body)

    rank.assert_called_once_with([1.0, 0.0], [[1.0, 0.0]])
    assert hit.hit is True
    assert hit.result is not None and hit.result["content"] == "rust ranked"
    assert hit.diagnostics["rankingBackend"] == "rust"


def test_semantic_cache_rejects_divergent_rust_vector_score(tmp_settings: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    monkeypatch.setattr(semantic_cache, "embed_text", lambda _text: [1.0, 0.0])
    first = {"messages": [{"role": "user", "content": "parity source"}], "toolsEnabled": False}
    second = {"messages": [{"role": "user", "content": "parity query"}], "toolsEnabled": False}
    first_body = {"model": "deepseek-v4-pro", "messages": first["messages"]}
    second_body = {"model": "deepseek-v4-pro", "messages": second["messages"]}
    assert semantic_cache.store(first, first_body, {"model": "deepseek-v4-pro", "content": "python parity"})["stored"] is True

    with patch.object(semantic_cache._rust_rag, "rank_vectors", return_value=((0, 0.99), True)):
        hit = semantic_cache.lookup(second, second_body)

    assert hit.hit is True
    assert hit.diagnostics["rankingBackend"] == "python"


def test_semantic_cache_rust_failure_falls_back_to_python(tmp_settings: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    monkeypatch.setattr(semantic_cache, "embed_text", lambda _text: [1.0])
    monkeypatch.setattr(semantic_cache, "cosine_similarity", lambda _left, _right: 0.99)
    first = {"messages": [{"role": "user", "content": "fallback source"}], "toolsEnabled": False}
    second = {"messages": [{"role": "user", "content": "fallback query"}], "toolsEnabled": False}
    first_body = {"model": "deepseek-v4-pro", "messages": first["messages"]}
    second_body = {"model": "deepseek-v4-pro", "messages": second["messages"]}
    assert semantic_cache.store(first, first_body, {"model": "deepseek-v4-pro", "content": "python ranked"})["stored"] is True

    with patch.object(semantic_cache._rust_rag, "rank_vectors", return_value=(None, False)):
        hit = semantic_cache.lookup(second, second_body)

    assert hit.hit is True
    assert hit.diagnostics["rankingBackend"] == "python"


def test_call_deepseek_uses_semantic_cache_hit_without_upstream(tmp_settings: Any) -> None:
    payload = {
        "apiKey": "test",
        "model": "expert",
        "toolsEnabled": False,
        "messages": [{"role": "user", "content": "summarize alpha"}],
    }
    fixed_time = "[Current time]\nLocal time: 2026-06-05T00:00:00+08:00\nUTC time: 2026-06-04T16:00:00Z"

    with (
        patch.object(deepseek_client, "format_current_time_context", return_value=fixed_time),
        patch("urllib.request.urlopen", return_value=FakeResponse(response_bytes("alpha summary"))) as urlopen,
    ):
        first = call_deepseek(dict(payload))

    with (
        patch.object(deepseek_client, "format_current_time_context", return_value=fixed_time),
        patch("urllib.request.urlopen", side_effect=AssertionError("upstream should not be called")) as urlopen_again,
    ):
        second = call_deepseek(dict(payload))

    urlopen.assert_called_once()
    urlopen_again.assert_not_called()
    assert first["diagnostics"]["semanticCache"]["stored"] is True
    assert second["content"] == "alpha summary"
    assert second["diagnostics"]["semanticCache"]["hit"] is True
    assert second["diagnostics"]["traceId"]


def test_semantic_cache_skips_tool_enabled_body(tmp_settings: Any) -> None:
    payload = {"messages": [{"role": "user", "content": "need tool"}]}
    body = {
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "need tool"}],
        "tools": [{"type": "function", "function": {"name": "python_eval"}}],
    }

    result = semantic_cache.lookup(payload, body)

    assert result.hit is False
    assert result.diagnostics["checked"] is False
    assert result.diagnostics["skippedReason"] == "tools_enabled"


def test_semantic_cache_version_isolation(tmp_settings: Any, monkeypatch: Any) -> None:
    payload = {"messages": [{"role": "user", "content": "version test"}]}
    body = {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "version test"}]}
    assert semantic_cache.store(payload, body, {"model": "deepseek-v4-pro", "content": "versioned answer"})["stored"] is True
    assert semantic_cache.lookup(payload, body).hit is True

    # Bumping the cache version (or switching embedding model) re-namespaces lookups,
    # so old entries are never served instead of being wrongly reused.
    monkeypatch.setattr(semantic_cache, "SEMANTIC_CACHE_VERSION", "99")
    miss = semantic_cache.lookup(payload, body)
    assert miss.hit is False
    assert miss.diagnostics["cacheVersion"].startswith("99:")


def test_semantic_cache_scope_isolation(tmp_settings: Any) -> None:
    body = {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "scoped q"}]}
    project_a = {"memoryScope": "project:a", "messages": [{"role": "user", "content": "scoped q"}]}
    project_b = {"memoryScope": "project:b", "messages": [{"role": "user", "content": "scoped q"}]}

    semantic_cache.store(project_a, body, {"model": "deepseek-v4-pro", "content": "answer for project A"})

    assert semantic_cache.lookup(project_a, body).hit is True
    miss = semantic_cache.lookup(project_b, body)
    assert miss.hit is False  # answer does not leak across project scopes
    assert miss.diagnostics["scope"] == "project:b"


def test_semantic_cache_skips_low_quality_answer(tmp_settings: Any) -> None:
    payload = {"messages": [{"role": "user", "content": "low quality test"}]}
    body = {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "low quality test"}]}

    refusal = semantic_cache.store(payload, body, {"model": "deepseek-v4-pro", "content": "综合阶段没有返回正文，请重试"})
    assert refusal["stored"] is False
    assert refusal["storeSkippedReason"] == "low_quality"
    assert semantic_cache.lookup(payload, body).hit is False


def test_semantic_cache_attachments_use_exact_match_only(tmp_settings: Any, monkeypatch: Any) -> None:
    # Force cosine similarity to 1.0 so only the exact-match guard can prevent a hit.
    monkeypatch.setattr(semantic_cache, "cosine_similarity", lambda _a, _b: 1.0)

    att_payload_1 = {"messages": [{"role": "user", "content": "Q1", "attachments": [{"kind": "file", "name": "a.txt", "text": "file body"}]}]}
    body_1 = {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "Q1 with file body"}]}
    assert semantic_cache.store(att_payload_1, body_1, {"model": "deepseek-v4-pro", "content": "answer one"})["stored"] is True

    # A different question over the same file: fuzzy cosine is 1.0 but exact-match
    # only -> miss (no false reuse from file-text-dominated embeddings).
    att_payload_2 = {"messages": [{"role": "user", "content": "Q2", "attachments": [{"kind": "file", "name": "a.txt", "text": "file body"}]}]}
    body_2 = {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "Q2 with file body"}]}
    miss = semantic_cache.lookup(att_payload_2, body_2)
    assert miss.hit is False
    assert miss.diagnostics["exactMatchOnly"] is True

    # The exact same file + question reuses the cached answer.
    hit = semantic_cache.lookup(att_payload_1, body_1)
    assert hit.hit is True
    assert hit.result is not None
    assert hit.result["content"] == "answer one"

    # Without attachments, fuzzy matching is allowed (cosine 1.0 -> cross-prompt hit).
    plain_hit = semantic_cache.lookup({"messages": [{"role": "user", "content": "Q2"}]}, body_2)
    assert plain_hit.hit is True
    assert plain_hit.diagnostics["exactMatchOnly"] is False


def test_semantic_cache_empty_large_and_lookup_error_paths(tmp_settings: Any, monkeypatch: Any) -> None:
    payload = {"messages": [{"role": "user", "content": "q"}]}
    body = {"model": "deepseek-v4-pro", "messages": []}
    monkeypatch.setattr(semantic_cache, "prompt_text_for_body", lambda _: "")
    assert semantic_cache.lookup(payload, body).diagnostics["skippedReason"] == "empty_prompt"
    monkeypatch.setattr(semantic_cache, "prompt_text_for_body", lambda _: "x" * 20)
    monkeypatch.setattr(semantic_cache, "SEMANTIC_CACHE_MAX_PROMPT_CHARS", 10)
    assert semantic_cache.lookup(payload, body).diagnostics["skippedReason"] == "prompt_too_large"
    monkeypatch.setattr(semantic_cache, "SEMANTIC_CACHE_MAX_PROMPT_CHARS", 100)
    monkeypatch.setattr(semantic_cache, "embed_text", lambda _: (_ for _ in ()).throw(RuntimeError("embedding corrupt")))
    result = semantic_cache.lookup(payload, body)
    assert result.diagnostics["skippedReason"] == "lookup_error"
    assert "embedding corrupt" in result.diagnostics["lastError"]


def test_semantic_cache_expired_and_bad_record_paths(tmp_settings: Any, monkeypatch: Any) -> None:
    payload = {"messages": [{"role": "user", "content": "q"}]}
    body = {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "q"}]}
    monkeypatch.setattr(semantic_cache, "embed_text", lambda _: [1.0])
    expired = {"updated_at": 1, "prompt_hash": "other", "embedding": "[1]"}
    monkeypatch.setattr(semantic_cache, "candidate_rows", lambda *_: [expired])
    monkeypatch.setattr(semantic_cache.time, "time", lambda: 10_000_000)
    assert semantic_cache.lookup(payload, body).hit is False

    prompt_hash = semantic_cache.stable_hash(semantic_cache.prompt_text_for_body(body))
    bad = {
        "updated_at": 10_000_000,
        "prompt_hash": prompt_hash,
        "embedding": "[1]",
        "response_json": "[]",
        "cache_id": "bad",
        "quality_score": 1,
        "hit_count": 0,
        "usage_json": "{}",
    }
    monkeypatch.setattr(semantic_cache, "candidate_rows", lambda *_: [bad])
    assert semantic_cache.lookup(payload, body).diagnostics["skippedReason"] == "bad_cache_record"


def test_semantic_cache_store_skip_and_failure_matrix(tmp_settings: Any, monkeypatch: Any) -> None:
    payload = {"messages": [{"role": "user", "content": "q"}]}
    body = {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "q"}]}
    monkeypatch.setattr(semantic_cache, "prompt_text_for_body", lambda _: "")
    assert semantic_cache.store(payload, body, {"content": "answer"})["storeSkippedReason"] == "empty_prompt_or_response"
    monkeypatch.setattr(semantic_cache, "prompt_text_for_body", lambda _: "prompt")
    monkeypatch.setattr(semantic_cache, "SEMANTIC_CACHE_MAX_PROMPT_CHARS", 3)
    assert semantic_cache.store(payload, body, {"content": "answer"})["storeSkippedReason"] == "prompt_too_large"
    monkeypatch.setattr(semantic_cache, "SEMANTIC_CACHE_MAX_PROMPT_CHARS", 100)
    monkeypatch.setattr(semantic_cache, "SEMANTIC_CACHE_MAX_RESPONSE_CHARS", 3)
    assert semantic_cache.store(payload, body, {"content": "answer"})["storeSkippedReason"] == "response_too_large"
    monkeypatch.setattr(semantic_cache, "SEMANTIC_CACHE_MAX_RESPONSE_CHARS", 100)
    assert semantic_cache.store(payload, body, {"content": "answer", "search": {"results": []}})["storeSkippedReason"] == "side_effect_response"
    monkeypatch.setattr(semantic_cache, "embed_text", lambda _: (_ for _ in ()).throw(RuntimeError("store db failure")))
    failed = semantic_cache.store(payload, body, {"content": "answer"})
    assert failed["storeSkippedReason"] == "store_error"


def test_semantic_cache_status_clear_touch_and_skip_reason_failures(tmp_settings: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(semantic_cache, "connect_db", lambda: (_ for _ in ()).throw(RuntimeError("db locked")))
    assert semantic_cache.status()["items"] == 0
    assert semantic_cache.clear()["ok"] is False
    semantic_cache.touch_cache("missing")
    assert "db locked" in semantic_cache._last_error

    monkeypatch.setattr(semantic_cache, "SEMANTIC_CACHE_ENABLED", False)
    assert semantic_cache.skip_reason({}, {}) == "disabled"
    monkeypatch.setattr(semantic_cache, "SEMANTIC_CACHE_ENABLED", True)
    monkeypatch.setattr(semantic_cache, "SEMANTIC_CACHE_ATTACHMENTS", False)
    attached = {"messages": [{"role": "user", "content": "q", "attachments": [{"name": "x"}]}]}
    assert semantic_cache.skip_reason(attached, {}) == "attachments"
    assert semantic_cache.skip_reason({}, {"tool_choice": "auto"}) == "tool_choice_enabled"
    assert semantic_cache.skip_reason({}, {"stream_options": {"include_usage": True}}) == ""


def test_semantic_cache_scope_quality_decoders_and_schema_migration() -> None:
    assert semantic_cache.scope_for({"projectId": "alpha"}) == "project:alpha"
    assert semantic_cache.quality_score("") == 0.0
    assert semantic_cache.quality_score("abc") == 0.1
    assert semantic_cache.decode_embedding("not json") == []
    assert semantic_cache.decode_embedding("{}") == []
    assert semantic_cache.decode_embedding('[1,"bad",null]') == [1.0, 0.0, 0.0]
    assert semantic_cache.decode_json("not json") == {}

    class FakeConn:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement: str) -> Any:
            self.statements.append(statement)
            return type("Rows", (), {"fetchall": lambda self: [{"name": "scope"}]})()

    conn = FakeConn()
    semantic_cache._ensure_columns(conn)  # type: ignore[arg-type]
    assert sum("ALTER TABLE" in statement for statement in conn.statements) == 3

"""Targeted test coverage boosters for deepseek_client and semantic_cache."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from deepseek_infra.infra.gateway import deepseek_client, semantic_cache


def test_deepseek_client_helpers(tmp_settings: Path) -> None:
    # 1. Cancellation helpers
    ev = threading.Event()
    assert deepseek_client.request_cancelled(ev) is False
    deepseek_client.raise_if_cancelled(ev)
    ev.set()
    assert deepseek_client.request_cancelled(ev) is True
    with pytest.raises(deepseek_client.RequestCancelled):
        deepseek_client.raise_if_cancelled(ev)

    # 2. Force final answer without tools
    body = {
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": [{"type": "function", "function": {"name": "test"}}],
        "tool_choice": "auto",
    }
    forced = deepseek_client.force_final_answer_without_tools(body)
    assert forced["tool_choice"] == "none"

    # 3. Token budget
    atb = deepseek_client.TokenBudget(total_limit=1000, per_agent_limit=500)
    assert atb.exhausted() is False
    assert atb.agent_exhausted("agent_1") is False
    atb.record(600, key="agent_1")
    assert atb.agent_exhausted("agent_1") is True
    assert atb.exhausted() is False
    atb.record(500, key="agent_2")
    assert atb.exhausted() is True

    # 4. Search and presentation intent helpers
    assert deepseek_client.search_mode({"searchMode": "deep"}) == "deep"
    assert deepseek_client.forced_search_mode({"searchMode": "on"}) is True
    assert deepseek_client.search_tool_enabled({"searchEnabled": False}) is False

    ppt_payload = {"messages": [{"role": "user", "content": "帮我生成一个关于AI的PPT演示文稿"}]}
    assert deepseek_client.presentation_intent_requested(ppt_payload) is True
    assert deepseek_client.should_force_create_pptx(ppt_payload) is True

    tools = [{"type": "function", "function": {"name": "create_pptx"}}]
    assert deepseek_client.has_create_pptx_tool(tools) is True
    assert deepseek_client.forced_artifact_tool_name(ppt_payload, tools) == "create_pptx"

    # 5. Normalization helpers
    assert deepseek_client.normalize_reasoning_effort("low") == "low"
    assert deepseek_client.normalize_reasoning_effort("high") == "high"
    assert deepseek_client.normalize_reasoning_effort("invalid") == "high"

    norm_msgs = deepseek_client.normalize_chat_messages(
        [
            {"role": "user", "content": "Hello", "reasoning": ""},
            {"role": "assistant", "content": "World", "reasoning": "thought"},
        ]
    )
    assert len(norm_msgs) == 2

    # 6. Tool call id and argument canonicalization
    t_id = deepseek_client.stable_tool_call_id(0, "fetch_data")
    assert "fetch_data" in t_id
    args = deepseek_client.canonical_tool_arguments({"b": 2, "a": 1})
    assert args == '{"a":1,"b":2}'

    # 7. Time and context helpers
    time_ctx = deepseek_client.format_current_time_context()
    assert "[Current time]" in time_ctx

    appended = deepseek_client.append_context_to_latest_user(
        [{"role": "user", "content": "Original"}],
        "Extra Context",
    )
    assert appended[-1]["role"] == "system"
    assert "Extra Context" in str(appended[-1]["content"])


def test_semantic_cache_helpers(tmp_settings: Path) -> None:
    # 1. Base status and diagnostics
    st = semantic_cache.status()
    assert "enabled" in st
    assert "items" in st

    diag = semantic_cache.base_diagnostics()
    assert "checked" in diag
    assert "cacheId" in diag

    # 2. Quality score and stable hash
    assert semantic_cache.quality_score("Comprehensive and detailed response content") > 0.0
    assert semantic_cache.quality_score("") == 0.0
    for marker in semantic_cache.LOW_QUALITY_MARKERS:
        assert semantic_cache.quality_score(marker) == 0.1

    sh = semantic_cache.stable_hash("test string")
    assert len(sh) == 32

    # 3. Vector embedding encoding / decoding
    vec = [0.1, 0.2, 0.3, 0.4]
    enc = semantic_cache.encode_embedding(vec)
    assert isinstance(enc, str)
    dec = semantic_cache.decode_embedding(enc)
    assert len(dec) == 4
    assert pytest.approx(dec[0], 1e-4) == 0.1

    norm_emb = semantic_cache.encode_embedding_representations(vec)
    assert norm_emb.dimensions == 4
    assert len(norm_emb.blob) > 0

    blob_dec = semantic_cache.decode_embedding_blob(norm_emb.blob, 4)
    assert len(blob_dec) == 4

    assert semantic_cache.validate_embedding_blob(norm_emb.blob, 4, expected_dimensions=4) is True
    assert semantic_cache.validate_embedding_blob(b"corrupted_bytes", 4, expected_dimensions=4) is False

    # 4. Error setter
    semantic_cache.set_last_error("Cache test error")
    semantic_cache.set_last_error("")

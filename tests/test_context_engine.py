from __future__ import annotations

import pytest

import deepseek_infra.infra.gateway.context_engine as ce
import deepseek_infra.infra.gateway.context_manager as cm
from deepseek_infra.infra.gateway.context_manager import manage_request_body, merge_context_manager_diagnostics
from deepseek_infra.infra.gateway.deepseek_client import build_deepseek_request


def test_estimate_tokens_basic_and_monotonic() -> None:
    assert ce.estimate_tokens("") == 0
    assert ce.estimate_tokens("hello world") == 3  # 11 latin chars / 4 -> ceil
    assert ce.estimate_tokens("你好世界") == 3  # 4 CJK chars / 1.6 -> ceil(2.5)
    assert ce.estimate_tokens("a" * 100) > ce.estimate_tokens("a" * 10)
    # CJK is weighted denser than Latin: same char count costs more tokens.
    assert ce.estimate_tokens("中" * 20) > ce.estimate_tokens("a" * 20)


def test_estimate_message_tokens_counts_image_and_tool_calls() -> None:
    text_only = ce.estimate_message_tokens({"role": "user", "content": "hi there"})
    with_image = ce.estimate_message_tokens(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi there"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxxx"}},
            ],
        }
    )
    assert with_image - text_only >= ce.IMAGE_TOKENS

    with_tool_calls = ce.estimate_message_tokens(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "web_search", "arguments": '{"q":"deepseek"}'}}],
        }
    )
    assert with_tool_calls > ce.MESSAGE_OVERHEAD_TOKENS


def test_estimate_body_breakdown_sums_to_prompt_total() -> None:
    body = {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "你好世界"},
            {"role": "assistant", "content": "Hi!"},
            {"role": "system", "content": "[Per-turn context] current time ..."},
        ],
        "tools": [{"function": {"name": "web_search"}}, {"function": {"name": "eval_math"}}],
    }
    breakdown = ce.estimate_body_breakdown(body)
    assert set(breakdown) == {"system", "tools", "history", "dynamic"}
    assert breakdown["system"] > 0
    assert breakdown["tools"] > 0
    assert breakdown["dynamic"] > 0
    plan = ce.plan_token_budget(body)
    assert plan.estimated_prompt_tokens == sum(breakdown.values())


def test_context_window_for_model_uses_registry_and_default() -> None:
    assert ce.context_window_for_model("deepseek-v4-pro") == 131_072
    assert ce.context_window_for_model("deepseek-v4-flash") == 131_072
    # Edge / Ollama / unknown models fall back to the default window.
    assert ce.context_window_for_model("ollama/llama3") == ce.CONTEXT_ENGINE_DEFAULT_CONTEXT_WINDOW
    assert ce.context_window_for_model("") == ce.CONTEXT_ENGINE_DEFAULT_CONTEXT_WINDOW
    assert ce.context_window_for_model(None) == ce.CONTEXT_ENGINE_DEFAULT_CONTEXT_WINDOW


def test_available_input_tokens_reserves_output_and_margin() -> None:
    window = ce.context_window_for_model("deepseek-v4-pro")
    margin = int(window * ce.CONTEXT_ENGINE_SAFETY_MARGIN_RATIO)
    expected = window - ce.CONTEXT_ENGINE_RESERVE_OUTPUT_TOKENS - margin
    assert ce.available_input_tokens("deepseek-v4-pro") == expected


def test_plan_token_budget_within_budget_for_small_request() -> None:
    body = {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "hi"}]}
    plan = ce.plan_token_budget(body)
    assert plan.within_budget is True
    assert plan.recommendation == "ok"
    assert plan.headroom_tokens == plan.available_input_tokens - plan.estimated_prompt_tokens


def test_plan_token_budget_recommendation_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ce, "CONTEXT_ENGINE_RESERVE_OUTPUT_TOKENS", 0)
    monkeypatch.setattr(ce, "CONTEXT_ENGINE_SAFETY_MARGIN_RATIO", 0.0)
    monkeypatch.setattr(ce, "CONTEXT_ENGINE_DEFAULT_CONTEXT_WINDOW", 100)
    monkeypatch.setattr(ce, "CONTEXT_ENGINE_COMPRESS_THRESHOLD_PCT", 75.0)

    # ~90 latin chars -> ~23 tokens vs 100 budget -> compress band.
    near_full = {"model": "unknown", "messages": [{"role": "user", "content": "a" * 360}]}
    plan = ce.plan_token_budget(near_full, model="unknown")
    assert plan.within_budget is True
    assert plan.recommendation == "compress"

    # Overflow the window -> trim.
    overflow = {"model": "unknown", "messages": [{"role": "user", "content": "a" * 4_000}]}
    plan = ce.plan_token_budget(overflow, model="unknown")
    assert plan.within_budget is False
    assert plan.recommendation == "trim"


def test_token_trim_is_noop_within_budget() -> None:
    messages = [
        {"role": "system", "content": "stable prefix"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "system", "content": "[Per-turn context] now"},
    ]
    trimmed, dropped = ce.token_trim(messages, model="deepseek-v4-pro")
    assert dropped == 0
    assert trimmed == messages


def test_token_trim_drops_oldest_and_preserves_system_anchors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ce, "CONTEXT_ENGINE_RESERVE_OUTPUT_TOKENS", 0)
    monkeypatch.setattr(ce, "CONTEXT_ENGINE_SAFETY_MARGIN_RATIO", 0.0)
    monkeypatch.setattr(ce, "CONTEXT_ENGINE_DEFAULT_CONTEXT_WINDOW", 40)
    monkeypatch.setattr(ce, "CONTEXT_ENGINE_MIN_KEEP_MESSAGES", 2)

    front = {"role": "system", "content": "S"}
    tail = {"role": "system", "content": "[Per-turn context] D"}
    variable = [{"role": "user", "content": f"msg-{i}-" + "x" * 80} for i in range(5)]
    messages = [front, *variable, tail]

    trimmed, dropped = ce.token_trim(messages, model="unknown")
    # Each heavy message forces extra drops down to min_keep (2 of 5 -> drop 3).
    assert dropped == 3
    assert trimmed[0] is front
    assert trimmed[-1] is tail
    middle = trimmed[1:-1]
    assert len(middle) == 2
    # The most recent variable messages survive, in order.
    assert [m["content"][:5] for m in middle] == ["msg-3", "msg-4"]


def test_token_trim_honors_fixed_overhead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ce, "CONTEXT_ENGINE_RESERVE_OUTPUT_TOKENS", 0)
    monkeypatch.setattr(ce, "CONTEXT_ENGINE_SAFETY_MARGIN_RATIO", 0.0)
    monkeypatch.setattr(ce, "CONTEXT_ENGINE_DEFAULT_CONTEXT_WINDOW", 200)
    monkeypatch.setattr(ce, "CONTEXT_ENGINE_MIN_KEEP_MESSAGES", 1)

    messages = [{"role": "user", "content": "a" * 80} for _ in range(4)]
    # Without overhead, comfortably within 200; a large tool-schema overhead forces drops.
    _, dropped_no_overhead = ce.token_trim(list(messages), model="unknown")
    _, dropped_with_overhead = ce.token_trim(list(messages), model="unknown", fixed_overhead_tokens=180)
    assert dropped_no_overhead == 0
    assert dropped_with_overhead > 0


def test_base_context_id_stable_across_dynamic_tail_changes() -> None:
    prefix = {"role": "system", "content": "ROLE PROMPT"}
    tools = [{"function": {"name": "web_search"}}, {"function": {"name": "eval_math"}}]
    turn1 = {
        "model": "deepseek-v4-pro",
        "messages": [prefix, {"role": "user", "content": "a"}, {"role": "system", "content": "dyn-1"}],
        "tools": tools,
    }
    turn2 = {
        "model": "deepseek-v4-pro",
        "messages": [
            prefix,
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
            {"role": "system", "content": "dyn-2-much-longer"},
        ],
        "tools": tools,
    }
    assert ce.base_context_id(turn1) == ce.base_context_id(turn2)

    changed_prefix = {
        "model": "deepseek-v4-pro",
        "messages": [{"role": "system", "content": "DIFFERENT PROMPT"}, {"role": "user", "content": "a"}],
        "tools": tools,
    }
    assert ce.base_context_id(changed_prefix) != ce.base_context_id(turn1)


def test_build_context_diff_describes_turn_composition() -> None:
    body = {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": "prefix"},
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
            {"role": "system", "content": "[Per-turn context] now"},
        ],
        "tools": [{"function": {"name": "web_search"}}],
    }
    diff = ce.build_context_diff(body, dropped=3)
    assert diff["baseContextId"].startswith("ce_")
    kinds = {entry["type"]: entry for entry in diff["delta"]}
    assert kinds["history"]["messages"] == 2
    assert kinds["dynamic_context"]["chars"] > 0
    assert kinds["tools"]["count"] == 1
    assert kinds["trim"]["droppedMessages"] == 3


def test_manage_request_body_attaches_context_engine_block() -> None:
    body = {
        "model": "deepseek-v4-pro",
        "messages": [{"role": "system", "content": "prefix"}, {"role": "user", "content": "hi"}],
        "tools": [{"function": {"name": "eval_math"}}, {"function": {"name": "web_search"}}],
    }
    managed, diag = manage_request_body(body, allow_sliding_window=False)
    engine = diag["contextEngine"]
    assert engine["enabled"] is True
    assert engine["model"] == "deepseek-v4-pro"
    assert engine["tokenBudget"]["contextWindow"] == 131_072
    assert engine["contextDiff"]["baseContextId"].startswith("ce_")
    # Tool ordering is still stabilized by the context manager.
    assert [t["function"]["name"] for t in managed["tools"]] == ["eval_math", "web_search"]

    merged = merge_context_manager_diagnostics({"requestMessageCount": 1}, diag)
    assert "contextEngine" in merged
    assert "contextEngine" not in merged["contextManager"]


def test_manage_request_body_skips_engine_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cm, "CONTEXT_ENGINE_ENABLED", False)
    body = {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "hi"}]}
    _, diag = manage_request_body(body, allow_sliding_window=False)
    assert "contextEngine" not in diag


def test_build_deepseek_request_surfaces_token_budget() -> None:
    prepared = build_deepseek_request(
        {"apiKey": "test", "model": "expert", "messages": [{"role": "user", "content": "hi"}]},
        stream=False,
    )
    engine = prepared.diagnostics["contextEngine"]
    assert engine["model"] == "deepseek-v4-pro"
    assert engine["tokenBudget"]["estimatedPromptTokens"] > 0
    assert engine["tokenBudget"]["withinBudget"] is True

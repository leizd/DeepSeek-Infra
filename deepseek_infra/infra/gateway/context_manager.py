"""Gateway context management for cache-friendly DeepSeek requests."""

from __future__ import annotations

import copy
import json
from typing import Any

from deepseek_infra.core.config import (
    CONTEXT_ENGINE_ENABLED,
    CONTEXT_ENGINE_TOKEN_AWARE_TRIM,
    GATEWAY_CONTEXT_MANAGER_ENABLED,
    GATEWAY_CONTEXT_WINDOW_MESSAGES,
)
from deepseek_infra.infra.gateway import context_engine


def stable_json_dumps(value: Any) -> str:
    """Serialize request bodies deterministically for gateway idempotency."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def manage_request_body(body: dict[str, Any], *, allow_sliding_window: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    if not GATEWAY_CONTEXT_MANAGER_ENABLED:
        return dict(body), {"enabled": False}

    managed = copy.deepcopy(body)
    diagnostics: dict[str, Any] = {
        "enabled": True,
        "stableJson": True,
        "stableSystemPosition": "front",
        "dynamicContextPosition": "tail-system",
        "toolOrderStable": True,
        "slidingWindowApplied": False,
        "slidingWindowAllowed": allow_sliding_window,
        "droppedMessages": 0,
    }

    tools = managed.get("tools")
    if isinstance(tools, list):
        ordered_tools = sorted(tools, key=tool_sort_key)
        managed["tools"] = ordered_tools
        names = [tool_name(tool) for tool in ordered_tools if tool_name(tool)]
        diagnostics["toolOrder"] = names
        diagnostics["toolCount"] = len(ordered_tools)

    messages = managed.get("messages")
    if isinstance(messages, list):
        if allow_sliding_window:
            trimmed_messages, dropped = sliding_window_messages(messages)
            # Token-aware second pass: drop *extra* oldest history only when the
            # per-model estimate still overflows the budget. A no-op for normal
            # turns, so the message-count window's behavior is unchanged.
            if CONTEXT_ENGINE_ENABLED and CONTEXT_ENGINE_TOKEN_AWARE_TRIM:
                trimmed_messages, extra_dropped = context_engine.token_trim(
                    trimmed_messages,
                    model=managed.get("model"),
                    fixed_overhead_tokens=context_engine.estimate_tools_tokens(managed.get("tools")),
                )
                dropped += extra_dropped
                if extra_dropped:
                    diagnostics["tokenAwareTrimApplied"] = True
        else:
            trimmed_messages, dropped = [copy.deepcopy(item) for item in messages], 0
        if dropped:
            managed["messages"] = trimmed_messages
            diagnostics["slidingWindowApplied"] = True
            diagnostics["droppedMessages"] = dropped
        diagnostics["messageCount"] = len(managed.get("messages") or [])
        diagnostics["requestMessageCount"] = sum(
            1 for item in managed.get("messages") or [] if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
        )
        diagnostics["hasFrontSystemPrompt"] = bool(managed["messages"] and isinstance(managed["messages"][0], dict) and managed["messages"][0].get("role") == "system")
        diagnostics["hasTrailingDynamicContext"] = bool(
            len(managed["messages"]) > 1
            and isinstance(managed["messages"][-1], dict)
            and managed["messages"][-1].get("role") == "system"
        )

    if CONTEXT_ENGINE_ENABLED:
        diagnostics["contextEngine"] = context_engine.build_engine_diagnostics(
            managed,
            model=managed.get("model"),
            dropped=int(diagnostics.get("droppedMessages") or 0),
        )

    return managed, diagnostics


def tool_sort_key(tool: Any) -> tuple[str, str]:
    if not isinstance(tool, dict):
        return ("~", "")
    return (tool_name(tool), str(tool.get("type") or ""))


def tool_name(tool: dict[str, Any]) -> str:
    function = tool.get("function")
    if not isinstance(function, dict):
        return ""
    return str(function.get("name") or "")


def sliding_window_messages(messages: list[Any]) -> tuple[list[Any], int]:
    if len(messages) <= GATEWAY_CONTEXT_WINDOW_MESSAGES:
        return [copy.deepcopy(item) for item in messages], 0

    first: list[Any] = []
    tail: list[Any] = []
    start_index = 0
    end_index = len(messages)

    if isinstance(messages[0], dict) and messages[0].get("role") == "system":
        first = [messages[0]]
        start_index = 1
    if end_index > start_index and isinstance(messages[-1], dict) and messages[-1].get("role") == "system":
        tail = [messages[-1]]
        end_index -= 1

    variable_messages = messages[start_index:end_index]
    remaining_budget = max(1, GATEWAY_CONTEXT_WINDOW_MESSAGES - len(first) - len(tail))
    kept_variable = variable_messages[-remaining_budget:]
    dropped = max(0, len(variable_messages) - len(kept_variable))
    result = [*first, *kept_variable, *tail]
    return [copy.deepcopy(item) for item in result], dropped


def merge_context_manager_diagnostics(diagnostics: dict[str, Any], context_manager: dict[str, Any]) -> dict[str, Any]:
    result = dict(diagnostics)
    # Hoist the Context Engine block to the top level so callers / traces can
    # read ``diagnostics["contextEngine"]`` without reaching into contextManager.
    context_engine_block = context_manager.pop("contextEngine", None)
    result["contextManager"] = context_manager
    if context_engine_block is not None:
        result["contextEngine"] = context_engine_block
    if context_manager.get("requestMessageCount") is not None:
        result["requestMessageCount"] = context_manager["requestMessageCount"]
    return result

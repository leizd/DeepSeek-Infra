"""Prompt-cache-aware Context Engine.

This module is the formal home for the context-engineering primitives the
gateway already relies on, plus the genuinely new pieces for 2.0.3:

- **Token Budget Planner** — a deterministic, tokenizer-free estimate of how
  many prompt tokens a request body will cost, broken down by stable system
  prefix / tool schema / history / trailing dynamic context, compared against
  the per-model context window.
- **Model context-window adaptation** — :func:`context_window_for_model` maps a
  normalized model name to its window (DeepSeek pro/flash, with a default for
  edge / Ollama / unknown models), so trimming and budgeting adapt per model.
- **Token-aware trimming** — :func:`token_trim` drops extra *oldest* history
  when the estimate overflows the budget, layered *on top of* the existing
  message-count sliding window. It never reorders or rewrites the
  cache-anchored stable prefix, and it preserves the leading system message and
  the trailing dynamic-context system message.
- **Context Diff** — :func:`build_context_diff` emits a stable ``baseContextId``
  (a hash of the cache-anchored prefix) plus a per-turn ``delta`` describing
  what this turn layered on top, for cache-hygiene observability.

Everything here is pure (no network, no I/O). The engine *plans and measures*;
the byte-for-byte prompt prefix that DeepSeek prompt cache matches on is owned
by ``deepseek_client`` / ``context_manager`` and is intentionally left
untouched. Runtime knobs are imported as module attributes so tests can
monkeypatch them (mirroring the rest of the gateway).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

from deepseek_infra.core.config import (
    CONTEXT_ENGINE_COMPRESS_THRESHOLD_PCT,
    CONTEXT_ENGINE_DEFAULT_CONTEXT_WINDOW,
    CONTEXT_ENGINE_ENABLED,
    CONTEXT_ENGINE_MIN_KEEP_MESSAGES,
    CONTEXT_ENGINE_MODEL_CONTEXT_WINDOWS,
    CONTEXT_ENGINE_RESERVE_OUTPUT_TOKENS,
    CONTEXT_ENGINE_SAFETY_MARGIN_RATIO,
)

# Tokenizer-free heuristics. DeepSeek's BPE packs CJK denser per char than
# Latin text, so we weight the two character classes separately rather than
# using a single chars/token ratio. These are deliberately conservative (they
# round up) so the planner errs toward "leave headroom", never toward
# under-counting a prompt that would actually overflow.
CJK_CHARS_PER_TOKEN = 1.6
LATIN_CHARS_PER_TOKEN = 4.0
# Per-message structural overhead (role, delimiters) in the chat-completions
# wire format, plus a flat cost for an inline image part.
MESSAGE_OVERHEAD_TOKENS = 4
IMAGE_TOKENS = 1_024


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF  # CJK Unified Ideographs
        or 0x3400 <= code <= 0x4DBF  # CJK Extension A
        or 0x3040 <= code <= 0x30FF  # Hiragana + Katakana
        or 0xAC00 <= code <= 0xD7A3  # Hangul syllables
        or 0xF900 <= code <= 0xFAFF  # CJK Compatibility Ideographs
        or 0xFF00 <= code <= 0xFFEF  # Fullwidth forms
    )


def estimate_tokens(text: str) -> int:
    """Estimate the token count of a plain string (deterministic, no tokenizer)."""
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    other = len(text) - cjk
    return int(math.ceil(cjk / CJK_CHARS_PER_TOKEN + other / LATIN_CHARS_PER_TOKEN))


def estimate_message_tokens(message: Any) -> int:
    """Estimate tokens for one chat message (str or multimodal-parts content)."""
    if not isinstance(message, dict):
        return 0
    total = MESSAGE_OVERHEAD_TOKENS
    content = message.get("content")
    if isinstance(content, str):
        total += estimate_tokens(content)
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                total += estimate_tokens(str(part.get("text") or ""))
            elif part.get("type") == "image_url":
                total += IMAGE_TOKENS
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if isinstance(function, dict):
                total += estimate_tokens(str(function.get("name") or ""))
                total += estimate_tokens(str(function.get("arguments") or ""))
    return total


def estimate_tools_tokens(tools: Any) -> int:
    """Estimate tokens for the serialized tool-schema array."""
    if not isinstance(tools, list) or not tools:
        return 0
    try:
        serialized = json.dumps(tools, ensure_ascii=False)
    except (TypeError, ValueError):
        return 0
    return estimate_tokens(serialized)


def estimate_body_breakdown(body: dict[str, Any]) -> dict[str, int]:
    """Break a request body's estimated prompt tokens into engine categories.

    - ``system``: the leading (and any mid-sequence) stable system message.
    - ``dynamic``: the trailing dynamic-context system message, if present.
    - ``history``: every user / assistant / tool message.
    - ``tools``: the serialized tool-schema array.
    """
    messages = body.get("messages")
    messages = messages if isinstance(messages, list) else []
    last_index = len(messages) - 1
    system_tokens = 0
    dynamic_tokens = 0
    history_tokens = 0
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        tokens = estimate_message_tokens(message)
        if message.get("role") == "system":
            if index == last_index and last_index > 0:
                dynamic_tokens += tokens
            else:
                system_tokens += tokens
        else:
            history_tokens += tokens
    return {
        "system": system_tokens,
        "tools": estimate_tools_tokens(body.get("tools")),
        "history": history_tokens,
        "dynamic": dynamic_tokens,
    }


def context_window_for_model(model: str | None) -> int:
    """Per-model context window in tokens, with a default for unknown models."""
    name = str(model or "").strip()
    window = CONTEXT_ENGINE_MODEL_CONTEXT_WINDOWS.get(name)
    if isinstance(window, int) and window > 0:
        return window
    return CONTEXT_ENGINE_DEFAULT_CONTEXT_WINDOW


def available_input_tokens(model: str | None) -> int:
    """Tokens left for the prompt after reserving output + safety margin."""
    window = context_window_for_model(model)
    margin = int(window * max(0.0, CONTEXT_ENGINE_SAFETY_MARGIN_RATIO))
    return max(0, window - CONTEXT_ENGINE_RESERVE_OUTPUT_TOKENS - margin)


@dataclass(frozen=True)
class TokenBudgetPlan:
    model: str
    context_window: int
    reserved_output_tokens: int
    available_input_tokens: int
    estimated_prompt_tokens: int
    breakdown: dict[str, int] = field(default_factory=dict)
    headroom_tokens: int = 0
    utilization_pct: float = 0.0
    within_budget: bool = True
    recommendation: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "contextWindow": self.context_window,
            "reservedOutputTokens": self.reserved_output_tokens,
            "availableInputTokens": self.available_input_tokens,
            "estimatedPromptTokens": self.estimated_prompt_tokens,
            "breakdown": dict(self.breakdown),
            "headroomTokens": self.headroom_tokens,
            "utilizationPct": self.utilization_pct,
            "withinBudget": self.within_budget,
            "recommendation": self.recommendation,
        }


def plan_token_budget(body: dict[str, Any], *, model: str | None = None) -> TokenBudgetPlan:
    """Plan the prompt token budget for a (already-assembled) request body."""
    resolved_model = str(model or body.get("model") or "").strip()
    breakdown = estimate_body_breakdown(body)
    prompt_tokens = sum(breakdown.values())
    window = context_window_for_model(resolved_model)
    available = available_input_tokens(resolved_model)
    headroom = available - prompt_tokens
    utilization = round(prompt_tokens / available * 100, 1) if available > 0 else 100.0
    within_budget = prompt_tokens <= available
    if not within_budget:
        recommendation = "trim"
    elif utilization >= CONTEXT_ENGINE_COMPRESS_THRESHOLD_PCT:
        recommendation = "compress"
    else:
        recommendation = "ok"
    return TokenBudgetPlan(
        model=resolved_model,
        context_window=window,
        reserved_output_tokens=CONTEXT_ENGINE_RESERVE_OUTPUT_TOKENS,
        available_input_tokens=available,
        estimated_prompt_tokens=prompt_tokens,
        breakdown=breakdown,
        headroom_tokens=headroom,
        utilization_pct=utilization,
        within_budget=within_budget,
        recommendation=recommendation,
    )


def _split_front_tail(messages: list[Any]) -> tuple[list[Any], list[Any], list[Any]]:
    """Split into (leading system, variable middle, trailing system).

    Mirrors the invariant the cache layer depends on: a single optional leading
    system message (stable prefix) and a single optional trailing system message
    (this turn's dynamic context). Only the variable middle is ever trimmed.
    """
    front: list[Any] = []
    tail: list[Any] = []
    start = 0
    end = len(messages)
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        front = [messages[0]]
        start = 1
    if end > start and isinstance(messages[-1], dict) and messages[-1].get("role") == "system":
        tail = [messages[-1]]
        end -= 1
    return front, messages[start:end], tail


def token_trim(messages: list[Any], *, model: str | None, fixed_overhead_tokens: int = 0) -> tuple[list[Any], int]:
    """Drop extra oldest history when the estimate overflows the token budget.

    Layered on top of the message-count sliding window: callers pass the
    already-count-capped messages, and this drops *more* only when needed. It
    never drops the leading/trailing system messages, and always keeps at least
    ``CONTEXT_ENGINE_MIN_KEEP_MESSAGES`` of the most recent variable messages —
    so for normal-sized turns it is a no-op and returns ``(messages, 0)``.

    ``fixed_overhead_tokens`` is the cost of prompt parts that live in the body
    but not in ``messages`` (the tool-schema array), so the budget check stays
    accurate without trimming reordering anything outside the message list.

    Returns ``(trimmed_messages, extra_dropped_count)``.
    """
    if not isinstance(messages, list) or not messages:
        return list(messages), 0
    available = available_input_tokens(model)
    if available <= 0:
        return list(messages), 0

    front, variable, tail = _split_front_tail(messages)
    fixed_tokens = max(0, int(fixed_overhead_tokens))
    fixed_tokens += sum(estimate_message_tokens(item) for item in (*front, *tail))
    variable_tokens = [estimate_message_tokens(item) for item in variable]

    min_keep = max(1, CONTEXT_ENGINE_MIN_KEEP_MESSAGES)
    drop = 0
    total = fixed_tokens + sum(variable_tokens)
    while total > available and (len(variable) - drop) > min_keep:
        total -= variable_tokens[drop]
        drop += 1

    if drop <= 0:
        return list(messages), 0
    kept_variable = variable[drop:]
    return [*front, *kept_variable, *tail], drop


def base_context_id(body: dict[str, Any]) -> str:
    """Stable id for the cache-anchored prefix (leading system + tool names).

    Constant across turns as long as the stable prefix is unchanged, so a
    diff of this value over a conversation reveals accidental prefix churn —
    the #1 cause of prompt-cache misses.
    """
    messages = body.get("messages")
    messages = messages if isinstance(messages, list) else []
    system_prefix = ""
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        system_prefix = str(messages[0].get("content") or "")
    tool_names: list[str] = []
    tools = body.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            function = tool.get("function")
            if isinstance(function, dict) and function.get("name"):
                tool_names.append(str(function["name"]))
    digest = hashlib.sha1(
        " ".join([system_prefix, str(body.get("model") or ""), *tool_names]).encode("utf-8")
    ).hexdigest()
    return f"ce_{digest[:12]}"


def build_context_diff(body: dict[str, Any], *, dropped: int = 0) -> dict[str, Any]:
    """Per-turn context composition relative to the stable ``baseContextId``."""
    messages = body.get("messages")
    messages = messages if isinstance(messages, list) else []
    history_count = sum(1 for item in messages if isinstance(item, dict) and item.get("role") in {"user", "assistant"})
    delta: list[dict[str, Any]] = [{"type": "history", "messages": history_count}]

    if len(messages) > 1 and isinstance(messages[-1], dict) and messages[-1].get("role") == "system":
        delta.append({"type": "dynamic_context", "chars": len(str(messages[-1].get("content") or ""))})

    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        delta.append({"type": "tools", "count": len(tools)})

    if dropped:
        delta.append({"type": "trim", "droppedMessages": dropped})

    return {"baseContextId": base_context_id(body), "delta": delta}


def build_engine_diagnostics(body: dict[str, Any], *, model: str | None = None, dropped: int = 0) -> dict[str, Any]:
    """Consolidated ``contextEngine`` diagnostics block for a final body."""
    resolved_model = str(model or body.get("model") or "").strip()
    plan = plan_token_budget(body, model=resolved_model)
    return {
        "enabled": True,
        "model": resolved_model,
        "tokenBudget": plan.to_dict(),
        "contextDiff": build_context_diff(body, dropped=dropped),
    }


def context_engine_enabled() -> bool:
    return bool(CONTEXT_ENGINE_ENABLED)

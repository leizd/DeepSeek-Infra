"""Quantify the round-limit cache fix with a faithful prefix-cache simulator.

DeepSeek prompt cache is a deterministic *prefix* cache: a request's
``prompt_cache_hit_tokens`` is the longest block-aligned prefix of its prompt
that matches a previously-processed request; the remainder is a miss. We rebuild
the model-visible prompt as ``[tools] + [messages]`` (functions-first, the
standard OpenAI-compatible templating) from the REAL request bodies the code
produces, then simulate the cache across one tool turn that hits the round limit.

This proves the behavioral *improvement* (not just an invariant):
``force_final_answer_without_tools`` keeping the ~13 KB ``tools`` block instead of
deleting it lets the largest (round-limit) request reuse that prefix instead of
reprocessing it — raising the turn's cache-hit rate substantially. Even if a
backend templated tools *after* messages, keeping them can never be worse than
dropping them, so the fix is strictly cache-positive.
"""

from __future__ import annotations

import json
import unittest

from deepseek_infra.infra.gateway import deepseek_client
from deepseek_infra.infra.gateway.deepseek_client import (
    append_tool_exchange,
    build_deepseek_request,
    force_final_answer_without_tools,
)

BLOCK = 16  # block-aligned prefix matching, like real KV-cache blocks


def _old_force_final(body: dict) -> dict:
    """Pre-fix behavior: drop the whole tools array at the round limit."""
    messages = list(body.get("messages") or [])
    messages.append({"role": "user", "content": deepseek_client.TOOL_BUDGET_EXHAUSTED_PROMPT})
    next_body = {key: value for key, value in body.items() if key != "tools"}
    next_body["messages"] = messages
    return next_body


def _model_prompt(body: dict) -> str:
    parts = []
    if body.get("tools"):
        parts.append("<TOOLS>" + json.dumps(body["tools"], ensure_ascii=False, sort_keys=True))
    for msg in body.get("messages", []):
        seg = f"<{msg.get('role')}>" + str(msg.get("content") or "")
        if msg.get("tool_calls"):
            seg += json.dumps(msg["tool_calls"], ensure_ascii=False, sort_keys=True)
        if msg.get("reasoning_content"):
            seg += "<rc>" + str(msg["reasoning_content"])
        parts.append(seg)
    return "\n".join(parts)


def _common_prefix_blocks(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    n = 0
    while n < limit and a[n] == b[n]:
        n += 1
    return (n // BLOCK) * BLOCK


def _build_turn(force_final_fn) -> list[dict]:
    payload = {
        "apiKey": "k",
        "model": "expert",
        "systemPrompt": "你是一个有帮助、严谨的助理。" * 4,
        "messages": [{"role": "user", "content": "先查最新资料，再算一个阶乘，然后给结论。"}],
    }
    body = build_deepseek_request(payload, stream=False).body
    reasoning = "需要先确认事实并验证数值。" * 30
    tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "python_eval", "arguments": '{"expression":"factorial(20)"}'}}]
    bodies = [body]
    for _ in range(2):  # two tool rounds, then the limit is hit
        body = append_tool_exchange(body, {"content": "", "reasoning_content": reasoning}, tool_calls)
        bodies.append(body)
    bodies.append(force_final_fn(body))
    return bodies


def _turn_hit_rate(force_final_fn) -> tuple[float, float]:
    """Return (aggregate hit-rate %, forced-request reuse fraction)."""
    prompts = [_model_prompt(b) for b in _build_turn(force_final_fn)]
    cache: list[str] = []
    hit_total = 0
    total = 0
    for prompt in prompts:
        best = max((_common_prefix_blocks(prompt, c) for c in cache), default=0)
        hit_total += best
        total += len(prompt)
        cache.append(prompt)
    rate = 100.0 * hit_total / total if total else 0.0
    forced = prompts[-1]
    forced_reuse = max((_common_prefix_blocks(forced, c) for c in prompts[:-1]), default=0) / max(1, len(forced))
    return rate, forced_reuse


class ToolCacheSimulationTests(unittest.TestCase):
    def test_round_limit_fix_raises_prefix_cache_hit_rate(self) -> None:
        old_rate, _ = _turn_hit_rate(_old_force_final)
        new_rate, _ = _turn_hit_rate(force_final_answer_without_tools)
        # 保留 tools 前缀后，达上限工具回合的整轮命中率明显高于删 tools 的旧实现。
        self.assertGreater(new_rate, old_rate + 8.0)

    def test_forced_request_reuses_tools_prefix(self) -> None:
        _, old_reuse = _turn_hit_rate(_old_force_final)
        _, new_reuse = _turn_hit_rate(force_final_answer_without_tools)
        # 旧实现：收尾请求几乎全 miss（tools 被删，前缀对不上）；新实现：几乎全命中。
        self.assertLess(old_reuse, 0.1)
        self.assertGreater(new_reuse, 0.9)


if __name__ == "__main__":
    unittest.main()

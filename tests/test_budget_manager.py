from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import deepseek_infra.infra.gateway.budget_manager as budget_manager
from deepseek_infra.infra.gateway.deepseek_client import TokenBudget, build_deepseek_request, call_deepseek


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _response_bytes(usage: dict[str, int]) -> bytes:
    return json.dumps(
        {"id": "r", "model": "deepseek-v4-pro", "choices": [{"message": {"content": "an answer"}}], "usage": usage}
    ).encode("utf-8")


def test_estimate_cost_by_model_pricing() -> None:
    assert budget_manager.estimate_cost(1_000_000, 1_000_000, "deepseek-v4-pro") == 2.74
    assert budget_manager.estimate_cost(1_000_000, 0, "deepseek-v4-flash") == 0.27
    assert budget_manager.estimate_cost(1_000_000, 1_000_000, "edge-local") == 0.0
    assert budget_manager.cost_from_usage({"prompt_tokens": 500_000, "completion_tokens": 0}, "deepseek-v4-pro") == 0.275


def test_budget_policy_from_payload_parses_block() -> None:
    policy = budget_manager.budget_policy_from_payload(
        {
            "budget": {"max_total_tokens": 50000, "max_search_calls": 8, "max_tool_calls": 12, "max_estimated_cost_usd": 0.2},
            "budgetPolicy": "downgrade_to_flash_when_exceeded",
        }
    )
    assert policy.max_total_tokens == 50000
    assert policy.max_search_calls == 8
    assert policy.max_estimated_cost_usd == 0.2
    assert policy.downgrade is True


def test_tool_budget_enforces_limit() -> None:
    budget = budget_manager.ToolBudget(total_limit=2)
    assert budget.try_consume("a") is True
    assert budget.try_consume("a") is True
    assert budget.try_consume("b") is False
    assert budget.used == 2
    assert budget.used_by_key == {"a": 2}
    assert budget_manager.ToolBudget(total_limit=0).try_consume() is True  # 0 = unlimited


def test_token_budget_tracks_per_agent_and_exhaustion() -> None:
    budget = TokenBudget(total_limit=100, per_agent_limit=10)
    budget.record(5, "coder")
    budget.record(6, "coder")
    budget.record(3, "critic")
    assert budget.used == 14
    assert budget.used_by_key == {"coder": 11, "critic": 3}
    assert budget.agent_exhausted("coder") is True
    assert budget.agent_exhausted("critic") is False


def test_record_and_read_daily_spend(tmp_settings: Any) -> None:
    budget_manager.record_spend("project:web", prompt_tokens=1000, completion_tokens=500, cost_usd=0.01, model_calls=1, tool_calls=2)
    budget_manager.record_spend("project:web", prompt_tokens=2000, completion_tokens=0, cost_usd=0.02, model_calls=1, search_calls=3)
    spend = budget_manager.daily_spend("project:web")
    assert spend["promptTokens"] == 3000
    assert spend["totalTokens"] == 3500
    assert spend["costUsd"] == 0.03
    assert spend["modelCalls"] == 2
    assert spend["toolCalls"] == 2
    assert spend["searchCalls"] == 3
    # A different scope is isolated.
    assert budget_manager.daily_spend("global")["totalTokens"] == 0


def test_over_daily_budget_and_should_downgrade(tmp_settings: Any) -> None:
    budget_manager.record_spend("project:web", prompt_tokens=40000, completion_tokens=20000, cost_usd=0.3)
    policy = budget_manager.budget_policy_from_payload(
        {"budget": {"max_total_tokens": 50000, "max_estimated_cost_usd": 0.2}, "budgetPolicy": "downgrade_to_flash_when_exceeded"}
    )
    status = budget_manager.over_daily_budget("project:web", policy)
    assert status["exceeded"] is True
    assert set(status["reasons"]) == {"max_total_tokens", "max_estimated_cost_usd"}
    assert budget_manager.should_downgrade("project:web", policy) is True
    # A run with no downgrade policy never downgrades.
    none_policy = budget_manager.budget_policy_from_payload({"budget": {"max_total_tokens": 50000}})
    assert budget_manager.should_downgrade("project:web", none_policy) is False


def test_build_request_downgrades_model_when_over_budget(tmp_settings: Any) -> None:
    budget_manager.record_spend("project:web", prompt_tokens=60000, completion_tokens=0, cost_usd=0.0)
    prepared = build_deepseek_request(
        {
            "apiKey": "k",
            "model": "deepseek-v4-pro",
            "memoryScope": "project:web",
            "budget": {"max_total_tokens": 50000},
            "budgetPolicy": "downgrade_to_flash_when_exceeded",
            "messages": [{"role": "user", "content": "hi"}],
        },
        stream=False,
    )
    assert prepared.body["model"] == "deepseek-v4-flash"
    assert prepared.diagnostics["budgetDowngraded"] is True


def test_call_deepseek_records_cost_and_spend(tmp_settings: Any) -> None:
    payload = {
        "apiKey": "k",
        "model": "deepseek-v4-pro",
        "memoryScope": "project:billing",
        "toolsEnabled": False,
        "semanticCacheEnabled": False,
        "messages": [{"role": "user", "content": "estimate the cost please"}],
    }
    with patch("urllib.request.urlopen", return_value=FakeResponse(_response_bytes({"prompt_tokens": 1_000_000, "completion_tokens": 0}))):
        result = call_deepseek(payload)
    assert result["diagnostics"]["costUsd"] == 0.55  # 1M input tokens at pro pricing
    spend = budget_manager.daily_spend("project:billing")
    assert spend["promptTokens"] == 1_000_000
    assert spend["costUsd"] == 0.55
    assert spend["modelCalls"] == 1


def test_budget_status_shape(tmp_settings: Any) -> None:
    status = budget_manager.budget_status("global")
    assert status["enabled"] is True
    assert "deepseek-v4-pro" in status["pricing"]
    assert status["pricing"]["deepseek-v4-pro"]["inputPerMTok"] == 0.55
    assert status["today"]["totalTokens"] == 0
    assert status["overBudget"]["exceeded"] is False

"""Targeted test coverage boosters for workspace projects and budget manager."""

from __future__ import annotations

from pathlib import Path

from deepseek_infra.infra.data import projects as legacy_projects
from deepseek_infra.infra.gateway import budget_manager
from deepseek_infra.infra.workspace import projects


def test_projects_lifecycle_and_conversations(tmp_settings: Path) -> None:
    # 1. Create project
    created = legacy_projects.create_project("Test Projects Lifecycle")
    p_id = str(created["id"])

    # 2. Get project
    proj = projects.get_project(p_id)
    assert proj["projectId"] == p_id
    assert proj["name"] == "Test Projects Lifecycle"

    # 3. List projects
    projs = projects.list_projects()
    assert any(p["id"] == p_id for p in projs)

    # 4. Upsert project conversation
    conv = projects.upsert_project_conversation(
        p_id,
        {
            "conversationId": "conv_123",
            "title": "Chat 1",
            "messages": [
                {"role": "user", "content": "Hello", "reasoning": ""},
                {"role": "assistant", "content": "Hi there!", "reasoning": "thought"},
            ],
        },
    )
    assert conv["conversationId"] == "conv_123"
    assert conv["messageCount"] == 2

    # 5. List conversations
    convs = projects.list_project_conversations(p_id)
    assert len(convs) == 1
    assert convs[0]["conversationId"] == "conv_123"

    # 6. Touch project
    projects.touch_project(p_id)

    # 7. Delete project
    deleted = projects.delete_project(p_id)
    assert deleted >= 1


def test_budget_manager_pure_helpers_and_spend(tmp_settings: Path) -> None:
    # 1. Pricing and cost estimation
    inp, out = budget_manager.model_pricing("deepseek-chat")
    assert inp >= 0.0 and out >= 0.0
    cost = budget_manager.estimate_cost(1000, 2000, "deepseek-chat")
    assert cost >= 0.0

    # Unknown model is free
    assert budget_manager.model_pricing("unknown-nonexistent-model") == (0.0, 0.0)
    assert budget_manager.estimate_cost(1000, 2000, "unknown-nonexistent-model") == 0.0

    # 2. Set last error
    budget_manager.set_last_error("Test budget warning")
    budget_manager.set_last_error("")

    # 3. ToolBudget class
    tb = budget_manager.ToolBudget(total_limit=2)
    assert tb.try_consume("exec") is True
    assert tb.try_consume("exec") is True
    assert tb.try_consume("exec") is False

    # 4. Record spend & get daily spend
    budget_manager.record_spend("proj_test", prompt_tokens=5000, completion_tokens=1000, cost_usd=0.005)
    spend = budget_manager.daily_spend("proj_test")
    assert spend["promptTokens"] >= 5000
    assert spend["costUsd"] >= 0.005

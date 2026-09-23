from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from deepseek_infra.infra.mcp import permissions as mcp_permissions
from deepseek_infra.infra.skills import permissions as skills_permissions


# =========================================================================
# MCP Permissions Coverage
# =========================================================================

def test_mcp_hub_capability_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_permissions, "MCP_CAPABILITY", "unknown_profile_xyz")
    assert mcp_permissions.hub_capability() == "full"

    monkeypatch.setattr(mcp_permissions, "MCP_CAPABILITY", "researcher")
    assert mcp_permissions.hub_capability() == "researcher"

    monkeypatch.setattr(mcp_permissions, "MCP_CAPABILITY", "")
    assert mcp_permissions.hub_capability() == "full"


def test_mcp_allowed_tool_names_full_and_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_permissions, "MCP_CAPABILITY", "full")

    # Path 1: Normal refresh with bridged profiles
    fake_profile = MagicMock()
    fake_profile.bridged_name = "bridged_custom_tool"
    fake_registry = MagicMock()
    fake_registry.list_profiles.return_value = [fake_profile]

    with patch("deepseek_infra.infra.mcp.bridge.external_mcp_registry", fake_registry):
        names = mcp_permissions.allowed_tool_names()
        assert "bridged_custom_tool" in names

    # Path 2: Exception during refresh is swallowed
    failing_registry = MagicMock()
    failing_registry.refresh.side_effect = RuntimeError("Bridge refresh failed")

    with patch("deepseek_infra.infra.mcp.bridge.external_mcp_registry", failing_registry):
        names = mcp_permissions.allowed_tool_names()
        assert isinstance(names, list)
        assert "bridged_custom_tool" not in names

    # Path 3: Restricted capability
    monkeypatch.setattr(mcp_permissions, "MCP_CAPABILITY", "browser_reader")
    browser_names = mcp_permissions.allowed_tool_names()
    assert isinstance(browser_names, list)


def test_mcp_connection_policy() -> None:
    policy = mcp_permissions.connection_policy({"tool_a", "tool_b"})
    assert policy.scope == "mcp"
    assert "tool_a" in policy.approvals


def test_mcp_approvals_from_meta() -> None:
    # Non-dict meta
    assert mcp_permissions.approvals_from_meta(None) == set()
    assert mcp_permissions.approvals_from_meta(["list"]) == set()

    # Dict without list approvedTools
    assert mcp_permissions.approvals_from_meta({}) == set()
    assert mcp_permissions.approvals_from_meta({"approvedTools": "not_a_list"}) == set()
    assert mcp_permissions.approvals_from_meta({"approvedTools": 123}) == set()

    # Valid approvedTools with whitespace and empty items
    meta = {"approvedTools": ["  tool_1  ", "", None, "tool_2"]}
    assert mcp_permissions.approvals_from_meta(meta) == {"tool_1", "tool_2"}


# =========================================================================
# Skills Permissions Coverage
# =========================================================================

def test_skills_allowed_tools_filtering() -> None:
    all_tools = [
        "browser_click",
        "browser_type_text",
        "browser_select",
        "browser_download",
        "web_search",
    ]

    # Case 1: No policy -> all tools returned
    skill_no_policy = {"allowedTools": all_tools}
    assert set(skills_permissions.skill_allowed_tools(skill_no_policy)) == set(all_tools)

    # Case 2: Empty tools
    assert skills_permissions.skill_allowed_tools({"allowedTools": []}) == []

    # Case 3: Filter all browser tools
    skill_deny_all = {
        "allowedTools": all_tools,
        "browserPolicy": {
            "allowClick": False,
            "allowType": False,
            "allowDownload": False,
        },
    }
    filtered = skills_permissions.skill_allowed_tools(skill_deny_all)
    assert "browser_click" not in filtered
    assert "browser_type_text" not in filtered
    assert "browser_select" not in filtered
    assert "browser_download" not in filtered
    assert "web_search" in filtered

    # Case 4: Allow only click
    skill_click_only = {
        "allowedTools": all_tools,
        "browserPolicy": {
            "allowClick": True,
            "allowType": False,
            "allowDownload": False,
        },
    }
    filtered_click = skills_permissions.skill_allowed_tools(skill_click_only)
    assert "browser_click" in filtered_click
    assert "browser_type_text" not in filtered_click
    assert "browser_select" not in filtered_click
    assert "browser_download" not in filtered_click

    # Case 5: Allow type only
    skill_type_only = {
        "allowedTools": all_tools,
        "browserPolicy": {
            "allowClick": False,
            "allowType": True,
            "allowDownload": False,
        },
    }
    filtered_type = skills_permissions.skill_allowed_tools(skill_type_only)
    assert "browser_click" not in filtered_type
    assert "browser_type_text" in filtered_type
    assert "browser_select" in filtered_type
    assert "browser_download" not in filtered_type

    # Case 6: Allow download only
    skill_download_only = {
        "allowedTools": all_tools,
        "browserPolicy": {
            "allowClick": False,
            "allowType": False,
            "allowDownload": True,
        },
    }
    filtered_download = skills_permissions.skill_allowed_tools(skill_download_only)
    assert "browser_click" not in filtered_download
    assert "browser_type_text" not in filtered_download
    assert "browser_select" not in filtered_download
    assert "browser_download" in filtered_download

    # Case 7: Allow all browser tools
    skill_allow_all = {
        "allowedTools": all_tools,
        "browserPolicy": {
            "allowClick": True,
            "allowType": True,
            "allowDownload": True,
        },
    }
    filtered_all = skills_permissions.skill_allowed_tools(skill_allow_all)
    assert set(filtered_all) == set(all_tools)


def test_skills_build_policy_and_evaluate() -> None:
    # With project_id
    policy_proj = skills_permissions.build_skill_tool_policy(
        {"allowedTools": ["web_search"]},
        project_id="proj_123",
        approvals={"web_search"},
        enforce_schema=True,
    )
    assert policy_proj.scope == "project:proj_123"
    assert "web_search" in policy_proj.approvals

    # Without project_id, with skillId
    policy_skill = skills_permissions.build_skill_tool_policy(
        {"skillId": "skill_abc", "allowedTools": ["web_search"]},
    )
    assert policy_skill.scope == "skill:skill_abc"

    # Without project_id, without skillId
    policy_unknown = skills_permissions.build_skill_tool_policy(
        {"allowedTools": ["web_search"]},
    )
    assert policy_unknown.scope == "skill:unknown"

    # evaluate_skill_tool
    decision = skills_permissions.evaluate_skill_tool(
        {"allowedTools": ["web_search"]},
        "web_search",
        {"query": "deepseek"},
    )
    assert decision.allowed is True

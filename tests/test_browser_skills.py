from __future__ import annotations

from pathlib import Path

from deepseek_infra.infra.skills import registry
from deepseek_infra.infra.skills.permissions import skill_allowed_tools
from deepseek_infra.infra.tool_runtime.tool_policy import TOOL_METADATA, all_tool_names
from deepseek_infra.infra.tool_runtime.tools import available_tool_definitions


def test_browser_tools_are_registered_for_policy_and_tool_catalog() -> None:
    names = {definition["function"]["name"] for definition in available_tool_definitions()}

    for tool in (
        "browser.open_url",
        "browser.read_page",
        "browser.screenshot",
        "browser.click",
        "browser.type_text",
        "browser.download",
        "browser.close_session",
    ):
        assert tool in names
        assert tool in all_tool_names()
        assert TOOL_METADATA[tool].capability == "browser"


def test_builtin_browser_skills_load_with_fine_grained_browser_policy(tmp_settings: Path) -> None:
    skills = {skill["skillId"]: skill for skill in registry.list_builtin_skills()}

    for skill_id in (
        "web_researcher",
        "webpage_reader",
        "website_summarizer",
        "form_assistant",
        "download_and_summarize",
        "browser_to_report",
    ):
        assert skill_id in skills
        assert skills[skill_id]["browserPolicy"]["requireConfirmation"] is True

    assert "browser.click" not in skill_allowed_tools(skills["webpage_reader"])
    assert "browser.type_text" not in skill_allowed_tools(skills["web_researcher"])
    assert "browser.click" in skill_allowed_tools(skills["form_assistant"])
    assert "browser.download" in skill_allowed_tools(skills["download_and_summarize"])

"""Targeted test coverage boosters for home, title generator, and workspace helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.data import projects as legacy_projects
from deepseek_infra.infra.gateway import title_generator
from deepseek_infra.infra.workspace import home


def test_title_generator_helpers(tmp_settings: Path) -> None:
    # 1. Sanitize title
    assert title_generator._sanitize_title("Title: Python Performance!") == "Python Performance"
    assert title_generator._sanitize_title("Title: My Document?") == "My Document"
    assert title_generator._sanitize_title("   ") == ""

    # 2. Truncate
    assert title_generator._truncate("Short", 10) == "Short"
    assert title_generator._truncate("Very long sentence that needs truncating", 10) == "Very long ..."

    # 3. Payload with empty user message
    res = title_generator.generate_title_payload({"apiKey": "test-key", "userMessage": "   "})
    assert res == {"title": ""}

    # 4. Missing API key
    with pytest.raises(AppError):
        title_generator.generate_title_payload({"apiKey": "", "userMessage": "hello"})

    # 5. Rate limit
    for _ in range(12):
        title_generator.check_title_rate_limit("rate-test-key")
    with pytest.raises(AppError):
        title_generator.check_title_rate_limit("rate-test-key")


def test_workspace_home_aggregation(tmp_settings: Path) -> None:
    # 1. Create a project to give workspace_home some content
    legacy_projects.create_project("Home Workspace Project")

    # 2. Aggregate workspace home
    home_data = home.workspace_home(limit=5)
    assert home_data["ok"] is True
    assert "modules" in home_data
    assert "recent" in home_data
    assert "counts" in home_data
    assert home_data["counts"]["projects"] >= 1

"""Targeted test coverage boosters for documents, mindmaps, and slides skill."""

from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.tool_runtime import documents, mindmaps, slides_skill


def test_slides_skill_context() -> None:
    ctx = slides_skill.format_slides_skill_context()
    assert "slides" in ctx
    assert "create_pptx" in ctx


def test_mindmaps_generation(tmp_settings: Path) -> None:
    # 1. Successful mindmap creation
    nodes = [
        {
            "label": "Architecture",
            "children": [
                {"label": "Frontend", "children": [{"label": "React + Vite"}]},
                {"label": "Backend", "children": [{"label": "Python + Fast Server"}]},
            ],
        },
        {
            "label": "Operations",
            "children": [
                {"label": "CI/CD", "children": [{"label": "GitHub Actions"}]},
            ],
        },
    ]
    res = mindmaps.create_mindmap("System Overview", nodes, subtitle="High-level architecture")
    assert res["format"] == "svg"
    assert res["nodeCount"] >= 4
    assert "downloadUrl" in res

    # 2. Empty title error
    with pytest.raises(AppError):
        mindmaps.create_mindmap("", nodes)

    # 3. Empty nodes error
    with pytest.raises(AppError):
        mindmaps.create_mindmap("Title", [])


def test_documents_generation(tmp_settings: Path) -> None:
    sections = [
        {
            "heading": "Introduction",
            "body": ["This is the introductory paragraph for testing."],
            "bullets": ["Point 1: Detail A", "Point 2: Detail B"],
            "table": {
                "headers": ["Col 1", "Col 2"],
                "rows": [["Val 1", "Val 2"], ["Val 3", "Val 4"]],
            },
        }
    ]

    # 1. Create DOCX document
    docx_res = documents.create_document("docx", "Test Report", sections, subtitle="Automated test")
    assert "downloadUrl" in docx_res

    # 2. Create PDF document
    pdf_res = documents.create_document("pdf", "Test PDF Report", sections, subtitle="Automated PDF test")
    assert "downloadUrl" in pdf_res

    # 3. Invalid format
    with pytest.raises(AppError):
        documents.create_document("invalid_fmt", "Test", sections)

    # 4. Empty title
    with pytest.raises(AppError):
        documents.create_document("docx", "", sections)

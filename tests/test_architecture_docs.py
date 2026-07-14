"""Documentation and architecture asset tests.

These tests guard the v3.9.0 hybrid-architecture documentation refresh:
- architecture.svg is valid XML and contains the expected version/ownership labels.
- README.md references the SVG and links to ARCHITECTURE.md.
- ARCHITECTURE.md contains a Mermaid diagram and the required ownership statements.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("substring", "should_contain"),
    [
        ("v3.9.0", True),
        ("v2.1.6", False),
        ("Optional Rust Sidecar", True),
        ("Python Default Runtime", True),
        ("Python fallback", True),
        ("POST /gateway/request/prepare", True),
        ("POST /mcp/request/prepare", True),
        ("POST /rag/vectors/rank", True),
        ("POST /rag/vectors/rank-binary", True),
        ("RAG Vector Ranking · JSON / compact binary", True),
        ("POST /rag/documents/prepare", True),
    ],
)
def test_architecture_svg_content(substring: str, should_contain: bool) -> None:
    svg_text = _read(ROOT / "docs" / "assets" / "architecture.svg")
    assert (substring in svg_text) is should_contain, (
        f"Expected {'to find' if should_contain else 'not to find'} {substring!r} in architecture.svg"
    )


def test_architecture_svg_is_valid_xml() -> None:
    ET.parse(ROOT / "docs" / "assets" / "architecture.svg")


def test_readme_references_architecture_svg() -> None:
    readme = _read(ROOT / "README.md")
    assert "docs/assets/architecture.svg" in readme


def test_readme_links_to_architecture_md() -> None:
    readme = _read(ROOT / "README.md")
    assert "docs/ARCHITECTURE.md" in readme


def test_architecture_md_contains_mermaid_diagram() -> None:
    doc = _read(ROOT / "docs" / "ARCHITECTURE.md")
    assert "```mermaid" in doc
    assert "flowchart TB" in doc


def test_architecture_md_states_rust_is_disabled_by_default() -> None:
    doc = _read(ROOT / "docs" / "ARCHITECTURE.md")
    assert "默认禁用" in doc


def test_architecture_md_states_python_ownership() -> None:
    doc = _read(ROOT / "docs" / "ARCHITECTURE.md")
    assert "持久化与工具执行仍由 Python 拥有" in doc


def test_architecture_md_and_svg_are_consistent_on_ownership() -> None:
    svg_text = _read(ROOT / "docs" / "assets" / "architecture.svg").lower()
    doc = _read(ROOT / "docs" / "ARCHITECTURE.md").lower()
    assert "python default runtime" in svg_text
    assert "optional rust sidecar" in svg_text
    assert "python 默认运行时" in doc
    assert "可选 rust sidecar" in doc

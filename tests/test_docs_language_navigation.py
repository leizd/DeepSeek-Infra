from __future__ import annotations

import re
from pathlib import Path

from scripts import update_docs_language_nav as language_nav

ROOT = Path(__file__).resolve().parents[1]


def test_all_managed_markdown_has_current_language_switcher() -> None:
    files = language_nav.iter_managed_markdown()
    assert len(files) >= 60
    assert language_nav.check_all() == []


def test_language_switcher_targets_resolve() -> None:
    pattern = re.compile(r"\[中文\]\(([^)]+)\) / \[English\]\(([^)]+)\)")
    for path in language_nav.iter_managed_markdown():
        match = pattern.search(path.read_text(encoding="utf-8"))
        assert match is not None, path
        for target in match.groups():
            assert (path.parent / target).resolve().is_file(), (path, target)


def test_machine_generated_and_instruction_markdown_are_excluded() -> None:
    relative = {path.relative_to(ROOT).as_posix() for path in language_nav.iter_managed_markdown()}
    assert "AGENTS.md" not in relative
    assert not any(path.startswith("docs/evidence/") for path in relative)
    assert not any(path.startswith("evals/reports/") for path in relative)


def test_readme_roadmap_is_a_standalone_document() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("\n## Roadmap\n")
    end = readme.index("\n## 文档\n", start)
    inline_section = readme[start:end]
    assert "路线图已迁移为独立文档" in inline_section
    assert "### v2.2.0" not in inline_section

    roadmap = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
    assert "### v2.2.0: Visualization & Verification" in roadmap
    assert "### v2.6.3: Custom Skill Builder" in roadmap


def test_english_entry_documents_are_present() -> None:
    readme = (ROOT / "README.en.md").read_text(encoding="utf-8")
    roadmap = (ROOT / "ROADMAP.en.md").read_text(encoding="utf-8")
    assert "## 4.0.8 at a glance" in readme
    assert "## Next frontend slices" in roadmap

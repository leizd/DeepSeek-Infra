from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


def _load_preflight() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "preflight_release.py"
    spec = importlib.util.spec_from_file_location("preflight_release_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _skeleton(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "CHANGELOG.md").write_text("## [2.3.4] - clean\n", encoding="utf-8")
    (root / "README.md").write_text("ok\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "COMPATIBILITY.md").write_text("ok\n", encoding="utf-8")
    (root / "docs" / "IMPLEMENTATION_STATUS.md").write_text("ok\n", encoding="utf-8")
    (root / "docs" / "RELEASE_READINESS.md").write_text("ok\n", encoding="utf-8")
    (root / "docs" / "EVIDENCE_INDEX.md").write_text("ok\n", encoding="utf-8")
    (root / "docs" / "integrations").mkdir()
    (root / "docs" / "integrations" / "headless-mcp-client.md").write_text("ok\n", encoding="utf-8")
    (root / "deepseek_infra" / "core").mkdir(parents=True)
    (root / "deepseek_infra" / "core" / "config.py").write_text("# clean\n", encoding="utf-8")
    return root


def test_docs_encoding_sanity_passes_when_clean(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path)
    result = preflight.check_docs_encoding_sanity(root)
    assert result.status == "pass"


def test_docs_encoding_sanity_fails_on_triple_question_marks(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path)
    (root / "CHANGELOG.md").write_text("## [2.3.3]\n\n**???A2A ?? peer**\n", encoding="utf-8")
    result = preflight.check_docs_encoding_sanity(root)
    assert result.status == "fail"
    assert "CHANGELOG.md" in str(result.detail)


def test_docs_encoding_sanity_fails_on_replacement_character(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path)
    (root / "docs" / "integrations" / "headless-mcp-client.md").write_text("bad \ufffd char\n", encoding="utf-8")
    result = preflight.check_docs_encoding_sanity(root)
    assert result.status == "fail"


def test_docs_encoding_sanity_fails_on_kunjinkao(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path)
    (root / "README.md").write_text("锟斤拷\n", encoding="utf-8")
    result = preflight.check_docs_encoding_sanity(root)
    assert result.status == "fail"


def test_docs_encoding_sanity_ignores_patterns_inside_code_spans(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path)
    (root / "CHANGELOG.md").write_text("Check for `???` and `锟斤拷` and `\ufffd` patterns.\n", encoding="utf-8")
    result = preflight.check_docs_encoding_sanity(root)
    assert result.status == "pass"

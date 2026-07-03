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
    (root / "CHANGELOG.md").write_text("## [2.7.3] - clean\n", encoding="utf-8")
    (root / "Dockerfile").write_text("# clean dockerfile\n", encoding="utf-8")
    (root / "README.md").write_text("clean\n", encoding="utf-8")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "ok.py").write_text("# clean script\n", encoding="utf-8")
    (root / "docs" / "nested").mkdir(parents=True)
    (root / "docs" / "nested" / "guide.md").write_text("clean\n", encoding="utf-8")
    return root


def _assert_fails_on(root: Path, expected_path: str) -> None:
    preflight = _load_preflight()
    result = preflight.check_docs_encoding_sanity(root)
    assert result.status == "fail"
    assert expected_path in str(result.detail)


def test_encoding_scope_includes_dockerfile(tmp_path: Path) -> None:
    root = _skeleton(tmp_path)
    (root / "Dockerfile").write_text("# bad \u93cb\n", encoding="utf-8")

    _assert_fails_on(root, "Dockerfile")


def test_encoding_scope_includes_github_workflows(tmp_path: Path) -> None:
    root = _skeleton(tmp_path)
    (root / ".github" / "workflows" / "ci.yml").write_text("name: ci\n# bad \u9225\n", encoding="utf-8")

    _assert_fails_on(root, ".github/workflows/ci.yml")


def test_encoding_scope_includes_scripts(tmp_path: Path) -> None:
    root = _skeleton(tmp_path)
    (root / "scripts" / "ok.py").write_text("# bad \u6769\n", encoding="utf-8")

    _assert_fails_on(root, "scripts/ok.py")


def test_encoding_scope_includes_recursive_docs(tmp_path: Path) -> None:
    root = _skeleton(tmp_path)
    (root / "docs" / "nested" / "guide.md").write_text("bad \ufffd\n", encoding="utf-8")

    _assert_fails_on(root, "docs/nested/guide.md")


def test_encoding_scope_ignores_markdown_code_examples(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path)
    (root / "docs" / "nested" / "guide.md").write_text("Document the `???` fallback literally.\n", encoding="utf-8")

    result = preflight.check_docs_encoding_sanity(root)

    assert result.status == "pass"
    checked = result.data["checked"]
    assert "Dockerfile" in checked
    assert ".github/workflows/ci.yml" in checked
    assert "scripts/ok.py" in checked
    assert "docs/nested/guide.md" in checked

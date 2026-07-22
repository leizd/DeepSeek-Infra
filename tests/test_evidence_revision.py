from __future__ import annotations

from pathlib import Path

from deepseek_infra.infra.diagnostics import evidence_revision as revision_module


def test_evidence_revision_block_has_honest_semantics(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_git(root: Path, *args: str) -> str:
        calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return "abc1234def"
        if args == ("status", "--porcelain"):
            return " M frontend/src/main.tsx"
        return ""

    monkeypatch.setattr(revision_module, "_git", fake_git)
    monkeypatch.delenv("GITHUB_SHA", raising=False)

    block = revision_module.evidence_revision(tmp_path)
    assert block == {
        "testedRevision": "abc1234def",
        "sourceRevision": "abc1234def",
        "sourceTreeDirty": True,
        "releaseRevision": None,
        "ciRevision": None,
    }
    assert calls == [("rev-parse", "HEAD"), ("status", "--porcelain")]


def test_evidence_revision_reads_github_sha_and_missing_head(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(revision_module, "_git", lambda root, *args: "")
    monkeypatch.setenv("GITHUB_SHA", "deadbeef999")

    block = revision_module.evidence_revision(tmp_path)
    assert block["sourceRevision"] == "unknown"
    assert block["testedRevision"] == "unknown"
    assert block["sourceTreeDirty"] is False
    assert block["ciRevision"] == "deadbeef999"


def test_evidence_revision_present_accepts_new_and_legacy_fields() -> None:
    assert revision_module.evidence_revision_present({"testedRevision": "abc"})
    assert revision_module.evidence_revision_present({"sourceRevision": "abc"})
    assert revision_module.evidence_revision_present({"commit": "abc"})
    assert not revision_module.evidence_revision_present({})
    assert not revision_module.evidence_revision_present({"sourceRevision": "", "commit": ""})


def test_evidence_revision_uses_one_shared_source_context(tmp_path: Path, monkeypatch) -> None:
    context_path = tmp_path / "context.json"
    context_path.write_text(
        """{
  "schemaVersion": 1,
  "version": "4.2.7",
  "testedRevision": "candidate123",
  "sourceTreeDirty": false,
  "capturedAt": "2026-07-22T10:00:00Z",
  "generator": "scripts/generate_release_evidence.py"
}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv(revision_module.EVIDENCE_SOURCE_CONTEXT_ENV, str(context_path))
    monkeypatch.setattr(revision_module, "_git", lambda root, *args: "should-not-be-read")

    first = revision_module.evidence_revision(tmp_path)
    second = revision_module.evidence_revision(tmp_path)
    assert first == second
    assert first["testedRevision"] == "candidate123"
    assert first["sourceRevision"] == "candidate123"
    assert first["sourceTreeDirty"] is False


def test_capture_source_context_rejects_dirty_tree(tmp_path: Path, monkeypatch) -> None:
    def fake_git(root: Path, *args: str) -> str:
        return "candidate123" if args == ("rev-parse", "HEAD") else " M source.py"

    monkeypatch.setattr(revision_module, "_git", fake_git)
    try:
        revision_module.capture_source_context(tmp_path, "4.2.7", generator="test")
    except ValueError as exc:
        assert "clean source tree" in str(exc)
    else:
        raise AssertionError("dirty source tree was accepted")

from __future__ import annotations

from pathlib import Path

import pytest

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
  "version": "4.3.0",
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
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    try:
        revision_module.capture_source_context(tmp_path, "4.3.0", generator="test")
    except ValueError as exc:
        assert "clean source tree" in str(exc)
    else:
        raise AssertionError("dirty source tree was accepted")


def test_capture_schema_v2_context_binds_github_identity(tmp_path: Path, monkeypatch) -> None:
    revision = "a" * 40

    def fake_git(root: Path, *args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return revision
        if args == ("status", "--porcelain"):
            return ""
        if args == ("symbolic-ref", "-q", "HEAD"):
            return "refs/heads/fallback"
        return ""

    monkeypatch.setattr(revision_module, "_git", fake_git)
    monkeypatch.setenv("GITHUB_SHA", revision)
    monkeypatch.setenv("GITHUB_REPOSITORY", "leizd/DeepSeek-Infra")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")

    context = revision_module.capture_source_context(tmp_path, "4.3.0", generator="test")

    assert context == {
        "schemaVersion": 2,
        "version": "4.3.0",
        "testedRevision": revision,
        "sourceTreeDirty": False,
        "capturedAt": context["capturedAt"],
        "generator": "test",
        "repository": "leizd/DeepSeek-Infra",
        "workflowRunId": "12345",
        "workflowAttempt": 2,
        "eventName": "push",
        "ref": "refs/heads/main",
    }
    assert revision_module.validate_source_context(context, version="4.3.0", expected_revision=revision) == []


def test_capture_source_context_rejects_github_checkout_mismatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        revision_module,
        "_git",
        lambda root, *args: "a" * 40 if args == ("rev-parse", "HEAD") else "",
    )
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)

    try:
        revision_module.capture_source_context(tmp_path, "4.3.0", generator="test")
    except ValueError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("mismatched GITHUB_SHA was accepted")


def test_load_source_context_rejects_missing_malformed_and_non_object(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(revision_module.EVIDENCE_SOURCE_CONTEXT_ENV, raising=False)
    assert revision_module.source_context_path() is None
    assert revision_module.load_source_context() is None

    missing = tmp_path / "missing.json"
    monkeypatch.setenv(revision_module.EVIDENCE_SOURCE_CONTEXT_ENV, str(missing))
    assert revision_module.source_context_path() == missing.resolve()
    with pytest.raises(ValueError, match="invalid evidence source context"):
        revision_module.load_source_context()

    malformed = tmp_path / "malformed.json"
    malformed.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match="expected a JSON object"):
        revision_module.load_source_context(malformed)

    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="missing version"):
        revision_module.load_source_context(incomplete)

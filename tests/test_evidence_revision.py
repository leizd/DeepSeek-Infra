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
    assert block["sourceTreeDirty"] is False
    assert block["ciRevision"] == "deadbeef999"


def test_evidence_revision_present_accepts_new_and_legacy_fields() -> None:
    assert revision_module.evidence_revision_present({"sourceRevision": "abc"})
    assert revision_module.evidence_revision_present({"commit": "abc"})
    assert not revision_module.evidence_revision_present({})
    assert not revision_module.evidence_revision_present({"sourceRevision": "", "commit": ""})

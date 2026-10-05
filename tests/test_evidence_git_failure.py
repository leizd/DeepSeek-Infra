from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from deepseek_infra.infra.diagnostics import evidence_revision as revision_module
from deepseek_infra.core.config import APP_VERSION


@pytest.mark.parametrize("failure", ["exit", "timeout", "missing"])
@pytest.mark.parametrize("producer", ["report", "release_context"])
def test_git_status_failure_never_attests_a_clean_tree(tmp_path: Path, monkeypatch, failure: str, producer: str) -> None:
    monkeypatch.delenv(revision_module.EVIDENCE_SOURCE_CONTEXT_ENV, raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)

    def run_git(command, **kwargs):
        if command[-2:] == ["status", "--porcelain"]:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, 15)
            if failure == "missing":
                raise FileNotFoundError("git unavailable")
            return subprocess.CompletedProcess(command, 128, stdout="", stderr="status inaccessible")
        return subprocess.CompletedProcess(command, 0, stdout="a" * 40, stderr="")

    monkeypatch.setattr(revision_module.subprocess, "run", run_git)
    with pytest.raises(ValueError, match="Git status unavailable"):
        if producer == "report":
            revision_module.evidence_revision(tmp_path)
        else:
            revision_module.capture_source_context(tmp_path, APP_VERSION, generator="offline test")

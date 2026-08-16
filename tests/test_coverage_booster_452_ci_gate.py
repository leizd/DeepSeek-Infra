"""Extra branch coverage for the py3.11 95% CI gate."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deepseek_infra.core.config import APP_VERSION
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.diagnostics import evidence_revision as revision_module
from deepseek_infra.infra.media import evidence as media_evidence
from deepseek_infra.infra.memory import policy as mem_policy
from deepseek_infra.infra.observability import export as obs_export
from deepseek_infra.infra.observability import health as obs_health
from deepseek_infra.infra.observability import trace_api as obs_trace_api


def test_memory_policy_scopes_and_read_gates() -> None:
    assert mem_policy.readable_scopes() == ["global"]
    assert mem_policy.readable_scopes(skill_id="sk1") == ["global", "skill:sk1"]
    assert mem_policy.readable_scopes(automation_id="au1") == ["global", "automation:au1"]
    assert mem_policy.readable_scopes(project_id="p1", skill_id="sk1", automation_id="au1") == [
        "global",
        "project:p1",
        "skill:sk1",
        "automation:au1",
    ]
    assert mem_policy.skill_can_read_memory({}, project_id="") is True
    assert mem_policy.skill_can_read_memory({"memoryPolicy": {"read": False}}) is False
    assert mem_policy.skill_can_read_memory({"memoryPolicy": {"scope": "project"}}, project_id="") is False
    assert mem_policy.skill_can_read_memory({"memoryPolicy": {"scope": "project"}}, project_id="p1") is True
    assert mem_policy.skill_can_read_memory({"memoryPolicy": "not-a-dict"}) is True
    assert mem_policy.is_sensitive_memory("api key should be sk-live-secret") is True
    with pytest.raises(AppError):
        mem_policy.assert_memory_safe("api key should be sk-live-secret")


def test_readyz_tracing_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(obs_health, "trace_status", lambda: {"enabled": False})
    data = obs_health.readyz()
    assert data["checks"]["tracing"] == "disabled"
    assert data["status"] == "ready"

    monkeypatch.setattr(obs_health, "trace_status", lambda: {"enabled": True, "lastError": "boom"})
    data = obs_health.readyz()
    assert data["checks"]["tracing"] == "degraded"
    assert data["status"] == "degraded"

    monkeypatch.setattr(obs_health, "trace_status", lambda: {"enabled": True, "lastError": None})
    data = obs_health.readyz()
    assert data["checks"]["tracing"] == "ok"


def test_trace_api_limit_and_not_found(tmp_path: Path) -> None:
    index = tmp_path / "index.html"
    index.write_text("<html></html>", encoding="utf-8")
    app = FastAPI()

    def _auth(_request: Any) -> None:
        return None

    obs_trace_api.register_trace_routes(
        app,
        require_api_auth=_auth,
        frontend_index_path=lambda: index,
        get_trace_fn=lambda _tid: None,
        export_trace_fn=lambda _tid: None,
        list_traces_fn=lambda _limit: [],
        trace_status_fn=lambda: {"enabled": True},
    )
    client = TestClient(app, raise_server_exceptions=True)
    assert client.get("/api/traces?limit=not-int").status_code == 200
    with pytest.raises(AppError):
        client.get("/api/traces/missing")
    with pytest.raises(AppError):
        client.get("/api/traces/missing/export.json")
    with pytest.raises(AppError):
        client.get("/trace/missing")
    assert obs_trace_api.export_filename("a/b!c") == "trace-abc.json"
    assert obs_trace_api.export_filename("") == "trace-export.json"


def test_export_redaction_edge_paths() -> None:
    assert obs_export.export_trace("does-not-exist") is None
    assert obs_export.redact_value(("a", "b"), key="x") == ["a", "b"]
    assert isinstance(obs_export.redact_value(object(), key="note"), str)
    assert obs_export.is_sensitive_key("api_key") is True
    assert obs_export.is_sensitive_key("access_token") is True
    assert obs_export.is_sensitive_key("user_password") is True
    assert obs_export.is_sensitive_key("prompt_tokens") is False
    assert obs_export.redact_trace_for_response({"api_key": "sk-abcdefghi"})["api_key"] == "[redacted]"


def test_evidence_revision_git_errors_and_validate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def os_error_run(*_a: Any, **_k: Any) -> Any:
        raise OSError("git missing")

    monkeypatch.setattr(subprocess, "run", os_error_run)
    assert revision_module._git(tmp_path, "rev-parse", "HEAD") == ""  # noqa: SLF001

    def timeout_run(*_a: Any, **_k: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="git", timeout=15)

    monkeypatch.setattr(subprocess, "run", timeout_run)
    assert revision_module._git(tmp_path, "status", "--porcelain") == ""  # noqa: SLF001

    base = {
        "schemaVersion": 2,
        "version": APP_VERSION,
        "testedRevision": "abc",
        "sourceTreeDirty": False,
        "capturedAt": "2026-08-16T00:00:00Z",
        "generator": "test",
        "repository": "org/repo",
        "workflowRunId": "1",
        "workflowAttempt": 1,
        "eventName": "push",
        "ref": "refs/heads/main",
    }
    assert "version mismatch" in " ".join(
        revision_module.validate_source_context(base, version="9.9.9", expected_revision="abc")
    )
    assert "testedRevision mismatch" in " ".join(
        revision_module.validate_source_context(base, version=APP_VERSION, expected_revision="zzz")
    )
    missing_v2 = {**base, "repository": ""}
    assert any("missing repository" in e for e in revision_module.validate_source_context(missing_v2))

    monkeypatch.setattr(revision_module, "_git", lambda root, *args: "" if args[:1] == ("rev-parse",) else "")
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    with pytest.raises(ValueError, match="known Git HEAD"):
        revision_module.capture_source_context(tmp_path, APP_VERSION, generator="test")

    def clean_git(root: Path, *args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return "c" * 40
        return ""

    monkeypatch.setattr(revision_module, "_git", clean_git)
    ctx = revision_module.capture_source_context(tmp_path, APP_VERSION, generator="test", schema_version=1)
    assert ctx["schemaVersion"] == 1
    assert "repository" not in ctx


def test_media_evidence_prefers_source_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context_path = tmp_path / "ctx.json"
    context_path.write_text(
        (
            "{\n"
            '  "schemaVersion": 1,\n'
            f'  "version": "{APP_VERSION}",\n'
            '  "testedRevision": "context-sha-xyz",\n'
            '  "sourceTreeDirty": false,\n'
            '  "capturedAt": "2026-08-16T00:00:00Z",\n'
            '  "generator": "test"\n'
            "}\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(revision_module.EVIDENCE_SOURCE_CONTEXT_ENV, str(context_path))
    assert media_evidence.git_short_sha() == "context-sha-xyz"
    payload = media_evidence.evidence_metadata(APP_VERSION, status="PASS", checks={"x": "PASS"}, details=None)
    assert payload["commit"] == "context-sha-xyz"
    assert "details" not in payload


def test_capture_source_context_invalid_after_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    revision = "d" * 40

    def fake_git(root: Path, *args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return revision
        return ""

    monkeypatch.setattr(revision_module, "_git", fake_git)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setattr(revision_module, "validate_source_context", lambda *a, **k: ["forced"])
    with pytest.raises(ValueError, match="invalid captured"):
        revision_module.capture_source_context(tmp_path, APP_VERSION, generator="test", schema_version=1)

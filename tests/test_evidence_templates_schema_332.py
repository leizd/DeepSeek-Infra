from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from deepseek_infra.infra.browser import evidence as browser_evidence
from deepseek_infra.infra.media import evidence as media_evidence
from deepseek_infra.infra.memory import evidence as memory_evidence
from deepseek_infra.infra.memory import schema as memory_schema
from deepseek_infra.infra.skills import evidence as skill_evidence
from deepseek_infra.infra.skills import templates


def test_browser_media_and_memory_evidence_status_and_git_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    success = subprocess.CompletedProcess([], 0, "abc123\n", "")
    failure = subprocess.CompletedProcess([], 1, "", "fatal")
    monkeypatch.setattr(media_evidence.subprocess, "run", lambda *_args, **_kwargs: success)
    payload = media_evidence.evidence_metadata("3.3.2", status="PASS", checks={"media": "PASS"}, details=None)
    assert payload["commit"] == "abc123" and "details" not in payload
    assert browser_evidence.browser_evidence_payload("3.3.2", checks={"browser": "FAIL"})["status"] == "FAIL"
    monkeypatch.setattr(media_evidence.subprocess, "run", lambda *_args, **_kwargs: failure)
    assert media_evidence.git_short_sha() == "unknown"

    monkeypatch.setattr(memory_evidence.subprocess, "run", lambda *_args, **_kwargs: failure)
    assert memory_evidence.git_short_sha() == "unknown"
    assert memory_evidence.memory_evidence({"a": "PASS"})["status"] == "PASS"
    assert memory_evidence.memory_evidence({"a": "FAIL"})["status"] == "FAIL"
    assert set(memory_evidence.default_policy_checks()) >= {"sensitiveMemoryPolicy", "globalScope", "projectScope"}


def test_skill_artifact_index_corruption_register_filters_and_queries(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = skill_evidence.artifact_index_path()
    assert skill_evidence.load_artifact_index() == []
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")
    assert skill_evidence.load_artifact_index() == []
    path.write_text("{}", encoding="utf-8")
    assert skill_evidence.load_artifact_index() == []
    path.write_text('[null,{"artifactId":"old","source":{"projectId":"p","skillRunId":"r"}}]', encoding="utf-8")
    assert len(skill_evidence.load_artifact_index()) == 1
    assert skill_evidence.register_generated_artifact({"fileId": "bad"}, skill_id="s", skill_run_id="r") is None

    file_id = "a" * 32
    generated = tmp_settings / ".generated" / "report.md"
    generated.write_text("report", encoding="utf-8")
    monkeypatch.setattr(skill_evidence, "resolve_generated_file", lambda _file_id: generated)
    artifact = skill_evidence.register_generated_artifact(
        {"fileId": file_id, "filename": "report.md"},
        skill_id="s",
        skill_run_id="r",
        project_id="p",
    )
    assert artifact is not None and artifact["type"] == "md"
    assert artifact["artifactId"] in {item["artifactId"] for item in skill_evidence.artifacts_for_project("p")}
    assert artifact["artifactId"] in {item["artifactId"] for item in skill_evidence.artifacts_for_skill_run("r")}
    assert skill_evidence.save_markdown_artifact(title="x", content="", skill_id="s", skill_run_id="r") is None


def test_skill_release_evidence_write_ci_and_git_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_git_commit = skill_evidence.git_commit
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    monkeypatch.setattr(skill_evidence, "git_commit", lambda: "commit")
    passing = skill_evidence.release_evidence_payload(checks={"one": "pass"}, version="3.3.2", details={"x": 1})
    assert passing["status"] == "PASS" and passing["environment"]["ci"] is True
    assert skill_evidence.release_evidence_payload(checks={})["status"] == "FAIL"
    target = skill_evidence.write_release_evidence(tmp_path / "nested" / "evidence.json", passing)
    assert json.loads(target.read_text(encoding="utf-8"))["commit"] == "commit"
    monkeypatch.setattr(skill_evidence, "git_commit", real_git_commit)
    monkeypatch.setattr(skill_evidence.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("git missing")))
    assert skill_evidence.git_commit() == "unknown"
    monkeypatch.setattr(skill_evidence.subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""))
    assert skill_evidence.git_commit() == "unknown"


def test_skill_templates_format_partial_project_and_offline_content() -> None:
    assert templates.format_project_context(None) == ""
    project_context = templates.format_project_context(
        {
            "id": "p",
            "name": "Project",
            "documents": [None, {"name": "doc", "kind": "text", "fileId": "f"}],
            "savedItems": [None, {"title": "saved", "kind": "note"}],
            "skillRuns": [None, {"skillId": "s", "status": "done", "startedAt": "now"}],
        }
    )
    assert "documents:" in project_context and "saved items:" in project_context and "recent Skill runs:" in project_context
    prompt = templates.skill_system_prompt({"systemPrompt": "Do work"}, project_context=project_context)
    assert "Do work" in prompt and project_context in prompt
    assert '"x": 1' in templates.skill_user_message({"x": 1})
    content = templates.offline_skill_content(
        {"name": "Demo", "skillId": "skill_demo"},
        {"question": "Question", "task": "Explain"},
        project_context="context" * 500,
    )
    assert content.startswith("# Question") and "## Request" in content and len(content) < 2300
    fallback = templates.offline_skill_content({"name": "Demo", "skillId": "skill_demo"}, {}, project_context="")
    assert "# Demo" in fallback and "## Request" not in fallback


def test_memory_schema_scope_type_source_and_confidence_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    assert memory_schema.public_scope("project:p") == "project"
    assert memory_schema.public_scope("skill:s") == "skill"
    assert memory_schema.public_scope("automation:a") == "automation"
    assert memory_schema.public_scope("unknown") == "global"
    assert memory_schema.storage_scope("project", project_id="p") == "project:p"
    assert memory_schema.storage_scope("skill", skill_id="s") == "skill:s"
    assert memory_schema.storage_scope("automation", automation_id="a") == "automation:a"
    assert memory_schema.storage_scope("project:p") == "project:p"
    assert memory_schema.public_type("todo") == "instruction"
    assert memory_schema.public_type("unknown") == "fact"
    assert memory_schema.legacy_category("summary") == "fact"
    assert memory_schema.legacy_category("preference") == "preference"
    source = memory_schema.public_source({"type": "unknown", "messageId": "m", "extra": 1})
    assert source == {"kind": "manual", "refId": "m", "messageId": "m", "extra": 1}
    assert memory_schema.public_source("chat", fallback_ref="x") == {"kind": "chat", "refId": "x"}
    assert memory_schema.public_confidence("bad") == 0.9
    assert memory_schema.public_confidence(2) == 1.0
    assert memory_schema.public_confidence(-1) == 0.0
    record = memory_schema.public_memory({"id": "m", "scope": "project:p", "category": "todo", "content": " value ", "pinned": True})
    assert record["memoryId"] == "m" and record["scope"] == "project" and record["type"] == "instruction" and record["pinned"] is True

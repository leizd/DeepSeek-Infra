from __future__ import annotations

from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.skills import runner


def skill(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "skillId": "skill-test",
        "name": "Test Skill",
        "version": "1.0.0",
        "inputSchema": {"type": "object"},
        "outputSchema": {"type": "object", "required": ["content"]},
        "projectBinding": {"enabled": False},
        "memoryPolicy": {},
        "artifactPolicy": {"autoSave": False},
        "allowedTools": [],
    }
    value.update(overrides)
    return value


class Span:
    span_id = "span"

    def __init__(self) -> None:
        self.finished: list[dict[str, Any]] = []

    def finish(self, **kwargs: Any) -> None:
        self.finished.append(kwargs)


def _base_mocks(monkeypatch: pytest.MonkeyPatch, current: dict[str, Any]) -> Span:
    span = Span()
    monkeypatch.setattr(runner.registry, "get_skill", lambda skill_id: current)
    monkeypatch.setattr(runner.security, "security_context_for_skill", lambda value, **kwargs: {"blocked": False})
    monkeypatch.setattr(runner.security, "run_security_metadata", lambda value: {})
    monkeypatch.setattr(runner, "start_trace", lambda **kwargs: "trace")
    monkeypatch.setattr(runner, "start_span", lambda *args, **kwargs: span)
    monkeypatch.setattr(runner, "finish_trace", lambda *args, **kwargs: None)
    return span


def test_security_block_and_non_object_input_record_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    current = skill()
    monkeypatch.setattr(runner.registry, "get_skill", lambda skill_id: current)
    monkeypatch.setattr(runner.security, "security_context_for_skill", lambda value, **kwargs: {"blocked": True, "blockedReason": "approval required"})
    monkeypatch.setattr(runner.security, "run_security_metadata", lambda value: {"review": "required"})
    failures: list[str] = []

    def record_failure(**kwargs: Any) -> dict[str, Any]:
        failures.append(kwargs["category"])
        return {}

    monkeypatch.setattr(runner.analytics, "record_failure", record_failure)

    with pytest.raises(AppError, match="approval required"):
        runner.run_skill("skill-test", {}, persist=True)
    assert failures == ["security_review_blocked"]

    monkeypatch.setattr(runner.security, "security_context_for_skill", lambda value, **kwargs: {"blocked": False})
    with pytest.raises(AppError, match="input must be an object"):
        runner.run_skill("skill-test", "bad", persist=True)  # type: ignore[arg-type]
    assert failures[-1] == "schema_validation_failed"


def test_input_schema_project_binding_and_output_validation_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    current = skill(projectBinding={"enabled": True})
    _base_mocks(monkeypatch, current)
    failures: list[dict[str, Any]] = []

    def record_failure(**kwargs: Any) -> dict[str, Any]:
        failures.append(kwargs)
        return {}

    monkeypatch.setattr(runner.analytics, "record_failure", record_failure)
    monkeypatch.setattr(runner, "validate_instance", lambda value, schema, label: ["missing required"] if label == "input" else [])
    with pytest.raises(AppError, match="schema validation"):
        runner.run_skill("skill-test", {}, persist=True)
    assert failures[-1]["category"] == "schema_validation_failed"

    monkeypatch.setattr(runner, "validate_instance", lambda value, schema, label: [])
    monkeypatch.setattr(runner.projects, "require_project", lambda project_id: (_ for _ in ()).throw(AppError("project missing")))
    with pytest.raises(AppError, match="project missing"):
        runner.run_skill("skill-test", {}, project_id="project-missing", offline=True)
    assert failures[-1]["category"] == "project_binding_failed"

    monkeypatch.setattr(runner.projects, "require_project", lambda project_id: {"id": project_id})
    monkeypatch.setattr(runner, "format_project_context", lambda project: "project context")
    monkeypatch.setattr(runner, "_media_context", lambda value, project_id: "media context")
    monkeypatch.setattr(runner, "validate_instance", lambda value, schema, label: ["bad output"] if label == "output" else [])
    with pytest.raises(AppError, match="output failed schema"):
        runner.run_skill("skill-test", {}, project_id="project-1", offline=True)


def test_executor_exception_records_failure_and_project_append_failure_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    current = skill(projectBinding={"enabled": True})
    span = _base_mocks(monkeypatch, current)
    monkeypatch.setattr(runner.projects, "require_project", lambda project_id: {"id": project_id})
    monkeypatch.setattr(runner, "format_project_context", lambda project: "")
    monkeypatch.setattr(runner, "_media_context", lambda value, project_id: "")
    monkeypatch.setattr(runner, "validate_instance", lambda *args, **kwargs: [])
    monkeypatch.setattr(runner, "_offline_output", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("skill timeout")))
    monkeypatch.setattr(runner.analytics, "record_failure", lambda **kwargs: {"skillRunId": "failed"})
    monkeypatch.setattr(runner.analytics, "project_run_record", lambda value, **kwargs: value)
    monkeypatch.setattr(runner.projects, "append_project_skill_run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("persist failed")))

    with pytest.raises(TimeoutError, match="skill timeout"):
        runner.run_skill("skill-test", {}, project_id="project-1", offline=True)
    assert span.finished[-1]["status"] == "error"


def test_media_context_missing_cross_project_ranking_and_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.media import library

    assert runner._media_context({}) == ""
    monkeypatch.setattr(library, "get_media", lambda media_id: (_ for _ in ()).throw(AppError("missing")))
    missing = runner._media_context({"mediaId": "missing"})
    assert "was not found" in missing

    monkeypatch.setattr(
        library,
        "get_media",
        lambda media_id: {"mediaId": media_id, "projectId": "other", "title": "Media", "type": "pdf", "status": "ready"},
    )
    assert "different project" in runner._media_context({"mediaIds": ["media-1"]}, project_id="project-1")

    monkeypatch.setattr(
        library,
        "get_media",
        lambda media_id: {"mediaId": media_id, "projectId": "project-1", "title": "Media", "type": "pdf", "status": "ready"},
    )
    monkeypatch.setattr(
        library,
        "list_segments",
        lambda media_id: [
            {"type": "text", "index": 2, "text": "x" * (runner.MEDIA_SEGMENT_MAX_CHARS + 10), "citation": {"uri": "media://2"}},
            {"type": "text", "index": 1, "text": "target words", "citation": {"markdown": "[page](media://1)"}},
        ],
    )
    context = runner._media_context({"mediaIds": ["media-1"], "query": "target words"}, project_id="project-1")
    assert context.index("target words") < context.index("[truncated]")
    assert "media://1" in context

    monkeypatch.setattr(runner, "MEDIA_CONTEXT_MAX_CHARS", 40)
    assert "[Media context]" in runner._media_context({"mediaId": "media-1"}, project_id="project-1")


def test_context_terms_ranking_and_append_limit() -> None:
    assert runner._combined_context("one", "", " two ") == "one\n\n two "
    assert runner._media_context_terms({"task": "Find Alpha beta", "query": 3}) == {"find", "alpha", "beta"}
    assert runner._media_segment_rank({"text": "alpha", "index": 3, "citation": {"uri": "u"}}, {"alpha"}) == (-1, -1, 3)
    assert runner._media_segment_rank({"text": "none", "index": 1, "citation": "bad"}, {"alpha"}) == (0, 0, 1)
    lines = ["head"]
    assert runner._append_context_line(lines, "line", 4) is True
    assert lines[-1] == "line"
    assert runner._append_context_line(lines, "x" * runner.MEDIA_CONTEXT_MAX_CHARS, 1) is False


def test_llm_output_signature_memory_and_malformed_result() -> None:
    captured: list[tuple[dict[str, Any], str]] = []

    def llm(payload: dict[str, Any], *, parent_span_id: str) -> dict[str, Any]:
        captured.append((payload, parent_span_id))
        return {"content": "answer", "model": "used", "usage": "bad", "diagnostics": []}

    output = runner._llm_output(
        skill(memoryPolicy={"scope": "project", "read": True}),
        {"question": "hello"},
        project_id="project-1",
        llm_callable=llm,
        parent_span_id="parent",
    )
    assert output["usage"] == {}
    assert output["diagnostics"] == {}
    assert captured[0][0]["memoryScope"] == "project:project-1"
    assert captured[0][1] == "parent"
    assert "model" not in captured[0][0]

    assert runner._call_llm(lambda payload: {"content": "plain"}, {}, parent_span_id="ignored") == {"content": "plain"}


def test_artifact_policy_file_discovery_dedup_and_no_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    file_id = "a" * 32
    output = {
        "content": "report",
        "nested": {"tool": "create_document", "result": {"fileId": file_id, "filename": "report.md"}},
        "duplicate": {"fileId": file_id, "downloadUrl": "/download"},
    }
    assert runner._find_file_results(output)[0]["tool"] == "create_document"
    assert runner._looks_like_file_result({"fileId": "bad", "filename": "x"}) is False
    assert runner._looks_like_file_result({"fileId": file_id}) is False
    assert runner._apply_artifact_policy(skill(), output, project_id="", run_id="run", persist=True) == ([], [])

    current = skill(artifactPolicy={"autoSave": True, "types": ["md"]})
    monkeypatch.setattr(runner.evidence, "save_markdown_artifact", lambda **kwargs: {"artifactId": "markdown"})
    monkeypatch.setattr(runner.evidence, "register_generated_artifact", lambda value, **kwargs: {"artifactId": value["fileId"]})
    monkeypatch.setattr(runner.projects, "add_project_saved_item", lambda *args, **kwargs: {"id": "saved"})
    linked: list[str] = []
    monkeypatch.setattr(runner.projects, "link_project_artifact", lambda project_id, artifact: linked.append(artifact["artifactId"]))
    artifacts, saved = runner._apply_artifact_policy(current, output, project_id="project-1", run_id="run", persist=True)
    assert [item["artifactId"] for item in artifacts] == ["markdown", file_id]
    assert saved == [{"id": "saved"}]
    assert linked == ["markdown", file_id]
    assert runner._apply_artifact_policy(current, output, project_id="project-1", run_id="run", persist=False) == ([], [])


def test_project_run_record_handles_malformed_lists_and_security() -> None:
    record = runner._project_run_record(
        {
            "skillRunId": "run",
            "input": "bad",
            "output": "bad",
            "artifacts": [{"artifactId": "one"}, "bad"],
            "savedItems": [{"id": "two"}, "bad"],
            "security": {"approved": True},
        }
    )
    assert record["input"] == {}
    assert record["artifactIds"] == ["one"]
    assert record["savedItemIds"] == ["two"]
    assert record["approved"] is True

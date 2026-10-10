"""Trace and generated-file writers cannot survive their native handover."""
from pathlib import Path
from contextlib import closing
import sqlite3

import pytest

from deepseek_infra.infra.native_runtime.authority import PythonWriterMechanicallyDeniedError
from deepseek_infra.infra.observability import observability
from deepseek_infra.infra.skills import evidence
from deepseek_infra.infra.tool_runtime import generated_files
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.skills import analytics, registry, runner
from deepseek_infra.infra.workspace import mutation_gate


def write_retained(path: Path) -> None:
    path.write_text("retained", encoding="utf-8")


def test_trace_handover_refuses_schema_and_all_existing_writer_paths(
    tmp_settings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = observability.start_trace(kind="skill", title="Before handover")
    span = observability.start_span(trace, name="skill.run", kind="skill_run")
    span.finish(output_data={"count": 1})
    observability.finish_trace(trace)
    assert trace
    with sqlite3.connect(observability.TRACE_DB) as conn:
        before = conn.iterdump()
        saved = list(before)
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="observability_trace_store"):
        observability.connect_db()
    with sqlite3.connect(observability.TRACE_DB) as conn:
        with pytest.raises(PythonWriterMechanicallyDeniedError, match="observability_trace_store"):
            observability.initialize_schema(conn)
    # The tracing facade deliberately records errors instead of failing business
    # requests. Its swallowed errors must still leave every table unchanged.
    observability.start_trace(kind="skill", title="Denied")
    observability.finish_trace(trace, status="error", error="Denied update")
    span.finish(output_data={"count": 2})
    with sqlite3.connect(observability.TRACE_DB) as conn:
        assert list(conn.iterdump()) == saved


def test_generated_handover_refuses_file_callback_cleanup_and_artifact_index(
    tmp_settings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = generated_files.store_generated_file("Before handover", "md", write_retained)
    evidence.register_generated_artifact(file, tool="skill_markdown", skill_id="native-test", skill_run_id="before-handover")
    directory = generated_files.GENERATED_DIR
    before = {str(path.relative_to(directory)): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    invoked = []
    for write in [
        lambda: generated_files.store_generated_file("Denied", "md", lambda path: invoked.append(path)),
        generated_files.cleanup_generated_files,
        lambda: evidence.save_artifact_index([]),
        lambda: generated_files.save_generated_file_to_downloads(file["fileId"], downloads_dir=tmp_settings / "downloads"),
    ]:
        with pytest.raises(PythonWriterMechanicallyDeniedError, match="generated_files_store"):
            write()
    assert not invoked
    assert {str(path.relative_to(directory)): path.read_bytes() for path in directory.rglob("*") if path.is_file()} == before


def test_trace_recovery_fence_refuses_schema_and_swallowed_facade_writes(tmp_settings: Path) -> None:
    trace = observability.start_trace(kind="skill", title="Retained trace")
    span = observability.start_span(trace, name="retained", kind="skill_run")
    span.finish()
    observability.finish_trace(trace)
    with closing(sqlite3.connect(observability.TRACE_DB)) as connection:
        before = list(connection.iterdump())
    mutation_gate.write_fence({"restoreId": "trace-recovery"}, root=tmp_settings)
    with pytest.raises(AppError) as failure:
        observability.connect_db()
    assert failure.value.status == 423
    observability.start_trace(kind="skill", title="Fenced trace")
    observability.finish_trace(trace, status="error", error="Fenced update")
    span.finish(status="error", error="Fenced span")
    with closing(sqlite3.connect(observability.TRACE_DB)) as connection:
        assert list(connection.iterdump()) == before


def test_generated_recovery_fence_denies_callback_cleanup_index_and_download(tmp_settings: Path) -> None:
    file = generated_files.store_generated_file("Retained", "md", write_retained)
    evidence.register_generated_artifact(file, skill_id="fixture", skill_run_id="retained")
    before = {str(path.relative_to(tmp_settings)): path.read_bytes() for path in generated_files.GENERATED_DIR.rglob("*") if path.is_file()}
    mutation_gate.write_fence({"restoreId": "generated-recovery"}, root=tmp_settings)
    invoked = []
    for write in [
        lambda: generated_files.store_generated_file("Denied", "md", lambda path: invoked.append(path)),
        generated_files.cleanup_generated_files,
        lambda: evidence.save_artifact_index([]),
        lambda: generated_files.save_generated_file_to_downloads(file["fileId"], downloads_dir=tmp_settings / "downloads"),
    ]:
        with pytest.raises(AppError) as failure:
            write()
        assert failure.value.status == 423
    assert not invoked
    assert not (tmp_settings / "downloads").exists()
    assert {str(path.relative_to(tmp_settings)): path.read_bytes() for path in generated_files.GENERATED_DIR.rglob("*") if path.is_file()} == before


def test_transient_skill_run_recovery_fence_prevents_trace_side_effects(tmp_settings: Path) -> None:
    registry.create_custom_skill({"skillId": "transient-recovery", "name": "Transient", "description": "Recovery fixture", "version": "1.0",
                                  "systemPrompt": "Explain topic", "inputSchema": {"type": "object", "required": ["topic"]},
                                  "outputSchema": {"type": "object"}, "allowedTools": [], "memoryPolicy": {"scope": "none"},
                                  "artifactPolicy": {"types": [], "autoSave": False}, "projectBinding": {"enabled": False}})
    mutation_gate.write_fence({"restoreId": "run-recovery"}, root=tmp_settings)
    with pytest.raises(AppError) as failure:
        runner.run_skill("transient-recovery", {"topic": "Denied"}, offline=True, persist=False)
    assert failure.value.status == 423
    assert not observability.TRACE_DIR.exists()


def test_run_summary_redacts_credential_fields_and_keeps_input_order() -> None:
    payload = {"topic": "Retained topic", "apiKey": "private-api-key", "AUTHORIZATION": "private-header", "TavilyApiKey": "private-search-key"}
    assert analytics.summarize_payload(payload) == "topic=Retained topic, apiKey=[redacted], AUTHORIZATION=[redacted], TavilyApiKey=[redacted]"
    sanitized = observability.sanitize_value(payload, limit=1000)
    assert sanitized == {"topic": "Retained topic", "apiKey": "[redacted]", "AUTHORIZATION": "[redacted]", "TavilyApiKey": "[redacted]"}

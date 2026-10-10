from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from deepseek_infra.infra.agent_runtime import agent_runs
from deepseek_infra.infra.native_runtime.authority import PythonWriterMechanicallyDeniedError


def file_state(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "directory"
        for path in root.rglob("*")
    }


@pytest.mark.parametrize("mode", ["go_authoritative", "python_disabled"])
def test_agent_create_denied_before_filesystem_mutation(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", mode)
    monkeypatch.setenv("DEEPSEEK_LEGACY_PYTHON", "1")
    before = file_state(tmp_settings)
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="Domain 'agent_run'"):
        agent_runs.create_run({"messages": [{"role": "user", "content": "isolated request"}]})
    assert file_state(tmp_settings) == before


@pytest.mark.parametrize("mode", ["go_authoritative", "python_disabled"])
def test_agent_existing_snapshot_and_events_cannot_be_rewritten(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_authoritative")
    monkeypatch.delenv("DEEPSEEK_GO_CONTROL", raising=False)
    run_id = agent_runs.create_run({"model": "deepseek-v4-pro", "apiKey": "fixture-secret"})["runId"]
    agent_runs.append_status(run_id, "running")
    before = file_state(tmp_settings)
    original = agent_runs.load_run(run_id)
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", mode)
    updated = {**original, "status": "done"}
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="Domain 'agent_run'"):
        agent_runs.write_run(updated)
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="Domain 'agent_run'"):
        agent_runs.append_event(run_id, {"type": "content", "text": "unauthorized output"})
    assert agent_runs.mark_orphan_runs_on_startup() == 0
    assert agent_runs.load_run(run_id) == original
    assert agent_runs.events_after(run_id) == original["events"]
    assert file_state(tmp_settings) == before


def test_agent_auto_resume_denied_before_starting_workers(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_authoritative")
    monkeypatch.delenv("DEEPSEEK_GO_CONTROL", raising=False)
    run_id = agent_runs.create_run({})["runId"]
    agent_runs.append_status(run_id, "orphaned")
    starts: list[str] = []

    def record_start(run: str, *_args: object, **_kwargs: object) -> bool:
        starts.append(run)
        return True

    monkeypatch.setattr(agent_runs.registry, "ensure_started", record_start)
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "go_authoritative")
    monkeypatch.setattr(agent_runs, "AGENT_RUNTIME_AUTO_RESUME", False)
    assert agent_runs.resume_orphaned_runs() == 0
    monkeypatch.setattr(agent_runs, "AGENT_RUNTIME_AUTO_RESUME", True)
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="Domain 'agent_run'"):
        agent_runs.resume_orphaned_runs()
    assert starts == []


@pytest.mark.parametrize("mode", ["go_authoritative", "python_disabled"])
@pytest.mark.parametrize("operation", ["start", "approve", "resume", "rerun"])
def test_agent_execution_entry_cannot_call_provider_after_cutover(
    tmp_settings: Path, monkeypatch: pytest.MonkeyPatch, mode: str, operation: str,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_authoritative")
    monkeypatch.delenv("DEEPSEEK_GO_CONTROL", raising=False)
    payload = {"apiKey": "fixture-secret", "model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "isolated task"}]}
    run_id = agent_runs.create_run(payload)["runId"]
    before = file_state(tmp_settings)
    calls: list[bool] = []

    def reject_provider(*_args: object, **_kwargs: object) -> None:
        calls.append(True)
        raise AssertionError("provider must not run after control ownership changes")

    for name in ("plan_for_preset", "execute_plan", "stream_agent_plan", "run_agent", "stream_synthesis_for_outputs"):
        monkeypatch.setattr(agent_runs, name, reject_provider)
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", mode)
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="Domain 'agent_run'"):
        if operation == "start":
            agent_runs.start_planned_run(run_id, payload, confirm_plan=False, agent_preset="full")
        elif operation == "approve":
            agent_runs.continue_with_plan(run_id, payload)
        elif operation == "resume":
            agent_runs.resume_run(run_id, payload)
        else:
            agent_runs.rerun_agent(run_id, payload, agent_id="coder")
    assert calls == []
    assert file_state(tmp_settings) == before


@pytest.mark.parametrize("mode", ["python_authoritative", "shadow"])
def test_agent_reference_writer_still_preserves_indexed_events(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", mode)
    monkeypatch.delenv("DEEPSEEK_GO_CONTROL", raising=False)
    run_id = agent_runs.create_run({"apiKey": "fixture-secret"})["runId"]
    agent_runs.append_status(run_id, "planning")
    agent_runs.append_status(run_id, "awaiting_plan")
    original = agent_runs.load_run(run_id)
    assert original["nextIndex"] == 2
    assert original["status"] == "awaiting_plan"
    assert "apiKey" not in original["requestPayload"]
    assert [event["index"] for event in agent_runs.events_after(run_id, 0)] == [1]
    assert agent_runs.mark_orphan_runs_on_startup() == 0
    assert agent_runs.load_run(run_id) == original

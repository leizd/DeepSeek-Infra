from __future__ import annotations

import pytest

from deepseek_infra.infra.native_runtime.authority import (
    PythonRuntimeDisabledError,
    PythonWriterMechanicallyDeniedError,
    RuntimeMode,
    assert_production_python_allowed,
    assert_python_writer_allowed,
    get_runtime_mode,
)


def test_python_writer_allowed_in_legacy_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_GO_CONTROL", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUNTIME_MODE", raising=False)
    assert get_runtime_mode() == RuntimeMode.PYTHON_AUTHORITATIVE
    assert_python_writer_allowed("scheduler")
    assert_python_writer_allowed("policy")


def test_python_writer_mechanically_denied_when_go_authoritative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_GO_CONTROL", "1")
    assert get_runtime_mode() == RuntimeMode.GO_AUTHORITATIVE

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="Domain 'scheduler' write mutation is mechanically denied"):
        assert_python_writer_allowed("scheduler")

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="Domain 'policy' write mutation is mechanically denied"):
        assert_python_writer_allowed("policy")


def test_python_production_disabled_without_legacy_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    monkeypatch.delenv("DEEPSEEK_LEGACY_PYTHON", raising=False)
    assert get_runtime_mode() == RuntimeMode.PYTHON_DISABLED

    with pytest.raises(PythonRuntimeDisabledError, match="Python production server is de-authoritized"):
        assert_production_python_allowed()


def test_python_production_rollback_allowed_with_legacy_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    monkeypatch.setenv("DEEPSEEK_LEGACY_PYTHON", "1")
    assert_production_python_allowed()


def test_real_policy_writer_mechanically_denied_when_go_authoritative(
    monkeypatch: pytest.MonkeyPatch,
    tmp_settings: None,
) -> None:
    from deepseek_infra.infra.workspace.backup_policies import create_policy

    monkeypatch.setenv("DEEPSEEK_GO_CONTROL", "1")
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="Domain 'policy' write mutation is mechanically denied"):
        create_policy({"policyId": "pol-test-deny", "schedule": "0 0 * * *"})


def test_real_scheduler_worker_mechanically_denied_when_go_authoritative(
    monkeypatch: pytest.MonkeyPatch,
    tmp_settings: None,
) -> None:
    from deepseek_infra.infra.workspace.backup_scheduler import claim_due_drill_slots, worker_tick

    monkeypatch.setenv("DEEPSEEK_GO_CONTROL", "1")
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="Domain 'scheduler' write mutation is mechanically denied"):
        claim_due_drill_slots([], instance_id="inst-1")

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="Domain 'scheduler' write mutation is mechanically denied"):
        worker_tick(instance_id="inst-1", executor=lambda run: None)


def test_real_server_startup_denied_when_python_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deepseek_infra.web.server import create_app

    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    monkeypatch.delenv("DEEPSEEK_LEGACY_PYTHON", raising=False)

    with pytest.raises(PythonRuntimeDisabledError, match="Python production server is de-authoritized"):
        create_app()


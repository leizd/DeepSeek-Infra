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

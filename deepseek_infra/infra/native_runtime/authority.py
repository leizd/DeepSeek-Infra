"""Native runtime ownership and mechanical writer denial gates."""

from __future__ import annotations

import os
from enum import Enum


class RuntimeMode(str, Enum):
    PYTHON_AUTHORITATIVE = "python_authoritative"
    SHADOW = "shadow"
    GO_AUTHORITATIVE = "go_authoritative"
    PYTHON_DISABLED = "python_disabled"


class PythonWriterMechanicallyDeniedError(RuntimeError):
    """Raised when an unauthorized Python writer attempts to mutate a Go-owned control domain."""

    pass


class PythonRuntimeDisabledError(RuntimeError):
    """Raised when Python is invoked in a production topology where Python is de-authoritized."""

    pass


GO_CONTROL_DOMAINS = frozenset(
    {
        "policy",
        "target",
        "scheduler",
        "action",
        "risk",
        "wave",
        "capacity",
        "forecast",
        "maintenance",
        "federation_peer",
        "federation_session",
        "federation_transfer",
        "agent_run",
        "dr_orchestration",
    }
)


def get_runtime_mode() -> RuntimeMode:
    mode_str = os.environ.get("DEEPSEEK_RUNTIME_MODE", "").strip().lower()
    if mode_str == "python_disabled":
        return RuntimeMode.PYTHON_DISABLED
    if mode_str == "go_authoritative" or os.environ.get("DEEPSEEK_GO_CONTROL", "").strip() == "1":
        return RuntimeMode.GO_AUTHORITATIVE
    if mode_str == "shadow":
        return RuntimeMode.SHADOW
    return RuntimeMode.PYTHON_AUTHORITATIVE


def assert_python_writer_allowed(domain: str) -> None:
    """Mechanically deny Python writers for Go-owned control domains when Go is authoritative."""
    mode = get_runtime_mode()
    if mode in (RuntimeMode.GO_AUTHORITATIVE, RuntimeMode.PYTHON_DISABLED):
        if domain.lower() in GO_CONTROL_DOMAINS:
            raise PythonWriterMechanicallyDeniedError(
                f"Domain {domain!r} write mutation is mechanically denied in Python: "
                f"Go control plane is authoritative (mode={mode.value})"
            )


def assert_production_python_allowed() -> None:
    """Verify that Python production execution is allowed or explicitly opted into legacy rollback."""
    mode = get_runtime_mode()
    if mode == RuntimeMode.PYTHON_DISABLED:
        if os.environ.get("DEEPSEEK_LEGACY_PYTHON", "").strip() != "1":
            raise PythonRuntimeDisabledError(
                "Python production server is de-authoritized in Native Rust+Go topology. "
                "Set DEEPSEEK_LEGACY_PYTHON=1 to enable explicit emergency rollback."
            )

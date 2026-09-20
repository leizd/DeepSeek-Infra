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
    """Raised when an unauthorized Python writer attempts to mutate a native-owned domain."""

    pass


class PythonRuntimeDisabledError(RuntimeError):
    """Raised when Python is invoked in a production topology where Python is de-authoritized."""

    pass


GO_CONTROL_DOMAINS = frozenset(
    {
        "policy",
        "policy_crud",
        "target",
        "target_registry",
        "scheduler",
        "backup_scheduler",
        "action",
        "action_journal",
        "risk",
        "risk_manager",
        "wave",
        "wave_scheduler",
        "capacity",
        "capacity_controller",
        "forecast",
        "forecast_service",
        "maintenance",
        "maintenance_window",
        "federation_peer",
        "federation_topology",
        "federation_session",
        "federation_session_handshake",
        "federation_transfer",
        "federation_transfer_broker",
        "agent_run",
        "a2a_task_lifecycle",
        "agent_dag_scheduler",
        "dr_orchestration",
        "dr_orchestrator",
    }
)


RUST_DATA_DOMAINS = frozenset(
    {
        # Both are declared in `release/native_runtime_ownership_v1.json` as python -> rust at 4.9.4,
        # and each has one write choke point in Python — `memory._save_memories_unlocked` and
        # `reminders._write_reminders` — so this set is what makes the handover mechanical rather than
        # a convention each caller has to remember. Both stores also have a *second* Python writer
        # path that runs outside any conversation (the memory tools and routes; `due_reminders`'s
        # delivery marking), which is the reason the gate is per store and not per call site.
        "memory_store",
        "reminders_store",
        # Project metadata is the project.json record, including conversation
        # snapshots and legacy bindings. Child stores retain separate ownership.
        "project_metadata_store",
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
    """Mechanically deny Python writers for native-owned domains.

    Two planes, and deliberately **different** mode conditions:

    - **Go control domains** are denied once Go is authoritative (`GO_AUTHORITATIVE`) or Python is
      disabled, matching ADR-0049's staging, which hands over the control plane first ("4.9.3 makes
      Go control domains authoritative one at a time").
    - **Rust data domains** are denied only once Python is disabled
      (`PYTHON_DISABLED`, "4.9.4 disables Python production authority"). Denying them under
      `GO_AUTHORITATIVE` would be wrong in the other direction: that mode says nothing about the
      *data* plane, and during 4.9.3 the data plane can still be Python's.

    That asymmetry is the point. ADR-0049's rule is "a cutover gate that has not passed leaves the
    prior owner authoritative; it does not permit dual writers" — so a domain is denied in exactly
    the mode that means its owner has changed, and not before.
    """
    mode = get_runtime_mode()
    lowered = domain.lower()
    if mode == RuntimeMode.GO_AUTHORITATIVE and lowered in GO_CONTROL_DOMAINS:
        raise PythonWriterMechanicallyDeniedError(
            f"Domain {domain!r} write mutation is mechanically denied in Python: "
            f"Go control plane is authoritative (mode={mode.value})"
        )
    if mode == RuntimeMode.PYTHON_DISABLED and lowered in GO_CONTROL_DOMAINS | RUST_DATA_DOMAINS:
        plane = "Rust data plane" if lowered in RUST_DATA_DOMAINS else "Go control plane"
        raise PythonWriterMechanicallyDeniedError(
            f"Domain {domain!r} write mutation is mechanically denied in Python: "
            f"{plane} is authoritative (mode={mode.value})"
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

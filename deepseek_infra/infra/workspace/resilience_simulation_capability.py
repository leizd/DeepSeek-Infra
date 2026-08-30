"""Read-only capability and mutation audit for production What-If execution."""

from __future__ import annotations

import copy
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any, Iterator

_ACTIVE_CAPABILITY: ContextVar[SimulationCapability | None] = ContextVar("resilience_simulation_capability", default=None)


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class SimulationViolation(RuntimeError):
    """A production mutation path was reached while simulation was active."""

    def __init__(self, entry: dict[str, Any]) -> None:
        self.entry = dict(entry)
        super().__init__(f"SIMULATION_VIOLATION: {entry['domain']}:{entry['operation']}")


class SimulationCapability:
    """Expose only immutable snapshots required by the optimizer."""

    def __init__(self, authoritative_inputs: dict[str, Any]) -> None:
        self._inputs = copy.deepcopy(authoritative_inputs)
        self._attempted_writes: list[dict[str, Any]] = []
        self._blocked_writes: list[dict[str, Any]] = []

    def read_target_metadata(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._inputs.get("targetMetadata") or [])

    def read_capacity(self) -> dict[str, Any]:
        return copy.deepcopy(self._inputs.get("capacitySnapshot") or {})

    def read_policy(self) -> dict[str, Any]:
        return copy.deepcopy(self._inputs.get("baseline") or {})

    def read_forecast(self) -> dict[str, Any]:
        return copy.deepcopy(self._inputs.get("forecast") or {})

    def read_cost(self) -> dict[str, Any]:
        return copy.deepcopy(self._inputs.get("priceCatalog") or {})

    def read_running_effects(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._inputs.get("runningEffects") or [])

    def inputs(self) -> dict[str, Any]:
        return copy.deepcopy(self._inputs)

    def bind_inputs(self, authoritative_inputs: dict[str, Any]) -> None:
        """Replace the initial candidate shell with the completed read-only snapshot."""
        self._inputs = copy.deepcopy(authoritative_inputs)

    @contextmanager
    def activate(self) -> Iterator[SimulationCapability]:
        token: Token[SimulationCapability | None] = _ACTIVE_CAPABILITY.set(self)
        try:
            yield self
        finally:
            _ACTIVE_CAPABILITY.reset(token)

    def _block(self, domain: str, operation: str, detail: dict[str, Any] | None) -> None:
        entry = {
            "domain": str(domain),
            "operation": str(operation),
            "detail": copy.deepcopy(detail or {}),
            "attemptedAt": _utc_iso(),
        }
        self._attempted_writes.append(entry)
        self._blocked_writes.append(dict(entry))
        raise SimulationViolation(entry)

    def audit(self) -> dict[str, Any]:
        return {
            "attemptedWrites": copy.deepcopy(self._attempted_writes),
            "blockedWrites": copy.deepcopy(self._blocked_writes),
            "attemptedMutationCount": len(self._attempted_writes),
            "blockedMutationCount": len(self._blocked_writes),
        }


def assert_mutation_allowed(
    domain: str,
    operation: str,
    *,
    detail: dict[str, Any] | None = None,
) -> None:
    capability = _ACTIVE_CAPABILITY.get()
    if capability is not None:
        capability._block(domain, operation, detail)  # noqa: SLF001

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from scripts import run_recovery_fault_evidence


def test_recovery_fault_evidence_maps_every_check_to_an_executed_scenario() -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0)

    results = run_recovery_fault_evidence.run_scenarios(run, helper_available=True)  # type: ignore[arg-type]
    report = run_recovery_fault_evidence.build_report(results)
    checks = cast(dict[str, str], report["checks"])

    assert report["ok"] is True
    assert set(checks) == set(run_recovery_fault_evidence.CHECK_SCENARIOS)
    assert len(commands) == len(run_recovery_fault_evidence.SCENARIOS)
    assert all("--no-cov" in command for command in commands)


def test_recovery_fault_evidence_fails_when_helper_or_scenario_is_unavailable() -> None:
    def fail_remote_mutation(command: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=int("test_object_set_fetch_fails_closed" in " ".join(command)))

    results = run_recovery_fault_evidence.run_scenarios(fail_remote_mutation, helper_available=False)  # type: ignore[arg-type]
    report = run_recovery_fault_evidence.build_report(results)
    checks = cast(dict[str, str], report["checks"])

    assert results["process-component-resume"]["exitCode"] == 2
    assert checks["componentTransferResumesAfterRealProcessExit"] == "FAIL"
    assert checks["remoteMutationFailsClosed"] == "FAIL"
    assert report["ok"] is False

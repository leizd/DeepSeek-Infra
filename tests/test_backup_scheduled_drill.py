"""Unit tests for Scheduled Recovery Drills (Recovery Assurance Gate H)."""

from __future__ import annotations

from pathlib import Path


from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_policies,
    backup_recovery_credential,
    backup_recovery_drill,
)


def test_execute_scheduled_drill_disabled_policy(tmp_settings: Path) -> None:
    backup_policies.create_policy(
        {
            "policyId": "policy_no_drill",
            "name": "No Drill Policy",
            "recoveryDrill": {"enabled": False},
        }
    )
    result = backup_recovery_drill.execute_scheduled_drill("policy_no_drill")
    assert result["status"] == "skipped"
    assert result["reason"] == "drill-disabled"


def test_execute_scheduled_drill_blocked_when_no_credentials(tmp_settings: Path) -> None:
    backup_policies.create_policy(
        {
            "policyId": "policy_with_drill",
            "name": "Drill Policy",
            "recoveryDrill": {
                "enabled": True,
                "credentialRef": "slot_missing_key",
                "frequency": "weekly",
            },
        }
    )
    # Ensure credential provider has no secret for slot_missing_key
    provider = backup_recovery_credential.InMemoryCredentialProvider()
    backup_recovery_credential.set_default_credential_provider(provider)

    result = backup_recovery_drill.execute_scheduled_drill("policy_with_drill")
    assert result["status"] == "blocked"
    assert result["reason"] == "unlock-required"

    # Verify drill evidence in ledger
    drill_ev = backup_dr_ledger.get_latest_drill_outcome("managed-local")
    assert drill_ev is not None
    assert drill_ev["result"] == "blocked"
    assert drill_ev["drillKind"] == "scheduled"

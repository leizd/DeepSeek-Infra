"""4.7.3 proof-carrying resilience correctness contracts."""

from __future__ import annotations

import pytest

from deepseek_infra.infra.workspace import evidence_proof, resilience_outcome_verifier, resilience_planner, resilience_risk_engine


def _valid_dr_proof() -> dict[str, object]:
    return {
        "schema": "dr-readiness-proof-v1",
        "drillId": "drill-1",
        "backupId": "backup-1",
        "testedBackupId": "backup-1",
        "resilienceActionId": "action-1",
        "restoreDurationMs": 10,
        "workspaceDigestBefore": "a" * 64,
        "workspaceDigestAfter": "a" * 64,
        "objectCount": 1,
        "commitVerified": True,
        "receiptVerified": True,
        "ageVerified": True,
        "cleanupCompleted": True,
    }


def _verify_dr_proof(proof: object) -> tuple[bool, dict[str, object]]:
    return resilience_outcome_verifier.verify_action_outcome(
        {
            "actionId": "action-1",
            "type": "START_DR_DRILL",
            "parameters": {"backupId": "backup-1"},
        },
        {
            "status": "success",
            "drillId": "drill-1",
            "testedBackupId": "backup-1",
            "proof": proof,
        },
    )


def test_risk_subject_matches_backup_id_exactly() -> None:
    subject = {
        "type": "REPLICA_LAG",
        "policyId": "policy-a",
        "backupId": "backup-b",
        "targetId": "target-c",
    }
    snapshot = {
        "risks": [
            {
                "type": "REPLICA_LAG",
                "policyId": "policy-a",
                "backupId": "backup-other",
                "target": "target-c",
                "severity": "critical",
            },
            {
                "type": "REPLICA_LAG",
                "policyId": "policy-a",
                "backupId": "backup-b",
                "target": "target-c",
                "severity": "warning",
            },
        ]
    }

    matched = resilience_outcome_verifier.find_matching_risk(subject, snapshot)

    assert matched is snapshot["risks"][1]


@pytest.mark.parametrize("missing_field", ["policyId", "backupId", "target"])
def test_risk_subject_missing_scope_cannot_match_scoped_action(missing_field: str) -> None:
    subject = {
        "type": "REPLICA_LAG",
        "policyId": "policy-a",
        "backupId": "backup-b",
        "targetId": "target-c",
    }
    risk = {
        "type": "REPLICA_LAG",
        "policyId": "policy-a",
        "backupId": "backup-b",
        "target": "target-c",
        "severity": "critical",
    }
    del risk[missing_field]

    matched = resilience_outcome_verifier.find_matching_risk(subject, {"risks": [risk]})

    assert matched is None


def test_risk_subject_matches_failure_domain_exactly() -> None:
    subject = {
        "type": "FAILURE_DOMAIN_VIOLATION",
        "policyId": "policy-a",
        "backupId": "backup-b",
        "failureDomain": "zone-b",
    }
    snapshot = {
        "risks": [
            {
                "type": "FAILURE_DOMAIN_VIOLATION",
                "policyId": "policy-a",
                "backupId": "backup-b",
                "failureDomain": "zone-a",
            }
        ]
    }

    assert resilience_outcome_verifier.find_matching_risk(subject, snapshot) is None


def test_scoped_risk_reduction_requires_exact_subject_before_observation() -> None:
    action = {
        "type": "CREATE_REPAIR_JOB",
        "riskSubject": {
            "type": "REPLICA_LAG",
            "policyId": "policy-a",
            "backupId": "backup-b",
        },
        "severityBefore": "critical",
    }
    incomplete_before = {
        "risks": [
            {
                "type": "REPLICA_LAG",
                "policyId": "policy-a",
                "severity": "critical",
            }
        ]
    }

    verified, details = resilience_outcome_verifier.verify_scoped_risk_reduction(
        action,
        incomplete_before,
        {"risks": []},
    )

    assert verified is False
    assert details["reason"] == "target-risk-subject-not-observed-before"


def test_replica_risks_publish_backup_id_at_subject_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        resilience_risk_engine.backup_policies,
        "get_policy",
        lambda _policy_id: {
            "replication": {
                "enabled": True,
                "minCommittedCopies": 2,
                "minFailureDomains": 2,
            }
        },
    )
    monkeypatch.setattr(
        resilience_risk_engine.backup_dr_ledger,
        "latest_recovery_point",
        lambda **_kwargs: {"backupId": "backup-b", "targetId": "target-a"},
    )
    monkeypatch.setattr(
        resilience_risk_engine.backup_dr_ledger,
        "list_logical_recovery_copies",
        lambda **_kwargs: [{"targetId": "target-a", "state": "healthy", "failureDomain": "zone-a"}],
    )

    risks = resilience_risk_engine.evaluate_policy_replica_risk("policy-a")

    assert {risk["backupId"] for risk in risks} == {"backup-b"}


def test_planner_does_not_invent_risk_subject_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resilience_planner, "select_rebalance_destination", lambda _source: "target-b")
    monkeypatch.setattr(resilience_planner, "find_rebalance_candidate_copy", lambda _source: ("policy-a", "backup-b"))
    snapshot = {
        "overallRisk": "critical",
        "riskDigest": "a" * 64,
        "risks": [
            {
                "type": "CAPACITY_EXHAUSTION",
                "target": "target-a",
                "severity": "critical",
                "confidence": "verified",
            }
        ],
    }

    plan = resilience_planner.plan_resilience_actions(snapshot)

    assert plan["actions"][0]["riskSubject"] == {
        "type": "CAPACITY_EXHAUSTION",
        "policyId": None,
        "backupId": None,
        "targetId": "target-a",
        "failureDomain": None,
    }


def test_dr_drill_requires_typed_identity_bound_proof() -> None:
    verified, details = _verify_dr_proof(_valid_dr_proof())

    assert verified is True
    assert details["executionVerified"] is True
    assert details["proofSchema"] == "dr-readiness-proof-v1"


@pytest.mark.parametrize("proof", [None, {}, "not-an-object"])
def test_dr_drill_without_proof_cannot_succeed(proof: object) -> None:
    verified, details = _verify_dr_proof(proof)

    assert verified is False
    assert details["error"] == "dr-drill-proof-required"


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("schema", "dr-readiness-proof-v0"),
        ("drillId", "drill-other"),
        ("backupId", "backup-other"),
        ("resilienceActionId", "action-other"),
    ],
)
def test_dr_drill_proof_identity_must_match(field: str, wrong_value: str) -> None:
    proof = _valid_dr_proof()
    proof[field] = wrong_value

    verified, details = _verify_dr_proof(proof)

    assert verified is False
    assert field in str(details["error"])


@pytest.mark.parametrize("field", ["commitVerified", "receiptVerified", "ageVerified", "cleanupCompleted"])
@pytest.mark.parametrize("invalid_value", [False, None])
def test_dr_drill_proof_requires_all_verification_flags(field: str, invalid_value: object) -> None:
    proof = _valid_dr_proof()
    proof[field] = invalid_value

    verified, details = _verify_dr_proof(proof)

    assert verified is False
    assert field in str(details["error"])


def test_dr_drill_proof_requires_equal_workspace_digests() -> None:
    proof = _valid_dr_proof()
    proof["workspaceDigestAfter"] = "b" * 64

    verified, details = _verify_dr_proof(proof)

    assert verified is False
    assert "drill-workspace-digest-mismatch" in str(details["error"])


def test_dr_readiness_validator_rejects_legacy_untyped_proof() -> None:
    proof = _valid_dr_proof()
    del proof["schema"]
    del proof["backupId"]

    errors = evidence_proof.validate_dr_readiness_proof(proof, "drReadinessProofValid")

    assert "missing-field:schema" in errors
    assert "missing-field:backupId" in errors

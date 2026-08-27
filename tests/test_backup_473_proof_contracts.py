"""4.7.3 proof-carrying resilience correctness contracts."""

from __future__ import annotations

import pytest

from deepseek_infra.infra.workspace import resilience_outcome_verifier, resilience_planner, resilience_risk_engine


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

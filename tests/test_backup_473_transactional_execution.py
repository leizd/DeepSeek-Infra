"""4.7.3 transactional admission, lease, and reconciliation contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest

from deepseek_infra.infra.workspace import autonomous_action_policy, resilience_action_journal


def _limits(**overrides: int) -> dict[str, int]:
    limits = {
        "maxConcurrentActions": 3,
        "maxActionsPerHour": 20,
        "maxConcurrentPerTarget": 2,
        "maxConcurrentPerPolicy": 2,
        "maxSimultaneousFailureDomainsTouched": 1,
        "maxRebalancesPerTargetPerHour": 5,
        "maxDrillsPerPolicyPerDay": 2,
    }
    limits.update(overrides)
    return limits


def _race_admission(action_ids: list[str]) -> list[tuple[bool, dict[str, Any] | None, str]]:
    barrier = Barrier(len(action_ids))

    def admit(action_id: str) -> tuple[bool, dict[str, Any] | None, str]:
        barrier.wait(timeout=5)
        return resilience_action_journal.admit_and_claim_action(action_id, owner_instance_id=f"worker-{action_id}")

    with ThreadPoolExecutor(max_workers=len(action_ids)) as pool:
        return list(pool.map(admit, action_ids))


def test_two_workers_cannot_oversubscribe_global_budget(
    tmp_settings: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        autonomous_action_policy,
        "get_action_rate_limits",
        lambda: _limits(maxConcurrentActions=1),
    )
    for suffix in ("a", "b"):
        resilience_action_journal.record_action_intent(
            {
                "actionId": f"action-global-{suffix}",
                "type": "START_DR_DRILL",
                "parameters": {"policyId": f"policy-{suffix}", "backupId": f"backup-{suffix}"},
            }
        )

    results = _race_admission(["action-global-a", "action-global-b"])

    assert sum(1 for admitted, _action, _reason in results if admitted) == 1
    assert sum(1 for _admitted, action, _reason in results if action and action["state"] == "CLAIMED") == 1
    assert any("max-concurrent-actions-exceeded" in reason for admitted, _action, reason in results if not admitted)


def test_two_workers_cannot_oversubscribe_target_budget(
    tmp_settings: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        autonomous_action_policy,
        "get_action_rate_limits",
        lambda: _limits(maxConcurrentActions=2, maxConcurrentPerTarget=1),
    )
    for suffix in ("a", "b"):
        resilience_action_journal.record_action_intent(
            {
                "actionId": f"action-target-{suffix}",
                "type": "CREATE_REBALANCE_JOB",
                "parameters": {
                    "policyId": f"policy-{suffix}",
                    "backupId": f"backup-{suffix}",
                    "sourceTargetId": f"source-{suffix}",
                    "destTargetId": "shared-target",
                },
            }
        )

    results = _race_admission(["action-target-a", "action-target-b"])

    assert sum(1 for admitted, _action, _reason in results if admitted) == 1
    assert any("max-per-target-concurrent-actions-exceeded" in reason for admitted, _action, reason in results if not admitted)


def test_two_workers_cannot_oversubscribe_policy_budget(
    tmp_settings: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        autonomous_action_policy,
        "get_action_rate_limits",
        lambda: _limits(maxConcurrentActions=2, maxConcurrentPerPolicy=1),
    )
    for suffix in ("a", "b"):
        resilience_action_journal.record_action_intent(
            {
                "actionId": f"action-policy-{suffix}",
                "type": "START_DR_DRILL",
                "parameters": {"policyId": "shared-policy", "backupId": f"backup-{suffix}"},
            }
        )

    results = _race_admission(["action-policy-a", "action-policy-b"])

    assert sum(1 for admitted, _action, _reason in results if admitted) == 1
    assert any("max-per-policy-concurrent-actions-exceeded" in reason for admitted, _action, reason in results if not admitted)


def test_two_workers_cannot_oversubscribe_failure_domain_budget(
    tmp_settings: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        autonomous_action_policy,
        "get_action_rate_limits",
        lambda: _limits(maxConcurrentActions=2, maxSimultaneousFailureDomainsTouched=1),
    )
    for suffix, domain in (("a", "zone-a"), ("b", "zone-b")):
        resilience_action_journal.record_action_intent(
            {
                "actionId": f"action-domain-{suffix}",
                "type": "START_DR_DRILL",
                "riskSubject": {"type": "DR_STALENESS", "failureDomain": domain},
                "parameters": {"policyId": f"policy-{suffix}", "backupId": f"backup-{suffix}"},
            }
        )

    results = _race_admission(["action-domain-a", "action-domain-b"])

    assert sum(1 for admitted, _action, _reason in results if admitted) == 1
    assert any("max-failure-domains-touched-exceeded" in reason for admitted, _action, reason in results if not admitted)

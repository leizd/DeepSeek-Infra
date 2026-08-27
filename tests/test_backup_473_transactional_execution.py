"""Transactional admission, lease, and reconciliation contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    autonomous_action_policy,
    backup_replication,
    resilience_action_journal,
    resilience_effect_reconciler,
)


def _limits(**overrides: int) -> dict[str, int]:
    limits = {
        "maxConcurrentActions": 3,
        "maxActionsPerHour": 1000,
        "maxConcurrentPerTarget": 2,
        "maxConcurrentPerPolicy": 2,
        "maxSimultaneousFailureDomainsTouched": 1,
        "maxRebalancesPerTargetPerHour": 500,
        "maxDrillsPerPolicyPerDay": 500,
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


def test_action_lease_renewal_also_renews_resource_locks(tmp_settings: object) -> None:
    claimed_at = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)
    resilience_action_journal.record_action_intent(
        {
            "actionId": "action-renew",
            "type": "CREATE_REBALANCE_JOB",
            "parameters": {
                "policyId": "policy-renew",
                "backupId": "backup-renew",
                "sourceTargetId": "target-a",
                "destTargetId": "target-b",
            },
        }
    )
    admitted, action, _reason = resilience_action_journal.admit_and_claim_action(
        "action-renew",
        now=claimed_at,
        lease_seconds=30,
    )
    assert admitted is True
    assert action is not None

    renewed_at = claimed_at + timedelta(seconds=20)
    renewed = resilience_action_journal.renew_action_lease(
        "action-renew",
        int(action["executionEpoch"]),
        str(action["claimToken"]),
        now=renewed_at,
        lease_seconds=90,
    )

    assert renewed is True
    expected_lease = resilience_action_journal._utc_iso(renewed_at + timedelta(seconds=90))
    assert resilience_action_journal.get_action("action-renew")["leaseUntil"] == expected_lease  # type: ignore[index]
    with resilience_action_journal._connect() as conn:
        locks = resilience_action_journal.resilience_resource_locks.list_active_locks(conn)
    assert len(locks) == 3
    assert {lock["leaseUntil"] for lock in locks} == {expected_lease}


def test_stale_token_cannot_renew_action_or_resource_locks(tmp_settings: object) -> None:
    claimed_at = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)
    resilience_action_journal.record_action_intent(
        {
            "actionId": "action-stale-renew",
            "type": "CREATE_REPAIR_JOB",
            "parameters": {"policyId": "policy-a", "backupId": "backup-a", "destTargetId": "target-a"},
        }
    )
    admitted, action, _reason = resilience_action_journal.admit_and_claim_action(
        "action-stale-renew",
        now=claimed_at,
        lease_seconds=30,
    )
    assert admitted is True
    assert action is not None
    original_lease = str(action["leaseUntil"])

    renewed = resilience_action_journal.renew_action_lease(
        "action-stale-renew",
        int(action["executionEpoch"]),
        "stale-token",
        now=claimed_at + timedelta(seconds=10),
        lease_seconds=90,
    )

    assert renewed is False
    assert resilience_action_journal.get_action("action-stale-renew")["leaseUntil"] == original_lease  # type: ignore[index]
    with resilience_action_journal._connect() as conn:
        locks = resilience_action_journal.resilience_resource_locks.list_active_locks(conn)
    assert {lock["leaseUntil"] for lock in locks} == {original_lease}


def test_long_operation_heartbeat_renews_lease(
    tmp_settings: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_renewal = Event()
    calls = 0

    def renew(*_args: object, **_kwargs: object) -> bool:
        nonlocal calls
        calls += 1
        if calls >= 2:
            second_renewal.set()
        return True

    monkeypatch.setattr(resilience_action_journal, "renew_action_lease", renew)

    def operation() -> str:
        assert second_renewal.wait(timeout=2)
        return "completed"

    result = resilience_action_journal._run_with_action_lease_heartbeat(
        action_id="action-heartbeat",
        execution_epoch=1,
        claim_token="token-heartbeat",
        lease_seconds=30,
        heartbeat_interval_seconds=0.01,
        operation_name="repair",
        operation=operation,
    )

    assert result == "completed"
    assert calls >= 2


def _expire_executing_action(
    action_id: str,
    action_type: str,
    effect_handle: dict[str, object],
    *,
    effect_class: str = "CANCELABLE",
) -> dict[str, Any]:
    resilience_action_journal.record_action_intent(
        {"actionId": action_id, "type": action_type, "parameters": {}}
    )
    claimed, action, _reason = resilience_action_journal.claim_action(action_id)
    assert claimed is True
    assert action is not None
    resilience_action_journal.update_action_state(
        action_id,
        "EXECUTING",
        execution_epoch=int(action["executionEpoch"]),
        claim_token=str(action["claimToken"]),
        lease_until="2000-01-01T00:00:00Z",
        effect_class=effect_class,
        effect_handle=effect_handle,
    )
    return action


def test_expired_executing_action_enters_reconciling_with_new_epoch(tmp_settings: object) -> None:
    original = _expire_executing_action(
        "action-takeover",
        "CREATE_REPAIR_JOB",
        {"kind": "repair", "repairId": "repair-takeover"},
    )

    claimed, takeover, _reason = resilience_action_journal.claim_action("action-takeover")

    assert claimed is True
    assert takeover is not None
    assert takeover["state"] == "RECONCILING"
    assert takeover["executionEpoch"] == int(original["executionEpoch"]) + 1
    assert takeover["claimToken"] != original["claimToken"]


@pytest.mark.parametrize("effect_kind", ["repair", "rebalance"])
def test_reconciler_finds_existing_storage_effect(tmp_settings: object, effect_kind: str) -> None:
    action_id = f"action-existing-{effect_kind}"
    if effect_kind == "repair":
        job = backup_replication.create_repair_job(
            policy_id="policy-a",
            backup_id="backup-a",
            source_target_id="target-a",
            dest_target_id="target-b",
            resilience_action_id=action_id,
        )
        handle = {"kind": "repair", "repairId": job["repairId"]}
        action_type = "CREATE_REPAIR_JOB"
    else:
        job = backup_replication.create_rebalance_job(
            policy_id="policy-a",
            backup_id="backup-a",
            source_target_id="target-a",
            dest_target_id="target-b",
            resilience_action_id=action_id,
        )
        handle = {"kind": "rebalance", "jobId": job["jobId"]}
        action_type = "CREATE_REBALANCE_JOB"
    _expire_executing_action(action_id, action_type, handle)
    claimed, takeover, _reason = resilience_action_journal.claim_action(action_id)
    assert claimed is True
    assert takeover is not None

    directive, details = resilience_effect_reconciler.reconcile_action_effect(takeover)

    assert directive == "RESUME_EXECUTION"
    assert details["job"]["resilienceActionId"] == action_id


def test_unknown_remote_effect_never_blindly_reexecutes(
    tmp_settings: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _expire_executing_action(
        "action-unknown-effect",
        "CREATE_REPAIR_JOB",
        {"kind": "repair", "repairId": "repair-missing"},
    )
    create_called = False

    def create_effect(**_kwargs: object) -> dict[str, object]:
        nonlocal create_called
        create_called = True
        return {}

    monkeypatch.setattr(backup_replication, "create_repair_job", create_effect)

    with pytest.raises(AppError, match="effect reconciliation failed closed"):
        resilience_action_journal.execute_autonomous_action("action-unknown-effect")

    assert create_called is False
    assert resilience_action_journal.get_action("action-unknown-effect")["state"] == "EFFECT_UNKNOWN"  # type: ignore[index]

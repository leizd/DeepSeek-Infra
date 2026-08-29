"""True waves, enforced transfer reserve, safe preemption, and blast safety."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import (
    autonomous_action_policy,
    backup_policies,
    backup_targets,
    backup_transfer_budget,
    resilience_action_journal,
    resilience_coordinator,
    resilience_fleet_scheduler,
)


def _repair(action_id: str, *, policy: str, backup: str, target: str) -> dict[str, Any]:
    return {
        "actionId": action_id,
        "type": "CREATE_REPAIR_JOB",
        "severity": "critical",
        "parameters": {"policyId": policy, "backupId": backup, "destTargetId": target},
    }


def _rebalance(action_id: str, *, policy: str, backup: str, source: str, target: str) -> dict[str, Any]:
    return {
        "actionId": action_id,
        "type": "CREATE_REBALANCE_JOB",
        "severity": "warning",
        "parameters": {"policyId": policy, "backupId": backup, "sourceTargetId": source, "destTargetId": target},
    }


def test_all_schedulable_actions_are_partitioned_into_dependency_waves(tmp_settings: Path) -> None:
    actions = [
        _repair("repair-a", policy="policy-a", backup="backup-a", target="target-b"),
        _rebalance("rebalance-a", policy="policy-a", backup="backup-a", source="target-a", target="target-c"),
        {
            "actionId": "drill-b",
            "type": "START_DR_DRILL",
            "severity": "warning",
            "parameters": {"policyId": "policy-b", "backupId": "backup-b", "targetId": "target-d"},
        },
    ]

    schedule = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"riskDigest": "risk-waves", "risks": []},
        candidate_actions=actions,
        now=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    assert [[item["actionId"] for item in wave["actions"]] for wave in schedule["executionWaves"]] == [
        ["repair-a", "drill-b"],
        ["rebalance-a"],
    ]
    assert schedule["deferredActions"] == []
    assert schedule["unschedulableActions"] == []
    assert schedule["admittedCount"] == 3
    assert all("waveIndex" in item for wave in schedule["executionWaves"] for item in wave["actions"])


def test_missing_dependency_is_typed_unschedulable(tmp_settings: Path) -> None:
    action = _repair("repair-missing-dep", policy="policy-x", backup="backup-x", target="target-x")
    action["dependsOn"] = ["action-that-does-not-exist"]

    schedule = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"riskDigest": "risk-missing-dep", "risks": []},
        candidate_actions=[action],
    )

    assert schedule["executionWaves"] == []
    assert schedule["unschedulableActions"][0]["unschedulableReason"] == "MISSING_DEPENDENCY"


def test_missing_and_duplicate_action_ids_are_typed_unschedulable(tmp_settings: Path) -> None:
    missing_id = _repair("", policy="policy-x", backup="backup-x", target="target-x")
    duplicate_a = _repair("duplicate", policy="policy-a", backup="backup-a", target="target-a")
    duplicate_b = _repair("duplicate", policy="policy-b", backup="backup-b", target="target-b")

    schedule = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"riskDigest": "risk-invalid-ids", "risks": []},
        candidate_actions=[missing_id, duplicate_a, duplicate_b],
    )

    assert schedule["executionWaves"] == []
    assert [item["unschedulableReason"] for item in schedule["unschedulableActions"]] == [
        "MISSING_ACTION_ID",
        "DUPLICATE_ACTION_ID",
        "DUPLICATE_ACTION_ID",
    ]


def test_rebalance_is_deferred_to_next_wave_by_real_transfer_reserve(tmp_settings: Path) -> None:
    backup_transfer_budget.configure_global_transfer_budget(
        global_bandwidth_bytes_per_sec=100 * 1024 * 1024,
        reserved_repair_bandwidth_bytes_per_sec=50 * 1024 * 1024,
        reserved_dr_bandwidth_bytes_per_sec=25 * 1024 * 1024,
    )
    schedule = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"riskDigest": "risk-transfer", "risks": []},
        candidate_actions=[
            _repair("repair-reserved", policy="policy-r", backup="backup-r", target="target-r"),
            _rebalance("rebalance-opportunistic", policy="policy-b", backup="backup-b", source="target-s", target="target-d"),
        ],
    )

    assert [[item["actionId"] for item in wave["actions"]] for wave in schedule["executionWaves"]] == [
        ["repair-reserved"],
        ["rebalance-opportunistic"],
    ]
    assert schedule["transferBudget"]["repairReservedBytesPerSecond"] == 50 * 1024 * 1024
    rebalance = schedule["executionWaves"][1]["actions"][0]
    assert "DEFERRED_TRANSFER_BUDGET" in rebalance["priorDeferReasons"]


def test_runtime_rebalance_cannot_consume_active_repair_reserved_tokens(tmp_settings: Path) -> None:
    manager = backup_transfer_budget.TransferBudgetManager(
        global_bandwidth_bytes_per_sec=4 * 1024 * 1024,
        reserved_repair_bandwidth_bytes_per_sec=2 * 1024 * 1024,
        reserved_dr_bandwidth_bytes_per_sec=1 * 1024 * 1024,
        max_burst_bytes=4 * 1024 * 1024,
    )
    manager.acquire_transfer_token(
        "active-repair",
        backup_transfer_budget.TrafficClass.P2_REQUIRED_REPAIR,
    )
    manager.acquire_transfer_token(
        "opportunistic-rebalance",
        backup_transfer_budget.TrafficClass.P5_REBALANCE_DRAIN,
    )

    wait_seconds = manager.consume_bandwidth("opportunistic-rebalance", 3 * 1024 * 1024, now=100.0)

    assert wait_seconds > 0
    manager.release_transfer_token("opportunistic-rebalance")
    manager.release_transfer_token("active-repair")


def test_safe_preemption_releases_victim_and_claims_repair_atomically(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        autonomous_action_policy,
        "get_action_rate_limits",
        lambda: {
            "maxConcurrentActions": 1,
            "maxActionsPerHour": 100,
            "maxConcurrentPerTarget": 2,
            "maxConcurrentPerPolicy": 2,
            "maxSimultaneousFailureDomainsTouched": 2,
            "maxRebalancesPerTargetPerHour": 100,
            "maxDrillsPerPolicyPerDay": 100,
        },
    )
    victim = _rebalance("victim-rebalance", policy="policy-v", backup="backup-v", source="source-v", target="target-v")
    victim["severity"] = "warning"
    resilience_action_journal.record_action_intent(victim)
    claimed, victim_action, _ = resilience_action_journal.admit_and_claim_action(
        "victim-rebalance",
        owner_instance_id="victim-worker",
        enforce_budgets=False,
    )
    assert claimed and victim_action is not None and victim_action["effectClass"] == "NO_EFFECT"

    repair = _repair("critical-repair", policy="policy-r", backup="backup-r", target="target-r")
    repair["severityBefore"] = "critical"
    resilience_action_journal.record_action_intent(repair)
    admitted, repair_action, reason = resilience_action_journal.admit_and_claim_action(
        "critical-repair",
        owner_instance_id="repair-worker",
    )

    assert admitted is True and reason == "admitted-with-preemption"
    assert repair_action is not None and repair_action["state"] == "CLAIMED"
    assert resilience_action_journal.get_action("victim-rebalance")["state"] == "PREEMPTED"  # type: ignore[index]
    decisions = resilience_action_journal.list_preemption_decisions(preemptor_action_id="critical-repair")
    assert decisions == [
        {
            "preemptor": "critical-repair",
            "victim": "victim-rebalance",
            "victimState": "CLAIMED",
            "victimEffectClass": "NO_EFFECT",
            "safe": True,
            "reason": "critical-repair-preempts-warning-rebalance",
        }
    ]


def test_unsafe_preemption_cannot_modify_executing_victim(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        autonomous_action_policy,
        "get_action_rate_limits",
        lambda: {
            "maxConcurrentActions": 1,
            "maxActionsPerHour": 100,
            "maxConcurrentPerTarget": 2,
            "maxConcurrentPerPolicy": 2,
            "maxSimultaneousFailureDomainsTouched": 2,
            "maxRebalancesPerTargetPerHour": 100,
            "maxDrillsPerPolicyPerDay": 100,
        },
    )
    victim = _rebalance("unsafe-victim", policy="policy-v", backup="backup-v", source="source-v", target="target-v")
    resilience_action_journal.record_action_intent(victim)
    claimed, victim_action, _ = resilience_action_journal.admit_and_claim_action(
        "unsafe-victim", owner_instance_id="victim-worker", enforce_budgets=False
    )
    assert claimed and victim_action is not None
    resilience_action_journal.update_action_state(
        "unsafe-victim",
        "EXECUTING",
        execution_epoch=int(victim_action["executionEpoch"]),
        claim_token=str(victim_action["claimToken"]),
        effect_class="CANCELABLE",
        effect_handle={"kind": "rebalance", "jobId": "job-live"},
    )
    repair = _repair("blocked-repair", policy="policy-r", backup="backup-r", target="target-r")
    repair["severityBefore"] = "critical"
    resilience_action_journal.record_action_intent(repair)

    admitted, _repair_action, reason = resilience_action_journal.admit_and_claim_action("blocked-repair")

    assert admitted is False
    assert "max-concurrent-actions-exceeded" in reason
    assert resilience_action_journal.get_action("unsafe-victim")["state"] == "EXECUTING"  # type: ignore[index]
    assert resilience_action_journal.list_preemption_decisions(preemptor_action_id="blocked-repair") == []


def test_degraded_baseline_cannot_be_further_degraded_and_running_effects_count(tmp_settings: Path) -> None:
    target_a = tmp_settings / "target-a"
    target_b = tmp_settings / "target-b"
    target_a.mkdir()
    target_b.mkdir()
    backup_targets.register_filesystem_target("target_a", path=target_a, failure_domain="zone-a")
    backup_targets.register_filesystem_target("target_b", path=target_b, failure_domain="zone-b")
    backup_policies.create_policy(
        {
            "name": "Blast Safety",
            "policyId": "policy-blast",
            "targetId": "target_a",
            "replication": {"enabled": True, "minCommittedCopies": 2, "minFailureDomains": 2},
        }
    )
    copies = {
        ("policy-blast", "backup-blast"): [
            {"targetId": "target_a", "state": "healthy", "failureDomain": "zone-a"},
        ]
    }
    running = _rebalance(
        "running-rebalance",
        policy="policy-blast",
        backup="backup-blast",
        source="target_a",
        target="target_b",
    )
    running["effectPhase"] = "pruning_source"

    passed, details = resilience_coordinator.simulate_coordination_wave(
        [],
        current_copies=copies,
        failure_domains={"target_a": "zone-a", "target_b": "zone-b"},
        running_actions=[running],
    )

    assert passed is False
    evaluation = details["evaluations"]["policy-blast:backup-blast"]
    assert evaluation["copiesBefore"] == 1
    assert evaluation["copiesDuring"] == 0
    assert evaluation["copySafetyFloor"] == 1
    assert evaluation["failureDomainSafetyFloor"] == 1
    assert evaluation["runningEffectCount"] == 1


def test_maintenance_window_parser_fails_closed_and_supports_full_day_and_overnight() -> None:
    now = datetime(2026, 8, 28, 0, 30, tzinfo=timezone.utc)
    action = _rebalance("window-edge", policy="policy-window", backup="backup-window", source="a", target="b")
    action["parameters"]["maintenanceWindow"] = {"timezone": "Not/A_Zone", "start": "00:00", "end": "01:00"}
    invalid = resilience_fleet_scheduler.evaluate_maintenance_window(action, now=now)
    assert invalid["allowed"] is False
    assert invalid["reason"] == "INVALID_MAINTENANCE_WINDOW"

    action["parameters"]["maintenanceWindow"] = {"timezone": "UTC", "start": "25:00", "end": "01:00"}
    out_of_range = resilience_fleet_scheduler.evaluate_maintenance_window(action, now=now)
    assert out_of_range["allowed"] is False
    assert out_of_range["reason"] == "INVALID_MAINTENANCE_WINDOW"

    action["parameters"]["maintenanceWindow"] = {"timezone": "UTC", "start": "00:00", "end": "00:00"}
    full_day = resilience_fleet_scheduler.evaluate_maintenance_window(action, now=now)
    assert full_day["allowed"] is True
    assert full_day["reason"] == "WITHIN_MAINTENANCE_WINDOW"

    action["parameters"]["maintenanceWindow"] = {"timezone": "UTC", "start": "23:00", "end": "02:00"}
    overnight = resilience_fleet_scheduler.evaluate_maintenance_window(action, now=now)
    assert overnight["allowed"] is True


def test_dependency_cycle_and_approval_are_typed_unschedulable(tmp_settings: Path) -> None:
    repair = _repair("cycle-repair", policy="policy-cycle", backup="backup-cycle", target="target-b")
    rebalance = _rebalance(
        "cycle-rebalance",
        policy="policy-cycle",
        backup="backup-cycle",
        source="target-a",
        target="target-c",
    )
    repair["dependsOn"] = ["cycle-rebalance"]
    cycle = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"riskDigest": "cycle", "risks": []},
        candidate_actions=[repair, rebalance],
    )
    assert {item["unschedulableReason"] for item in cycle["unschedulableActions"]} == {
        "DEPENDENCY_CYCLE_OR_BLOCKED_PREDECESSOR"
    }

    approval = _repair("approval-repair", policy="policy-approval", backup="backup-approval", target="target-b")
    approval["requiresApproval"] = True
    blocked = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"riskDigest": "approval", "risks": []},
        candidate_actions=[approval],
    )
    assert blocked["unschedulableActions"][0]["unschedulableReason"] == "APPROVAL_REQUIRED"


def test_invalid_zero_capacity_and_blast_failure_are_typed_unschedulable(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = _repair("zero-capacity", policy="policy-zero", backup="backup-zero", target="target-zero")
    zero_capacity = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"riskDigest": "zero", "risks": []},
        candidate_actions=[action],
        action_policy={"rateLimits": {"maxConcurrentActions": 0}},
    )
    assert zero_capacity["executionWaves"] == []
    assert zero_capacity["unschedulableActions"][0]["unschedulableReason"] == "INVALID_ZERO_CAPACITY_BUDGET"

    monkeypatch.setattr(
        resilience_coordinator,
        "simulate_coordination_wave",
        lambda *_args, **_kwargs: (False, {"passed": False, "reason": "copy-floor"}),
    )
    blast = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"riskDigest": "blast", "risks": []},
        candidate_actions=[_repair("blast-repair", policy="policy-b", backup="backup-b", target="target-b")],
    )
    assert blast["executionWaves"] == []
    assert blast["unschedulableActions"][0]["unschedulableReason"] == "BLAST_RADIUS_VIOLATION"


def test_blast_conflict_partitions_individually_safe_actions_into_separate_waves(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def simulate(actions: list[dict[str, Any]], **_kwargs: Any) -> tuple[bool, dict[str, Any]]:
        passed = len(actions) <= 1
        return passed, {
            "passed": passed,
            "reason": "safe" if passed else "combined-wave-conflict",
            "proposedActionIds": [str(action["actionId"]) for action in actions],
        }

    monkeypatch.setattr(resilience_coordinator, "simulate_coordination_wave", simulate)
    schedule = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"riskDigest": "blast-partition", "risks": []},
        candidate_actions=[
            _repair("blast-safe-a", policy="policy-a", backup="backup-a", target="target-a"),
            _repair("blast-safe-b", policy="policy-b", backup="backup-b", target="target-b"),
        ],
    )

    assert [[item["actionId"] for item in wave["actions"]] for wave in schedule["executionWaves"]] == [
        ["blast-safe-a"],
        ["blast-safe-b"],
    ]
    assert schedule["unschedulableActions"] == []
    assert "BLAST_RADIUS_WAVE_CONFLICT" in schedule["executionWaves"][1]["actions"][0]["priorDeferReasons"]


def test_wave_deferral_records_global_target_policy_and_domain_constraints(tmp_settings: Path) -> None:
    first = _repair("limit-first", policy="policy-one", backup="backup-one", target="target-one")
    first["parameters"]["failureDomain"] = "zone-one"
    same_target = _repair("limit-target", policy="policy-two", backup="backup-two", target="target-one")
    same_target["parameters"]["failureDomain"] = "zone-one"
    same_policy_new_domain = _repair(
        "limit-policy-domain",
        policy="policy-one",
        backup="backup-three",
        target="target-three",
    )
    same_policy_new_domain["parameters"]["failureDomain"] = "zone-two"
    schedule = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"riskDigest": "limits", "risks": []},
        candidate_actions=[first, same_target, same_policy_new_domain],
        action_policy={
            "rateLimits": {
                "maxConcurrentActions": 1,
                "maxConcurrentPerTarget": 1,
                "maxConcurrentPerPolicy": 1,
                "maxSimultaneousFailureDomainsTouched": 1,
            }
        },
    )
    deferred_reasons = {
        item["actionId"]: set(item["priorDeferReasons"])
        for wave in schedule["executionWaves"]
        for item in wave["actions"]
    }
    assert "TARGET_CONCURRENCY_EXCEEDED" in deferred_reasons["limit-target"]
    assert "GLOBAL_CONCURRENCY_EXCEEDED" in deferred_reasons["limit-target"]
    assert "POLICY_CONCURRENCY_EXCEEDED" in deferred_reasons["limit-policy-domain"]
    assert "FAILURE_DOMAIN_LIMIT_EXCEEDED" in deferred_reasons["limit-policy-domain"]


def test_coordinator_edges_and_drill_rebalance_dependency_are_preserved(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator_actions = [
        {"actionId": "coordinator-a", "type": "NOOP_A", "parameters": {}},
        {"actionId": "coordinator-b", "type": "NOOP_B", "parameters": {}},
    ]
    monkeypatch.setattr(
        resilience_coordinator,
        "plan_coordinated_resilience",
        lambda *_args, **_kwargs: {
            "actions": coordinator_actions,
            "dependencies": [["coordinator-a", "coordinator-b"]],
        },
    )
    coordinated = resilience_fleet_scheduler.schedule_fleet_resilience({"riskDigest": "coordinated", "risks": []})
    assert [[item["actionId"] for item in wave["actions"]] for wave in coordinated["executionWaves"]] == [
        ["coordinator-a"],
        ["coordinator-b"],
    ]

    drill = {
        "actionId": "drill-before-rebalance",
        "type": "START_DR_DRILL",
        "severity": "warning",
        "parameters": {"policyId": "policy-drill", "backupId": "shared-backup", "targetId": "target-drill"},
    }
    rebalance = _rebalance(
        "rebalance-after-drill",
        policy="policy-other",
        backup="shared-backup",
        source="target-a",
        target="target-b",
    )
    result = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"riskDigest": "drill-dependency", "risks": []},
        candidate_actions=[drill, rebalance],
    )
    assert [[item["actionId"] for item in wave["actions"]] for wave in result["executionWaves"]] == [
        ["drill-before-rebalance"],
        ["rebalance-after-drill"],
    ]

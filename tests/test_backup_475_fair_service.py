"""Reserved versus consumed fair service (Gate B)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from deepseek_infra.infra.workspace import resilience_scheduler_service


def test_schedule_result_reserves_without_consuming(tmp_settings: Path) -> None:
    action = {
        "actionId": "reserve-only",
        "waveIndex": 0,
        "executionEpoch": 1,
        "parameters": {"policyId": "policy-reserve", "estimatedBytes": 2048},
    }
    schedule = {"scheduleId": "sched-reserve", "executionWaves": [{"waveIndex": 0, "actions": [action]}]}
    resilience_scheduler_service.record_schedule_result(schedule, [action])
    assert resilience_scheduler_service.get_policy_service("policy-reserve") is None
    reservation = resilience_scheduler_service.get_reservation("reserve-only")
    assert reservation is not None
    assert reservation["status"] == "RESERVED"
    assert reservation["scheduleId"] == "sched-reserve"
    assert reservation["waveIndex"] == 0
    assert reservation["executionEpoch"] == 1


def test_completed_action_charges_observed_bytes_exactly_once(tmp_settings: Path) -> None:
    action = {
        "actionId": "consume-once",
        "scheduleId": "sched-consume",
        "parameters": {"policyId": "policy-consume", "estimatedBytes": 4096},
    }
    resilience_scheduler_service.reserve_scheduled_actions([action], schedule_id="sched-consume")
    first = resilience_scheduler_service.consume_action_service(action, actual_bytes=1024, actual_duration_ms=12, actual_traffic_class="repair")
    second = resilience_scheduler_service.consume_action_service(action, actual_bytes=999999)
    assert first is not None and first["status"] == "CONSUMED"
    assert first["actualBytes"] == 1024
    assert second is not None and second["actualBytes"] == 1024
    state = resilience_scheduler_service.get_policy_service("policy-consume")
    assert state is not None and state["actionsServed"] == 1
    assert state["bytesServed"] == 1024


def test_preempted_action_releases_reservation(tmp_settings: Path) -> None:
    action = {"actionId": "preempt-me", "parameters": {"policyId": "policy-preempt", "estimatedBytes": 100}}
    resilience_scheduler_service.record_schedule_result({"scheduleId": "sched-preempt", "executionWaves": []}, [action])
    released = resilience_scheduler_service.release_action_reservation("preempt-me", reason="PREEMPTED")
    assert released is not None and released["status"] == "RELEASED"
    assert released["releaseReason"] == "PREEMPTED"
    assert resilience_scheduler_service.get_policy_service("policy-preempt") is None
    consumed = resilience_scheduler_service.consume_action_service(action, actual_bytes=100)
    assert consumed is not None and consumed["status"] == "RELEASED"
    assert resilience_scheduler_service.get_policy_service("policy-preempt") is None


def test_service_consumption_survives_reconnect(tmp_settings: Path) -> None:
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    resilience_scheduler_service.record_consumed_service(
        [{"actionId": "persist-a", "parameters": {"policyId": "policy-persist", "estimatedBytes": 512}}],
        consumed_at=now,
    )
    state = resilience_scheduler_service.get_policy_service("policy-persist")
    assert state is not None and state["actionsServed"] == 1
    assert state["virtualRuntime"] > 0
    listed = resilience_scheduler_service.list_reservations(schedule_id="manual", status="CONSUMED")
    assert listed[0]["actionId"] == "persist-a"


def test_release_schedule_reservations_and_empty_action_id(tmp_settings: Path) -> None:
    resilience_scheduler_service.record_schedule_result(
        {"scheduleId": "sched-bulk", "executionWaves": []},
        [
            {"actionId": "bulk-1", "parameters": {"policyId": "p-bulk"}},
            {"actionId": "bulk-2", "parameters": {"policyId": "p-bulk"}},
            {},
        ],
    )
    assert resilience_scheduler_service.release_schedule_reservations("sched-bulk", reason="REPLAN") == 2
    assert resilience_scheduler_service.release_action_reservation("missing", reason="STALE") is None
    assert resilience_scheduler_service.consume_action_service({}) is None
    expired = resilience_scheduler_service.release_action_reservation("bulk-1", reason="EXPIRED")
    assert expired is not None and expired["status"] == "RELEASED"

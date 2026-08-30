"""Reserved versus consumed fair service (Gate B)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from deepseek_infra.infra.workspace import resilience_scheduler_service


def _seed_historical_service(action: dict[str, object], *, consumed_at: datetime | None = None) -> None:
    action_id = str(action["actionId"])
    schedule_id = str(action.get("scheduleId") or "manual")
    if resilience_scheduler_service.get_reservation(action_id) is None:
        resilience_scheduler_service.reserve_scheduled_actions([action], schedule_id=schedule_id, scheduled_at=consumed_at)
    timestamp = resilience_scheduler_service._utc_iso(consumed_at)  # noqa: SLF001 - legacy DB fixture
    byte_count = resilience_scheduler_service._estimated_bytes(action)  # noqa: SLF001 - legacy DB fixture
    policy_id = resilience_scheduler_service._policy_id(action)  # noqa: SLF001 - legacy DB fixture
    with resilience_scheduler_service._connect() as conn:  # noqa: SLF001 - legacy DB fixture
        conn.execute("BEGIN IMMEDIATE")
        charged = resilience_scheduler_service._charge_consumed(  # noqa: SLF001 - legacy DB fixture
            conn,
            action_id=action_id,
            policy_id=policy_id,
            byte_count=byte_count,
            timestamp=timestamp,
        )
        if charged:
            conn.execute(
                """
                UPDATE resilience_service_reservations
                SET status = 'CONSUMED', actual_bytes = ?, outcome = 'SUCCEEDED', updated_at = ?
                WHERE action_id = ? AND status = 'RESERVED'
                """,
                (byte_count, timestamp, action_id),
            )


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


def test_historical_service_seed_charges_exactly_once(tmp_settings: Path) -> None:
    action = {
        "actionId": "consume-once",
        "scheduleId": "sched-consume",
        "parameters": {"policyId": "policy-consume", "estimatedBytes": 4096},
    }
    resilience_scheduler_service.reserve_scheduled_actions([action], schedule_id="sched-consume")
    seeded: dict[str, object] = {**action, "parameters": {"policyId": "policy-consume", "estimatedBytes": 1024}}
    _seed_historical_service(seeded)
    _seed_historical_service(
        {**seeded, "parameters": {"policyId": "policy-consume", "estimatedBytes": 999999}}
    )
    consumed = resilience_scheduler_service.get_reservation("consume-once")
    assert consumed is not None and consumed["status"] == "CONSUMED"
    assert consumed["actualBytes"] == 1024
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
    consumed = resilience_scheduler_service.get_reservation("preempt-me")
    assert consumed is not None and consumed["status"] == "RELEASED"
    assert resilience_scheduler_service.get_policy_service("policy-preempt") is None


def test_service_consumption_survives_reconnect(tmp_settings: Path) -> None:
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    _seed_historical_service(
        {"actionId": "persist-a", "parameters": {"policyId": "policy-persist", "estimatedBytes": 512}},
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
    expired = resilience_scheduler_service.release_action_reservation("bulk-1", reason="EXPIRED")
    assert expired is not None and expired["status"] == "RELEASED"

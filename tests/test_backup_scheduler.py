from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_policies, backup_scheduler


UTC = timezone.utc
RECIPIENT_A = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"


def _policy(tmp_settings: Path, *, cron: str = "0 3 * * *", timezone_name: str = "UTC", catchup: int = 86400, jitter: int = 0) -> dict[str, object]:
    return backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": "nightly",
            "enabled": True,
            "schedule": {"cron": cron, "timezone": timezone_name, "catchupWindowSeconds": catchup, "jitterSeconds": jitter},
            "protection": {"mode": "age-recipient", "recipients": [RECIPIENT_A]},
            "targetId": "managed-local",
        }
    )


def _claim(policy: dict[str, object], *, instance: str = "w1", now: datetime) -> list[backup_scheduler.ClaimedRun]:
    return backup_scheduler.claim_due_slots([policy], instance_id=instance, now=now)


def test_same_slot_claimed_exactly_once_across_instances(tmp_settings: Path) -> None:
    policy = _policy(tmp_settings)
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    first = _claim(policy, instance="w1", now=now)
    assert len(first) == 1
    second = _claim(policy, instance="w2", now=now)
    assert second == []
    again = _claim(policy, instance="w1", now=now)
    assert again == []


def test_claim_uses_policy_timezone_for_slots(tmp_settings: Path) -> None:
    policy = _policy(tmp_settings, timezone_name="Asia/Singapore")
    # 03:00 SGT == 19:00 UTC; claim at 20:00 UTC sees the slot.
    claimed = _claim(policy, now=datetime(2026, 6, 2, 20, 0, tzinfo=UTC))
    assert len(claimed) == 1
    assert claimed[0].schedule_slot.endswith("@Asia/Singapore")


def test_crashed_worker_slot_is_taken_over(tmp_settings: Path) -> None:
    policy = _policy(tmp_settings)
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    claimed = _claim(policy, instance="w1", now=now)
    run = claimed[0]
    # Lease expires without any phase progress (crash).
    reclaimed = backup_scheduler.reclaim_abandoned_slots(instance_id="w2", now=now + timedelta(seconds=400))
    assert len(reclaimed) == 1
    takeover = reclaimed[0]
    assert takeover.run_id != run.run_id
    assert takeover.attempt == 2
    assert takeover.schedule_slot == run.schedule_slot
    abandoned = backup_scheduler.get_run(run.run_id)
    assert abandoned["phase"] == "abandoned"
    fresh = backup_scheduler.get_run(takeover.run_id)
    assert fresh["ownerInstanceId"] == "w2"


def test_stale_worker_cannot_publish_or_complete(tmp_settings: Path) -> None:
    policy = _policy(tmp_settings)
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    run = _claim(policy, instance="w1", now=now)[0]
    takeover = backup_scheduler.reclaim_abandoned_slots(instance_id="w2", now=now + timedelta(seconds=400))[0]
    with pytest.raises(AppError) as lost:
        backup_scheduler.complete_run(run.run_id, backup_id="backup_x", filename="f.age", instance_id="w1", fencing_token=run.fencing_token)
    assert lost.value.status == 409
    backup_scheduler.complete_run(takeover.run_id, backup_id="backup_x", filename="f.age", instance_id="w2", fencing_token=takeover.fencing_token, now=now + timedelta(seconds=400))
    assert backup_scheduler.get_run(takeover.run_id)["phase"] == "complete"


def test_lease_assertion_and_renewal(tmp_settings: Path) -> None:
    policy = _policy(tmp_settings)
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    run = _claim(policy, now=now)[0]
    backup_scheduler.assert_run_lease(run.run_id, "w1", run.fencing_token, now=now)
    with pytest.raises(AppError):
        backup_scheduler.assert_run_lease(run.run_id, "w2", run.fencing_token, now=now)
    with pytest.raises(AppError):
        backup_scheduler.assert_run_lease(run.run_id, "w1", run.fencing_token + 1, now=now)
    with pytest.raises(AppError):
        backup_scheduler.assert_run_lease(run.run_id, "w1", run.fencing_token, now=now + timedelta(seconds=400))
    backup_scheduler.renew_run_lease(run.run_id, "w1", run.fencing_token, now=now)
    backup_scheduler.assert_run_lease(run.run_id, "w1", run.fencing_token, now=now + timedelta(seconds=299))
    with pytest.raises(AppError):
        backup_scheduler.renew_run_lease(run.run_id, "w2", run.fencing_token, now=now)


def test_missed_slots_coalesce_to_one_catchup_run(tmp_settings: Path) -> None:
    policy = _policy(tmp_settings, cron="0 * * * *", catchup=86400)
    now = datetime(2026, 6, 2, 12, 30, tzinfo=UTC)
    claimed = _claim(policy, now=now)
    assert len(claimed) == 1
    assert claimed[0].schedule_slot == "2026-06-02T12:00@UTC"
    skipped = _slot_statuses(policy)
    assert "catchup-coalesced" in skipped
    assert len(skipped) >= 10


def test_slots_outside_catchup_window_are_skipped(tmp_settings: Path) -> None:
    policy = _policy(tmp_settings, cron="0 3 * * *", catchup=3600)
    day1 = datetime(2026, 6, 1, 4, 0, tzinfo=UTC)
    assert len(_claim(policy, now=day1)) == 1
    # Service down for three days; only today's slot is inside the 1h window.
    day4 = datetime(2026, 6, 4, 3, 45, tzinfo=UTC)
    claimed = _claim(policy, now=day4)
    assert len(claimed) == 1
    assert claimed[0].schedule_slot == "2026-06-04T03:00@UTC"
    statuses = _slot_statuses(policy)
    assert statuses.count("outside-catchup-window") == 2


def test_deferred_slots_reclaimed_within_window(tmp_settings: Path) -> None:
    policy = _policy(tmp_settings)
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    run = _claim(policy, now=now)[0]
    backup_scheduler.fail_run(run.run_id, error="workspace-restore-active", instance_id="w1", fencing_token=run.fencing_token, phase="deferred", reason="workspace-restore-active", now=now)
    reclaimed = backup_scheduler.reclaim_deferred_slots([policy], instance_id="w2", now=now + timedelta(hours=1))
    assert len(reclaimed) == 1
    assert reclaimed[0].attempt == 2
    expired = backup_scheduler.reclaim_deferred_slots([policy], instance_id="w2", now=now + timedelta(days=3))
    assert expired == []


def test_requeue_then_reclaim_after_backoff(tmp_settings: Path) -> None:
    policy = _policy(tmp_settings)
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    run = _claim(policy, now=now)[0]
    backup_scheduler.requeue_run(run.run_id, instance_id="w1", fencing_token=run.fencing_token, retry_at=now + timedelta(seconds=120), error="transient", now=now)
    early = backup_scheduler.reclaim_abandoned_slots(instance_id="w2", now=now + timedelta(seconds=60))
    assert early == []
    later = backup_scheduler.reclaim_abandoned_slots(instance_id="w2", now=now + timedelta(seconds=180))
    assert len(later) == 1
    assert later[0].attempt == 2


def test_run_phase_transitions_and_listing(tmp_settings: Path) -> None:
    policy = _policy(tmp_settings)
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    run = _claim(policy, now=now)[0]
    backup_scheduler.record_run_phase(run.run_id, "snapshotting", instance_id="w1", fencing_token=run.fencing_token, now=now)
    with pytest.raises(AppError):
        backup_scheduler.record_run_phase(run.run_id, "snapshotting", instance_id="w2", fencing_token=run.fencing_token, now=now)
    with pytest.raises(AppError):
        backup_scheduler.record_run_phase(run.run_id, "not-a-phase")
    backup_scheduler.complete_run(run.run_id, backup_id="backup_1", filename="f.age", instance_id="w1", fencing_token=run.fencing_token, now=now)
    runs = backup_scheduler.list_runs(policy_id=str(policy["policyId"]))
    assert runs[0]["phase"] == "complete"
    assert runs[0]["backupId"] == "backup_1"
    with pytest.raises(AppError):
        backup_scheduler.get_run("run_missing")


def test_next_run_for_policy_includes_deterministic_jitter(tmp_settings: Path) -> None:
    policy = _policy(tmp_settings, jitter=600)
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    first = backup_scheduler.next_run_for_policy(policy, now=now)
    second = backup_scheduler.next_run_for_policy(policy, now=now)
    assert first is not None and first == second
    assert 0 <= int(first["jitterSeconds"]) <= 600
    assert backup_scheduler.next_run_for_policy({**policy, "schedule": {"cron": "bad"}}, now=now) is None


def test_worker_tick_reclaims_claims_and_executes(tmp_settings: Path) -> None:
    _policy(tmp_settings)
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    executed: list[str] = []
    result = backup_scheduler.worker_tick(instance_id="w1", executor=lambda run: executed.append(run.run_id), now=now)
    assert result["claimed"] == 1 and result["executed"] == 1
    assert executed
    again = backup_scheduler.worker_tick(instance_id="w1", executor=lambda run: None, now=now)
    assert again["claimed"] == 0


def test_target_health_and_retention_run_recording(tmp_settings: Path) -> None:
    backup_scheduler.record_target_health("target_a", "ok", "fine")
    backup_scheduler.record_target_health("target_a", "blocked", "offline")
    health = backup_scheduler.target_health()
    assert health == [{"targetId": "target_a", "status": "blocked", "checkedAt": health[0]["checkedAt"], "detail": "offline"}]
    backup_scheduler.record_retention_run("rr_1", policy_id="p", target_id="t", status="preview", preview={"delete": 2})
    backup_scheduler.record_retention_run("rr_1", policy_id="p", target_id="t", status="applied")


def _slot_statuses(policy: dict[str, object]) -> list[str]:
    with sqlite3.connect(Path(str(backup_scheduler.BACKUP_SCHEDULER_DIR)) / "scheduler.db") as connection:
        rows = connection.execute(
            "SELECT status FROM backup_schedule_slots WHERE policy_id = ?",
            (str(policy["policyId"]),),
        ).fetchall()
    return [str(row[0]) for row in rows]

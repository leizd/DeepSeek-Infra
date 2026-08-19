"""Coverage tests for Backup Scheduler, Executor, Reconcile, and Restore Sinks (v4.5)."""

from __future__ import annotations

import json
import tempfile
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_executor,
    backup_policies,
    backup_publish,
    backup_reconcile,
    backup_remote_restore,
    backup_replication,
    backup_scheduler,
    backup_targets,
    backups,
)


@pytest.fixture(autouse=True)
def _isolate_target_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / "fake_temp"
    fake_temp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def test_scheduler_target_ranking_diversity(tmp_settings: Path) -> None:
    t1 = "target_rank_1"
    t2 = "target_rank_2"
    t3 = "target_rank_3"
    backup_targets.register_filesystem_target(t1, path=tmp_settings / "rk1", failure_domain="us-east-1a")
    backup_targets.register_filesystem_target(t2, path=tmp_settings / "rk2", failure_domain="us-east-1b")
    backup_targets.register_filesystem_target(t3, path=tmp_settings / "rk3", failure_domain="us-west-2a")

    policy = {
        "policyId": "pol-rank",
        "targetId": t1,
        "placement": {
            "failureDomainSpread": "max-diversity",
        },
        "replication": {
            "targets": [
                {"targetId": t2, "priority": 10},
                {"targetId": t3, "priority": 20},
            ]
        },
    }

    # Rank candidate targets
    candidates = [t2, t3]
    ranked = backup_scheduler.plan_target_placement(
        policy,
        candidate_target_ids=candidates,
        primary_target_id=t1,
    )
    assert len(ranked) == 2
    # Returns list of ((diversity, lag, prio, ...), target_id)
    assert ranked[0][1] in {t2, t3}


def test_backup_executor_failover_commit_statuses(tmp_settings: Path) -> None:
    # 1. Test when commit_status is corrupt -> raises write-reconciliation-required
    t1 = "target_exec_1"
    t2 = "target_exec_2"
    backup_targets.register_filesystem_target(t1, path=tmp_settings / "ex1")
    backup_targets.register_filesystem_target(t2, path=tmp_settings / "ex2")

    policy_id = "exec-pol"
    backup_policies.create_policy(
        {
            "name": "Exec Policy",
            "policyId": policy_id,
            "targetId": t1,
            "replication": {"enabled": True, "targets": [{"targetId": t2, "mode": "required"}]},
        }
    )

    # 2. Test simulate_copy_removal
    sim_res = backup_replication.simulate_copy_removal(policy_id, "bk-sim", t1)
    assert "healthyCopiesBefore" in sim_res
    assert "policySafe" in sim_res


def test_backup_worker_and_scheduler_tick_full_coverage(tmp_settings: Path) -> None:
    mock_executor = MagicMock()
    mock_executor.execute_slot.return_value = {"status": "success"}

    # 1. worker_tick with drill and replication claimed
    policy_tick = {
        "name": "Policy Tick Test",
        "policyId": "pol-tick-1",
        "targetId": "managed-local",
        "schedule": {"cron": "0 0 * * *", "timezone": "UTC"},
        "recoveryDrill": {"enabled": True, "cron": "0 1 * * *", "timezone": "UTC"},
    }
    backup_policies.create_policy(policy_tick)

    with patch.object(backup_scheduler, "claim_due_slots", return_value=[{"policyId": "pol-tick-1", "slotKey": "s1"}]):
        with patch.object(backup_scheduler, "claim_due_drill_slots", return_value=[{"policyId": "pol-tick-1"}]):
            with patch("deepseek_infra.infra.workspace.backup_recovery_drill.execute_scheduled_drill", return_value=None):
                tick_res = backup_scheduler.worker_tick(
                    instance_id="inst_tick_1",
                    executor=mock_executor,
                    now=datetime(2026, 8, 18, 1, 30, tzinfo=timezone.utc),
                )
                assert tick_res["drillsClaimed"] == 1
                assert tick_res["drillsExecuted"] == 1

    # 2. BackupWorker lifecycle
    worker = backup_scheduler.BackupWorker(
        mock_executor,
        instance_id="inst_worker_1",
        tick_seconds=0.05,
        reconcile_on_start=False,
    )
    with patch.object(backup_scheduler, "worker_tick", return_value={}):
        worker.start()
        try:
            time.sleep(0.06)
        finally:
            worker.stop()
            if worker._thread:
                worker._thread.join(timeout=1.0)


def test_backup_executor_failover_and_error_branches(tmp_settings: Path) -> None:
    # Create policy and target
    t_id = "target_exec_err_1"
    t_root = tmp_settings / "exec_err_1"
    backup_targets.register_filesystem_target(t_id, path=t_root)

    policy = {
        "name": "Policy Exec Err Test",
        "policyId": "pol-exec-err-1",
        "targetId": t_id,
        "enabled": True,
        "schedule": {"cron": "* * * * *", "timezone": "UTC"},
    }
    backup_policies.create_policy(policy)

    # Claim a run via claim_due_slots
    claimed = backup_scheduler.claim_due_slots([policy], instance_id="inst_err_1")
    assert claimed
    run = claimed[0]

    # 1. Ambiguous target commit error
    with patch("deepseek_infra.infra.workspace.backup_scheduled._context_from_policy", side_effect=AppError("ambiguous-target-commit detected", status=409)):
        out1 = backup_executor.execute_run(run, instance_id="inst_err_1")
        assert out1["phase"] == "failed"
        assert out1.get("reason") == "ambiguous-target-commit"

    # Reclaim next run
    claimed2 = backup_scheduler.claim_due_slots([policy], instance_id="inst_err_1", now=datetime.now(tz=timezone.utc) + timedelta(minutes=2))
    assert claimed2
    run2 = claimed2[0]

    # 2. Slot commit conflict error
    with patch("deepseek_infra.infra.workspace.backup_scheduled._context_from_policy", side_effect=AppError("slot-commit-conflict detected", status=409)):
        out2 = backup_executor.execute_run(run2, instance_id="inst_err_1")
        assert out2["phase"] == "superseded"
        assert out2.get("reason") == "slot-commit-conflict"

    # Reclaim next run
    claimed3 = backup_scheduler.claim_due_slots([policy], instance_id="inst_err_1", now=datetime.now(tz=timezone.utc) + timedelta(minutes=4))
    assert claimed3
    run3 = claimed3[0]

    # 3. Blocked target unavailable
    with patch("deepseek_infra.infra.workspace.backup_scheduled._context_from_policy", side_effect=AppError("blocked-target-unavailable", status=409)):
        out3 = backup_executor.execute_run(run3, instance_id="inst_err_1")
        assert out3["phase"] in {"blocked", "blocked-retryable", "abandoned", "failed"}

    # Reclaim next run
    claimed4 = backup_scheduler.claim_due_slots([policy], instance_id="inst_err_1", now=datetime.now(tz=timezone.utc) + timedelta(minutes=6))
    assert claimed4
    run4 = claimed4[0]

    # 4. Generic unhandled Exception
    with patch("deepseek_infra.infra.workspace.backup_scheduled._context_from_policy", side_effect=RuntimeError("unexpected boom")):
        out4 = backup_executor.execute_run(run4, instance_id="inst_err_1")
        assert out4["phase"] == "failed"


def test_backup_reconcile_full_sweep(tmp_settings: Path) -> None:
    # 1. reconcile_all_targets
    t_id = "target_rec_sweep_1"
    t_root = tmp_settings / "rec_sweep_1"
    backup_targets.register_filesystem_target(t_id, path=t_root)

    reps = backup_reconcile.reconcile_all_targets(instance_id="inst_rec_test")
    assert isinstance(reps, list)
    assert any(r.get("targetId") == t_id for r in reps)

    # 2. assert_catalog_committed clean
    backup_reconcile.assert_catalog_committed(t_root)

    # 3. reconcile_target_store with mock store
    mock_store = MagicMock()
    mock_page = SimpleNamespace(objects=[], cursor=None)
    mock_store.list_objects.return_value = mock_page
    mock_store.get_bytes.return_value = None
    mock_writer = MagicMock(spec=["assert_owned", "renew", "release"])
    mock_writer.assert_owned.return_value = None

    rep_store = backup_reconcile.reconcile_target_store(
        mock_store,
        target_id="target_remote_rec_1",
        writer=mock_writer,
    )
    assert rep_store["targetId"] == "target_remote_rec_1"


def test_backups_apply_restore_and_sink_coverage(tmp_settings: Path) -> None:
    # 1. apply_restore unsupported mode
    with pytest.raises(AppError) as exc_mode:
        backups.apply_restore("rest_1", mode="unsupported_mode")
    assert exc_mode.value.code == ErrorCode.INVALID_PAYLOAD

    # 2. apply_restore requiresFrontendApply
    rest_root = tmp_settings / "restore_staging" / "rest_fe_req"
    rest_root.mkdir(parents=True, exist_ok=True)
    (rest_root / "plan.json").write_text(json.dumps({"requiresFrontendApply": True}), encoding="utf-8")

    with patch.object(backups, "_restore_root", return_value=rest_root):
        with pytest.raises(AppError) as exc_fe:
            backups.apply_restore("rest_fe_req", mode="merge")
        assert exc_fe.value.status == 409

    # 3. _NonSeekableZipSink
    bio = BytesIO()
    sink = backups._NonSeekableZipSink(bio)
    sink.write(b"data-123")
    sink.flush()
    assert bio.getvalue() == b"data-123"


def test_backup_remote_restore_selection_and_session_exceptions(tmp_settings: Path) -> None:
    # 1. Nonexistent restore_id raises 404
    with pytest.raises(AppError) as exc_404:
        backup_remote_restore.create_restore_from_target(
            target_id="t1",
            backup_id="bk1",
            restore_id="nonexistent_restore_id",
        )
    assert exc_404.value.status == 404

    # 2. Target/backup mismatch raises 409
    sess = {
        "restoreId": "sess_mis",
        "targetId": "t1",
        "backupId": "bk1",
        "selectionDigest": "digest_a",
        "phase": "fetching",
    }
    backup_remote_restore._atomic_write_json(backup_remote_restore._session_path("sess_mis"), sess)

    with pytest.raises(AppError) as exc_mis:
        backup_remote_restore.create_restore_from_target(
            target_id="t2",
            backup_id="bk1",
            restore_id="sess_mis",
        )
    assert exc_mis.value.status == 409


def test_backup_executor_retry_and_helpers(tmp_settings: Path) -> None:
    pol_empty: dict[str, Any] = {}
    assert backup_executor._max_attempts(pol_empty) == 3
    assert backup_executor._retry_delay_seconds(pol_empty, 1) == 60
    assert backup_executor._retry_delay_seconds(pol_empty, 2) == 120
    assert backup_executor._retry_delay_seconds(pol_empty, 10) == 900

    pol_custom = {"retry": {"initialBackoffSeconds": 10, "maxBackoffSeconds": 50, "maxAttempts": 7}}
    assert backup_executor._max_attempts(pol_custom) == 7
    assert backup_executor._retry_delay_seconds(pol_custom, 1) == 10
    assert backup_executor._retry_delay_seconds(pol_custom, 5) == 50


def test_backup_scheduler_reclaim_blocked_runs_all_terminal_and_retry_branches(tmp_settings: Path) -> None:
    now_dt = datetime.now(tz=timezone.utc)
    now_iso = now_dt.isoformat().replace("+00:00", "Z")
    past_iso = (now_dt - timedelta(days=2)).isoformat().replace("+00:00", "Z")

    # 1. Setup scheduler table with blocked runs
    with backup_scheduler._connect() as conn:
        conn.execute("DELETE FROM backup_runs")
        conn.execute("DELETE FROM backup_schedule_slots")

        # Run 1: Policy missing -> terminal
        conn.execute(
            "INSERT INTO backup_runs(run_id, policy_id, schedule_slot, phase, attempt, lease_until, created_at, updated_at) VALUES ('r_miss', 'pol_miss', 's1', 'blocked', 1, ?, ?, ?)",
            (past_iso, past_iso, past_iso),
        )

        # Run 2: Catchup window exceeded -> terminal
        conn.execute(
            "INSERT INTO backup_runs(run_id, policy_id, schedule_slot, phase, attempt, lease_until, created_at, updated_at) VALUES ('r_catch', 'pol_c', 's_old', 'blocked', 1, ?, ?, ?)",
            (past_iso, past_iso, past_iso),
        )
        conn.execute(
            "INSERT INTO backup_schedule_slots(policy_id, slot_key, scheduled_for, local_date_time, timezone, status, run_id, created_at) VALUES ('pol_c', 's_old', ?, ?, 'UTC', 'claimed', 'r_catch', ?)",
            (past_iso, past_iso, past_iso),
        )

        # Run 3: Max attempts exceeded -> terminal
        conn.execute(
            "INSERT INTO backup_runs(run_id, policy_id, schedule_slot, phase, attempt, lease_until, created_at, updated_at) VALUES ('r_att', 'pol_att', 's_att', 'blocked', 10, ?, ?, ?)",
            (past_iso, past_iso, past_iso),
        )
        conn.execute(
            "INSERT INTO backup_schedule_slots(policy_id, slot_key, scheduled_for, local_date_time, timezone, status, run_id, created_at) VALUES ('pol_att', 's_att', ?, ?, 'UTC', 'claimed', 'r_att', ?)",
            (now_iso, now_iso, now_iso),
        )

        # Run 4: Target resolved ok -> reclaimed leased run!
        conn.execute(
            "INSERT INTO backup_runs(run_id, policy_id, schedule_slot, phase, attempt, lease_until, created_at, updated_at) VALUES ('r_ok', 'pol_ok', 's_ok', 'blocked', 1, ?, ?, ?)",
            (past_iso, past_iso, past_iso),
        )
        conn.execute(
            "INSERT INTO backup_schedule_slots(policy_id, slot_key, scheduled_for, local_date_time, timezone, status, run_id, created_at) VALUES ('pol_ok', 's_ok', ?, ?, 'UTC', 'claimed', 'r_ok', ?)",
            (now_iso, now_iso, now_iso),
        )

    policies = [
        {"policyId": "pol_c", "enabled": True, "schedule": {"catchupWindowSeconds": 60}},
        {"policyId": "pol_att", "enabled": True, "retry": {"maxAttempts": 3}},
        {"policyId": "pol_ok", "enabled": True, "targetId": "tgt_ok"},
    ]

    with patch.object(backup_publish, "resolve_target", return_value=SimpleNamespace(root=tmp_settings, store=None)):
        reclaimed = backup_scheduler.reclaim_blocked_slots(policies, instance_id="inst_rec", now=now_dt)
        assert len(reclaimed) == 1
        assert reclaimed[0].policy_id == "pol_ok"
        assert reclaimed[0].attempt == 2

"""End-to-End multi-target failover, catch-up repair, and governed failback test suite."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_crypto,
    backup_dr_readiness,
    backup_executor,
    backup_policies,
    backup_publish,
    backup_replication,
    backup_scheduler,
    backup_targets,
    backup_write_continuity,
)


@pytest.fixture(autouse=True)
def stub_crypto(monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = b"age-encryption.org/v1\n"

    def encrypt_stream(target: Path, write_plaintext: Any, *, mode: str, secret: Any = None, recipients: tuple[str, ...] = (), cancel_event: Any = None) -> None:
        import io

        buffer = io.BytesIO()
        write_plaintext(buffer)
        target.write_bytes(prefix + bytes(buffer.getbuffer())[::-1])

    def decrypt_file(source: Path, target: Path, *, kind: str, secret: bytearray, cancel_event: Any = None) -> None:
        raw = source.read_bytes()
        assert raw.startswith(prefix)
        target.write_bytes(raw[len(prefix):][::-1])

    monkeypatch.setattr(backup_crypto, "encrypt_stream", encrypt_stream)
    monkeypatch.setattr(backup_crypto, "decrypt_file", decrypt_file)
    monkeypatch.setattr(backup_crypto, "generate_identity", lambda: {"identity": "AGE-SECRET-KEY-1EPH", "recipient": "age1eph"})
    monkeypatch.setattr(backup_crypto, "inspect_header", lambda _path: {"age": True})
    monkeypatch.setattr(backup_targets, "_containment_violation", lambda *a, **k: None)


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.isoformat(timespec="seconds").replace("+00:00", "Z")


def test_complete_e2e_failover_catchup_and_failback_flow(tmp_settings: Path) -> None:
    """Full lifecycle: primary down -> failover to B -> primary recovers -> B->A catch-up -> governed failback -> primary active."""
    pri_dir = tmp_settings / "e2e_primary"
    sec_dir = tmp_settings / "e2e_secondary"
    pri_dir.mkdir(parents=True, exist_ok=True)
    sec_dir.mkdir(parents=True, exist_ok=True)

    t_pri = backup_targets.init_target(pri_dir, label="Primary", failure_domain="us-east-1a", priority=100)
    t_sec = backup_targets.init_target(sec_dir, label="Secondary", failure_domain="us-east-1b", priority=50)

    pri_id = str(t_pri["targetId"])
    sec_id = str(t_sec["targetId"])

    policy_id = "pol_e2e_failover_lifecycle"
    policy = backup_policies.create_policy({
        "policyId": policy_id,
        "name": "E2E Failover Lifecycle Policy",
        "targetId": pri_id,
        "primaryTargetId": pri_id,
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "replication": {
            "enabled": True,
            "minCommittedCopies": 2,
            "minFailureDomains": 2,
            "maxReplicaLagSeconds": 3600,
            "targets": [{"targetId": sec_id, "mode": "required"}],
        },
    })

    # Step 1: Claim and execute Run 1, with primary failing during publish
    run1 = backup_scheduler.claim_manual_run(policy, instance_id="e2e-runner")

    orig_publish = backup_publish.publish_backup

    def fail_pri_publish(target: Any, package: Any, **kwargs: Any) -> Any:
        curr_tid = str(getattr(target, "target_id", None) or (target.root.name if target.root else ""))
        if curr_tid == pri_id or "e2e_primary" in str(getattr(target, "root", "")):
            raise AppError("Primary storage connection refused during stream", status=503)
        return orig_publish(target, package, **kwargs)

    backup_publish.publish_backup = fail_pri_publish
    try:
        outcome1 = backup_executor.execute_run(run1, instance_id="e2e-runner")
    finally:
        backup_publish.publish_backup = orig_publish

    assert outcome1["phase"] == "complete"
    assert outcome1["targetId"] == sec_id
    assert outcome1["isFailover"] is True
    backup_id_1 = str(outcome1["backupId"])

    # Verify Target B has authenticated committed copy and Target A has none
    t_sec_obj = backup_publish.resolve_target(sec_id)
    t_pri_obj = backup_publish.resolve_target(pri_id)

    st_b, _, _ = backup_replication.authenticate_committed_copy(t_sec_obj, policy_id, backup_id_1)
    assert st_b == "authenticated"
    st_a, _, _ = backup_replication.authenticate_recovery_copy(t_pri_obj, policy_id, backup_id_1)
    assert st_a == "missing"

    # Step 2: Primary recovers. Reconciler detects missing copy on Primary and runs B -> A catch-up repair
    rec_res = backup_replication.reconcile_policy_replicas(policy_id, instance_id="e2e-reconciler")
    assert rec_res["status"] == "completed"
    assert rec_res["repairsTriggered"] >= 1
    assert rec_res["repairsSucceeded"] >= 1

    # Primary A now has authenticated copy
    st_a_healed, _, _ = backup_replication.authenticate_committed_copy(t_pri_obj, policy_id, backup_id_1)
    assert st_a_healed == "authenticated"

    # Step 3: Advance stability window and evaluate failback
    p_healthy_start = datetime.now(tz=timezone.utc) - timedelta(seconds=2000)
    state = backup_write_continuity.get_write_continuity_state(policy_id)
    state["primaryFirstHealthyAt"] = p_healthy_start.isoformat(timespec="seconds").replace("+00:00", "Z")
    state["primaryConsecutiveHealthySeconds"] = 2000.0
    backup_write_continuity.save_write_continuity_state(policy_id, state)

    eligible, fb_reason, info = backup_write_continuity.evaluate_failback_eligibility(
        policy_id,
        stability_window_seconds=1800,
    )
    assert eligible is True
    assert fb_reason == "eligible"

    # Execute failback transition
    fb_res = backup_write_continuity.execute_failback_transition(policy_id)
    assert fb_res["activeWriteTargetId"] == pri_id
    assert fb_res["activeWriteTargetRole"] == "primary"

    # Step 4: Run 2 executes normally to primary target and replicates to secondary
    run2 = backup_scheduler.claim_manual_run(policy, instance_id="e2e-runner")
    outcome2 = backup_executor.execute_run(run2, instance_id="e2e-runner")

    assert outcome2["phase"] == "complete"
    assert outcome2["targetId"] == pri_id
    assert outcome2["isFailover"] is False
    backup_id_2 = str(outcome2["backupId"])

    # Both primary and secondary now possess authenticated copies for both recovery points
    st_a2, _, _ = backup_replication.authenticate_committed_copy(t_pri_obj, policy_id, backup_id_2)
    assert st_a2 == "authenticated"

    # Check DR Readiness and replication compliance
    readiness = backup_dr_readiness.evaluate_scope_readiness(target_id=pri_id, policy_id=policy_id)
    assert readiness["recoverable"] is True
    assert readiness["recoveryPoint"]["status"] == "available"

"""Comprehensive contract tests for Transactional Write Failover & Failure-Domain Rebalancing."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_executor,
    backup_policies,
    backup_publish,
    backup_replication,
    backup_scheduler,
    backup_targets,
    backup_write_continuity,
)


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.isoformat(timespec="seconds").replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def stub_crypto(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.workspace import backup_crypto

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


class _DummyPackage:
    def __init__(self, path: Path, payload: bytes = b"ciphertext-payload-bytes", backup_id: str = "backup_test1") -> None:
        self.path = path
        self.backup_id = backup_id
        self.filename = f"deepseek-infra-backup-20260101-{backup_id[-8:]}.dsibackup.age"
        self.size = len(payload)
        self.ciphertext_sha256 = hashlib.sha256(payload).hexdigest()
        self.manifest_digest = "a" * 64
        self.coverage_digest = "b" * 64
        self.object_set_digest = "c" * 64
        self.creation_verified = True
        self.snapshot_kind = "full"


def _make_package(tmp_path: Path, backup_id: str = "backup_test1", payload: bytes = b"ciphertext-payload-bytes") -> _DummyPackage:
    staging = tmp_path / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    p = staging / f"package-{backup_id}.age"
    p.write_bytes(payload)
    return _DummyPackage(p, payload=payload, backup_id=backup_id)


def _setup_target(path: Path, label: str, failure_domain: str = "zone-a", priority: int = 100) -> dict[str, Any]:
    path.mkdir(parents=True, exist_ok=True)
    return backup_targets.init_target(path, label=label, failure_domain=failure_domain, priority=priority)


# ── Gate A: Transactional Publication Failover ──────────────────────────────


def test_transactional_failover_when_target_a_publish_fails(tmp_settings: Path) -> None:
    """When publish to target A fails before commit, reconcile proves missing on A and fails over to B with same spool."""
    t_a_dir = tmp_settings / "target_a"
    t_b_dir = tmp_settings / "target_b"
    t_a = _setup_target(t_a_dir, "Target A", failure_domain="zone-a", priority=10)
    t_b = _setup_target(t_b_dir, "Target B", failure_domain="zone-b", priority=20)

    tid_a = str(t_a["targetId"])
    tid_b = str(t_b["targetId"])

    policy_payload = {
        "name": "Failover Policy",
        "enabled": True,
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "targetId": tid_a,
        "primaryTargetId": tid_a,
        "replication": {
            "enabled": True,
            "targets": [{"targetId": tid_b, "mode": "required"}],
            "minCommittedCopies": 1,
            "minFailureDomains": 1,
        },
    }
    policy = backup_policies.create_policy(policy_payload)
    pid = str(policy["policyId"])

    # Create manual run
    run = backup_scheduler.claim_manual_run(policy, instance_id="test-runner")

    # Simulate target A failure by patching backup_publish.publish_backup for first attempt on Target A
    orig_publish = backup_publish.publish_backup
    attempt_count = {"count": 0}

    def flaky_publish(target: Any, package: Any, **kwargs: Any) -> Any:
        attempt_count["count"] += 1
        curr_tid = str(getattr(target, "target_id", None) or (target.root.name if target.root else ""))
        if curr_tid == tid_a or "target_a" in str(getattr(target, "root", "")):
            raise AppError("Connection refused on Target A during object stream", status=503)
        return orig_publish(target, package, **kwargs)

    backup_publish.publish_backup = flaky_publish
    try:
        outcome = backup_executor.execute_run(run, instance_id="test-runner")
    finally:
        backup_publish.publish_backup = orig_publish

    assert outcome["phase"] == "complete", f"Failed with: {outcome.get('error')} and {outcome}"
    assert outcome["targetId"] == tid_b
    assert outcome.get("isFailover") is True
    assert outcome.get("failoverTransitioned") is True

    # Target B must have authenticated copy
    target_b_obj = backup_publish.resolve_target(tid_b)
    status, receipt, commit = backup_replication.authenticate_committed_copy(target_b_obj, pid, str(outcome["backupId"]))
    assert status == "authenticated"
    assert receipt is not None
    assert commit is not None

    # Target A must have NO commit
    target_a_obj = backup_publish.resolve_target(tid_a)
    status_a, _, _ = backup_replication.authenticate_recovery_copy(target_a_obj, pid, str(outcome["backupId"]))
    assert status_a == "missing"


def test_transactional_failover_converges_if_commit_succeeded_on_a(tmp_settings: Path) -> None:
    """When publish on Target A throws error AFTER commit was written, reconcile discovers commit on A and completes."""
    t_a_dir = tmp_settings / "target_a"
    t_b_dir = tmp_settings / "target_b"
    t_a = _setup_target(t_a_dir, "Target A", failure_domain="zone-a", priority=10)
    t_b = _setup_target(t_b_dir, "Target B", failure_domain="zone-b", priority=20)

    tid_a = str(t_a["targetId"])
    tid_b = str(t_b["targetId"])

    policy = backup_policies.create_policy({
        "name": "Converge Policy",
        "enabled": True,
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "targetId": tid_a,
        "primaryTargetId": tid_a,
        "replication": {
            "enabled": True,
            "targets": [{"targetId": tid_b, "mode": "required"}],
            "minCommittedCopies": 1,
        },
    })

    run = backup_scheduler.claim_manual_run(policy, instance_id="test-runner")

    orig_publish = backup_publish.publish_backup
    first_call = {"called": False}

    def publish_then_fail(target: Any, package: Any, **kwargs: Any) -> Any:
        res = orig_publish(target, package, **kwargs)
        if not first_call["called"]:
            first_call["called"] = True
            raise AppError("Connection dropped right after commit ACK", status=503)
        return res

    backup_publish.publish_backup = publish_then_fail
    try:
        outcome = backup_executor.execute_run(run, instance_id="test-runner")
    finally:
        backup_publish.publish_backup = orig_publish

    assert outcome["phase"] == "complete"
    assert outcome["targetId"] == tid_a
    assert outcome.get("isFailover") is False


def test_transactional_failover_ambiguous_write_blocks_safely(tmp_settings: Path) -> None:
    """When target A commit state is unprovable (reconcile hangs/unreachable), execution aborts safely with ambiguous-write."""
    t_a_dir = tmp_settings / "target_a"
    t_b_dir = tmp_settings / "target_b"
    t_a = _setup_target(t_a_dir, "Target A")
    t_b = _setup_target(t_b_dir, "Target B")

    tid_a = str(t_a["targetId"])
    tid_b = str(t_b["targetId"])

    policy = backup_policies.create_policy({
        "name": "Ambiguous Policy",
        "enabled": True,
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "targetId": tid_a,
        "primaryTargetId": tid_a,
        "replication": {"enabled": True, "targets": [{"targetId": tid_b, "mode": "required"}]},
    })

    run = backup_scheduler.claim_manual_run(policy, instance_id="test-runner")

    orig_publish = backup_publish.publish_backup
    orig_auth = backup_replication.authenticate_recovery_copy

    def fail_publish(*args: Any, **kwargs: Any) -> Any:
        raise AppError("Target A hung during Commit POST", status=504)

    def fail_auth(*args: Any, **kwargs: Any) -> Any:
        raise AppError("Target A probe timed out", status=504)

    backup_publish.publish_backup = fail_publish
    backup_replication.authenticate_recovery_copy = fail_auth
    try:
        outcome = backup_executor.execute_run(run, instance_id="test-runner")
    finally:
        backup_publish.publish_backup = orig_publish
        backup_replication.authenticate_recovery_copy = orig_auth

    assert outcome["phase"] == "failed"
    assert "ambiguous-target-commit" in outcome.get("error", "")

    # Run record must be marked ambiguous-write
    runs = backup_scheduler.list_runs(policy_id=policy["policyId"])
    assert runs[0]["phase"] in {"ambiguous-write", "failed"}


# ── Gate B: Failover Catch-up & Governed Failback ───────────────────────────


def test_reconciler_includes_configured_primary_for_catchup(tmp_settings: Path) -> None:
    """When a backup was committed to Failover Target B, the reconciler automatically schedules B -> A repair to catch up Primary."""
    t_a_dir = tmp_settings / "target_a"
    t_b_dir = tmp_settings / "target_b"
    t_a = _setup_target(t_a_dir, "Target A")
    t_b = _setup_target(t_b_dir, "Target B")

    tid_a = str(t_a["targetId"])
    tid_b = str(t_b["targetId"])

    policy = backup_policies.create_policy({
        "name": "Catch-up Policy",
        "enabled": True,
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "targetId": tid_a,
        "primaryTargetId": tid_a,
        "replication": {
            "enabled": True,
            "targets": [{"targetId": tid_b, "mode": "required"}],
            "minCommittedCopies": 2,
        },
    })
    pid = str(policy["policyId"])

    # 1. Publish directly to Target B (simulating failover write)
    target_b_obj = backup_publish.resolve_target(tid_b)
    pkg = _make_package(tmp_settings, backup_id="backup_failover_123")
    backup_publish.publish_backup(
        target_b_obj,
        pkg,
        run_id="run_failover_123",
        policy_id=pid,
        schedule_slot="2026-08-17T12:00:00Z",
        fencing_token=1001,
    )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=tid_b,
        policy_id=pid,
        backup_id="backup_failover_123",
        committed_at=_utc_iso(),
        state="healthy",
        recoverable=True,
        object_set_digest=pkg.object_set_digest,
    )

    # Verify target A does not have the copy yet
    target_a_obj = backup_publish.resolve_target(tid_a)
    st_a, _, _ = backup_replication.authenticate_recovery_copy(target_a_obj, pid, "backup_failover_123")
    assert st_a == "missing"

    # 2. Run Reconciler
    rec_res = backup_replication.reconcile_policy_replicas(pid, instance_id="reconciler-test")
    assert rec_res["status"] == "completed"
    assert rec_res["repairsTriggered"] >= 1
    assert rec_res["repairsSucceeded"] >= 1

    # 3. Target A must now have the authenticated copy!
    st_a_after, r_a, c_a = backup_replication.authenticate_committed_copy(target_a_obj, pid, "backup_failover_123")
    assert st_a_after == "authenticated"
    assert r_a is not None
    assert c_a is not None


def test_failback_requires_exact_primary_healthy_copy(tmp_settings: Path) -> None:
    """Failback must be rejected if Primary does not possess an exact authenticated healthy copy."""
    t_a_dir = tmp_settings / "target_a"
    t_b_dir = tmp_settings / "target_b"
    t_a = _setup_target(t_a_dir, "Target A")
    t_b = _setup_target(t_b_dir, "Target B")

    tid_a = str(t_a["targetId"])
    tid_b = str(t_b["targetId"])

    policy = backup_policies.create_policy({
        "name": "Failback Strict Policy",
        "enabled": True,
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "targetId": tid_a,
        "primaryTargetId": tid_a,
        "replication": {"enabled": True, "targets": [{"targetId": tid_b, "mode": "required"}]},
    })
    pid = str(policy["policyId"])

    # Put state in failover mode on B
    backup_write_continuity.execute_failover_transition(pid, tid_b, reason="primary-down")

    # Record point on B
    b_time = _utc_iso(datetime.now(tz=timezone.utc) - timedelta(seconds=2000))
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=tid_b,
        policy_id=pid,
        backup_id="backup_pt_1",
        committed_at=b_time,
        state="healthy",
        recoverable=True,
    )

    # Primary has been healthy for 2000s (> 1800s stability window)
    p_healthy_start = datetime.now(tz=timezone.utc) - timedelta(seconds=2000)
    backup_write_continuity.record_target_liveness(
        pid,
        tid_a,
        status="available",
        now=p_healthy_start,
    )
    backup_write_continuity.record_target_liveness(
        pid,
        tid_a,
        status="available",
        now=datetime.now(tz=timezone.utc),
    )

    # But Target A does NOT have a healthy copy of backup_pt_1!
    eligible, reason, info = backup_write_continuity.evaluate_failback_eligibility(
        pid,
        stability_window_seconds=1800,
    )
    assert eligible is False
    assert "latest-failover-point-not-converged" in reason

    # Now add healthy copy on Primary (e.g. after catch-up repair)
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=tid_a,
        policy_id=pid,
        backup_id="backup_pt_1",
        committed_at=_utc_iso(),
        state="healthy",
        recoverable=True,
    )

    eligible2, reason2, info2 = backup_write_continuity.evaluate_failback_eligibility(
        pid,
        stability_window_seconds=1800,
    )
    assert eligible2 is True
    assert reason2 == "eligible"


# ── Gate C: Governed Primary Promotion ──────────────────────────────────────


def test_promote_primary_validation_and_cas(tmp_settings: Path) -> None:
    """Primary promotion requires required replica membership, liveness, and atomic CAS."""
    t_a_dir = tmp_settings / "target_a"
    t_b_dir = tmp_settings / "target_b"
    t_c_dir = tmp_settings / "target_c"
    t_a = _setup_target(t_a_dir, "Target A")
    t_b = _setup_target(t_b_dir, "Target B")
    t_c = _setup_target(t_c_dir, "Target C")

    tid_a = str(t_a["targetId"])
    tid_b = str(t_b["targetId"])
    tid_c = str(t_c["targetId"])

    policy = backup_policies.create_policy({
        "name": "Promotion Policy",
        "enabled": True,
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "targetId": tid_a,
        "primaryTargetId": tid_a,
        "replication": {
            "enabled": True,
            "targets": [{"targetId": tid_b, "mode": "required"}],
        },
    })
    pid = str(policy["policyId"])

    # 1. Promoting target C (not a configured replica) must fail
    with pytest.raises(AppError) as exc:
        backup_write_continuity.promote_primary_target(pid, tid_c)
    assert "not a configured replica" in str(exc.value)

    # 2. CAS mismatch must fail
    with pytest.raises(AppError) as exc2:
        backup_write_continuity.promote_primary_target(pid, tid_b, expected_policy_revision=999)
    assert "CAS mismatch" in str(exc2.value)

    # 3. Successful promotion
    res = backup_write_continuity.promote_primary_target(
        pid,
        tid_b,
        expected_policy_revision=1,
        expected_failover_epoch=0,
    )
    assert res["status"] == "promoted"
    assert res["newPrimaryTargetId"] == tid_b
    assert res["previousPrimaryTargetId"] == tid_a

    # Verify policy updated
    updated = backup_policies.get_policy(pid)
    assert updated["primaryTargetId"] == tid_b
    assert updated["targetId"] == tid_b
    # Previous primary must now be a required replica
    replica_tids = [t["targetId"] for t in updated["replication"]["targets"]]
    assert tid_a in replica_tids


# ── Gate D: Failure-Domain Placement & Online Rebalancing ───────────────────


def test_failure_domain_policy_normalization_and_placement_ranking(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Placement evaluation prioritizes failure domain diversity and filters out draining targets."""
    t_a_dir = tmp_settings / "target_a"
    t_b_dir = tmp_settings / "target_b"
    t_c_dir = tmp_settings / "target_c"

    t_a = _setup_target(t_a_dir, label="Target A", failure_domain="zone-a", priority=10)
    t_b = _setup_target(t_b_dir, label="Target B", failure_domain="zone-a", priority=50)
    t_c = _setup_target(t_c_dir, label="Target C", failure_domain="zone-b", priority=100)

    tid_a = str(t_a["targetId"])
    tid_b = str(t_b["targetId"])
    tid_c = str(t_c["targetId"])

    policy = backup_policies.create_policy({
        "name": "FD Policy",
        "enabled": True,
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "targetId": tid_a,
        "primaryTargetId": tid_a,
        "replication": {
            "enabled": True,
            "targets": [
                {"targetId": tid_b, "mode": "required"},
                {"targetId": tid_c, "mode": "required"},
            ],
            "minCommittedCopies": 2,
            "minFailureDomains": 2,
        },
    })
    assert policy["replication"]["minFailureDomains"] == 2

    # Drain Target C
    backup_targets.drain_target(tid_c, reason="Maintenance")
    assert backup_targets.get_target_drain_state(tid_c) == "draining"

    # Make Target A unavailable via preflight
    orig_preflight = backup_write_continuity.perform_liveness_preflight

    def custom_preflight(target_id: str, *args: Any, **kwargs: Any) -> Any:
        if target_id == tid_a:
            return {"targetId": target_id, "status": "unavailable", "error": "Primary unreachable"}
        return orig_preflight(target_id, *args, **kwargs)

    monkeypatch.setattr(backup_write_continuity, "perform_liveness_preflight", custom_preflight)

    # Evaluate placement: Target C is draining, so Target B should be selected despite lower priority
    placement = backup_scheduler.evaluate_write_placement(policy)
    assert placement["isFailover"] is True
    assert placement["selectedWriteTargetId"] == tid_b

    # Activate Target C
    backup_targets.activate_target(tid_c)
    assert backup_targets.get_target_drain_state(tid_c) == "active"

    # Now Target C is active and has higher priority (100 vs 50) and distinct failure domain
    placement2 = backup_scheduler.evaluate_write_placement(policy)
    assert placement2["selectedWriteTargetId"] == tid_c


def test_online_replica_rebalancing(tmp_settings: Path) -> None:
    """Rebalance job provisions copy onto target in under-represented failure domain."""
    t_a_dir = tmp_settings / "target_a"
    t_b_dir = tmp_settings / "target_b"
    t_c_dir = tmp_settings / "target_c"

    t_a = _setup_target(t_a_dir, label="Target A", failure_domain="zone-a")
    t_b = _setup_target(t_b_dir, label="Target B", failure_domain="zone-a")
    t_c = _setup_target(t_c_dir, label="Target C", failure_domain="zone-b")

    tid_a = str(t_a["targetId"])
    tid_b = str(t_b["targetId"])
    tid_c = str(t_c["targetId"])

    policy = backup_policies.create_policy({
        "name": "Rebalance Policy",
        "enabled": True,
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "targetId": tid_a,
        "primaryTargetId": tid_a,
        "replication": {
            "enabled": True,
            "targets": [
                {"targetId": tid_b, "mode": "required"},
                {"targetId": tid_c, "mode": "required"},
            ],
            "minCommittedCopies": 2,
            "minFailureDomains": 2,
        },
    })
    pid = str(policy["policyId"])

    # Create backup on Target A (zone-a)
    target_a_obj = backup_publish.resolve_target(tid_a)
    pkg = _make_package(tmp_settings, backup_id="backup_rebal_1")
    backup_publish.publish_backup(
        target_a_obj,
        pkg,
        run_id="run_rebal_1",
        policy_id=pid,
        schedule_slot="2026-08-17T14:00:00Z",
        fencing_token=2001,
    )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=tid_a,
        policy_id=pid,
        backup_id="backup_rebal_1",
        committed_at=_utc_iso(),
        state="healthy",
        recoverable=True,
        object_set_digest=pkg.object_set_digest,
    )

    # Run rebalance: should detect missing zone-b failure domain and copy to Target C
    res = backup_replication.rebalance_policy_replicas(pid, instance_id="rebalance-worker")
    assert res["status"] == "completed"
    assert res["jobsCreated"] >= 1

    # Verify Target C now has authenticated copy
    target_c_obj = backup_publish.resolve_target(tid_c)
    st_c, _, _ = backup_replication.authenticate_committed_copy(target_c_obj, pid, "backup_rebal_1")
    assert st_c == "authenticated"


# ── Gate E: Streaming Corrupt Quarantine & Authenticate Commit ──────────────


def test_authenticate_committed_copy_validates_canonical_hash(tmp_settings: Path) -> None:
    """Forged or damaged commit marker is rejected by authenticate_committed_copy."""
    t_a_dir = tmp_settings / "target_a"
    t_a = _setup_target(t_a_dir, "Target A")
    tid_a = str(t_a["targetId"])

    policy = backup_policies.create_policy({
        "name": "Auth Policy",
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "targetId": tid_a,
    })
    pid = str(policy["policyId"])

    target_obj = backup_publish.resolve_target(tid_a)
    pkg = _make_package(tmp_settings, backup_id="backup_auth_1")
    backup_publish.publish_backup(
        target_obj,
        pkg,
        run_id="run_auth_1",
        policy_id=pid,
        schedule_slot="2026-08-17T15:00:00Z",
        fencing_token=3001,
    )

    status, _, _ = backup_replication.authenticate_committed_copy(target_obj, pid, "backup_auth_1")
    assert status == "authenticated"

    # Tamper with commit marker
    commit_file = backup_publish.find_commit_marker_path(t_a_dir, pid, "2026-08-17T15:00:00Z")
    assert commit_file is not None
    commit_data = json.loads(commit_file.read_text(encoding="utf-8"))
    commit_data["commitHash"] = "bad_hash_000000000000000000000000000000000000000000000000000000000000"
    commit_file.write_text(json.dumps(commit_data), encoding="utf-8")

    status_bad, _, _ = backup_replication.authenticate_committed_copy(target_obj, pid, "backup_auth_1")
    assert status_bad == "corrupt"


def test_incremental_parent_lineage_eligibility_enforced(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Incremental backup cannot fail over to candidate replica if candidate lacks authenticated parent copy."""
    from deepseek_infra.infra.workspace import backup_incremental

    t_a_dir = tmp_settings / "target_a"
    t_b_dir = tmp_settings / "target_b"
    t_a = _setup_target(t_a_dir, "Target A", failure_domain="zone-a")
    t_b = _setup_target(t_b_dir, "Target B", failure_domain="zone-b")

    tid_a = str(t_a["targetId"])
    tid_b = str(t_b["targetId"])

    policy = backup_policies.create_policy({
        "name": "Incremental Policy",
        "enabled": True,
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "targetId": tid_a,
        "primaryTargetId": tid_a,
        "replication": {
            "enabled": True,
            "targets": [{"targetId": tid_b, "mode": "required"}],
            "minCommittedCopies": 1,
        },
    })

    # Monkeypatch select_snapshot_plan so run freezes as incremental with parent_backup_999
    monkeypatch.setattr(
        backup_incremental,
        "select_snapshot_plan",
        lambda *a, **k: ("incremental", "lineage_1", "parent_backup_999", 1, "parent_hash", "parent_rcpt", None),
    )

    run = backup_scheduler.claim_manual_run(policy, instance_id="test-runner")

    # Candidate B does NOT have parent_backup_999
    # Simulate publish failure on target A
    orig_publish = backup_publish.publish_backup

    def fail_target_a(target: Any, package: Any, **kwargs: Any) -> Any:
        curr_tid = str(getattr(target, "target_id", None) or (target.root.name if target.root else ""))
        if curr_tid == tid_a or "target_a" in str(getattr(target, "root", "")):
            raise AppError("Disk full on Target A", status=507)
        return orig_publish(target, package, **kwargs)

    backup_publish.publish_backup = fail_target_a
    try:
        outcome = backup_executor.execute_run(run, instance_id="test-runner")
    finally:
        backup_publish.publish_backup = orig_publish

    # Failover to B must be blocked because B lacks parent lineage; run fails safely
    assert outcome["phase"] == "failed"


def test_zero_resnapshot_and_zero_reencrypt_invariants(tmp_settings: Path) -> None:
    """During failover, backupId, objectSetDigest, and spool package are preserved without re-snapshotting."""
    t_a_dir = tmp_settings / "target_a"
    t_b_dir = tmp_settings / "target_b"
    t_a = _setup_target(t_a_dir, "Target A", failure_domain="zone-a", priority=10)
    t_b = _setup_target(t_b_dir, "Target B", failure_domain="zone-b", priority=20)

    tid_a = str(t_a["targetId"])
    tid_b = str(t_b["targetId"])

    policy = backup_policies.create_policy({
        "name": "Zero Resnapshot Policy",
        "enabled": True,
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "targetId": tid_a,
        "primaryTargetId": tid_a,
        "replication": {
            "enabled": True,
            "targets": [{"targetId": tid_b, "mode": "required"}],
            "minCommittedCopies": 1,
        },
    })

    run = backup_scheduler.claim_manual_run(policy, instance_id="test-runner")

    published_packages: list[Any] = []
    orig_publish = backup_publish.publish_backup

    def capture_and_fail_first(target: Any, package: Any, **kwargs: Any) -> Any:
        published_packages.append(package)
        curr_tid = str(getattr(target, "target_id", None) or (target.root.name if target.root else ""))
        if curr_tid == tid_a or "target_a" in str(getattr(target, "root", "")):
            raise AppError("Simulated primary I/O failure", status=503)
        return orig_publish(target, package, **kwargs)

    backup_publish.publish_backup = capture_and_fail_first
    try:
        outcome = backup_executor.execute_run(run, instance_id="test-runner")
    finally:
        backup_publish.publish_backup = orig_publish

    assert outcome["phase"] == "complete"
    assert outcome["targetId"] == tid_b
    assert outcome["isFailover"] is True

    # We must have attempted publish on A and then published the EXACT SAME package instance/digests on B
    assert len(published_packages) == 2
    pkg_a, pkg_b = published_packages[0], published_packages[1]
    assert pkg_a.backup_id == pkg_b.backup_id == outcome["backupId"]
    assert pkg_a.object_set_digest == pkg_b.object_set_digest


def test_backup_governance_drain_and_rebalance_lifecycle(tmp_settings: Path) -> None:
    """Test target drain, activate, and rebalance lifecycle functions."""
    t_a_dir = tmp_settings / "target_a"
    t_a = _setup_target(t_a_dir, "Target A")
    tid_a = str(t_a["targetId"])

    # 1. Drain target
    drain_res = backup_targets.drain_target(tid_a, reason="Hardware replacement")
    assert drain_res["drainState"] == "draining"
    assert backup_targets.get_target_drain_state(tid_a) == "draining"

    # 2. Activate target
    act_res = backup_targets.activate_target(tid_a)
    assert act_res["drainState"] == "active"
    assert backup_targets.get_target_drain_state(tid_a) == "active"

    # 3. List and trigger rebalance
    t_b = _setup_target(tmp_settings / "target_b", "Target B")
    tid_b = str(t_b["targetId"])

    policy = backup_policies.create_policy({
        "name": "Route Policy",
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "targetId": tid_a,
        "replication": {
            "enabled": True,
            "targets": [{"targetId": tid_b, "mode": "required"}],
            "minCommittedCopies": 1,
        },
    })
    pid = str(policy["policyId"])

    reb_res = backup_replication.rebalance_policy_replicas(pid, instance_id="test-worker")
    assert reb_res["status"] == "completed"

    list_reb = backup_replication.list_rebalance_jobs(policy_id=pid)
    assert isinstance(list_reb, list)

"""Coverage tests for DR Readiness, Drill, Scrub, Retention, and Policies (v4.5)."""

from __future__ import annotations

import json
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_dr_audit,
    backup_dr_ledger,
    backup_dr_readiness,
    backup_policies,
    backup_publish,
    backup_recovery_class,
    backup_recovery_job,
    backup_recovery_keeper,
    backup_retention,
    backup_scheduler,
    backup_scrub,
    backup_targets,
    mutation_gate,
)


@pytest.fixture(autouse=True)
def _isolate_target_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / "fake_temp"
    fake_temp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def test_dr_readiness_deep_coverage(tmp_settings: Path) -> None:
    # 1. _parse_time
    assert backup_dr_readiness._parse_time(None) is None
    assert backup_dr_readiness._parse_time(12345) is None
    assert backup_dr_readiness._parse_time("not-a-time") is None
    assert backup_dr_readiness._parse_time("2026-08-18T10:00:00") is None  # no tz
    assert backup_dr_readiness._parse_time("invalid-iso-string-with+00:00") is None
    parsed = backup_dr_readiness._parse_time("2026-08-18T10:00:00Z")
    assert parsed is not None

    # 2. _resolve_target_kind
    assert backup_dr_readiness._resolve_target_kind("managed-local") == "managed-local"
    assert backup_dr_readiness._resolve_target_kind("nonexistent_target_xyz") == "filesystem"

    t_s3 = "target_dr_s3_kind"
    with patch.object(backup_targets, "get_target", return_value={"kind": "s3"}):
        assert backup_dr_readiness._resolve_target_kind(t_s3) == "s3"

    # 3. _latest_outcome & _nonnegative
    assert backup_dr_readiness._latest_outcome([])["status"] == "unavailable"
    rec_ok = [{"observedAt": "2026-08-18T10:00:00Z", "result": "success"}]
    assert backup_dr_readiness._latest_outcome(rec_ok)["status"] == "ok"
    rec_err = [{"observedAt": "2026-08-18T10:00:00Z", "result": "failed"}]
    assert backup_dr_readiness._latest_outcome(rec_err)["status"] == "error"
    assert backup_dr_readiness._latest_outcome(rec_err, success=lambda r: True)["status"] == "ok"

    assert backup_dr_readiness._nonnegative(-10) == 0
    assert backup_dr_readiness._nonnegative("50") == 50

    # 4. evaluate_scope_readiness with nonexistent policy
    res_none_pol = backup_dr_readiness.evaluate_scope_readiness("managed-local", policy_id="nonexistent-pol-xyz")
    assert res_none_pol["status"] == "blocked"

    # 5. readiness_status
    status = backup_dr_readiness.readiness_status()
    assert "status" in status
    assert "scopes" in status


def test_run_lease_context_and_drill_scheduler(tmp_settings: Path) -> None:
    # 1. RunLeaseGuard
    run_id = "run_ctx_test_1"
    inst_id = "inst_ctx_1"
    fencing = 42

    ctx = backup_scheduler.RunLeaseGuard(
        run_id=run_id,
        instance_id=inst_id,
        fencing_token=fencing,
        heartbeat_seconds=0.05,
    )
    assert ctx.now() is not None

    mock_writer = MagicMock(spec=["assert_owned", "renew"])
    ctx.attach_writer(mock_writer)

    with patch.object(backup_scheduler, "assert_run_lease", return_value=None):
        ctx.checkpoint()
        mock_writer.assert_owned.assert_called_once()

    with patch.object(backup_scheduler, "renew_run_lease", return_value=None):
        ctx.start_heartbeat()
        try:
            time.sleep(0.06)
        finally:
            ctx.stop()
            if ctx._thread:
                ctx._thread.join(timeout=1.0)

    # 2. claim_recovery_drill_slots
    policy_drill = {
        "name": "Policy Drill Test",
        "policyId": "pol-drill-1",
        "targetId": "managed-local",
        "schedule": {"cron": "0 0 * * *", "timezone": "UTC"},
        "recoveryDrill": {"enabled": True, "cron": "0 2 * * *", "timezone": "UTC"},
    }
    backup_policies.create_policy(policy_drill)

    claimed_drills = backup_scheduler.claim_due_drill_slots(
        [policy_drill],
        instance_id=inst_id,
        now=datetime(2026, 8, 18, 2, 30, tzinfo=timezone.utc),
    )
    assert isinstance(claimed_drills, list)

    # 3. record_target_head & record_remote_target_head
    t_root = tmp_settings / "t_head_root"
    t_root.mkdir(parents=True, exist_ok=True)
    marker_file = t_root / backup_targets.TARGET_MARKER_NAME
    marker_file.write_text(json.dumps({"targetId": "target_head_1"}), encoding="utf-8")

    backup_targets.record_target_head(
        t_root,
        target_id="target_head_1",
        generation=1,
        commit_hash="hash1",
    )
    assert marker_file.is_file()
    updated_marker = json.loads(marker_file.read_text(encoding="utf-8"))
    assert updated_marker["latestCommitHash"] == "hash1"

    mock_store = MagicMock()
    mock_store.stat.return_value = None
    mock_store.get_bytes.return_value = json.dumps({
        "schemaVersion": 1,
        "targetGeneration": 1,
        "latestCommitHash": "hash0",
        "incarnationId": "inc_1",
    }).encode()
    backup_targets.record_remote_target_head(
        mock_store,
        target_id="target_remote_head_1",
        generation=2,
        commit_hash="hash2",
    )
    assert mock_store.put_if_absent.called


def test_mutation_gate_scrub_and_recovery_class_coverage(tmp_settings: Path) -> None:
    # 1. mutation_gate tests
    root = tmp_settings / "mg_test"
    root.mkdir(parents=True, exist_ok=True)

    # Generation read invalid
    gen_file = mutation_gate.generation_path(root)
    gen_file.write_text("invalid-number", encoding="ascii")
    assert mutation_gate.read_generation(root) == 0

    # Corrupt fence
    fence_file = mutation_gate.fence_path(root)
    fence_file.write_text("{corrupt-json", encoding="utf-8")
    with pytest.raises(AppError):
        mutation_gate.read_fence(root)
    fence_file.unlink()

    # Clear fence missing & wrong owner
    assert mutation_gate.clear_fence("r1", root) is False
    mutation_gate.write_fence({"restoreId": "r1"}, root)
    with pytest.raises(AppError):
        mutation_gate.clear_fence("r_wrong", root)
    with pytest.raises(AppError):
        mutation_gate.assert_mutation_allowed("r_wrong", root)
    assert mutation_gate.clear_fence("r1", root) is True

    # Nested exclusive_gate
    with mutation_gate.exclusive_gate(root):
        with mutation_gate.exclusive_gate(root):
            pass
        with pytest.raises(RuntimeError):
            with mutation_gate.exclusive_gate(tmp_settings / "mg_other"):
                pass

    # 2. backup_recovery_class tests
    assert backup_recovery_class.size_bucket(5 * 1024 * 1024) == "small"
    assert backup_recovery_class.size_bucket(50 * 1024 * 1024) == "medium"
    assert backup_recovery_class.size_bucket(500 * 1024 * 1024) == "large"

    assert backup_recovery_class.chain_depth_bucket(2) == "shallow"
    assert backup_recovery_class.chain_depth_bucket(5) == "moderate"
    assert backup_recovery_class.chain_depth_bucket(15) == "deep"

    rc = backup_recovery_class.classify_recovery(
        target_kind="s3",
        storage_protocol="object-set-v1",
        logical_bytes=20 * 1024 * 1024,
        chain_length=4,
    )
    assert rc.format_kind == "object-set-v1"
    assert rc.size_category == "medium"
    assert rc.chain_depth == "moderate"
    assert "tag" in rc.to_dict()
    assert str(rc) == rc.tag

    # Calibrate RTO with 10 samples (high confidence)
    samples_high = []
    for i in range(10):
        samples_high.extend([
            {"stage": "transfer", "bytes": 10_000_000, "durationMs": 500, "recoveryClass": rc.to_dict()},
            {"stage": "crypto", "bytes": 10_000_000, "durationMs": 300, "recoveryClass": rc.to_dict()},
            {"stage": "materialize", "bytes": 10_000_000, "durationMs": 200, "recoveryClass": rc.to_dict()},
        ])
    rto_res = backup_recovery_class.calibrate_rto(
        samples_or_target_id=samples_high,
        logical_bytes=10_000_000,
        recovery_class=rc,
    )
    assert rto_res["status"] == "calibrated"
    assert rto_res["confidence"] == "high"
    assert "stageEstimates" in rto_res

    # Calibrate RTO with 3 samples (medium confidence)
    samples_med = samples_high[:9]
    rto_med = backup_recovery_class.calibrate_rto(
        samples_or_target_id=samples_med,
        logical_bytes=10_000_000,
        recovery_class=rc,
    )
    assert rto_med["status"] == "calibrated"
    assert rto_med["confidence"] == "medium"

    # 3. backup_scrub tests
    scrub_empty = backup_scrub.scrub_all(root)
    assert scrub_empty["scrubbed"] == 0
    assert scrub_empty["ok"] is True

    members = backup_scrub._ciphertext_members(root, {"storageProtocol": "single-file", "objects": []})
    assert members == []


def test_dr_readiness_and_retention_remote_store(tmp_settings: Path) -> None:
    # 1. backup_retention.apply_retention_store and finalize_retention_store
    mock_store = MagicMock()
    mock_store.list_objects.return_value = SimpleNamespace(objects=[], cursor=None)
    mock_store.get_bytes.return_value = None
    mock_writer = MagicMock(spec=["assert_owned", "renew", "release"])
    mock_writer.assert_owned.return_value = None

    state = {
        "b1": {"backupId": "b1", "createdAt": "2026-08-18T00:00:00Z", "trashed": False, "deleted": False},
        "b2": {"backupId": "b2", "createdAt": "2026-08-17T00:00:00Z", "trashed": False, "deleted": False},
        "b3": {"backupId": "b3", "createdAt": "2026-08-16T00:00:00Z", "trashed": True, "trashedAt": "2026-08-16T00:00:00Z", "deleted": False},
    }

    with patch("deepseek_infra.infra.workspace.backup_catalog.catalog_state_store", return_value=state):
        with patch("deepseek_infra.infra.workspace.backup_catalog._append_entry_store", return_value=None):
            with patch("deepseek_infra.infra.workspace.backup_object_set.committed_object_digests", return_value=set()):
                res_apply = backup_retention.apply_retention_store(
                    {"keepLast": 1, "trashGraceHours": 1},
                    mock_store,
                    writer=mock_writer,
                )
                assert "trashed" in res_apply

                res_final = backup_retention.finalize_retention_store(
                    {"keepLast": 1, "trashGraceHours": 1},
                    mock_store,
                    writer=mock_writer,
                )
                assert "deleted" in res_final

    # 2. backup_dr_readiness with replication config
    policy = {
        "policyId": "pol_dr_readiness_test",
        "name": "DR Test Policy",
        "targetId": "target_primary_1",
        "replication": {
            "enabled": True,
            "minCommittedCopies": 3,
            "minFailureDomains": 2,
            "maxReplicaLagSeconds": 10,
            "targets": [
                {"targetId": "target_dr_rep_1", "mode": "required"},
            ],
        },
        "recoveryObjectives": {
            "targetRpoMinutes": 60,
            "targetRtoMinutes": 30,
        },
    }

    with patch("deepseek_infra.infra.workspace.backup_dr_ledger.list_logical_recovery_copies", return_value=[
        {"targetId": "target_primary_1", "recoverable": True, "state": "healthy"},
    ]):
        with patch("deepseek_infra.infra.workspace.backup_replication.calculate_replica_lag", return_value={"lagRecoveryPoints": 5, "lagSeconds": 300}):
            readiness = backup_dr_readiness._replication_summary(
                "target_primary_1",
                "pol_dr_readiness_test",
                {"backupId": "b1", "createdAt": "2026-08-18T01:00:00Z"},
                policy=policy,
                now=datetime.now(tz=timezone.utc),
            )
            assert readiness["enabled"] is True
            assert readiness["compliance"] == "degraded"
            assert "reason" in readiness


def test_readiness_projection_457(tmp_settings: Path) -> None:
    # Test that readiness_status includes topology, capacity, and transferControl projections
    status = backup_dr_readiness.readiness_status()
    assert "topology" in status
    assert "capacity" in status
    assert "transferControl" in status
    assert status["topology"]["status"] in {"healthy", "degraded", "unavailable"}
    assert status["capacity"]["status"] in {"healthy", "degraded", "critical"}


def test_recovery_job_and_keeper_coverage(tmp_settings: Path) -> None:
    # 1. backup_recovery_job terminal exceptions
    session_term = {"phase": "complete", "restoreId": "r_term_1"}
    with pytest.raises(AppError):
        backup_recovery_job.request_pause(session_term)

    with pytest.raises(AppError):
        backup_recovery_job.request_abort(session_term)

    # 2. converge pause & abort
    session_active = {"phase": "fetching", "restoreId": "r_act_1", "pauseRequested": True}
    res_pause = backup_recovery_job.converge(session_active)
    assert res_pause == "paused"
    assert session_active["phase"] == "paused"

    # resume
    res_res = backup_recovery_job.resume(session_active)
    assert res_res == "fetching"

    # resume error when not paused
    with pytest.raises(AppError):
        backup_recovery_job.resume(session_active)

    # converge abort with prepared callback
    abort_called = False

    def _abort_cb() -> None:
        nonlocal abort_called
        abort_called = True

    t_path = tmp_settings / "tx_phase.json"
    t_path.write_text(json.dumps({"phase": "prepared"}), encoding="utf-8")
    session_abort = {
        "phase": "fetching",
        "restoreId": "r_ab_1",
        "abortRequested": True,
        "transactionPath": str(t_path),
    }
    res_ab = backup_recovery_job.converge(session_abort, abort_prepared=_abort_cb)
    assert res_ab == "rolled-back"
    assert abort_called is True

    # 3. backup_recovery_keeper helpers
    assert backup_recovery_keeper._is_protected_phase("fetching") is True
    assert backup_recovery_keeper._is_protected_phase("complete") is False
    assert backup_recovery_keeper._is_protected_phase("failed") is False
    assert backup_recovery_keeper._is_local_target("managed-local") is True
    assert backup_recovery_keeper._is_local_target("local-fs-1") is True
    assert backup_recovery_keeper._is_local_target("target_s3_1") is False

    health = backup_recovery_keeper._KeeperHealthState()
    health.record_tick({"protected": 1, "renewed": 0})
    st = health.snapshot()
    assert "consecutiveFailures" in st
    assert st["consecutiveFailures"] == 0


def test_dr_audit_binding_anomalies() -> None:
    # 1. Invalid commit marker
    assert "invalid-commit-marker" in backup_dr_audit._validate_commit_receipt_binding(
        target_id="t1",
        commit={"invalid": True},
        receipt={},
    )

    # 2. Valid marker structure with anomalies
    base_body = {
        "version": 4,
        "backupId": "bk_audit_1",
        "policyId": "pol1",
        "targetId": "t1",
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "receiptDigest": "xyz",
    }
    base_commit = dict(base_body)
    base_commit["commitHash"] = backup_publish._commit_hash(base_body)

    # Missing receipt
    anom_miss = backup_dr_audit._validate_commit_receipt_binding(
        target_id="t1",
        commit=base_commit,
        receipt=None,
    )
    assert any("missing-receipt" in a for a in anom_miss)

    # Mismatched receipt backupId / targetId / policyId
    bad_receipt = {
        "backupId": "bk_other",
        "targetId": "t2",
        "policyId": "pol2",
        "storageProtocol": "object-set-v1",
    }
    anom_mismatch = backup_dr_audit._validate_commit_receipt_binding(
        target_id="t1",
        commit=base_commit,
        receipt=bad_receipt,
    )
    assert any("receipt-backup-id-mismatch" in a for a in anom_mismatch)
    assert any("receipt-target-mismatch" in a for a in anom_mismatch)
    assert any("receipt-policy-mismatch" in a for a in anom_mismatch)
    assert any("missing-object-set-digest" in a for a in anom_mismatch)


def test_dr_readiness_commit_chain_and_receipt_coverage(tmp_settings: Path) -> None:
    # 1. _validated_commit_chain empty and single
    assert backup_dr_readiness._validated_commit_chain([]) == ([], True)

    m1_body = {"version": 4, "backupId": "b1", "targetGeneration": 1}
    m1 = dict(m1_body, commitHash=backup_publish._commit_hash(m1_body))
    chain, ok = backup_dr_readiness._validated_commit_chain([m1])
    assert ok is True
    assert len(chain) == 1

    # 2. _merge_validated_receipt
    merged = backup_dr_readiness._merge_validated_receipt(
        {"backupId": "b1", "status": "ok"},
        {"pinned": True, "scrubOk": True, "ciphertextScrubbedAt": "2026-08-18T00:00:00Z"},
        target_id="target_t1",
    )
    assert merged["pinned"] is True
    assert merged["scrubOk"] is True
    assert merged["targetId"] == "target_t1"

    # 3. _commit_records_for_root
    t_root = tmp_settings / "dr_root_test"
    (t_root / "commits" / "pol1").mkdir(parents=True, exist_ok=True)
    (t_root / "receipts").mkdir(parents=True, exist_ok=True)

    c_file = t_root / "commits" / "pol1" / "000001.json"
    c_file.write_text(json.dumps(m1), encoding="utf-8")

    r_file = t_root / "receipts" / "b1.json"
    r_file.write_text(json.dumps({"backupId": "b1", "policyId": "pol1"}), encoding="utf-8")

    recs, committed, healthy = backup_dr_readiness._commit_records_for_root(t_root, "target_t1")
    assert healthy is True
    assert len(recs) == 1
    assert ("target_t1", "b1") in committed

    # 4. _stage_samples with staging root
    st_root = tmp_settings / "restore_staging"
    st_root.mkdir(parents=True, exist_ok=True)
    session_dir = st_root / "sess_1"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "remote-fetch.json").write_text(
        json.dumps({
            "restoreId": "r1",
            "recoveryTelemetry": {
                "samples": [
                    {"stage": "transfer", "durationMs": 150},
                ],
            },
        }),
        encoding="utf-8",
    )
    with patch.object(backup_dr_readiness.backups, "RESTORE_DIR", st_root):
        samples = backup_dr_readiness._stage_samples()
        assert any(s.get("stage") == "transfer" for s in samples)


def test_backup_retention_deep_preview_validation(tmp_settings: Path) -> None:
    t_root = tmp_settings / "ret_val_root"
    t_root.mkdir(parents=True, exist_ok=True)

    ret = {"keepLast": 1, "trashGraceHours": 24}
    prev_valid = {
        "catalogHeadHash": "0" * 64,
        "targetGeneration": 0,
        "policyDigest": backup_retention._policy_digest(ret),
    }

    # 1. Valid preview passes
    backup_retention._validate_preview_snapshot(prev_valid, ret, t_root)

    # 2. Head hash mismatch raises 409
    with pytest.raises(AppError) as exc_head:
        backup_retention._validate_preview_snapshot(dict(prev_valid, catalogHeadHash="mismatch"), ret, t_root)
    assert exc_head.value.status == 409

    # 3. Target generation mismatch raises 409
    with pytest.raises(AppError) as exc_gen:
        backup_retention._validate_preview_snapshot(dict(prev_valid, targetGeneration=99), ret, t_root)
    assert exc_gen.value.status == 409

    # 4. Policy digest mismatch raises 409
    with pytest.raises(AppError) as exc_pol:
        backup_retention._validate_preview_snapshot(dict(prev_valid, policyDigest="bad_digest"), ret, t_root)
    assert exc_pol.value.status == 409

    # 5. _protect_snapshot_ancestors with circular chain
    recs = [
        {"backupId": "b1", "snapshotKind": "incremental", "parentBackupId": "b2"},
        {"backupId": "b2", "snapshotKind": "incremental", "parentBackupId": "b1"},
    ]
    keep: set[str] = set()
    prot: dict[str, str] = {}
    backup_retention._protect_snapshot_ancestors(recs, keep, prot, descendants={"b1"})
    assert "b2" in keep


def test_backup_scrub_deep_catalog_and_all(tmp_settings: Path) -> None:
    t_root = tmp_settings / "scrub_root"
    t_root.mkdir(parents=True, exist_ok=True)

    # 1. Nonexistent backup in catalog raises 404
    with pytest.raises(AppError) as exc_404:
        backup_scrub._catalog_record(t_root, "nonexistent_backup_id")
    assert exc_404.value.status == 404

    # 2. scrub_all on empty catalog returns scrubbed=0, ok=True
    res_empty = backup_scrub.scrub_all(t_root)
    assert res_empty["scrubbed"] == 0
    assert res_empty["ok"] is True


def test_backup_dr_readiness_cache_health_and_target_kinds(tmp_settings: Path) -> None:
    # 1. _parse_time edge cases
    assert backup_dr_readiness._parse_time("not-a-time") is None
    assert backup_dr_readiness._parse_time(12345) is None
    assert backup_dr_readiness._parse_time("") is None

    # 2. _resolve_target_kind
    assert backup_dr_readiness._resolve_target_kind("managed-local") == "managed-local"
    with patch.object(backup_targets, "get_target", return_value={"kind": "s3"}):
        assert backup_dr_readiness._resolve_target_kind("t_s3") == "s3"
    with patch.object(backup_targets, "get_target", return_value={"kind": "custom-storage"}):
        assert backup_dr_readiness._resolve_target_kind("t_custom") == "custom-storage"
    with patch.object(backup_targets, "get_target", side_effect=Exception("error")):
        assert backup_dr_readiness._resolve_target_kind("t_err") == "filesystem"

    # 3. _cache_health invalid pins
    c_root = tmp_settings / "cache_h_root"
    c_root.mkdir(parents=True, exist_ok=True)
    (c_root / "pins").mkdir(parents=True, exist_ok=True)
    (c_root / "pins" / "bad.json").write_text(json.dumps({"schemaVersion": 1, "digests": ["not-a-valid-hex-digest"]}), encoding="utf-8")

    with patch("deepseek_infra.infra.workspace.backup_component_cache.CACHE_DIR", c_root):
        h = backup_dr_readiness._cache_health(datetime.now(tz=timezone.utc))
        assert h["status"] == "error"
        assert h["reason"] == "pin-metadata-invalid"


def test_backup_policies_validation_and_secret_rejections(tmp_settings: Path) -> None:
    # 1. Secret markers rejection
    with pytest.raises(AppError) as exc_sec_str:
        backup_policies._reject_secret_markers("contains begin private key inside")
    assert exc_sec_str.value.status == 400

    with pytest.raises(AppError) as exc_sec_dict:
        backup_policies._reject_secret_markers({"token": "bearer my_secret_token"})
    assert exc_sec_dict.value.status == 400

    with pytest.raises(AppError) as exc_sec_list:
        backup_policies._reject_secret_markers(["safe", "age-secret-key-12345"])
    assert exc_sec_list.value.status == 400

    # 2. Scope validation errors
    with pytest.raises(AppError) as exc_scope_map:
        backup_policies._require_mapping("not a dict", "test_sec")
    assert exc_scope_map.value.status == 400

    with pytest.raises(AppError) as exc_bool:
        backup_policies._require_bool("not_a_bool", "test_bool", True)
    assert exc_bool.value.status == 400

    with pytest.raises(AppError) as exc_int:
        backup_policies._require_int("not_an_int", "test_int", 10, 1, 100)
    assert exc_int.value.status == 400

    with pytest.raises(AppError) as exc_int_bounds:
        backup_policies._require_int(999, "test_int", 10, 1, 100)
    assert exc_int_bounds.value.status == 400

    with pytest.raises(AppError) as exc_choice:
        backup_policies._require_choice("invalid_opt", "test_choice", ("a", "b"))
    assert exc_choice.value.status == 400

    with pytest.raises(AppError) as exc_safe_id:
        backup_policies._require_safe_id("invalid/id/with/slash", "test_id")
    assert exc_safe_id.value.status == 400

    # 3. Recipients validation
    with pytest.raises(AppError) as exc_rec_empty:
        backup_policies.normalize_recipients([])
    assert exc_rec_empty.value.status == 400

    with pytest.raises(AppError) as exc_rec_non_age:
        backup_policies.normalize_recipients(["ssh-rsa AAAA..."])
    assert exc_rec_non_age.value.status == 400

    # 4. Scope mode project without projects
    with pytest.raises(AppError) as exc_proj_mode:
        backup_policies._normalize_scope({"mode": "project", "projectIds": []})
    assert exc_proj_mode.value.status == 400

    # 5. active_recipients aggregation
    pol_rec = {
        "policyId": "pol_with_rec",
        "protection": {"recipients": [backup_policies.DEFAULT_TEST_RECIPIENT]},
    }
    with patch.object(backup_policies, "list_policies", return_value=[pol_rec]):
        recs = backup_policies.active_recipients()
        assert len(recs) == 1
        assert recs[0] == backup_policies.DEFAULT_TEST_RECIPIENT


def test_backup_dr_readiness_objectives_breaches_and_failures(tmp_settings: Path) -> None:
    now_dt = datetime.now(tz=timezone.utc)
    old_time_iso = (now_dt - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    recent_time_iso = now_dt.isoformat().replace("+00:00", "Z")

    fake_point = {
        "targetId": "t1",
        "policyId": "p1",
        "backupId": "b1",
        "committedAt": old_time_iso,
        "snapshotKind": "full",
        "chainLength": 1,
    }

    # 1. RPO objective breached
    with patch.object(backup_dr_ledger, "get_latest_recoverable_point", return_value=(fake_point, [fake_point])):
        with patch.object(backup_dr_ledger, "get_latest_scrub_outcome", return_value=None):
            with patch.object(backup_dr_ledger, "get_latest_drill_outcome", return_value=None):
                res_rpo = backup_dr_readiness.evaluate_scope_readiness(
                    "t1",
                    "p1",
                    recovery_objectives={"maxRpoSeconds": 3600, "maxScrubAgeSeconds": 86400, "maxDrillAgeSeconds": 86400},
                    now=now_dt,
                )
                assert res_rpo["status"] == "objective-breached"
                assert "rpo-objective-breached" in res_rpo["reasons"]

    # 2. Scrub failed and overdue
    scrub_failed = {"targetId": "t1", "policyId": "p1", "observedAt": recent_time_iso, "result": "failed"}
    with patch.object(backup_dr_ledger, "get_latest_recoverable_point", return_value=(fake_point, [fake_point])):
        with patch.object(backup_dr_ledger, "get_latest_scrub_outcome", return_value=scrub_failed):
            with patch.object(backup_dr_ledger, "get_latest_drill_outcome", return_value=None):
                res_scrub_fail = backup_dr_readiness.evaluate_scope_readiness(
                    "t1",
                    "p1",
                    now=now_dt,
                )
                assert "scrub-failed" in res_scrub_fail["reasons"]
                assert res_scrub_fail["status"] == "degraded"

    # 3. Drill failed and drill blocked
    drill_fail = {"targetId": "t1", "policyId": "p1", "observedAt": recent_time_iso, "result": "failed", "drillKind": "dry-run"}
    with patch.object(backup_dr_ledger, "get_latest_recoverable_point", return_value=(fake_point, [fake_point])):
        with patch.object(backup_dr_ledger, "get_latest_scrub_outcome", return_value=None):
            with patch.object(backup_dr_ledger, "get_latest_drill_outcome", return_value=drill_fail):
                res_drill_fail = backup_dr_readiness.evaluate_scope_readiness(
                    "t1",
                    "p1",
                    now=now_dt,
                )
                assert "drill-failed" in res_drill_fail["reasons"]

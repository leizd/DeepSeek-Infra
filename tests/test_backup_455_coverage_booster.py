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
    backup_policies,
    backup_publish,
    backup_replication,
    backup_target_store,
    backup_targets,
    backup_write_continuity,
)


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MockTargetWrapper:
    def __init__(self, target_id: str, *, root: Path | None = None, store: Any = None) -> None:
        self.target_id = target_id
        self.root = root
        self.store = store


def test_write_continuity_state_defaults_and_fallbacks(tmp_settings: Path) -> None:
    # 1. Non-existent policy without registered policy object
    state = backup_write_continuity.get_write_continuity_state("non_existent_policy")
    assert state["policyId"] == "non_existent_policy"
    assert state["activeWriteTargetRole"] == "primary"
    assert state["failoverEpoch"] == 0

    # 2. Corrupt JSON file fallback
    p_path = backup_write_continuity._continuity_path("corrupt_policy")
    p_path.parent.mkdir(parents=True, exist_ok=True)
    p_path.write_text("invalid-json{", encoding="utf-8")
    corrupt_state = backup_write_continuity.get_write_continuity_state("corrupt_policy")
    assert corrupt_state["policyId"] == "corrupt_policy"
    assert corrupt_state["policyRevision"] == 1


def test_perform_liveness_preflight_branches(tmp_settings: Path) -> None:
    # 1. Root target path exists vs does not exist
    good_root = tmp_settings / "good_target_root"
    good_root.mkdir(parents=True, exist_ok=True)
    bad_root = tmp_settings / "bad_target_root_non_existent"

    t_good = MockTargetWrapper("tgt_good", root=good_root)
    t_bad = MockTargetWrapper("tgt_bad", root=bad_root)

    res_good = backup_write_continuity.perform_liveness_preflight("tgt_good", target=t_good)
    assert res_good["status"] == "available"
    assert res_good["latencyMs"] >= 0.0

    res_bad = backup_write_continuity.perform_liveness_preflight("tgt_bad", target=t_bad)
    assert res_bad["status"] == "unavailable"
    assert "does not exist" in str(res_bad["error"])

    # 2. Store target: available vs unavailable
    class MockStoreWithLiveness:
        def __init__(self, status: str, err: str | None = None) -> None:
            self._status = status
            self._err = err

        def check_liveness(self) -> dict[str, Any]:
            if self._status == "raise":
                raise RuntimeError("connection reset by peer")
            return {"status": self._status, "error": self._err}

    t_store_ok = MockTargetWrapper("store_ok", store=MockStoreWithLiveness("available"))
    t_store_fail = MockTargetWrapper("store_fail", store=MockStoreWithLiveness("unavailable", "503 timeout"))
    t_store_err = MockTargetWrapper("store_err", store=MockStoreWithLiveness("raise"))

    assert backup_write_continuity.perform_liveness_preflight("store_ok", target=t_store_ok)["status"] == "available"
    assert backup_write_continuity.perform_liveness_preflight("store_fail", target=t_store_fail)["status"] == "unavailable"
    assert backup_write_continuity.perform_liveness_preflight("store_err", target=t_store_err)["status"] == "unavailable"

    # 3. Target with neither root nor store
    t_empty = MockTargetWrapper("empty_target")
    res_empty = backup_write_continuity.perform_liveness_preflight("empty_target", target=t_empty)
    assert res_empty["status"] == "unavailable"
    assert "neither root nor store" in str(res_empty["error"])


def test_record_target_liveness_and_continuity_tracking(tmp_settings: Path) -> None:
    policy_id = "pol_liveness_track"
    t_id = "managed-local"

    now = datetime(2026, 8, 17, 10, 0, 0, tzinfo=timezone.utc)

    # Initial success
    e1 = backup_write_continuity.record_target_liveness(policy_id, t_id, status="available", now=now)
    assert e1["consecutiveSuccesses"] == 1
    assert e1["consecutiveFailures"] == 0

    state1 = backup_write_continuity.get_write_continuity_state(policy_id)
    assert state1["primaryConsecutiveHealthySeconds"] == 0.0

    # Success after 60 seconds
    now2 = now + timedelta(seconds=60)
    e2 = backup_write_continuity.record_target_liveness(policy_id, t_id, status="available", now=now2)
    assert e2["consecutiveSuccesses"] == 2

    state2 = backup_write_continuity.get_write_continuity_state(policy_id)
    assert state2["primaryConsecutiveHealthySeconds"] == 60.0

    # Failure resets tracking
    now3 = now2 + timedelta(seconds=10)
    e3 = backup_write_continuity.record_target_liveness(policy_id, t_id, status="unavailable", error="outage", now=now3)
    assert e3["consecutiveFailures"] == 1
    assert e3["consecutiveSuccesses"] == 0

    state3 = backup_write_continuity.get_write_continuity_state(policy_id)
    assert state3["primaryFirstHealthyAt"] is None
    assert state3["primaryConsecutiveHealthySeconds"] == 0.0


def test_evaluate_failback_eligibility_all_branches(tmp_settings: Path) -> None:
    policy_id = "pol_failback_eval"
    primary_id = "target_pri"
    failover_id = "target_sec"

    # 1. Active is primary -> already-primary
    ok, reason, _ = backup_write_continuity.evaluate_failback_eligibility(policy_id)
    assert not ok
    assert reason == "already-primary"

    # 2. Transition to failover target with primary unhealthy
    backup_write_continuity.execute_failover_transition(policy_id, failover_id, reason="primary-down")
    st = backup_write_continuity.get_write_continuity_state(policy_id)
    st["primaryFirstHealthyAt"] = None
    backup_write_continuity.save_write_continuity_state(policy_id, st)

    # Primary is not healthy
    ok, reason, _ = backup_write_continuity.evaluate_failback_eligibility(policy_id)
    assert not ok
    assert reason == "primary-not-healthy"

    # 3. Primary healthy but duration insufficient (< 1800s)
    t0 = datetime(2026, 8, 17, 10, 0, 0, tzinfo=timezone.utc)
    state = backup_write_continuity.get_write_continuity_state(policy_id)
    state["configuredPrimaryTargetId"] = primary_id
    state["primaryFirstHealthyAt"] = _utc_iso(t0)
    state["primaryConsecutiveHealthySeconds"] = 300.0
    backup_write_continuity.save_write_continuity_state(policy_id, state)

    t_short = t0 + timedelta(seconds=300)
    ok, reason, _ = backup_write_continuity.evaluate_failback_eligibility(policy_id, stability_window_seconds=1800, now=t_short)
    assert not ok
    assert "primary-stability-insufficient" in reason

    # 4. Primary healthy for >= 1800s, but point unconverged
    t_stable = t0 + timedelta(seconds=2000)
    # Record backup on failover target
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=failover_id,
        policy_id=policy_id,
        backup_id="bk_failover_1",
        committed_at=_utc_iso(t0 + timedelta(seconds=100)),
        recoverable=True,
        state="healthy",
    )

    ok, reason, _ = backup_write_continuity.evaluate_failback_eligibility(policy_id, stability_window_seconds=1800, now=t_stable)
    assert not ok
    assert "latest-failover-point-not-converged" in reason

    # 5. Point converged on primary -> eligible
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=primary_id,
        policy_id=policy_id,
        backup_id="bk_failover_1",
        committed_at=_utc_iso(t0 + timedelta(seconds=150)),
        recoverable=True,
        state="healthy",
    )

    ok, reason, _ = backup_write_continuity.evaluate_failback_eligibility(policy_id, stability_window_seconds=1800, now=t_stable)
    assert ok
    assert reason == "eligible"

    # 6. Execute failback transition
    fb_state = backup_write_continuity.execute_failback_transition(policy_id)
    assert fb_state["activeWriteTargetId"] == primary_id
    assert fb_state["activeWriteTargetRole"] == "primary"
    assert fb_state["lastFailbackReason"] == "governed-stability-window-and-point-convergence"


def test_promote_primary_target_and_cas(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_targets, "_containment_violation", lambda *a, **k: None)

    policy_id = "pol_promote_test"
    dir_a = tmp_settings / "t_a"
    dir_b = tmp_settings / "t_b"
    dir_a.mkdir(parents=True, exist_ok=True)
    dir_b.mkdir(parents=True, exist_ok=True)

    t_a = backup_targets.init_target(dir_a, label="Target A")
    t_b = backup_targets.init_target(dir_b, label="Target B")
    target_a = str(t_a["targetId"])
    target_b = str(t_b["targetId"])

    # Create policy with replication
    backup_policies.create_policy({
        "policyId": policy_id,
        "name": "Promote Test Policy",
        "targetId": target_a,
        "primaryTargetId": target_a,
        "policyRevision": 1,
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "replication": {
            "enabled": True,
            "targets": [{"targetId": target_b, "mode": "required"}],
        },
    })

    # 1. CAS mismatch on revision
    with pytest.raises(AppError) as exc_rev:
        backup_write_continuity.promote_primary_target(policy_id, target_b, expected_policy_revision=99)
    assert exc_rev.value.status == 412

    # 2. CAS mismatch on epoch
    with pytest.raises(AppError) as exc_ep:
        backup_write_continuity.promote_primary_target(policy_id, target_b, expected_failover_epoch=99)
    assert exc_ep.value.status == 412

    # 3. Successful promotion
    res = backup_write_continuity.promote_primary_target(
        policy_id,
        target_b,
        expected_policy_revision=1,
        expected_failover_epoch=0,
    )
    assert res["status"] == "promoted"
    assert res["newPrimaryTargetId"] == target_b
    assert res["previousPrimaryTargetId"] == target_a

    # Verify policy updated
    pol_updated = backup_policies.get_policy(policy_id)
    assert pol_updated["primaryTargetId"] == target_b
    assert pol_updated["policyRevision"] == 2
    # Verify previous primary target_a is now in replica targets
    repl_targets = pol_updated["replication"]["targets"]
    assert any(t["targetId"] == target_a for t in repl_targets)


def test_stream_ciphertext_transfer_edge_cases(tmp_settings: Path) -> None:
    src_dir = tmp_settings / "src_stream"
    dst_dir = tmp_settings / "dst_stream"
    src_dir.mkdir(parents=True, exist_ok=True)
    dst_dir.mkdir(parents=True, exist_ok=True)

    data = b"STREAMING-CIPHERTEXT-TEST-DATA-XYZ"
    digest = _sha256(data)
    src_rel = f"objects/{digest}.age"
    dst_rel = f"objects/{digest}.age"

    (src_dir / src_rel).parent.mkdir(parents=True, exist_ok=True)
    (src_dir / src_rel).write_bytes(data)

    src_tgt = MockTargetWrapper("src_t", root=src_dir)
    dst_tgt = MockTargetWrapper("dst_t", root=dst_dir)

    # 1. Successful filesystem streaming transfer
    transferred = backup_replication.stream_ciphertext_transfer(src_tgt, dst_tgt, src_rel, dst_rel, digest, chunk_size=8)
    assert transferred == len(data)
    assert (dst_dir / dst_rel).read_bytes() == data

    # 2. Corrupt source digest mismatch raises AppError
    with pytest.raises(AppError) as exc_dig:
        backup_replication.stream_ciphertext_transfer(src_tgt, dst_tgt, src_rel, "objects/bad.age", "wrong_digest", chunk_size=8)
    assert "digest mismatch" in str(exc_dig.value)

    # 3. Stream to remote store (single-chunk and multipart upload)
    store = backup_target_store.MemoryTargetStore()
    store_tgt = MockTargetWrapper("store_t", store=store)

    transferred_store = backup_replication.stream_ciphertext_transfer(src_tgt, store_tgt, src_rel, dst_rel, digest, chunk_size=8)
    assert transferred_store == len(data)
    assert store.get_bytes(dst_rel) == data


def test_verify_destination_component_all_branches(tmp_settings: Path) -> None:
    dst_dir = tmp_settings / "dest_verify"
    dst_dir.mkdir(parents=True, exist_ok=True)

    data = b"VERIFIED-COMPONENT-PAYLOAD"
    digest = _sha256(data)
    rel = f"objects/{digest}.age"

    (dst_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    (dst_dir / rel).write_bytes(data)

    tgt_fs = MockTargetWrapper("fs_t", root=dst_dir)

    # 1. Matching file
    is_valid, is_corrupt = backup_replication._verify_destination_component(tgt_fs, rel, digest)
    assert is_valid and not is_corrupt

    # 2. Corrupted file
    is_valid_c, is_corrupt_c = backup_replication._verify_destination_component(tgt_fs, rel, "other_digest")
    assert not is_valid_c and is_corrupt_c

    # 3. Missing file
    is_valid_m, is_corrupt_m = backup_replication._verify_destination_component(tgt_fs, "non_existent.age", digest)
    assert not is_valid_m and not is_corrupt_m

    # 4. Store component
    store = backup_target_store.MemoryTargetStore()
    store.put_if_absent(rel, data)
    tgt_store = MockTargetWrapper("store_t", store=store)

    is_valid_s, is_corrupt_s = backup_replication._verify_destination_component(tgt_store, rel, digest)
    assert is_valid_s and not is_corrupt_s


def test_reconcile_policy_replicas_cursor_pagination_and_wraparound(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_targets, "_containment_violation", lambda *a, **k: None)

    policy_id = "pol_cursor_recon"
    pri_dir = tmp_settings / "pri_recon"
    rep_dir = tmp_settings / "rep_recon"
    pri_dir.mkdir(parents=True, exist_ok=True)
    rep_dir.mkdir(parents=True, exist_ok=True)

    t_pri = backup_targets.init_target(pri_dir, label="Primary")
    t_rep = backup_targets.init_target(rep_dir, label="Replica")
    pri_id = str(t_pri["targetId"])
    rep_id = str(t_rep["targetId"])

    backup_policies.create_policy({
        "policyId": policy_id,
        "name": "Cursor Recon Policy",
        "targetId": pri_id,
        "primaryTargetId": pri_id,
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "replication": {
            "enabled": True,
            "targets": [{"targetId": rep_id, "mode": "required"}],
        },
    })

    # Populate 3 logical points on primary
    for i in range(1, 4):
        bk_id = f"bk_cursor_{i}"
        comp = f"data-payload-{i}".encode("utf-8")
        dig = _sha256(comp)
        rel = f"objects/{dig[:2]}/{dig[2:4]}/{dig}.age"
        (pri_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (pri_dir / rel).write_bytes(comp)

        receipt = {
            "schemaVersion": 4,
            "backupId": bk_id,
            "policyId": policy_id,
            "targetId": pri_id,
            "objectSetDigest": dig,
            "storageProtocol": "object-set-v1",
            "objects": [{"digest": dig, "size": len(comp)}],
        }
        (pri_dir / "receipts").mkdir(parents=True, exist_ok=True)
        r_bytes = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
        (pri_dir / "receipts" / f"{bk_id}.json").write_bytes(r_bytes)

        commit = {
            "schemaVersion": 4,
            "targetGeneration": 1,
            "previousCommitHash": "0" * 64,
            "policyId": policy_id,
            "backupId": bk_id,
            "receiptDigest": _sha256(r_bytes),
            "objectSetDigest": dig,
            "storageProtocol": "object-set-v1",
            "committedAt": f"2026-08-17T0{i}:00:00Z",
        }
        (pri_dir / "commits" / policy_id).mkdir(parents=True, exist_ok=True)
        (pri_dir / "commits" / policy_id / f"{bk_id}.json").write_text(json.dumps(commit), encoding="utf-8")

        backup_dr_ledger.record_logical_recovery_copy(
            target_id=pri_id,
            policy_id=policy_id,
            backup_id=bk_id,
            committed_at=f"2026-08-17T0{i}:00:00Z",
            object_set_digest=dig,
            recoverable=True,
            state="healthy",
        )

    t_map = {
        pri_id: MockTargetWrapper(pri_id, root=pri_dir),
        rep_id: MockTargetWrapper(rep_id, root=rep_dir),
    }
    monkeypatch.setattr(backup_publish, "resolve_target", lambda tid: t_map[tid])

    # Reconcile batch 1 (max_points=2, max_repairs=2)
    rep1 = backup_replication.reconcile_policy_replicas(policy_id, max_points=2, max_repairs=2)
    assert rep1["status"] == "completed"
    assert rep1["repairsTriggered"] == 2
    assert rep1["repairsSucceeded"] == 2
    cursors1 = backup_replication._load_cursors()
    assert policy_id in cursors1

    # Reconcile batch 2 (max_points=2, max_repairs=2) -> reaches end of current points
    rep2 = backup_replication.reconcile_policy_replicas(policy_id, max_points=2, max_repairs=2)
    assert rep2["status"] == "completed"
    assert rep2["repairsTriggered"] == 1
    assert rep2["repairsSucceeded"] == 1

    # Reconcile batch 3 -> wraps around to beginning
    rep3 = backup_replication.reconcile_policy_replicas(policy_id, max_points=2, max_repairs=2)
    assert rep3["status"] == "completed"
    cursors3 = backup_replication._load_cursors()
    assert cursors3[policy_id]["wrappedAround"] is True


def test_write_continuity_additional_branches(tmp_settings: Path) -> None:
    policy_id = "pol_wc_extra"
    target_id = "managed-local"
    t_root = tmp_settings / "root_wc_extra"
    t_root.mkdir(parents=True, exist_ok=True)
    t_wrapper = MockTargetWrapper(target_id, root=t_root)

    # 1. perform_liveness_preflight with policy_id passed
    res = backup_write_continuity.perform_liveness_preflight(target_id, target=t_wrapper, policy_id=policy_id)
    assert res["status"] == "available"

    # 2. record_target_liveness with corrupted primaryFirstHealthyAt
    state = backup_write_continuity.get_write_continuity_state(policy_id)
    state["configuredPrimaryTargetId"] = target_id
    state["primaryFirstHealthyAt"] = "INVALID_ISO_TIMESTAMP"
    backup_write_continuity.save_write_continuity_state(policy_id, state)

    entry = backup_write_continuity.record_target_liveness(policy_id, target_id, status="available")
    assert entry["status"] == "available"

    # 3. execute_failover_transition when failoverActiveSince is already set
    state_fo1 = backup_write_continuity.execute_failover_transition(policy_id, "sec_tgt", reason="test-failover-1")
    since1 = state_fo1["failoverActiveSince"]
    assert since1 is not None

    state_fo2 = backup_write_continuity.execute_failover_transition(policy_id, "sec_tgt", reason="test-failover-2")
    assert state_fo2["failoverActiveSince"] == since1
    assert state_fo2["failoverEpoch"] == 2

    # 4. evaluate_failback_eligibility when no active copies exist and get_latest_recoverable_point returns None
    pol_empty = "pol_empty_failback"
    backup_write_continuity.execute_failover_transition(pol_empty, "target_empty", reason="test-empty")
    st_empty = backup_write_continuity.get_write_continuity_state(pol_empty)
    st_empty["configuredPrimaryTargetId"] = "target_pri"
    st_empty["primaryFirstHealthyAt"] = _utc_iso(datetime.now(tz=timezone.utc) - timedelta(seconds=3600))
    st_empty["primaryConsecutiveHealthySeconds"] = 3600.0
    backup_write_continuity.save_write_continuity_state(pol_empty, st_empty)

    ok_e, reason_e, info_e = backup_write_continuity.evaluate_failback_eligibility(pol_empty, stability_window_seconds=1800)
    assert ok_e
    assert reason_e == "eligible"
    assert info_e["latestFailoverPointConverged"] is True

    # 5. evaluate_failback_eligibility when logical recovery point on primary exists
    pol_ledger = "pol_ledger_check"
    backup_write_continuity.execute_failover_transition(pol_ledger, "target_f2", reason="test-ledger")
    st_l = backup_write_continuity.get_write_continuity_state(pol_ledger)
    st_l["configuredPrimaryTargetId"] = "target_pri_l"
    st_l["primaryFirstHealthyAt"] = _utc_iso(datetime.now(tz=timezone.utc) - timedelta(seconds=3600))
    st_l["primaryConsecutiveHealthySeconds"] = 3600.0
    backup_write_continuity.save_write_continuity_state(pol_ledger, st_l)

    # Record copy on active failover target
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="target_f2",
        policy_id=pol_ledger,
        backup_id="bk_ledger_1",
        committed_at=_utc_iso(),
        recoverable=True,
        state="healthy",
    )
    # Record copy on primary target so convergence passes
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="target_pri_l",
        policy_id=pol_ledger,
        backup_id="bk_ledger_1",
        committed_at=_utc_iso(),
        recoverable=True,
        state="healthy",
    )

    ok_l, reason_l, info_l = backup_write_continuity.evaluate_failback_eligibility(pol_ledger, stability_window_seconds=1800)
    assert ok_l
    assert reason_l == "eligible"


def test_authenticate_recovery_copy_branches(tmp_settings: Path) -> None:
    policy_id = "pol_auth_copy"
    target_id = "t_auth"
    backup_id = "bk_auth_1"

    t_dir = tmp_settings / "tgt_auth"
    t_dir.mkdir(parents=True, exist_ok=True)
    t_wrapper = MockTargetWrapper(target_id, root=t_dir)

    # 1. Missing receipt file
    status1, r1, c1 = backup_replication.authenticate_recovery_copy(t_wrapper, policy_id, backup_id)
    assert status1 == "missing"
    assert r1 is None and c1 is None

    # 2. Corrupt receipt JSON
    (t_dir / "receipts").mkdir(parents=True, exist_ok=True)
    (t_dir / "receipts" / f"{backup_id}.json").write_text("invalid-json", encoding="utf-8")
    status2, r2, c2 = backup_replication.authenticate_recovery_copy(t_wrapper, policy_id, backup_id)
    assert status2 == "corrupt"

    # 3. Valid receipt, missing commit
    comp = b"auth-data-comp"
    dig = _sha256(comp)
    receipt = {
        "schemaVersion": 4,
        "backupId": backup_id,
        "policyId": policy_id,
        "targetId": target_id,
        "objectSetDigest": dig,
        "storageProtocol": "object-set-v1",
        "objects": [{"digest": dig, "size": len(comp)}],
    }
    r_bytes = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (t_dir / "receipts" / f"{backup_id}.json").write_bytes(r_bytes)

    status3, r3, c3 = backup_replication.authenticate_recovery_copy(t_wrapper, policy_id, backup_id)
    assert status3 == "corrupt"

    # 4. Valid receipt and commit, matching digests
    commit = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "policyId": policy_id,
        "backupId": backup_id,
        "receiptDigest": _sha256(r_bytes),
        "objectSetDigest": dig,
        "storageProtocol": "object-set-v1",
        "committedAt": _utc_iso(),
    }
    (t_dir / "commits" / policy_id).mkdir(parents=True, exist_ok=True)
    (t_dir / "commits" / policy_id / f"{backup_id}.json").write_text(json.dumps(commit), encoding="utf-8")

    status4, r4, c4 = backup_replication.authenticate_recovery_copy(t_wrapper, policy_id, backup_id)
    assert status4 == "authenticated"
    assert r4 is not None and c4 is not None

    # 5. Commit with mismatched receiptDigest
    bad_commit = dict(commit)
    bad_commit["receiptDigest"] = "bad" * 16
    (t_dir / "commits" / policy_id / f"{backup_id}.json").write_text(json.dumps(bad_commit), encoding="utf-8")
    status5, r5, c5 = backup_replication.authenticate_recovery_copy(t_wrapper, policy_id, backup_id)
    assert status5 == "corrupt"


def test_write_continuity_in_dr_readiness(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_targets, "_containment_violation", lambda *a, **k: None)
    from deepseek_infra.infra.workspace import backup_dr_readiness

    policy_id = "pol_dr_readiness_test"
    t_pri_dir = tmp_settings / "t_pri_dir"
    t_sec_dir = tmp_settings / "t_sec_dir"
    t_pri_dir.mkdir(parents=True, exist_ok=True)
    t_sec_dir.mkdir(parents=True, exist_ok=True)
    t_pri = backup_targets.init_target(t_pri_dir, label="Primary")
    t_sec = backup_targets.init_target(t_sec_dir, label="Secondary")
    target_pri = str(t_pri["targetId"])
    target_sec = str(t_sec["targetId"])

    backup_policies.create_policy({
        "policyId": policy_id,
        "name": "DR Readiness Policy",
        "targetId": target_pri,
        "primaryTargetId": target_pri,
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
    })

    st = backup_write_continuity.get_write_continuity_state(policy_id)
    st["configuredPrimaryTargetId"] = target_pri
    st["activeWriteTargetRole"] = "failover"
    st["activeWriteTargetId"] = target_sec
    backup_write_continuity.save_write_continuity_state(policy_id, st)

    # Readiness check includes write continuity state with zero remote I/O
    readiness = backup_dr_readiness.evaluate_scope_readiness(target_id=target_sec, policy_id=policy_id)
    assert readiness is not None
    assert readiness["writeContinuity"]["isFailover"] is True


def test_source_hold_and_lease_renewal(tmp_settings: Path) -> None:
    # 1. SourceHold basic enter and exit (filesystem target)
    t_dir = tmp_settings / "t_hold_fs"
    t_dir.mkdir(parents=True, exist_ok=True)
    hold = backup_replication.acquire_source_hold(
        target_id="t_pri",
        policy_id="pol_sh",
        backup_id="bk_1",
        holder_id="rep_1",
        target_root=t_dir,
    )
    assert hold.hold_id is not None
    hold.renew(duration_seconds=120)
    assert hold.generation == 2
    hold.release()

    # 2. SourceHold with remote store and lost lease error
    class LostStoreMock:
        def put_if_match(self, *a, **k):
            raise RuntimeError("ETag mismatch: 412 Precondition Failed")

    hold_store = backup_replication.SourceHold(
        hold_id="hold_st_1",
        target_id="t_store",
        policy_id="pol_st",
        backup_id="bk_st",
        holder_id="rep_st",
        target_store=LostStoreMock(),
        etag="etag_old",
    )
    with pytest.raises(backup_replication.RepairLeaseLostError):
        hold_store.renew()


def test_authenticate_recovery_copy_conflicts(tmp_settings: Path) -> None:
    policy_id = "pol_auth_conflicts"
    target_id = "t_conflicts"
    backup_id = "bk_conf_1"

    t_dir = tmp_settings / "tgt_conf"
    t_dir.mkdir(parents=True, exist_ok=True)
    t_wrapper = MockTargetWrapper(target_id, root=t_dir)

    comp = b"auth-conflicts-payload"
    dig = _sha256(comp)
    receipt = {
        "schemaVersion": 4,
        "backupId": backup_id,
        "policyId": policy_id,
        "targetId": target_id,
        "objectSetDigest": dig,
        "storageProtocol": "object-set-v1",
        "objects": [{"digest": dig, "size": len(comp)}],
    }
    r_bytes = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (t_dir / "receipts").mkdir(parents=True, exist_ok=True)
    (t_dir / "receipts" / f"{backup_id}.json").write_bytes(r_bytes)

    commit = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "policyId": policy_id,
        "backupId": backup_id,
        "receiptDigest": _sha256(r_bytes),
        "objectSetDigest": dig,
        "storageProtocol": "object-set-v1",
        "committedAt": _utc_iso(),
    }
    (t_dir / "commits" / policy_id).mkdir(parents=True, exist_ok=True)
    (t_dir / "commits" / policy_id / f"{backup_id}.json").write_text(json.dumps(commit), encoding="utf-8")

    # 1. Expected object set digest matches
    st_ok, _, _ = backup_replication.authenticate_recovery_copy(t_wrapper, policy_id, backup_id, expected_object_set_digest=dig)
    assert st_ok == "authenticated"

    # 2. Expected object set digest mismatch -> conflicting
    st_conf, _, _ = backup_replication.authenticate_recovery_copy(t_wrapper, policy_id, backup_id, expected_object_set_digest="mismatched" * 4)
    assert st_conf == "conflicting"


def test_reconcile_policy_replicas_disabled_and_empty_targets(tmp_settings: Path) -> None:
    # 1. Policy without replication enabled
    pol_dis = "pol_repl_disabled"
    backup_policies.create_policy({
        "policyId": pol_dis,
        "name": "Disabled Policy",
        "targetId": "managed-local",
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "replication": {"enabled": False},
    })
    res_dis = backup_replication.reconcile_policy_replicas(pol_dis)
    assert res_dis["status"] == "skipped"
    assert res_dis["reason"] == "replication-disabled"

    # 2. Policy with replication enabled but no targets
    pol_empty = "pol_repl_empty"
    backup_policies.create_policy({
        "policyId": pol_empty,
        "name": "Empty Targets Policy",
        "targetId": "managed-local",
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "replication": {"enabled": True, "targets": []},
    })
    res_empty = backup_replication.reconcile_policy_replicas(pol_empty)
    assert res_empty["status"] == "noop"
    assert res_empty["policyId"] == pol_empty


# ────────────────────────────────────────────────────────────────────────
# Additional coverage-booster tests targeting uncovered branches (≥ 95%)
# ────────────────────────────────────────────────────────────────────────


def test_target_store_check_liveness_error_path(tmp_settings: Path) -> None:
    """Cover MemoryTargetStore.check_liveness error path (lines 779-780)."""
    store = backup_target_store.MemoryTargetStore()
    # Happy path first
    result = store.check_liveness()
    assert result["status"] == "available"
    assert "latencyMs" in result

    # Inject failure to trigger the except branch
    store.inject_failure("check_liveness", RuntimeError("simulated-outage"))
    result = store.check_liveness()
    assert result["status"] == "unavailable"
    assert "simulated-outage" in result["error"]
    store.clear_failure("check_liveness")


def test_target_store_delete_if_match_etag_mismatch(tmp_settings: Path) -> None:
    """Cover delete_if_match with etag mismatch (line 373-374 in FilesystemTargetStore)."""
    store = backup_target_store.MemoryTargetStore()
    put_result = store.put_if_absent("test/obj.bin", b"data123", checksum_sha256=_sha256(b"data123"))
    with pytest.raises(AppError, match="conditional-delete-failed"):
        store.delete_if_match("test/obj.bin", expected_etag="wrong-etag")
    # Correct etag succeeds
    assert store.delete_if_match("test/obj.bin", expected_etag=put_result.etag) is True
    # Missing key returns False
    assert store.delete_if_match("nonexistent/key.bin") is False


def test_target_store_list_multipart_parts(tmp_settings: Path) -> None:
    """Cover list_multipart_parts on MemoryTargetStore."""
    store = backup_target_store.MemoryTargetStore()
    data = b"multipart-test-data"
    upload = store.begin_multipart("parts/test.bin", checksum_sha256=_sha256(data))
    store.upload_part(upload, 1, data, checksum_sha256=_sha256(data))
    parts = store.list_multipart_parts(upload)
    assert len(parts) == 1
    assert parts[0]["partNumber"] == 1
    assert parts[0]["size"] == len(data)
    # Abort and verify cleanup
    store.abort_multipart(upload)


def test_target_store_get_bytes_with_offset_and_length(tmp_settings: Path) -> None:
    """Cover get_bytes with offset and length parameters (line 284 in FilesystemTargetStore)."""
    store = backup_target_store.MemoryTargetStore()
    data = b"ABCDEFGHIJKLMNOP"
    store.put_if_absent("range/test.bin", data, checksum_sha256=_sha256(data))
    # offset only
    result = store.get_bytes("range/test.bin", offset=4)
    assert result == b"EFGHIJKLMNOP"
    # offset + length
    result = store.get_bytes("range/test.bin", offset=4, length=4)
    assert result == b"EFGH"
    # Missing key
    assert store.get_bytes("nonexistent/key.bin") is None


def test_target_store_hold_key_helpers(tmp_settings: Path) -> None:
    """Cover repair_hold_key and protection_hold_key (lines 97, 101)."""
    assert backup_target_store.repair_hold_key("repair-123") == "holds/repair/repair-123.json"
    assert backup_target_store.protection_hold_key("hold-456") == "holds/protection/hold-456.json"


def test_target_store_list_objects_with_cursor(tmp_settings: Path) -> None:
    """Cover list_objects cursor pagination on MemoryTargetStore."""
    store = backup_target_store.MemoryTargetStore()
    # Populate with a few objects
    for i in range(5):
        d = f"obj{i}".encode()
        store.put_if_absent(f"prefix/obj{i}.bin", d, checksum_sha256=_sha256(d))
    # List with limit=2 to get cursor
    page1 = store.list_objects("prefix/", limit=2)
    assert len(page1.objects) == 2
    assert page1.cursor is not None
    # Continue with cursor
    page2 = store.list_objects("prefix/", cursor=page1.cursor, limit=2)
    assert len(page2.objects) == 2
    # Last page
    page3 = store.list_objects("prefix/", cursor=page2.cursor, limit=2)
    assert len(page3.objects) == 1
    assert page3.cursor is None


def test_filesystem_target_store_check_liveness(tmp_settings: Path) -> None:
    """Cover FilesystemTargetStore.check_liveness (lines 462-467)."""
    root = tmp_settings / ".target-store-liveness"
    root.mkdir(parents=True, exist_ok=True)
    fs_store = backup_target_store.FilesystemTargetStore(root)
    result = fs_store.check_liveness()
    assert result["status"] == "available"

    # Non-existent directory -> "unavailable"
    import shutil
    shutil.rmtree(root)
    result = fs_store.check_liveness()
    assert result["status"] == "unavailable"
    assert "directory-missing" in result.get("error", "")


def test_filesystem_target_store_list_objects_empty_and_cursor(tmp_settings: Path) -> None:
    """Cover FilesystemTargetStore.list_objects with empty dir and cursor (lines 381, 393-396)."""
    root = tmp_settings / ".target-fs-list"
    fs_store = backup_target_store.FilesystemTargetStore(root)
    # root doesn't exist yet -> empty
    page = fs_store.list_objects("prefix/")
    assert len(page.objects) == 0

    # Create root and populate
    root.mkdir(parents=True, exist_ok=True)
    obj_dir = root / "prefix"
    obj_dir.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (obj_dir / f"obj{i}.bin").write_bytes(f"data{i}".encode())
    page_all = fs_store.list_objects("prefix/")
    assert len(page_all.objects) == 3
    # With cursor pointing at first object
    first_key = page_all.objects[0].key
    page_after = fs_store.list_objects("prefix/", cursor=first_key)
    assert len(page_after.objects) == 2


def test_filesystem_target_store_delete_if_match_etag(tmp_settings: Path) -> None:
    """Cover FilesystemTargetStore.delete_if_match with etag mismatch (lines 362-363, 373-374)."""
    root = tmp_settings / ".target-fs-delete"
    root.mkdir(parents=True, exist_ok=True)
    fs_store = backup_target_store.FilesystemTargetStore(root)
    data = b"test-data-for-delete"
    put_res = fs_store.put_if_absent("del/test.bin", data, checksum_sha256=_sha256(data))
    # Mismatch etag
    with pytest.raises(AppError, match="conditional-delete-failed"):
        fs_store.delete_if_match("del/test.bin", expected_etag="wrong")
    # Correct etag
    assert fs_store.delete_if_match("del/test.bin", expected_etag=put_res.etag) is True
    # Already deleted
    assert fs_store.delete_if_match("del/test.bin") is False


def test_filesystem_target_store_list_multipart_parts(tmp_settings: Path) -> None:
    """Cover FilesystemTargetStore.list_multipart_parts (lines 424-432)."""
    root = tmp_settings / ".target-fs-mp"
    root.mkdir(parents=True, exist_ok=True)
    fs_store = backup_target_store.FilesystemTargetStore(root)
    data = b"multipart-fs-data"
    upload = fs_store.begin_multipart("mp/test.bin", checksum_sha256=_sha256(data))
    fs_store.upload_part(upload, 1, data, checksum_sha256=_sha256(data))
    parts = fs_store.list_multipart_parts(upload)
    assert len(parts) == 1
    assert parts[0]["partNumber"] == 1
    # Complete
    result = fs_store.complete_multipart_if_absent(upload)
    assert result.key == "mp/test.bin"


def test_filesystem_target_store_put_if_match_etag_race(tmp_settings: Path) -> None:
    """Cover FilesystemTargetStore.put_if_match double-check etag mismatch (lines 362-363)."""
    root = tmp_settings / ".target-fs-put-match"
    root.mkdir(parents=True, exist_ok=True)
    fs_store = backup_target_store.FilesystemTargetStore(root)
    data = b"original"
    put1 = fs_store.put_if_absent("match/test.bin", data, checksum_sha256=_sha256(data))
    # Successful put_if_match
    new_data = b"updated"
    put2 = fs_store.put_if_match("match/test.bin", new_data, expected_etag=put1.etag, checksum_sha256=_sha256(new_data))
    assert put2.created is False
    # Wrong etag
    with pytest.raises(AppError, match="conditional-replace-failed"):
        fs_store.put_if_match("match/test.bin", b"bad", expected_etag="wrong")


def test_writer_lease_path_property_store_only(tmp_settings: Path) -> None:
    """Cover TargetWriterLease.path when root is None (line 118)."""
    from deepseek_infra.infra.workspace.backup_writer_lease import TargetWriterLease

    store = backup_target_store.MemoryTargetStore()
    lease = TargetWriterLease(
        store=store,
        target_id="store-lease",
        owner_run_id="run-1",
        owner_instance_id="inst-1",
        fencing_token=1,
    )
    with pytest.raises(AppError, match="remote writer lease has no local path"):
        _ = lease.path


def test_writer_lease_note_server_date_positive_skew(tmp_settings: Path) -> None:
    """Cover _note_server_date with positive skew (line 183)."""
    from deepseek_infra.infra.workspace.backup_writer_lease import TargetWriterLease

    fixed_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    lease = TargetWriterLease(
        root=tmp_settings / ".lease-skew",
        target_id="skew-test",
        owner_run_id="run-skew",
        owner_instance_id="inst-skew",
        fencing_token=1,
        clock=lambda: fixed_time,
    )
    # Server is 30 seconds ahead → positive skew
    future_iso = "2026-01-01T12:00:30Z"
    lease._note_server_date(future_iso)
    assert lease._server_skew.total_seconds() > 0

    # Server is 30 seconds behind → negative skew with safety margin
    past_iso = "2026-01-01T11:59:30Z"
    lease._note_server_date(past_iso)
    assert lease._server_skew.total_seconds() < 0

    # Invalid date string → no change
    old_skew = lease._server_skew
    lease._note_server_date("not-a-date")
    assert lease._server_skew == old_skew

    # Empty string → no change
    lease._note_server_date("")
    assert lease._server_skew == old_skew


def test_writer_lease_assert_owned_expired(tmp_settings: Path) -> None:
    """Cover assert_owned when lease is expired (line 325)."""
    from deepseek_infra.infra.workspace.backup_writer_lease import TargetWriterLease

    root = tmp_settings / ".lease-expired"
    lease = TargetWriterLease(
        root=root,
        target_id="expired-test",
        owner_run_id="run-exp",
        owner_instance_id="inst-exp",
        fencing_token=1,
        lease_seconds=1,
    )
    lease.acquire()
    # Advance clock past expiry
    future = datetime(2099, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    lease._clock = lambda: future
    with pytest.raises(AppError, match="expired"):
        lease.assert_owned()
    lease.release()


def test_writer_lease_assert_payload_owned_missing(tmp_settings: Path) -> None:
    """Cover _assert_payload_owned when payload is None (line 317)."""
    from deepseek_infra.infra.workspace.backup_writer_lease import TargetWriterLease

    lease = TargetWriterLease(
        root=tmp_settings / ".lease-miss",
        target_id="miss-test",
        owner_run_id="run-miss",
        owner_instance_id="inst-miss",
        fencing_token=1,
    )
    with pytest.raises(AppError, match="missing or unreadable"):
        lease._assert_payload_owned(None)


def test_writer_lease_assert_payload_owned_different_owner(tmp_settings: Path) -> None:
    """Cover _assert_payload_owned when payload has different owner (line 318-319)."""
    from deepseek_infra.infra.workspace.backup_writer_lease import TargetWriterLease

    lease = TargetWriterLease(
        root=tmp_settings / ".lease-diff",
        target_id="diff-test",
        owner_run_id="run-mine",
        owner_instance_id="inst-mine",
        fencing_token=1,
    )
    with pytest.raises(AppError, match="lost to another writer"):
        lease._assert_payload_owned({"ownerRunId": "run-other", "ownerInstanceId": "inst-other", "fencingToken": 99})


def test_writer_lease_release_on_store(tmp_settings: Path) -> None:
    """Cover release on store-backed lease (lines 331-337)."""
    from deepseek_infra.infra.workspace.backup_writer_lease import TargetWriterLease

    store = backup_target_store.MemoryTargetStore()
    lease = TargetWriterLease(
        store=store,
        target_id="store-release",
        owner_run_id="run-rel",
        owner_instance_id="inst-rel",
        fencing_token=1,
    )
    lease.acquire()
    assert lease.acquired is True
    lease.release()
    assert lease.acquired is False
    assert lease._etag is None


def test_writer_lease_acquire_preempt_expired(tmp_settings: Path) -> None:
    """Cover local acquire preempting an expired lease with lower fencing token (lines 209-224)."""
    from deepseek_infra.infra.workspace.backup_writer_lease import TargetWriterLease
    import json

    root = tmp_settings / ".lease-preempt"

    # First, create an expired lease from another owner
    lease1 = TargetWriterLease(
        root=root,
        target_id="preempt-test",
        owner_run_id="run-old",
        owner_instance_id="inst-old",
        fencing_token=1,
        lease_seconds=1,
    )
    lease1.acquire()
    # Manually expire it by overwriting with old timestamp
    expired_payload = lease1._payload(datetime(2020, 1, 1, tzinfo=timezone.utc))
    lease1._write(expired_payload)
    lease1.release()

    # Write the expired lease back for preemption
    path = root / ".target-lock" / "writer.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(expired_payload, sort_keys=True), encoding="utf-8")

    # Now a new lease should preempt it
    lease2 = TargetWriterLease(
        root=root,
        target_id="preempt-test",
        owner_run_id="run-new",
        owner_instance_id="inst-new",
        fencing_token=2,
    )
    lease2.acquire()
    assert lease2.acquired is True
    lease2.release()


def test_writer_lease_acquire_store_preempt(tmp_settings: Path) -> None:
    """Cover _acquire_store preempting expired lease (lines 256-282)."""
    from deepseek_infra.infra.workspace.backup_writer_lease import TargetWriterLease

    store = backup_target_store.MemoryTargetStore()

    # Create an expired lease in the store
    lease1 = TargetWriterLease(
        store=store,
        target_id="store-preempt",
        owner_run_id="run-old-s",
        owner_instance_id="inst-old-s",
        fencing_token=1,
        lease_seconds=1,
    )
    lease1.acquire()
    # Manually expire: write a payload with past expiry
    from deepseek_infra.infra.workspace.backup_target_store import writer_lease_key, put_json_if_match
    key = writer_lease_key()
    expired = lease1._payload(datetime(2020, 1, 1, tzinfo=timezone.utc))
    meta = store.stat(key)
    assert meta is not None
    put_json_if_match(store, key, expired, expected_etag=meta.etag)

    # New lease preempts
    lease2 = TargetWriterLease(
        store=store,
        target_id="store-preempt",
        owner_run_id="run-new-s",
        owner_instance_id="inst-new-s",
        fencing_token=2,
    )
    lease2.acquire()
    assert lease2.acquired is True
    lease2.release()


def test_recovery_lease_renew_session_colon_key_migration(tmp_settings: Path) -> None:
    """Cover renew_session with colon-containing holdKey migration (lines 87-99 of backup_recovery_lease.py)."""
    from deepseek_infra.infra.workspace import backup_recovery_lease

    store = backup_target_store.MemoryTargetStore()
    now = datetime.now(tz=timezone.utc)

    # Create a hold with a colon-containing key
    colon_key = "holds:restore:legacy-id.json"
    hold_data = {
        "schemaVersion": 2,
        "generation": 1,
        "createdAt": _utc_iso(now),
        "expiresAt": _utc_iso(now + timedelta(hours=6)),
    }
    store.put_if_absent(colon_key, json.dumps(hold_data).encode())

    session: dict[str, Any] = {
        "phase": "active",
        "holdKeys": [colon_key],
    }
    result = backup_recovery_lease.renew_session(store, session, now=now, min_interval_seconds=0)
    assert result is True
    # Key should have been migrated to dash form
    assert any("-" in str(k) for k in session.get("holdKeys", []))


def test_recovery_lease_renew_recovery_hold(tmp_settings: Path) -> None:
    """Cover renew_recovery_hold (lines 116-127 of backup_recovery_lease.py)."""
    from deepseek_infra.infra.workspace import backup_recovery_lease

    store = backup_target_store.MemoryTargetStore()
    now = datetime.now(tz=timezone.utc)
    key = "holds/restore/test-hold.json"
    hold_data = {
        "schemaVersion": 3,
        "generation": 1,
        "createdAt": _utc_iso(now),
        "expiresAt": _utc_iso(now + timedelta(hours=6)),
    }
    store.put_if_absent(key, json.dumps(hold_data).encode())

    hold_entry: dict[str, Any] = {"holdKey": key, "generation": 1}
    renewed = backup_recovery_lease.renew_recovery_hold(store, hold_entry, now=now)
    assert renewed["generation"] >= 2
    assert "renewedAt" in renewed


def test_recovery_lease_renew_recovery_hold_missing_key(tmp_settings: Path) -> None:
    """Cover renew_recovery_hold with missing holdKey (line 118)."""
    from deepseek_infra.infra.workspace import backup_recovery_lease

    store = backup_target_store.MemoryTargetStore()
    with pytest.raises(AppError, match="Missing holdKey"):
        backup_recovery_lease.renew_recovery_hold(store, {})


def test_component_cache_canonical_digest_edge_cases(tmp_settings: Path) -> None:
    """Cover _canonical_digest edge cases in backup_component_cache."""
    from deepseek_infra.infra.workspace import backup_component_cache

    # Valid 64-char hex
    hex_str = "a" * 64
    assert backup_component_cache._canonical_digest(hex_str) == hex_str

    # sha256: prefix with valid hex
    valid_with_prefix = "sha256:" + "b" * 64
    assert backup_component_cache._canonical_digest(valid_with_prefix) == "b" * 64

    # sha256: prefix with invalid hex → hashed
    invalid_hex = "sha256:not-valid-hex"
    result = backup_component_cache._canonical_digest(invalid_hex)
    assert len(result) == 64  # sha256 of the string

    # Non-hex non-sha256: → error
    with pytest.raises(ValueError, match="canonical sha256"):
        backup_component_cache._canonical_digest("not-hex-at-all")

    # Non-string → error
    with pytest.raises(ValueError, match="component digest must be canonical sha256"):
        backup_component_cache._canonical_digest(12345)  # type: ignore[arg-type]


def test_target_store_get_bytes_offset_on_filesystem(tmp_settings: Path) -> None:
    """Cover FilesystemTargetStore.get_bytes with offset (line 284)."""
    root = tmp_settings / ".target-fs-range"
    root.mkdir(parents=True, exist_ok=True)
    fs_store = backup_target_store.FilesystemTargetStore(root)
    data = b"0123456789ABCDEF"
    fs_store.put_if_absent("range/fs.bin", data, checksum_sha256=_sha256(data))
    result = fs_store.get_bytes("range/fs.bin", offset=4, length=4)
    assert result == b"4567"
    result_all = fs_store.get_bytes("range/fs.bin", offset=10)
    assert result_all == b"ABCDEF"


def test_backup_reconcile_assert_catalog_committed_corrupt(tmp_settings: Path) -> None:
    """Cover assert_catalog_committed and catalog_corrupt_backup_ids (lines 60-76 of backup_reconcile.py)."""
    from deepseek_infra.infra.workspace import backup_catalog, backup_reconcile, backup_writer_lease

    root = tmp_settings / ".reconcile-corrupt-target"
    root.mkdir(parents=True, exist_ok=True)

    # 1. Empty catalog -> no corrupt
    backup_reconcile.assert_catalog_committed(root)
    assert backup_reconcile.catalog_corrupt_backup_ids(root) == []

    # 2. Append a catalog record using backup_catalog.append_receipt without matching commit marker
    rec = {
        "schemaVersion": 2,
        "backupId": "uncommitted-backup-123",
        "filename": "uncommitted-backup-123.tar.age",
        "policyId": "pol-1",
        "createdAt": _utc_iso(),
    }
    writer = backup_writer_lease.TargetWriterLease(
        root=root,
        target_id="managed-local",
        owner_run_id="run-c",
        owner_instance_id="inst-c",
        fencing_token=1,
    )
    writer.acquire()
    try:
        backup_catalog.append_receipt(root, rec, writer=writer)
    finally:
        writer.release()

    corrupt = backup_reconcile.catalog_corrupt_backup_ids(root)
    assert "uncommitted-backup-123" in corrupt
    with pytest.raises(AppError, match="catalog-corrupt"):
        backup_reconcile.assert_catalog_committed(root)


def test_backup_reconcile_target_orphaned_and_rebuild(tmp_settings: Path) -> None:
    """Cover reconcile_target orphaned objects/receipts and catalog rebuild (lines 120-220)."""
    from deepseek_infra.infra.workspace import backup_reconcile, backup_writer_lease
    import os

    root = tmp_settings / ".reconcile-orphans-target"
    root.mkdir(parents=True, exist_ok=True)

    # Write an uncommitted old object (> 24h ago)
    obj_dir = root / "objects" / "sha256" / "ab"
    obj_dir.mkdir(parents=True, exist_ok=True)
    old_obj = obj_dir / ("ab" + "0" * 62 + ".age")
    old_obj.write_bytes(b"old-orphan-content")
    old_time = (datetime.now(tz=timezone.utc) - timedelta(days=2)).timestamp()
    os.utime(old_obj, (old_time, old_time))

    # Write an uncommitted old receipt (> 24h ago)
    rec_dir = root / "receipts"
    rec_dir.mkdir(parents=True, exist_ok=True)
    old_rec = rec_dir / "old-orphan-backup.json"
    old_rec.write_text(json.dumps({"backupId": "old-orphan-backup"}), encoding="utf-8")
    os.utime(old_rec, (old_time, old_time))

    # Reconcile target with writer lease
    writer = backup_writer_lease.TargetWriterLease(
        root=root,
        target_id="managed-local",
        owner_run_id="run-rec",
        owner_instance_id="inst-rec",
        fencing_token=1,
    )
    writer.acquire()
    try:
        report = backup_reconcile.reconcile_target(root, target_id="managed-local", writer=writer, orphan_grace_seconds=3600)
        assert len(report["orphanedObjects"]) >= 1
        assert len(report["orphanedReceipts"]) >= 1
    finally:
        writer.release()


def test_backup_reconcile_target_store_head_advancement(tmp_settings: Path) -> None:
    """Cover reconcile_target_store remote head advancement (lines 250-330)."""
    from deepseek_infra.infra.workspace import backup_reconcile, backup_writer_lease

    store = backup_target_store.MemoryTargetStore()

    # Write a commit marker with high generation
    commit_data = {
        "schemaVersion": 4,
        "commitHash": "c" * 64,
        "backupId": "b-store-1",
        "runId": "run-store-1",
        "targetGeneration": 10,
        "committedAt": _utc_iso(),
        "objectSetDigest": "d" * 64,
        "controlObjectDigest": "ctrl" + "0" * 60,
        "receiptDigest": "rec" + "0" * 61,
        "storageProtocol": "object-set-v1",
    }
    store.put_if_absent("commits/c.json", json.dumps(commit_data).encode())

    # Write receipt
    receipt_data = {
        "schemaVersion": 4,
        "backupId": "b-store-1",
        "policyId": "pol-1",
        "storageProtocol": "object-set-v1",
        "objectSetDigest": "d" * 64,
        "controlObjectDigest": "ctrl" + "0" * 60,
        "objects": [{"digest": "obj1" + "0" * 60, "size": 10}],
        "controlObject": {"digest": "ctrl" + "0" * 60, "size": 10},
    }
    r_bytes = json.dumps(receipt_data).encode()
    store.put_if_absent("receipts/b-store-1.json", r_bytes)
    # Update commit's receiptDigest to match actual sha256
    actual_rd = hashlib.sha256(r_bytes).hexdigest()
    commit_data["receiptDigest"] = actual_rd
    store.delete_if_match("commits/c.json")
    store.put_if_absent("commits/c.json", json.dumps(commit_data).encode())

    # Write object files
    store.put_if_absent("objects/sha256/ob/obj1" + "0" * 60 + ".age", b"content1")
    store.put_if_absent("objects/sha256/ct/ctrl" + "0" * 60 + ".age", b"content2")

    writer = backup_writer_lease.TargetWriterLease(
        store=store,
        target_id="remote-store-target",
        owner_run_id="run-rec-s",
        owner_instance_id="inst-rec-s",
        fencing_token=1,
    )
    writer.acquire()
    try:
        report = backup_reconcile.reconcile_target_store(store, target_id="remote-store-target", writer=writer)
        assert report["headAdvanced"] is True
    finally:
        writer.release()


def test_backup_replication_hold_protection_and_catalog_store(tmp_settings: Path) -> None:
    """Cover is_source_held on store and append_target_local_catalog (lines 330-371 of backup_replication.py)."""
    store = backup_target_store.MemoryTargetStore()
    target_store_wrapper = MockTargetWrapper("store-target", store=store)

    # 1. Put an active repair hold into store
    now = datetime.now(tz=timezone.utc)
    hold_data = {
        "schemaVersion": 3,
        "policyId": "pol-hold",
        "backupId": "b-hold-1",
        "expiresAt": _utc_iso(now + timedelta(hours=2)),
    }
    store.put_if_absent("holds/repair/hold1.json", json.dumps(hold_data).encode())

    # 2. Check hold protection
    assert backup_replication.is_source_held("store-target", "pol-hold", "b-hold-1", target=target_store_wrapper) is True
    assert backup_replication.is_source_held("store-target", "pol-hold", "b-other", target=target_store_wrapper) is False

    # 3. Test append_target_local_catalog on root and store
    rec = {
        "policyId": "pol-cat",
        "backupId": "b-cat-1",
        "createdAt": _utc_iso(),
    }
    root_target = MockTargetWrapper("local-root", root=tmp_settings / ".cat-root")
    backup_replication.append_target_local_catalog(root_target, rec)
    assert (tmp_settings / ".cat-root" / "catalogs" / "pol-cat.jsonl").is_file()

    backup_replication.append_target_local_catalog(target_store_wrapper, rec)
    assert store.stat("catalogs/pol-cat/b-cat-1.json") is not None


def test_server_web_share_target_and_agent_runs_extra(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cover server.py web share target (lines 712-735) and agent-runs action endpoints."""
    from starlette.testclient import TestClient
    from deepseek_infra.core import config
    from deepseek_infra.web import server as server_module

    auth_token = config.settings.auth.token or "test_secret_token"
    auth_headers = {"Authorization": f"Bearer {auth_token}"}

    srv, _ = server_module.create_server(0, host="127.0.0.1")
    client = TestClient(srv.app, base_url="http://127.0.0.1")

    # 1. Test /share-target multipart upload
    response = client.post(
        "/share-target",
        data={"title": "Test Title", "text": "Test body", "url": "https://example.com"},
        files={"file": ("shared.txt", b"hello world from shared file", "text/plain")},
        headers=auth_headers,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "share=" in response.headers.get("location", "")

    # 2. Test /api/agent-runs with confirmPlan
    monkeypatch.setattr(
        server_module,
        "create_agent_run",
        lambda p, **kw: {"runId": "run_fake_455", "status": "planned", "plan": []},
    )
    monkeypatch.setattr(
        server_module.agent_run_registry,
        "ensure_started",
        lambda *a, **kw: None,
    )
    resp = client.post(
        "/api/agent-runs",
        json={
            "payload": {
                "apiKey": "sk-dummy-key-455",
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "hi"}],
            },
            "confirmPlan": True,
            "agentPreset": "full",
            "conversationId": "c-123",
            "messageId": "m-456",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["runId"] == "run_fake_455"


def test_write_continuity_full_lifecycle_and_cas_errors(tmp_settings: Path) -> None:
    """Cover backup_write_continuity.py liveness, failover, failback, promotion and CAS failures."""
    from deepseek_infra.infra.workspace import backup_write_continuity, backup_policies

    # 1. perform_liveness_preflight with unavailable targets
    non_existent_root = MockTargetWrapper("missing", root=tmp_settings / "non-existent-dir")
    ev1 = backup_write_continuity.perform_liveness_preflight("missing", target=non_existent_root)
    assert ev1["status"] == "unavailable"

    no_root_no_store = MockTargetWrapper("empty")
    ev2 = backup_write_continuity.perform_liveness_preflight("empty", target=no_root_no_store)
    assert ev2["status"] == "unavailable"

    # Store with error
    class ErrorStore(backup_target_store.MemoryTargetStore):
        def check_liveness(self, *, timeout_seconds: float = 5.0) -> dict[str, Any]:
            return {"status": "error", "error": "simulated liveness failure"}

    err_store = ErrorStore()
    err_wrapper = MockTargetWrapper("err-store", store=err_store)
    ev3 = backup_write_continuity.perform_liveness_preflight("err-store", target=err_wrapper, policy_id="pol-test")
    assert ev3["status"] == "unavailable"

    # 2. evaluate_failback_eligibility branches
    # 2a. Already primary
    eligible, reason, _ = backup_write_continuity.evaluate_failback_eligibility("pol-test")
    assert eligible is False
    assert reason == "already-primary"

    # 2b. Failover transition
    backup_write_continuity.execute_failover_transition("pol-test", "secondary-target", reason="primary-down")
    state = backup_write_continuity.get_write_continuity_state("pol-test")
    assert state["activeWriteTargetId"] == "secondary-target"
    assert state["activeWriteTargetRole"] == "failover"

    # 2c. Primary not healthy
    backup_write_continuity.record_target_liveness("pol-test", "managed-local", status="unavailable")
    eligible, reason, _ = backup_write_continuity.evaluate_failback_eligibility("pol-test")
    assert eligible is False
    assert reason == "primary-not-healthy"

    # 2d. Primary stability insufficient
    now = datetime.now(tz=timezone.utc)
    backup_write_continuity.record_target_liveness("pol-test", "managed-local", status="available", now=now)
    eligible, reason, _ = backup_write_continuity.evaluate_failback_eligibility("pol-test", stability_window_seconds=1800, now=now + timedelta(seconds=10))
    assert eligible is False
    assert "primary-stability-insufficient" in reason

    # 2e. Execute failback transition
    fb_state = backup_write_continuity.execute_failback_transition("pol-test")
    assert fb_state["activeWriteTargetRole"] == "primary"

    # 3. promote_primary_target and CAS mismatches
    # Register secondary target in target registry
    sec_dir = tmp_settings / ".sec-target-dir"
    sec_dir.mkdir(parents=True, exist_ok=True)
    target_record = {
        "schemaVersion": 1,
        "targetId": "target_secondary_01",
        "kind": "filesystem",
        "label": "Secondary Target",
        "path": str(sec_dir),
    }
    backup_targets._atomic_write_json(backup_targets._registry_path("target_secondary_01"), target_record)

    # Create policy with replication
    pol_id = "pol-prom-test"
    backup_policies.create_policy({
        "policyId": pol_id,
        "name": "Promotion Test Policy",
        "enabled": True,
        "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
        "protection": {"mode": "age-recipient", "recipients": ["age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"]},
        "targetId": "managed-local",
        "primaryTargetId": "managed-local",
        "policyRevision": 1,
        "replication": {
            "enabled": True,
            "targets": [{"targetId": "target_secondary_01", "mode": "required"}],
        },
    })

    # CAS failure on policy revision
    with pytest.raises(AppError, match="CAS mismatch on policyRevision"):
        backup_write_continuity.promote_primary_target(pol_id, "target_secondary_01", expected_policy_revision=999)

    # CAS failure on failover epoch
    with pytest.raises(AppError, match="CAS mismatch on failoverEpoch"):
        backup_write_continuity.promote_primary_target(pol_id, "target_secondary_01", expected_failover_epoch=999)

    # Successful promotion
    prom_res = backup_write_continuity.promote_primary_target(
        pol_id,
        "target_secondary_01",
        expected_policy_revision=1,
        expected_failover_epoch=0,
    )
    assert prom_res["status"] == "promoted"
    assert prom_res["newPrimaryTargetId"] == "target_secondary_01"
    assert prom_res["previousPrimaryTargetId"] == "managed-local"







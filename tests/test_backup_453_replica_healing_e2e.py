"""End-to-End multi-target replica self-healing and lifecycle governance test suite."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_policies,
    backup_recovery_planner,
    backup_replication,
    backup_targets,
)


@pytest.fixture(autouse=True)
def _tempdir_outside_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / ".sys-temp"
    fake_temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def test_two_target_e2e_replica_self_healing_and_failover(tmp_settings: Path) -> None:
    # 1. Setup 3 targets: Primary, Replica A, Replica B
    base = tmp_settings / "e2e_targets"
    pri_dir = base / "primary"
    rep_a_dir = base / "replica_a"
    rep_b_dir = base / "replica_b"
    pri_dir.mkdir(parents=True, exist_ok=True)
    rep_a_dir.mkdir(parents=True, exist_ok=True)
    rep_b_dir.mkdir(parents=True, exist_ok=True)

    t_pri = backup_targets.init_target(pri_dir, label="Primary")
    t_a = backup_targets.init_target(rep_a_dir, label="Replica A")
    t_b = backup_targets.init_target(rep_b_dir, label="Replica B")

    pri_id = str(t_pri["targetId"])
    rep_a_id = str(t_a["targetId"])
    rep_b_id = str(t_b["targetId"])

    # 2. Configure Policy with multi-target replication
    policy_id = "pol_e2e_healing"
    policy = {
        "policyId": policy_id,
        "name": "E2E Replica Healing Policy",
        "targetId": pri_id,
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "replication": {
            "enabled": True,
            "minCommittedCopies": 3,
            "maxReplicaLagSeconds": 3600,
            "targets": [
                {"targetId": rep_a_id, "mode": "required"},
                {"targetId": rep_b_id, "mode": "required"},
            ],
        },
        "recoveryObjectives": {
            "rpoSeconds": 7200,
            "rtoSeconds": 300,
            "maxReplicaLagSeconds": 3600,
        },
    }
    backup_policies.create_policy(policy)

    # 3. Simulate a backup object set package published across all 3 targets
    # Component 1 (data)
    comp1_data = b"CIPHERTEXT-ENCRYPTED-COMPONENT-DATA-1"
    comp1_digest = hashlib.sha256(comp1_data).hexdigest()
    # Component 2 (control)
    ctrl_data = b"CIPHERTEXT-ENCRYPTED-CONTROL-MANIFEST-2"
    ctrl_digest = hashlib.sha256(ctrl_data).hexdigest()

    backup_id = "bk_e2e_001"
    objset_digest = "objset_" + hashlib.sha256(b"manifest-content").hexdigest()

    # Store components in Primary and Replica A
    for root, tid in [(pri_dir, pri_id), (rep_a_dir, rep_a_id), (rep_b_dir, rep_b_id)]:
        # write data comp
        dp = root / "objects" / comp1_digest[:2] / comp1_digest[2:4] / f"{comp1_digest}.age"
        dp.parent.mkdir(parents=True, exist_ok=True)
        dp.write_bytes(comp1_data)
        # write control comp
        cp = root / "control" / f"{ctrl_digest}.age"
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_bytes(ctrl_data)

        # write receipt
        rcpt = {
            "schemaVersion": 4,
            "backupId": backup_id,
            "policyId": policy_id,
            "targetId": tid,
            "storageProtocol": "object-set-v1",
            "objectSetDigest": objset_digest,
            "controlObjectDigest": ctrl_digest,
            "size": len(comp1_data) + len(ctrl_data),
            "objects": [
                {"digest": comp1_digest, "size": len(comp1_data)},
                {"digest": ctrl_digest, "size": len(ctrl_data)},
            ],
        }
        r_bytes = (json.dumps(rcpt, indent=2, sort_keys=True) + "\n").encode("utf-8")
        r_path = root / "receipts" / f"{backup_id}.json"
        r_path.parent.mkdir(parents=True, exist_ok=True)
        r_path.write_bytes(r_bytes)

        # write commit
        cmt = {
            "schemaVersion": 4,
            "targetGeneration": 1,
            "previousCommitHash": "0" * 64,
            "fencingToken": 1,
            "runId": "run_init",
            "policyId": policy_id,
            "scheduleSlot": "s1",
            "slotDigest": "slot_1",
            "backupId": backup_id,
            "committedAt": _utc_iso(),
            "receiptDigest": hashlib.sha256(r_bytes).hexdigest(),
            "storageProtocol": "object-set-v1",
            "objectSetDigest": objset_digest,
            "controlObjectDigest": ctrl_digest,
        }
        c_bytes = (json.dumps(cmt, indent=2, sort_keys=True) + "\n").encode("utf-8")
        cmt["commitHash"] = hashlib.sha256(c_bytes).hexdigest()
        c_path = root / "commits" / policy_id / f"{backup_id}.json"
        c_path.parent.mkdir(parents=True, exist_ok=True)
        c_path.write_bytes(json.dumps(cmt, indent=2, sort_keys=True).encode("utf-8") + b"\n")

        # write head
        h_path = root / "control" / "head.json"
        h_path.write_bytes(json.dumps({"latestCommitHash": cmt["commitHash"], "targetGeneration": 1}, indent=2).encode("utf-8") + b"\n")

        # Record logical copy in ledger
        backup_dr_ledger.record_logical_recovery_copy(
            target_id=tid,
            policy_id=policy_id,
            backup_id=backup_id,
            committed_at=str(cmt["committedAt"]),
            object_set_digest=objset_digest,
            recoverable=True,
            role="primary" if tid == pri_id else "replica",
            state="healthy",
        )

    # 4. Check initial compliance -> healthy (3 committed copies)
    comp = backup_replication.replication_compliance(policy=policy, backup_id=backup_id)
    assert comp["compliance"] == "healthy"
    assert comp["committedCopies"] == 3

    # 5. Corrupt Component 1 on Replica B
    corrupt_file = rep_b_dir / "objects" / comp1_digest[:2] / comp1_digest[2:4] / f"{comp1_digest}.age"
    corrupt_file.write_bytes(b"CORRUPTED-CIPHERTEXT-BIT-FLIP-ON-REPLICA-B")

    # Mark Replica B copy as degraded in DR Ledger
    backup_dr_ledger.update_recovery_copy_state(
        rep_b_id,
        policy_id,
        backup_id,
        state="degraded",
        recoverable=False,
        last_failure="component-checksum-mismatch",
    )

    # Check compliance -> degraded (only 2 healthy copies remaining)
    comp_degraded = backup_replication.replication_compliance(policy=policy, backup_id=backup_id)
    assert comp_degraded["compliance"] == "degraded"
    assert comp_degraded["healthyCopies"] == 2

    # 6. Execute Replica Self-Healing Reconciler (Zero Age decrypt, Zero Age encrypt)
    rec_report = backup_replication.reconcile_policy_replicas(policy_id)
    assert rec_report["status"] == "completed"
    assert rec_report["repairsTriggered"] == 1
    assert rec_report["repairsSucceeded"] == 1

    # Verify Replica B component was restored to exact ciphertext
    assert corrupt_file.read_bytes() == comp1_data
    # Verify corrupted artifact was moved into .quarantine
    assert len(list((rep_b_dir / ".quarantine").glob("*"))) == 1

    # Verify compliance converged back to healthy
    comp_healed = backup_replication.replication_compliance(policy=policy, backup_id=backup_id)
    assert comp_healed["compliance"] == "healthy"
    assert comp_healed["healthyCopies"] == 3

    # 7. Execute Recovery Planning on Replica B
    plan = backup_recovery_planner.plan_recovery(
        policy_id=policy_id,
        backup_id=backup_id,
        preferred_target_id=rep_b_id,
    )
    assert plan["selectedTargetId"] == rep_b_id

    # 8. Test Logical Point Retirement
    backup_dr_ledger.mark_logical_recovery_point_retired(policy_id, backup_id)
    assert backup_dr_ledger.is_logical_recovery_point_retired(policy_id, backup_id) is True

    # Run Reconciler again -> should skip retired point
    rec_report_2 = backup_replication.reconcile_policy_replicas(policy_id)
    assert rec_report_2["scannedPoints"] == 0
    assert rec_report_2["repairsTriggered"] == 0

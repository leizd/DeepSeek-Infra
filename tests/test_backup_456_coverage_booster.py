"""DeepSeek Infra 4.5.x Comprehensive Coverage Booster.

Targets all newly added and edge-case branches in:
- deepseek_infra.infra.workspace.backup_replication
- deepseek_infra.infra.workspace.backup_executor
- deepseek_infra.infra.workspace.backup_write_continuity
- deepseek_infra.infra.workspace.backup_policies
- deepseek_infra.infra.workspace.backup_targets
- deepseek_infra.infra.workspace.backup_scheduler
- deepseek_infra.web.routes.backup_governance
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import APIRouter, Request
from fastapi.routing import APIRoute

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_dr_ledger,
    backup_dr_readiness,
    backup_executor,
    backup_policies,
    backup_publish,
    backup_reconcile,
    backup_replication,
    backup_retention,
    backup_scheduler,
    backup_targets,
    backup_write_continuity,
)
from deepseek_infra.web.routes import backup_governance


@pytest.fixture(autouse=True)
def _tempdir_outside_targets(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / ".sys-temp"
    fake_temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def _init_local_target(target_id: str, tmp_path: Path, failure_domain: str = "fd-a", priority: int = 100) -> dict[str, Any]:
    norm_id = target_id if target_id.startswith("target_") else f"target_{target_id.replace('-', '_')}"
    root = tmp_path / "target_roots" / norm_id
    root.mkdir(parents=True, exist_ok=True)
    marker = root / backup_targets.TARGET_MARKER_NAME
    marker_payload = {
        "schemaVersion": backup_targets.TARGET_SCHEMA_VERSION,
        "targetId": norm_id,
        "targetNonce": f"nonce_{norm_id}",
        "incarnationId": f"inc_{norm_id}",
        "ownerInstallationId": backup_targets.installation_id(),
        "targetGeneration": 0,
        "latestCommitHash": backup_targets.TARGET_GENESIS_HASH,
        "createdAt": "2026-08-17T00:00:00Z",
    }
    marker.write_text(json.dumps(marker_payload), encoding="utf-8")
    return backup_targets.init_target(
        path=str(root),
        label=f"Target {norm_id}",
        failure_domain=failure_domain,
        priority=priority,
    )


def _request(payload: dict[str, Any] | None = None, *, query: str = "", method: str = "POST") -> Request:
    body = json.dumps(payload or {}).encode("utf-8")
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": query.encode("ascii"),
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))],
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
            "root_path": "",
        },
        receive,
    )


def _endpoint(router: APIRouter, path: str, method: str = "POST") -> Callable[..., Any]:
    for route in router.routes:
        if isinstance(route, APIRoute) and route.path == path and method in (route.methods or set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _call(endpoint: Callable[..., Any], payload: dict[str, Any] | None = None, method: str = "POST", query: str = "", **path_params: str) -> Any:
    return asyncio.run(endpoint(_request(payload, query=query, method=method), **path_params))


def _json(response: Any) -> dict[str, Any]:
    body = getattr(response, "body", None)
    if isinstance(body, (bytes, bytearray)):
        return json.loads(body.decode("utf-8"))
    raise AssertionError(f"unsupported response body: {type(body)}")


def test_rebalance_jobs_crud_and_runner(tmp_path: Path) -> None:
    t_a = _init_local_target("target_a", tmp_path, failure_domain="fd-a")
    t_b = _init_local_target("target_b", tmp_path, failure_domain="fd-b")

    job = backup_replication.create_rebalance_job(
        policy_id="pol-1",
        backup_id="bk-1",
        dest_target_id=t_b["targetId"],
        source_target_id=t_a["targetId"],
        reason="test-rebalance",
    )
    assert job["jobId"]
    assert job["phase"] == "pending"

    # Reading job
    read = backup_replication.read_rebalance_job(job["jobId"])
    assert read is not None
    assert read["jobId"] == job["jobId"]

    # Nonexistent job
    assert backup_replication.read_rebalance_job("nonexistent") is None

    # Listing jobs
    listed = backup_replication.list_rebalance_jobs(policy_id="pol-1", backup_id="bk-1")
    assert len(listed) >= 1
    assert listed[0]["jobId"] == job["jobId"]

    # Filters in list_rebalance_jobs
    assert len(backup_replication.list_rebalance_jobs(policy_id="other")) == 0
    assert len(backup_replication.list_rebalance_jobs(backup_id="other")) == 0
    assert len(backup_replication.list_rebalance_jobs(dest_target_id="other")) == 0
    assert len(backup_replication.list_rebalance_jobs(source_target_id="other")) == 0
    assert len(backup_replication.list_rebalance_jobs(phase="complete")) == 0

    # Create idempotent rebalance job returns existing open job
    job2 = backup_replication.create_rebalance_job(
        policy_id="pol-1",
        backup_id="bk-1",
        dest_target_id=t_b["targetId"],
        source_target_id=t_a["targetId"],
    )
    assert job2["jobId"] == job["jobId"]


def test_rebalance_execution_error_handling(tmp_path: Path) -> None:
    t_a = _init_local_target("target_a", tmp_path)
    t_b = _init_local_target("target_b", tmp_path)

    job = backup_replication.create_rebalance_job(
        policy_id="pol-fail",
        backup_id="bk-fail",
        dest_target_id=t_b["targetId"],
        source_target_id=t_a["targetId"],
    )

    with patch("deepseek_infra.infra.workspace.backup_replication.execute_replica_repair", side_effect=RuntimeError("simulated transfer fail")):
        res = backup_replication.execute_rebalance_job(job["jobId"])
        assert res["status"] == "failed"
        assert "simulated transfer fail" in res["error"]

    # Test non-existent job execution raises AppError
    with pytest.raises(AppError):
        backup_replication.execute_rebalance_job("non-existent-job-id")


def test_process_pending_rebalances_and_policy_rebalance(tmp_path: Path) -> None:
    t_a = _init_local_target("target_a", tmp_path, failure_domain="fd-a")
    t_b = _init_local_target("target_b", tmp_path, failure_domain="fd-b")

    backup_policies.create_policy({
        "policyId": "pol-reb",
        "name": "Rebalance Policy",
        "primaryTargetId": t_a["targetId"],
        "replication": {
            "enabled": True,
            "minFailureDomains": 2,
            "targets": [{"targetId": t_b["targetId"], "mode": "required"}],
        },
    })

    # Disabled replication policy rebalance returns skipped
    backup_policies.create_policy({
        "policyId": "pol-dis",
        "name": "Disabled Policy",
        "primaryTargetId": t_a["targetId"],
        "replication": {"enabled": False},
    })
    assert backup_replication.rebalance_policy_replicas("pol-dis")["status"] == "skipped"

    with pytest.raises(AppError):
        backup_replication.rebalance_policy_replicas("non-existent-pol")

    # Pending rebalances drain
    res = backup_replication.process_pending_rebalances()
    assert "processed" in res


def test_authenticate_committed_copy_all_branches(tmp_path: Path) -> None:
    t_auth = _init_local_target("target_auth", tmp_path)
    target = backup_publish.resolve_target(t_auth["targetId"])
    assert target.root is not None

    # 1. Missing receipt and missing commit
    status, r, c = backup_replication.authenticate_committed_copy(target, "pol-1", "bk-missing")
    assert status == "missing"
    assert r is None
    assert c is None

    # Write a receipt
    receipts_dir = target.root / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt_data = {
        "schemaVersion": 4,
        "backupId": "bk-auth",
        "policyId": "pol-1",
        "targetId": t_auth["targetId"],
        "objectSetDigest": "osd_auth_123",
        "createdAt": "2026-08-17T00:00:00Z",
    }
    receipt_bytes = (json.dumps(receipt_data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (receipts_dir / "bk-auth.json").write_bytes(receipt_bytes)

    # 2. Missing commit when receipt exists
    status, r, c = backup_replication.authenticate_committed_copy(target, "pol-1", "bk-auth")
    assert status == "corrupt"

    # Write commit with wrong receiptDigest
    commits_dir = target.root / "commits" / "pol-1"
    commits_dir.mkdir(parents=True, exist_ok=True)
    bad_commit = {
        "schemaVersion": 4,
        "backupId": "bk-auth",
        "policyId": "pol-1",
        "targetGeneration": 1,
        "previousCommitHash": "genesis",
        "receiptDigest": "bad-digest",
        "objectSetDigest": "osd_auth_123",
        "commitHash": "dummy",
    }
    (commits_dir / "bk-auth.json").write_bytes((json.dumps(bad_commit) + "\n").encode("utf-8"))

    # 3. Corrupt commit receiptDigest mismatch
    status, r, c = backup_replication.authenticate_committed_copy(target, "pol-1", "bk-auth")
    assert status == "corrupt"

    # Write valid commit with proper commitHash
    actual_receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
    good_commit = {
        "schemaVersion": 4,
        "backupId": "bk-auth",
        "policyId": "pol-1",
        "targetGeneration": 1,
        "previousCommitHash": "genesis",
        "receiptDigest": actual_receipt_digest,
        "objectSetDigest": "osd_auth_123",
    }
    good_commit["commitHash"] = backup_publish._commit_hash(good_commit)
    (commits_dir / "bk-auth.json").write_bytes((json.dumps(good_commit) + "\n").encode("utf-8"))

    # 4. Authenticated
    status, r, c = backup_replication.authenticate_committed_copy(target, "pol-1", "bk-auth")
    assert status == "authenticated"
    assert r is not None
    assert c is not None


def test_quarantine_corrupt_remote_object(tmp_path: Path) -> None:
    t_src = _init_local_target("target_src", tmp_path)
    t_dst = _init_local_target("target_dst", tmp_path)

    src_target = backup_publish.resolve_target(t_src["targetId"])
    dst_target = backup_publish.resolve_target(t_dst["targetId"])

    assert src_target.root is not None
    assert dst_target.root is not None

    # Setup source object
    obj_data = b"correct-ciphertext-content"
    digest = hashlib.sha256(obj_data).hexdigest()

    src_obj_path = src_target.root / "objects" / digest[:2] / digest[2:4] / f"{digest}.age"
    src_obj_path.parent.mkdir(parents=True, exist_ok=True)
    src_obj_path.write_bytes(obj_data)

    # Setup corrupt dest object
    dst_obj_path = dst_target.root / "objects" / digest[:2] / digest[2:4] / f"{digest}.age"
    dst_obj_path.parent.mkdir(parents=True, exist_ok=True)
    dst_obj_path.write_bytes(b"corrupted-content")

    # Quarantine and replace
    trans = backup_replication.quarantine_and_replace_corrupt_remote_object(
        dst_target,
        f"objects/{digest[:2]}/{digest[2:4]}/{digest}.age",
        digest,
        src_target,
        f"objects/{digest[:2]}/{digest[2:4]}/{digest}.age",
    )
    assert trans == len(obj_data)
    assert dst_obj_path.read_bytes() == obj_data


def test_calculate_replica_lag_and_compliance(tmp_path: Path) -> None:
    t_prim = _init_local_target("target_prim", tmp_path)
    t_repl = _init_local_target("target_repl", tmp_path)

    # No primary point
    lag = backup_replication.calculate_replica_lag("pol-lag", t_repl["targetId"], primary_target_id=t_prim["targetId"])
    assert lag["status"] == "no-primary"

    # Record primary point
    now = datetime.now(tz=timezone.utc)
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=t_prim["targetId"],
        policy_id="pol-lag",
        backup_id="bk-lag-1",
        committed_at=now.isoformat(),
        state="healthy",
        recoverable=True,
    )

    # No replica point
    lag2 = backup_replication.calculate_replica_lag("pol-lag", t_repl["targetId"], primary_target_id=t_prim["targetId"])
    assert lag2["status"] == "no-replica"

    # Record replica point 10s earlier
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=t_repl["targetId"],
        policy_id="pol-lag",
        backup_id="bk-lag-1",
        committed_at=(now - timedelta(seconds=10)).isoformat(),
        state="healthy",
        recoverable=True,
    )

    lag3 = backup_replication.calculate_replica_lag("pol-lag", t_repl["targetId"], primary_target_id=t_prim["targetId"])
    assert lag3["status"] == "calculated"
    assert lag3["lagSeconds"] == 10

    # Test replication compliance
    policy_dict = {
        "policyId": "pol-lag",
        "name": "Lag Policy",
        "targetId": t_prim["targetId"],
        "replication": {
            "enabled": True,
            "minCommittedCopies": 2,
            "maxReplicaLagSeconds": 5,
            "targets": [{"targetId": t_repl["targetId"], "mode": "required"}],
        },
    }
    comp = backup_replication.replication_compliance(policy=policy_dict, backup_id="bk-lag-1")
    assert comp["compliance"] == "degraded"
    assert any("replica-lag-exceeded" in r for r in comp["reasons"])


def test_backup_targets_drain_lifecycle(tmp_path: Path) -> None:
    t_drain = _init_local_target("target_drain", tmp_path)
    tid = t_drain["targetId"]

    assert backup_targets.get_target_drain_state(tid) == "active"

    drained = backup_targets.drain_target(tid, reason="maintenance")
    assert drained["drainState"] == "draining"
    assert backup_targets.get_target_drain_state(tid) == "draining"

    # Draining already draining target is idempotent
    drained2 = backup_targets.drain_target(tid)
    assert drained2["drainState"] == "draining"

    # Mark drained
    mark_d = backup_targets.mark_target_drained(tid)
    assert mark_d["drainState"] == "drained"
    assert backup_targets.get_target_drain_state(tid) == "drained"

    # Reactivate target
    activated = backup_targets.activate_target(tid)
    assert activated["drainState"] == "active"
    assert backup_targets.get_target_drain_state(tid) == "active"

    # Non-existent target raises
    with pytest.raises(AppError):
        backup_targets.drain_target("target_nonexistent")
    with pytest.raises(AppError):
        backup_targets.activate_target("target_nonexistent")


def test_backup_policies_failure_domain_validation_and_cas(tmp_path: Path) -> None:
    t_prim = _init_local_target("target_prim", tmp_path)
    policy = backup_policies.create_policy({
        "policyId": "pol-fd-val",
        "name": "FD Validation Policy",
        "primaryTargetId": t_prim["targetId"],
        "replication": {
            "enabled": True,
            "minFailureDomains": 2,
            "maxCopiesPerFailureDomain": 2,
        },
    })
    assert policy["replication"]["minFailureDomains"] == 2
    assert policy["replication"]["maxCopiesPerFailureDomain"] == 2

    # Validation errors on invalid failure domain numbers
    with pytest.raises(AppError):
        backup_policies.create_policy({
            "policyId": "pol-bad-fd",
            "name": "Bad FD",
            "replication": {"enabled": True, "minFailureDomains": 0},
        })

    with pytest.raises(AppError):
        backup_policies.create_policy({
            "policyId": "pol-bad-max-fd",
            "name": "Bad Max FD",
            "replication": {"enabled": True, "maxCopiesPerFailureDomain": 0},
        })

    # Update with expected revision CAS match
    updated = backup_policies.update_policy(
        "pol-fd-val",
        {"name": "Updated FD Name"},
        expected_revision=policy["policyRevision"],
    )
    assert updated["name"] == "Updated FD Name"

    # Update with mismatched expected revision raises CAS error
    with pytest.raises(AppError):
        backup_policies.update_policy(
            "pol-fd-val",
            {"name": "Mismatch Name"},
            expected_revision=999,
        )


def test_backup_write_continuity_promotion_and_failback_guards(tmp_path: Path) -> None:
    t_p = _init_local_target("target_p", tmp_path, failure_domain="fd-1")
    t_r = _init_local_target("target_r", tmp_path, failure_domain="fd-2")

    backup_policies.create_policy({
        "policyId": "pol-gov",
        "name": "Gov Policy",
        "primaryTargetId": t_p["targetId"],
        "replication": {
            "enabled": True,
            "targets": [{"targetId": t_r["targetId"], "mode": "required"}],
        },
    })

    # Promotion rejected when candidate is not a replica
    with pytest.raises(AppError):
        backup_write_continuity.promote_primary_target(
            "pol-gov",
            "target_unknown",
        )

    # CAS mismatch on epoch raises AppError
    with pytest.raises(AppError):
        backup_write_continuity.promote_primary_target(
            "pol-gov",
            t_r["targetId"],
            expected_failover_epoch=999,
        )

    # Failback evaluation when primary lacks recovery points
    eligible, reason, details = backup_write_continuity.evaluate_failback_eligibility("pol-gov")
    assert eligible is False

    # Idempotent failover transition does not increment epoch unnecessarily
    res1 = backup_write_continuity.execute_failover_transition(
        "pol-gov",
        t_r["targetId"],
        reason="offline",
    )
    res2 = backup_write_continuity.execute_failover_transition(
        "pol-gov",
        t_r["targetId"],
        reason="offline",
    )
    assert res1["failoverEpoch"] == res2["failoverEpoch"]


def test_backup_scheduler_write_placement_ranking(tmp_path: Path) -> None:
    t_1 = _init_local_target("target_1", tmp_path, failure_domain="fd-a", priority=200)
    t_2 = _init_local_target("target_2", tmp_path, failure_domain="fd-b", priority=100)
    t_drain = _init_local_target("target_drain", tmp_path, failure_domain="fd-c", priority=300)
    backup_targets.drain_target(t_drain["targetId"])

    policy = {
        "policyId": "pol-sched",
        "name": "Sched Policy",
        "primaryTargetId": t_1["targetId"],
        "replication": {
            "enabled": True,
            "minFailureDomains": 2,
            "targets": [
                {"targetId": t_2["targetId"], "mode": "required"},
                {"targetId": t_drain["targetId"], "mode": "required"},
            ],
        },
    }

    placement = backup_scheduler.evaluate_write_placement(policy)
    # Draining target is filtered out
    assert t_drain["targetId"] not in placement.get("candidateTargetIds", [])
    assert placement["selectedWriteTargetId"] == t_1["targetId"]


def test_backup_governance_web_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("deepseek_infra.web.routes.backup_governance.require_api_auth", lambda r: None)
    router = backup_governance.create_backup_governance_router()

    t_web = _init_local_target("target_web_drain", tmp_path)
    tid = t_web["targetId"]

    # 1. Drain route
    drain_ep = _endpoint(router, "/api/workspace/backup-targets/{target_id}/drain", "POST")
    res = _call(drain_ep, {"reason": "test-drain"}, target_id=tid)
    data = _json(res)
    assert data["drainState"] == "draining"

    # 2. Activate route
    activate_ep = _endpoint(router, "/api/workspace/backup-targets/{target_id}/activate", "POST")
    res2 = _call(activate_ep, target_id=tid)
    data2 = _json(res2)
    assert data2["drainState"] == "active"

    # 3. List rebalances route
    list_reb_ep = _endpoint(router, "/api/workspace/backup-rebalances", "GET")
    res3 = _call(list_reb_ep, method="GET")
    data3 = _json(res3)
    assert "rebalances" in data3

    # 4. Trigger rebalance route (missing policyId raises AppError)
    trig_reb_ep = _endpoint(router, "/api/workspace/backup-rebalances", "POST")
    with pytest.raises(AppError):
        _call(trig_reb_ep, {"policyId": ""})

    # Trigger rebalance route with valid policy
    t_prim = _init_local_target("target_web_prim", tmp_path)
    backup_policies.create_policy({
        "policyId": "pol-web-reb",
        "name": "Web Rebalance Policy",
        "primaryTargetId": t_prim["targetId"],
        "replication": {"enabled": False},
    })
    res4 = _call(trig_reb_ep, {"policyId": "pol-web-reb"})
    data4 = _json(res4)
    assert data4["status"] == "skipped"


def test_rebalance_policy_replicas_with_draining_and_diversity(tmp_path: Path) -> None:
    t_a = _init_local_target("target_drain_src", tmp_path, failure_domain="fd-1")
    t_b = _init_local_target("target_dest_cand", tmp_path, failure_domain="fd-2")
    t_c = _init_local_target("target_dest_extra", tmp_path, failure_domain="fd-3")
    backup_targets.drain_target(t_a["targetId"])

    backup_policies.create_policy({
        "policyId": "pol-reb-drain",
        "name": "Rebalance Drain Policy",
        "primaryTargetId": t_a["targetId"],
        "replication": {
            "enabled": True,
            "minFailureDomains": 2,
            "targets": [
                {"targetId": t_b["targetId"], "mode": "required"},
                {"targetId": t_c["targetId"], "mode": "required"},
            ],
        },
    })

    # Record a healthy logical recovery point on target A
    now = datetime.now(tz=timezone.utc).isoformat()
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=t_a["targetId"],
        policy_id="pol-reb-drain",
        backup_id="bk-drain-1",
        committed_at=now,
        state="healthy",
        recoverable=True,
    )

    with patch("deepseek_infra.infra.workspace.backup_replication.execute_replica_repair", return_value={"status": "success"}):
        res = backup_replication.rebalance_policy_replicas("pol-reb-drain")
        assert res["status"] == "completed"
        assert res["jobsCreated"] >= 1


def test_reconcile_replication_stream_with_pagination(tmp_path: Path) -> None:
    t_src = _init_local_target("target_stream_src", tmp_path, failure_domain="fd-1")
    t_dst = _init_local_target("target_stream_dst", tmp_path, failure_domain="fd-2")

    backup_policies.create_policy({
        "policyId": "pol-stream-reconcile",
        "name": "Stream Reconcile Policy",
        "primaryTargetId": t_src["targetId"],
        "replication": {
            "enabled": True,
            "targets": [{"targetId": t_dst["targetId"], "mode": "required"}],
        },
    })

    now = datetime.now(tz=timezone.utc)
    for i in range(3):
        backup_dr_ledger.record_logical_recovery_copy(
            target_id=t_src["targetId"],
            policy_id="pol-stream-reconcile",
            backup_id=f"bk-stream-{i}",
            committed_at=(now + timedelta(seconds=i * 10)).isoformat(),
            state="healthy",
            recoverable=True,
        )

    with patch("deepseek_infra.infra.workspace.backup_replication.execute_replica_repair", return_value={"status": "success"}):
        res1 = backup_replication.reconcile_policy_replicas("pol-stream-reconcile", max_points=2)
        assert res1["scannedPoints"] == 2
        assert res1["repairsTriggered"] == 2

        # Second pass using keyset cursor
        res2 = backup_replication.reconcile_policy_replicas("pol-stream-reconcile", max_points=2)
        assert res2["scannedPoints"] >= 1


def test_backup_reconcile_catalog_and_orphan_records(tmp_path: Path) -> None:
    t = _init_local_target("target_rec_cat", tmp_path)
    root = Path(t["path"])

    # Empty target should assert catalog committed
    backup_reconcile.assert_catalog_committed(root)
    assert backup_reconcile.catalog_corrupt_backup_ids(root) == []

    # Append catalog record without commit marker
    fake_receipt = {
        "schemaVersion": 4,
        "backupId": "bk-corrupt-cat",
        "policyId": "pol-test",
        "filename": "fake.age",
    }
    backup_catalog.append_receipt(root, fake_receipt)

    assert "bk-corrupt-cat" in backup_reconcile.catalog_corrupt_backup_ids(root)
    with pytest.raises(AppError) as exc_info:
        backup_reconcile.assert_catalog_committed(root)
    assert "catalog-corrupt" in str(exc_info.value)


def test_backup_replication_verify_destination_component_branches(tmp_path: Path) -> None:
    t = _init_local_target("target_ver_comp", tmp_path)
    target = backup_publish.resolve_target(t["targetId"])
    assert target.root is not None

    # Nonexistent component
    valid, corrupt = backup_replication._verify_destination_component(target, "objects/nonexistent.age", "abcd" * 16)
    assert not valid and not corrupt

    # Valid component
    data = b"component-payload-bytes"
    digest = hashlib.sha256(data).hexdigest()
    comp_path = target.root / "objects" / "test.age"
    comp_path.parent.mkdir(parents=True, exist_ok=True)
    comp_path.write_bytes(data)

    valid2, corrupt2 = backup_replication._verify_destination_component(target, "objects/test.age", digest)
    assert valid2 is True and corrupt2 is False

    # Corrupt component
    valid3, corrupt3 = backup_replication._verify_destination_component(target, "objects/test.age", "wrongdigest" * 5)
    assert valid3 is False and corrupt3 is True


def test_backup_dr_readiness_lag_and_drained_evaluation(tmp_path: Path) -> None:
    t_p = _init_local_target("target_dr_prim", tmp_path, failure_domain="fd-1")
    t_r = _init_local_target("target_dr_repl", tmp_path, failure_domain="fd-2")
    backup_targets.drain_target(t_r["targetId"])

    backup_policies.create_policy({
        "policyId": "pol-dr-ready",
        "name": "DR Readiness Policy",
        "primaryTargetId": t_p["targetId"],
        "replication": {
            "enabled": True,
            "minFailureDomains": 2,
            "targets": [{"targetId": t_r["targetId"], "mode": "required"}],
        },
    })

    readiness = backup_dr_readiness.evaluate_scope_readiness(t_p["targetId"], "pol-dr-ready")
    assert readiness["targetId"] == t_p["targetId"]
    assert readiness["policyId"] == "pol-dr-ready"


def test_backup_write_continuity_governed_promotion(tmp_path: Path) -> None:
    t_p = _init_local_target("target_promo_p", tmp_path, failure_domain="fd-1")
    t_r = _init_local_target("target_promo_r", tmp_path, failure_domain="fd-2")

    backup_policies.create_policy({
        "policyId": "pol-promo-test",
        "name": "Promo Policy",
        "primaryTargetId": t_p["targetId"],
        "replication": {
            "enabled": True,
            "targets": [{"targetId": t_r["targetId"], "mode": "required"}],
        },
    })

    # Record healthy recovery point on replica so promotion preflight passes
    now = datetime.now(tz=timezone.utc).isoformat()
    backup_dr_ledger.record_recovery_point(
        target_id=t_r["targetId"],
        policy_id="pol-promo-test",
        backup_id="bk-promo-1",
        committed_at=now,
        recoverable=True,
    )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=t_r["targetId"],
        policy_id="pol-promo-test",
        backup_id="bk-promo-1",
        committed_at=now,
        state="healthy",
        recoverable=True,
    )

    promoted = backup_write_continuity.promote_primary_target(
        "pol-promo-test",
        t_r["targetId"],
    )
    assert promoted["status"] == "promoted"
    assert promoted["newPrimaryTargetId"] == t_r["targetId"]


def test_backup_executor_publish_ambiguous_and_missing_parent_guards(tmp_path: Path) -> None:
    t_a = _init_local_target("target_ambig_a", tmp_path, failure_domain="fd-1")
    t_b = _init_local_target("target_ambig_b", tmp_path, failure_domain="fd-2")

    policy = backup_policies.create_policy({
        "policyId": "pol-ambig",
        "name": "Ambig Policy",
        "primaryTargetId": t_a["targetId"],
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "replication": {
            "enabled": True,
            "targets": [{"targetId": t_b["targetId"], "mode": "required"}],
        },
    })

    run = backup_scheduler.claim_manual_run(policy, instance_id="ambig-runner")

    def failing_publish(*args: Any, **kwargs: Any) -> Any:
        raise AppError("Connection lost during commit marker flush")

    # When authenticate_recovery_copy returns unreachable/unknown
    with patch("deepseek_infra.infra.workspace.backup_publish.publish_backup", side_effect=failing_publish), \
         patch("deepseek_infra.infra.workspace.backup_replication.authenticate_recovery_copy", side_effect=RuntimeError("network unreachable")):
        outcome = backup_executor.execute_run(run, instance_id="ambig-runner")
        assert outcome["phase"] == "failed"


def test_repair_jobs_crud_and_filters(tmp_path: Path) -> None:
    job = backup_replication.create_repair_job(
        policy_id="pol-rep-crud",
        backup_id="bk-rep-1",
        dest_target_id="target_dest_rep",
        source_target_id="target_src_rep",
    )
    assert job["repairId"]
    assert job["phase"] == "queued"

    read = backup_replication.read_repair_job(job["repairId"])
    assert read is not None
    assert read["repairId"] == job["repairId"]

    # Nonexistent repair job
    assert backup_replication.read_repair_job("nonexistent") is None

    # Filtered list
    listed = backup_replication.list_repair_jobs(policy_id="pol-rep-crud", backup_id="bk-rep-1")
    assert len(listed) >= 1
    assert listed[0]["repairId"] == job["repairId"]

    assert len(backup_replication.list_repair_jobs(policy_id="other")) == 0
    assert len(backup_replication.list_repair_jobs(backup_id="other")) == 0
    assert len(backup_replication.list_repair_jobs(dest_target_id="other")) == 0
    assert len(backup_replication.list_repair_jobs(source_target_id="other")) == 0
    assert len(backup_replication.list_repair_jobs(phase="complete")) == 0

    updated = backup_replication._set_repair_phase(job, "running", bytesRepaired=1024)
    assert updated["phase"] == "running"
    assert updated["bytesRepaired"] == 1024


def test_execute_rebalance_job_with_prune_source_and_process_pending(tmp_path: Path) -> None:
    t_src = _init_local_target("target_reb_src", tmp_path, failure_domain="fd-1")
    t_dst = _init_local_target("target_reb_dst", tmp_path, failure_domain="fd-2")

    backup_policies.create_policy({
        "policyId": "pol-reb-prune",
        "name": "Rebalance Prune Policy",
        "primaryTargetId": t_src["targetId"],
        "replication": {
            "enabled": True,
            "minCommittedCopies": 1,
            "minFailureDomains": 1,
            "targets": [{"targetId": t_dst["targetId"], "mode": "required"}],
        },
    })

    now = datetime.now(tz=timezone.utc).isoformat()
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=t_src["targetId"],
        policy_id="pol-reb-prune",
        backup_id="bk-reb-prune-1",
        committed_at=now,
        state="healthy",
        recoverable=True,
    )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=t_dst["targetId"],
        policy_id="pol-reb-prune",
        backup_id="bk-reb-prune-1",
        committed_at=now,
        state="healthy",
        recoverable=True,
    )

    job = backup_replication.create_rebalance_job(
        policy_id="pol-reb-prune",
        backup_id="bk-reb-prune-1",
        dest_target_id=t_dst["targetId"],
        source_target_id=t_src["targetId"],
        prune_source_after=True,
    )

    with patch("deepseek_infra.infra.workspace.backup_replication.execute_replica_repair", return_value={"status": "success", "bytesRepaired": 1234}), \
         patch("deepseek_infra.infra.workspace.backup_replication.authenticate_committed_copy", return_value=("authenticated", {"backupId": "bk-reb-prune-1"}, {"commitHash": "abc"})):
        res = backup_replication.execute_rebalance_job(job["jobId"])
        assert res["status"] == "success"
        assert res["job"]["phase"] == "complete"

        # Draining queue with process_pending_rebalances
        backup_replication.create_rebalance_job(
            policy_id="pol-reb-prune",
            backup_id="bk-reb-prune-2",
            dest_target_id=t_dst["targetId"],
            source_target_id=t_src["targetId"],
        )
        proc = backup_replication.process_pending_rebalances()
        assert proc["processed"] >= 1
        assert proc["succeeded"] >= 1


def test_authenticate_committed_copy_expected_params_branches(tmp_path: Path) -> None:
    t_auth = _init_local_target("target_auth_ext", tmp_path)
    target = backup_publish.resolve_target(t_auth["targetId"])
    assert target.root is not None

    receipts_dir = target.root / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt_data = {
        "schemaVersion": 4,
        "backupId": "bk-auth-ext",
        "policyId": "pol-auth-ext",
        "targetId": t_auth["targetId"],
        "objectSetDigest": "osd_ext_123",
        "createdAt": "2026-08-17T00:00:00Z",
    }
    receipt_bytes = (json.dumps(receipt_data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (receipts_dir / "bk-auth-ext.json").write_bytes(receipt_bytes)

    commits_dir = target.root / "commits" / "pol-auth-ext"
    commits_dir.mkdir(parents=True, exist_ok=True)
    actual_receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
    good_commit = {
        "schemaVersion": 4,
        "backupId": "bk-auth-ext",
        "policyId": "pol-auth-ext",
        "targetGeneration": 2,
        "previousCommitHash": "prev_hash_123",
        "receiptDigest": actual_receipt_digest,
        "objectSetDigest": "osd_ext_123",
    }
    good_commit["commitHash"] = backup_publish._commit_hash(good_commit)
    (commits_dir / "bk-auth-ext.json").write_bytes((json.dumps(good_commit) + "\n").encode("utf-8"))

    # Conflicting previous commit hash
    status, _, _ = backup_replication.authenticate_committed_copy(
        target,
        "pol-auth-ext",
        "bk-auth-ext",
        expected_previous_commit_hash="mismatched_prev_hash",
    )
    assert status == "conflicting"

    # Conflicting target generation
    status2, _, _ = backup_replication.authenticate_committed_copy(
        target,
        "pol-auth-ext",
        "bk-auth-ext",
        expected_target_generation=999,
    )
    assert status2 == "conflicting"

    # Conflicting object set digest
    status3, _, _ = backup_replication.authenticate_committed_copy(
        target,
        "pol-auth-ext",
        "bk-auth-ext",
        expected_object_set_digest="wrong_osd",
    )
    assert status3 == "conflicting"


def test_backup_policies_validation_and_crud_edge_cases(tmp_path: Path) -> None:
    t = _init_local_target("target_pol_crud", tmp_path)

    # 1. Create and get policy
    backup_policies.create_policy({
        "policyId": "pol-crud-1",
        "name": "CRUD Policy 1",
        "primaryTargetId": t["targetId"],
    })
    read_p = backup_policies.get_policy("pol-crud-1")
    assert read_p["policyId"] == "pol-crud-1"

    # 2. List policies
    listed = backup_policies.list_policies()
    assert any(x["policyId"] == "pol-crud-1" for x in listed)

    # 3. Delete policy
    del_res = backup_policies.delete_policy("pol-crud-1")
    assert del_res["policyId"] == "pol-crud-1"

    with pytest.raises(AppError):
        backup_policies.get_policy("pol-crud-1")

    # 4. Target binding validations: target not found
    with pytest.raises(AppError):
        backup_policies.create_policy({
            "name": "Missing Target",
            "primaryTargetId": "target_does_not_exist",
        })

    # Target binding validations: replica target not found
    with pytest.raises(AppError):
        backup_policies.create_policy({
            "name": "Missing Replica Target",
            "primaryTargetId": t["targetId"],
            "replication": {
                "enabled": True,
                "targets": [{"targetId": "target_replica_missing", "mode": "required"}],
            },
        })

    # Secret markers rejection
    with pytest.raises(AppError):
        backup_policies.create_policy({
            "name": "Secret Policy",
            "primaryTargetId": t["targetId"],
            "description": "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASC",
        })

    # Invalid payload types
    with pytest.raises(AppError):
        backup_policies.create_policy("not-a-dict")  # type: ignore[arg-type]
    with pytest.raises(AppError):
        backup_policies.create_policy({"name": ""})
    with pytest.raises(AppError):
        backup_policies.create_policy({"name": "A" * 200})
    with pytest.raises(AppError):
        backup_policies.update_policy("nonexistent", "not-a-dict")  # type: ignore[arg-type]


def test_backup_replication_pending_repairs_and_retired_recovery_points(tmp_path: Path) -> None:
    # 1. Backoff calculation
    assert backup_replication._compute_repair_backoff_seconds(1) == 5
    assert backup_replication._compute_repair_backoff_seconds(2) == 15
    assert backup_replication._compute_repair_backoff_seconds(5) == 300
    assert backup_replication._compute_repair_backoff_seconds(10) == 300

    # 2. Retired recovery point skipped
    with patch("deepseek_infra.infra.workspace.backup_dr_ledger.is_logical_recovery_point_retired", return_value=True):
        res = backup_replication.execute_replica_repair(policy_id="pol-retired", backup_id="bk-retired", dest_target_id="target_dest")
        assert res["status"] == "skipped"
        assert res["reason"] == "retired"

    # 3. Process pending repairs with max attempts exceeded
    job = backup_replication.create_repair_job(
        policy_id="pol-pending-rep",
        backup_id="bk-pending-1",
        dest_target_id="target_dest",
        repair_id="repair_exceeded",
    )
    backup_replication._set_repair_phase(job, "retry-wait", attempt=5, maxAttempts=5)
    drained = backup_replication.process_pending_repairs()
    assert drained["processed"] == 0
    read_job = backup_replication.read_repair_job("repair_exceeded")
    assert read_job is not None
    assert read_job["phase"] == "failed-terminal"


def test_backup_targets_open_store_and_remote_head_branches(tmp_path: Path) -> None:
    # 1. open_target_store for managed-local
    local_store = backup_targets.open_target_store("managed-local")
    assert local_store is not None

    # 2. open_target_store for filesystem target
    t = _init_local_target("target_open_fs", tmp_path)
    fs_store = backup_targets.open_target_store(t["targetId"], write_intent=False)
    assert fs_store is not None

    # 3. record_remote_target_head
    backup_targets.record_remote_target_head(
        fs_store,
        target_id=t["targetId"],
        generation=1,
        commit_hash="c_hash_123",
    )


def test_backup_reconcile_branches(tmp_path: Path) -> None:
    from deepseek_infra.infra.workspace import backup_object_set

    _init_local_target("target_rec_branch", tmp_path)
    target_root = tmp_path / "target_rec_branch"

    # 1. Empty catalog assertion passes
    backup_reconcile.assert_catalog_committed(target_root)

    # 2. _committed_receipt_objects with mismatched receipt digest
    marker = {
        "schemaVersion": 4,
        "receiptDigest": "digest_a",
        "objectSetDigest": "osd_1",
        "controlObjectDigest": "cod_1",
    }
    receipt = {
        "schemaVersion": 4,
        "storageProtocol": backup_object_set.OBJECT_SET_V1,
        "objectSetDigest": "osd_1",
        "controlObjectDigest": "cod_1",
        "components": {},
    }
    digests = backup_reconcile._committed_receipt_objects(marker, receipt, receipt_digest="digest_b")
    assert len(digests) == 0

    # Non-OBJECT_SET_V1 storage protocol
    marker_legacy = {"objectDigest": "obj_digest_123"}
    receipt_legacy = {"objectDigest": "obj_digest_123"}
    digs = backup_reconcile._committed_receipt_objects(marker_legacy, receipt_legacy)
    assert isinstance(digs, set)


def test_backup_dr_readiness_utility_functions(tmp_path: Path) -> None:
    # 1. _validated_commit_chain empty
    chain, ok = backup_dr_readiness._validated_commit_chain([])
    assert ok is True and chain == []

    # 2. _validated_commit_chain valid vs broken
    m1 = {"schemaVersion": 4, "backupId": "b1", "policyId": "p1", "commitHash": "hash1", "parentCommitHash": None, "objectSetDigest": "osd1"}
    m1["commitHash"] = backup_publish._commit_hash(m1)
    m2 = {"schemaVersion": 4, "backupId": "b2", "policyId": "p1", "parentCommitHash": m1["commitHash"], "objectSetDigest": "osd2"}
    m2["commitHash"] = backup_publish._commit_hash(m2)

    chain2, ok2 = backup_dr_readiness._validated_commit_chain([m1, m2])
    assert ok2 is True
    assert len(chain2) == 2

    # Broken chain
    chain3, ok3 = backup_dr_readiness._validated_commit_chain([m1, {"invalid": True}])
    assert ok3 is False

    # 3. _merge_validated_receipt
    merged = backup_dr_readiness._merge_validated_receipt(
        {"backupId": "bk-m1"},
        {"pinned": True, "scrubOk": True, "ciphertextScrubbedAt": "2026-08-17T00:00:00Z"},
        target_id="target_merged",
    )
    assert merged["pinned"] is True
    assert merged["scrubOk"] is True
    assert merged["targetId"] == "target_merged"

    # 4. _stage_samples and _drill_records
    samples = backup_dr_readiness._stage_samples()
    assert isinstance(samples, list)
    drills = backup_dr_readiness._drill_records(tmp_path)
    assert isinstance(drills, list)


def test_backup_executor_error_handling_and_retry_branches(tmp_path: Path) -> None:
    t = _init_local_target("target_exec_err", tmp_path)
    policy = backup_policies.create_policy({
        "policyId": "pol-exec-err",
        "name": "Exec Err Policy",
        "primaryTargetId": t["targetId"],
    })
    run = backup_scheduler.claim_manual_run(policy, instance_id="err-runner")

    # 1. Lease expired / abandoned outcome (499)
    with patch("deepseek_infra.infra.workspace.backup_publish.publish_backup", side_effect=AppError("writer lease expired", status=499)):
        res = backup_executor.execute_run(run, instance_id="err-runner")
        assert res["phase"] in {"abandoned", "failed"}

    # 2. Slot commit conflict (409)
    run2 = backup_scheduler.claim_manual_run(policy, instance_id="err-runner")
    with patch("deepseek_infra.infra.workspace.backup_publish.publish_backup", side_effect=AppError("slot-commit-conflict detected", status=409)):
        res2 = backup_executor.execute_run(run2, instance_id="err-runner")
        assert res2["phase"] in {"superseded", "failed"}

    # 3. Transient error with retry (503)
    run3 = backup_scheduler.claim_manual_run(policy, instance_id="err-runner")
    with patch("deepseek_infra.infra.workspace.backup_publish.publish_backup", side_effect=AppError("service unavailable", status=503)):
        res3 = backup_executor.execute_run(run3, instance_id="err-runner")
        assert res3["phase"] in {"queued", "failed"}


def test_backup_replication_source_hold_and_local_catalog_branches(tmp_path: Path) -> None:
    t = _init_local_target("target_hold_test", tmp_path)
    target = backup_publish.resolve_target(t["targetId"])
    assert target.root is not None

    # 1. Acquire source hold on filesystem target
    hold = backup_replication.acquire_source_hold(
        target_id=t["targetId"],
        policy_id="pol-hold",
        backup_id="bk-hold-1",
        holder_id="test-repair-worker",
        target_root=target.root,
        hold_seconds=3600,
    )
    assert hold.hold_id
    assert backup_replication.is_source_held(t["targetId"], "pol-hold", "bk-hold-1", target=target) is True
    assert backup_replication.is_source_held(t["targetId"], "pol-hold", "bk-other", target=target) is False

    # 2. Renew hold
    hold.renew(duration_seconds=7200)
    assert hold.generation == 2

    # 3. Release hold
    hold.release()
    assert backup_replication.is_source_held(t["targetId"], "pol-hold", "bk-hold-1", target=target) is False

    # 4. append_target_local_catalog
    receipt = {
        "schemaVersion": 4,
        "backupId": "bk-cat-append-1",
        "policyId": "pol-hold",
        "targetId": t["targetId"],
        "createdAt": "2026-08-17T00:00:00Z",
    }
    backup_replication.append_target_local_catalog(target, receipt)
    cat_file = target.root / "catalogs" / "pol-hold.jsonl"
    assert cat_file.is_file()


def test_backup_replication_jobs_crud_and_enqueue_branches(tmp_path: Path) -> None:
    t1 = _init_local_target("target_q1", tmp_path)
    t2 = _init_local_target("target_q2", tmp_path)

    policy = {
        "policyId": "pol-repl-crud",
        "primaryTargetId": t1["targetId"],
        "replication": {
            "enabled": True,
            "targets": [{"targetId": t2["targetId"], "mode": "required"}],
        },
    }

    # 1. Enqueue replica jobs
    jobs = backup_replication.enqueue_replica_jobs(
        policy=policy,
        primary_target_id=t1["targetId"],
        backup_id="bk-q-1",
        package=None,
        run_id="run-q-1",
        schedule_slot="2026-08-17T02:00:00Z",
        slot_digest="slot_digest_q1",
    )
    assert len(jobs) == 1
    assert jobs[0]["phase"] == "queued"

    # 2. list_jobs
    listed = backup_replication.list_jobs(policy_id="pol-repl-crud", backup_id="bk-q-1")
    assert len(listed) >= 1

    # 3. has_open_required_jobs
    assert backup_replication.has_open_required_jobs(policy_id="pol-repl-crud", backup_id="bk-q-1") is True

    # Mark completed
    backup_replication._set_phase(jobs[0], "committed")
    assert backup_replication.has_open_required_jobs(policy_id="pol-repl-crud", backup_id="bk-q-1") is False


def test_backup_replication_fail_job_and_stream_branches(tmp_path: Path) -> None:
    # 1. _fail_job branches
    job = {"jobId": "job-fail-1", "phase": "running", "attempts": 1, "maxAttempts": 5}
    f1 = backup_replication._fail_job(job, RuntimeError("spool directory is missing"), mode="required")
    assert f1["phase"] == "repair-needed"

    f2 = backup_replication._fail_job(job, RuntimeError("temporary network drop"), mode="required")
    assert f2["phase"] == "retry-wait"

    job_max = {"jobId": "job-fail-2", "phase": "running", "attempts": 5, "maxAttempts": 5}
    f3 = backup_replication._fail_job(job_max, RuntimeError("fatal disk failure"), mode="required")
    assert f3["phase"] == "failed-terminal"

    f4 = backup_replication._fail_job(job, RuntimeError("best effort fail"), mode="best_effort")
    assert f4["phase"] == "failed"

    # 2. _iter_source_stream
    t = _init_local_target("target_stream_t", tmp_path)
    target = backup_publish.resolve_target(t["targetId"])
    # Nonexistent
    chunks_empty = list(backup_replication._iter_source_stream(target, "objects/missing.bin"))
    assert len(chunks_empty) == 0

    # Existing
    assert target.root is not None
    (target.root / "objects").mkdir(parents=True, exist_ok=True)
    test_obj = target.root / "objects" / "chunk_test.bin"
    test_obj.write_bytes(b"hello-world-data")
    chunks = list(backup_replication._iter_source_stream(target, "objects/chunk_test.bin", chunk_size=4))
    assert b"".join(chunks) == b"hello-world-data"

    # 3. execute_repair_job_instance error branches
    with pytest.raises(AppError):
        backup_replication.execute_repair_job_instance("nonexistent_repair_id")

    term_job = backup_replication.create_repair_job(
        policy_id="pol-term-rep",
        backup_id="bk-term",
        dest_target_id=t["targetId"],
        repair_id="repair_terminal_1",
    )
    backup_replication._set_repair_phase(term_job, "healthy")
    res_term = backup_replication.execute_repair_job_instance("repair_terminal_1")
    assert res_term["status"] == "success"

    exceed_job = backup_replication.create_repair_job(
        policy_id="pol-term-rep",
        backup_id="bk-term-exceed",
        dest_target_id=t["targetId"],
        repair_id="repair_exceed_1",
    )
    backup_replication._set_repair_phase(exceed_job, "queued", attempt=6, maxAttempts=5)
    with pytest.raises(AppError):
        backup_replication.execute_repair_job_instance("repair_exceed_1")

    backup_replication.create_repair_job(
        policy_id="pol-no-src",
        backup_id="bk-no-src",
        dest_target_id=t["targetId"],
        repair_id="repair_no_src",
    )
    with pytest.raises(AppError):
        backup_replication.execute_repair_job_instance("repair_no_src")


def test_backup_replication_store_holds_and_repair_shortcuts(tmp_path: Path) -> None:
    # 1. SourceHold with mock store
    class MockStat:
        etag = "etag-1"

    class MockPutRes:
        etag = "etag-2"

    mock_store = MagicMock()
    mock_store.put_if_absent.return_value = MockPutRes()
    mock_store.put_if_match.return_value = MockPutRes()
    mock_store.stat.return_value = MockStat()

    hold = backup_replication.acquire_source_hold(
        target_id="target_store_t",
        policy_id="pol-store",
        backup_id="bk-store-1",
        holder_id="test-holder",
        target_store=mock_store,
    )
    assert hold.etag == "etag-2"

    # Renew with existing etag
    hold.renew(duration_seconds=1800)
    assert mock_store.put_if_match.called

    # Renew without etag (stat path)
    hold.etag = None
    hold.renew(duration_seconds=1800)

    # Renew exception -> RepairLeaseLostError
    mock_store.put_if_match.side_effect = RuntimeError("network down")
    with pytest.raises(backup_replication.RepairLeaseLostError):
        hold.renew()

    # Release with store
    hold.release()
    assert mock_store.delete_if_match.called

    # is_source_held with remote store
    class MockObj:
        key = "holds/repair/hold_1.json"

    class MockPage:
        objects = [MockObj()]

    mock_store_2 = MagicMock()
    mock_store_2.list_objects.return_value = MockPage()
    hold_data = {
        "policyId": "pol-remote-held",
        "backupId": "bk-remote-1",
        "expiresAt": (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    mock_store_2.get_bytes.return_value = json.dumps(hold_data).encode("utf-8")
    remote_target = MagicMock()
    remote_target.root = None
    remote_target.store = mock_store_2

    assert backup_replication.is_source_held("target_remote", "pol-remote-held", "bk-remote-1", target=remote_target) is True
    assert backup_replication.is_source_held("target_remote", "pol-remote-held", "bk-other", target=remote_target) is False


def test_backup_targets_crud_and_probe_branches(tmp_path: Path) -> None:
    t = _init_local_target("target_probe_fs", tmp_path)

    # 1. list_targets
    targets = backup_targets.list_targets()
    assert any(x["targetId"] == t["targetId"] for x in targets)

    # 2. probe_target filesystem
    p_res = backup_targets.probe_target(t["targetId"])
    assert p_res["targetId"] == t["targetId"]
    assert p_res["ready"] is True

    # 3. probe_target nonexistent
    p_non = backup_targets.probe_target("target_nonexistent")
    assert p_non["ready"] is False
    assert p_non["status"] == "blocked-target-unavailable"

    # 4. delete_target
    del_res = backup_targets.delete_target(t["targetId"])
    assert del_res["deleted"] is True


def test_backup_targets_validation_and_drain_error_branches(tmp_path: Path) -> None:
    # 1. Target ID format validations via _register
    with pytest.raises(AppError):
        backup_targets._register(tmp_path / "t_inv1", "INVALID_UPPER", "nonce1", label="test")
    with pytest.raises(AppError):
        backup_targets._register(tmp_path / "t_inv2", "no_prefix", "nonce2", label="test")
    with pytest.raises(AppError):
        backup_targets._register(tmp_path / "t_inv3", "target_" + "a" * 70, "nonce3", label="test")

    # 2. Drain state transitions and error branches
    with pytest.raises(AppError):
        backup_targets.drain_target("target_nonexistent")
    with pytest.raises(AppError):
        backup_targets.activate_target("target_nonexistent")
    with pytest.raises(AppError):
        backup_targets.mark_target_drained("target_nonexistent")

    assert backup_targets.get_target_drain_state("target_nonexistent") == "unknown"

    t = _init_local_target("target_drain_err", tmp_path)
    drained_rec = backup_targets.mark_target_drained(t["targetId"])
    assert drained_rec["drainState"] == "drained"

    active_rec = backup_targets.activate_target(t["targetId"])
    assert active_rec["drainState"] == "active"
    assert backup_targets.get_target_drain_state(t["targetId"]) == "active"


def test_backup_write_continuity_promotion_precondition_errors(tmp_path: Path) -> None:
    t_p = _init_local_target("target_pr_p", tmp_path, failure_domain="fd-1")
    t_r = _init_local_target("target_pr_r", tmp_path, failure_domain="fd-2")
    t_other = _init_local_target("target_pr_other", tmp_path, failure_domain="fd-3")

    backup_policies.create_policy({
        "policyId": "pol-pr-err",
        "name": "Promo Err Policy",
        "primaryTargetId": t_p["targetId"],
        "replication": {
            "enabled": True,
            "targets": [{"targetId": t_r["targetId"], "mode": "required"}],
        },
    })

    # 1. CAS failoverEpoch mismatch
    with pytest.raises(AppError) as exc1:
        backup_write_continuity.promote_primary_target(
            "pol-pr-err",
            t_r["targetId"],
            expected_failover_epoch=999,
        )
    assert exc1.value.status == 412

    # 2. Not a member of configured replicas
    with pytest.raises(AppError) as exc2:
        backup_write_continuity.promote_primary_target(
            "pol-pr-err",
            t_other["targetId"],
        )
    assert exc2.value.status == 400

    # 3. Target is in draining state
    backup_targets.drain_target(t_r["targetId"])
    with pytest.raises(AppError) as exc3:
        backup_write_continuity.promote_primary_target(
            "pol-pr-err",
            t_r["targetId"],
        )
    assert "draining" in str(exc3.value)


def test_backup_retention_remote_trash_and_gc_lifecycle(tmp_path: Path) -> None:
    t = _init_local_target("target_ret_full", tmp_path)
    store = backup_targets.open_target_store(t["targetId"])

    now = datetime.now(tz=timezone.utc)
    old_time = now - timedelta(days=5)

    # 1. Append active backup records
    backup_catalog._append_entry_store(
        store,
        "receipt",
        {
            "backupId": "bk-ret-1",
            "policyId": "pol-ret-full",
            "createdAt": old_time.isoformat(),
            "manifest": {
                "objectSet": {
                    "ciphertext": {"digest": "1" * 64}
                }
            },
        },
    )
    backup_catalog._append_entry_store(
        store,
        "receipt",
        {
            "backupId": "bk-ret-2",
            "policyId": "pol-ret-full",
            "createdAt": now.isoformat(),
            "manifest": {
                "objectSet": {
                    "ciphertext": {"digest": "2" * 64}
                }
            },
        },
    )

    # 2. Put hold objects
    active_hold = {
        "holdId": "hold-act-1",
        "objectDigest": "3" * 64,
        "objects": [{"digest": "4" * 64}],
        "expiresAt": (now + timedelta(hours=2)).isoformat(),
    }
    expired_hold = {
        "holdId": "hold-exp-1",
        "objectDigest": "5" * 64,
        "expiresAt": (now - timedelta(days=10)).isoformat(),
    }
    store.put_if_absent("holds/restore/hold_act.json", json.dumps(active_hold).encode("utf-8"))
    store.put_if_absent("holds/restore/hold_exp.json", json.dumps(expired_hold).encode("utf-8"))

    digests = backup_retention._restore_hold_digests(store, now=now)
    assert "3" * 64 in digests
    assert "4" * 64 in digests
    assert "5" * 64 not in digests

    # 3. Apply remote trash phase (keepLast=1)
    retention_cfg = {
        "keepLast": 1,
        "trashGraceHours": 24,
        "retentionPolicyId": "pol-ret-full",
    }
    res_trash = backup_retention.apply_retention_store(
        retention_cfg,
        store,
        now=now,
    )
    assert "bk-ret-1" in res_trash["trashed"]

    # 4. Apply GC with now (should keep bk-ret-1 because within grace)
    res_gc1 = backup_retention.finalize_retention_store(
        retention_cfg,
        store,
        target_id=t["targetId"],
        now=now,
    )
    assert "bk-ret-1" in res_gc1["kept"]

    # 5. Apply GC after grace period
    future_time = now + timedelta(days=3)
    res_gc2 = backup_retention.finalize_retention_store(
        retention_cfg,
        store,
        target_id=t["targetId"],
        now=future_time,
    )
    assert "bk-ret-1" in res_gc2["deleted"]


def test_backup_reconcile_all_targets_and_store_reconciliation(tmp_path: Path) -> None:
    t_fs = _init_local_target("target_rec_fs", tmp_path)
    store = backup_targets.open_target_store(t_fs["targetId"])

    # 1. Put commit marker with higher generation to test head advance
    commit_marker = {
        "commitHash": "c" * 64,
        "targetGeneration": 5,
        "backupId": "bk-rec-store-1",
        "createdAt": datetime.now(tz=timezone.utc).isoformat(),
    }
    store.put_if_absent("commits/c1.json", json.dumps(commit_marker).encode("utf-8"))

    # 2. Put an orphan transaction beyond grace
    old_tx = {
        "transactionId": "tx-old-1",
        "createdAt": (datetime.now(tz=timezone.utc) - timedelta(days=2)).isoformat(),
        "stagingKey": "staging/tx1.tmp",
    }
    store.put_if_absent("transactions/tx_old.json", json.dumps(old_tx).encode("utf-8"))
    store.put_if_absent("staging/tx1.tmp", b"orphan data")

    # 3. Call reconcile_all_targets
    reports = backup_reconcile.reconcile_all_targets(
        instance_id="test_inst_1",
        now=datetime.now(tz=timezone.utc),
        orphan_grace_seconds=3600,
    )
    assert len(reports) >= 1

    # 4. Call reconcile_target_store directly with dummy writer
    class DummyWriter:
        fencing_token = 1
        def assert_owned(self) -> None:
            pass

    rep_store = backup_reconcile.reconcile_target_store(
        store,
        target_id=t_fs["targetId"],
        writer=DummyWriter(),  # type: ignore[arg-type]
        now=datetime.now(tz=timezone.utc),
    )
    assert rep_store["targetId"] == t_fs["targetId"]


def test_backup_policies_helpers_and_projections(tmp_path: Path) -> None:
    t = _init_local_target("target_pol_hlp", tmp_path)
    pol = backup_policies.create_policy({
        "policyId": "pol-helpers-1",
        "name": "Helper Policy",
        "primaryTargetId": t["targetId"],
        "enabled": True,
        "encryption": {
            "recipients": [backup_policies.DEFAULT_TEST_RECIPIENT],
        },
    })

    # active_recipients
    recips = backup_policies.active_recipients()
    assert backup_policies.DEFAULT_TEST_RECIPIENT in recips

    # enabled_policies
    enb = backup_policies.enabled_policies()
    assert any(p["policyId"] == "pol-helpers-1" for p in enb)

    # restore_projection
    proj = backup_policies.restore_projection(pol)
    assert proj["enabled"] is False
    assert proj["targetId"] == backup_policies.UNBOUND_TARGET

    # delete_policy
    res_del = backup_policies.delete_policy("pol-helpers-1")
    assert res_del["deleted"] is True


@pytest.mark.anyio
async def test_workspace_backup_finalize_disconnect(tmp_path: Path) -> None:
    from deepseek_infra.web.routes import workspace

    deps = workspace.WorkspaceRouteDeps(read_multipart_files=MagicMock())
    router = workspace.create_workspace_router(deps)

    # Find the finalize route
    finalize_route = None
    for route in router.routes:
        if isinstance(route, APIRoute) and route.path == "/api/workspace/backups/{backup_id}/finalize":
            finalize_route = route
            break
    assert finalize_route is not None

    mock_req = MagicMock(spec=Request)
    mock_req.headers = {"Authorization": "Bearer test-auth"}
    mock_req.is_disconnected = AsyncMock(return_value=True)

    with patch("deepseek_infra.web.routes.workspace.require_api_auth"):
        with patch("deepseek_infra.web.routes.workspace.workspace_backups.finalize_session", side_effect=lambda *a, **kw: time.sleep(0.5)):
            with pytest.raises(AppError) as exc:
                await finalize_route.endpoint(mock_req, backup_id="bk-disc-1")
            assert exc.value.status == 499


def test_server_share_target_and_multipart_handling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.testclient import TestClient
    from deepseek_infra.web import server as server_module

    srv, _ = server_module.create_server(0, host="127.0.0.1")
    client = TestClient(srv.app, base_url="http://127.0.0.1")

    # 1. Share target with error fallback
    with patch("deepseek_infra.web.server.extract_uploaded_file", side_effect=AppError("mocked extract failure", status=400)):
        resp = client.post(
            "/share-target",
            data={},
            files={"file": ("invalid.txt", b"corrupted content", "text/plain")},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "share=" in resp.headers.get("location", "")

    # 2. read_multipart_files with ocrEnabled and apiKey
    mock_req = MagicMock(spec=Request)
    mock_req.headers = {"Content-Type": "multipart/form-data; boundary=xyz", "Content-Length": "100"}

    async def _mock_read_form(req: Request) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
        return {"ocrEnabled": ["true"], "apiKey": ["test-ocr-key"]}, [{"filename": "doc.pdf", "data": b"123", "content_type": "application/pdf"}]

    with patch("deepseek_infra.web.server.read_multipart_form", _mock_read_form):
        uploads, ocr_en, api_key = asyncio.run(server_module.read_multipart_files(mock_req))
        assert ocr_en is True
        assert api_key == "test-ocr-key"
        assert len(uploads) == 1


def test_agent_state_edge_cases() -> None:
    from deepseek_infra.infra.agent_runtime import agent_state

    # _usage_int
    assert agent_state._usage_int({"prompt_tokens": "invalid_num"}, "prompt_tokens") == 0
    assert agent_state._usage_int({"prompt_tokens": "-5"}, "prompt_tokens") == 0
    assert agent_state._usage_int({"promptTokens": "42"}, "prompt_tokens", "promptTokens") == 42

    # _plan_dependencies with invalid items
    deps = agent_state._plan_dependencies([None, 123, {"no_id": True}, {"id": "n1", "depends_on": ["n0", ""]}])
    assert deps == {"n1": {"n0"}}

    # incomplete_plan_nodes and completed_node_ids
    plan = [None, "str_item", {"no_id": 1}, {"id": "n1"}, {"id": "n2"}]
    nodes = {"n1": {"state": "succeeded"}, "n2": {"state": "failed"}}
    assert agent_state.incomplete_plan_nodes(plan, nodes) == [{"id": "n2"}]
    assert agent_state.completed_node_ids(plan, nodes) == ["n1"]

    plan_test = [{"id": "n1"}, {"id": "n2", "depends_on": ["n1"]}]
    events = [
        {"type": "agent", "phase": "planning", "status": "running"},
        {"type": "agent_reset", "phase": "planning"},
        {"type": "agent_output", "phase": "n1", "output": {"failed": False, "duration_ms": 100, "usage": {"prompt_tokens": 10}}},
        {"type": "run_status", "status": "cancelled"},
    ]
    replayed = agent_state.reduce_node_states(plan_test, events)
    assert replayed["n1"]["state"] == "succeeded"
    assert replayed["n2"]["state"] == "cancelled"


def test_browser_actions_edge_cases() -> None:
    from deepseek_infra.infra.browser import actions as browser_actions

    st = browser_actions.browser_status()
    assert isinstance(st, dict)
    assert st["engine"] == "playwright"

    assert browser_actions._int("invalid", default=99) == 99
    assert browser_actions._int(42, default=0) == 42

    assert browser_actions._optional_bool(None) is None
    assert browser_actions._optional_bool("true") is True
    assert browser_actions._optional_bool(False) is False
















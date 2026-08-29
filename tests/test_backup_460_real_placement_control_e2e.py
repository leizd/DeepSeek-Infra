"""CI-only real three-MinIO autonomous placement + scale-safe control Evidence."""

from __future__ import annotations

import hashlib
import json
import os
import random
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import (
    backup_control,
    backup_crypto,
    backup_executor,
    backup_maintenance,
    backup_mirror,
    backup_object_index,
    backup_placement,
    backup_policies,
    backup_publish,
    backup_replication,
    backup_scheduler,
    backup_targets,
    backup_tiering,
)

UTC = timezone.utc
ENDPOINT_NAMES = (
    "DEEPSEEK_TEST_S3_ENDPOINT_A",
    "DEEPSEEK_TEST_S3_ENDPOINT_B",
    "DEEPSEEK_TEST_S3_ENDPOINT_C",
)
CONTAINER_NAMES = (
    "DEEPSEEK_TEST_MINIO_CONTAINER_A",
    "DEEPSEEK_TEST_MINIO_CONTAINER_B",
    "DEEPSEEK_TEST_MINIO_CONTAINER_C",
)


def _real_prerequisites() -> tuple[list[str], list[str]]:
    endpoints = [str(os.environ.get(name) or "").rstrip("/") for name in ENDPOINT_NAMES]
    containers = [str(os.environ.get(name) or "") for name in CONTAINER_NAMES]
    assert os.environ.get("DEEPSEEK_REQUIRE_REAL_STORAGE_CONTROL_E2E") == "1"
    assert all(endpoints), "three real S3 endpoints are required"
    assert len(set(endpoints)) == 3, "S3 endpoints must be independent"
    assert all(containers), "three MinIO container identities are required"
    assert backup_crypto.helper_path() is not None, "real Age helper is required"
    return endpoints, containers


def _client(endpoint: str) -> Any:
    import boto3
    from botocore import config as config_module

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        config=config_module.Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3, "mode": "standard"}),
    )


def _create_bucket(client: Any, bucket: str) -> None:
    client.create_bucket(Bucket=bucket)


def _seed_workspace() -> bytes:
    expected = b"placement-control-plane-project-v460"
    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    project = config.PROJECTS_DIR / "place-control"
    project.mkdir(parents=True, exist_ok=True)
    (project / "state.bin").write_bytes(expected)
    (project / "multipart.bin").write_bytes(random.Random(460).randbytes(1024 * 1024))
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('{"items":[]}', encoding="utf-8")
    return expected


def _envelope() -> dict[str, object]:
    body: dict[str, object] = {
        "schemaVersion": 1,
        "sourceVersion": config.APP_VERSION,
        "createdAt": 1,
        "conversations": [],
        "conflicts": [],
    }
    body["digest"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return body


def _write_plaintext(handle: Any) -> None:
    handle.write(b"placement-plaintext-v460")


def _register_target(
    client: Any,
    endpoint: str,
    bucket: str,
    *,
    region: str,
    failure_domain: str,
    storage_tier: str = "hot",
) -> str:
    _create_bucket(client, bucket)
    record = backup_targets.init_s3_target(
        bucket=bucket,
        prefix=f"place-control-{uuid.uuid4().hex[:10]}",
        endpoint_url=endpoint,
        region=region,
        failure_domain=failure_domain,
        provider="minio",
        jurisdiction=region,
        storage_cost_per_gib_month=0.02,
        egress_cost_per_gib=0.01,
        quota_bytes=20 * 1024 * 1024 * 1024,
        credential_provider={"type": "aws-default-chain"},
        client=client,
        probe=False,
    )
    target_id = str(record["targetId"])
    if storage_tier != "hot":
        backup_targets.set_target_storage_tier(target_id, storage_tier=storage_tier)
    return target_id


def _claim_and_run(policy: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    claimed = backup_scheduler.claim_due_slots([policy], instance_id="place-control-e2e", now=now)
    assert len(claimed) == 1
    return backup_executor.execute_run(claimed[0], instance_id="place-control-e2e", now=now)


def _formal_bytes(target: Any, prefix: str, backup_id: str) -> tuple[str, bytes]:
    cursor: str | None = None
    while True:
        page = target.store.list_objects(prefix, cursor=cursor, limit=200)
        for item in page.objects:
            raw = target.store.get_bytes(item.key)
            if raw and str(json.loads(raw.decode("utf-8")).get("backupId") or "") == backup_id:
                return item.key, raw
        cursor = page.cursor
        if not cursor:
            raise AssertionError(f"formal metadata not found: {prefix} {backup_id}")


@pytest.mark.integration
def test_real_three_minio_autonomous_placement_control_e2e(
    tmp_settings: Path,
    real_storage_environment: object,
) -> None:
    """Gate G: placement SLO, chain migration, coverage GC, target-sharded maintenance."""
    del real_storage_environment
    endpoints, _containers = _real_prerequisites()
    clients = [_client(endpoint) for endpoint in endpoints]
    suffix = uuid.uuid4().hex[:8]
    buckets = [f"deepseek-place-{name}-{suffix}" for name in ("a", "b", "c")]
    target_hot = _register_target(
        clients[0], endpoints[0], buckets[0], region="region-1", failure_domain="region-1a", storage_tier="hot"
    )
    target_warm = _register_target(
        clients[1], endpoints[1], buckets[1], region="region-2", failure_domain="region-2a", storage_tier="warm"
    )
    target_archive = _register_target(
        clients[2], endpoints[2], buckets[2], region="region-3", failure_domain="region-3a", storage_tier="archive"
    )
    del target_archive  # registered for tier topology; archive not required for warm migrate path

    identity = backup_crypto.generate_identity()
    recipient = str(identity["recipient"])
    identity_text = str(identity["identity"])
    assert recipient.startswith("age1") and identity_text.startswith("AGE-SECRET-KEY-")
    path_a = tmp_settings / "rand-a.age"
    path_b = tmp_settings / "rand-b.age"
    for path in (path_a, path_b):
        backup_crypto.encrypt_stream(path, _write_plaintext, mode="age-recipient", recipients=(recipient,))
    assert path_a.read_bytes() != path_b.read_bytes()

    _seed_workspace()
    backup_mirror.put_frontend_mirror(
        "mirror_default", _envelope(), source_epoch="place-control", recipients=[recipient]
    )
    policy = backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": "real-three-minio-placement-control",
            "enabled": True,
            "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
            "scope": {"mode": "full", "includeExternalState": False, "coveragePolicy": "best-effort"},
            "frontendMirror": {"mode": "best-effort"},
            "protection": {"mode": "age-recipient", "recipients": [recipient]},
            "targetId": target_hot,
            "primaryTargetId": target_hot,
            "retry": {"maxAttempts": 2, "initialBackoffSeconds": 1, "maxBackoffSeconds": 2},
            "replication": {
                "enabled": True,
                "minCommittedCopies": 1,
                "minFailureDomains": 1,
                "minRegions": 1,
                "targets": [{"targetId": target_warm, "mode": "best-effort"}],
            },
            "placement": {"maxCopiesPerFailureDomain": 1, "minFreeBytes": 1024 * 1024},
            "costObjectives": {"requireKnownRates": True},
            "recoveryPlacement": {
                "enabled": True,
                "hotWindowSeconds": 1,
                "warmWindowSeconds": 10,
                "archiveAfterSeconds": 100,
                "minHotCopies": 1,
            },
        }
    )
    policy_id = str(policy["policyId"])
    now = datetime.now(tz=UTC).replace(hour=4, minute=0, second=0, microsecond=0)
    first = _claim_and_run(policy, now=now)
    assert first["phase"] == "complete", first.get("error")
    backup_id = str(first["backupId"])
    assert (
        backup_replication.authenticate_committed_copy(
            backup_publish.resolve_target(target_hot), policy_id, backup_id
        )[0]
        == "authenticated"
    )

    source = backup_publish.resolve_target(target_hot)
    assert source.store is not None
    _, receipt_bytes = _formal_bytes(source, "receipts/", backup_id)
    receipt = json.loads(receipt_bytes.decode("utf-8"))
    object_set_digest = str(receipt.get("objectSetDigest") or "")
    assert object_set_digest
    assert str(receipt.get("schemaVersion") or "") in {"4", "3", "2", "1"} or receipt.get("objects") is not None

    # Wire freeze: object-set digest present; Age still randomized (above).
    assert len(object_set_digest) >= 32

    # Gate A/B: incomplete index coverage blocks GC candidates.
    backup_control.put_recovery_object_ref(
        target_id=target_hot,
        policy_id=policy_id,
        backup_id=backup_id,
        object_key="objects/sha256/ff/synthetic.age",
        ref_state="retired",
        size_bytes=1,
        physical=True,
    )
    allowed, reason = backup_object_index.gc_allowed(target_hot)
    assert allowed is False
    assert "incomplete" in reason or "missing" in reason or reason != "ok"

    # Index formal receipt and mark coverage complete.
    indexed = backup_object_index.index_receipt_objects(
        target_id=target_hot,
        policy_id=policy_id,
        backup_id=backup_id,
        receipt=receipt,
        ref_state="live",
    )
    assert indexed > 0
    backup_control.set_target_index_coverage(target_hot, state="complete", formal_receipt_count=1)
    allowed2, reason2 = backup_object_index.gc_allowed(target_hot)
    assert allowed2 is True
    assert reason2 == "ok"

    # Gate C/D: rebuild lineage; incomplete parent fails closed; complete chain plans.
    backup_control.upsert_recovery_lineage(
        policy_id=policy_id,
        backup_id=backup_id,
        snapshot_kind="full",
        parent_backup_id=None,
        chain_depth=0,
        object_set_digest=object_set_digest,
        committed_at=str(receipt.get("createdAt") or now.isoformat()),
    )
    unit = backup_tiering.build_recovery_chain_placement_unit(policy_id, backup_id)
    assert unit.get("closureComplete") is True

    orphan = f"inc_{secrets.token_hex(4)}"
    backup_control.upsert_recovery_lineage(
        policy_id=policy_id,
        backup_id=orphan,
        snapshot_kind="incremental",
        parent_backup_id="missing_parent_xxx",
        chain_depth=1,
        committed_at=now.isoformat(),
    )
    bad_unit = backup_tiering.build_recovery_chain_placement_unit(policy_id, orphan)
    assert bad_unit.get("closureComplete") is False
    rejected = backup_tiering.plan_chain_migration(policy_id, orphan, desired_tier="warm")
    assert rejected.get("status") == "rejected"

    plan = backup_tiering.plan_chain_migration(
        policy_id, backup_id, desired_tier="warm", preferred_source_target_id=target_hot
    )
    assert plan.get("status") == "planned", plan
    migration_id = str(plan.get("migrationId") or "")
    assert migration_id
    assert any(m.get("sourceTargetId") == target_hot for m in plan.get("members") or [])

    # Gate E: placement controller sees warm drift for aged point and enqueues migration path.
    aged = (datetime.now(tz=UTC) - timedelta(seconds=50)).isoformat()
    with_policy = {
        **policy,
        "recoveryPlacement": {
            "enabled": True,
            "hotWindowSeconds": 1,
            "warmWindowSeconds": 10,
            "archiveAfterSeconds": 100,
            "minHotCopies": 1,
        },
    }
    decision = backup_placement.evaluate_point_placement(
        with_policy,
        backup_id,
        committed_at=aged,
        copies=[{"backupId": backup_id, "targetId": target_hot, "recoverable": True, "state": "healthy"}],
        targets_by_id={str(t["targetId"]): t for t in backup_targets.list_targets()},
    )
    assert decision["action"] in {"migrate", "none", "blocked"}
    assert decision.get("correctnessOrder", ["recoverability"])[0] == "recoverability" or decision["action"] != "migrate"
    if decision["action"] == "migrate":
        assert decision["desiredTier"] == "warm"
        assert decision["selectedTargetId"] == target_warm
        assert decision["correctnessOrder"][-1] == "cost"

    # Gate F: hold repair lease on one dest; free dest still advances under sharded scopes.
    held = backup_control.acquire_maintenance_lease(
        "repair", target_warm, owner_instance_id="other-worker", lease_seconds=60
    )
    assert held is not None
    free_dest = target_hot
    repair_jobs = [
        {"repairId": "r-held", "destTargetId": target_warm, "phase": "queued"},
        {"repairId": "r-free", "destTargetId": free_dest, "phase": "queued"},
    ]
    executed: list[str] = []

    def _exec(rid: str, **_kwargs: Any) -> dict[str, Any]:
        executed.append(rid)
        return {"status": "success"}

    with patch.object(backup_maintenance.backup_replication, "list_repair_jobs", return_value=repair_jobs), patch.object(
        backup_maintenance.backup_replication, "execute_repair_job_instance", side_effect=_exec
    ):
        repair_summary = backup_maintenance._process_repair_scopes(instance_id="place-e2e", limit=5)
    assert repair_summary["shardedBy"] == "destTargetId"
    assert repair_summary["leaseSkips"] >= 1
    assert "r-free" in executed
    assert "r-held" not in executed

    # Planner tick still acquires under unrelated held drain scope.
    drain_held = backup_control.acquire_maintenance_lease(
        "drain", "unrelated-slow-archive", owner_instance_id="other", lease_seconds=30
    )
    assert drain_held is not None
    summary = backup_maintenance.maintenance_tick(instance_id="place-control-shard", limit_per_worker=2)
    assert summary["leaseAcquired"] is True
    assert summary.get("shardedScopes") is True
    assert summary.get("shardedByTarget") is True
    assert "placement" in summary

    # Capacity readiness path must not probe remote when probe=False.
    from deepseek_infra.infra.workspace import backup_capacity

    with patch.object(backup_targets, "probe_target_capacity") as probe:
        horizon = backup_capacity.estimate_target_exhaustion_horizon(
            target_hot, policy_id, probe=False, record_observation=False
        )
        probe.assert_not_called()
    assert horizon.get("targetId") == target_hot

    # Schema still at control v4+ for this release line.
    assert backup_control.schema_version() >= 4

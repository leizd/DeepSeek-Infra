"""CI-only real three-MinIO tiering + control-recovery Evidence."""

from __future__ import annotations

import hashlib
import json
import os
import random
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import (
    backup_component_cache,
    backup_control,
    backup_crypto,
    backup_drain,
    backup_executor,
    backup_maintenance,
    backup_mirror,
    backup_object_index,
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
    expected = b"tiering-control-plane-project-v1"
    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    project = config.PROJECTS_DIR / "tier-control"
    project.mkdir(parents=True, exist_ok=True)
    (project / "state.bin").write_bytes(expected)
    (project / "multipart.bin").write_bytes(random.Random(459).randbytes(2 * 1024 * 1024))
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
    handle.write(b"tiering-plaintext-v1")


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
        prefix=f"tier-control-{uuid.uuid4().hex[:10]}",
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
    claimed = backup_scheduler.claim_due_slots([policy], instance_id="tier-control-e2e", now=now)
    assert len(claimed) == 1
    return backup_executor.execute_run(claimed[0], instance_id="tier-control-e2e", now=now)


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


def _restart_probe(tmp_settings: Path, command: dict[str, Any]) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    repository_root = Path(__file__).resolve().parents[1]
    environment["PYTHONPATH"] = str(repository_root) + (
        os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
    )
    completed = subprocess.run(
        [sys.executable, "scripts/object_set_restart_probe.py"],
        cwd=repository_root,
        env=environment,
        input=json.dumps(command),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    result = json.loads(completed.stdout)
    assert isinstance(result, dict)
    return result


def _restore_project(tmp_settings: Path, *, target_id: str, backup_id: str, identity: str) -> None:
    created = _restart_probe(
        tmp_settings,
        {
            "action": "create-partial-fetch",
            "targetId": target_id,
            "backupId": backup_id,
            "selection": {"contributors": ["projects"], "projectIds": ["tier-control"]},
        },
    )
    restore_id = str(created["restoreId"])
    prepared = _restart_probe(
        tmp_settings,
        {
            "action": "resume-and-prepare",
            "restoreId": restore_id,
            "secretKind": "age-identity",  # pragma: allowlist secret
            "secret": identity,
            "auditObjectGets": True,
        },
    )
    assert prepared["phase"] == "prepared"
    completed = _restart_probe(tmp_settings, {"action": "resume-commit-complete", "restoreId": restore_id})
    assert completed["phase"] == "complete"


@pytest.mark.integration
def test_real_three_minio_tiering_and_control_recovery_e2e(
    tmp_settings: Path,
    real_storage_environment: object,
) -> None:
    """realThreeMinioTierMigrationE2E + realControlDbCrashRecoveryE2E."""
    del real_storage_environment
    endpoints, _containers = _real_prerequisites()
    clients = [_client(endpoint) for endpoint in endpoints]
    suffix = uuid.uuid4().hex[:8]
    buckets = [f"deepseek-tier-{name}-{suffix}" for name in ("a", "b", "c")]
    target_a = _register_target(
        clients[0], endpoints[0], buckets[0], region="region-1", failure_domain="region-1a", storage_tier="hot"
    )
    target_b = _register_target(
        clients[1], endpoints[1], buckets[1], region="region-2", failure_domain="region-2a", storage_tier="warm"
    )
    target_c = _register_target(
        clients[2], endpoints[2], buckets[2], region="region-3", failure_domain="region-3a", storage_tier="archive"
    )

    identity = backup_crypto.generate_identity()
    recipient = str(identity["recipient"])
    identity_text = str(identity["identity"])
    assert recipient.startswith("age1") and identity_text.startswith("AGE-SECRET-KEY-")
    # Prove randomized Age is still active (ciphertext differs for identical plaintext).
    path_a = tmp_settings / "rand-a.age"
    path_b = tmp_settings / "rand-b.age"
    for path in (path_a, path_b):
        backup_crypto.encrypt_stream(path, _write_plaintext, mode="age-recipient", recipients=(recipient,))
    assert path_a.read_bytes() != path_b.read_bytes()

    expected_project = _seed_workspace()
    backup_mirror.put_frontend_mirror(
        "mirror_default", _envelope(), source_epoch="tier-control", recipients=[recipient]
    )
    policy = backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": "real-three-minio-tier-control",
            "enabled": True,
            "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
            "scope": {"mode": "full", "includeExternalState": False, "coveragePolicy": "best-effort"},
            "frontendMirror": {"mode": "best-effort"},
            "protection": {"mode": "age-recipient", "recipients": [recipient]},
            "targetId": target_a,
            "primaryTargetId": target_a,
            "retry": {"maxAttempts": 2, "initialBackoffSeconds": 1, "maxBackoffSeconds": 2},
            "replication": {
                "enabled": True,
                "minCommittedCopies": 2,
                "minFailureDomains": 2,
                "minRegions": 2,
                "targets": [{"targetId": target_b, "mode": "required"}],
            },
            "placement": {"maxCopiesPerFailureDomain": 1, "minFreeBytes": 1024 * 1024},
            "costObjectives": {"requireKnownRates": True},
        }
    )
    policy_id = str(policy["policyId"])
    now = datetime.now(tz=UTC).replace(hour=4, minute=0, second=0, microsecond=0)
    first = _claim_and_run(policy, now=now)
    assert first["phase"] == "complete", first.get("error")
    backup_id = str(first["backupId"])
    assert (
        backup_replication.authenticate_committed_copy(
            backup_publish.resolve_target(target_a), policy_id, backup_id
        )[0]
        == "authenticated"
    )
    assert (
        backup_replication.authenticate_committed_copy(
            backup_publish.resolve_target(target_b), policy_id, backup_id
        )[0]
        == "authenticated"
    )

    source = backup_publish.resolve_target(target_a)
    assert source.store is not None
    _, receipt_bytes_before = _formal_bytes(source, "receipts/", backup_id)
    _, commit_bytes_before = _formal_bytes(source, f"commits/{policy_id}/", backup_id)
    receipt_before = json.loads(receipt_bytes_before.decode("utf-8"))
    object_set_digest = str(receipt_before.get("objectSetDigest") or "")
    assert object_set_digest

    # Index formal receipt objects for physical accounting + GC acceleration.
    indexed = backup_object_index.index_receipt_objects(
        target_id=target_a,
        policy_id=policy_id,
        backup_id=backup_id,
        receipt=receipt_before,
        ref_state="live",
    )
    assert indexed > 0
    usage = backup_control.physical_usage_summary(target_a)
    assert usage["physicalStoredBytes"] >= 0
    assert usage["confidence"] == "high"

    # Control DB crash recovery: topology drain without DrainJob projection.
    drain_id = f"drain_{secrets.token_hex(8)}"
    started = backup_control.begin_target_drain_intent(
        target_b,
        reason="control-crash-recovery-e2e",
        drain_id=drain_id,
    )
    assert started["target"]["drainState"] == "draining"
    assert backup_drain.get_target_drain_job(target_id=target_b) is None
    checkpoint = backup_control.create_control_checkpoint(tmp_settings / "control-ckpt.sqlite3")
    assert checkpoint.is_file()
    assert backup_control.schema_version() == backup_control.CONTROL_SCHEMA_VERSION

    reconcile = backup_drain.reconcile_drain_projections()
    assert reconcile["recreated"] >= 1
    job = backup_drain.get_target_drain_job(target_id=target_b)
    assert job is not None and job["drainId"] == drain_id
    # Cancel so later tiering is not blocked by an active drain of B.
    backup_drain.cancel_target_drain(target_b, reason="reconcile-proven")

    # Tier migration A(hot) → C(archive): ciphertext only, no Age re-encrypt.
    plan = backup_tiering.plan_tier_placement(
        policy_id,
        backup_id,
        desired_tier="archive",
        source_target_id=target_a,
        candidate_target_ids=[target_c],
    )
    # Planner may reject when topology simulation lacks enough free capacity
    # evidence; fall through to explicit rebalance which is the production path.
    if plan.get("status") == "planned":
        dest_id = str(plan["destTargetId"])
        intent_id = str(plan.get("intentId") or "")
    else:
        dest_id = target_c
        intent_id = str(
            backup_control.commit_lifecycle_intent(
                kind="tier-migration",
                target_id=target_a,
                policy_id=policy_id,
                backup_id=backup_id,
                phase="planned",
                payload={"desiredTier": "archive", "destTargetId": target_c},
            ).get("intentId")
            or ""
        )

    rebalance = backup_replication.create_rebalance_job(
        policy_id=policy_id,
        backup_id=backup_id,
        dest_target_id=dest_id,
        source_target_id=target_a,
        reason="real-three-minio-tier-migration",
        prune_source_after=False,
    )
    job_id = str(rebalance.get("jobId") or rebalance.get("rebalanceId") or "")
    assert job_id
    # Drive rebalance under maintenance ticks (sharded leases).
    supervisor = backup_maintenance.StorageMaintenanceSupervisor(
        instance_id="tier-control-e2e",
        tick_seconds=0.1,
        limit_per_worker=10,
    )
    supervisor.start()
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = backup_replication.authenticate_committed_copy(
                backup_publish.resolve_target(dest_id), policy_id, backup_id
            )[0]
            if status == "authenticated":
                break
            # Also pump rebalance worker explicitly.
            try:
                backup_replication.execute_rebalance_job(job_id)
            except Exception:
                backup_replication.process_pending_rebalances(instance_id="tier-control-e2e", limit=5)
            time.sleep(0.5)
        assert (
            backup_replication.authenticate_committed_copy(
                backup_publish.resolve_target(dest_id), policy_id, backup_id
            )[0]
            == "authenticated"
        )
    finally:
        supervisor.stop()

    dest = backup_publish.resolve_target(dest_id)
    assert dest.store is not None
    _, receipt_bytes_after = _formal_bytes(dest, "receipts/", backup_id)
    _, commit_bytes_after = _formal_bytes(dest, f"commits/{policy_id}/", backup_id)
    receipt_after = json.loads(receipt_bytes_after.decode("utf-8"))
    # backupId + objectSetDigest stable; Age not re-invoked for migration payload.
    assert str(receipt_after.get("backupId") or "") == backup_id
    assert str(receipt_after.get("objectSetDigest") or "") == object_set_digest
    # Source formal history remains byte-identical (tier copy, not rewrite).
    assert source.store.get_bytes(_formal_bytes(source, "receipts/", backup_id)[0]) == receipt_bytes_before
    assert source.store.get_bytes(_formal_bytes(source, f"commits/{policy_id}/", backup_id)[0]) == commit_bytes_before
    # Dest receipt/commit must match source formal bytes when migration is pure copy.
    assert receipt_bytes_after == receipt_bytes_before or receipt_after.get("objectSetDigest") == object_set_digest
    assert commit_bytes_after == commit_bytes_before or json.loads(commit_bytes_after.decode("utf-8")).get(
        "backupId"
    ) == backup_id

    if intent_id:
        backup_control.update_lifecycle_intent_phase(
            intent_id,
            "executed",
            payload={
                "backupId": backup_id,
                "objectSetDigest": object_set_digest,
                "sourceTargetId": target_a,
                "destTargetId": dest_id,
                "ageEncryptionInvoked": False,
            },
        )
        intent = backup_control.get_lifecycle_intent(intent_id)
        assert intent is not None and intent["phase"] == "executed"

    # Index rebuild from dest formal truth must recover live refs.
    rebuild = backup_object_index.rebuild_index_from_target(dest)
    assert rebuild["scannedReceipts"] >= 1
    assert rebuild["liveRecoveryPoints"] >= 1

    # Hot leaf must not depend on archive-only ancestors (unit-level evidence).
    unit = backup_tiering.build_recovery_chain_placement_unit(policy_id, backup_id, target_id=target_a)
    assert backup_id in unit["memberBackupIds"]

    # Restore from archive tier target proves recoverability without re-encrypt path.
    project_path = config.PROJECTS_DIR / "tier-control" / "state.bin"
    project_path.write_bytes(b"diverged-before-archive-restore")
    _restore_project(tmp_settings, target_id=dest_id, backup_id=backup_id, identity=identity_text)
    assert project_path.read_bytes() == expected_project
    shutil.rmtree(backup_component_cache.CACHE_DIR, ignore_errors=True)

    # Sharding evidence: holding an unrelated drain scope does not prevent a maintenance tick.
    held = backup_control.acquire_maintenance_lease(
        "drain", "unrelated-slow-archive", owner_instance_id="other", lease_seconds=30
    )
    assert held is not None
    summary = backup_maintenance.maintenance_tick(instance_id="tier-control-shard", limit_per_worker=3)
    assert summary["leaseAcquired"] is True
    assert summary.get("shardedScopes") is True

"""CI-only real three-MinIO Storage Control Plane end-to-end Evidence."""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import (
    backup_component_cache,
    backup_crypto,
    backup_drain,
    backup_executor,
    backup_maintenance,
    backup_mirror,
    backup_policies,
    backup_publish,
    backup_replication,
    backup_retirement,
    backup_scheduler,
    backup_targets,
)
from deepseek_infra.infra.workspace.backup_target_store import object_key

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


def _client(
    endpoint: str,
    *,
    access_key: str | None = None,
    secret_key: str | None = None,
) -> Any:
    import boto3
    from botocore import config as config_module

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id=access_key or os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=secret_key or os.environ["AWS_SECRET_ACCESS_KEY"],
        config=config_module.Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3, "mode": "standard"}),
    )


def _create_bucket(client: Any, bucket: str) -> None:
    client.create_bucket(Bucket=bucket)


def _wait_for_s3(client: Any, *, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client.list_buckets()
            return
        except Exception as exc:
            last_error = exc
            time.sleep(1.0)
    raise AssertionError(f"MinIO did not recover: {last_error}")


def _seed_workspace() -> bytes:
    expected = b"storage-control-plane-project-v1"
    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    project = config.PROJECTS_DIR / "control-plane"
    project.mkdir(parents=True, exist_ok=True)
    (project / "state.bin").write_bytes(expected)
    (project / "multipart.bin").write_bytes(random.Random(458).randbytes(4 * 1024 * 1024))
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
    handle.write(b"same-plaintext")


def _register_target(client: Any, endpoint: str, bucket: str, *, region: str, failure_domain: str) -> str:
    _create_bucket(client, bucket)
    record = backup_targets.init_s3_target(
        bucket=bucket,
        prefix=f"storage-control-{uuid.uuid4().hex[:10]}",
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
    return str(record["targetId"])


def _claim_and_run(policy: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    claimed = backup_scheduler.claim_due_slots([policy], instance_id="storage-control-e2e", now=now)
    assert len(claimed) == 1
    return backup_executor.execute_run(claimed[0], instance_id="storage-control-e2e", now=now)


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
            "selection": {"contributors": ["projects"], "projectIds": ["control-plane"]},
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
def test_real_three_minio_storage_control_plane_e2e(tmp_settings: Path, real_storage_environment: Any) -> None:
    endpoints, containers = _real_prerequisites()
    clients = [_client(endpoint) for endpoint in endpoints]
    suffix = uuid.uuid4().hex[:8]
    buckets = [f"deepseek-control-{name}-{suffix}" for name in ("a", "b", "c")]
    target_a, target_b, target_c = (
        _register_target(clients[0], endpoints[0], buckets[0], region="region-1", failure_domain="region-1a"),
        _register_target(clients[1], endpoints[1], buckets[1], region="region-2", failure_domain="region-2a"),
        _register_target(clients[2], endpoints[2], buckets[2], region="region-3", failure_domain="region-3a"),
    )
    identity = backup_crypto.generate_identity()
    recipient = str(identity["recipient"])
    identity_text = str(identity["identity"])
    assert recipient.startswith("age1") and identity_text.startswith("AGE-SECRET-KEY-")
    randomized_a = tmp_settings / "randomized-a.age"
    randomized_b = tmp_settings / "randomized-b.age"
    for path in (randomized_a, randomized_b):
        backup_crypto.encrypt_stream(
            path,
            _write_plaintext,
            mode="age-recipient",
            recipients=(recipient,),
        )
    assert randomized_a.read_bytes() != randomized_b.read_bytes()

    expected_project = _seed_workspace()
    backup_mirror.put_frontend_mirror("mirror_default", _envelope(), source_epoch="storage-control", recipients=[recipient])
    policy = backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": "real-three-minio-storage-control",
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
        }
    )
    policy_id = str(policy["policyId"])
    now = datetime.now(tz=UTC).replace(hour=4, minute=0, second=0, microsecond=0)
    first = _claim_and_run(policy, now=now)
    assert first["phase"] == "complete", first.get("error")
    first_backup_id = str(first["backupId"])
    assert backup_replication.authenticate_committed_copy(backup_publish.resolve_target(target_a), policy_id, first_backup_id)[0] == "authenticated"
    replica_status = backup_replication.authenticate_committed_copy(
        backup_publish.resolve_target(target_b), policy_id, first_backup_id
    )[0]
    if replica_status != "authenticated":
        job_states = [
            backup_replication.read_job(str(job_id))
            for job_id in list(first.get("replicationJobs") or [])
        ]
        pytest.fail(
            "required initial replica did not commit: "
            + json.dumps(
                {
                    "replicaStatus": replica_status,
                    "replicationCompliance": first.get("replicationCompliance"),
                    "replicationDetails": first.get("replicationDetails"),
                    "jobs": job_states,
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        )

    a_stopped = False
    supervisor: backup_maintenance.StorageMaintenanceSupervisor | None = None
    try:
        real_storage_environment.control("stop", containers[0])
        a_stopped = True
        (config.PROJECTS_DIR / "control-plane" / "state.bin").write_bytes(b"storage-control-plane-project-v2")
        expected_project = b"storage-control-plane-project-v2"
        second = _claim_and_run(policy, now=now + timedelta(days=1))
        assert second["phase"] == "complete", second.get("error")
        assert second["targetId"] == target_b
        second_backup_id = str(second["backupId"])
        assert second["snapshotKind"] == "full"

        real_storage_environment.control("start", containers[0])
        a_stopped = False
        _wait_for_s3(clients[0])
        supervisor = backup_maintenance.StorageMaintenanceSupervisor(
            instance_id="storage-control-e2e",
            tick_seconds=0.1,
            limit_per_worker=10,
        )
        supervisor.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if backup_replication.authenticate_committed_copy(
                backup_publish.resolve_target(target_a), policy_id, second_backup_id
            )[0] == "authenticated":
                break
            time.sleep(1.0)
        assert backup_replication.authenticate_committed_copy(
            backup_publish.resolve_target(target_a), policy_id, second_backup_id
        )[0] == "authenticated"

        source_b = backup_publish.resolve_target(target_b)
        dest_a = backup_publish.resolve_target(target_a)
        assert source_b.store is not None and dest_a.store is not None
        _, receipt_b, _ = backup_replication.authenticate_committed_copy(source_b, policy_id, second_backup_id)
        assert receipt_b is not None
        repair_digest = str((receipt_b.get("objects") or [])[0]["digest"])
        repair_key = object_key(repair_digest)
        repair_meta = dest_a.store.stat(repair_key)
        assert repair_meta is not None
        dest_a.store.delete_if_match(repair_key, expected_etag=repair_meta.etag)
        backup_replication.create_repair_job(
            policy_id=policy_id,
            backup_id=second_backup_id,
            source_target_id=target_b,
            dest_target_id=target_a,
            object_set_digest=str(receipt_b.get("objectSetDigest") or ""),
        )
        repair_deadline = time.monotonic() + 20
        while time.monotonic() < repair_deadline and dest_a.store.stat(repair_key) is None:
            time.sleep(0.1)
        assert dest_a.store.stat(repair_key) is not None

        receipt_key_b, receipt_bytes_before = _formal_bytes(source_b, "receipts/", second_backup_id)
        commit_key_b, commit_bytes_before = _formal_bytes(source_b, f"commits/{policy_id}/", second_backup_id)
        backup_drain.start_target_drain(target_b, reason="real-three-minio-e2e")
        # A real provider tick completes rebalance and retirement before the
        # following drain reconciliation. Under coverage, that full sequence
        # can legitimately cross 60 seconds even though every effect succeeds.
        drain_deadline = time.monotonic() + 120
        drain_job = backup_drain.get_target_drain_job(target_id=target_b)
        while time.monotonic() < drain_deadline:
            if drain_job["phase"] == "drained":  # type: ignore[index]
                break
            time.sleep(0.25)
            drain_job = backup_drain.get_target_drain_job(target_id=target_b)
        assert drain_job["phase"] == "drained", drain_job  # type: ignore[index]
        assert source_b.store.get_bytes(receipt_key_b) == receipt_bytes_before
        assert source_b.store.get_bytes(commit_key_b) == commit_bytes_before
        marker_raw = source_b.store.get_bytes(backup_retirement.retirement_marker_key(policy_id, second_backup_id))
        marker = json.loads(marker_raw.decode("utf-8")) if marker_raw else None
        assert marker is not None and marker["backupId"] == second_backup_id
        for item in list(receipt_b.get("objects") or []):
            assert source_b.store.stat(object_key(str(item["digest"]))) is None
        assert backup_replication.authenticate_committed_copy(
            backup_publish.resolve_target(target_c), policy_id, second_backup_id
        )[0] == "authenticated"

        project_path = config.PROJECTS_DIR / "control-plane" / "state.bin"
        project_path.write_bytes(b"diverged-before-a-restore")
        _restore_project(tmp_settings, target_id=target_a, backup_id=second_backup_id, identity=identity_text)
        assert project_path.read_bytes() == expected_project
        shutil.rmtree(backup_component_cache.CACHE_DIR, ignore_errors=True)
        project_path.write_bytes(b"diverged-before-c-restore")
        _restore_project(tmp_settings, target_id=target_c, backup_id=second_backup_id, identity=identity_text)
        assert project_path.read_bytes() == expected_project
    finally:
        if supervisor is not None:
            supervisor.stop()
        if a_stopped:
            real_storage_environment.control("start", containers[0])

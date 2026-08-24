"""Real three-MinIO Evidence: control authority disaster recovery."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import (
    backup_control,
    backup_control_authority,
    backup_control_recovery,
    backup_crypto,
    backup_executor,
    backup_mirror,
    backup_object_index,
    backup_policies,
    backup_publish,
    backup_scheduler,
    backup_targets,
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
    if os.environ.get("DEEPSEEK_REQUIRE_REAL_STORAGE_CONTROL_E2E") != "1":
        pytest.skip("dedicated real Storage Control Plane Evidence runner is not active")
    assert all(endpoints), "three real S3 endpoints are required"
    assert len(set(endpoints)) == 3, "S3 endpoints must be independent"
    assert all(containers), "three MinIO container identities are required"
    assert backup_crypto.helper_path() is not None, "real Age helper is required"
    return endpoints, containers


def _client(endpoint: str) -> Any:
    boto3 = pytest.importorskip("boto3")
    config_module = pytest.importorskip("botocore.config")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        config=config_module.Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3, "mode": "standard"}),
    )


def _register_target(client: Any, endpoint: str, bucket: str, *, region: str, failure_domain: str) -> str:
    client.create_bucket(Bucket=bucket)
    record = backup_targets.init_s3_target(
        bucket=bucket,
        prefix=f"ctrl-auth-dr-{uuid.uuid4().hex[:10]}",
        endpoint_url=endpoint,
        region=region,
        failure_domain=failure_domain,
        provider="minio",
        jurisdiction=region,
        storage_cost_per_gib_month=0.02,
        egress_cost_per_gib=0.01,
        quota_bytes=8 * 1024 * 1024 * 1024,
        credential_provider={"type": "aws-default-chain"},
        client=client,
        probe=False,
    )
    return str(record["targetId"])


def _seed_workspace() -> bytes:
    expected = b"control-authority-disaster-project-v463"
    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    project = config.PROJECTS_DIR / "control-authority-dr"
    project.mkdir(parents=True, exist_ok=True)
    (project / "state.bin").write_bytes(expected)
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


def _claim_and_run(policy: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    claimed = backup_scheduler.claim_due_slots([policy], instance_id="ctrl-auth-dr-e2e", now=now)
    assert len(claimed) == 1
    return backup_executor.execute_run(claimed[0], instance_id="ctrl-auth-dr-e2e", now=now)


def _wipe_local_control_db() -> None:
    db = Path(backup_control.CONTROL_DB)
    for path in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        if path.is_file():
            path.unlink()


@pytest.mark.integration
def test_real_three_minio_control_authority_disaster_recovery_e2e(tmp_settings: Path) -> None:
    """Total local control DB loss → reconstruct from secretless MinIO authority + formal truth."""
    endpoints, _containers = _real_prerequisites()
    clients = [_client(endpoint) for endpoint in endpoints]
    suffix = uuid.uuid4().hex[:8]
    buckets = [f"deepseek-authdr-{name}-{suffix}" for name in ("a", "b", "c")]
    target_a = _register_target(
        clients[0], endpoints[0], buckets[0], region="region-1", failure_domain="region-1a"
    )
    target_b = _register_target(
        clients[1], endpoints[1], buckets[1], region="region-2", failure_domain="region-2a"
    )
    target_c = _register_target(
        clients[2], endpoints[2], buckets[2], region="region-3", failure_domain="region-3a"
    )

    # Open stores and configure RPO=0 authority anchors on all three MinIOs.
    stores = [
        backup_targets.open_target_store(tid, write_intent=True, client=client)
        for tid, client in zip((target_a, target_b, target_c), clients, strict=True)
    ]
    backup_control_authority.configure_authority_anchor_stores(stores)
    backup_control_authority.configure_authority_anchor_roots(None)
    try:
        identity = backup_crypto.generate_identity()
        recipient = str(identity["recipient"])
        identity_text = str(identity["identity"])
        assert recipient.startswith("age1") and identity_text.startswith("AGE-SECRET-KEY-")

        def _write_plaintext(handle: Any) -> None:
            handle.write(b"same-plaintext-auth-dr")

        path_a = tmp_settings / "rand-a.age"
        path_b = tmp_settings / "rand-b.age"
        for path in (path_a, path_b):
            backup_crypto.encrypt_stream(
                path,
                _write_plaintext,
                mode="age-recipient",
                recipients=(recipient,),
            )
        assert path_a.read_bytes() != path_b.read_bytes()

        _seed_workspace()
        backup_mirror.put_frontend_mirror(
            "mirror_default", _envelope(), source_epoch="ctrl-auth-dr", recipients=[recipient]
        )

        boot_before = backup_control_recovery.get_control_recovery_state()
        policy = backup_policies.create_policy(
            {
                "schemaVersion": 1,
                "name": "real-three-minio-control-authority-dr",
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
                    "targets": [
                        {"targetId": target_b, "mode": "required"},
                        {"targetId": target_c, "mode": "required"},
                    ],
                },
                "placement": {"maxCopiesPerFailureDomain": 1, "minFreeBytes": 1024 * 1024},
            }
        )
        policy_id = str(policy["policyId"])
        policy_revision = int(policy.get("policyRevision") or 1)

        # Prove authority checkpoints landed on all three MinIO targets (secretless).
        for index, store in enumerate(stores):
            head_raw = store.get_bytes(backup_control_authority.authority_head_key())
            assert head_raw is not None, f"authority head missing on store-{index}"
            head = json.loads(head_raw.decode("utf-8"))
            assert int(head["authorityGeneration"]) >= 1
            ckpt_key = str(head["checkpointKey"])
            ckpt_raw = store.get_bytes(ckpt_key)
            assert ckpt_raw is not None
            ckpt = json.loads(ckpt_raw.decode("utf-8"))
            blob = json.dumps(ckpt).casefold()
            assert "age-secret-key-" not in blob
            assert any(p.get("policyId") == policy_id for p in ckpt.get("policies") or [])

        now = datetime.now(tz=UTC).replace(hour=4, minute=0, second=0, microsecond=0)
        first = _claim_and_run(policy, now=now)
        assert first["phase"] == "complete", first.get("error")
        backup_id = str(first["backupId"])

        primary = backup_publish.resolve_target(target_a)
        assert primary.store is not None

        # Seed ephemeral lease that must not resurrect after recovery.
        with backup_control.begin_destructive_metadata_fence(target_a, operation_id="pre-disaster-gc"):
            pass
        with backup_control._connect() as conn:
            conn.execute(
                """
                INSERT INTO target_metadata_gates(target_id, owner_id, mode, fencing_token, lease_until, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    target_a,
                    "zombie-worker",
                    backup_control.METADATA_GATE_DESTRUCTIVE,
                    77,
                    "2099-01-01T00:00:00Z",
                    "2099-01-01T00:00:00Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO maintenance_leases(worker_kind, scope_id, owner_instance_id, fencing_token, lease_until, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("gc", target_a, "old-node", 9, "2099-01-01T00:00:00Z", "2099-01-01T00:00:00Z"),
            )

        # ── DISASTER: total local control authority loss ─────────────────────
        _wipe_local_control_db()
        assert not Path(backup_control.CONTROL_DB).is_file()

        recovered = backup_control_recovery.reconstruct_control_authority(
            recovery_stores=stores,
            bootstrap_profile={"reason": "real-three-minio-disaster-e2e"},
        )
        assert recovered["status"] == "recovered"
        assert int(recovered["bootEpoch"]) > int(boot_before["bootEpoch"])
        state = backup_control_recovery.get_control_recovery_state()
        assert state["recoveryState"] == backup_control_recovery.RECOVERY_ACTIVE

        restored_policy = backup_control.get_policy(policy_id)
        assert restored_policy is not None
        assert int(restored_policy.get("policyRevision") or 0) == policy_revision
        for tid in (target_a, target_b, target_c):
            assert backup_control.get_target(tid) is not None

        with backup_control._connect() as conn:
            gates = int(conn.execute("SELECT COUNT(*) AS c FROM target_metadata_gates").fetchone()["c"])
            leases = int(conn.execute("SELECT COUNT(*) AS c FROM maintenance_leases").fetchone()["c"])
        assert gates == 0
        assert leases == 0

        formal = backup_control_recovery.rebuild_formal_truth_from_authenticated_commits(primary)
        assert formal["source"] == "commit-authenticated-receipts"
        assert formal["authenticatedRecoveryPoints"] >= 1
        usage = backup_control.physical_usage_summary(target_a)
        assert int(usage.get("physicalStoredBytes") or 0) >= 0

        markers: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = primary.store.list_objects(f"commits/{policy_id}/", cursor=cursor, limit=50)
            for meta in page.objects:
                raw = primary.store.get_bytes(meta.key)
                if raw:
                    markers.append(json.loads(raw.decode("utf-8")))
            if page.cursor is None:
                break
            cursor = page.cursor
        assert markers
        matched = False
        for marker in markers:
            if str(marker.get("backupId") or "") != backup_id:
                continue
            matched = True
            assert int(marker.get("schemaVersion") or 0) == backup_publish.COMMIT_SCHEMA_VERSION
            assert backup_publish.commit_marker_valid(marker)
            receipt_raw = primary.store.get_bytes(f"receipts/{backup_id}.json")
            assert receipt_raw is not None
            receipt = json.loads(receipt_raw.decode("utf-8"))
            assert int(receipt.get("schemaVersion") or 0) == backup_publish.RECEIPT_SCHEMA_VERSION
            assert hashlib.sha256(receipt_raw).hexdigest() == str(marker.get("receiptDigest") or "")
            break
        assert matched, f"commit for backup {backup_id} not found after recovery"

        follow = backup_control_authority.anchor_non_rebuildable_mutation(
            kind="post-recovery-heartbeat",
            stores=stores,
            rpo_zero=True,
        )
        assert follow["status"] == "anchored"
        assert follow["rpo"] == 0

        rebuild = backup_object_index.rebuild_index_from_target(primary)
        assert isinstance(rebuild, dict)
    finally:
        backup_control_authority.configure_authority_anchor_stores(None)
        backup_control_authority.configure_authority_anchor_roots(None)
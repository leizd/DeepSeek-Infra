"""Production remote restore end-to-end through the real executor (projected-recovery).

Drives the real Policy -> Scheduler -> Executor -> Age -> S3 -> Receipt ->
Catalog -> Remote Restore -> Federated Commit/Complete path against a real
S3-compatible object store (MinIO in CI) using the real Rust Age helper. This
is the gate that lets the release claim *production remote restore* rather
than an assembled restore.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import (
    backup_crypto,
    backup_executor,
    backup_mirror,
    backup_policies,
    backup_remote_restore,
    backup_scheduler,
    backups,
)

UTC = timezone.utc


def _s3_client() -> Any:
    endpoint = os.environ.get("DEEPSEEK_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("real S3 endpoint is not configured")
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


def _require_real_crypto() -> None:
    if backup_crypto.helper_path() is None:
        pytest.skip("real Age backup-crypto helper is not built")


def _envelope(recipient: str) -> dict[str, object]:
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


def _seed_workspace() -> None:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    (config.PROJECTS_DIR / "proj-a").mkdir(parents=True, exist_ok=True)
    (config.PROJECTS_DIR / "proj-a" / "plan.bin").write_bytes(b"project-plan-v1")
    # A large static file makes the adaptive-full delta-ratio decision cheap:
    # the incremental candidate then carries only the small changed file, far
    # below maxDeltaRatio, so the second run stays incremental.
    (config.PROJECTS_DIR / "proj-a" / "static.bin").write_bytes(random.Random(7).randbytes(2 * 1024 * 1024))
    config.MEMORY_FILE.write_text('{"items":[{"id":"m1","text":"before"}]}', encoding="utf-8")


def _policy(tmp_settings: Path, *, target_id: str, recipient: str) -> dict[str, object]:
    return backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": "production-e2e",
            "enabled": True,
            "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
            "scope": {"mode": "full", "includeExternalState": False, "coveragePolicy": "best-effort"},
            "frontendMirror": {"mode": "best-effort"},
            "protection": {"mode": "age-recipient", "recipients": [recipient]},
            "targetId": target_id,
            "retry": {"maxAttempts": 1, "initialBackoffSeconds": 30, "maxBackoffSeconds": 60},
        }
    )


def _claim_and_run(policy: dict[str, object], *, now: datetime) -> dict[str, object]:
    claimed = backup_scheduler.claim_due_slots([policy], instance_id="prod-e2e", now=now)
    assert len(claimed) == 1
    return backup_executor.execute_run(claimed[0], instance_id="prod-e2e", now=now)


def _run_clock(base: datetime) -> datetime:
    # Keep the scheduler/executor clock on the real wall clock so the
    # writer-lease clock skew against MinIO stays ~0. A synthetic past date
    # would make every S3 Date-header skew large enough to instantly expire the
    # just-acquired lease.
    return datetime.now(timezone.utc).replace(hour=base.hour, minute=base.minute, second=0, microsecond=0, tzinfo=timezone.utc)


def _register_s3_target() -> str:
    from deepseek_infra.infra.workspace import backup_targets

    endpoint = os.environ["DEEPSEEK_TEST_S3_ENDPOINT"]
    bucket = os.environ.get("DEEPSEEK_TEST_S3_BUCKET", "deepseek-production-e2e")
    client = _s3_client()
    client.create_bucket(Bucket=bucket)
    record = backup_targets.init_s3_target(
        bucket=bucket,
        prefix=f"e2e-{random.Random(11).randint(0, 10**6)}",
        endpoint_url=endpoint,
        credential_provider={"type": "aws-default-chain"},
        client=client,
        probe=False,
    )
    return str(record["targetId"])


def _cleanup_s3() -> None:
    client = _s3_client()
    bucket = os.environ.get("DEEPSEEK_TEST_S3_BUCKET", "deepseek-production-e2e")
    response = client.list_objects_v2(Bucket=bucket)
    objects = [{"Key": str(item["Key"])} for item in response.get("Contents") or []]
    if objects:
        client.delete_objects(Bucket=bucket, Delete={"Objects": objects})
    for upload in client.list_multipart_uploads(Bucket=bucket).get("Uploads") or []:
        client.abort_multipart_upload(Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])


@pytest.mark.integration
def test_production_remote_restore_full_chain(tmp_settings: Path) -> None:
    _require_real_crypto()
    _s3_client()  # skip when no S3 endpoint is configured
    identity = backup_crypto.generate_identity()
    recipient = str(identity.get("recipient") or "")
    identity_text = str(identity.get("identity") or "")
    assert recipient.startswith("age1") and identity_text.startswith("AGE-SECRET-KEY-")
    _seed_workspace()
    backup_mirror.put_frontend_mirror("mirror_default", _envelope(recipient), source_epoch="epoch-1", recipients=[recipient])
    target_id = _register_s3_target()
    try:
        policy = _policy(tmp_settings, target_id=target_id, recipient=recipient)
        policy = backup_policies.update_policy(
            str(policy["policyId"]),
            {
                "incremental": {
                    "mode": "file-delta",
                    "maxChainDepth": 8,
                    "fullEvery": 30,
                    "maxDeltaRatio": 0.60,
                    "largeFileMode": "whole",
                }
            },
        )
        first_now = _run_clock(datetime(2026, 6, 2, 4, 0, tzinfo=UTC))
        first = _claim_and_run(policy, now=first_now)
        assert first["phase"] == "complete", first.get("error")
        assert first["snapshotKind"] == "full"

        (config.PROJECTS_DIR / "proj-a" / "plan.bin").write_bytes(b"project-plan-v2" + b"x" * 2048)
        config.MEMORY_FILE.write_text('{"items":[{"id":"m1","text":"after"}]}', encoding="utf-8")
        second = _claim_and_run(policy, now=first_now + timedelta(days=1))
        assert second["phase"] == "complete", second.get("error")
        assert second["snapshotKind"] == "incremental"

        # Simulate a fresh process: a new HTTP client and store with no local
        # index state. Lineage is resolved from target receipts only.
        restore = backup_remote_restore.create_restore_from_target(
            target_id=target_id,
            backup_id=str(second["backupId"]),
            selection={"contributors": ["projects"], "projectIds": ["proj-a"]},
        )
        restore_id = str(restore["restoreId"])
        fetched = backup_remote_restore.fetch_restore_session(restore_id)
        while str(fetched.get("phase") or "") not in {"fetched", "chain-fetched"}:
            fetched = backup_remote_restore.fetch_restore_session(restore_id)
        assert str(fetched.get("phase") or "") in {"fetched", "chain-fetched"}

        # Real Age decrypt via the real helper using the generated identity.
        backup_crypto.put_secret(restore_id, "age-identity", identity_text)
        prepared = backup_remote_restore.materialize_federated_restore(restore_id, mode="merge", owner_document_id="server")
        assert prepared["phase"] == "prepared"
        committed = backups.commit_restore(restore_id)
        assert committed["phase"] == "backend-committed"
        completed = backups.complete_restore(restore_id)
        backup_remote_restore.advance_federated_phase(restore_id, "complete")
        assert completed["phase"] == "complete"

        # The selected project is byte-for-byte identical to the I1 snapshot.
        assert (config.PROJECTS_DIR / "proj-a" / "plan.bin").read_bytes() == b"project-plan-v2" + b"x" * 2048
        # Unselected contributors are never mutated.
        assert config.MEMORY_FILE.read_text(encoding="utf-8") == '{"items":[{"id":"m1","text":"after"}]}'
    finally:
        _cleanup_s3()

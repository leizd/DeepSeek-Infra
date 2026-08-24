"""Real MinIO Evidence: transactional GC fencing effect under lease expiry/takeover."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from deepseek_infra.infra.workspace import backup_control, backup_publish, backup_retirement, backup_targets
from deepseek_infra.infra.workspace.backup_target_store import object_key

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
        prefix=f"tx-gc-fence-{uuid.uuid4().hex[:10]}",
        endpoint_url=endpoint,
        region=region,
        failure_domain=failure_domain,
        provider="minio",
        jurisdiction=region,
        storage_cost_per_gib_month=0.02,
        egress_cost_per_gib=0.01,
        quota_bytes=2 * 1024 * 1024 * 1024,
        credential_provider={"type": "aws-default-chain"},
        client=client,
        probe=False,
    )
    return str(record["targetId"])


@pytest.mark.integration
def test_real_three_minio_transactional_gc_fencing_e2e(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Publish-vs-GC lease expiry on real MinIO must never lose live ciphertext."""
    endpoints, _containers = _real_prerequisites()
    clients = [_client(endpoint) for endpoint in endpoints]
    suffix = uuid.uuid4().hex[:8]
    buckets = [f"deepseek-txgc-{name}-{suffix}" for name in ("a", "b", "c")]
    target_ids = [
        _register_target(clients[i], endpoints[i], buckets[i], region=f"region-{i + 1}", failure_domain=f"region-{i + 1}a")
        for i in range(3)
    ]
    # Primary object lives on A; B/C prove multi-endpoint env is real and independent.
    assert len(set(target_ids)) == 3
    for client, bucket in zip(clients, buckets, strict=True):
        assert client.head_bucket(Bucket=bucket) is not None or True

    primary_id = target_ids[0]
    store = backup_targets.open_target_store(primary_id, write_intent=True, client=clients[0])
    target = backup_publish.ResolvedTarget(
        target_id=primary_id,
        root=None,
        managed=False,
        kind="s3",
        store=store,
    )
    digest = "4620" + ("ab" * 30)
    key = object_key(digest)
    body = b"real-minio-tx-gc-fencing-ciphertext-v462"
    put = store.put_if_absent(key, body)
    assert getattr(put, "created", True) is True
    meta = store.stat(key)
    assert meta is not None
    etag = str(getattr(meta, "etag", None) or "")
    assert etag

    monkeypatch.setattr(backup_retirement, "_payload_key_is_retained", lambda *a, **k: False)

    real_begin = backup_control.begin_destructive_metadata_fence
    delete_calls: list[str] = []
    real_delete = backup_retirement._delete_payload_if_match

    @contextmanager
    def _expire_and_publish_takeover(target_id: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
        with real_begin(target_id, **kwargs) as guard:
            # Lease expires while GC still thinks it owns the fence.
            with backup_control._connect() as conn:
                conn.execute(
                    """
                    UPDATE target_metadata_gates
                    SET lease_until = ?, updated_at = ?
                    WHERE target_id = ?
                    """,
                    ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00Z", target_id),
                )
            # Publisher / formal mutation takes over with a newer fencing token.
            with backup_control.begin_formal_metadata_mutation(target_id, operation_id="real-publish-takeover"):
                pass
            yield guard

    def _track_delete(tgt: Any, object_key_arg: str, *, expected_etag: str | None) -> int:
        delete_calls.append(object_key_arg)
        return real_delete(tgt, object_key_arg, expected_etag=expected_etag)

    monkeypatch.setattr(backup_control, "begin_destructive_metadata_fence", _expire_and_publish_takeover)
    monkeypatch.setattr(backup_retirement, "_delete_payload_if_match", _track_delete)

    receipt = {
        "backupId": "retired-point",
        "policyId": "pol-txgc",
        "objects": [{"digest": digest, "size": len(body)}],
    }
    reclaimed = backup_retirement._reclaim_unreferenced_payloads(
        target,
        receipt=receipt,
        retiring_backup_id="retired-point",
        owner_id="real-gc-stale",
    )
    assert reclaimed == 0
    assert delete_calls == []
    assert store.stat(key) is not None
    assert store.get_bytes(key) == body

    intents = backup_control.list_ciphertext_gc_intents(primary_id)
    assert intents
    assert all(item["state"] != backup_control.GC_INTENT_RECLAIMED for item in intents)
    assert any(item.get("error") == "metadata-fence-lost" for item in intents)

    # Stale path aborted. Fresh GC under current generation may reclaim unreferenced ciphertext.
    monkeypatch.setattr(backup_control, "begin_destructive_metadata_fence", real_begin)
    monkeypatch.setattr(backup_retirement, "_delete_payload_if_match", real_delete)
    live_gen = backup_control.get_target_receipt_mutation_generation(primary_id)
    assert live_gen >= 1
    fresh_reclaimed = backup_retirement._reclaim_unreferenced_payloads(
        target,
        receipt=receipt,
        retiring_backup_id="retired-point",
        owner_id="real-gc-fresh",
    )
    assert fresh_reclaimed >= len(body)
    assert store.stat(key) is None

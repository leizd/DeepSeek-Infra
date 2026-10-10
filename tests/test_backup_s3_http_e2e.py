"""Real HTTP S3-compatible packed incremental restore contract."""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import zipfile
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import backup_incremental, backup_incremental_restore, backup_pack, backup_target_s3, backups
from deepseek_infra.infra.workspace.backup_target_store import MultipartUpload


def _s3_client() -> Any:
    endpoint = os.environ.get("DEEPSEEK_TEST_S3_ENDPOINT")
    assert endpoint, "real S3 endpoint is not configured"
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


def _archive(root: Path) -> bytes:
    output = io.BytesIO()
    backups._write_zip_tree(root, output)
    return output.getvalue()


def _extract_age_stub(raw: bytes, destination: Path) -> None:
    prefix = b"age-encryption.org/v1\n"
    assert raw.startswith(prefix)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(raw[len(prefix) :][::-1])) as archive:
        archive.extractall(destination)


@pytest.mark.integration
def test_real_s3_packed_incremental_restore_and_multipart_resume(
    tmp_path: Path,
    real_storage_environment: object,
) -> None:
    del real_storage_environment
    endpoint = os.environ.get("DEEPSEEK_TEST_S3_ENDPOINT")
    assert endpoint, "real S3 endpoint is not configured"
    bucket = os.environ.get("DEEPSEEK_TEST_S3_BUCKET", "deepseek-packed-e2e")
    client1 = _s3_client()
    client1.create_bucket(Bucket=bucket)
    try:
        prefix = b"age-encryption.org/v1\n"
        logical_path = "payload/local/workspace.bin"
        baseline_data = b"baseline"
        baseline_record = backup_incremental.FileRecord(
            "local", logical_path, len(baseline_data), hashlib.sha256(baseline_data).hexdigest()
        )
        full_root = tmp_path / "full"
        (full_root / logical_path).parent.mkdir(parents=True)
        (full_root / logical_path).write_bytes(baseline_data)
        (full_root / "manifest.json").write_text(
            json.dumps(
                {
                    "snapshotKind": "full",
                    "files": [
                        {
                            "contributorId": "local",
                            "path": logical_path,
                            "size": len(baseline_data),
                            "sha256": baseline_record.sha256,
                        }
                    ],
                    "snapshot": {"kind": "full", "rootDigest": backup_incremental.snapshot_root([baseline_record])},
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        full_object = prefix + _archive(full_root)[::-1]
        store1 = backup_target_s3.S3TargetStore(bucket=bucket, prefix="workspace", client=client1)
        full_digest = hashlib.sha256(full_object).hexdigest()
        store1.put_if_absent("F0.age", full_object, checksum_sha256=full_digest)

        final_data = random.Random(412).randbytes(11 * 1024 * 1024)
        final_digest = hashlib.sha256(final_data).hexdigest()
        final_record = backup_incremental.FileRecord("local", logical_path, len(final_data), final_digest)
        delta_root = tmp_path / "delta"
        writer = backup_pack.PackWriter(delta_root)
        ref = writer.append(io.BytesIO(final_data), expected_length=len(final_data), expected_sha256=final_digest)
        writer.finalize()
        (delta_root / "delta").mkdir()
        (delta_root / "delta" / "operations.json").write_text(
            json.dumps(
                {
                    "put": [
                        {
                            "contributorId": "local",
                            "path": logical_path,
                            "size": len(final_data),
                            "sha256": final_digest,
                            "storage": "whole",
                            "payloadRef": ref,
                        }
                    ],
                    "delete": [],
                    "parentRootDigest": backup_incremental.snapshot_root([baseline_record]),
                    "rootDigest": backup_incremental.snapshot_root([final_record]),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        (delta_root / "manifest.json").write_text(
            json.dumps(
                {
                    "snapshotKind": "incremental",
                    "files": [
                        {
                            "contributorId": "local",
                            "path": logical_path,
                            "size": len(final_data),
                            "sha256": final_digest,
                        }
                    ],
                    "snapshot": {
                        "kind": "incremental",
                        "format": "incremental-v5",
                        "chunkProtocol": backup_incremental.CURRENT_CDC_PROTOCOL,
                        "rootDigest": backup_incremental.snapshot_root([final_record]),
                    },
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        delta_object = prefix + _archive(delta_root)[::-1]
        delta_digest = hashlib.sha256(delta_object).hexdigest()
        upload = store1.begin_multipart("I1.age", checksum_sha256=delta_digest)
        upload.expected_size = len(delta_object)
        part_size = 5 * 1024 * 1024
        store1.upload_part(
            upload,
            1,
            delta_object[:part_size],
            checksum_sha256=hashlib.sha256(delta_object[:part_size]).hexdigest(),
        )

        # Simulate process death: reconstruct only durable upload identity and
        # discover already committed parts through a fresh HTTP client.
        client2 = _s3_client()
        store2 = backup_target_s3.S3TargetStore(bucket=bucket, prefix="workspace", client=client2)
        resumed = MultipartUpload(
            key=upload.key,
            upload_id=upload.upload_id,
            checksum_sha256=upload.checksum_sha256,
            expected_size=len(delta_object),
        )
        resumed.parts = store2.list_multipart_parts(resumed)
        assert [int(item["partNumber"]) for item in resumed.parts] == [1]
        for part_number, start in enumerate(range(part_size, len(delta_object), part_size), start=2):
            part = delta_object[start : start + part_size]
            store2.upload_part(resumed, part_number, part, checksum_sha256=hashlib.sha256(part).hexdigest())
        completed = store2.complete_multipart_if_absent(resumed)
        assert completed.size == len(delta_object)
        assert store2.get_bytes("I1.age", offset=1024, length=4096) == delta_object[1024:5120]

        restored_full = store2.get_bytes("F0.age")
        restored_delta = store2.get_bytes("I1.age")
        assert restored_full is not None and restored_delta == delta_object
        roots = [tmp_path / "restored-full", tmp_path / "restored-delta"]
        _extract_age_stub(restored_full, roots[0])
        _extract_age_stub(restored_delta, roots[1])
        output = tmp_path / "restored-workspace"
        backup_incremental_restore.materialize_chain(roots, output)
        assert (output / logical_path).read_bytes() == final_data
    finally:
        cleanup = _s3_client()
        response = cleanup.list_objects_v2(Bucket=bucket)
        objects = [{"Key": str(item["Key"])} for item in response.get("Contents") or []]
        if objects:
            cleanup.delete_objects(Bucket=bucket, Delete={"Objects": objects})
        for upload in cleanup.list_multipart_uploads(Bucket=bucket).get("Uploads") or []:
            cleanup.abort_multipart_upload(Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])
        cleanup.delete_bucket(Bucket=bucket)

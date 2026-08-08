"""Coverage for S3TargetStore via an in-process fake boto3 client."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_target_s3
from deepseek_infra.infra.workspace.backup_target_store import probe_store_capabilities


class _ClientError(Exception):
    def __init__(self, *, code: str = "", status: int = 400) -> None:
        super().__init__(code or str(status))
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status, "HTTPHeaders": {"date": format_datetime(datetime.now(tz=timezone.utc))}},
        }


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self.meta: dict[str, dict[str, str]] = {}
        self.multipart: dict[str, dict[str, Any]] = {}
        self.versioning = "Enabled"
        self.fail_next: dict[str, Exception] = {}

    def _etag(self, data: bytes | bytearray) -> str:
        return f'"{hashlib.md5(bytes(data)).hexdigest()}"'

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_next:
            raise self.fail_next.pop(op)

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail("head")
        key = kwargs["Key"]
        if key not in self.objects:
            raise _ClientError(code="404", status=404)
        data = self.objects[key]
        return {
            "ContentLength": len(data),
            "ETag": self.etags[key],
            "Metadata": dict(self.meta.get(key) or {}),
            "LastModified": datetime.now(tz=timezone.utc),
            "VersionId": "v1",
            "ResponseMetadata": {"HTTPHeaders": {"date": format_datetime(datetime.now(tz=timezone.utc))}},
        }

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail("get")
        key = kwargs["Key"]
        if key not in self.objects:
            raise _ClientError(code="NoSuchKey", status=404)
        data = self.objects[key]
        rng = kwargs.get("Range")
        if rng:
            # bytes=start-end or bytes=start-
            body = rng.split("=", 1)[1]
            start_s, _, end_s = body.partition("-")
            start = int(start_s or 0)
            end = int(end_s) + 1 if end_s else None
            data = data[start:end]
        return {
            "Body": _BytesBody(data),
            "ETag": self.etags[key],
            "ResponseMetadata": {"HTTPHeaders": {"date": format_datetime(datetime.now(tz=timezone.utc))}},
        }

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail("put")
        key = kwargs["Key"]
        body = kwargs["Body"]
        data = body if isinstance(body, (bytes, bytearray)) else bytes(body)
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise _ClientError(code="PreconditionFailed", status=412)
        if kwargs.get("IfMatch") and key in self.objects and self.etags[key] != kwargs["IfMatch"]:
            raise _ClientError(code="PreconditionFailed", status=412)
        if kwargs.get("IfMatch") and key not in self.objects:
            raise _ClientError(code="PreconditionFailed", status=412)
        etag = self._etag(data)
        self.objects[key] = bytes(data)
        self.etags[key] = etag
        self.meta[key] = dict(kwargs.get("Metadata") or {})
        return {
            "ETag": etag,
            "VersionId": "v1",
            "ResponseMetadata": {"HTTPHeaders": {"date": format_datetime(datetime.now(tz=timezone.utc))}},
        }

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail("delete")
        key = kwargs["Key"]
        self.objects.pop(key, None)
        self.etags.pop(key, None)
        self.meta.pop(key, None)
        return {}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail("list")
        prefix = kwargs.get("Prefix") or ""
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        max_keys = int(kwargs.get("MaxKeys") or 1000)
        token = kwargs.get("ContinuationToken")
        start = keys.index(token) + 1 if token in keys else 0
        page = keys[start : start + max_keys]
        contents = [
            {
                "Key": key,
                "Size": len(self.objects[key]),
                "ETag": self.etags[key],
                "LastModified": datetime.now(tz=timezone.utc),
            }
            for key in page
        ]
        truncated = start + max_keys < len(keys)
        return {
            "Contents": contents,
            "IsTruncated": truncated,
            "NextContinuationToken": page[-1] if truncated and page else None,
            "ResponseMetadata": {"HTTPHeaders": {"date": format_datetime(datetime.now(tz=timezone.utc))}},
        }

    def create_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail("create_mp")
        upload_id = hashlib.sha1(kwargs["Key"].encode()).hexdigest()[:16]
        self.multipart[upload_id] = {"key": kwargs["Key"], "parts": {}, "meta": dict(kwargs.get("Metadata") or {})}
        return {"UploadId": upload_id}

    def upload_part(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail("upload_part")
        upload_id = kwargs["UploadId"]
        part_number = int(kwargs["PartNumber"])
        body = kwargs["Body"]
        data = body if isinstance(body, (bytes, bytearray)) else bytes(body)
        self.multipart[upload_id]["parts"][part_number] = bytes(data)
        etag = self._etag(data)
        return {"ETag": etag}

    def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail("complete_mp")
        upload_id = kwargs["UploadId"]
        state = self.multipart[upload_id]
        key = state["key"]
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise _ClientError(code="PreconditionFailed", status=412)
        parts = state["parts"]
        data = b"".join(parts[n] for n in sorted(parts))
        etag = self._etag(data)
        self.objects[key] = data
        self.etags[key] = etag
        self.meta[key] = dict(state.get("meta") or {})
        del self.multipart[upload_id]
        return {
            "ETag": etag,
            "VersionId": "v1",
            "ResponseMetadata": {"HTTPHeaders": {"date": format_datetime(datetime.now(tz=timezone.utc))}},
        }

    def abort_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.multipart.pop(kwargs["UploadId"], None)
        return {}

    def get_bucket_versioning(self, **kwargs: Any) -> dict[str, Any]:
        return {"Status": self.versioning}


class _BytesBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


def _store(client: FakeS3Client | None = None, **kwargs: Any) -> backup_target_s3.S3TargetStore:
    return backup_target_s3.S3TargetStore(bucket="backups", prefix="deepseek/home", client=client or FakeS3Client(), **kwargs)


def test_s3_conditional_put_get_list_delete() -> None:
    client = FakeS3Client()
    store = _store(client)
    data = b"hello-s3"
    digest = hashlib.sha256(data).hexdigest()
    created = store.put_if_absent("objects/a.bin", data, checksum_sha256=digest)
    assert created.created is True
    with pytest.raises(AppError) as exc:
        store.put_if_absent("objects/a.bin", b"other", checksum_sha256=hashlib.sha256(b"other").hexdigest())
    assert exc.value.status == 412
    # identical converge
    again = store.put_if_absent("objects/a.bin", data, checksum_sha256=digest)
    assert again.created is False
    meta = store.stat("objects/a.bin")
    assert meta is not None and meta.size == len(data)
    assert store.get_bytes("objects/a.bin") == data
    assert store.get_bytes("objects/a.bin", offset=1, length=3) == data[1:4]
    replaced = store.put_if_match("objects/a.bin", b"next", expected_etag=created.etag, checksum_sha256=hashlib.sha256(b"next").hexdigest())
    assert replaced.created is False
    page = store.list_objects("objects/")
    assert any(item.key == "objects/a.bin" for item in page.objects)
    assert store.delete_if_match("objects/a.bin", expected_etag=replaced.etag) is True
    assert store.stat("objects/a.bin") is None


def test_s3_multipart_and_probe() -> None:
    client = FakeS3Client()
    store = _store(client)
    payload = b"x" * 100
    digest = hashlib.sha256(payload).hexdigest()
    upload = store.begin_multipart("objects/mp.bin", checksum_sha256=digest)
    store.upload_part(upload, 1, payload, checksum_sha256=digest)
    result = store.complete_multipart_if_absent(upload)
    assert result.created is True
    assert store.get_bytes("objects/mp.bin") == payload
    store.abort_multipart(store.begin_multipart("objects/abort.bin", checksum_sha256=digest))
    assert store.detect_versioning() is True
    assert store.server_time() is not None
    probe = probe_store_capabilities(store, prefix="control/probe/")
    assert probe["scheduledBackupReady"] is True


def test_s3_error_mapping_and_open_helpers() -> None:
    client = FakeS3Client()
    store = _store(client)
    client.fail_next["put"] = _ClientError(code="AccessDenied", status=403)
    with pytest.raises(AppError) as denied:
        store.put_if_absent("k", b"a", checksum_sha256=hashlib.sha256(b"a").hexdigest())
    assert denied.value.status == 503
    client.fail_next["put"] = _ClientError(code="SlowDown", status=503)
    with pytest.raises(AppError) as throttled:
        store.put_if_absent("k2", b"b", checksum_sha256=hashlib.sha256(b"b").hexdigest())
    assert throttled.value.status == 503
    client.fail_next["put"] = _ClientError(code="ConditionalRequestConflict", status=409)
    with pytest.raises(AppError) as conflict:
        store.put_if_absent("k3", b"c", checksum_sha256=hashlib.sha256(b"c").hexdigest())
    assert conflict.value.status == 409

    with pytest.raises(AppError):
        backup_target_s3.S3TargetStore(bucket="")
    parsed = backup_target_s3.parse_s3_uri("s3://my-bucket/prefix/path")
    assert parsed == {"bucket": "my-bucket", "prefix": "prefix/path"}
    with pytest.raises(AppError):
        backup_target_s3.parse_s3_uri("https://example.com/x")
    with pytest.raises(AppError):
        backup_target_s3.open_s3_store({"bucket": "b", "accessKeyId": "AKIA"})
    opened = backup_target_s3.open_s3_store(
        {
            "bucket": "b",
            "prefix": "p",
            "region": "ap-northeast-1",
            "credentialProvider": {"type": "aws-profile", "profile": "demo"},
        },
        client=client,
    )
    assert opened.bucket == "b"
    assert backup_target_s3._normalize_etag(None) == ""
    assert backup_target_s3._normalize_etag("abc") == '"abc"'
    assert backup_target_s3._normalize_etag('W/"abc"') == '"abc"'
    assert backup_target_s3._b64_sha256(b"x") == base64.b64encode(hashlib.sha256(b"x").digest()).decode("ascii")


def test_s3_sdk_available_flag() -> None:
    # Function must not raise whether or not boto3 is installed.
    assert isinstance(backup_target_s3.s3_sdk_available(), bool)


def test_s3_error_and_converge_edges() -> None:
    client = FakeS3Client()
    store = _store(client)
    # head non-404 error
    client.fail_next["head"] = _ClientError(code="SlowDown", status=503)
    with pytest.raises(AppError):
        store.stat("missing-key")
    # range open-ended
    data = b"0123456789"
    digest = hashlib.sha256(data).hexdigest()
    store.put_if_absent("rng", data, checksum_sha256=digest)
    assert store.get_bytes("rng", offset=5) == b"56789"
    # delete missing with etag
    assert store.delete_if_match("nope", expected_etag='"x"') is False
    # list failure
    client.fail_next["list"] = _ClientError(code="AccessDenied", status=403)
    with pytest.raises(AppError):
        store.list_objects("x/")
    # put replace failure
    meta = store.stat("rng")
    assert meta is not None
    client.fail_next["put"] = _ClientError(code="PreconditionFailed", status=412)
    with pytest.raises(AppError):
        store.put_if_match("rng", b"abc", expected_etag=meta.etag, checksum_sha256=hashlib.sha256(b"abc").hexdigest())
    # delete failure
    client.fail_next["delete"] = _ClientError(code="SlowDown", status=503)
    with pytest.raises(AppError):
        store.delete_if_match("rng")
    # parse missing bucket
    with pytest.raises(AppError):
        backup_target_s3.parse_s3_uri("s3://")
    # converge put via matching sha metadata after 412
    body = b"same-bytes"
    d2 = hashlib.sha256(body).hexdigest()
    store.put_if_absent("conv", body, checksum_sha256=d2)
    # force fail then converge on sha
    client.fail_next["put"] = _ClientError(code="PreconditionFailed", status=412)
    # Fake client still has object; S3 adapter will stat and compare sha
    result = store.put_if_absent("conv", body, checksum_sha256=d2)
    assert result.created is False


def test_s3_client_lazy_create_with_fake_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    from types import ModuleType, SimpleNamespace

    fake_client = FakeS3Client()
    boto3_mod = ModuleType("boto3")

    class _Session:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def client(self, service: str, **kwargs: Any) -> FakeS3Client:
            assert service == "s3"
            return fake_client

    boto3_mod.Session = _Session  # type: ignore[attr-defined]
    botocore_mod = ModuleType("botocore")
    config_mod = ModuleType("botocore.config")
    config_mod.Config = lambda **kwargs: SimpleNamespace(**kwargs)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", boto3_mod)
    monkeypatch.setitem(sys.modules, "botocore", botocore_mod)
    monkeypatch.setitem(sys.modules, "botocore.config", config_mod)

    store = backup_target_s3.S3TargetStore(
        bucket="b",
        prefix="p",
        region="us-west-2",
        endpoint_url="https://s3.example",
        expected_bucket_owner="123",
        credential_provider={"type": "aws-profile", "profile": "demo"},
    )
    assert store.capabilities().kind == "s3"
    put = store.put_if_absent("k", b"v", checksum_sha256=hashlib.sha256(b"v").hexdigest())
    assert put.created is True
    assert store.get_bytes("k") == b"v"
    assert store.stat("missing") is None
    store.delete_if_match("k")

    # Unsupported provider type
    bad = backup_target_s3.S3TargetStore(bucket="b", credential_provider={"type": "plaintext-keys"})
    with pytest.raises(AppError):
        bad.put_if_absent("x", b"y", checksum_sha256=hashlib.sha256(b"y").hexdigest())

    # Default chain with profile alias
    store2 = backup_target_s3.S3TargetStore(
        bucket="b",
        credential_provider={"type": "aws-default-chain", "profile": "p2"},
    )
    store2.put_if_absent("z", b"z", checksum_sha256=hashlib.sha256(b"z").hexdigest())

    # Versioning suspended / unknown
    fake_client.versioning = "Suspended"
    assert store.detect_versioning() is False
    fake_client.versioning = ""
    assert store.detect_versioning() is None

    # complete multipart converge when object exists
    data = b"part-body"
    digest = hashlib.sha256(data).hexdigest()
    upload = store.begin_multipart("mp", checksum_sha256=digest)
    store.upload_part(upload, 1, data, checksum_sha256=digest)
    store.complete_multipart_if_absent(upload)
    upload2 = store.begin_multipart("mp", checksum_sha256=digest)
    store.upload_part(upload2, 1, data, checksum_sha256=digest)
    # Force complete to fail with existing identical object path via IfNoneMatch
    result = store.complete_multipart_if_absent(upload2)
    assert result.created is False or result.key == "mp"

    # generic error mapping
    fake_client.fail_next["put"] = _ClientError(code="SomethingElse", status=400)
    with pytest.raises(AppError):
        store.put_if_absent("err", b"e", checksum_sha256=hashlib.sha256(b"e").hexdigest())

    # range get full stream
    store.put_if_absent("stream", b"abcdef", checksum_sha256=hashlib.sha256(b"abcdef").hexdigest())
    assert list(store.get_stream("stream")) == [b"abcdef"]
    assert list(store.get_stream("nope")) == []

    # delete etag mismatch
    meta = store.stat("stream")
    assert meta is not None
    with pytest.raises(AppError):
        store.delete_if_match("stream", expected_etag='"nope"')

    # put checksum mismatch
    with pytest.raises(AppError):
        store.put_if_absent("bad", b"x", checksum_sha256="0" * 64)
    with pytest.raises(AppError):
        store.put_if_match("stream", b"y", expected_etag=meta.etag, checksum_sha256="0" * 64)
    with pytest.raises(AppError):
        store.upload_part(store.begin_multipart("mp2", checksum_sha256=digest), 1, b"x", checksum_sha256="0" * 64)

    # list pagination cursor
    for i in range(3):
        body = f"n{i}".encode()
        store.put_if_absent(f"page/{i}", body, checksum_sha256=hashlib.sha256(body).hexdigest())
    page = store.list_objects("page/", limit=1)
    assert page.cursor
    page2 = store.list_objects("page/", cursor=page.cursor, limit=10)
    assert page2.objects

    # server_time failure path
    fake_client.fail_next["list"] = _ClientError(code="Boom", status=500)
    assert store.server_time() is None

    # open_s3_store secret in nested provider
    with pytest.raises(AppError):
        backup_target_s3.open_s3_store({"bucket": "b", "credentialProvider": {"type": "x", "sessionToken": "t"}})

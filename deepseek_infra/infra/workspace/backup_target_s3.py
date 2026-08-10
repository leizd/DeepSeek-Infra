"""S3-compatible backup target adapter (4.4.6).

Uses the official AWS SDK when available (lazy import). Conditional writes map
4.4.5 O_EXCL / CAS onto ``IfNoneMatch='*'`` and ``IfMatch=<etag>``. Credentials
never enter target JSON — only a credential provider descriptor is stored.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, BinaryIO, NoReturn
from urllib.parse import urlparse

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace.backup_target_store import (
    ListPage,
    MultipartUpload,
    ObjectMeta,
    PutResult,
    TargetCapabilities,
    _read_source,
    _sha256_bytes,
)


def s3_sdk_available() -> bool:
    try:
        import boto3  # noqa: F401
    except ImportError:  # pragma: no cover - optional dependency absent in CI
        return False
    return True


def _normalize_etag(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip()
    if text.startswith("W/"):
        text = text[2:].strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text
    return f'"{text}"'


def _raise_from_client_error(exc: Exception, *, action: str) -> NoReturn:
    response = getattr(exc, "response", None) or {}
    error = response.get("Error") if isinstance(response, dict) else {}
    code = str((error or {}).get("Code") or "")
    status = int((response.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 0) if isinstance(response, dict) else 0
    if code in {"PreconditionFailed", "412"} or status == 412:
        raise AppError(f"conditional-{action}-failed: precondition", code=ErrorCode.INVALID_REQUEST, status=412) from exc
    if code in {"ConditionalRequestConflict", "409"} or status == 409:
        raise AppError(f"conditional-{action}-conflict", code=ErrorCode.INVALID_REQUEST, status=409) from exc
    if status in {401, 403} or code in {"AccessDenied", "InvalidAccessKeyId", "ExpiredToken", "TokenRefreshRequired"}:
        raise AppError(f"blocked-target-unavailable: credential error ({code or status})", code=ErrorCode.INVALID_REQUEST, status=503) from exc
    if status in {429, 500, 502, 503, 504} or code in {"SlowDown", "ServiceUnavailable", "InternalError", "RequestTimeout"}:
        raise AppError(f"blocked-target-unavailable: {code or status}", code=ErrorCode.INVALID_REQUEST, status=503) from exc
    raise AppError(f"s3 {action} failed: {code or exc}", code=ErrorCode.INVALID_REQUEST, status=503) from exc


class S3TargetStore:
    """Object-store adapter for AWS S3 and compatible endpoints."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        region: str | None = None,
        endpoint_url: str | None = None,
        expected_bucket_owner: str | None = None,
        credential_provider: dict[str, Any] | None = None,
        client: Any | None = None,
    ) -> None:
        if not bucket:
            raise AppError("S3 bucket is required", code=ErrorCode.INVALID_PAYLOAD)
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.region = region
        self.endpoint_url = endpoint_url
        self.expected_bucket_owner = expected_bucket_owner
        self.credential_provider = dict(credential_provider or {"type": "aws-default-chain"})
        self._client = client
        self._caps = TargetCapabilities(
            conditional_create=True,
            conditional_replace=True,
            range_get=True,
            multipart_upload=True,
            multipart_checksum=True,
            list_pagination=True,
            delete=True,
            server_date=True,
            versioning=None,
            kind="s3",
        )

    def _full_key(self, key: str) -> str:
        key = key.lstrip("/")
        if not self.prefix:
            return key
        return f"{self.prefix}/{key}"

    def _client_or_create(self) -> Any:
        if self._client is not None:
            return self._client
        if not s3_sdk_available():
            raise AppError("s3TargetAvailable=false: boto3 is not installed", code=ErrorCode.INVALID_REQUEST, status=503)
        import boto3
        from botocore.config import Config

        session_kwargs: dict[str, Any] = {}
        provider = self.credential_provider
        provider_type = str(provider.get("type") or "aws-default-chain")
        if provider_type == "aws-profile":
            session_kwargs["profile_name"] = str(provider.get("profile") or "") or None
        elif provider_type not in {"aws-default-chain", "aws-profile", "environment", "instance-role", "workload-identity"}:
            raise AppError(f"unsupported credential provider type: {provider_type}", code=ErrorCode.INVALID_PAYLOAD)
        if provider_type == "aws-default-chain" and provider.get("profile"):
            session_kwargs["profile_name"] = str(provider.get("profile"))
        session = boto3.Session(**{k: v for k, v in session_kwargs.items() if v})
        self._client = session.client(
            "s3",
            region_name=self.region,
            endpoint_url=self.endpoint_url,
            config=Config(retries={"max_attempts": 8, "mode": "standard"}),
        )
        return self._client

    def _owner_args(self) -> dict[str, Any]:
        if self.expected_bucket_owner:
            return {"ExpectedBucketOwner": self.expected_bucket_owner}
        return {}

    def capabilities(self) -> TargetCapabilities:
        return self._caps

    def stat(self, key: str) -> ObjectMeta | None:
        client = self._client_or_create()
        full = self._full_key(key)
        try:
            response = client.head_object(Bucket=self.bucket, Key=full, **self._owner_args())
        except Exception as exc:  # noqa: BLE001
            error = getattr(exc, "response", {}) or {}
            status = int((error.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 0)
            code = str((error.get("Error") or {}).get("Code") or "")
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                return None
            _raise_from_client_error(exc, action="stat")
            return None  # pragma: no cover
        checksum = None
        metadata = response.get("Metadata") or {}
        if isinstance(metadata, dict):
            checksum = metadata.get("sha256") or metadata.get("checksum-sha256")
        return ObjectMeta(
            key=key,
            size=int(response.get("ContentLength") or 0),
            etag=_normalize_etag(response.get("ETag")),
            sha256=checksum,
            last_modified=response.get("LastModified").astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if response.get("LastModified") else None,
            version_id=response.get("VersionId"),
        )

    def get_bytes(self, key: str, *, offset: int = 0, length: int | None = None) -> bytes | None:
        client = self._client_or_create()
        full = self._full_key(key)
        args: dict[str, Any] = {"Bucket": self.bucket, "Key": full, **self._owner_args()}
        if offset or length is not None:
            if length is None:
                args["Range"] = f"bytes={offset}-"
            else:
                end = offset + length - 1
                args["Range"] = f"bytes={offset}-{end}"
        try:
            response = client.get_object(**args)
        except Exception as exc:  # noqa: BLE001
            error = getattr(exc, "response", {}) or {}
            status = int((error.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 0)
            code = str((error.get("Error") or {}).get("Code") or "")
            if status == 404 or code in {"NoSuchKey", "404", "NotFound"}:
                return None
            _raise_from_client_error(exc, action="get")
            return None  # pragma: no cover
        body = response["Body"].read()
        return body

    def get_stream(self, key: str, *, offset: int = 0):
        data = self.get_bytes(key, offset=offset)
        if data is None:
            return iter(())
        return iter((data,))

    def put_if_absent(
        self,
        key: str,
        source: BinaryIO | bytes,
        *,
        checksum_sha256: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> PutResult:
        data = _read_source(source)
        digest = _sha256_bytes(data)
        if checksum_sha256 is not None and digest != checksum_sha256:
            raise AppError("object checksum mismatch before put", code=ErrorCode.INTERNAL, status=500)
        client = self._client_or_create()
        full = self._full_key(key)
        args: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": full,
            "Body": data,
            "ContentType": content_type,
            "IfNoneMatch": "*",
            "Metadata": {"sha256": digest},
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": _b64_sha256(data),
            **self._owner_args(),
        }
        try:
            response = client.put_object(**args)
        except Exception as exc:  # noqa: BLE001
            # Converge when identical object already exists.
            existing = self.stat(key)
            if existing is not None and existing.sha256 == digest:
                return PutResult(key=key, etag=existing.etag, size=existing.size, created=False, version_id=existing.version_id)
            if existing is not None and existing.size == len(data):  # pragma: no cover - sha metadata missing fallback
                body = self.get_bytes(key)
                if body == data:
                    return PutResult(key=key, etag=existing.etag, size=existing.size, created=False, version_id=existing.version_id)
            _raise_from_client_error(exc, action="create")
            raise  # pragma: no cover
        return PutResult(
            key=key,
            etag=_normalize_etag(response.get("ETag")),
            size=len(data),
            created=True,
            version_id=response.get("VersionId"),
            server_date=_header_date(response),
        )

    def put_if_match(
        self,
        key: str,
        source: BinaryIO | bytes,
        *,
        expected_etag: str,
        checksum_sha256: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> PutResult:
        data = _read_source(source)
        digest = _sha256_bytes(data)
        if checksum_sha256 is not None and digest != checksum_sha256:
            raise AppError("object checksum mismatch before put", code=ErrorCode.INTERNAL, status=500)
        client = self._client_or_create()
        full = self._full_key(key)
        args: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": full,
            "Body": data,
            "ContentType": content_type,
            "IfMatch": _normalize_etag(expected_etag),
            "Metadata": {"sha256": digest},
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": _b64_sha256(data),
            **self._owner_args(),
        }
        try:
            response = client.put_object(**args)
        except Exception as exc:  # noqa: BLE001
            _raise_from_client_error(exc, action="replace")
            raise  # pragma: no cover
        return PutResult(
            key=key,
            etag=_normalize_etag(response.get("ETag")),
            size=len(data),
            created=False,
            version_id=response.get("VersionId"),
            server_date=_header_date(response),
        )

    def delete_if_match(self, key: str, *, expected_etag: str | None = None) -> bool:
        client = self._client_or_create()
        full = self._full_key(key)
        if expected_etag is not None:
            current = self.stat(key)
            if current is None:
                return False
            if current.etag != _normalize_etag(expected_etag):
                raise AppError("conditional-delete-failed: etag mismatch", code=ErrorCode.INVALID_REQUEST, status=412)
        try:
            client.delete_object(Bucket=self.bucket, Key=full, **self._owner_args())
        except Exception as exc:  # noqa: BLE001
            _raise_from_client_error(exc, action="delete")
            raise  # pragma: no cover
        return True

    def list_objects(self, prefix: str, *, cursor: str | None = None, limit: int = 1000) -> ListPage:
        client = self._client_or_create()
        full_prefix = self._full_key(prefix)
        args: dict[str, Any] = {
            "Bucket": self.bucket,
            "Prefix": full_prefix,
            "MaxKeys": max(1, min(limit, 1000)),
            **self._owner_args(),
        }
        if cursor:
            args["ContinuationToken"] = cursor
        try:
            response = client.list_objects_v2(**args)
        except Exception as exc:  # noqa: BLE001
            _raise_from_client_error(exc, action="list")
            raise  # pragma: no cover
        objects: list[ObjectMeta] = []
        strip = f"{self.prefix}/" if self.prefix else ""
        for item in response.get("Contents") or []:
            full_key = str(item.get("Key") or "")
            rel = full_key[len(strip) :] if strip and full_key.startswith(strip) else full_key
            objects.append(
                ObjectMeta(
                    key=rel,
                    size=int(item.get("Size") or 0),
                    etag=_normalize_etag(item.get("ETag")),
                    last_modified=item.get("LastModified").astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if item.get("LastModified") else None,
                )
            )
        next_cursor = response.get("NextContinuationToken") if response.get("IsTruncated") else None
        return ListPage(objects=tuple(objects), cursor=next_cursor)

    def begin_multipart(self, key: str, *, checksum_sha256: str) -> MultipartUpload:
        client = self._client_or_create()
        full = self._full_key(key)
        try:
            response = client.create_multipart_upload(
                Bucket=self.bucket,
                Key=full,
                Metadata={"sha256": checksum_sha256},
                ChecksumAlgorithm="SHA256",
                **self._owner_args(),
            )
        except Exception as exc:  # noqa: BLE001
            _raise_from_client_error(exc, action="begin-multipart")
        return MultipartUpload(key=key, upload_id=str(response["UploadId"]), checksum_sha256=checksum_sha256)

    def upload_part(self, upload: MultipartUpload, part_number: int, data: bytes, *, checksum_sha256: str | None = None) -> dict[str, Any]:
        if checksum_sha256 is not None and _sha256_bytes(data) != checksum_sha256:
            raise AppError("multipart part checksum mismatch", code=ErrorCode.INTERNAL, status=500)
        client = self._client_or_create()
        full = self._full_key(upload.key)
        try:
            response = client.upload_part(
                Bucket=self.bucket,
                Key=full,
                UploadId=upload.upload_id,
                PartNumber=part_number,
                Body=data,
                ChecksumAlgorithm="SHA256",
                ChecksumSHA256=_b64_sha256(data),
                **self._owner_args(),
            )
        except Exception as exc:  # noqa: BLE001
            _raise_from_client_error(exc, action="upload-part")
        part = {"partNumber": part_number, "etag": _normalize_etag(response.get("ETag")), "size": len(data), "checksumSHA256": _b64_sha256(data)}
        upload.parts = [item for item in upload.parts if int(item["partNumber"]) != part_number]
        upload.parts.append(part)
        upload.parts.sort(key=lambda item: int(item["partNumber"]))
        return part

    def list_multipart_parts(self, upload: MultipartUpload) -> list[dict[str, Any]]:
        client = self._client_or_create()
        full = self._full_key(upload.key)
        marker: int | None = None
        parts: list[dict[str, Any]] = []
        while True:
            args: dict[str, Any] = {
                "Bucket": self.bucket,
                "Key": full,
                "UploadId": upload.upload_id,
                **self._owner_args(),
            }
            if marker is not None:
                args["PartNumberMarker"] = marker
            try:
                response = client.list_parts(**args)
            except Exception as exc:  # noqa: BLE001
                _raise_from_client_error(exc, action="list-parts")
            for item in response.get("Parts") or []:
                parts.append(
                    {
                        "partNumber": int(item["PartNumber"]),
                        "etag": _normalize_etag(item.get("ETag")),
                        "size": int(item.get("Size") or 0),
                        **({"checksumSHA256": item["ChecksumSHA256"]} if item.get("ChecksumSHA256") else {}),
                    }
                )
            if not response.get("IsTruncated"):
                break
            marker = int(response.get("NextPartNumberMarker") or 0)
        return parts

    def complete_multipart_if_absent(self, upload: MultipartUpload) -> PutResult:
        client = self._client_or_create()
        full = self._full_key(upload.key)
        parts = [
            {
                "ETag": item["etag"],
                "PartNumber": int(item["partNumber"]),
                **({"ChecksumSHA256": item["checksumSHA256"]} if item.get("checksumSHA256") else {}),
            }
            for item in sorted(upload.parts, key=lambda item: int(item["partNumber"]))
        ]
        args: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": full,
            "UploadId": upload.upload_id,
            "MultipartUpload": {"Parts": parts},
            "IfNoneMatch": "*",
            **self._owner_args(),
        }
        try:
            response = client.complete_multipart_upload(**args)
        except Exception as exc:  # noqa: BLE001
            existing = self.stat(upload.key)
            expected_size = upload.expected_size if upload.expected_size is not None else sum(int(item.get("size") or 0) for item in upload.parts)
            if existing is not None and existing.sha256 == upload.checksum_sha256 and existing.size == expected_size:
                return PutResult(key=upload.key, etag=existing.etag, size=existing.size, created=False, version_id=existing.version_id)
            if existing is not None:
                raise AppError("object-integrity-unproven", code=ErrorCode.INVALID_REQUEST, status=409) from exc
            _raise_from_client_error(exc, action="complete-multipart")
        size = sum(int(item.get("size") or 0) for item in upload.parts)
        return PutResult(
            key=upload.key,
            etag=_normalize_etag(response.get("ETag")),
            size=size,
            created=True,
            version_id=response.get("VersionId"),
            server_date=_header_date(response),
        )

    def abort_multipart(self, upload: MultipartUpload) -> None:
        client = self._client_or_create()
        full = self._full_key(upload.key)
        try:
            client.abort_multipart_upload(Bucket=self.bucket, Key=full, UploadId=upload.upload_id, **self._owner_args())
        except Exception:  # pragma: no cover - best-effort abort
            pass

    def server_time(self) -> datetime | None:
        client = self._client_or_create()
        try:
            response = client.list_objects_v2(Bucket=self.bucket, MaxKeys=1, **self._owner_args())
        except Exception:
            return None
        meta = response.get("ResponseMetadata") or {}
        headers = meta.get("HTTPHeaders") or {}
        date_header = headers.get("date") or headers.get("Date")
        if not date_header:
            return None
        try:
            from email.utils import parsedate_to_datetime

            return parsedate_to_datetime(date_header).astimezone(timezone.utc)
        except (TypeError, ValueError, IndexError):  # pragma: no cover
            return None

    def detect_versioning(self) -> bool | None:
        client = self._client_or_create()
        try:
            response = client.get_bucket_versioning(Bucket=self.bucket, **self._owner_args())
        except Exception:  # pragma: no cover - endpoint may lack versioning API
            return None
        status = str(response.get("Status") or "")
        versioning: bool | None
        if status == "Enabled":
            versioning = True
        elif status == "Suspended":
            versioning = False
        else:
            versioning = None
        self._caps = TargetCapabilities(
            conditional_create=True,
            conditional_replace=True,
            range_get=True,
            multipart_upload=True,
            multipart_checksum=True,
            list_pagination=True,
            delete=True,
            server_date=True,
            versioning=versioning if versioning is not None else False,
            kind="s3",
        )
        return versioning


def _b64_sha256(data: bytes) -> str:
    import base64

    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def _header_date(response: dict[str, Any]) -> str | None:
    meta = response.get("ResponseMetadata") or {}
    headers = meta.get("HTTPHeaders") or {}
    value = headers.get("date") or headers.get("Date")
    return str(value) if value else None


def open_s3_store(record: dict[str, Any], *, client: Any | None = None) -> S3TargetStore:
    provider = record.get("credentialProvider") if isinstance(record.get("credentialProvider"), dict) else {"type": "aws-default-chain"}
    # Reject any accidental secret material in the registry record.
    forbidden = ("accessKey", "accessKeyId", "secretAccessKey", "secret", "sessionToken", "password")
    blob = str(record)
    lowered = blob.lower()
    for name in forbidden:
        if name.lower() in lowered and record.get(name) not in (None, ""):
            raise AppError("cloud credentials must not be stored in target registry", code=ErrorCode.INVALID_PAYLOAD)
    if isinstance(provider, dict):
        for name in forbidden:
            if provider.get(name) not in (None, ""):
                raise AppError("cloud credentials must not be stored in credentialProvider", code=ErrorCode.INVALID_PAYLOAD)
    return S3TargetStore(
        bucket=str(record.get("bucket") or ""),
        prefix=str(record.get("prefix") or ""),
        region=str(record.get("region") or "") or None,
        endpoint_url=str(record.get("endpointUrl") or "") or None,
        expected_bucket_owner=str(record.get("expectedBucketOwner") or "") or None,
        credential_provider=provider,
        client=client,
    )


def parse_s3_uri(uri: str) -> dict[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise AppError("S3 URI must start with s3://", code=ErrorCode.INVALID_PAYLOAD)
    bucket = parsed.netloc
    prefix = parsed.path.lstrip("/")
    if not bucket:
        raise AppError("S3 URI is missing bucket", code=ErrorCode.INVALID_PAYLOAD)
    return {"bucket": bucket, "prefix": prefix}

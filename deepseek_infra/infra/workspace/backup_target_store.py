"""Backend-neutral backup target store protocol (4.4.6).

Filesystem targets keep the 4.4.5 O_EXCL / atomic-rename semantics via
:class:`FilesystemTargetStore`. S3-compatible targets implement the same
contract with conditional writes (``If-None-Match: *`` / ``If-Match``) and
multipart upload. Callers must never assume a local ``Path`` for remote
targets — only the store API is portable.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Literal, Protocol, runtime_checkable

from deepseek_infra.core.errors import AppError, ErrorCode

PutMode = Literal["if_absent", "if_match", "overwrite"]


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stable_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def object_key(digest: str) -> str:
    return f"objects/sha256/{digest[:2]}/{digest}.age"


def receipt_key(backup_id: str) -> str:
    return f"receipts/{backup_id}.json"


def transaction_key(run_id: str) -> str:
    return f"transactions/{run_id}.json"


def commit_slot_digest(schedule_slot: str) -> str:
    return hashlib.sha256(schedule_slot.encode("utf-8")).hexdigest()


def commit_marker_key(policy_id: str, schedule_slot: str, *, full_digest: bool = True) -> str:
    digest = commit_slot_digest(schedule_slot)
    name = digest if full_digest else digest[:16]
    return f"commits/{policy_id}/{name}.json"


def commit_marker_keys(policy_id: str, schedule_slot: str) -> tuple[str, ...]:
    """Prefer full SHA-256 key; keep 4.4.5 truncated key for read compatibility."""
    digest = commit_slot_digest(schedule_slot)
    return (f"commits/{policy_id}/{digest}.json", f"commits/{policy_id}/{digest[:16]}.json")


def writer_lease_key() -> str:
    return "control/writer.json"


def identity_key() -> str:
    return "control/identity.json"


def head_key() -> str:
    return "control/head.json"


def event_key(entry_hash: str) -> str:
    return f"events/{entry_hash[:2]}/{entry_hash}.json"


def catalog_head_key() -> str:
    return "catalog/head.json"


def catalog_snapshot_key(snapshot_hash: str) -> str:
    return f"catalog/snapshots/{snapshot_hash}.json"


def restore_hold_key(restore_id: str) -> str:
    return f"holds/restore/{restore_id}.json"


@dataclass(frozen=True, slots=True)
class TargetCapabilities:
    conditional_create: bool = False
    conditional_replace: bool = False
    range_get: bool = False
    multipart_upload: bool = False
    multipart_checksum: bool = False
    list_pagination: bool = False
    delete: bool = False
    server_date: bool = False
    versioning: bool | None = None
    kind: str = "filesystem"

    @property
    def scheduled_backup_ready(self) -> bool:
        return self.conditional_create and self.conditional_replace

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "conditionalCreate": self.conditional_create,
            "conditionalReplace": self.conditional_replace,
            "rangeGet": self.range_get,
            "multipartUpload": self.multipart_upload,
            "multipartChecksum": self.multipart_checksum,
            "listPagination": self.list_pagination,
            "delete": self.delete,
            "serverDate": self.server_date,
            "versioning": self.versioning,
            "scheduledBackupReady": self.scheduled_backup_ready,
        }


@dataclass(frozen=True, slots=True)
class ObjectMeta:
    key: str
    size: int
    etag: str
    sha256: str | None = None
    last_modified: str | None = None
    version_id: str | None = None


@dataclass(frozen=True, slots=True)
class PutResult:
    key: str
    etag: str
    size: int
    created: bool
    version_id: str | None = None
    server_date: str | None = None


@dataclass(frozen=True, slots=True)
class ListPage:
    objects: tuple[ObjectMeta, ...]
    cursor: str | None = None


@dataclass
class MultipartUpload:
    key: str
    upload_id: str
    checksum_sha256: str
    parts: list[dict[str, Any]] = field(default_factory=list)


@runtime_checkable
class BackupTargetStore(Protocol):  # pragma: no cover - structural interface only
    def capabilities(self) -> TargetCapabilities: ...

    def stat(self, key: str) -> ObjectMeta | None: ...

    def get_bytes(self, key: str, *, offset: int = 0, length: int | None = None) -> bytes | None: ...

    def get_stream(self, key: str, *, offset: int = 0) -> Iterator[bytes]: ...

    def put_if_absent(
        self,
        key: str,
        source: BinaryIO | bytes,
        *,
        checksum_sha256: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> PutResult: ...

    def put_if_match(
        self,
        key: str,
        source: BinaryIO | bytes,
        *,
        expected_etag: str,
        checksum_sha256: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> PutResult: ...

    def delete_if_match(self, key: str, *, expected_etag: str | None = None) -> bool: ...

    def list_objects(self, prefix: str, *, cursor: str | None = None, limit: int = 1000) -> ListPage: ...

    def begin_multipart(self, key: str, *, checksum_sha256: str) -> MultipartUpload: ...

    def upload_part(self, upload: MultipartUpload, part_number: int, data: bytes, *, checksum_sha256: str | None = None) -> dict[str, Any]: ...

    def complete_multipart_if_absent(self, upload: MultipartUpload) -> PutResult: ...

    def abort_multipart(self, upload: MultipartUpload) -> None: ...

    def server_time(self) -> datetime | None: ...


def _read_source(source: BinaryIO | bytes) -> bytes:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source)
    return source.read()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _etag_for_bytes(data: bytes) -> str:
    return f'"{_sha256_bytes(data)}"'


class FilesystemTargetStore:
    """Local directory store preserving 4.4.5 exclusive-create semantics."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._caps = TargetCapabilities(
            conditional_create=True,
            conditional_replace=True,
            range_get=True,
            multipart_upload=True,
            multipart_checksum=True,
            list_pagination=True,
            delete=True,
            server_date=True,
            versioning=False,
            kind="filesystem",
        )
        self._multipart: dict[str, dict[str, Any]] = {}

    def capabilities(self) -> TargetCapabilities:
        return self._caps

    def _path(self, key: str) -> Path:
        if key.startswith("/") or ".." in key.split("/"):
            raise AppError("invalid object key", code=ErrorCode.INVALID_REQUEST, status=400)
        return self.root.joinpath(*key.split("/"))

    def _meta_for(self, key: str, path: Path) -> ObjectMeta:
        data = path.read_bytes()
        return ObjectMeta(key=key, size=len(data), etag=_etag_for_bytes(data), sha256=_sha256_bytes(data))

    def stat(self, key: str) -> ObjectMeta | None:
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            return self._meta_for(key, path)
        except OSError:  # pragma: no cover - filesystem race
            return None

    def get_bytes(self, key: str, *, offset: int = 0, length: int | None = None) -> bytes | None:
        path = self._path(key)
        if not path.is_file():
            return None
        with path.open("rb") as handle:
            if offset:
                handle.seek(offset)
            if length is None:
                return handle.read()
            return handle.read(length)

    def get_stream(self, key: str, *, offset: int = 0) -> Iterator[bytes]:
        path = self._path(key)
        if not path.is_file():
            return iter(())
        def _gen() -> Iterator[bytes]:
            with path.open("rb") as handle:
                if offset:
                    handle.seek(offset)
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        return _gen()

    def _write_exclusive(self, path: Path, data: bytes) -> PutResult:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as exc:
            raise AppError("conditional-create-failed: object already exists", code=ErrorCode.INVALID_REQUEST, status=412) from exc
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return PutResult(key=str(path.relative_to(self.root)).replace("\\", "/"), etag=_etag_for_bytes(data), size=len(data), created=True, server_date=_utc_iso())

    def put_if_absent(
        self,
        key: str,
        source: BinaryIO | bytes,
        *,
        checksum_sha256: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> PutResult:
        del content_type
        data = _read_source(source)
        if checksum_sha256 is not None and _sha256_bytes(data) != checksum_sha256:
            raise AppError("object checksum mismatch before put", code=ErrorCode.INTERNAL, status=500)
        path = self._path(key)
        if path.is_file():
            existing = path.read_bytes()
            if existing == data:
                return PutResult(key=key, etag=_etag_for_bytes(existing), size=len(existing), created=False, server_date=_utc_iso())
            raise AppError("conditional-create-failed: object already exists", code=ErrorCode.INVALID_REQUEST, status=412)
        result = self._write_exclusive(path, data)
        return PutResult(key=key, etag=result.etag, size=result.size, created=True, server_date=result.server_date)

    def put_if_match(
        self,
        key: str,
        source: BinaryIO | bytes,
        *,
        expected_etag: str,
        checksum_sha256: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> PutResult:
        del content_type
        data = _read_source(source)
        if checksum_sha256 is not None and _sha256_bytes(data) != checksum_sha256:
            raise AppError("object checksum mismatch before put", code=ErrorCode.INTERNAL, status=500)
        path = self._path(key)
        current = self.stat(key)
        if current is None or current.etag != expected_etag:
            raise AppError("conditional-replace-failed: etag mismatch", code=ErrorCode.INVALID_REQUEST, status=412)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Re-check etag immediately before replace.
        current = self.stat(key)
        if current is None or current.etag != expected_etag:
            tmp.unlink(missing_ok=True)
            raise AppError("conditional-replace-failed: etag mismatch", code=ErrorCode.INVALID_REQUEST, status=412)
        os.replace(tmp, path)
        return PutResult(key=key, etag=_etag_for_bytes(data), size=len(data), created=False, server_date=_utc_iso())

    def delete_if_match(self, key: str, *, expected_etag: str | None = None) -> bool:
        path = self._path(key)
        if not path.is_file():
            return False
        if expected_etag is not None:
            current = self.stat(key)
            if current is None or current.etag != expected_etag:
                raise AppError("conditional-delete-failed: etag mismatch", code=ErrorCode.INVALID_REQUEST, status=412)
        path.unlink()
        return True

    def list_objects(self, prefix: str, *, cursor: str | None = None, limit: int = 1000) -> ListPage:
        base = self.root
        if not base.is_dir():
            return ListPage(objects=())
        matches: list[ObjectMeta] = []
        prefix_path = prefix.replace("\\", "/").rstrip("/")
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(base).as_posix()
            if prefix_path and not (rel == prefix_path or rel.startswith(prefix_path + "/")):
                continue
            matches.append(self._meta_for(rel, path))
        start = 0
        if cursor:
            for index, item in enumerate(matches):
                if item.key == cursor:
                    start = index + 1
                    break
        page = matches[start : start + limit]
        next_cursor = page[-1].key if len(page) == limit and start + limit < len(matches) else None
        return ListPage(objects=tuple(page), cursor=next_cursor)

    def begin_multipart(self, key: str, *, checksum_sha256: str) -> MultipartUpload:
        upload_id = uuid.uuid4().hex
        staging = self.root / ".multipart" / upload_id
        staging.mkdir(parents=True, exist_ok=True)
        meta = {"key": key, "checksum_sha256": checksum_sha256, "parts": []}
        (staging / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        self._multipart[upload_id] = meta
        return MultipartUpload(key=key, upload_id=upload_id, checksum_sha256=checksum_sha256)

    def upload_part(self, upload: MultipartUpload, part_number: int, data: bytes, *, checksum_sha256: str | None = None) -> dict[str, Any]:
        if checksum_sha256 is not None and _sha256_bytes(data) != checksum_sha256:
            raise AppError("multipart part checksum mismatch", code=ErrorCode.INTERNAL, status=500)
        staging = self.root / ".multipart" / upload.upload_id
        part_path = staging / f"part-{part_number:05d}"
        part_path.write_bytes(data)
        etag = _etag_for_bytes(data)
        part = {"partNumber": part_number, "etag": etag, "size": len(data)}
        upload.parts = [item for item in upload.parts if int(item["partNumber"]) != part_number]
        upload.parts.append(part)
        upload.parts.sort(key=lambda item: int(item["partNumber"]))
        return part

    def complete_multipart_if_absent(self, upload: MultipartUpload) -> PutResult:
        staging = self.root / ".multipart" / upload.upload_id
        parts = sorted(upload.parts, key=lambda item: int(item["partNumber"]))
        buffer = io.BytesIO()
        for part in parts:
            part_path = staging / f"part-{int(part['partNumber']):05d}"
            buffer.write(part_path.read_bytes())
        data = buffer.getvalue()
        if _sha256_bytes(data) != upload.checksum_sha256:
            raise AppError("multipart object checksum mismatch", code=ErrorCode.INTERNAL, status=500)
        result = self.put_if_absent(upload.key, data, checksum_sha256=upload.checksum_sha256)
        shutil.rmtree(staging, ignore_errors=True)
        self._multipart.pop(upload.upload_id, None)
        return result

    def abort_multipart(self, upload: MultipartUpload) -> None:
        staging = self.root / ".multipart" / upload.upload_id
        shutil.rmtree(staging, ignore_errors=True)
        self._multipart.pop(upload.upload_id, None)

    def server_time(self) -> datetime | None:
        return datetime.now(tz=timezone.utc)


def put_json_if_absent(store: BackupTargetStore, key: str, payload: dict[str, Any]) -> PutResult:
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return store.put_if_absent(key, data, checksum_sha256=_sha256_bytes(data), content_type="application/json")


def put_json_if_match(store: BackupTargetStore, key: str, payload: dict[str, Any], *, expected_etag: str) -> PutResult:
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return store.put_if_match(key, data, expected_etag=expected_etag, checksum_sha256=_sha256_bytes(data), content_type="application/json")


def read_json(store: BackupTargetStore, key: str) -> dict[str, Any] | None:
    raw = store.get_bytes(key)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def probe_store_capabilities(store: BackupTargetStore, *, prefix: str = "control/probe/") -> dict[str, Any]:
    """Run harmless canaries; scheduled backup requires conditional create+replace."""
    results: dict[str, str] = {}
    nonce = uuid.uuid4().hex
    key = f"{prefix}{nonce}.bin"
    payload = b"deepseek-infra-capability-probe-v1\n"
    replace_payload = b"deepseek-infra-capability-probe-v1-replaced\n"
    try:
        created = store.put_if_absent(key, payload, checksum_sha256=_sha256_bytes(payload))
        results["conditional-create"] = "PASS"
    except AppError:
        results["conditional-create"] = "FAIL"
        created = None
    except Exception as exc:  # noqa: BLE001 - probe must never crash callers
        results["conditional-create"] = f"FAIL:{type(exc).__name__}"
        created = None

    if created is not None:
        try:
            store.put_if_match(key, replace_payload, expected_etag=created.etag, checksum_sha256=_sha256_bytes(replace_payload))
            results["conditional-replace"] = "PASS"
        except Exception as exc:  # noqa: BLE001
            results["conditional-replace"] = f"FAIL:{type(exc).__name__}"
        try:
            ranged = store.get_bytes(key, offset=0, length=8)
            results["range-get"] = "PASS" if ranged is not None else "FAIL"
        except Exception as exc:  # noqa: BLE001
            results["range-get"] = f"FAIL:{type(exc).__name__}"
        try:
            page = store.list_objects(prefix)
            results["list-pagination"] = "PASS" if isinstance(page.objects, tuple) else "FAIL"
        except Exception as exc:  # noqa: BLE001
            results["list-pagination"] = f"FAIL:{type(exc).__name__}"
        try:
            mp_key = f"{prefix}{nonce}.mp.bin"
            upload = store.begin_multipart(mp_key, checksum_sha256=_sha256_bytes(payload))
            store.upload_part(upload, 1, payload, checksum_sha256=_sha256_bytes(payload))
            store.complete_multipart_if_absent(upload)
            results["multipart-upload"] = "PASS"
            results["multipart-checksum"] = "PASS"
            store.delete_if_match(mp_key)
        except Exception as exc:  # noqa: BLE001
            results["multipart-upload"] = f"FAIL:{type(exc).__name__}"
            results["multipart-checksum"] = results.get("multipart-checksum", f"FAIL:{type(exc).__name__}")
        try:
            store.delete_if_match(key)
            results["delete"] = "PASS"
        except Exception as exc:  # noqa: BLE001
            results["delete"] = f"FAIL:{type(exc).__name__}"
    else:
        for name in ("conditional-replace", "range-get", "list-pagination", "multipart-upload", "multipart-checksum", "delete"):
            results.setdefault(name, "SKIP")

    try:
        server = store.server_time()
        results["server-date"] = "PASS" if server is not None else "FAIL"
    except Exception as exc:  # noqa: BLE001
        results["server-date"] = f"FAIL:{type(exc).__name__}"

    caps = store.capabilities()
    conditional_ok = results.get("conditional-create") == "PASS" and results.get("conditional-replace") == "PASS"
    status = "READY" if conditional_ok else "unsupported-conditional-target"
    return {
        "status": status,
        "scheduledBackupReady": conditional_ok,
        "results": results,
        "capabilities": caps.as_dict(),
        "probedAt": _utc_iso(),
    }


def open_filesystem_store(root: Path) -> FilesystemTargetStore:
    root.mkdir(parents=True, exist_ok=True)
    return FilesystemTargetStore(root)


class MemoryTargetStore:
    """In-process store for tests: enforces conditional create/replace like S3."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._etags: dict[str, str] = {}
        self._multipart: dict[str, dict[str, Any]] = {}
        self._fail_on: dict[str, Exception] = {}
        self._caps = TargetCapabilities(
            conditional_create=True,
            conditional_replace=True,
            range_get=True,
            multipart_upload=True,
            multipart_checksum=True,
            list_pagination=True,
            delete=True,
            server_date=True,
            versioning=False,
            kind="s3",
        )

    def inject_failure(self, action: str, exc: Exception) -> None:
        self._fail_on[action] = exc

    def clear_failure(self, action: str) -> None:
        self._fail_on.pop(action, None)

    def _maybe_fail(self, action: str) -> None:
        if action in self._fail_on:
            raise self._fail_on[action]

    def capabilities(self) -> TargetCapabilities:
        return self._caps

    def stat(self, key: str) -> ObjectMeta | None:
        data = self._objects.get(key)
        if data is None:
            return None
        return ObjectMeta(key=key, size=len(data), etag=self._etags[key], sha256=_sha256_bytes(data))

    def get_bytes(self, key: str, *, offset: int = 0, length: int | None = None) -> bytes | None:
        data = self._objects.get(key)
        if data is None:
            return None
        if length is None:
            return data[offset:]
        return data[offset : offset + length]

    def get_stream(self, key: str, *, offset: int = 0) -> Iterator[bytes]:
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
        del content_type
        self._maybe_fail("put_if_absent")
        data = _read_source(source)
        if checksum_sha256 is not None and _sha256_bytes(data) != checksum_sha256:
            raise AppError("object checksum mismatch before put", code=ErrorCode.INTERNAL, status=500)
        if key in self._objects:
            if self._objects[key] == data:
                return PutResult(key=key, etag=self._etags[key], size=len(data), created=False, server_date=_utc_iso())
            raise AppError("conditional-create-failed: object already exists", code=ErrorCode.INVALID_REQUEST, status=412)
        etag = _etag_for_bytes(data)
        self._objects[key] = data
        self._etags[key] = etag
        return PutResult(key=key, etag=etag, size=len(data), created=True, server_date=_utc_iso())

    def put_if_match(
        self,
        key: str,
        source: BinaryIO | bytes,
        *,
        expected_etag: str,
        checksum_sha256: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> PutResult:
        del content_type
        self._maybe_fail("put_if_match")
        data = _read_source(source)
        if checksum_sha256 is not None and _sha256_bytes(data) != checksum_sha256:
            raise AppError("object checksum mismatch before put", code=ErrorCode.INTERNAL, status=500)
        if key not in self._objects or self._etags.get(key) != expected_etag:
            raise AppError("conditional-replace-failed: etag mismatch", code=ErrorCode.INVALID_REQUEST, status=412)
        etag = _etag_for_bytes(data)
        self._objects[key] = data
        self._etags[key] = etag
        return PutResult(key=key, etag=etag, size=len(data), created=False, server_date=_utc_iso())

    def delete_if_match(self, key: str, *, expected_etag: str | None = None) -> bool:
        self._maybe_fail("delete")
        if key not in self._objects:
            return False
        if expected_etag is not None and self._etags.get(key) != expected_etag:
            raise AppError("conditional-delete-failed: etag mismatch", code=ErrorCode.INVALID_REQUEST, status=412)
        del self._objects[key]
        self._etags.pop(key, None)
        return True

    def list_objects(self, prefix: str, *, cursor: str | None = None, limit: int = 1000) -> ListPage:
        keys = sorted(key for key in self._objects if key.startswith(prefix))
        start = 0
        if cursor and cursor in keys:
            start = keys.index(cursor) + 1
        page_keys = keys[start : start + limit]
        objects = tuple(self.stat(key) for key in page_keys if self.stat(key) is not None)
        next_cursor = page_keys[-1] if len(page_keys) == limit and start + limit < len(keys) else None
        return ListPage(objects=objects, cursor=next_cursor)  # type: ignore[arg-type]

    def begin_multipart(self, key: str, *, checksum_sha256: str) -> MultipartUpload:
        self._maybe_fail("begin_multipart")
        upload_id = uuid.uuid4().hex
        self._multipart[upload_id] = {"key": key, "parts": {}, "checksum": checksum_sha256}
        return MultipartUpload(key=key, upload_id=upload_id, checksum_sha256=checksum_sha256)

    def upload_part(self, upload: MultipartUpload, part_number: int, data: bytes, *, checksum_sha256: str | None = None) -> dict[str, Any]:
        self._maybe_fail("upload_part")
        if checksum_sha256 is not None and _sha256_bytes(data) != checksum_sha256:
            raise AppError("multipart part checksum mismatch", code=ErrorCode.INTERNAL, status=500)
        state = self._multipart[upload.upload_id]
        state["parts"][part_number] = data
        part = {"partNumber": part_number, "etag": _etag_for_bytes(data), "size": len(data)}
        upload.parts = [item for item in upload.parts if int(item["partNumber"]) != part_number]
        upload.parts.append(part)
        upload.parts.sort(key=lambda item: int(item["partNumber"]))
        return part

    def complete_multipart_if_absent(self, upload: MultipartUpload) -> PutResult:
        self._maybe_fail("complete_multipart")
        state = self._multipart.get(upload.upload_id) or {"parts": {}}
        parts = state.get("parts") or {}
        data = b"".join(parts[number] for number in sorted(parts))
        if _sha256_bytes(data) != upload.checksum_sha256:
            raise AppError("multipart object checksum mismatch", code=ErrorCode.INTERNAL, status=500)
        result = self.put_if_absent(upload.key, data, checksum_sha256=upload.checksum_sha256)
        self._multipart.pop(upload.upload_id, None)
        return result

    def abort_multipart(self, upload: MultipartUpload) -> None:
        self._multipart.pop(upload.upload_id, None)

    def server_time(self) -> datetime | None:
        return datetime.now(tz=timezone.utc)


StreamFactory = Callable[[], BinaryIO]

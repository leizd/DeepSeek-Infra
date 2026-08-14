"""Encrypted object-set protocol contracts for remote backup storage."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable, Mapping, Sequence

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_unattended

WHOLE_AGE_V1 = "whole-age-v1"
OBJECT_SET_V1 = "object-set-v1"
CURRENT_STORAGE_PROTOCOL = OBJECT_SET_V1
DEFAULT_COMPONENT_PLAINTEXT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class EncryptedComponent:
    """One independently encrypted object in a snapshot object set.

    ``component_id`` and ``control`` are local orchestration facts. They are
    never serialized into the public remote object inventory.
    """

    component_id: str
    path: Path
    ciphertext_digest: str
    ciphertext_size: int
    control: bool = False


@dataclass(frozen=True, slots=True)
class ObjectSetPackage:
    """Verified independently encrypted components for one snapshot."""

    backup_id: str
    components: tuple[EncryptedComponent, ...]
    manifest_digest: str
    coverage_digest: str
    manifest: dict[str, Any]
    creation_verified: bool = True
    frontend: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    chunk_records: tuple[Any, ...] = ()
    effective_files: tuple[Any, ...] = ()
    savings: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        validate_components(self.components)

    @property
    def storage_protocol(self) -> str:
        return OBJECT_SET_V1

    @property
    def control(self) -> EncryptedComponent:
        return next(item for item in self.components if item.control)

    @property
    def object_set_digest(self) -> str:
        return object_set_digest(self.components)

    @property
    def filename(self) -> str:
        return f"{self.backup_id}.object-set"

    @property
    def path(self) -> Path:
        return self.control.path

    @property
    def size(self) -> int:
        return total_ciphertext_bytes(self.components)

    @property
    def ciphertext_sha256(self) -> str:
        """Compatibility commitment; never used as an object key."""
        return self.object_set_digest


@dataclass(frozen=True, slots=True)
class PreparedComponent:
    """One final plaintext Payload ZIP awaiting randomized Age encryption."""

    component_id: str
    path: Path
    plaintext_digest: str
    plaintext_size: int
    entries: tuple[str, ...]
    expected_entries: Mapping[str, tuple[int, str]]


@dataclass(frozen=True, slots=True)
class PreparedObjectSet:
    """Short-lived plaintext object-set preparation owned by one build."""

    output_dir: Path
    components: tuple[PreparedComponent, ...]
    control_entries: Mapping[str, bytes]
    manifest: dict[str, Any]
    snapshot_format: str

    @property
    def plaintext_bytes(self) -> int:
        return sum(item.plaintext_size for item in self.components)

    def scrub(self) -> None:
        for component in self.components:
            backup_unattended.scrub_plaintext_file(component.path)


class PreparedObjectSetTooLarge(Exception):
    """Exact prepared Payload bytes crossed the Adaptive Full budget."""

    def __init__(self, *, byte_limit: int, attempted_size: int) -> None:
        self.byte_limit = byte_limit
        self.attempted_size = attempted_size
        super().__init__(f"prepared object set exceeded {byte_limit} byte adaptive limit")


def validate_components(components: Sequence[EncryptedComponent]) -> None:
    if sum(1 for item in components if item.control) != 1:
        raise AppError("object set must contain exactly one control object", code=ErrorCode.INVALID_PAYLOAD)
    digests: set[str] = set()
    component_ids: set[str] = set()
    for item in components:
        if not item.component_id or item.component_id in component_ids:
            raise AppError("object set contains duplicate component ids", code=ErrorCode.INVALID_PAYLOAD)
        component_ids.add(item.component_id)
        if len(item.ciphertext_digest) != 64 or any(char not in "0123456789abcdef" for char in item.ciphertext_digest):
            raise AppError("object set contains an invalid ciphertext digest", code=ErrorCode.INVALID_PAYLOAD)
        if item.ciphertext_digest in digests:
            raise AppError("object set contains duplicate ciphertext digests", code=ErrorCode.INVALID_PAYLOAD)
        digests.add(item.ciphertext_digest)
        if item.ciphertext_size < 0:
            raise AppError("object set contains an invalid ciphertext size", code=ErrorCode.INVALID_PAYLOAD)


def remote_object_inventory(components: Sequence[EncryptedComponent]) -> list[dict[str, int | str]]:
    """Return the role-blind public commitment inventory."""
    validate_components(components)
    return sorted(
        ({"digest": item.ciphertext_digest, "size": int(item.ciphertext_size)} for item in components),
        key=lambda item: (str(item["digest"]), int(item["size"])),
    )


def object_set_commitment(components: Sequence[EncryptedComponent]) -> bytes:
    inventory = remote_object_inventory(components)
    return "".join(f"{item['digest']}:{item['size']}\n" for item in inventory).encode("ascii")


def object_set_digest(components: Sequence[EncryptedComponent]) -> str:
    return hashlib.sha256(object_set_commitment(components)).hexdigest()


def object_inventory_digest(objects: Sequence[Mapping[str, Any]]) -> str:
    normalized: list[tuple[str, int]] = []
    seen: set[str] = set()
    for item in objects:
        digest = str(item.get("digest") or "")
        size = item.get("size")
        if (
            len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or digest in seen
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise AppError("object-set receipt inventory is invalid", code=ErrorCode.INVALID_PAYLOAD)
        seen.add(digest)
        normalized.append((digest, size))
    if not normalized:
        raise AppError("object-set receipt inventory is empty", code=ErrorCode.INVALID_PAYLOAD)
    commitment = "".join(f"{digest}:{size}\n" for digest, size in sorted(normalized)).encode("ascii")
    return hashlib.sha256(commitment).hexdigest()


def committed_object_inventory(record: Mapping[str, Any]) -> list[dict[str, int | str]]:
    """Return the exact ciphertext objects committed by a receipt/catalog row.

    Object-set receipts are validated as a single commitment before any member
    is returned. Legacy Whole-Age rows normalize to their one ciphertext object.
    """
    if str(record.get("storageProtocol") or "") == OBJECT_SET_V1:
        raw_objects = record.get("objects")
        control_digest = str(record.get("controlObjectDigest") or "")
        expected_digest = str(record.get("objectSetDigest") or "")
        if not isinstance(raw_objects, list) or any(not isinstance(item, Mapping) for item in raw_objects):
            raise AppError("object-set receipt inventory is invalid", code=ErrorCode.INVALID_PAYLOAD)
        inventory: list[dict[str, int | str]] = []
        for item in raw_objects:
            raw_size = item.get("size")
            inventory.append(
                {
                    "digest": str(item.get("digest") or ""),
                    "size": raw_size if isinstance(raw_size, int) and not isinstance(raw_size, bool) else -1,
                }
            )
        if object_inventory_digest(inventory) != expected_digest:
            raise AppError("object-set receipt commitment mismatch", code=ErrorCode.INVALID_PAYLOAD)
        if len(control_digest) != 64 or control_digest not in {str(item["digest"]) for item in inventory}:
            raise AppError("object-set receipt control object is invalid", code=ErrorCode.INVALID_PAYLOAD)
        return sorted(inventory, key=lambda item: (str(item["digest"]), int(item["size"])))
    digest = str(record.get("objectDigest") or record.get("ciphertextSha256") or "")
    raw_size = record.get("size")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return []
    size = int(raw_size) if isinstance(raw_size, int) and not isinstance(raw_size, bool) and raw_size >= 0 else 0
    return [{"digest": digest, "size": size}]


def committed_object_digests(record: Mapping[str, Any]) -> set[str]:
    return {str(item["digest"]) for item in committed_object_inventory(record)}


def total_ciphertext_bytes(components: Iterable[EncryptedComponent]) -> int:
    return sum(item.ciphertext_size for item in components)


def _safe_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise AppError("object-set payload contains an unsafe path", code=ErrorCode.INVALID_PAYLOAD)
    return path.as_posix()


def _zip_info(relative: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100600 << 16
    return info


def _write_zip_entries(
    output: BinaryIO,
    *,
    file_entries: Mapping[str, Path] | None = None,
    byte_entries: Mapping[str, bytes] | None = None,
) -> None:
    files = {_safe_relative_path(key): value for key, value in (file_entries or {}).items()}
    values = {_safe_relative_path(key): value for key, value in (byte_entries or {}).items()}
    if set(files) & set(values):
        raise AppError("object-set ZIP entry collision", code=ErrorCode.INVALID_PAYLOAD)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for relative in sorted(set(files) | set(values)):
            with archive.open(_zip_info(relative), "w") as target:
                source_path = files.get(relative)
                if source_path is not None:
                    with source_path.open("rb") as source:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                else:
                    target.write(values[relative])


def _verify_component_archive(path: Path, expected: Mapping[str, tuple[int, str]]) -> None:
    with zipfile.ZipFile(path) as archive:
        names = {_safe_relative_path(info.filename) for info in archive.infolist() if not info.is_dir()}
        if names != set(expected):
            raise AppError("object-set component inventory mismatch", code=ErrorCode.INVALID_PAYLOAD)
        for relative, (expected_size, expected_digest) in expected.items():
            digest = hashlib.sha256()
            size = 0
            with archive.open(relative) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
            if size != expected_size or digest.hexdigest() != expected_digest:
                raise AppError("object-set component plaintext mismatch", code=ErrorCode.INVALID_PAYLOAD)


def extract_component_archive(archive_path: Path, destination: Path, expected_paths: Sequence[str]) -> None:
    """Extract one verified payload component without allowing path overlap."""
    expected = {_safe_relative_path(path) for path in expected_paths}
    with zipfile.ZipFile(archive_path) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        names = [_safe_relative_path(info.filename) for info in infos]
        if len(names) != len(set(names)) or set(names) != expected:
            raise AppError("object-set component inventory mismatch", code=ErrorCode.INVALID_PAYLOAD)
        for info, relative in zip(infos, names, strict=True):
            target = destination.joinpath(*PurePosixPath(relative).parts)
            if target.exists():
                raise AppError("object-set components overlap", code=ErrorCode.INVALID_PAYLOAD)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def verify_control_metadata(destination: Path) -> None:
    """Verify every plaintext Control entry against its encrypted checksum map."""
    checksum_path = destination / "checksums.sha256"
    if not checksum_path.is_file():
        raise AppError("object-set Control checksum map is missing", code=ErrorCode.INVALID_PAYLOAD)
    expected: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        digest, separator, raw_path = line.partition("  ")
        relative = _safe_relative_path(raw_path) if separator else ""
        if not separator or relative == "checksums.sha256" or relative in expected or len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest
        ):
            raise AppError("object-set Control checksum map is invalid", code=ErrorCode.INVALID_PAYLOAD)
        expected[relative] = digest
    actual = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() and path != checksum_path
    }
    if actual != set(expected):
        raise AppError("object-set Control inventory mismatch", code=ErrorCode.INVALID_PAYLOAD)
    for relative, digest in expected.items():
        path = destination.joinpath(*PurePosixPath(relative).parts)
        if backup_unattended.sha256_file(path) != digest:
            raise AppError("object-set Control checksum mismatch", code=ErrorCode.INVALID_PAYLOAD)


def _stream_file(path: Path, output: BinaryIO) -> None:
    with path.open("rb") as source:
        shutil.copyfileobj(source, output, length=1024 * 1024)


def _encrypt_zip_component(
    *,
    component_id: str,
    plaintext_path: Path,
    ciphertext_path: Path,
    recipients: tuple[str, ...],
    expected_entries: Mapping[str, tuple[int, str]],
    control: bool = False,
    cancel_event: Any | None = None,
) -> tuple[EncryptedComponent, dict[str, int | str]]:
    plaintext_size = plaintext_path.stat().st_size
    plaintext_digest = backup_unattended.sha256_file(plaintext_path)
    encrypted = backup_unattended.encrypt_unattended(
        ciphertext_path,
        lambda output: _stream_file(plaintext_path, output),
        recipients=recipients,
        verify=lambda decrypted: _verify_component_archive(decrypted, expected_entries),
        cancel_event=cancel_event,
    )
    component = EncryptedComponent(
        component_id=component_id,
        path=ciphertext_path,
        ciphertext_digest=encrypted.ciphertext_sha256,
        ciphertext_size=encrypted.size,
        control=control,
    )
    descriptor: dict[str, int | str] = {
        "ciphertextDigest": encrypted.ciphertext_sha256,
        "ciphertextSize": encrypted.size,
        "plaintextSize": plaintext_size,
        "plaintextSha256": plaintext_digest,
    }
    return component, descriptor


def _physical_payload_paths(staging: Path, manifest: Mapping[str, Any]) -> list[str]:
    snapshot_kind = str(manifest.get("snapshotKind") or "full")
    entries = manifest.get("deltaFiles") if snapshot_kind == "incremental" else manifest.get("files")
    metadata_paths = {"delta/operations.json", "payload/packs/index.json"}
    result: list[str] = []
    for raw in entries or []:
        if not isinstance(raw, Mapping):
            continue
        relative = _safe_relative_path(str(raw.get("path") or ""))
        if relative in metadata_paths:
            continue
        if (staging / relative).is_file():
            result.append(relative)
    return sorted(set(result))


def _payload_component_groups(staging: Path, paths: Sequence[str], target_bytes: int) -> list[list[str]]:
    if target_bytes <= 0:
        raise AppError("object-set component target must be positive", code=ErrorCode.INVALID_PAYLOAD)
    buckets: dict[str, list[str]] = {}
    for relative in paths:
        parts = PurePosixPath(relative).parts
        if len(parts) >= 3 and parts[:2] == ("payload", "projects"):
            bucket = f"projects/{parts[2]}"
        elif len(parts) >= 2:
            bucket = f"contributor/{parts[1]}"
        else:  # pragma: no cover - payload paths always have a contributor segment
            bucket = relative
        buckets.setdefault(bucket, []).append(relative)
    groups: list[list[str]] = []
    for bucket in sorted(buckets):
        current: list[str] = []
        current_bytes = 0
        for relative in buckets[bucket]:
            size = staging.joinpath(*PurePosixPath(relative).parts).stat().st_size
            if current and current_bytes + size > target_bytes:
                groups.append(current)
                current = []
                current_bytes = 0
            current.append(relative)
            current_bytes += size
            if current_bytes >= target_bytes:
                groups.append(current)
                current = []
                current_bytes = 0
        if current:
            groups.append(current)
    return groups


def _stable_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _scrub_prepared_plaintext(output_dir: Path) -> None:
    if not output_dir.is_dir():
        return
    for plaintext in output_dir.glob(".*.plaintext.tmp"):
        if plaintext.is_file():
            backup_unattended.scrub_plaintext_file(plaintext)


def prepare_object_set(
    staging: Path,
    output_dir: Path,
    *,
    manifest: dict[str, Any],
    component_target_bytes: int = DEFAULT_COMPONENT_PLAINTEXT_BYTES,
    plaintext_byte_limit: int | None = None,
    cancel_event: Any | None = None,
) -> PreparedObjectSet:
    """Build the final plaintext Payload ZIPs exactly once.

    The returned object owns every plaintext path. Callers must either pass it
    to :func:`encrypt_prepared_object_set` or call ``scrub()`` themselves.
    """
    if plaintext_byte_limit is not None and plaintext_byte_limit < 0:
        raise ValueError("plaintext_byte_limit must be non-negative")
    _scrub_prepared_plaintext(output_dir)
    shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True)
    prepared_components: list[PreparedComponent] = []
    plaintext_paths: list[Path] = []
    plaintext_bytes = 0
    component_paths: dict[str, str] = {}
    component_entries: dict[str, list[str]] = {}
    full_payload_map: dict[str, dict[str, dict[str, int | str]]] = {}
    try:
        physical_paths = _physical_payload_paths(staging, manifest)
        groups = _payload_component_groups(staging, physical_paths, int(component_target_bytes))
        for ordinal, relatives in enumerate(groups):
            checkpoint = getattr(cancel_event, "backup_checkpoint", None)
            if callable(checkpoint):
                checkpoint()
            if cancel_event is not None and cancel_event.is_set():
                raise AppError("Object-set preparation cancelled", code=ErrorCode.INVALID_REQUEST, status=499)
            component_id = f"p{ordinal:04d}"
            file_entries = {
                relative: staging.joinpath(*PurePosixPath(relative).parts)
                for relative in relatives
            }
            expected = {
                relative: (source.stat().st_size, backup_unattended.sha256_file(source))
                for relative, source in file_entries.items()
            }
            plaintext = output_dir / f".{component_id}.plaintext.tmp"
            plaintext_paths.append(plaintext)
            with plaintext.open("w+b") as output:
                _write_zip_entries(output, file_entries=file_entries)
                output.flush()
                os.fsync(output.fileno())
            prepared_component = PreparedComponent(
                component_id=component_id,
                path=plaintext,
                plaintext_digest=backup_unattended.sha256_file(plaintext),
                plaintext_size=plaintext.stat().st_size,
                entries=tuple(relatives),
                expected_entries=expected,
            )
            prepared_components.append(prepared_component)
            plaintext_bytes += prepared_component.plaintext_size
            if plaintext_byte_limit is not None and plaintext_bytes > plaintext_byte_limit:
                raise PreparedObjectSetTooLarge(
                    byte_limit=plaintext_byte_limit,
                    attempted_size=plaintext_bytes,
                )
            component_entries[component_id] = list(relatives)
            for entry_ordinal, relative in enumerate(relatives):
                component_paths[relative] = component_id
                if str(manifest.get("snapshotKind") or "full") == "full":
                    full_payload_map[relative] = {
                        "source": {
                            "kind": "pack-range",
                            "componentId": component_id,
                            "entry": entry_ordinal,
                        }
                    }

        control_manifest = dict(manifest)
        control_manifest["storageProtocol"] = OBJECT_SET_V1
        snapshot = control_manifest.get("snapshot")
        snapshot_format = str(snapshot.get("format") or "full-v2") if isinstance(snapshot, Mapping) else "full-v2"
        control_manifest["snapshotFormat"] = snapshot_format
        component_map = {
            "schemaVersion": 1,
            "paths": component_paths,
            "components": component_entries,
        }
        if full_payload_map:
            component_map["fullPayloadMap"] = full_payload_map
        control_entries: dict[str, bytes] = {
            "manifest.json": _stable_json(control_manifest),
            "component-map.json": _stable_json(component_map),
        }
        for relative in ("delta/operations.json", "payload/packs/index.json"):
            source = staging / relative
            if source.is_file():
                control_entries[relative] = source.read_bytes()
        return PreparedObjectSet(
            output_dir=output_dir,
            components=tuple(prepared_components),
            control_entries=control_entries,
            manifest=control_manifest,
            snapshot_format=snapshot_format,
        )
    except BaseException:
        for plaintext in plaintext_paths:
            backup_unattended.scrub_plaintext_file(plaintext)
        raise


def encrypt_prepared_object_set(
    prepared: PreparedObjectSet,
    *,
    backup_id: str,
    recipients: tuple[str, ...],
    manifest: dict[str, Any],
    manifest_digest: str,
    coverage_digest: str,
    frontend: dict[str, Any] | None = None,
    coverage: dict[str, Any] | None = None,
    cancel_event: Any | None = None,
    chunk_records: tuple[Any, ...] = (),
    effective_files: tuple[Any, ...] = (),
    savings: dict[str, Any] | None = None,
) -> ObjectSetPackage:
    """Age-encrypt prepared Payload ZIPs directly, then build Control."""
    del manifest_digest  # Object-set metadata changes the committed manifest digest.
    payload_components: list[EncryptedComponent] = []
    payload_descriptors: dict[str, dict[str, int | str]] = {}
    control_plaintext = prepared.output_dir / ".control.plaintext.tmp"
    try:
        expected_manifest = dict(manifest)
        expected_manifest["storageProtocol"] = OBJECT_SET_V1
        expected_manifest["snapshotFormat"] = prepared.snapshot_format
        if _stable_json(expected_manifest) != prepared.control_entries.get("manifest.json"):
            raise AppError("prepared object set manifest mismatch", code=ErrorCode.INVALID_PAYLOAD)
        for item in prepared.components:
            try:
                prepared_matches = (
                    item.path.stat().st_size == item.plaintext_size
                    and backup_unattended.sha256_file(item.path) == item.plaintext_digest
                )
            except OSError:
                prepared_matches = False
            if not prepared_matches:
                raise AppError("prepared object set changed before encryption", code=ErrorCode.INVALID_REQUEST, status=409)
            component, descriptor = _encrypt_zip_component(
                component_id=item.component_id,
                plaintext_path=item.path,
                ciphertext_path=prepared.output_dir / f"{item.component_id}.age",
                recipients=recipients,
                expected_entries=item.expected_entries,
                cancel_event=cancel_event,
            )
            payload_components.append(component)
            payload_descriptors[item.component_id] = descriptor
            backup_unattended.scrub_plaintext_file(item.path)

        payload_index = {
            "schemaVersion": 1,
            "storageProtocol": OBJECT_SET_V1,
            "snapshotFormat": prepared.snapshot_format,
            "payloadComponents": payload_descriptors,
        }
        control_entries = {
            **prepared.control_entries,
            "payload-index.json": _stable_json(payload_index),
        }
        checksum_lines = [
            f"{hashlib.sha256(content).hexdigest()}  {relative}"
            for relative, content in sorted(control_entries.items())
        ]
        control_entries["checksums.sha256"] = ("\n".join(checksum_lines) + "\n").encode("utf-8")
        control_expected = {
            relative: (len(content), hashlib.sha256(content).hexdigest())
            for relative, content in control_entries.items()
        }
        with control_plaintext.open("w+b") as output:
            _write_zip_entries(output, byte_entries=control_entries)
            output.flush()
            os.fsync(output.fileno())
        control, _descriptor = _encrypt_zip_component(
            component_id="control",
            plaintext_path=control_plaintext,
            ciphertext_path=prepared.output_dir / "control.age",
            recipients=recipients,
            expected_entries=control_expected,
            control=True,
            cancel_event=cancel_event,
        )
        manifest_bytes = prepared.control_entries["manifest.json"]
        return ObjectSetPackage(
            backup_id=backup_id,
            components=(control, *payload_components),
            manifest_digest=hashlib.sha256(manifest_bytes).hexdigest(),
            coverage_digest=coverage_digest,
            manifest=prepared.manifest,
            creation_verified=True,
            frontend=dict(frontend or {}),
            coverage=dict(coverage or {}),
            chunk_records=chunk_records,
            effective_files=effective_files,
            savings=savings,
        )
    finally:
        backup_unattended.scrub_plaintext_file(control_plaintext)
        prepared.scrub()


def build_encrypted_object_set(
    staging: Path,
    output_dir: Path,
    *,
    backup_id: str,
    recipients: tuple[str, ...],
    manifest: dict[str, Any],
    manifest_digest: str,
    coverage_digest: str,
    frontend: dict[str, Any] | None = None,
    coverage: dict[str, Any] | None = None,
    cancel_event: Any | None = None,
    chunk_records: tuple[Any, ...] = (),
    effective_files: tuple[Any, ...] = (),
    savings: dict[str, Any] | None = None,
    component_target_bytes: int = DEFAULT_COMPONENT_PLAINTEXT_BYTES,
) -> ObjectSetPackage:
    """Compatibility wrapper that prepares once and encrypts directly."""
    prepared = prepare_object_set(
        staging,
        output_dir,
        manifest=manifest,
        component_target_bytes=component_target_bytes,
        cancel_event=cancel_event,
    )
    return encrypt_prepared_object_set(
        prepared,
        backup_id=backup_id,
        recipients=recipients,
        manifest=manifest,
        manifest_digest=manifest_digest,
        coverage_digest=coverage_digest,
        frontend=frontend,
        coverage=coverage,
        cancel_event=cancel_event,
        chunk_records=chunk_records,
        effective_files=effective_files,
        savings=savings,
    )

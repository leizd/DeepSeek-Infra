"""Offline export and durable fencing of an explicitly named Python mirror copy.

This tool does not open or mutate the native target. The versioned inventory
preserves every accepted source file and directory, including legacy mirrors.
Rust import, independent target attestation, mutation admission, and a verified
handback are separate gates. A checksum inventory is not an Age decrypt proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from deepseek_infra.infra.workspace import backup_mirror, mutation_gate
from deepseek_infra.core.errors import AppError

EXPORT_SCHEMA = "python-mirror-inventory-export-v1"
DOMAIN = "frontend_mirror_store"
MAX_DOCUMENT_BYTES = 16 << 20
MAX_METADATA_BYTES = 1 << 20
MAX_ENTRIES = 100_000
_TRANSFER_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _reject_link(path: Path, info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError(f"Mirror handoff refuses a symlink or reparse point: {path}")


def _bound_path(path: Path) -> Path:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        if component == "..":
            raise ValueError("Mirror handoff requires normalized explicit paths")
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ValueError(f"Mirror handoff path cannot be inspected: {current}") from exc
        _reject_link(current, info)
    return absolute.resolve(strict=False)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _json_object(path: Path, maximum: int) -> dict[str, Any]:
    try:
        info = path.lstat()
        _reject_link(path, info)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError(f"Invalid mirror document: {path}")
        with path.open("rb") as handle:
            raw = handle.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError(f"Mirror document is too large: {path}")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unreadable mirror document: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Mirror document must be an object: {path}")
    return value


def _file_digest(path: Path, initial: os.stat_result) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"Mirror entry is not a regular file: {path}")
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
        final = os.fstat(handle.fileno())
    current = path.lstat()
    _reject_link(path, current)
    fingerprints = {(item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_mode) for item in (initial, opened, final, current)}
    if len(fingerprints) != 1:
        raise ValueError(f"Mirror source changed during inventory: {path}")
    return digest.hexdigest()


def _inventory(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            info = path.lstat()
            _reject_link(path, info)
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(info.st_mode):
                entry = {"path": relative, "kind": "directory", "size": 0, "sha256": ""}
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                entry = {"path": relative, "kind": "file", "size": info.st_size, "sha256": _file_digest(path, info)}
            else:
                raise ValueError(f"Mirror handoff refuses a non-regular entry: {path}")
            entries.append(entry)
            if len(entries) > MAX_ENTRIES:
                raise ValueError("Mirror inventory has too many entries")
    entries.sort(key=lambda item: str(item["path"]))
    _validate_layout(root, entries)
    return entries


def _validate_layout(root: Path, entries: list[dict[str, Any]]) -> None:
    rows = {str(entry["path"]): entry for entry in entries}
    accepted: set[str] = set()

    def require(path: str, kind: str) -> dict[str, Any]:
        entry = rows.get(path)
        if entry is None or entry["kind"] != kind:
            raise ValueError(f"Incomplete mirror inventory: {path}")
        accepted.add(path)
        return entry

    def metadata(directory: str, filename: str, profile: str, generation: str | None) -> None:
        path = f"{directory}/{filename}"
        require(path, "file")
        value = _json_object(root / path, MAX_METADATA_BYTES)
        if value.get("profileId", profile) != profile:
            raise ValueError(f"Mirror metadata profile mismatch: {path}")
        if generation is not None:
            if type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 2 or value.get("generationId") != generation:
                raise ValueError(f"Mirror generation metadata mismatch: {path}")
            variants = value.get("recipientVariants")
            if not isinstance(variants, list) or not variants:
                raise ValueError(f"Mirror generation has no recipient variants: {path}")
            seen: set[str] = set()
            for variant in variants:
                if not isinstance(variant, dict):
                    raise ValueError(f"Invalid mirror recipient variant: {path}")
                variant_name = variant.get("filename")
                digest = variant.get("ciphertextSha256")
                if not isinstance(variant_name, str) or not backup_mirror._VARIANT_FILENAME.fullmatch(variant_name) or variant_name in seen:
                    raise ValueError(f"Invalid mirror variant filename: {path}")
                seen.add(variant_name)
                ciphertext = require(f"{directory}/{variant_name}", "file")
                if not isinstance(digest, str) or not _SHA256.fullmatch(digest) or ciphertext["sha256"] != digest:
                    raise ValueError(f"Mirror ciphertext digest mismatch: {path}")
            if value.get("ciphertextSha256") != variants[0].get("ciphertextSha256"):
                raise ValueError(f"Mirror primary ciphertext digest mismatch: {path}")
        else:
            ciphertext = require(f"{directory}/{backup_mirror.MIRROR_CIPHERTEXT_NAME}", "file")
            digest = value.get("ciphertextSha256")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest) or ciphertext["sha256"] != digest:
                raise ValueError(f"Legacy mirror ciphertext digest mismatch: {path}")

    profiles = [entry for entry in entries if "/" not in str(entry["path"])]
    for entry in profiles:
        profile = str(entry["path"])
        try:
            backup_mirror._profile_id(profile)
        except AppError as exc:
            raise ValueError(f"Invalid mirror profile directory: {profile}") from exc
        require(profile, "directory")
        head_path = f"{profile}/{backup_mirror.HEAD_NAME}"
        legacy_path = f"{profile}/{backup_mirror.MIRROR_METADATA_NAME}"
        if head_path in rows:
            require(head_path, "file")
            head = _json_object(root / head_path, MAX_METADATA_BYTES)
            generation = head.get("generationId")
            if type(head.get("schemaVersion")) is not int or head["schemaVersion"] != 2 or not isinstance(generation, str) or not backup_mirror._GENERATION_ID.fullmatch(generation):
                raise ValueError(f"Invalid mirror HEAD: {head_path}")
            require(f"{profile}/generations/{generation}", "directory")
        elif legacy_path not in rows:
            raise ValueError(f"Mirror profile has no settled HEAD or legacy metadata: {profile}")
        if legacy_path in rows:
            metadata(profile, backup_mirror.MIRROR_METADATA_NAME, profile, None)
        for container in ("generations", "previous"):
            directory = f"{profile}/{container}"
            if directory not in rows:
                continue
            require(directory, "directory")
            if container == "previous":
                if any(str(row).startswith(directory + "/") for row in rows):
                    metadata(directory, backup_mirror.MIRROR_METADATA_NAME, profile, None)
                continue
            for row in entries:
                path = str(row["path"])
                if not path.startswith(directory + "/") or len(path.split("/")) != 3:
                    continue
                generation = path.split("/")[-1]
                if not backup_mirror._GENERATION_ID.fullmatch(generation):
                    raise ValueError(f"Invalid mirror generation directory: {path}")
                require(path, "directory")
                metadata(path, backup_mirror.GENERATION_METADATA_NAME, profile, generation)
        lock = f"{profile}/.mirror.lock"
        if lock in rows:
            require(lock, "file")
    if accepted != set(rows):
        sample = sorted(set(rows) - accepted)[:3]
        raise ValueError(f"Unknown or unfinished mirror source entries: {sample}")


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_document(path: Path, data: bytes) -> None:
    """Publish a fully fsynced document without replacing an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _json_object(path, MAX_DOCUMENT_BYTES) != json.loads(data):
                raise ValueError(f"Mirror handoff refuses to replace an existing document: {path}")
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _native_mirror_gate(source: Path) -> Iterator[None]:
    """Match the Rust store lock, including direct native CLI writers."""
    with _named_file_gate(source.with_name(source.name + ".native-import.lock")):
        yield


@contextmanager
def _named_file_gate(path: Path) -> Iterator[None]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass
    else:
        _reject_link(path, info)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Invalid native mirror handoff lock file")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        mutation_gate._lock_file(handle)
        try:
            yield
        finally:
            mutation_gate._unlock_file(handle)


@contextmanager
def _target_workspace_gate(source: Path, target: Path) -> Iterator[None]:
    if source.parent == target.parent:
        yield
        return
    with _named_file_gate(target.parent / ".workspace-mutation.lock"):
        yield


def _restore_fenced(root: Path) -> bool:
    try:
        (root / ".workspace-restore-fence.json").lstat()
    except FileNotFoundError:
        return False
    return True


def export_and_fence(source_root: Path, target_root: Path, transfer_id: str, output: Path, *, allow_empty: bool = False) -> dict[str, Any]:
    if not _TRANSFER_ID.fullmatch(transfer_id):
        raise ValueError("Invalid mirror transfer id")
    source, target, output = (_bound_path(path) for path in (source_root, target_root, output))
    if _inside(source, target) or _inside(target, source) or _inside(output, source) or _inside(output, target):
        raise ValueError("Mirror source, native target, and output must have independent paths")
    try:
        if not source.is_dir():
            raise ValueError("Mirror source directory must exist; no empty source is fabricated")
    except OSError as exc:
        raise ValueError("Mirror source directory cannot be inspected") from exc
    fence = backup_mirror.mirror_handoff_fence_path(source)
    if output == fence:
        raise ValueError("Mirror export output must be independent of the source fence")
    _bound_path(fence)
    with mutation_gate.exclusive_gate(root=source.parent), _native_mirror_gate(source):
        if _restore_fenced(source.parent):
            raise ValueError("Mirror handoff is blocked by an unfinished workspace restore")
        entries = _inventory(source)
        if not entries and not allow_empty:
            raise ValueError("Empty mirror inventory requires explicit --allow-empty attestation")
        manifest: dict[str, Any] = {
            "schema": EXPORT_SCHEMA, "domain": DOMAIN, "transferId": transfer_id,
            "sourceRoot": source.as_posix(), "targetRoot": target.as_posix(),
            "entries": entries, "emptyInventory": not entries, "sourceDigest": _digest(entries),
        }
        manifest["manifestDigest"] = _digest(manifest)
        data = _canonical(manifest) + b"\n"
        if len(data) > MAX_DOCUMENT_BYTES:
            raise ValueError("Mirror export document is too large")
        if fence.exists():
            if _json_object(fence, MAX_DOCUMENT_BYTES) != manifest:
                raise ValueError("Existing mirror source fence cannot be rebound or replaced")
        elif target.exists():
            raise ValueError("The independent native mirror target must not exist before export")
        if output.exists() and _json_object(output, MAX_DOCUMENT_BYTES) != manifest:
            raise ValueError("Existing mirror output cannot be replaced")
        _publish_document(fence, data)
        # A crash after the source fence must leave the old writer denied. A
        # retry can republish only the exact same inventory and path bindings.
        _publish_document(output, data)
        return manifest


def handback_revoked_candidate(source_root: Path, target_root: Path, manifest_path: Path) -> dict[str, Any]:
    """Release only an unchanged pre-admission source after native revocation.

    This does not restore an active native owner or change runtime mode. The
    candidate and all history stay fenced; a native denial marker is published
    before the original source writer can resume.
    """
    source, target, manifest_path = (_bound_path(path) for path in (source_root, target_root, manifest_path))
    if _inside(source, target) or _inside(target, source) or _inside(manifest_path, source) or _inside(manifest_path, target):
        raise ValueError("Mirror source, native target, and manifest must have independent paths")
    if not source.is_dir() or not target.is_dir():
        raise ValueError("Mirror handback requires both attested directories")
    manifest = _json_object(manifest_path, MAX_DOCUMENT_BYTES)
    manifest_fields = {"schema", "domain", "transferId", "sourceRoot", "targetRoot", "entries", "emptyInventory", "sourceDigest", "manifestDigest"}
    transfer = manifest.get("transferId")
    entries = manifest.get("entries")
    unsigned_manifest = {key: value for key, value in manifest.items() if key != "manifestDigest"}
    if (set(manifest) != manifest_fields or manifest.get("schema") != EXPORT_SCHEMA or manifest.get("domain") != DOMAIN
            or not isinstance(transfer, str) or not _TRANSFER_ID.fullmatch(transfer)
            or manifest.get("sourceRoot") != source.as_posix() or manifest.get("targetRoot") != target.as_posix()
            or not isinstance(entries, list) or len(entries) > MAX_ENTRIES
            or type(manifest.get("emptyInventory")) is not bool or manifest["emptyInventory"] != (not entries)
            or manifest.get("sourceDigest") != _digest(entries) or manifest.get("manifestDigest") != _digest(unsigned_manifest)):
        raise ValueError("Invalid mirror handback manifest or path binding")
    fence = _bound_path(backup_mirror.mirror_handoff_fence_path(source))
    receipt_path = _bound_path(target.with_name(target.name + ".native-import.json"))
    stage = _bound_path(target.with_name(target.name + ".native-staging-" + transfer))
    marker_path = _bound_path(source.with_name(source.name + ".native-handback.json"))
    audit_path = _bound_path(source.with_name(source.name + ".native-handback-" + transfer + ".json"))
    archive_path = _bound_path(source.with_name(source.name + ".native-handoff-" + transfer + ".revoked.json"))
    marker = {"schema": "python-mirror-original-writer-v1", "domain": DOMAIN,
              "sourceRoot": source.as_posix(), "nativeWriterDenied": True}
    with (mutation_gate.exclusive_gate(root=source.parent), _native_mirror_gate(source),
          _target_workspace_gate(source, target), _native_mirror_gate(target)):
        if any(_restore_fenced(root.parent) for root in (source, target)):
            raise ValueError("Mirror handback is blocked by an unfinished workspace restore")
        if stage.exists():
            raise ValueError("Mirror staging state still exists; reconciliation required")
        receipt = _json_object(receipt_path, MAX_DOCUMENT_BYTES)
        unsigned_receipt = {key: value for key, value in receipt.items() if key != "receiptDigest"}
        expected_receipt = {
            "schema": "native-mirror-inventory-import-v1", "domain": DOMAIN,
            "phase": "revoked", "transferId": transfer,
            "sourceRoot": source.as_posix(), "targetRoot": target.as_posix(), "stagingRoot": stage.as_posix(),
            "manifestDigest": manifest["manifestDigest"], "sourceDigest": manifest["sourceDigest"],
            "targetDigest": manifest["sourceDigest"],
        }
        if unsigned_receipt != expected_receipt or receipt.get("receiptDigest") != _digest(unsigned_receipt):
            raise ValueError("Mirror candidate must have an exact native revocation receipt before handback")
        # Rehash the actual native directory, not just the supplied receipt.
        if _inventory(target) != entries:
            raise ValueError("Native mirror inventory changed before handback")
        result: dict[str, Any] = {
            "schema": "python-mirror-candidate-handback-v1", "domain": DOMAIN, "transferId": transfer,
            "sourceRoot": source.as_posix(), "targetRoot": target.as_posix(),
            "manifestDigest": manifest["manifestDigest"], "sourceDigest": manifest["sourceDigest"],
            "targetDigest": receipt["targetDigest"], "revocationReceiptDigest": receipt["receiptDigest"],
        }
        result["handbackDigest"] = _digest(result)
        try:
            fence.lstat()
        except FileNotFoundError:
            # A completed replay verifies durable history but cannot overwrite
            # original-writer changes made since the handback.
            if (_json_object(audit_path, MAX_DOCUMENT_BYTES) != result
                    or _json_object(marker_path, MAX_DOCUMENT_BYTES) != marker
                    or _json_object(archive_path, MAX_DOCUMENT_BYTES) != manifest):
                raise ValueError("Mirror handback history is missing or inconsistent")
            return result
        if _json_object(fence, MAX_DOCUMENT_BYTES) != manifest or _inventory(source) != entries:
            raise ValueError("Original mirror source or source fence changed before handback")
        raw_fence = fence.read_bytes()
        _publish_document(archive_path, raw_fence)
        if archive_path.read_bytes() != raw_fence:
            raise ValueError("Archived mirror source fence differs from the original bytes")
        _publish_document(marker_path, _canonical(marker) + b"\n")
        _publish_document(audit_path, _canonical(result) + b"\n")
        if (_inventory(source) != entries or _inventory(target) != entries or fence.read_bytes() != raw_fence
                or any(_restore_fenced(root.parent) for root in (source, target))):
            raise ValueError("Mirror handback attestation changed before releasing the source fence")
        fence.unlink()
        _fsync_directory(fence.parent)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--transfer-id")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-empty", action="store_true")
    parser.add_argument("--handback", action="store_true")
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    if args.handback:
        if args.manifest is None or args.transfer_id is not None or args.output is not None or args.allow_empty:
            parser.error("--handback requires --manifest and cannot be combined with export arguments")
        print(json.dumps(handback_revoked_candidate(args.source, args.target, args.manifest), sort_keys=True))
        return
    if args.manifest is not None or args.transfer_id is None or args.output is None:
        parser.error("export requires --transfer-id and --output; --manifest is only for --handback")
    manifest = export_and_fence(args.source, args.target, args.transfer_id, args.output, allow_empty=args.allow_empty)
    print(json.dumps({"domain": DOMAIN, "entries": len(manifest["entries"]), "sourceDigest": manifest["sourceDigest"],
                      "manifestDigest": manifest["manifestDigest"], "output": args.output.as_posix()}, sort_keys=True))


if __name__ == "__main__":
    main()

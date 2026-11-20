"""Offline, fenced export/import of the S0 workspace backup stores.

S0 declared fifteen workspace backup/restore/DR/resilience stores in
`release/native_runtime_ownership_v1.json` as `python -> rust`. Ownership can only
move if the state can move with it, so this tool implements the state movement
and the checks that make it reversible:

* **export** writes a content-addressed inventory of a store directory: every
  entry's relative path, kind, size and SHA-256, plus a store-level digest and the
  declared ownership record the export was taken under;
* **verify** re-reads an export and confirms the inventory matches the bytes on
  disk, so a truncated or edited archive is caught before anything is imported;
* **import** writes the inventory into a *different* root and refuses when the
  destination already holds conflicting state — it never merges into a live
  store;
* **rollback** restores a pre-import snapshot of the destination, which is how an
  interrupted or wrong import is undone without hand editing files.

Every command is offline and operates on explicit paths. Nothing here reads or
writes a production store by default, and no command starts a service. Fencing
is the caller's responsibility via the named lock file: `--fence` takes an
exclusive lock for the whole operation and the tool refuses to run when another
process holds it, which is what stops a second writer during the transfer.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
OWNERSHIP_PATH = ROOT / "release" / "native_runtime_ownership_v1.json"

EXPORT_SCHEMA = "workspace-store-export-v1"
SNAPSHOT_SCHEMA = "workspace-store-snapshot-v1"
MAX_ENTRIES = 200_000
MAX_BYTES = 8 << 30
FENCE_SUFFIX = ".s0-cutover.lock"


class CutoverError(RuntimeError):
    pass


def declared_stores() -> dict[str, dict[str, Any]]:
    data = json.loads(OWNERSHIP_PATH.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for item in data["domains"]:
        if "storePath" not in item:
            continue
        out[item["id"]] = item
    return out


def _digest(document: Any) -> str:
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_link(path: Path, info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise CutoverError(f"refusing symlinked entry: {path}")
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise CutoverError(f"refusing non-regular entry: {path}")


def _inventory(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    if not root.is_dir():
        raise CutoverError(f"store root is not a directory: {root}")
    entries: list[dict[str, Any]] = []
    pending = [root]
    total = 0
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            info = path.lstat()
            _reject_link(path, info)
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(info.st_mode):
                entries.append({"path": relative, "kind": "directory", "size": 0, "sha256": ""})
                pending.append(path)
            else:
                total += info.st_size
                if total > MAX_BYTES:
                    raise CutoverError("store exceeds the export size limit")
                entries.append(
                    {"path": relative, "kind": "file", "size": info.st_size, "sha256": _file_digest(path)}
                )
            if len(entries) > MAX_ENTRIES:
                raise CutoverError("store exceeds the export entry limit")
    entries.sort(key=lambda item: str(item["path"]))
    return entries


def _publish(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


@contextlib.contextmanager
def _fence(store_root: Path, *, enabled: bool) -> Iterator[None]:
    """Exclusive named-file lock covering the whole transfer."""
    if not enabled:
        yield
        return
    lock = store_root.parent / (store_root.name + FENCE_SUFFIX)
    lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        # Both binding modules are platform-specific and neither is typed on the
        # other platform, so they are imported dynamically with the branch already
        # selected at runtime. Static imports here would fail mypy on one of them.
        module_name, attribute = ("fcntl", "flock") if os.name == "posix" else ("msvcrt", "locking")
        binding = importlib.import_module(module_name)
        flags = getattr(binding, "LOCK_EX", 0) | getattr(binding, "LOCK_NB", 0)
        try:
            if os.name == "posix":
                binding.flock(descriptor, flags)
            else:
                # msvcrt takes an explicit byte count; fcntl locks the whole file.
                binding.locking(descriptor, getattr(binding, "LK_NBLCK", 0), 1)
        except OSError as exc:
            raise CutoverError(f"another process holds the cutover fence: {lock}") from exc
        yield
    finally:
        os.close(descriptor)


def export_store(
    store_id: str,
    source_root: Path,
    output: Path,
    *,
    transfer_id: str,
    fence: bool = True,
) -> dict[str, Any]:
    stores = declared_stores()
    if store_id not in stores:
        raise CutoverError(f"store {store_id!r} is not declared in the ownership contract")
    record = stores[store_id]
    with _fence(source_root, enabled=fence):
        entries = _inventory(source_root)
        document = {
            "schema": EXPORT_SCHEMA,
            "storeId": store_id,
            "transferId": transfer_id,
            "declaredStorePath": record["storePath"],
            "declaredCutover": record["cutover"],
            "declaredTargetOwner": record["target_owner"],
            "declaredWriterEntryPoints": record.get("writerEntryPoints", []),
            "entries": entries,
            "totalBytes": sum(int(entry["size"]) for entry in entries),
            "entryDigest": _digest(entries),
        }
        document["exportDigest"] = _digest({k: v for k, v in document.items() if k != "exportDigest"})
        _publish(output, document)
    return document


def verify_export(manifest_path: Path, source_root: Path) -> dict[str, Any]:
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    if document.get("schema") != EXPORT_SCHEMA:
        raise CutoverError(f"unexpected export schema: {document.get('schema')!r}")
    recorded = document.get("exportDigest")
    recomputed = _digest({k: v for k, v in document.items() if k != "exportDigest"})
    if recorded != recomputed:
        raise CutoverError("export digest does not match its own contents")
    actual = _inventory(source_root)
    if actual != document["entries"]:
        raise CutoverError("store no longer matches the export inventory")
    return {"storeId": document["storeId"], "entries": len(actual), "entryDigest": document["entryDigest"]}


def snapshot_store(source_root: Path, snapshot_root: Path, *, fence: bool = True) -> dict[str, Any]:
    """Copy the destination aside so a failed import can be rolled back."""
    with _fence(source_root, enabled=fence):
        if snapshot_root.exists():
            raise CutoverError(f"snapshot destination already exists: {snapshot_root}")
        entries = _inventory(source_root)
        if source_root.is_dir():
            shutil.copytree(source_root, snapshot_root)
        document = {
            "schema": SNAPSHOT_SCHEMA,
            "entries": entries,
            "entryDigest": _digest(entries),
            "source": str(source_root),
        }
        document["snapshotDigest"] = _digest({k: v for k, v in document.items() if k != "snapshotDigest"})
        _publish(snapshot_root.parent / (snapshot_root.name + ".manifest.json"), document)
    return document


def import_store(manifest_path: Path, source_root: Path, destination_root: Path, *, fence: bool = True) -> dict[str, Any]:
    """Materialise an export into an empty destination and verify the result.

    Refuses when the destination already holds entries: an import is a transfer of
    authority, never a merge. That is what keeps the operation reversible — the
    destination either becomes exactly the exported state or is untouched.
    """
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    if document.get("schema") != EXPORT_SCHEMA:
        raise CutoverError(f"unexpected export schema: {document.get('schema')!r}")
    with _fence(destination_root, enabled=fence):
        if _inventory(destination_root):
            raise CutoverError("refusing to import into a destination that already holds state")
        for entry in document["entries"]:
            target = destination_root / entry["path"]
            if entry["kind"] == "directory":
                target.mkdir(parents=True, exist_ok=True)
                continue
            source = source_root / entry["path"]
            if not source.is_file():
                raise CutoverError(f"export references a missing source file: {entry['path']}")
            if _file_digest(source) != entry["sha256"]:
                raise CutoverError(f"source changed since the export: {entry['path']}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        imported = _inventory(destination_root)
        if imported != document["entries"]:
            raise CutoverError("imported store does not match the export inventory")
    return {"storeId": document["storeId"], "entries": len(imported), "entryDigest": _digest(imported)}


def rollback_store(snapshot_root: Path, destination_root: Path, *, fence: bool = True) -> dict[str, Any]:
    """Restore a snapshot over the destination, verifying the restored inventory."""
    manifest = snapshot_root.parent / (snapshot_root.name + ".manifest.json")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    if document.get("schema") != SNAPSHOT_SCHEMA:
        raise CutoverError(f"unexpected snapshot schema: {document.get('schema')!r}")
    recorded = document.get("snapshotDigest")
    if recorded != _digest({k: v for k, v in document.items() if k != "snapshotDigest"}):
        raise CutoverError("snapshot digest does not match its own contents")
    with _fence(destination_root, enabled=fence):
        if destination_root.exists():
            shutil.rmtree(destination_root)
        shutil.copytree(snapshot_root, destination_root)
        restored = _inventory(destination_root)
        if restored != document["entries"]:
            raise CutoverError("restored store does not match the snapshot inventory")
    return {"entries": len(restored), "entryDigest": _digest(restored)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline fenced export/import of the S0 workspace stores")
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export")
    p_export.add_argument("--store", required=True)
    p_export.add_argument("--source", required=True, type=Path)
    p_export.add_argument("--output", required=True, type=Path)
    p_export.add_argument("--transfer-id", required=True)
    p_export.add_argument("--no-fence", action="store_true")

    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--manifest", required=True, type=Path)
    p_verify.add_argument("--source", required=True, type=Path)

    p_snapshot = sub.add_parser("snapshot")
    p_snapshot.add_argument("--source", required=True, type=Path)
    p_snapshot.add_argument("--snapshot", required=True, type=Path)
    p_snapshot.add_argument("--no-fence", action="store_true")

    p_import = sub.add_parser("import")
    p_import.add_argument("--manifest", required=True, type=Path)
    p_import.add_argument("--source", required=True, type=Path)
    p_import.add_argument("--destination", required=True, type=Path)
    p_import.add_argument("--no-fence", action="store_true")

    p_rollback = sub.add_parser("rollback")
    p_rollback.add_argument("--snapshot", required=True, type=Path)
    p_rollback.add_argument("--destination", required=True, type=Path)
    p_rollback.add_argument("--no-fence", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            result = export_store(
                args.store, args.source, args.output,
                transfer_id=args.transfer_id, fence=not args.no_fence,
            )
        elif args.command == "verify":
            result = verify_export(args.manifest, args.source)
        elif args.command == "snapshot":
            result = snapshot_store(args.source, args.snapshot, fence=not args.no_fence)
        elif args.command == "import":
            result = import_store(
                args.manifest, args.source, args.destination, fence=not args.no_fence
            )
        else:
            result = rollback_store(args.snapshot, args.destination, fence=not args.no_fence)
    except (CutoverError, OSError, json.JSONDecodeError) as exc:
        print(f"workspace store cutover FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

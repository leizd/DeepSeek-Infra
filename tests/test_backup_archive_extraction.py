"""Selective archive extraction contracts (4.4.13 projected restore)."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_pack, backups


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_zip(root: Path, archive_path: Path) -> None:
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            archive.write(path, path.relative_to(root).as_posix())


def _full_package(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    root = tmp_path / "full-tree"
    a = b"selected content"
    b = b"unselected content"
    files = [
        {"contributorId": "projects", "path": "payload/projects/p1/a.bin", "size": len(a), "sha256": _sha(a)},
        {"contributorId": "projects", "path": "payload/projects/p2/b.bin", "size": len(b), "sha256": _sha(b)},
    ]
    manifest: dict[str, Any] = {
        "schemaVersion": backups.BACKUP_SCHEMA,
        "purpose": backups.PACKAGE_PURPOSE,
        "snapshotKind": "full",
        "files": files,
    }
    (root / "payload/projects/p1").mkdir(parents=True)
    (root / "payload/projects/p2").mkdir(parents=True)
    (root / "payload/projects/p1/a.bin").write_bytes(a)
    (root / "payload/projects/p2/b.bin").write_bytes(b)
    (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    (root / "checksums.sha256").write_text("", encoding="utf-8")
    archive_path = tmp_path / "F0.dsibackup"
    _write_zip(root, archive_path)
    return archive_path, manifest


def _incremental_package(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "inc-tree"
    standalone = b"standalone blob"
    pack_payload = b"packed payload"
    pack_ref = {"kind": "standalone", "path": "payload/files/000000"}
    writer = backup_pack.PackWriter(root)
    blob_ref = writer.append(io.BytesIO(pack_payload), expected_length=len(pack_payload), expected_sha256=_sha(pack_payload))
    writer.finalize()
    (root / "payload/files").mkdir(parents=True)
    (root / "payload/files/000000").write_bytes(standalone)
    operations = {
        "put": [
            {
                "contributorId": "projects",
                "path": "payload/projects/p1/c.bin",
                "size": len(pack_payload),
                "sha256": _sha(pack_payload),
                "storage": "whole",
                "payloadRef": blob_ref,
            },
            {
                "contributorId": "projects",
                "path": "payload/projects/p1/d.bin",
                "size": len(standalone),
                "sha256": _sha(standalone),
                "storage": "whole",
                "payloadRef": pack_ref,
            },
        ],
        "delete": [],
    }
    (root / "delta").mkdir()
    (root / "delta/operations.json").write_text(json.dumps(operations, sort_keys=True), encoding="utf-8")
    index_path = root / backup_pack.PACK_INDEX_PATH
    delta_files = [
        {"path": "delta/operations.json", "size": len(json.dumps(operations).encode()), "sha256": _sha(json.dumps(operations).encode())},
        {"path": "payload/files/000000", "size": len(standalone), "sha256": _sha(standalone)},
        {"path": "payload/packs/0000.pack", "size": (root / "payload/packs/0000.pack").stat().st_size, "sha256": _sha((root / "payload/packs/0000.pack").read_bytes())},
        {"path": "payload/packs/index.json", "size": index_path.stat().st_size, "sha256": _sha(index_path.read_bytes())},
    ]
    manifest: dict[str, Any] = {
        "schemaVersion": backups.BACKUP_SCHEMA,
        "purpose": backups.PACKAGE_PURPOSE,
        "snapshotKind": "incremental",
        "files": [
            {"contributorId": "projects", "path": "payload/projects/p1/c.bin", "size": len(pack_payload), "sha256": _sha(pack_payload)},
        ],
        "deltaFiles": delta_files,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    (root / "checksums.sha256").write_text("", encoding="utf-8")
    archive_path = tmp_path / "I1.dsibackup"
    _write_zip(root, archive_path)
    return archive_path, manifest


def test_extract_archive_metadata_writes_no_payload(tmp_path: Path) -> None:
    archive, manifest = _full_package(tmp_path)
    destination = tmp_path / "meta"
    result = backups.extract_archive_metadata(archive, destination)
    assert result["snapshotKind"] == "full"
    extracted = {path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()}
    assert extracted == {"manifest.json", "checksums.sha256"}
    assert not (destination / "payload").exists()


def test_extract_selected_full_package_only_required_entries(tmp_path: Path) -> None:
    archive, manifest = _full_package(tmp_path)
    destination = tmp_path / "selected-full"
    backups.extract_selected_archive(
        archive,
        destination,
        needed_full={"payload/projects/p1/a.bin"},
        needed_packs=set(),
        needed_standalone=set(),
        manifest=manifest,
    )
    assert (destination / "payload/projects/p1/a.bin").read_bytes() == b"selected content"
    assert not (destination / "payload/projects/p2").exists()
    assert (destination / "manifest.json").is_file()


def test_extract_selected_incremental_package_packs_and_standalone(tmp_path: Path) -> None:
    archive, manifest = _incremental_package(tmp_path)
    destination = tmp_path / "selected-inc"
    backups.extract_selected_archive(
        archive,
        destination,
        needed_full=set(),
        needed_packs={"payload/packs/0000.pack"},
        needed_standalone={"payload/files/000000"},
        manifest=manifest,
    )
    assert (destination / "payload/files/000000").read_bytes() == b"standalone blob"
    assert (destination / "payload/packs/0000.pack").is_file()
    assert (destination / "delta/operations.json").is_file()
    assert (destination / "payload/packs/index.json").is_file()


def test_extract_selected_missing_entry_fails(tmp_path: Path) -> None:
    archive, manifest = _full_package(tmp_path)
    destination = tmp_path / "missing"
    with pytest.raises(AppError, match="missing required entries"):
        backups.extract_selected_archive(
            archive,
            destination,
            needed_full={"payload/projects/p3/missing.bin"},
            needed_packs=set(),
            needed_standalone=set(),
            manifest=manifest,
        )


def test_extract_selected_checksum_mismatch_fails(tmp_path: Path) -> None:
    archive, manifest = _full_package(tmp_path)
    (tmp_path / "full-tree/payload/projects/p1/a.bin").write_bytes(b"tampered")
    tampered_archive = tmp_path / "F0-tampered.dsibackup"
    _write_zip(tmp_path / "full-tree", tampered_archive)
    destination = tmp_path / "corrupt"
    with pytest.raises(AppError, match="failed checksum"):
        backups.extract_selected_archive(
            tampered_archive,
            destination,
            needed_full={"payload/projects/p1/a.bin"},
            needed_packs=set(),
            needed_standalone=set(),
            manifest=manifest,
        )


def test_extract_selected_rejects_traversal_entries(tmp_path: Path) -> None:
    archive, _ = _full_package(tmp_path)
    traversal = tmp_path / "traversal"
    with zipfile.ZipFile(traversal, "w") as zf:
        zf.writestr("manifest.json", "{}")
        zf.writestr("checksums.sha256", "")
        zf.writestr("../escape.bin", b"evil")
    destination = tmp_path / "escape-dest"
    with pytest.raises(AppError):
        backups.extract_selected_archive(
            traversal,
            destination,
            needed_full=set(),
            needed_packs=set(),
            needed_standalone=set(),
            manifest={"schemaVersion": backups.BACKUP_SCHEMA, "purpose": backups.PACKAGE_PURPOSE, "snapshotKind": "full"},
        )

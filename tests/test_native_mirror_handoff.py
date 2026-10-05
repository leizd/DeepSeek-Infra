"""Offline mirror inventory handoff on isolated data copies.

The opaque fixture bytes exercise inventory integrity, not Age encryption.
The native import integration separately verifies real Age decryption.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.native_runtime.authority import PythonWriterMechanicallyDeniedError
from deepseek_infra.infra.workspace import backup_mirror
from scripts import native_mirror_handoff


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _source(root: Path) -> Path:
    profile = root / "mirror_main"
    generation = profile / "generations" / "gen_12345678"
    generation.mkdir(parents=True)
    ciphertext = b"opaque mirror inventory unit fixture"
    digest = hashlib.sha256(ciphertext).hexdigest()
    (generation / "state.12345678.age").write_bytes(ciphertext)
    _write_json(generation / "metadata.json", {
        "schemaVersion": 2, "profileId": "mirror_main", "generationId": "gen_12345678",
        "ciphertextSha256": digest, "recipientVariants": [{
            "filename": "state.12345678.age", "ciphertextSha256": digest,
            "recipientSetDigest": "a" * 64, "creationVerified": True,
        }],
    })
    _write_json(profile / "HEAD.json", {"schemaVersion": 2, "generationId": "gen_12345678"})
    (profile / "previous").mkdir()
    return root


def _export(source: Path, target: Path, output: Path, **kwargs: Any) -> dict[str, Any]:
    return native_mirror_handoff.export_and_fence(source, target, "transfer-1", output, **kwargs)


def test_export_freezes_inventory_and_replays_without_writing_a_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source(tmp_path / "source" / ".backup-mirror")
    target = tmp_path / "target" / ".backup-mirror"
    output = tmp_path / "export.json"
    manifest = _export(source, target, output)
    assert manifest["schema"] == "python-mirror-inventory-export-v1"
    assert manifest["domain"] == "frontend_mirror_store"
    assert manifest["sourceRoot"] == source.resolve().as_posix()
    assert manifest["targetRoot"] == target.resolve().as_posix()
    rows = {item["path"]: item for item in manifest["entries"]}
    assert rows["mirror_main/previous"] == {"path": "mirror_main/previous", "kind": "directory", "size": 0, "sha256": ""}
    path = "mirror_main/generations/gen_12345678/state.12345678.age"
    assert rows[path]["sha256"] == hashlib.sha256((source / path).read_bytes()).hexdigest()
    fence = backup_mirror.mirror_handoff_fence_path(source)
    assert json.loads(fence.read_text(encoding="utf-8")) == manifest
    assert json.loads(output.read_text(encoding="utf-8")) == manifest
    before = (fence.read_bytes(), output.read_bytes())
    assert _export(source, target, output) == manifest
    assert (fence.read_bytes(), output.read_bytes()) == before
    assert not target.exists()
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_authoritative")
    monkeypatch.setattr(backup_mirror, "BACKUP_MIRROR_DIR", source)
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="persistently fenced"):
        backup_mirror.put_frontend_mirror("mirror_main", {}, source_epoch="epoch-2", recipients=[])


@pytest.mark.parametrize("damage", ["ciphertext", "head", "metadata", "unfinished", "unknown"])
def test_invalid_or_unsettled_source_is_not_fenced(tmp_path: Path, damage: str) -> None:
    source = _source(tmp_path / "source" / ".backup-mirror")
    generation = source / "mirror_main/generations/gen_12345678"
    if damage == "ciphertext":
        (generation / "state.12345678.age").write_bytes(b"tampered")
    elif damage == "head":
        _write_json(source / "mirror_main/HEAD.json", {"schemaVersion": 2, "generationId": "gen_abcdef00"})
    elif damage == "metadata":
        (generation / "metadata.json").write_text("partial metadata", encoding="utf-8")
    elif damage == "unfinished":
        (generation / ".state.12345678.age.12.tmp").write_bytes(b"partial")
    else:
        (source / "unknown-runtime-state").write_bytes(b"must not silently omit data")
    with pytest.raises(ValueError):
        _export(source, tmp_path / "target/.backup-mirror", tmp_path / "export.json")
    assert not backup_mirror.mirror_handoff_fence_path(source).exists()


def test_missing_inventory_is_not_fabricated_as_an_empty_export(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="source"):
        _export(tmp_path / "absent", tmp_path / "target", tmp_path / "export.json", allow_empty=True)
    source = tmp_path / "empty"
    source.mkdir()
    with pytest.raises(ValueError, match="empty"):
        _export(source, tmp_path / "target", tmp_path / "export.json")
    manifest = _export(source, tmp_path / "target", tmp_path / "export.json", allow_empty=True)
    assert manifest["entries"] == []
    assert manifest["emptyInventory"] is True


def test_output_publication_crash_keeps_fence_and_retry_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source(tmp_path / "source/.backup-mirror")
    target = tmp_path / "target/.backup-mirror"
    output = tmp_path / "export.json"
    publish = native_mirror_handoff._publish_document

    def fail_output(path: Path, data: bytes) -> None:
        if path == output:
            raise OSError("simulated output disk failure after source fencing")
        publish(path, data)

    monkeypatch.setattr(native_mirror_handoff, "_publish_document", fail_output)
    with pytest.raises(OSError, match="disk failure"):
        _export(source, target, output)
    fence = backup_mirror.mirror_handoff_fence_path(source)
    assert fence.is_file() and not output.exists()
    before = fence.read_bytes()
    monkeypatch.setattr(native_mirror_handoff, "_publish_document", publish)
    manifest = _export(source, target, output)
    assert fence.read_bytes() == before
    assert json.loads(output.read_text(encoding="utf-8")) == manifest


@pytest.mark.parametrize("change", ["source", "transfer", "target"])
def test_existing_fence_cannot_be_rebound_or_replaced(tmp_path: Path, change: str) -> None:
    source = _source(tmp_path / "source/.backup-mirror")
    target = tmp_path / "target/.backup-mirror"
    output = tmp_path / "export.json"
    _export(source, target, output)
    fence = backup_mirror.mirror_handoff_fence_path(source)
    before = fence.read_bytes()
    transfer_id = "transfer-1"
    if change == "source":
        (source / "mirror_main/HEAD.json").write_text("{}", encoding="utf-8")
    elif change == "transfer":
        transfer_id = "transfer-2"
    else:
        target = tmp_path / "other-target/.backup-mirror"
    with pytest.raises(ValueError):
        native_mirror_handoff.export_and_fence(source, target, transfer_id, output)
    assert fence.read_bytes() == before


@pytest.mark.parametrize("overlap", ["same", "descendant", "ancestor", "source-output", "target-output"])
def test_transfer_paths_must_be_independent(tmp_path: Path, overlap: str) -> None:
    source = _source(tmp_path / "source/.backup-mirror")
    target = tmp_path / "target/.backup-mirror"
    output = tmp_path / "export.json"
    if overlap == "same":
        target = source
    elif overlap == "descendant":
        target = source / "native-target"
    elif overlap == "ancestor":
        target = source.parent
    elif overlap == "source-output":
        output = source / "manifest.json"
    else:
        output = target / "manifest.json"
    with pytest.raises(ValueError, match="independent"):
        _export(source, target, output)
    assert not backup_mirror.mirror_handoff_fence_path(source).exists()


def test_existing_unrelated_output_is_preserved_without_fencing_source(tmp_path: Path) -> None:
    source = _source(tmp_path / "source/.backup-mirror")
    output = tmp_path / "export.json"
    output.write_bytes(b"unrelated evidence")
    with pytest.raises(ValueError):
        _export(source, tmp_path / "target/.backup-mirror", output)
    assert output.read_bytes() == b"unrelated evidence"
    assert not backup_mirror.mirror_handoff_fence_path(source).exists()

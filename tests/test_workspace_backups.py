from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backups


def _mutate_archive(source: Path, target: Path, mutate: object) -> None:
    with zipfile.ZipFile(source) as package:
        entries = [(info, package.read(info)) for info in package.infolist()]
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for info, raw in entries:
            replacement = mutate(info.filename, raw)  # type: ignore[operator]
            if replacement is not None:
                package.writestr(info, replacement)


def _frontend_envelope() -> dict[str, object]:
    body: dict[str, object] = {
        "schemaVersion": 1,
        "sourceVersion": config.APP_VERSION,
        "createdAt": 1,
        "conversations": [{"conversationId": "c1", "headRevision": "r1", "checkpoint": {"messages": []}}],
        "conflicts": [],
    }
    body["digest"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body


def test_backup_round_trip_and_manifest_integrity(tmp_settings: Path) -> None:
    project = config.PROJECTS_DIR / "proj-a"
    project.mkdir(parents=True)
    (project / "project.json").write_text('{"id":"proj-a","name":"A"}', encoding="utf-8")
    config.MEMORY_DIR.mkdir(parents=True)
    config.MEMORY_FILE.write_text('{"items":[{"id":"m1","text":"remember"}]}', encoding="utf-8")

    created = backups.create_backup(
        {"mode": "full", "includeHistory": False, "includeDrafts": True},
        frontend_state=_frontend_envelope(),
    )
    archive = backups.backup_path(str(created["backupId"]))
    assert archive.suffix == ".dsibackup"
    with zipfile.ZipFile(archive) as package:
        manifest = json.loads(package.read("manifest.json"))
        assert manifest["purpose"] == "restorable-backup"
        assert manifest["encrypted"] is False
        assert "payload/projects/proj-a/project.json" in {item["path"] for item in manifest["files"]}
        assert "frontend/state.json" in {item["path"] for item in manifest["files"]}

    plan = backups.inspect_archive(archive)
    assert plan["compatible"] is True
    assert plan["requiresFrontendApply"] is True


def test_export_zip_and_tampered_backup_cannot_restore(tmp_settings: Path, tmp_path: Path) -> None:
    fake_export = tmp_path / "share.zip"
    with zipfile.ZipFile(fake_export, "w") as package:
        package.writestr("metadata.json", '{"schemaVersion":"workspace-export.v1"}')
    with pytest.raises(AppError, match="manifest"):
        backups.inspect_archive(fake_export)

    config.MEMORY_DIR.mkdir(parents=True)
    config.MEMORY_FILE.write_text('{"items":[]}', encoding="utf-8")
    created = backups.create_backup({"mode": "full"})
    archive = backups.backup_path(str(created["backupId"]))
    tampered = tmp_path / "tampered.dsibackup"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            raw = source.read(info)
            if info.filename == "payload/memory/memories.json":
                raw += b"x"
            target.writestr(info, raw)
    with pytest.raises(AppError, match="checksum"):
        backups.inspect_archive(tampered)


def test_archive_traversal_and_secret_frontend_state_are_rejected(tmp_settings: Path, tmp_path: Path) -> None:
    traversal = tmp_path / "traversal.dsibackup"
    with zipfile.ZipFile(traversal, "w") as package:
        package.writestr("../outside", "bad")
    with pytest.raises(AppError, match="unsafe path"):
        backups.inspect_archive(traversal)

    created = backups.create_session({"mode": "full"})
    envelope = _frontend_envelope()
    envelope["apiKey"] = "secret"
    body = {key: value for key, value in envelope.items() if key != "digest"}
    envelope["digest"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(AppError, match="credentials"):
        backups.put_frontend_state(str(created["backupId"]), envelope)


def test_inspect_is_read_only_and_replace_empty_is_guarded(tmp_settings: Path) -> None:
    config.PROJECTS_DIR.mkdir(parents=True)
    (config.PROJECTS_DIR / "source.json").write_text('{"value":1}', encoding="utf-8")
    created = backups.create_backup({"mode": "full"})
    archive = backups.backup_path(str(created["backupId"]))
    before = (config.PROJECTS_DIR / "source.json").read_bytes()
    plan = backups.inspect_archive(archive)
    assert (config.PROJECTS_DIR / "source.json").read_bytes() == before
    with pytest.raises(AppError, match="not empty"):
        backups.apply_restore(str(plan["restoreId"]), mode="replace-empty")


def test_project_collision_is_deterministically_remapped_with_references(tmp_settings: Path) -> None:
    project = config.PROJECTS_DIR / "proj-source"
    project.mkdir(parents=True)
    (project / "project.json").write_text(
        '{"id":"proj-source","projectId":"proj-source","sourceRef":{"projectId":"proj-source"}}',
        encoding="utf-8",
    )
    created = backups.create_backup({"mode": "full"})
    archive = backups.backup_path(str(created["backupId"]))
    (project / "project.json").write_text('{"id":"proj-source","name":"new local value"}', encoding="utf-8")

    plan = backups.inspect_archive(archive)
    restored = backups.apply_restore(str(plan["restoreId"]), mode="merge")
    identities = restored["restoredIdentities"]
    assert len(identities) == 1
    restored_id = identities[0]["restoredId"]
    imported = json.loads((config.PROJECTS_DIR / restored_id / "project.json").read_text(encoding="utf-8"))
    assert imported["id"] == restored_id
    assert imported["sourceRef"]["projectId"] == restored_id
    assert imported["importedFrom"]["originalId"] == "proj-source"


def test_project_scope_secret_exclusion_and_session_lifecycle(tmp_settings: Path) -> None:
    for project_id in ("proj-a", "proj-b"):
        root = config.PROJECTS_DIR / project_id
        root.mkdir(parents=True)
        (root / "project.json").write_text(
            json.dumps({"id": project_id, "artifact": {"path": f".generated/{project_id}/result.md"}}),
            encoding="utf-8",
        )
        artifact = config.GENERATED_DIR / project_id / "result.md"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(project_id, encoding="utf-8")
    export = config.GENERATED_DIR / "workspace-exports" / "share.zip"
    export.parent.mkdir(parents=True)
    export.write_bytes(b"not a restorable artifact")
    (config.PROJECTS_DIR / "proj-a" / ".env").write_text("API_KEY=nope", encoding="utf-8")
    (config.PROJECTS_DIR / "proj-a" / "active.lock").write_text("1", encoding="utf-8")
    created = backups.create_session({"mode": "project", "projectId": "proj-a", "requiresFrontendState": False})
    assert backups.get_session(str(created["backupId"]))["phase"] == "preparing"
    with pytest.raises(AppError, match="not ready"):
        backups.backup_path(str(created["backupId"]))
    ready = backups.finalize_session(str(created["backupId"]))
    archive = backups.backup_path(str(created["backupId"]))
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
    assert any("proj-a/project.json" in name for name in names)
    assert "payload/artifacts/proj-a/result.md" in names
    assert not any("proj-b" in name or ".env" in name or name.endswith(".lock") for name in names)
    assert not any("workspace-exports" in name for name in names)
    assert ready["phase"] == "ready"
    assert backups.delete_backup(str(created["backupId"])) is True
    assert not archive.exists()
    with pytest.raises(AppError, match="not found"):
        backups.get_session(str(created["backupId"]))


def test_backup_input_and_frontend_validation_failures(tmp_settings: Path) -> None:
    assert "artifacts" in backups.capabilities()["includedByDefault"]
    with pytest.raises(AppError, match="mode"):
        backups.create_session({"mode": "other"})
    with pytest.raises(AppError, match="project id"):
        backups.create_session({"mode": "project"})
    with pytest.raises(AppError, match="Invalid backup id"):
        backups.get_session("../bad")
    with pytest.raises(AppError, match="not found"):
        backups.apply_restore("restore_missing")
    with pytest.raises(AppError, match="Unsupported restore mode"):
        backups.apply_restore("restore_missing", mode="overwrite")

    created = backups.create_session({"mode": "full"})
    backup_id = str(created["backupId"])
    with pytest.raises(AppError, match="required"):
        backups.finalize_session(backup_id)
    envelope = _frontend_envelope()
    envelope["schemaVersion"] = 2
    with pytest.raises(AppError, match="schema"):
        backups.put_frontend_state(backup_id, envelope)
    envelope = _frontend_envelope()
    envelope["digest"] = "bad"
    with pytest.raises(AppError, match="digest"):
        backups.put_frontend_state(backup_id, envelope)


def test_session_failure_limits_and_restore_metadata_errors(
    tmp_settings: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = backups.create_session({"mode": "full", "requiresFrontendState": False})
    original_build_archive = backups._build_archive
    monkeypatch.setattr(backups, "_build_archive", lambda *_args: (_ for _ in ()).throw(RuntimeError("snapshot failed")))
    with pytest.raises(RuntimeError, match="snapshot failed"):
        backups.finalize_session(str(failed["backupId"]))
    assert backups.get_session(str(failed["backupId"]))["phase"] == "failed"
    monkeypatch.setattr(backups, "_build_archive", original_build_archive)

    ready = backups.create_backup({"mode": "full"}, frontend_state=_frontend_envelope())
    with pytest.raises(AppError, match="no longer accepts"):
        backups.put_frontend_state(str(ready["backupId"]), _frontend_envelope())
    session_path = backups._session_dir(str(ready["backupId"])) / "session.json"
    session = backups._read_json(session_path)
    session["path"] = str(tmp_path / "outside.dsibackup")
    backups._write_json(session_path, session)
    with pytest.raises(AppError, match="not found"):
        backups.backup_path(str(ready["backupId"]))

    monkeypatch.setattr(backups, "MAX_ARCHIVE_BYTES", 0)
    with pytest.raises(AppError, match="too large"):
        backups.inspect_archive(b"x")
    source = tmp_path / "source.dsibackup"
    source.write_bytes(b"x")
    with pytest.raises(AppError, match="too large"):
        backups.inspect_archive(source)
    with pytest.raises(AppError, match="Invalid restore id"):
        backups._restore_root("../bad")


def test_contributor_collision_and_project_artifact_reference_helpers(tmp_settings: Path, tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "old.bin").write_bytes(b"incoming")
    (destination / "new.bin").write_bytes(b"existing")
    contributor = backups.DirectoryContributor("sample", 1, "durable", "merge", lambda: destination)
    contributor.apply_restore(
        {
            "source": str(source),
            "destination": str(destination),
            "mode": "merge",
            "identityMap": {"old": "new"},
        },
        backups.BackupContext(),
    )
    assert (destination / "new.bin").read_bytes() == b"existing"
    assert len(list(destination.glob("new.imported-*.bin"))) == 1
    (source / "same.txt").write_text("same", encoding="utf-8")
    (destination / "same.txt").write_text("same", encoding="utf-8")
    contributor.apply_restore(
        {"source": str(source), "destination": str(destination), "mode": "merge"},
        backups.BackupContext(),
    )

    generated = config.GENERATED_DIR / "nested" / "result.md"
    generated.parent.mkdir(parents=True)
    generated.write_text("artifact", encoding="utf-8")
    project = config.PROJECTS_DIR / "proj-helper"
    project.mkdir(parents=True)
    (project / "bad.json").write_text("{", encoding="utf-8")
    (project / "project.json").write_text(
        json.dumps({"refs": [str(generated), ".generated/nested/result.md", "not-a-path"]}),
        encoding="utf-8",
    )
    context = backups.BackupContext(mode="project", project_ids=("missing", "proj-helper"))
    assert backups._project_artifact_paths(context) == {"nested/result.md"}


def test_archive_manifest_security_variants(tmp_settings: Path, tmp_path: Path) -> None:
    config.MEMORY_DIR.mkdir(parents=True)
    config.MEMORY_FILE.write_text('{"items":[]}', encoding="utf-8")
    created = backups.create_backup({"mode": "full"})
    source = backups.backup_path(str(created["backupId"]))

    def manifest_mutator(change: object) -> Path:
        target = tmp_path / f"mutated-{os.urandom(3).hex()}.dsibackup"

        def mutate(name: str, raw: bytes) -> bytes:
            if name != "manifest.json":
                return raw
            value = json.loads(raw)
            change(value)  # type: ignore[operator]
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

        _mutate_archive(source, target, mutate)
        return target

    with pytest.raises(AppError, match="supported restorable"):
        backups.inspect_archive(manifest_mutator(lambda value: value.update({"purpose": "share-export"})))
    with pytest.raises(AppError, match="inventory"):
        backups.inspect_archive(manifest_mutator(lambda value: value.update({"files": {}})))
    with pytest.raises(AppError, match="duplicate"):
        backups.inspect_archive(manifest_mutator(lambda value: value["files"].append(value["files"][0])))

    undeclared = tmp_path / "undeclared.dsibackup"
    with zipfile.ZipFile(source) as package, zipfile.ZipFile(undeclared, "w") as target:
        for info in package.infolist():
            target.writestr(info, package.read(info))
        target.writestr("payload/memory/extra.json", "{}")
    with pytest.raises(AppError, match="undeclared"):
        backups.inspect_archive(undeclared)

    bad_manifest_digest = tmp_path / "manifest-digest.dsibackup"
    _mutate_archive(
        source,
        bad_manifest_digest,
        lambda name, raw: b"0" * len(raw) if name == "checksums.sha256" else raw,
    )
    with pytest.raises(AppError, match="Manifest digest"):
        backups.inspect_archive(bad_manifest_digest)

    duplicate = tmp_path / "duplicate.dsibackup"
    with zipfile.ZipFile(duplicate, "w") as package:
        package.writestr("manifest.json", "{}")
        package.writestr("MANIFEST.JSON", "{}")
    with pytest.raises(AppError, match="duplicate"):
        backups.inspect_archive(duplicate)

    link = tmp_path / "link.dsibackup"
    link_info = zipfile.ZipInfo("link")
    link_info.external_attr = (0o120777 << 16)
    with zipfile.ZipFile(link, "w") as package:
        package.writestr(link_info, "target")
    with pytest.raises(AppError, match="link or special"):
        backups.inspect_archive(link)


def test_restore_tamper_rollback_helpers_and_sqlite_snapshot(
    tmp_settings: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.MEMORY_DIR.mkdir(parents=True)
    config.MEMORY_FILE.write_text('{"items":[]}', encoding="utf-8")
    created = backups.create_backup({"mode": "full"})
    archive = backups.backup_path(str(created["backupId"]))
    plan = backups.inspect_archive(archive)
    restore_root = backups.RESTORE_DIR / str(plan["restoreId"])
    stored = next(restore_root.glob("*.dsibackup"))
    with stored.open("ab") as output:
        output.write(b"x")
    with pytest.raises(AppError, match="changed"):
        backups.apply_restore(str(plan["restoreId"]))

    for unsafe in ("", "../x", "/absolute", "C:/drive", "bad\\path", "x" * 241):
        with pytest.raises(AppError, match="unsafe path"):
            backups._validate_archive_path(unsafe)
    assert backups._safe_archive_name("backup.zip").endswith(".dsibackup")
    with pytest.raises(AppError, match="deeply nested"):
        nested: object = {}
        for _ in range(66):
            nested = {"x": nested}
        backups._check_json_depth(nested)
    assert backups._rewrite_json_references(b"not-json", {"a": "b"}, "backup") == b"not-json"
    assert backups._rewrite_json_references(b'["a",1]', {"a": "b"}, "backup") == b'["b",1]'
    assert backups._merge_json_payload(b"{", b'{"incoming":true}') == b'{"incoming":true}'

    monkeypatch.setenv("DEEPSEEK_BUILD_REVISION", "revision-test")
    assert backups._build_revision() == "revision-test"

    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{", encoding="utf-8")
    with pytest.raises(AppError, match="Invalid backup metadata"):
        backups._read_json(invalid_json)
    invalid_json.write_text("[]", encoding="utf-8")
    with pytest.raises(AppError, match="must be an object"):
        backups._read_json(invalid_json)

    database = tmp_path / "source.sqlite3"
    snapshot = tmp_path / "snapshot.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sample(value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('ok')")
    backups._copy_consistent(database, snapshot)
    with sqlite3.connect(snapshot) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone() == ("ok",)
    broken = tmp_path / "broken.sqlite3"
    broken.write_text("not sqlite", encoding="utf-8")
    with pytest.raises(AppError, match="consistent SQLite"):
        backups._copy_consistent(broken, tmp_path / "broken-copy.sqlite3")


def test_partial_contributor_failure_rolls_back_its_writes(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.MEMORY_DIR.mkdir(parents=True)
    config.MEMORY_FILE.write_text('[{"id":"backup","content":"from backup"}]', encoding="utf-8")
    created = backups.create_backup({"mode": "full"})
    archive = backups.backup_path(str(created["backupId"]))
    config.MEMORY_FILE.write_text('[{"id":"local","content":"keep me"}]', encoding="utf-8")
    plan = backups.inspect_archive(archive)

    original_apply = backups.DirectoryContributor.apply_restore

    def fail_after_write(
        contributor: backups.DirectoryContributor,
        operation: dict[str, object],
        context: backups.BackupContext,
    ) -> None:
        original_apply(contributor, operation, context)
        if contributor.contributor_id == "memory":
            raise RuntimeError("forced partial restore failure")

    monkeypatch.setattr(backups.DirectoryContributor, "apply_restore", fail_after_write)
    with pytest.raises(RuntimeError, match="forced partial"):
        backups.apply_restore(str(plan["restoreId"]), mode="merge")
    assert json.loads(config.MEMORY_FILE.read_text(encoding="utf-8")) == [{"id": "local", "content": "keep me"}]


def test_merge_restore_combines_single_file_stores_and_remaps_media_paths(tmp_settings: Path) -> None:
    config.MEMORY_DIR.mkdir(parents=True)
    config.MEMORY_FILE.write_text('[{"id":"memory-a","content":"from backup"}]', encoding="utf-8")
    config.MEDIA_DIR.mkdir(parents=True)
    (config.MEDIA_DIR / "objects" / "media-a").mkdir(parents=True)
    (config.MEDIA_DIR / "objects" / "media-a" / "source.txt").write_text("backup bytes", encoding="utf-8")
    (config.MEDIA_DIR / "library.json").write_text(
        '{"media":[{"mediaId":"media-a","projectId":"proj-a","title":"backup","path":"objects/media-a/source.txt"}]}',
        encoding="utf-8",
    )
    created = backups.create_backup({"mode": "full"})
    archive = backups.backup_path(str(created["backupId"]))

    config.MEMORY_FILE.write_text('[{"id":"memory-local","content":"local"}]', encoding="utf-8")
    (config.MEDIA_DIR / "objects" / "media-a" / "source.txt").write_text("local bytes", encoding="utf-8")
    (config.MEDIA_DIR / "library.json").write_text(
        '{"media":[{"mediaId":"media-a","projectId":"proj-local","title":"local","path":"objects/media-a/source.txt"}]}',
        encoding="utf-8",
    )
    plan = backups.inspect_archive(archive)
    restored = backups.apply_restore(str(plan["restoreId"]), mode="merge")
    mapping = {item["originalId"]: item["restoredId"] for item in restored["restoredIdentities"]}
    restored_media_id = mapping["media-a"]
    memories = json.loads(config.MEMORY_FILE.read_text(encoding="utf-8"))
    assert {item["content"] for item in memories} == {"local", "from backup"}
    media = json.loads((config.MEDIA_DIR / "library.json").read_text(encoding="utf-8"))["media"]
    assert {item["title"] for item in media} == {"local", "backup"}
    imported = next(item for item in media if item["title"] == "backup")
    assert imported["mediaId"] == restored_media_id
    assert imported["path"] == f"objects/{restored_media_id}/source.txt"
    assert (config.MEDIA_DIR / imported["path"]).read_text(encoding="utf-8") == "backup bytes"

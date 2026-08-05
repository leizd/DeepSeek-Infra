from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import pytest

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backups, mutation_gate


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
    assert backups.delete_backup(str(failed["backupId"])) is True
    monkeypatch.setattr(backups, "_build_archive", original_build_archive)

    ready = backups.create_backup({"mode": "full"}, frontend_state=_frontend_envelope())
    archive = backups.backup_path(str(ready["backupId"]))
    assert backups.inspect_archive(archive.read_bytes())["compatible"] is True
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
    monkeypatch.setattr(backups, "MAX_ARCHIVE_BYTES", 2_000_000_000)
    monkeypatch.setattr(backups, "MAX_ENTRIES", 0)
    with pytest.raises(AppError, match="too many files"):
        backups.inspect_archive(archive)
    monkeypatch.setattr(backups, "MAX_ENTRIES", 10_000)
    monkeypatch.setattr(backups, "MAX_EXPANDED_BYTES", 0)
    with pytest.raises(AppError, match="entry is too large"):
        backups.inspect_archive(archive)
    monkeypatch.setattr(backups, "MAX_EXPANDED_BYTES", 5_000_000_000)
    monkeypatch.setattr(backups, "MAX_COMPRESSION_RATIO", 0)
    with pytest.raises(AppError, match="compression ratio"):
        backups.inspect_archive(archive)
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
    config.FILE_CACHE_DIR.mkdir(parents=True)
    (config.FILE_CACHE_DIR / "unrelated.txt").write_text("cache", encoding="utf-8")
    file_cache = backups.DirectoryContributor("project-files", 1, "durable", "merge", lambda: config.FILE_CACHE_DIR)
    assert file_cache.inventory(context) == {"records": 0, "bytes": 0}

    invalid_identity = tmp_path / "invalid-identity"
    invalid_identity.mkdir()
    (invalid_identity / "bad.json").write_text("{", encoding="utf-8")
    assert backups._json_identity_index(invalid_identity) == {}
    assert backups._restore_identity_map({"operations": [None], "manifest": {}}, "merge") == {}
    assert backups._merge_json_payload(b"[1]", b"[1,2]") == b"[1,2]"
    assert backups._merge_json_payload(b"1", b"2") == b"1"


def test_archive_manifest_security_variants(
    tmp_settings: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    with pytest.raises(AppError, match="inventory"):
        backups.inspect_archive(manifest_mutator(lambda value: value["files"].__setitem__(0, "invalid")))
    with pytest.raises(AppError, match="duplicate"):
        backups.inspect_archive(manifest_mutator(lambda value: value["files"].append(value["files"][0])))

    unsupported_manifest = {
        "schemaVersion": backups.BACKUP_SCHEMA,
        "purpose": backups.PACKAGE_PURPOSE,
        "source": {"version": config.APP_VERSION},
        "scope": {"mode": "full", "projectIds": [], "includeHistory": False, "includeDrafts": False},
        "contributors": [{"id": "unknown"}],
        "files": [],
    }
    with monkeypatch.context() as patch:
        patch.setattr(backups, "_safe_extract_and_verify", lambda *_args: unsupported_manifest)
        with pytest.raises(AppError, match="Unsupported contributor"):
            backups.inspect_archive(source)

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


def test_build_revision_tracks_clean_dirty_and_unavailable_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_BUILD_REVISION", raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)

    def set_results(*results: subprocess.CompletedProcess[object]) -> None:
        pending = iter(results)
        monkeypatch.setattr(backups.subprocess, "run", lambda *_args, **_kwargs: next(pending))

    set_results(subprocess.CompletedProcess(["git"], 1, stdout="", stderr="missing"))
    assert backups._build_revision() == "unknown"

    set_results(
        subprocess.CompletedProcess(["git"], 0, stdout="abc123\n", stderr=""),
        subprocess.CompletedProcess(["git"], 0, stdout=b"", stderr=b""),
    )
    assert backups._build_revision() == "abc123"

    dirty_status = b" M deepseek_infra/infra/workspace/backups.py\n"
    dirty_diff = b"diff --git a/backups.py b/backups.py\n"
    set_results(
        subprocess.CompletedProcess(["git"], 0, stdout="abc123\n", stderr=""),
        subprocess.CompletedProcess(["git"], 0, stdout=dirty_status, stderr=b""),
        subprocess.CompletedProcess(["git"], 0, stdout=dirty_diff, stderr=b""),
    )
    expected = hashlib.sha256(dirty_status + dirty_diff).hexdigest()[:16]
    assert backups._build_revision() == f"abc123-dirty-{expected}"

    set_results(
        subprocess.CompletedProcess(["git"], 0, stdout="abc123\n", stderr=""),
        subprocess.CompletedProcess(["git"], 0, stdout=dirty_status, stderr=b""),
        subprocess.CompletedProcess(["git"], 1, stdout=b"", stderr=b"failed"),
    )
    expected_without_diff = hashlib.sha256(dirty_status).hexdigest()[:16]
    assert backups._build_revision() == f"abc123-dirty-{expected_without_diff}"


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


def test_cross_tier_restore_commit_abort_and_complete_are_idempotent(tmp_settings: Path) -> None:
    config.MEMORY_DIR.mkdir(parents=True)
    config.MEMORY_FILE.write_text('[{"id":"backup","content":"from backup"}]', encoding="utf-8")
    created = backups.create_backup({"mode": "full"}, frontend_state=_frontend_envelope())
    archive = backups.backup_path(str(created["backupId"]))
    config.MEMORY_FILE.write_text('[{"id":"local","content":"before restore"}]', encoding="utf-8")
    before = config.MEMORY_FILE.read_bytes()

    plan = backups.inspect_archive(archive)
    restore_id = str(plan["restoreId"])
    prepared = backups.prepare_restore(
        restore_id,
        mode="merge",
        previous_epoch="legacy",
        target_epoch="epoch-test",
        owner_document_id="document-test",
    )
    assert prepared["phase"] == "backend-staged"
    assert config.MEMORY_FILE.read_bytes() == before
    digest = "d" * 64
    assert backups.frontend_prepared(restore_id, digest=digest)["phase"] == "frontend-staged"
    assert backups.frontend_prepared(restore_id, digest=digest)["phase"] == "frontend-staged"
    assert backups.commit_restore(restore_id, frontend_digest=digest)["phase"] == "commit-intent"
    assert config.MEMORY_FILE.read_bytes() == before

    committed = backups.commit_restore(
        restore_id,
        frontend_committed=True,
        frontend_digest=digest,
    )
    assert committed["phase"] == "backend-committed"
    assert backups.commit_restore(restore_id, frontend_committed=True, frontend_digest=digest)["phase"] == "backend-committed"
    assert {item["content"] for item in json.loads(config.MEMORY_FILE.read_text(encoding="utf-8"))} == {
        "before restore",
        "from backup",
    }
    safety_id = str(committed["safetyBackupId"])
    rolled_back = backups.abort_restore(restore_id)
    assert rolled_back["phase"] == "rolled-back"
    assert config.MEMORY_FILE.read_bytes() == before
    assert backups.backup_path(safety_id).is_file()
    assert backups.abort_restore(restore_id)["phase"] == "rolled-back"

    second = backups.inspect_archive(archive)
    second_id = str(second["restoreId"])
    backups.prepare_restore(second_id, previous_epoch="legacy", target_epoch="epoch-complete")
    backups.frontend_prepared(second_id, digest=digest)
    backups.commit_restore(second_id, frontend_digest=digest)
    backups.commit_restore(second_id, frontend_committed=True, frontend_digest=digest)
    assert backups.complete_restore(second_id, frontend_digest=digest)["phase"] == "complete"
    assert backups.complete_restore(second_id, frontend_digest=digest)["phase"] == "complete"


def test_startup_recovery_rolls_back_interrupted_directory_exchange(tmp_settings: Path) -> None:
    config.MEMORY_DIR.mkdir(parents=True)
    config.MEMORY_FILE.write_text('[{"id":"backup","content":"backup"}]', encoding="utf-8")
    created = backups.create_backup({"mode": "full"})
    archive = backups.backup_path(str(created["backupId"]))
    config.MEMORY_FILE.write_text('[{"id":"local","content":"keep"}]', encoding="utf-8")
    before = config.MEMORY_FILE.read_bytes()
    plan = backups.inspect_archive(archive)
    restore_id = str(plan["restoreId"])
    backups.prepare_restore(restore_id)
    root = backups.RESTORE_DIR / restore_id
    transaction = backups._read_json(root / "transaction.json")
    memory_entry = next(item for item in transaction["contributors"] if item["id"] == "memory")
    destination = Path(memory_entry["destination"])
    rollback = Path(memory_entry["rollbackPath"])
    rollback.parent.mkdir(parents=True, exist_ok=True)
    memory_entry["swapState"] = "moving-old"
    transaction["phase"] = "commit-intent"
    backups._write_json(root / "transaction.json", transaction)
    os.replace(destination, rollback)
    memory_entry["swapState"] = "old-moved"
    backups._write_json(root / "transaction.json", transaction)

    recovered = backups.recover_interrupted_restores()
    assert restore_id in recovered["rolledBack"]
    assert config.MEMORY_FILE.read_bytes() == before
    assert backups.get_restore(restore_id)["phase"] == "rolled-back"


def test_schema_compatibility_and_typed_rewrite_leave_user_content_untouched(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = config.PROJECTS_DIR / "proj-user-text"
    project.mkdir(parents=True)
    (project / "project.json").write_text(
        json.dumps(
            {
                "id": "proj-user-text",
                "projectId": "proj-user-text",
                "prompt": "proj-user-text",
                "message": "notes/proj-user-text/body",
                "sourceRef": {"projectId": "proj-user-text"},
            }
        ),
        encoding="utf-8",
    )
    created = backups.create_backup({"mode": "full"})
    archive = backups.backup_path(str(created["backupId"]))
    (project / "project.json").write_text('{"id":"proj-user-text","name":"local"}', encoding="utf-8")
    plan = backups.inspect_archive(archive)
    restored = backups.apply_restore(str(plan["restoreId"]))
    restored_id = restored["restoredIdentities"][0]["restoredId"]
    imported = json.loads((config.PROJECTS_DIR / restored_id / "project.json").read_text(encoding="utf-8"))
    assert imported["id"] == restored_id
    assert imported["sourceRef"]["projectId"] == restored_id
    assert imported["prompt"] == "proj-user-text"
    assert imported["message"] == "notes/proj-user-text/body"

    original_extract = backups._safe_extract_and_verify

    def future_schema(archive_path: Path, destination: Path) -> dict[str, object]:
        manifest = original_extract(archive_path, destination)
        copied = json.loads(json.dumps(manifest))
        copied["contributors"][0]["schemaVersion"] = 999
        return copied

    with monkeypatch.context() as patch:
        patch.setattr(backups, "_safe_extract_and_verify", future_schema)
        future = backups.inspect_archive(archive)
    assert future["compatible"] is False
    assert future["migrations"][0]["from"] == 999
    with pytest.raises(AppError, match="not compatible"):
        backups.prepare_restore(str(future["restoreId"]))


def test_restore_state_machine_rejects_invalid_transitions_and_digests(tmp_settings: Path) -> None:
    config.MEMORY_DIR.mkdir(parents=True)
    config.MEMORY_FILE.write_text('[{"id":"backup","content":"backup"}]', encoding="utf-8")
    created = backups.create_backup({"mode": "full"}, frontend_state=_frontend_envelope())
    plan = backups.inspect_archive(backups.backup_path(str(created["backupId"])))
    restore_id = str(plan["restoreId"])

    with pytest.raises(AppError, match="Unsupported"):
        backups.prepare_restore(restore_id, mode="unsupported")
    prepared = backups.prepare_restore(restore_id, target_epoch="epoch-errors")
    assert backups.get_restore(restore_id)["phase"] == "backend-staged"
    assert backups.prepare_restore(restore_id, target_epoch="epoch-errors")["serverTransactionDigest"] == prepared["serverTransactionDigest"]
    with pytest.raises(AppError, match="parameters"):
        backups.prepare_restore(restore_id, target_epoch="different")
    with pytest.raises(AppError, match="invalid"):
        backups.frontend_prepared(restore_id, digest="short")
    with pytest.raises(AppError, match="not committed"):
        backups.complete_restore(restore_id)
    with pytest.raises(AppError, match="acknowledgement"):
        backups.apply_restore(restore_id)

    digest = "a" * 64
    backups.frontend_prepared(restore_id, digest=digest)
    with pytest.raises(AppError, match="changed"):
        backups.frontend_prepared(restore_id, digest="b" * 64)
    with pytest.raises(AppError, match="does not match"):
        backups.commit_restore(restore_id, frontend_digest="c" * 64)
    assert backups.commit_restore(restore_id, frontend_digest=digest)["phase"] == "commit-intent"
    assert backups.commit_restore(restore_id, frontend_digest=digest)["phase"] == "commit-intent"
    with pytest.raises(AppError, match="not committed"):
        backups.complete_restore(restore_id, frontend_digest=digest)
    backups.commit_restore(restore_id, frontend_committed=True, frontend_digest=digest)
    with pytest.raises(AppError, match="digest"):
        backups.complete_restore(restore_id, frontend_digest="b" * 64)
    backups.complete_restore(restore_id, frontend_digest=digest)
    with pytest.raises(AppError, match="cannot be aborted"):
        backups.abort_restore(restore_id)


def test_restore_retention_listing_deletion_and_recovery_required(tmp_settings: Path) -> None:
    def write_record(restore_id: str, phase: str, updated_at: object) -> Path:
        root = backups.RESTORE_DIR / restore_id
        root.mkdir(parents=True)
        backups._write_json(
            root / "transaction.json",
            {
                "restoreId": restore_id,
                "phase": phase,
                "updatedAt": updated_at,
                "contributors": [],
                "identityMap": {},
            },
        )
        return root

    old_complete = write_record("restore_oldcomplete", "complete", "2000-01-01T00:00:00+00:00")
    write_record("restore_recent", "complete", "2999-01-01T00:00:00+00:00")
    old_rollback = write_record("restore_oldrollback", "rolled-back", 1)
    active = write_record("restore_active", "backend-staged", "2000-01-01T00:00:00+00:00")
    broken = backups.RESTORE_DIR / "restore_broken"
    broken.mkdir(parents=True)
    (broken / "transaction.json").write_text("{", encoding="utf-8")

    listed = {item["restoreId"]: item for item in backups.list_restores()["restores"]}
    assert listed["restore_broken"]["phase"] == "recovery-required"
    with pytest.raises(AppError, match="Active"):
        backups.delete_restore(active.name)

    fenced = write_record("restore_fenceddelete", "complete", "2000-01-01T00:00:00+00:00")
    mutation_gate.write_fence(
        {
            "schemaVersion": 1,
            "restoreId": fenced.name,
            "phase": "complete",
        },
        backups.RESTORE_DIR.parent,
    )
    with pytest.raises(AppError, match="active fence"):
        backups.delete_restore(fenced.name)
    mutation_gate.clear_fence(fenced.name, backups.RESTORE_DIR.parent)
    assert backups.delete_restore(fenced.name) is True

    cleaned = backups.cleanup_restores(now=2_000_000_000)
    assert set(cleaned["deleted"]) == {old_complete.name, old_rollback.name}
    assert not old_complete.exists()
    assert not old_rollback.exists()
    assert backups._iso_age_seconds("not-a-date", 1) is None
    assert backups._iso_age_seconds(object(), 1) is None


def test_startup_recovery_finishes_verified_exchange_and_fences_corruption(tmp_settings: Path) -> None:
    committed_root = backups.RESTORE_DIR / "restore_installed"
    committed_root.mkdir(parents=True)
    destination = tmp_settings / "installed-destination"
    destination.mkdir()
    (destination / "state.json").write_text('{"ok":true}', encoding="utf-8")
    transaction = {
        "restoreId": committed_root.name,
        "phase": "commit-intent",
        "previousEpoch": "legacy",
        "targetEpoch": "target",
        "ownerDocumentId": "owner",
        "createdAt": 1,
        "expiresAt": 2**63 - 1,
        "contributors": [
            {
                "id": "memory",
                "destination": str(destination),
                "stagedPath": str(committed_root / "staged" / "memory"),
                "rollbackPath": str(committed_root / "rollback" / "memory"),
                "digest": backups._tree_digest(destination),
                "swapped": False,
                "swapState": "installing-staged",
            }
        ],
    }
    backups._write_json(committed_root / "transaction.json", transaction)

    corrupt_root = backups.RESTORE_DIR / "restore_corruptjournal"
    corrupt_root.mkdir(parents=True)
    (corrupt_root / "transaction.json").write_text("{", encoding="utf-8")
    invalid_root = backups.RESTORE_DIR / "restore_invalidcontributor"
    invalid_root.mkdir(parents=True)
    backups._write_json(
        invalid_root / "transaction.json",
        {
            "restoreId": invalid_root.name,
            "phase": "commit-intent",
            "previousEpoch": "legacy",
            "targetEpoch": "target",
            "contributors": ["invalid"],
        },
    )

    recovered = backups.recover_interrupted_restores()
    assert committed_root.name in recovered["backendCommitted"]
    assert {corrupt_root.name, invalid_root.name} <= set(recovered["recoveryRequired"])
    assert backups.get_restore(committed_root.name)["phase"] == "backend-committed"
    fence = mutation_gate.read_fence(backups.RESTORE_DIR.parent)
    assert fence is not None and fence["phase"] == "recovery-required"
    mutation_gate.clear_fence(str(fence["restoreId"]), backups.RESTORE_DIR.parent)


def test_startup_recovery_queries_external_participant_status(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def write_external_transaction(restore_id: str) -> Path:
        root = backups.RESTORE_DIR / restore_id
        root.mkdir(parents=True)
        backups._write_json(
            root / "transaction.json",
            {
                "restoreId": restore_id,
                "phase": "commit-intent",
                "previousEpoch": "legacy",
                "targetEpoch": "target",
                "contributors": [{"id": "stateless-mcp", "external": True, "swapped": False, "phase": "commit-intent"}],
            },
        )
        return root

    def journal(phase: str) -> dict[str, object]:
        return {
            "sourceDigest": "a" * 64,
            "preparedDigest": "b" * 64,
            "phase": phase,
            "imported": 0,
            "skipped": 0,
            "interrupted": 0,
            "remapped": {},
        }

    def clear_active_fence() -> None:
        fence = mutation_gate.read_fence(backups.RESTORE_DIR.parent)
        if fence is not None:
            mutation_gate.clear_fence(str(fence["restoreId"]), backups.RESTORE_DIR.parent)

    committed_root = write_external_transaction("restore_externalinstalled")
    monkeypatch.setattr(
        backups.StatelessMcpContributor,
        "restore_status",
        lambda _self, restore_id: journal("committed-pending-complete"),
    )
    recovered = backups.recover_interrupted_restores()
    assert "restore_externalinstalled" in recovered["backendCommitted"]
    transaction = backups._read_json(committed_root / "transaction.json")
    assert transaction["contributors"][0]["phase"] == "committed-pending-complete"
    clear_active_fence()
    committed_root = write_external_transaction("restore_externalinstalled2")
    monkeypatch.setattr(backups.StatelessMcpContributor, "restore_status", lambda _self, restore_id: journal("complete"))
    assert "restore_externalinstalled2" in backups.recover_interrupted_restores()["backendCommitted"]
    clear_active_fence()
    (committed_root / "transaction.json").unlink()
    committed_root.rmdir()

    write_external_transaction("restore_externalpending")
    aborted: list[str] = []
    monkeypatch.setattr(backups.StatelessMcpContributor, "restore_status", lambda _self, restore_id: journal("prepared"))
    monkeypatch.setattr(
        backups.StatelessMcpContributor,
        "abort_restore",
        lambda _self, restore_id: aborted.append(restore_id) or journal("rolled-back"),
    )
    recovered = backups.recover_interrupted_restores()
    assert "restore_externalpending" in recovered["rolledBack"]
    assert aborted == ["restore_externalpending"]
    clear_active_fence()

    write_external_transaction("restore_externalmissing")
    monkeypatch.setattr(backups.StatelessMcpContributor, "restore_status", lambda _self, restore_id: None)
    recovered = backups.recover_interrupted_restores()
    assert "restore_externalmissing" in recovered["rolledBack"]
    clear_active_fence()

    write_external_transaction("restore_externaldown")
    monkeypatch.setattr(
        backups.StatelessMcpContributor,
        "restore_status",
        lambda _self, restore_id: (_ for _ in ()).throw(AppError("down")),
    )
    monkeypatch.setattr(
        backups.StatelessMcpContributor,
        "abort_restore",
        lambda _self, restore_id: (_ for _ in ()).throw(AppError("down")),
    )
    recovered = backups.recover_interrupted_restores()
    assert "restore_externaldown" in recovered["recoveryRequired"]


def test_mutation_gate_fence_generation_and_path_resolution(tmp_settings: Path) -> None:
    root = tmp_settings
    assert mutation_gate.read_generation(root) == 0
    with mutation_gate.exclusive_gate(root):
        with mutation_gate.exclusive_gate(root):
            assert mutation_gate.bump_generation(root) == 1
    assert mutation_gate.read_generation(root) == 1
    with mutation_gate.mutation_scope(root=root):
        assert mutation_gate.read_generation(root) == 2
    assert mutation_gate.read_generation(root) == 3

    fence = {"schemaVersion": 1, "restoreId": "restore-owner", "phase": "preparing"}
    mutation_gate.write_fence(fence, root)
    assert mutation_gate.read_fence(root) == fence
    mutation_gate.assert_mutation_allowed("restore-owner", root)
    with pytest.raises(AppError, match="fenced"):
        mutation_gate.assert_mutation_allowed("peer", root)
    with pytest.raises(AppError, match="another transaction"):
        mutation_gate.clear_fence("wrong-owner", root)
    assert mutation_gate.clear_fence("restore-owner", root) is True
    assert mutation_gate.clear_fence("restore-owner", root) is False

    mutation_gate.fence_path(root).write_text("[]", encoding="utf-8")
    assert mutation_gate.read_fence(root) is None
    mutation_gate.fence_path(root).write_text("{", encoding="utf-8")
    with pytest.raises(AppError, match="unreadable"):
        mutation_gate.read_fence(root)
    mutation_gate.fence_path(root).unlink()
    assert mutation_gate.workspace_root_for_path(config.MEMORY_FILE) == config.MEMORY_DIR.parent
    arbitrary = tmp_settings / "external" / "item.json"
    assert mutation_gate.workspace_root_for_path(arbitrary) == arbitrary.parent


def test_backup_generation_retry_and_restore_helper_edges(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = backups.create_session({"mode": "full", "requiresFrontendState": False})
    backup_id = str(created["backupId"])
    candidates: list[Path] = []

    def candidate(*_args: object) -> dict[str, object]:
        path = backups.BACKUP_DIR / f"candidate-{len(candidates)}.dsibackup"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        candidates.append(path)
        return {"path": str(path), "bytes": 1, "archiveSha256": "x"}

    generations = iter((0, 1, 2, 3, 4, 5))
    monkeypatch.setattr(backups, "_build_archive", candidate)
    monkeypatch.setattr(backups.mutation_gate, "read_generation", lambda _root: next(generations))
    with pytest.raises(AppError, match="changed repeatedly"):
        backups.finalize_session(backup_id)
    assert all(not path.exists() for path in candidates)

    raw = b"not-json"
    assert backups._rewrite_typed_json_references(
        raw,
        {"old": "new"},
        "backup",
        identity_fields=frozenset({"id"}),
        reference_fields=frozenset(),
        path_fields=frozenset(),
    ) == raw
    rewritten = json.loads(backups._rewrite_json_references(b'{"id":"old","nested":["old"]}', {"old": "new"}, "backup"))
    assert rewritten["id"] == "new"
    assert rewritten["nested"] == ["new"]
    assert rewritten["importedFrom"]["originalId"] == "old"
    assert backups._version_compatible("4.4.0", "4.4.1") is True
    assert backups._version_compatible("4.4", "4.4.1") is False
    assert backups._version_compatible("future", "4.4.1") is False
    assert backups._version_compatible("5.0.0", "4.4.1") is False
    assert backups._iso_age_seconds("2000-01-01T00:00:00", 2_000_000_000) is not None

    plan_only = backups.RESTORE_DIR / "restore_planonly"
    plan_only.mkdir(parents=True)
    backups._write_json(plan_only / "plan.json", {"restoreId": plan_only.name, "phase": "inspected"})
    terminal = backups.RESTORE_DIR / "restore_terminal"
    terminal.mkdir(parents=True)
    backups._write_json(terminal / "transaction.json", {"restoreId": terminal.name, "phase": "failed", "contributors": []})
    listed = {item["restoreId"] for item in backups.list_restores()["restores"]}
    assert {plan_only.name, terminal.name} <= listed
    assert backups.recover_interrupted_restores()["recoveryRequired"] == []

    public_root = backups.RESTORE_DIR / "restore_public"
    public_root.mkdir(parents=True)
    public = backups._public_restore({"restoreId": public_root.name, "manifest": "invalid"}, public_root)
    assert public["restoredIdentities"] == []

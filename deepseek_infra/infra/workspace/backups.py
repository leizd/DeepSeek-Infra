"""Portable, integrity-verified workspace backups and transactional restores.

Unlike :mod:`exports`, this module never redacts or reshapes user data.  It
copies only registered durable contributors, rejects secrets at the boundary,
and records every restorable byte in a deterministic manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal, Protocol

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode

BACKUP_SCHEMA = "deepseek-workspace-backup.v1"
FRONTEND_SCHEMA_VERSION = 1
PACKAGE_PURPOSE = "restorable-backup"
MAX_ARCHIVE_BYTES = 2_000_000_000
MAX_EXPANDED_BYTES = 5_000_000_000
MAX_ENTRIES = 10_000
MAX_PATH_LENGTH = 240
MAX_JSON_DEPTH = 64
MAX_COMPRESSION_RATIO = 100

BackupDataClass = Literal["durable", "rebuildable", "optional-history", "ephemeral", "secret"]
RestorePolicy = Literal["merge", "rebuild", "replace-empty"]

BACKUP_DIR = config.ROOT / ".backups"
RESTORE_DIR = config.ROOT / ".restore-staging"

_BARRIER = threading.RLock()
_SECRET_NAMES = {
    ".env",
    ".auth-token",
    "auth-token",
    "credentials",
    "credentials.json",
    "api-key",
    "api_key",
    "tavily-key",
    "bearer-token",
}
_EPHEMERAL_SUFFIXES = {".lock", ".pid", ".tmp", ".part"}


class BackupContributor(Protocol):  # pragma: no cover - structural typing contract
    contributor_id: str
    schema_version: int
    data_class: BackupDataClass
    restore_policy: RestorePolicy

    def inventory(self, context: "BackupContext") -> dict[str, int]: ...

    def flush(self, context: "BackupContext") -> None: ...

    def snapshot(self, staging_dir: Path, context: "BackupContext") -> "BackupContribution": ...

    def validate(self, source: Path, context: "BackupContext") -> list[str]: ...

    def plan_restore(self, source: Path, context: "BackupContext") -> dict[str, Any]: ...

    def apply_restore(self, plan: dict[str, Any], context: "BackupContext") -> None: ...


@dataclass(frozen=True, slots=True)
class BackupContext:
    mode: str = "full"
    project_ids: tuple[str, ...] = ()
    include_history: bool = False
    include_drafts: bool = False
    include_rebuildable_indexes: bool = False


@dataclass(frozen=True, slots=True)
class BackupContribution:
    contributor_id: str
    schema_version: int
    records: int
    bytes: int
    digest: str
    restore_policy: RestorePolicy
    files: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class DirectoryContributor:
    contributor_id: str
    schema_version: int
    data_class: BackupDataClass
    restore_policy: RestorePolicy
    path_getter: Callable[[], Path]

    def inventory(self, context: BackupContext) -> dict[str, int]:
        paths = list(self._source_files(context))
        return {"records": len(paths), "bytes": sum(path.stat().st_size for path, _ in paths)}

    def flush(self, context: BackupContext) -> None:
        del context

    def snapshot(self, staging_dir: Path, context: BackupContext) -> BackupContribution:
        root = staging_dir / "payload" / self.contributor_id
        files: list[dict[str, Any]] = []
        digest = hashlib.sha256()
        for source, relative in self._source_files(context):
            target = root.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_consistent(source, target)
            raw = target.read_bytes()
            file_digest = hashlib.sha256(raw).hexdigest()
            package_path = target.relative_to(staging_dir).as_posix()
            entry = {"path": package_path, "size": len(raw), "sha256": file_digest}
            files.append(entry)
            digest.update(_stable_json(entry))
        return BackupContribution(
            contributor_id=self.contributor_id,
            schema_version=self.schema_version,
            records=len(files),
            bytes=sum(int(item["size"]) for item in files),
            digest=digest.hexdigest(),
            restore_policy=self.restore_policy,
            files=tuple(files),
        )

    def validate(self, source: Path, context: BackupContext) -> list[str]:
        del context
        # Empty contributors intentionally have no ZIP directory entry.
        return [] if source.is_dir() or not source.exists() else [f"{self.contributor_id}: payload is invalid"]

    def plan_restore(self, source: Path, context: BackupContext) -> dict[str, Any]:
        del context
        destination = self.path_getter()
        files = [path for path in source.rglob("*") if path.is_file()]
        conflicts = sum(1 for path in files if (destination / path.relative_to(source)).exists())
        return {
            "contributorId": self.contributor_id,
            "source": str(source),
            "destination": str(destination),
            "files": len(files),
            "conflicts": conflicts,
            "policy": self.restore_policy,
        }

    def apply_restore(self, plan: dict[str, Any], context: BackupContext) -> None:
        source = Path(str(plan["source"]))
        destination = Path(str(plan["destination"]))
        mode = str(plan.get("mode") or "merge")
        if mode == "replace-empty" and destination.exists() and any(destination.iterdir()):
            raise AppError(f"{self.contributor_id} is not empty", code=ErrorCode.INVALID_REQUEST, status=409)
        destination.mkdir(parents=True, exist_ok=True)
        identity_map = {
            str(key): str(value)
            for key, value in (plan.get("identityMap") or {}).items()
            if isinstance(key, str) and isinstance(value, str)
        }
        for item in sorted(source.rglob("*"), key=lambda value: value.as_posix()):
            if not item.is_file():
                continue
            relative = item.relative_to(source)
            if relative.parts and identity_map:
                remapped_parts = []
                for part in relative.parts:
                    if part in identity_map:
                        remapped_parts.append(identity_map[part])
                    elif Path(part).stem in identity_map:
                        remapped_parts.append(identity_map[Path(part).stem] + Path(part).suffix)
                    else:
                        remapped_parts.append(part)
                relative = Path(*remapped_parts)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            rewritten = (
                _rewrite_json_references(item.read_bytes(), identity_map, str(plan.get("sourceBackupId") or ""))
                if item.suffix.casefold() == ".json" and identity_map
                else item.read_bytes()
            )
            if target.exists():
                if hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(rewritten).digest():
                    continue
                if item.suffix.casefold() == ".json" and mode in {"merge", "project-copy"}:
                    target.write_bytes(_merge_json_payload(target.read_bytes(), rewritten))
                    continue
                if mode in {"merge", "project-copy"}:
                    target = _collision_path(target, context)
            target.write_bytes(rewritten)

    def _source_files(self, context: BackupContext) -> list[tuple[Path, str]]:
        root = self.path_getter()
        if not root.exists():
            return []
        # Project-owned source bytes already live under .projects/<id>/files.
        # The global file cache belongs only to non-project conversations and
        # must not leak unrelated attachments into a project-scoped package.
        if context.mode == "project" and self.contributor_id == "project-files":
            return []
        allowed_projects = set(context.project_ids) if context.mode == "project" and self.contributor_id == "projects" else None
        allowed_artifacts = _project_artifact_paths(context) if context.mode == "project" and self.contributor_id == "artifacts" else None
        result: list[tuple[Path, str]] = []
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root)
            if allowed_projects and (not relative.parts or relative.parts[0] not in allowed_projects):
                continue
            if self.contributor_id == "artifacts" and relative.parts and relative.parts[0].casefold() == "workspace-exports":
                continue
            if allowed_artifacts is not None and relative.as_posix() not in allowed_artifacts:
                continue
            if _excluded_path(relative):
                continue
            result.append((path, relative.as_posix()))
        return sorted(result, key=lambda item: item[1].casefold())


def _project_artifact_paths(context: BackupContext) -> set[str]:
    """Return generated files referenced by the selected project records."""
    generated = config.GENERATED_DIR.resolve()
    prefix = config.GENERATED_DIR.name.strip("/\\") + "/"
    result: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, str):
            normalized = value.replace("\\", "/")
            relative = normalized[len(prefix) :] if normalized.startswith(prefix) else ""
            if not relative:
                try:
                    relative = Path(value).resolve().relative_to(generated).as_posix()
                except (OSError, ValueError):
                    return
            candidate = generated.joinpath(*PurePosixPath(relative).parts)
            if candidate.is_file():
                result.add(PurePosixPath(relative).as_posix())

    for project_id in context.project_ids:
        project_root = config.PROJECTS_DIR / project_id
        if not project_root.is_dir():
            continue
        for metadata in project_root.rglob("*.json"):
            try:
                collect(json.loads(metadata.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    return result


def _registered_contributors() -> tuple[DirectoryContributor, ...]:
    return (
        DirectoryContributor("projects", 1, "durable", "merge", lambda: config.PROJECTS_DIR),
        DirectoryContributor("project-files", 1, "durable", "merge", lambda: config.FILE_CACHE_DIR),
        DirectoryContributor("artifacts", 1, "durable", "merge", lambda: config.GENERATED_DIR),
        DirectoryContributor("memory", 1, "durable", "merge", lambda: config.MEMORY_DIR),
        DirectoryContributor("media", 1, "durable", "merge", lambda: config.MEDIA_DIR),
        DirectoryContributor("custom-skills-and-packs", 1, "durable", "merge", lambda: config.SKILLS_DIR),
        DirectoryContributor("automations", 1, "durable", "merge", lambda: config.AUTOMATION_DIR),
        DirectoryContributor("reminders", 1, "durable", "merge", lambda: config.REMINDERS_DIR),
        DirectoryContributor("agent-checkpoints", 1, "optional-history", "merge", lambda: config.AGENT_RUNS_DIR),
        DirectoryContributor("a2a-tasks", 1, "optional-history", "merge", lambda: config.A2A_TASKS_DIR),
        DirectoryContributor("traces-and-run-history", 1, "optional-history", "merge", lambda: config.TRACE_DIR),
    )


def capabilities() -> dict[str, Any]:
    included = [item.contributor_id for item in _registered_contributors() if item.data_class == "durable"]
    optional = [item.contributor_id for item in _registered_contributors() if item.data_class == "optional-history"]
    return {
        "ok": True,
        "schemaVersion": BACKUP_SCHEMA,
        "purpose": PACKAGE_PURPOSE,
        "encrypted": False,
        "integrityVerified": True,
        "modes": ["full", "project"],
        "restoreModes": ["merge", "project-copy", "replace-empty"],
        "includedByDefault": included,
        "optionalHistory": optional,
        "alwaysExcluded": sorted(_SECRET_NAMES | {"rebuildable indexes", "runtime locks", "active tasks"}),
        "limits": {
            "maxPackageBytes": MAX_ARCHIVE_BYTES,
            "maxExpandedBytes": MAX_EXPANDED_BYTES,
            "maxEntries": MAX_ENTRIES,
            "maxPathLength": MAX_PATH_LENGTH,
        },
    }


def create_session(payload: dict[str, Any]) -> dict[str, Any]:
    context = _context_from_payload(payload)
    backup_id = f"backup_{uuid.uuid4().hex[:16]}"
    session_dir = _session_dir(backup_id)
    session_dir.mkdir(parents=True, exist_ok=False)
    contributors = _selected_contributors(context)
    estimates = [item.inventory(context) for item in contributors]
    state = {
        "backupId": backup_id,
        "phase": "preparing",
        "createdAt": _utc_iso(),
        "context": _context_dict(context),
        "requiresFrontendState": bool(payload.get("requiresFrontendState", True)),
        "estimatedBytes": sum(item["bytes"] for item in estimates),
        "included": [item.contributor_id for item in contributors],
        "excluded": _exclusions(context),
    }
    _write_json(session_dir / "session.json", state)
    return {"ok": True, **state}


def put_frontend_state(backup_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
    session = _load_session(backup_id)
    if session["phase"] not in {"preparing", "quiescing"}:
        raise AppError("Backup no longer accepts frontend state", code=ErrorCode.INVALID_REQUEST, status=409)
    normalized = _validate_frontend_envelope(envelope)
    _write_json(_session_dir(backup_id) / "frontend.json", normalized)
    session["frontendStateReceived"] = True
    _write_json(_session_dir(backup_id) / "session.json", session)
    return {"ok": True, "backupId": backup_id, "digest": normalized["digest"]}


def finalize_session(backup_id: str) -> dict[str, Any]:
    session = _load_session(backup_id)
    context = _context_from_payload(session["context"])
    frontend_path = _session_dir(backup_id) / "frontend.json"
    if session.get("requiresFrontendState") and not frontend_path.is_file():
        raise AppError("Verified frontend state is required", code=ErrorCode.INVALID_REQUEST, status=409)
    with _BARRIER:
        try:
            session["phase"] = "quiescing"
            _write_json(_session_dir(backup_id) / "session.json", session)
            result = _build_archive(backup_id, context, frontend_path if frontend_path.is_file() else None)
            session.update(result)
            session["phase"] = "ready"
        except Exception as exc:
            session["phase"] = "failed"
            session["error"] = str(exc)
            _write_json(_session_dir(backup_id) / "session.json", session)
            raise
        _write_json(_session_dir(backup_id) / "session.json", session)
    return {"ok": True, **_public_session(session)}


def create_backup(payload: dict[str, Any], *, frontend_state: dict[str, Any] | None = None) -> dict[str, Any]:
    request = dict(payload)
    request["requiresFrontendState"] = frontend_state is not None
    created = create_session(request)
    backup_id = str(created["backupId"])
    if frontend_state is not None:
        put_frontend_state(backup_id, frontend_state)
    return finalize_session(backup_id)


def get_session(backup_id: str) -> dict[str, Any]:
    return {"ok": True, **_public_session(_load_session(backup_id))}


def backup_path(backup_id: str) -> Path:
    session = _load_session(backup_id)
    if session.get("phase") != "ready":
        raise AppError("Backup is not ready", code=ErrorCode.INVALID_REQUEST, status=409)
    path = Path(str(session.get("path") or ""))
    if not path.is_file() or path.parent.resolve() != BACKUP_DIR.resolve():
        raise AppError("Backup file not found", code=ErrorCode.NOT_FOUND, status=404)
    return path


def delete_backup(backup_id: str) -> bool:
    session = _load_session(backup_id)
    path = Path(str(session.get("path") or ""))
    if path.is_file() and path.parent.resolve() == BACKUP_DIR.resolve():
        path.unlink()
    shutil.rmtree(_session_dir(backup_id), ignore_errors=True)
    return True


def inspect_archive(source: Path | bytes, *, filename: str = "workspace.dsibackup") -> dict[str, Any]:
    RESTORE_DIR.mkdir(parents=True, exist_ok=True)
    restore_id = f"restore_{uuid.uuid4().hex[:16]}"
    restore_root = RESTORE_DIR / restore_id
    restore_root.mkdir(parents=True)
    archive_path = restore_root / _safe_archive_name(filename)
    if isinstance(source, bytes):
        if len(source) > MAX_ARCHIVE_BYTES:
            raise AppError("Backup package is too large", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
        archive_path.write_bytes(source)
    else:
        if source.stat().st_size > MAX_ARCHIVE_BYTES:
            raise AppError("Backup package is too large", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
        shutil.copyfile(source, archive_path)
    extracted = restore_root / "extracted"
    try:
        manifest = _safe_extract_and_verify(archive_path, extracted)
        context = _context_from_manifest(manifest)
        operations: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        known = {item.contributor_id: item for item in _registered_contributors()}
        for entry in manifest["contributors"]:
            contributor = known.get(str(entry["id"]))
            if contributor is None:
                raise AppError(f"Unsupported contributor: {entry['id']}", code=ErrorCode.INVALID_PAYLOAD)
            source_dir = extracted / "payload" / contributor.contributor_id
            errors = contributor.validate(source_dir, context)
            if errors:
                raise AppError("; ".join(errors), code=ErrorCode.INVALID_PAYLOAD)
            plan = contributor.plan_restore(source_dir, context)
            operations.append(plan)
            if plan["conflicts"]:
                conflicts.append({"contributorId": contributor.contributor_id, "count": plan["conflicts"], "strategy": "deterministic-remap"})
        plan = {
            "restoreId": restore_id,
            "sourceVersion": manifest["source"]["version"],
            "targetVersion": config.APP_VERSION,
            "compatible": True,
            "purpose": manifest["purpose"],
            "operations": operations,
            "conflicts": conflicts,
            "migrations": [],
            "warnings": ["Backup is integrity-verified but not encrypted."],
            "estimatedWriteBytes": sum(int(item["size"]) for item in manifest["files"]),
            "requiresFrontendApply": bool(manifest.get("frontend")),
            "archiveSha256": _sha256_file(archive_path),
            "manifest": manifest,
            "phase": "inspected",
        }
        _write_json(restore_root / "plan.json", plan)
        return {"ok": True, **plan}
    except Exception:
        shutil.rmtree(restore_root, ignore_errors=True)
        raise


def apply_restore(restore_id: str, *, mode: str = "merge") -> dict[str, Any]:
    if mode not in {"merge", "project-copy", "replace-empty"}:
        raise AppError("Unsupported restore mode", code=ErrorCode.INVALID_PAYLOAD)
    root = _restore_root(restore_id)
    plan = _read_json(root / "plan.json")
    archive = next(root.glob("*.dsibackup"), None)
    if archive is None or _sha256_file(archive) != plan.get("archiveSha256"):
        raise AppError("Backup changed after inspection", code=ErrorCode.INVALID_PAYLOAD)
    _safe_extract_and_verify(archive, root / "verified")
    context = _context_from_manifest(plan["manifest"])
    journal = root / "rollback"
    applied: list[str] = []
    known = {item.contributor_id: item for item in _registered_contributors()}
    identity_map = _restore_identity_map(plan, mode)
    with _BARRIER:
        safety = create_backup({"mode": "full", "includeHistory": True, "requiresFrontendState": False})
        try:
            for operation in plan["operations"]:
                contributor_id = str(operation["contributorId"])
                contributor = known[contributor_id]
                destination = Path(str(operation["destination"]))
                if destination.exists():
                    shutil.copytree(destination, journal / contributor_id)
                # Record the contributor before applying it so a partial write
                # from a failing contributor is removed or restored as well.
                applied.append(contributor_id)
                current = dict(operation)
                current["source"] = str(root / "verified" / "payload" / contributor_id)
                current["mode"] = mode
                current["identityMap"] = identity_map
                current["sourceBackupId"] = str(plan["manifest"].get("backupId") or "")
                contributor.apply_restore(current, context)
            marker = {
                "restoreId": restore_id,
                "mode": mode,
                "committedAt": _utc_iso(),
                "safetyBackupId": safety["backupId"],
                "restoreEpoch": int(time.time() * 1000),
            }
            _write_json(root / "restore-commit.json", marker)
        except Exception:
            for contributor_id in reversed(applied):
                destination = known[contributor_id].path_getter()
                if destination.exists():
                    shutil.rmtree(destination)
                saved = journal / contributor_id
                if saved.exists():
                    shutil.copytree(saved, destination)
            raise
    return {
        "ok": True,
        "restoreId": restore_id,
        "phase": "ready-for-frontend" if plan.get("requiresFrontendApply") else "complete",
        "applied": applied,
        "restoredIdentities": [
            {
                "originalId": original,
                "restoredId": restored,
                "reason": "collision",
                "sourceBackupId": str(plan["manifest"].get("backupId") or ""),
            }
            for original, restored in sorted(identity_map.items())
        ],
        "frontend": _read_json(root / "verified" / "frontend" / "state.json") if plan.get("requiresFrontendApply") else None,
        **marker,
    }


def _build_archive(backup_id: str, context: BackupContext, frontend_path: Path | None) -> dict[str, Any]:
    session_dir = _session_dir(backup_id)
    staging = session_dir / "staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    contributions: list[BackupContribution] = []
    for contributor in _selected_contributors(context):
        contributor.flush(context)
        contributions.append(contributor.snapshot(staging, context))
    frontend_manifest: dict[str, Any] | None = None
    files = [entry for contribution in contributions for entry in contribution.files]
    if frontend_path is not None:
        target = staging / "frontend" / "state.json"
        target.parent.mkdir(parents=True)
        shutil.copyfile(frontend_path, target)
        raw = target.read_bytes()
        entry = {"path": "frontend/state.json", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        files.append(entry)
        envelope = _read_json(target)
        frontend_manifest = {
            "schemaVersion": FRONTEND_SCHEMA_VERSION,
            "conversations": len(envelope.get("conversations", [])),
            "conflicts": len(envelope.get("conflicts", [])),
            "digest": envelope["digest"],
        }
    source_schemas = {item.contributor_id: item.schema_version for item in contributions}
    migration_path = staging / "migration" / "source-schemas.json"
    migration_path.parent.mkdir(parents=True)
    migration_path.write_bytes(_stable_json(source_schemas))
    raw_migration = migration_path.read_bytes()
    files.append({"path": "migration/source-schemas.json", "size": len(raw_migration), "sha256": hashlib.sha256(raw_migration).hexdigest()})
    files.sort(key=lambda entry: str(entry["path"]).casefold())
    manifest: dict[str, Any] = {
        "schemaVersion": BACKUP_SCHEMA,
        "purpose": PACKAGE_PURPOSE,
        "backupId": backup_id,
        "source": {
            "version": config.APP_VERSION,
            "revision": _build_revision(),
            "platform": platform.platform(),
            "createdAt": _utc_iso(),
        },
        "scope": {
            "mode": context.mode,
            "projectIds": list(context.project_ids),
            "includeHistory": context.include_history,
            "includeDrafts": context.include_drafts,
        },
        "contributors": [
            {
                "id": item.contributor_id,
                "schemaVersion": item.schema_version,
                "records": item.records,
                "bytes": item.bytes,
                "digest": item.digest,
                "restorePolicy": item.restore_policy,
            }
            for item in contributions
        ],
        "exclusions": _exclusions(context),
        "files": files,
        "encrypted": False,
    }
    if frontend_manifest:
        manifest["frontend"] = frontend_manifest
    manifest_bytes = _stable_json(manifest)
    (staging / "manifest.json").write_bytes(manifest_bytes)
    checksums = [f"{item['sha256']}  {item['path']}" for item in files]
    checksums.append(f"{hashlib.sha256(manifest_bytes).hexdigest()}  manifest.json")
    (staging / "checksums.sha256").write_text("\n".join(checksums) + "\n", encoding="utf-8", newline="\n")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"deepseek-infra-backup-{time.strftime('%Y%m%d')}-{backup_id[-8:]}.dsibackup"
    temporary = BACKUP_DIR / f".{filename}.tmp"
    target = BACKUP_DIR / filename
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted((item for item in staging.rglob("*") if item.is_file()), key=lambda item: item.relative_to(staging).as_posix()):
            info = zipfile.ZipInfo(path.relative_to(staging).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, path.read_bytes())
    os.replace(temporary, target)
    _safe_extract_and_verify(target, session_dir / "verification")
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(session_dir / "verification", ignore_errors=True)
    return {
        "filename": filename,
        "path": str(target),
        "size": target.stat().st_size,
        "manifestDigest": hashlib.sha256(manifest_bytes).hexdigest(),
        "downloadUrl": f"/api/workspace/backups/{backup_id}/download",
    }


def _safe_extract_and_verify(archive_path: Path, destination: Path) -> dict[str, Any]:
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    seen: set[str] = set()
    total = 0
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise AppError("Backup contains too many files", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
        for info in infos:
            normalized = _validate_archive_path(info.filename)
            folded = normalized.casefold()
            if folded in seen:
                raise AppError("Backup contains duplicate or case-colliding paths", code=ErrorCode.INVALID_PAYLOAD)
            seen.add(folded)
            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if stat.S_ISLNK(mode) or (file_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                raise AppError("Backup contains a link or special file", code=ErrorCode.INVALID_PAYLOAD)
            if info.file_size < 0 or info.file_size > MAX_EXPANDED_BYTES:
                raise AppError("Backup entry is too large", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
            total += info.file_size
            if total > MAX_EXPANDED_BYTES:
                raise AppError("Expanded backup is too large", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
            if info.compress_size == 0 and info.file_size > 0:
                raise AppError("Suspicious compression ratio", code=ErrorCode.INVALID_PAYLOAD)
            if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise AppError("Suspicious compression ratio", code=ErrorCode.INVALID_PAYLOAD)
            target = destination.joinpath(*PurePosixPath(normalized).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not info.is_dir():
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    manifest_path = destination / "manifest.json"
    checksum_path = destination / "checksums.sha256"
    if not manifest_path.is_file() or not checksum_path.is_file():
        raise AppError("Backup manifest is missing", code=ErrorCode.INVALID_PAYLOAD)
    manifest = _read_json(manifest_path)
    _check_json_depth(manifest)
    if manifest.get("schemaVersion") != BACKUP_SCHEMA or manifest.get("purpose") != PACKAGE_PURPOSE:
        raise AppError("Package is not a supported restorable backup", code=ErrorCode.INVALID_PAYLOAD)
    declared = manifest.get("files")
    if not isinstance(declared, list):
        raise AppError("Backup file inventory is invalid", code=ErrorCode.INVALID_PAYLOAD)
    declared_paths: set[str] = set()
    for entry in declared:
        if not isinstance(entry, dict):
            raise AppError("Backup file inventory is invalid", code=ErrorCode.INVALID_PAYLOAD)
        relative = _validate_archive_path(str(entry.get("path") or ""))
        if relative in {"manifest.json", "checksums.sha256"} or relative in declared_paths:
            raise AppError("Backup manifest contains duplicate entries", code=ErrorCode.INVALID_PAYLOAD)
        declared_paths.add(relative)
        path = destination.joinpath(*PurePosixPath(relative).parts)
        if not path.is_file() or path.stat().st_size != int(entry.get("size") or -1) or _sha256_file(path) != entry.get("sha256"):
            raise AppError(f"Backup checksum mismatch: {relative}", code=ErrorCode.INVALID_PAYLOAD)
    actual = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() and path.name not in {"manifest.json", "checksums.sha256"}
    }
    if actual != declared_paths:
        raise AppError("Backup contains undeclared or missing restorable files", code=ErrorCode.INVALID_PAYLOAD)
    checksum_lines = checksum_path.read_text(encoding="utf-8").splitlines()
    expected_manifest_line = f"{_sha256_file(manifest_path)}  manifest.json"
    if expected_manifest_line not in checksum_lines:
        raise AppError("Manifest digest is invalid", code=ErrorCode.INVALID_PAYLOAD)
    return manifest


def _selected_contributors(context: BackupContext) -> tuple[DirectoryContributor, ...]:
    return tuple(
        item
        for item in _registered_contributors()
        if item.data_class == "durable"
        or (item.data_class == "optional-history" and context.include_history)
        or (item.data_class == "rebuildable" and context.include_rebuildable_indexes)
    )


def _context_from_payload(payload: dict[str, Any]) -> BackupContext:
    mode = str(payload.get("mode") or "full")
    if mode not in {"full", "project"}:
        raise AppError("Backup mode must be full or project", code=ErrorCode.INVALID_PAYLOAD)
    raw_ids = payload.get("projectIds")
    ids = tuple(sorted({str(value).strip() for value in raw_ids if str(value).strip()})) if isinstance(raw_ids, list) else ()
    if mode == "project" and not ids:
        single = str(payload.get("projectId") or "").strip()
        ids = (single,) if single else ()
    if mode == "project" and not ids:
        raise AppError("Project backup requires a project id", code=ErrorCode.INVALID_PAYLOAD)
    return BackupContext(
        mode=mode,
        project_ids=ids,
        include_history=bool(payload.get("includeHistory", False)),
        include_drafts=bool(payload.get("includeDrafts", False)),
        include_rebuildable_indexes=bool(payload.get("includeRebuildableIndexes", False)),
    )


def _context_from_manifest(manifest: dict[str, Any]) -> BackupContext:
    scope = manifest.get("scope")
    return _context_from_payload(scope if isinstance(scope, dict) else {})


def _context_dict(context: BackupContext) -> dict[str, Any]:
    return {
        "mode": context.mode,
        "projectIds": list(context.project_ids),
        "includeHistory": context.include_history,
        "includeDrafts": context.include_drafts,
        "includeRebuildableIndexes": context.include_rebuildable_indexes,
    }


def _validate_frontend_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    if envelope.get("schemaVersion") != FRONTEND_SCHEMA_VERSION:
        raise AppError("Unsupported frontend backup schema", code=ErrorCode.INVALID_PAYLOAD)
    forbidden = {"apiKey", "tavilyKey", "authorizationToken", "writerSessionId", "documentInstanceId", "lease"}
    if _contains_key(envelope, forbidden):
        raise AppError("Frontend backup contains ephemeral identity or credentials", code=ErrorCode.SENSITIVE_CONTENT)
    body = {key: value for key, value in envelope.items() if key != "digest"}
    digest = str(envelope.get("digest") or "")
    if not digest or digest != hashlib.sha256(_stable_json(body)).hexdigest():
        raise AppError("Frontend backup digest is invalid", code=ErrorCode.INVALID_PAYLOAD)
    _check_json_depth(envelope)
    return envelope


def _contains_key(value: Any, forbidden: set[str]) -> bool:
    if isinstance(value, dict):
        return any(str(key) in forbidden or _contains_key(item, forbidden) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_key(item, forbidden) for item in value)
    return False


def _excluded_path(path: Path) -> bool:
    parts = {part.casefold() for part in path.parts}
    return bool(parts & _SECRET_NAMES) or path.suffix.casefold() in _EPHEMERAL_SUFFIXES


def _collision_path(path: Path, context: BackupContext) -> Path:
    seed = hashlib.sha256((path.as_posix() + "|" + ",".join(context.project_ids)).encode()).hexdigest()[:12]
    return path.with_name(f"{path.stem}.imported-{seed}{path.suffix}")


def _restore_identity_map(plan: dict[str, Any], mode: str) -> dict[str, str]:
    if mode not in {"merge", "project-copy"}:
        return {}
    project_operation = next(
        (
            item
            for item in plan.get("operations", [])
            if isinstance(item, dict) and item.get("contributorId") == "projects"
        ),
        None,
    )
    backup_id = str(plan.get("manifest", {}).get("backupId") or "")
    result: dict[str, str] = {}
    if isinstance(project_operation, dict):
        source = Path(str(project_operation.get("source") or ""))
        destination = Path(str(project_operation.get("destination") or ""))
        if source.is_dir():
            for project in sorted((item for item in source.iterdir() if item.is_dir()), key=lambda item: item.name):
                existing = destination / project.name
                if mode == "project-copy" or (existing.is_dir() and _tree_digest(existing) != _tree_digest(project)):
                    result[project.name] = _restored_id(project.name, backup_id, _tree_digest(project))
    for operation in plan.get("operations", []):
        if not isinstance(operation, dict):
            continue
        source = Path(str(operation.get("source") or ""))
        destination = Path(str(operation.get("destination") or ""))
        source_objects = _json_identity_index(source)
        destination_objects = _json_identity_index(destination)
        for original_id, source_digest in source_objects.items():
            destination_digest = destination_objects.get(original_id)
            if destination_digest is not None and destination_digest != source_digest and original_id not in result:
                result[original_id] = _restored_id(original_id, backup_id, source_digest)
    return result


_REFERENCE_ID_FIELDS = {
    "id",
    "projectId",
    "conversationId",
    "messageId",
    "savedId",
    "artifactId",
    "mediaId",
    "automationId",
    "traceId",
}


def _json_identity_index(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not root.is_dir():
        return result

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        encoded = hashlib.sha256(_stable_json(value)).hexdigest()
        for key in _REFERENCE_ID_FIELDS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                result.setdefault(candidate, encoded)
        for item in value.values():
            visit(item)

    for path in sorted(root.rglob("*.json"), key=lambda item: item.as_posix()):
        try:
            visit(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    return result


def _restored_id(original_id: str, backup_id: str, source_digest: str) -> str:
    suffix = hashlib.sha256(f"{backup_id}\0{original_id}\0{source_digest}".encode()).hexdigest()[:10]
    return f"{original_id[:43]}-imported-{suffix}"


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(bytes.fromhex(_sha256_file(path)))
    return digest.hexdigest()


def _rewrite_json_references(raw: bytes, identity_map: dict[str, str], backup_id: str) -> bytes:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw

    def rewrite(item: Any) -> Any:
        if isinstance(item, str):
            if item in identity_map:
                return identity_map[item]
            if "/" in item:
                parts = item.split("/")
                rewritten_parts = [
                    identity_map.get(part, identity_map.get(Path(part).stem, Path(part).stem) + Path(part).suffix)
                    for part in parts
                ]
                return "/".join(rewritten_parts)
            return item
        if isinstance(item, list):
            return [rewrite(value) for value in item]
        if isinstance(item, dict):
            result = {key: rewrite(value) for key, value in item.items()}
            original_id = item.get("id") or item.get("projectId")
            if isinstance(original_id, str) and original_id in identity_map:
                result["importedFrom"] = {"originalId": original_id, "sourceBackupId": backup_id}
            return result
        return item

    return _stable_json(rewrite(value))


def _merge_json_payload(existing_raw: bytes, incoming_raw: bytes) -> bytes:
    try:
        existing = json.loads(existing_raw.decode("utf-8"))
        incoming = json.loads(incoming_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return incoming_raw

    def merge(left: Any, right: Any) -> Any:
        if isinstance(left, list) and isinstance(right, list):
            fingerprints = {hashlib.sha256(_stable_json(item)).digest() for item in left}
            list_result = list(left)
            for item in right:
                fingerprint = hashlib.sha256(_stable_json(item)).digest()
                if fingerprint not in fingerprints:
                    list_result.append(item)
                    fingerprints.add(fingerprint)
            return list_result
        if isinstance(left, dict) and isinstance(right, dict):
            dict_result = dict(left)
            for key, value in right.items():
                dict_result[key] = merge(dict_result[key], value) if key in dict_result else value
            return dict_result
        return left if left is not None else right

    return _stable_json(merge(existing, incoming))


def _validate_archive_path(raw: str) -> str:
    if not raw or "\\" in raw or "\x00" in raw or len(raw) > MAX_PATH_LENGTH:
        raise AppError("Backup contains an unsafe path", code=ErrorCode.INVALID_PAYLOAD)
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or ":" in path.parts[0]:
        raise AppError("Backup contains an unsafe path", code=ErrorCode.INVALID_PAYLOAD)
    return path.as_posix().rstrip("/")


def _safe_archive_name(filename: str) -> str:
    name = Path(filename).name
    if not name.lower().endswith(".dsibackup"):
        name += ".dsibackup"
    return name[:120]


def _check_json_depth(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise AppError("Backup JSON is too deeply nested", code=ErrorCode.INVALID_PAYLOAD)
    if isinstance(value, dict):
        for item in value.values():
            _check_json_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_json_depth(item, depth + 1)


def _build_revision() -> str:
    value = os.environ.get("DEEPSEEK_BUILD_REVISION", "").strip() or os.environ.get("GITHUB_SHA", "").strip()
    if value:
        return value
    repository = Path(__file__).resolve().parents[3]
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True, text=True, check=False)
    revision = result.stdout.strip() if result.returncode == 0 else ""
    if not revision:
        return "unknown"
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    if status.returncode == 0 and status.stdout:
        diff = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--binary", "HEAD"],
            cwd=repository,
            capture_output=True,
            check=False,
        )
        dirty_digest = hashlib.sha256(status.stdout + (diff.stdout if diff.returncode == 0 else b"")).hexdigest()[:16]
        return f"{revision}-dirty-{dirty_digest}"
    return revision


def _exclusions(context: BackupContext) -> list[str]:
    exclusions = ["credentials and auth tokens", "ephemeral locks and process state", "rebuildable indexes", "active task replay"]
    if not context.include_history:
        exclusions.append("optional run history")
    if not context.include_drafts:
        exclusions.append("frontend drafts")
    return exclusions


def _session_dir(backup_id: str) -> Path:
    if not backup_id.startswith("backup_") or not backup_id[7:].isalnum():
        raise AppError("Invalid backup id", code=ErrorCode.INVALID_PAYLOAD)
    return BACKUP_DIR / "sessions" / backup_id


def _restore_root(restore_id: str) -> Path:
    if not restore_id.startswith("restore_") or not restore_id[8:].isalnum():
        raise AppError("Invalid restore id", code=ErrorCode.INVALID_PAYLOAD)
    root = RESTORE_DIR / restore_id
    if not root.is_dir():
        raise AppError("Restore plan not found", code=ErrorCode.NOT_FOUND, status=404)
    return root


def _load_session(backup_id: str) -> dict[str, Any]:
    path = _session_dir(backup_id) / "session.json"
    if not path.is_file():
        raise AppError("Backup session not found", code=ErrorCode.NOT_FOUND, status=404)
    return _read_json(path)


def _public_session(session: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in session.items() if key not in {"path", "context"}}


def _stable_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(_stable_json(value))
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError(f"Invalid backup metadata: {path.name}", code=ErrorCode.INVALID_PAYLOAD) from exc
    if not isinstance(value, dict):
        raise AppError("Backup metadata must be an object", code=ErrorCode.INVALID_PAYLOAD)
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_consistent(source: Path, target: Path) -> None:
    """Copy ordinary files and use SQLite's online backup API for databases."""
    if source.suffix.casefold() not in {".sqlite", ".sqlite3", ".db"}:
        shutil.copyfile(source, target)
        return
    origin: sqlite3.Connection | None = None
    snapshot: sqlite3.Connection | None = None
    try:
        origin = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        snapshot = sqlite3.connect(target)
        origin.backup(snapshot)
    except sqlite3.DatabaseError as exc:
        if snapshot is not None:
            snapshot.close()
            snapshot = None
        if origin is not None:
            origin.close()
            origin = None
        if target.exists():
            target.unlink()
        raise AppError(f"Could not create a consistent SQLite snapshot: {source.name}", code=ErrorCode.INTERNAL, status=500) from exc
    finally:
        if snapshot is not None:
            snapshot.close()
        if origin is not None:
            origin.close()


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

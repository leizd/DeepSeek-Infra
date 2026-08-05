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
from urllib import error as urllib_error
from urllib import request as urllib_request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Literal, Protocol, cast

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_crypto, mutation_gate

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
CoveragePolicy = Literal["strict", "best-effort"]

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

    def inspect_schema(self, source_version: int) -> dict[str, Any]: ...

    def migrate(self, source: Path, target_version: int) -> Path: ...

    def build_identity_map(self, source: Path, destination: Path) -> dict[str, str]: ...

    def rewrite_references(self, raw: bytes, identity_map: dict[str, str], backup_id: str) -> bytes: ...

    def merge_into_staging(self, plan: dict[str, Any], context: "BackupContext") -> None: ...

    def validate_staging(self, staging: Path, context: "BackupContext") -> list[str]: ...


class ExternalRestoreParticipant(Protocol):  # pragma: no cover - structural typing contract
    """Durable external store joining the workspace restore transaction."""

    contributor_id: str
    schema_version: int

    def prepare_restore(self, restore_id: str, source: Path, transaction_digest: str) -> dict[str, Any]: ...

    def commit_restore_intent(self, restore_id: str, transaction_digest: str) -> dict[str, Any]: ...

    def commit_restore(self, restore_id: str, transaction_digest: str) -> dict[str, Any]: ...

    def complete_restore(self, restore_id: str) -> dict[str, Any]: ...

    def abort_restore(self, restore_id: str) -> dict[str, Any]: ...

    def restore_status(self, restore_id: str) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class BackupContext:
    mode: str = "full"
    project_ids: tuple[str, ...] = ()
    include_history: bool = False
    include_drafts: bool = False
    include_rebuildable_indexes: bool = False
    coverage_policy: CoveragePolicy = "strict"
    include_external_state: bool = True


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
    identity_fields: frozenset[str] = frozenset()
    reference_fields: frozenset[str] = frozenset()
    path_fields: frozenset[str] = frozenset()

    def inspect_schema(self, source_version: int) -> dict[str, Any]:
        if source_version == self.schema_version:
            return {"compatible": True, "migration": None}
        return {
            "compatible": False,
            "migration": {
                "contributorId": self.contributor_id,
                "from": source_version,
                "to": self.schema_version,
                "reversible": False,
            },
        }

    def migrate(self, source: Path, target_version: int) -> Path:
        if target_version != self.schema_version:
            raise AppError(f"{self.contributor_id} has no migration to schema {target_version}", code=ErrorCode.INVALID_REQUEST, status=409)
        return source

    def build_identity_map(self, source: Path, destination: Path) -> dict[str, str]:
        source_objects = _json_identity_index(source, self.identity_fields)
        destination_objects = _json_identity_index(destination, self.identity_fields)
        return {
            original_id: source_digest
            for original_id, source_digest in source_objects.items()
            if original_id in destination_objects and destination_objects[original_id] != source_digest
        }

    def rewrite_references(self, raw: bytes, identity_map: dict[str, str], backup_id: str) -> bytes:
        return _rewrite_typed_json_references(
            raw,
            identity_map,
            backup_id,
            identity_fields=self.identity_fields,
            reference_fields=self.reference_fields,
            path_fields=self.path_fields,
        )

    def merge_into_staging(self, plan: dict[str, Any], context: BackupContext) -> None:
        self.apply_restore(plan, context)

    def validate_staging(self, staging: Path, context: BackupContext) -> list[str]:
        return self.validate(staging, context)

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
            size = target.stat().st_size
            file_digest = _sha256_file(target)
            package_path = target.relative_to(staging_dir).as_posix()
            entry = {"path": package_path, "size": size, "sha256": file_digest}
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
            is_rewritten_json = item.suffix.casefold() == ".json" and bool(identity_map)
            rewritten = (
                self.rewrite_references(item.read_bytes(), identity_map, str(plan.get("sourceBackupId") or ""))
                if is_rewritten_json
                else None
            )
            if target.exists():
                incoming_digest = hashlib.sha256(rewritten).hexdigest() if rewritten is not None else _sha256_file(item)
                if _sha256_file(target) == incoming_digest:
                    continue
                if item.suffix.casefold() == ".json" and mode in {"merge", "project-copy"}:
                    incoming = rewritten if rewritten is not None else item.read_bytes()
                    target.write_bytes(_merge_json_payload(target.read_bytes(), incoming))
                    continue
                if mode in {"merge", "project-copy"}:
                    target = _collision_path(target, context)
            if rewritten is not None:
                target.write_bytes(rewritten)
            else:
                shutil.copyfile(item, target)

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


@dataclass(frozen=True, slots=True)
class StatelessMcpContributor:
    contributor_id: str = "stateless-mcp"
    schema_version: int = 1
    data_class: BackupDataClass = "durable"
    restore_policy: RestorePolicy = "merge"
    external: bool = True

    @staticmethod
    def _url(path: str) -> str:
        base = os.environ.get("STATELESS_MCP_BACKUP_URL", "").strip().rstrip("/")
        if not base:
            raise AppError("Stateless MCP backup endpoint is not configured", code=ErrorCode.INVALID_REQUEST, status=409)
        return f"{base}{path}"

    @staticmethod
    def _request(
        path: str,
        *,
        method: str = "GET",
        body: bytes | BinaryIO | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> Any:
        token = os.environ.get("STATELESS_MCP_BACKUP_TOKEN", "").strip()
        request_headers = {"Accept": "application/json, application/x-ndjson"}
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            request_headers["Content-Type"] = "application/x-ndjson" if path.startswith("/internal/restores/") else "application/json"
        if headers:
            request_headers.update(headers)
        try:
            return urllib_request.urlopen(
                urllib_request.Request(StatelessMcpContributor._url(path), data=body, headers=request_headers, method=method),
                timeout=timeout,
            )
        except urllib_error.HTTPError as exc:
            raise AppError("Stateless MCP durable state is unavailable", code=ErrorCode.INVALID_REQUEST, status=exc.code) from exc
        except (OSError, urllib_error.URLError) as exc:
            raise AppError("Stateless MCP durable state is unavailable", code=ErrorCode.INVALID_REQUEST, status=409) from exc

    def capabilities(self) -> dict[str, Any]:
        try:
            with self._request("/internal/backups/capabilities", timeout=2.0) as response:
                value = json.loads(response.read(64_000).decode("utf-8"))
            available = isinstance(value, dict) and value.get("contributorId") == self.contributor_id and value.get("schemaVersion") == 1
            return {"id": self.contributor_id, "available": available, "schemaVersion": 1}
        except AppError:
            return {"id": self.contributor_id, "available": False, "schemaVersion": 1, "reason": "service unavailable"}

    def inventory(self, context: BackupContext) -> dict[str, int]:
        del context
        return {"records": 0, "bytes": 0}

    def flush(self, context: BackupContext) -> None:
        del context

    def snapshot(self, staging_dir: Path, context: BackupContext) -> BackupContribution:
        del context
        backup_id = f"external_{uuid.uuid4().hex}"
        target = staging_dir / "payload" / self.contributor_id / "state.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        prepared = _stable_json({"backupId": backup_id})
        try:
            with self._request("/internal/backups/prepare", method="POST", body=prepared) as response:
                response.read(64_000)
            last_error: AppError | None = None
            for _attempt in range(3):
                try:
                    with self._request(f"/internal/backups/{backup_id}/stream", timeout=60.0) as response, target.open("wb") as output:
                        total = 0
                        while chunk := response.read(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_EXPANDED_BYTES:
                                raise AppError("External backup snapshot is too large", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    last_error = None
                    break
                except AppError as exc:
                    last_error = exc
            if last_error is not None:
                raise last_error
            errors = self.validate(target.parent, BackupContext())
            if errors:
                raise AppError("; ".join(errors), code=ErrorCode.INVALID_PAYLOAD)
        finally:
            try:
                with self._request(f"/internal/backups/{backup_id}/release", method="POST", body=b"{}") as response:
                    response.read(64_000)
            except AppError:
                pass
        entry = {"path": f"payload/{self.contributor_id}/state.jsonl", "size": target.stat().st_size, "sha256": _sha256_file(target)}
        return BackupContribution(
            contributor_id=self.contributor_id,
            schema_version=self.schema_version,
            records=_jsonl_task_count(target),
            bytes=target.stat().st_size,
            digest=str(entry["sha256"]),
            restore_policy=self.restore_policy,
            files=(entry,),
        )

    def validate(self, source: Path, context: BackupContext) -> list[str]:
        del context
        path = source / "state.jsonl"
        if not path.is_file():
            return ["stateless-mcp: snapshot is missing"]
        try:
            complete = False
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    value = json.loads(line)
                    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
                        return ["stateless-mcp: unsupported snapshot schema"]
                    if value.get("type") == "complete":
                        complete = True
                    encoded = line.casefold()
                    if any(secret in encoded for secret in ("redis://", "bearer ", "mcp_auth_token", "fencingtoken")):
                        return ["stateless-mcp: snapshot contains forbidden deployment state"]
            return [] if complete else ["stateless-mcp: snapshot is incomplete"]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return ["stateless-mcp: snapshot is invalid"]

    def plan_restore(self, source: Path, context: BackupContext) -> dict[str, Any]:
        del context
        status = self.capabilities()
        return {
            "contributorId": self.contributor_id,
            "source": str(source),
            "destination": "stateless-mcp://durable-task-state",
            "files": 1,
            "conflicts": 0,
            "policy": self.restore_policy,
            "external": True,
            "available": bool(status.get("available")),
        }

    def apply_restore(self, plan: dict[str, Any], context: BackupContext) -> None:
        del plan, context
        raise AppError(
            "External restores are coordinated through the restore participant protocol",
            code=ErrorCode.INVALID_REQUEST,
        )

    def prepare_restore(self, restore_id: str, source: Path, transaction_digest: str) -> dict[str, Any]:
        snapshot = source / "state.jsonl" if source.is_dir() else source
        size = snapshot.stat().st_size
        digest = _sha256_file(snapshot)
        with snapshot.open("rb") as stream:
            with self._request(
                f"/internal/restores/{restore_id}/prepare",
                method="POST",
                body=stream,
                headers={
                    "Content-Length": str(size),
                    "X-Transaction-Digest": transaction_digest,
                    "X-Content-SHA256": digest,
                },
                timeout=300.0,
            ) as response:
                journal = json.loads(response.read(2_000_000).decode("utf-8"))
        return _participant_journal(journal)

    def commit_restore_intent(self, restore_id: str, transaction_digest: str) -> dict[str, Any]:
        body = json.dumps({"transactionDigest": transaction_digest}).encode("utf-8")
        with self._request(f"/internal/restores/{restore_id}/commit-intent", method="POST", body=body, timeout=60.0) as response:
            return _participant_journal(json.loads(response.read(2_000_000).decode("utf-8")))

    def commit_restore(self, restore_id: str, transaction_digest: str) -> dict[str, Any]:
        body = json.dumps({"transactionDigest": transaction_digest}).encode("utf-8")
        with self._request(f"/internal/restores/{restore_id}/commit", method="POST", body=body, timeout=300.0) as response:
            return _participant_journal(json.loads(response.read(2_000_000).decode("utf-8")))

    def complete_restore(self, restore_id: str) -> dict[str, Any]:
        with self._request(f"/internal/restores/{restore_id}/complete", method="POST", body=b"{}", timeout=60.0) as response:
            return _participant_journal(json.loads(response.read(2_000_000).decode("utf-8")))

    def abort_restore(self, restore_id: str) -> dict[str, Any]:
        with self._request(f"/internal/restores/{restore_id}/abort", method="POST", body=b"{}", timeout=60.0) as response:
            return _participant_journal(json.loads(response.read(2_000_000).decode("utf-8")))

    def restore_status(self, restore_id: str) -> dict[str, Any] | None:
        try:
            with self._request(f"/internal/restores/{restore_id}", timeout=10.0) as response:
                return _participant_journal(json.loads(response.read(2_000_000).decode("utf-8")))
        except AppError as exc:
            if exc.status == 404:
                return None
            raise

    def inspect_schema(self, source_version: int) -> dict[str, Any]:
        return {"compatible": source_version == self.schema_version, "migration": None}

    def migrate(self, source: Path, target_version: int) -> Path:
        if target_version != self.schema_version:
            raise AppError("stateless-mcp schema is incompatible", code=ErrorCode.INVALID_REQUEST, status=409)
        return source

    def build_identity_map(self, source: Path, destination: Path) -> dict[str, str]:
        del source, destination
        return {}

    def rewrite_references(self, raw: bytes, identity_map: dict[str, str], backup_id: str) -> bytes:
        del identity_map, backup_id
        return raw

    def merge_into_staging(self, plan: dict[str, Any], context: BackupContext) -> None:
        del plan, context
        raise AppError("External contributors do not merge into local staging", code=ErrorCode.INVALID_REQUEST)

    def validate_staging(self, staging: Path, context: BackupContext) -> list[str]:
        return self.validate(staging, context)


RegisteredContributor = DirectoryContributor | StatelessMcpContributor


def _participant_journal(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("phase"), str):
        raise AppError("Stateless MCP restore journal is invalid", code=ErrorCode.INVALID_PAYLOAD)
    remapped = value.get("remapped")
    return {
        "sourceDigest": str(value.get("sourceDigest") or ""),
        "preparedDigest": str(value.get("preparedDigest") or ""),
        "phase": value["phase"],
        "imported": int(value.get("imported") or 0),
        "skipped": int(value.get("skipped") or 0),
        "interrupted": int(value.get("interrupted") or 0),
        "remapped": {str(key): str(item) for key, item in remapped.items()} if isinstance(remapped, dict) else {},
    }


def _external_participants(transaction: dict[str, Any]) -> list[dict[str, Any]]:
    contributors = transaction.get("contributors")
    if not isinstance(contributors, list):
        return []
    return [item for item in contributors if isinstance(item, dict) and bool(item.get("external"))]


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
    project_ids = frozenset({"id", "projectId"})
    project_refs = frozenset({"projectId", "sourceProjectId", "parentProjectId", "mediaId", "artifactId", "savedId"})
    return (
        DirectoryContributor(
            "projects", 1, "durable", "merge", lambda: config.PROJECTS_DIR,
            identity_fields=project_ids,
            reference_fields=project_refs,
            path_fields=frozenset({"path", "sourcePath", "artifactPath"}),
        ),
        DirectoryContributor("project-files", 1, "durable", "merge", lambda: config.FILE_CACHE_DIR),
        DirectoryContributor(
            "artifacts", 1, "durable", "merge", lambda: config.GENERATED_DIR,
            identity_fields=frozenset({"artifactId"}),
            reference_fields=frozenset({"artifactId", "projectId", "mediaId"}),
            path_fields=frozenset({"path", "sourcePath"}),
        ),
        DirectoryContributor(
            "memory", 1, "durable", "merge", lambda: config.MEMORY_DIR,
            identity_fields=frozenset({"id"}),
            reference_fields=frozenset({"projectId", "sourceId"}),
        ),
        DirectoryContributor(
            "media", 1, "durable", "merge", lambda: config.MEDIA_DIR,
            identity_fields=frozenset({"mediaId"}),
            reference_fields=frozenset({"mediaId", "projectId", "sourceMediaId"}),
            path_fields=frozenset({"path", "sourcePath", "thumbnailPath"}),
        ),
        DirectoryContributor(
            "custom-skills-and-packs", 1, "durable", "merge", lambda: config.SKILLS_DIR,
            identity_fields=frozenset({"skillId", "packId"}),
            reference_fields=frozenset({"skillId", "packId", "projectId"}),
        ),
        DirectoryContributor(
            "automations", 1, "durable", "merge", lambda: config.AUTOMATION_DIR,
            identity_fields=frozenset({"automationId"}),
            reference_fields=frozenset({"automationId", "projectId", "skillId", "packId"}),
        ),
        DirectoryContributor(
            "reminders", 1, "durable", "merge", lambda: config.REMINDERS_DIR,
            identity_fields=frozenset({"id", "reminderId"}),
            reference_fields=frozenset({"projectId", "conversationId"}),
        ),
        DirectoryContributor(
            "agent-checkpoints", 1, "optional-history", "merge", lambda: config.AGENT_RUNS_DIR,
            identity_fields=frozenset({"runId"}),
            reference_fields=frozenset({"runId", "projectId", "conversationId"}),
        ),
        DirectoryContributor(
            "a2a-tasks", 1, "optional-history", "merge", lambda: config.A2A_TASKS_DIR,
            identity_fields=frozenset({"taskId"}),
            reference_fields=frozenset({"taskId", "projectId"}),
        ),
        DirectoryContributor(
            "traces-and-run-history", 1, "optional-history", "merge", lambda: config.TRACE_DIR,
            identity_fields=frozenset({"traceId", "runId"}),
            reference_fields=frozenset({"traceId", "runId", "projectId", "conversationId"}),
        ),
    )


def capabilities() -> dict[str, Any]:
    included = [item.contributor_id for item in _registered_contributors() if item.data_class == "durable"]
    optional = [item.contributor_id for item in _registered_contributors() if item.data_class == "optional-history"]
    crypto = backup_crypto.capabilities()
    return {
        "ok": True,
        "schemaVersion": BACKUP_SCHEMA,
        "purpose": PACKAGE_PURPOSE,
        "encrypted": bool(crypto["encryptedBackupAvailable"]),
        **crypto,
        "integrityVerified": True,
        "modes": ["full", "project"],
        "restoreModes": ["merge", "project-copy", "replace-empty"],
        "coveragePolicies": ["strict", "best-effort"],
        "includedByDefault": included,
        "optionalHistory": optional,
        "externalContributors": [StatelessMcpContributor().capabilities()],
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
    protection = _protection_from_payload(payload)
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
        "protection": protection,
        "requiresFrontendState": bool(payload.get("requiresFrontendState", True)),
        "estimatedBytes": sum(item["bytes"] for item in estimates),
        "included": [item.contributor_id for item in contributors],
        "excluded": _exclusions(context),
        "coverage": _coverage_status(context),
    }
    _write_json(session_dir / "session.json", state)
    return {"ok": True, **state}


def put_session_secret(session_id: str, payload: dict[str, Any]) -> dict[str, object]:
    kind = str(payload.get("kind") or "")
    secret = str(payload.get("secret") or "")
    if not (session_id.startswith("backup_") or session_id.startswith("restore_")):
        raise AppError("Invalid backup secret session", code=ErrorCode.INVALID_PAYLOAD)
    if session_id.startswith("backup_"):
        _load_session(session_id)
    else:
        _restore_root(session_id)
    return backup_crypto.put_secret(session_id, kind, secret)


def generate_recovery_identity() -> dict[str, Any]:
    return {"ok": True, **backup_crypto.generate_identity(), "displayedOnce": True}


def put_frontend_state(backup_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
    session = _load_session(backup_id)
    if session["phase"] not in {"preparing", "quiescing"}:
        raise AppError("Backup no longer accepts frontend state", code=ErrorCode.INVALID_REQUEST, status=409)
    normalized = _validate_frontend_envelope(envelope)
    _write_json(_session_dir(backup_id) / "frontend.json", normalized)
    session["frontendStateReceived"] = True
    _write_json(_session_dir(backup_id) / "session.json", session)
    return {"ok": True, "backupId": backup_id, "digest": normalized["digest"]}


def finalize_session(
    backup_id: str,
    *,
    owner_restore_id: str | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    session = _load_session(backup_id)
    context = _context_from_payload(session["context"])
    frontend_path = _session_dir(backup_id) / "frontend.json"
    if session.get("requiresFrontendState") and not frontend_path.is_file():
        raise AppError("Verified frontend state is required", code=ErrorCode.INVALID_REQUEST, status=409)
    protection = _protection_from_payload({"protection": session.get("protection")})
    secret: bytearray | None = None
    if protection["mode"] == "passphrase":
        _, secret = backup_crypto.consume_secret(backup_id, "passphrase")
    elif protection["mode"] == "age-recipient":
        _, secret = backup_crypto.consume_secret(backup_id, "age-identity")
        derived = set(backup_crypto.derive_recipients(secret))
        if not derived.intersection(protection["recipients"]):
            secret[:] = b"\x00" * len(secret)
            raise AppError("Recovery Identity does not match a configured recipient", code=ErrorCode.INVALID_REQUEST, status=409)
    with _BARRIER:
        try:
            result: dict[str, Any] | None = None
            gate_root = BACKUP_DIR.parent
            for attempt in range(1, 4):
                session["phase"] = "quiescing"
                session["snapshotAttempt"] = attempt
                _write_json(_session_dir(backup_id) / "session.json", session)
                with mutation_gate.exclusive_gate(gate_root):
                    mutation_gate.assert_mutation_allowed(owner_restore_id, root=gate_root)
                    start_generation = mutation_gate.read_generation(gate_root)
                    for contributor in _selected_contributors(context):
                        contributor.flush(context)
                session["phase"] = "snapshotting"
                _write_json(_session_dir(backup_id) / "session.json", session)
                candidate = _build_archive(
                    backup_id,
                    context,
                    frontend_path if frontend_path.is_file() else None,
                    protection,
                    secret,
                    cancel_event,
                )
                with mutation_gate.exclusive_gate(gate_root):
                    end_generation = mutation_gate.read_generation(gate_root)
                if start_generation == end_generation:
                    result = candidate
                    break
                Path(str(candidate["path"])).unlink(missing_ok=True)
            if result is None:
                raise AppError(
                    "Workspace changed repeatedly during backup; retry when writes are quieter",
                    code=ErrorCode.INVALID_REQUEST,
                    status=409,
                )
            session.update(result)
            session["phase"] = "ready"
        except Exception as exc:
            session["phase"] = "failed"
            session["error"] = str(exc)
            _write_json(_session_dir(backup_id) / "session.json", session)
            raise
        finally:
            if secret is not None:
                secret[:] = b"\x00" * len(secret)
        _write_json(_session_dir(backup_id) / "session.json", session)
    return {"ok": True, **_public_session(session)}


def create_backup(
    payload: dict[str, Any],
    *,
    frontend_state: dict[str, Any] | None = None,
    owner_restore_id: str | None = None,
) -> dict[str, Any]:
    request = dict(payload)
    request["requiresFrontendState"] = frontend_state is not None
    created = create_session(request)
    backup_id = str(created["backupId"])
    if frontend_state is not None:
        put_frontend_state(backup_id, frontend_state)
    return finalize_session(backup_id, owner_restore_id=owner_restore_id)


def _create_safety_backup(plan: dict[str, Any], restore_id: str) -> dict[str, Any]:
    request: dict[str, Any] = {
        "mode": "full",
        "includeHistory": True,
        "requiresFrontendState": False,
        "coveragePolicy": "best-effort",
    }
    kind: backup_crypto.SecretKind | None = None
    secret: bytearray | None = None
    if plan.get("encrypted"):
        expected: backup_crypto.SecretKind = "passphrase" if plan.get("protection") == "passphrase" else "age-identity"
        kind, secret = backup_crypto.consume_secret(restore_id, expected)
        if kind == "passphrase":
            request["protection"] = {"mode": "passphrase"}
        else:
            request["protection"] = {"mode": "age-recipient", "recipients": list(backup_crypto.derive_recipients(secret))}
    try:
        created = create_session(request)
        backup_id = str(created["backupId"])
        if kind is not None and secret is not None:
            backup_crypto.put_secret_bytes(backup_id, kind, bytearray(secret))
        return finalize_session(backup_id, owner_restore_id=restore_id)
    finally:
        if secret is not None:
            secret[:] = b"\x00" * len(secret)


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
    try:
        if _is_age_archive(archive_path):
            header = backup_crypto.inspect_header(archive_path)
            upload = {
                "restoreId": restore_id,
                "phase": "locked",
                "filename": archive_path.name,
                "ciphertextSha256": _sha256_file(archive_path),
                "protection": "passphrase" if header.get("passphrase") else "age-recipient",
                "createdAt": _utc_iso(),
            }
            _write_json(restore_root / "upload.json", upload)
            return {"ok": True, **upload}
        return _inspect_plain_archive(archive_path, restore_root, restore_id, encrypted=False)
    except Exception:
        shutil.rmtree(restore_root, ignore_errors=True)
        raise


def unlock_restore(restore_id: str) -> dict[str, Any]:
    root = _restore_root(restore_id)
    upload_path = root / "upload.json"
    if not upload_path.is_file():
        plan = _read_json(root / "plan.json")
        if plan.get("phase") == "inspected":
            return {"ok": True, **plan}
        raise AppError("Encrypted restore upload is not available", code=ErrorCode.INVALID_REQUEST, status=409)
    upload = _read_json(upload_path)
    source = root / str(upload.get("filename") or "")
    if not source.is_file() or _sha256_file(source) != upload.get("ciphertextSha256"):
        raise AppError("Encrypted backup upload changed", code=ErrorCode.INVALID_PAYLOAD)
    expected: backup_crypto.SecretKind = "passphrase" if upload.get("protection") == "passphrase" else "age-identity"
    kind, secret = backup_crypto.consume_secret(restore_id, expected)
    unlocked = root / "unlocked.dsibackup"
    try:
        backup_crypto.decrypt_file(source, unlocked, kind=kind, secret=secret)
        result = _inspect_plain_archive(
            unlocked,
            root,
            restore_id,
            encrypted=True,
            ciphertext_sha256=str(upload["ciphertextSha256"]),
            protection=str(upload["protection"]),
        )
        # The same in-memory secret protects the pre-restore Safety Backup. It
        # remains ephemeral and must be re-entered after a server restart.
        backup_crypto.put_secret_bytes(restore_id, kind, bytearray(secret))
        return result
    except Exception as exc:
        unlocked.unlink(missing_ok=True)
        shutil.rmtree(root / "extracted", ignore_errors=True)
        backup_crypto.record_unlock_failure(restore_id)
        if isinstance(exc, AppError) and "helper unavailable" in str(exc).casefold():
            raise
        raise AppError("Unable to unlock backup", code=ErrorCode.INVALID_PAYLOAD) from None
    finally:
        secret[:] = b"\x00" * len(secret)


def _inspect_plain_archive(
    archive_path: Path,
    restore_root: Path,
    restore_id: str,
    *,
    encrypted: bool,
    ciphertext_sha256: str | None = None,
    protection: str = "none",
) -> dict[str, Any]:
    extracted = restore_root / "extracted"
    manifest = _safe_extract_and_verify(archive_path, extracted)
    context = _context_from_manifest(manifest)
    operations: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    migrations: list[dict[str, Any]] = []
    compatible = _version_compatible(str(manifest["source"].get("version") or ""), config.APP_VERSION)
    known: dict[str, RegisteredContributor] = {item.contributor_id: item for item in _registered_contributors()}
    external_contributor = StatelessMcpContributor()
    known[external_contributor.contributor_id] = external_contributor
    for entry in manifest["contributors"]:
        contributor = known.get(str(entry["id"]))
        if contributor is None:
            raise AppError(f"Unsupported contributor: {entry['id']}", code=ErrorCode.INVALID_PAYLOAD)
        try:
            source_schema = int(entry.get("schemaVersion"))
        except (TypeError, ValueError):
            source_schema = -1
        schema = contributor.inspect_schema(source_schema)
        compatible = compatible and bool(schema["compatible"])
        if schema["migration"]:
            migrations.append(schema["migration"])
        source_dir = extracted / "payload" / contributor.contributor_id
        errors = contributor.validate(source_dir, context)
        if errors:
            raise AppError("; ".join(errors), code=ErrorCode.INVALID_PAYLOAD)
        operation = contributor.plan_restore(source_dir, context)
        if operation.get("external") and not operation.get("available"):
            compatible = False
        operations.append(operation)
        if operation["conflicts"]:
            conflicts.append({"contributorId": contributor.contributor_id, "count": operation["conflicts"], "strategy": "deterministic-remap"})
    plan = {
        "restoreId": restore_id,
        "sourceVersion": manifest["source"]["version"],
        "targetVersion": config.APP_VERSION,
        "compatible": compatible,
        "purpose": manifest["purpose"],
        "operations": operations,
        "conflicts": conflicts,
        "migrations": migrations,
        "warnings": [] if encrypted else ["Backup is integrity-verified but not encrypted."],
        "estimatedWriteBytes": sum(int(item["size"]) for item in manifest["files"]),
        "requiresFrontendApply": bool(manifest.get("frontend")),
        "archiveSha256": _sha256_file(archive_path),
        "ciphertextSha256": ciphertext_sha256,
        "encrypted": encrypted,
        "protection": protection,
        "coverage": manifest.get("coverage") or {},
        "manifest": manifest,
        "phase": "inspected",
    }
    _write_json(restore_root / "plan.json", plan)
    return {"ok": True, **plan}


def prepare_restore(
    restore_id: str,
    *,
    mode: str = "merge",
    previous_epoch: str = "legacy",
    target_epoch: str | None = None,
    owner_document_id: str = "server",
) -> dict[str, Any]:
    """Build and verify every target contributor without touching live data."""

    if mode not in {"merge", "project-copy", "replace-empty"}:
        raise AppError("Unsupported restore mode", code=ErrorCode.INVALID_PAYLOAD)
    root = _restore_root(restore_id)
    journal_path = root / "transaction.json"
    if journal_path.is_file():
        existing = _read_json(journal_path)
        if (
            existing.get("mode") != mode
            or (target_epoch and existing.get("targetEpoch") != target_epoch)
            or existing.get("previousEpoch") != previous_epoch
        ):
            raise AppError("Restore retry parameters do not match the durable transaction", code=ErrorCode.INVALID_REQUEST, status=409)
        return _public_restore(existing, root)
    plan = _read_json(root / "plan.json")
    if not plan.get("compatible"):
        raise AppError("Backup schema is not compatible with this version", code=ErrorCode.INVALID_REQUEST, status=409)
    archive = next(root.glob("*.dsibackup"), None)
    if archive is None or _sha256_file(archive) != plan.get("archiveSha256"):
        raise AppError("Backup changed after inspection", code=ErrorCode.INVALID_PAYLOAD)

    target = target_epoch or f"epoch-{uuid.uuid4()}"
    now = int(time.time() * 1000)
    fence = {
        "schemaVersion": 1,
        "restoreId": restore_id,
        "previousEpoch": previous_epoch,
        "targetEpoch": target,
        "ownerDocumentId": owner_document_id,
        "phase": "preparing",
        "createdAt": now,
        "expiresAt": now + 24 * 60 * 60 * 1000,
    }
    gate_root = RESTORE_DIR.parent
    with mutation_gate.exclusive_gate(gate_root):
        active = mutation_gate.read_fence(gate_root)
        if active and active.get("restoreId") != restore_id:
            raise AppError("Another workspace restore is active", code=ErrorCode.INVALID_REQUEST, status=423)
        mutation_gate.write_fence(fence, gate_root)
        transaction: dict[str, Any] = {
            **fence,
            "mode": mode,
            "phase": "preparing",
            "updatedAt": _utc_iso(),
            "requiresFrontendApply": bool(plan.get("requiresFrontendApply")),
            "contributors": [],
            "identityMap": {},
        }
        _write_json(journal_path, transaction)
        try:
            manifest = _safe_extract_and_verify(archive, root / "verified")
            context = _context_from_manifest(manifest)
            identity_map = _restore_identity_map(plan, mode)
            transaction["identityMap"] = identity_map
            safety = _create_safety_backup(plan, restore_id)
            transaction["safetyBackupId"] = safety["backupId"]
            _write_json(journal_path, transaction)
            known: dict[str, RegisteredContributor] = {item.contributor_id: item for item in _registered_contributors()}
            external_contributor = StatelessMcpContributor()
            known[external_contributor.contributor_id] = external_contributor
            staged_root = root / "staged"
            shutil.rmtree(staged_root, ignore_errors=True)
            staged_root.mkdir(parents=True)
            for operation in plan["operations"]:
                contributor_id = str(operation["contributorId"])
                contributor = known[contributor_id]
                if bool(operation.get("external")):
                    source = root / "verified" / "payload" / contributor_id
                    errors = contributor.validate(source, context)
                    if errors:
                        raise AppError("; ".join(errors), code=ErrorCode.INVALID_PAYLOAD)
                    transaction["contributors"].append(
                        {
                            "id": contributor_id,
                            "external": True,
                            "source": str(source),
                            "prepared": False,
                            "swapped": False,
                            "verified": True,
                            "digest": _tree_digest(source),
                            "swapState": "validated",
                            "phase": "preparing",
                        }
                    )
                    _write_json(journal_path, transaction)
                    continue
                destination = Path(str(operation["destination"]))
                staged = staged_root / contributor_id
                if destination.exists():
                    shutil.copytree(destination, staged)
                else:
                    staged.mkdir(parents=True)
                current = dict(operation)
                current.update(
                    {
                        "source": str(root / "verified" / "payload" / contributor_id),
                        "destination": str(staged),
                        "mode": mode,
                        "identityMap": identity_map,
                        "sourceBackupId": str(plan["manifest"].get("backupId") or ""),
                    }
                )
                migrated = contributor.migrate(Path(str(current["source"])), contributor.schema_version)
                current["source"] = str(migrated)
                contributor.merge_into_staging(current, context)
                errors = contributor.validate_staging(staged, context)
                if errors:
                    raise AppError("; ".join(errors), code=ErrorCode.INVALID_PAYLOAD)
                _fsync_tree(staged)
                transaction["contributors"].append(
                    {
                        "id": contributor_id,
                        "destination": str(destination),
                        "stagedPath": str(staged),
                        "rollbackPath": str(root / "rollback" / contributor_id),
                        "hadDestination": destination.exists(),
                        "prepared": True,
                        "swapped": False,
                        "verified": True,
                        "digest": _tree_digest(staged),
                        "swapState": "prepared",
                    }
                )
                _write_json(journal_path, transaction)
            transaction["serverTransactionDigest"] = hashlib.sha256(
                _stable_json(
                    {
                        "restoreId": restore_id,
                        "mode": mode,
                        "targetEpoch": target,
                        "contributors": [
                            {"id": item["id"], "digest": item["digest"]}
                            for item in transaction["contributors"]
                        ],
                    }
                )
            ).hexdigest()
            participant = StatelessMcpContributor()
            for external in _external_participants(transaction):
                journal = participant.prepare_restore(
                    restore_id,
                    Path(str(external["source"])),
                    str(transaction["serverTransactionDigest"]),
                )
                external["transactionDigest"] = transaction["serverTransactionDigest"]
                external["participant"] = journal
                external["phase"] = journal["phase"]
                external["prepared"] = True
                external["swapState"] = "prepared"
                _write_json(journal_path, transaction)
            transaction["phase"] = "backend-staged"
            transaction["updatedAt"] = _utc_iso()
            fence["phase"] = "preparing" if plan.get("requiresFrontendApply") else "commit-intent"
            mutation_gate.write_fence(fence, gate_root)
            _write_json(journal_path, transaction)
        except Exception as exc:
            transaction["phase"] = "failed"
            transaction["error"] = str(exc)
            transaction["updatedAt"] = _utc_iso()
            _write_json(journal_path, transaction)
            mutation_gate.clear_fence(restore_id, gate_root)
            _cleanup_restore_payload(root, remove_upload=True)
            raise
    return _public_restore(transaction, root)


def frontend_prepared(restore_id: str, *, digest: str) -> dict[str, Any]:
    root = _restore_root(restore_id)
    journal_path = root / "transaction.json"
    transaction = _read_json(journal_path)
    if transaction.get("phase") in {"frontend-staged", "commit-intent", "frontend-committed", "backend-committed", "complete"}:
        if transaction.get("frontendDigest") not in {None, digest}:
            raise AppError("Frontend restore digest changed", code=ErrorCode.INVALID_REQUEST, status=409)
        return _public_restore(transaction, root)
    if transaction.get("phase") != "backend-staged" or not transaction.get("requiresFrontendApply"):
        raise AppError("Restore is not waiting for frontend staging", code=ErrorCode.INVALID_REQUEST, status=409)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.casefold()):
        raise AppError("Frontend restore digest is invalid", code=ErrorCode.INVALID_PAYLOAD)
    transaction["frontendDigest"] = digest.casefold()
    transaction["phase"] = "frontend-staged"
    transaction["updatedAt"] = _utc_iso()
    _write_json(journal_path, transaction)
    _update_server_fence(transaction, "frontend-staged")
    return _public_restore(transaction, root)


def _external_commit_intent(transaction: dict[str, Any], restore_id: str) -> None:
    participant = StatelessMcpContributor()
    for contributor in _external_participants(transaction):
        journal = participant.commit_restore_intent(restore_id, str(transaction["serverTransactionDigest"]))
        contributor["participant"] = journal
        contributor["phase"] = journal["phase"]


def commit_restore(
    restore_id: str,
    *,
    frontend_committed: bool = False,
    frontend_digest: str | None = None,
) -> dict[str, Any]:
    """Persist commit intent, then atomically exchange all staged directories.

    A frontend restore uses two idempotent calls.  The first records commit
    intent and returns; the owner switches the single active-epoch pointer, then
    the second call confirms that switch and commits the backend.
    """

    root = _restore_root(restore_id)
    journal_path = root / "transaction.json"
    gate_root = RESTORE_DIR.parent
    with mutation_gate.exclusive_gate(gate_root):
        transaction = _read_json(journal_path)
        phase = str(transaction.get("phase") or "")
        if phase in {"backend-committed", "complete"}:
            return _public_restore(transaction, root)
        requires_frontend = bool(transaction.get("requiresFrontendApply"))
        if requires_frontend:
            if frontend_digest and frontend_digest != transaction.get("frontendDigest"):
                raise AppError("Frontend restore digest does not match staged state", code=ErrorCode.INVALID_REQUEST, status=409)
            if phase == "frontend-staged":
                transaction["phase"] = "commit-intent"
                transaction["commitIntentAt"] = _utc_iso()
                transaction["updatedAt"] = _utc_iso()
                _write_json(journal_path, transaction)
                _update_server_fence(transaction, "commit-intent")
                _external_commit_intent(transaction, restore_id)
                _write_json(journal_path, transaction)
                if not frontend_committed:
                    return _public_restore(transaction, root)
            elif phase != "commit-intent":
                raise AppError("Restore is not ready to commit", code=ErrorCode.INVALID_REQUEST, status=409)
            if not frontend_committed:
                return _public_restore(transaction, root)
            transaction["phase"] = "frontend-committed"
            transaction["frontendCommittedAt"] = _utc_iso()
            _write_json(journal_path, transaction)
            _update_server_fence(transaction, "frontend-committed")
        elif phase == "backend-staged":
            transaction["phase"] = "commit-intent"
            transaction["commitIntentAt"] = _utc_iso()
            _write_json(journal_path, transaction)
            _update_server_fence(transaction, "commit-intent")
            _external_commit_intent(transaction, restore_id)
            _write_json(journal_path, transaction)
        elif phase not in {"commit-intent", "frontend-committed"}:
            raise AppError("Restore is not ready to commit", code=ErrorCode.INVALID_REQUEST, status=409)

        try:
            rollback_root = root / "rollback"
            rollback_root.mkdir(parents=True, exist_ok=True)
            local_contributors = [item for item in transaction["contributors"] if not bool(item.get("external"))]
            for contributor in [*local_contributors, *_external_participants(transaction)]:
                if bool(contributor.get("external")):
                    contributor["swapState"] = "committing-external"
                    _write_json(journal_path, transaction)
                    journal = StatelessMcpContributor().commit_restore(
                        restore_id,
                        str(transaction["serverTransactionDigest"]),
                    )
                    contributor["participant"] = journal
                    contributor["phase"] = journal["phase"]
                    contributor["swapped"] = True
                    contributor["swapState"] = "swapped"
                    _write_json(journal_path, transaction)
                    continue
                destination = Path(str(contributor["destination"]))
                staged = Path(str(contributor["stagedPath"]))
                rollback = Path(str(contributor["rollbackPath"]))
                contributor["swapState"] = "moving-old"
                _write_json(journal_path, transaction)
                if bool(contributor["hadDestination"]) and destination.exists():
                    rollback.parent.mkdir(parents=True, exist_ok=True)
                    if rollback.exists():
                        shutil.rmtree(rollback)
                    os.replace(destination, rollback)
                    _fsync_directory(destination.parent)
                contributor["swapState"] = "old-moved"
                _write_json(journal_path, transaction)
                destination.parent.mkdir(parents=True, exist_ok=True)
                contributor["swapState"] = "installing-staged"
                _write_json(journal_path, transaction)
                os.replace(staged, destination)
                _fsync_directory(destination.parent)
                contributor["swapped"] = True
                contributor["swapState"] = "swapped"
                _write_json(journal_path, transaction)
            transaction["phase"] = "backend-committed"
            transaction["backendCommittedAt"] = _utc_iso()
            transaction["updatedAt"] = _utc_iso()
            _write_json(journal_path, transaction)
            _update_server_fence(transaction, "backend-committed")
            mutation_gate.bump_generation(gate_root)
        except Exception:
            _rollback_transaction(transaction, root)
            raise
    return _public_restore(transaction, root)


def complete_restore(restore_id: str, *, frontend_digest: str | None = None) -> dict[str, Any]:
    root = _restore_root(restore_id)
    journal_path = root / "transaction.json"
    gate_root = RESTORE_DIR.parent
    with mutation_gate.exclusive_gate(gate_root):
        transaction = _read_json(journal_path)
        if transaction.get("phase") == "complete":
            result = _public_restore(transaction, root)
            _cleanup_restore_payload(root, remove_upload=True)
            return result
        if transaction.get("phase") != "backend-committed":
            raise AppError("Backend restore is not committed", code=ErrorCode.INVALID_REQUEST, status=409)
        if transaction.get("requiresFrontendApply") and frontend_digest != transaction.get("frontendDigest"):
            raise AppError("Frontend completion digest does not match", code=ErrorCode.INVALID_REQUEST, status=409)
        participant = StatelessMcpContributor()
        for contributor in _external_participants(transaction):
            if contributor.get("phase") == "committed-pending-complete":
                journal = participant.complete_restore(restore_id)
                contributor["participant"] = journal
                contributor["phase"] = journal["phase"]
        transaction["phase"] = "complete"
        transaction["completedAt"] = _utc_iso()
        transaction["updatedAt"] = _utc_iso()
        _write_json(journal_path, transaction)
        mutation_gate.clear_fence(restore_id, gate_root)
        result = _public_restore(transaction, root)
        _cleanup_restore_payload(root, remove_upload=True)
    return result


def abort_restore(restore_id: str) -> dict[str, Any]:
    root = _restore_root(restore_id)
    gate_root = RESTORE_DIR.parent
    with mutation_gate.exclusive_gate(gate_root):
        transaction = _read_json(root / "transaction.json")
        if transaction.get("phase") == "rolled-back":
            result = _public_restore(transaction, root)
            _cleanup_restore_payload(root, remove_upload=True)
            return result
        if transaction.get("phase") == "complete":
            raise AppError("Completed restore cannot be aborted", code=ErrorCode.INVALID_REQUEST, status=409)
        _rollback_transaction(transaction, root)
        result = _public_restore(transaction, root)
        _cleanup_restore_payload(root, remove_upload=True)
    return result


def get_restore(restore_id: str) -> dict[str, Any]:
    root = _restore_root(restore_id)
    metadata = next((root / name for name in ("transaction.json", "plan.json", "upload.json") if (root / name).is_file()), None)
    if metadata is None:
        raise AppError("Restore metadata is unavailable", code=ErrorCode.INVALID_PAYLOAD)
    return _public_restore(_read_json(metadata), root)


def list_restores() -> dict[str, Any]:
    RESTORE_DIR.mkdir(parents=True, exist_ok=True)
    restores: list[dict[str, Any]] = []
    for root in sorted((item for item in RESTORE_DIR.iterdir() if item.is_dir()), key=lambda item: item.name):
        metadata = next((root / name for name in ("transaction.json", "plan.json", "upload.json") if (root / name).is_file()), root / "plan.json")
        try:
            restores.append(_public_restore(_read_json(metadata), root, include_frontend=False))
        except AppError:
            restores.append({"restoreId": root.name, "phase": "recovery-required", "compatible": False})
    return {"ok": True, "restores": restores}


def delete_restore(restore_id: str) -> bool:
    root = _restore_root(restore_id)
    metadata_path = next((root / name for name in ("transaction.json", "plan.json", "upload.json") if (root / name).is_file()), None)
    if metadata_path is None:
        raise AppError("Restore metadata is unavailable", code=ErrorCode.INVALID_PAYLOAD)
    metadata = _read_json(metadata_path)
    if metadata.get("phase") not in {"locked", "inspected", "complete", "rolled-back", "failed"}:
        raise AppError("Active restore records cannot be deleted", code=ErrorCode.INVALID_REQUEST, status=409)
    fence = mutation_gate.read_fence(RESTORE_DIR.parent)
    if fence and fence.get("restoreId") == restore_id:
        raise AppError("Restore is still referenced by the active fence", code=ErrorCode.INVALID_REQUEST, status=409)
    backup_crypto.clear_secret(restore_id)
    shutil.rmtree(root)
    return True


def cleanup_restores(*, now: float | None = None) -> dict[str, Any]:
    current = time.time() if now is None else now
    deleted: list[str] = []
    for item in list_restores()["restores"]:
        phase = item.get("phase")
        if phase not in {"complete", "rolled-back", "failed"}:
            continue
        retention_days = 7 if phase == "complete" else 30
        timestamp = item.get("completedAt") or item.get("rolledBackAt") or item.get("updatedAt") or item.get("createdAt")
        age = _iso_age_seconds(timestamp, current)
        if age is None or age < retention_days * 86_400:
            continue
        restore_id = str(item["restoreId"])
        if delete_restore(restore_id):
            deleted.append(restore_id)
    return {"ok": True, "deleted": deleted}


def recover_interrupted_restores() -> dict[str, Any]:
    """Reconcile durable journals after a process or machine crash."""

    RESTORE_DIR.mkdir(parents=True, exist_ok=True)
    recovered: list[str] = []
    committed: list[str] = []
    required: list[str] = []
    for root in sorted((item for item in RESTORE_DIR.iterdir() if item.is_dir()), key=lambda item: item.name):
        journal_path = root / "transaction.json"
        if not journal_path.is_file():
            continue
        try:
            transaction = _read_json(journal_path)
        except AppError:
            required.append(root.name)
            mutation_gate.write_fence(
                {
                    "schemaVersion": 1,
                    "restoreId": root.name,
                    "previousEpoch": "unknown",
                    "targetEpoch": "unknown",
                    "ownerDocumentId": "startup-recovery",
                    "phase": "recovery-required",
                    "createdAt": int(time.time() * 1000),
                    "expiresAt": 2**63 - 1,
                },
                RESTORE_DIR.parent,
            )
            continue
        phase = str(transaction.get("phase") or "")
        if phase in {"complete", "rolled-back", "failed", "backend-committed"}:
            continue
        try:
            contributors = transaction.get("contributors")
            if phase in {"commit-intent", "frontend-committed"} and isinstance(contributors, list) and contributors:
                all_installed = True
                for contributor in contributors:
                    if not isinstance(contributor, dict):
                        all_installed = False
                        break
                    if bool(contributor.get("external")):
                        if not bool(contributor.get("swapped")):
                            all_installed = False
                            break
                        continue
                    destination = Path(str(contributor.get("destination") or ""))
                    staged = Path(str(contributor.get("stagedPath") or ""))
                    if (
                        not destination.is_dir()
                        or staged.exists()
                        or _tree_digest(destination) != contributor.get("digest")
                    ):
                        all_installed = False
                        break
                if all_installed:
                    for contributor in contributors:
                        contributor["swapped"] = True
                        contributor["swapState"] = "swapped"
                    transaction["phase"] = "backend-committed"
                    transaction["backendCommittedAt"] = _utc_iso()
                    transaction["updatedAt"] = _utc_iso()
                    _write_json(journal_path, transaction)
                    _update_server_fence(transaction, "backend-committed")
                    committed.append(root.name)
                    continue
            _rollback_transaction(transaction, root)
            recovered.append(root.name)
        except Exception:
            transaction["phase"] = "recovery-required"
            transaction["updatedAt"] = _utc_iso()
            _write_json(journal_path, transaction)
            _update_server_fence(transaction, "recovery-required")
            required.append(root.name)
    return {
        "ok": not required,
        "rolledBack": recovered,
        "backendCommitted": committed,
        "recoveryRequired": required,
    }


def apply_restore(restore_id: str, *, mode: str = "merge") -> dict[str, Any]:
    """Compatibility wrapper for the 4.4.0 single-call restore API."""

    if mode not in {"merge", "project-copy", "replace-empty"}:
        raise AppError("Unsupported restore mode", code=ErrorCode.INVALID_PAYLOAD)
    root = _restore_root(restore_id)
    plan = _read_json(root / "plan.json")
    if plan.get("requiresFrontendApply"):
        raise AppError(
            "Frontend replica acknowledgement is required; use the coordinated restore API",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    prepare_restore(restore_id, mode=mode)
    committed = commit_restore(restore_id)
    committed = complete_restore(restore_id)
    return {
        **committed,
        "phase": "complete",
        "restoreEpoch": int(time.time() * 1000),
    }


def _write_zip_tree(staging: Path, output: BinaryIO) -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted((item for item in staging.rglob("*") if item.is_file()), key=lambda item: item.relative_to(staging).as_posix()):
            info = zipfile.ZipInfo(path.relative_to(staging).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            with path.open("rb") as source, archive.open(info, "w") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)


def _build_archive(
    backup_id: str,
    context: BackupContext,
    frontend_path: Path | None,
    protection: dict[str, Any] | None = None,
    secret: bytearray | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    session_dir = _session_dir(backup_id)
    staging = session_dir / "staging"
    normalized_protection = _protection_from_payload({"protection": protection})
    encrypted = normalized_protection["mode"] != "none"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    suffix = ".dsibackup.age" if encrypted else ".dsibackup"
    filename = f"deepseek-infra-backup-{time.strftime('%Y%m%d')}-{backup_id[-8:]}{suffix}"
    temporary = BACKUP_DIR / f".{filename}.tmp"
    target = BACKUP_DIR / filename
    verification_dir = session_dir / "verification"
    decrypted_verification = session_dir / "verification.dsibackup"
    published = False
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise AppError("Backup creation cancelled", code=ErrorCode.INVALID_REQUEST, status=499)
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        contributions = [contributor.snapshot(staging, context) for contributor in _selected_contributors(context)]
        frontend_manifest: dict[str, Any] | None = None
        files = [entry for contribution in contributions for entry in contribution.files]
        if frontend_path is not None:
            frontend_target = staging / "frontend" / "state.json"
            frontend_target.parent.mkdir(parents=True)
            shutil.copyfile(frontend_path, frontend_target)
            entry = {
                "path": "frontend/state.json",
                "size": frontend_target.stat().st_size,
                "sha256": _sha256_file(frontend_target),
            }
            files.append(entry)
            envelope = _read_json(frontend_target)
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
        files.append(
            {
                "path": "migration/source-schemas.json",
                "size": len(raw_migration),
                "sha256": hashlib.sha256(raw_migration).hexdigest(),
            }
        )
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
            "coverage": _coverage_status(context),
            "exclusions": _exclusions(context),
            "files": files,
            "encrypted": encrypted,
        }
        if frontend_manifest:
            manifest["frontend"] = frontend_manifest
        manifest_bytes = _stable_json(manifest)
        (staging / "manifest.json").write_bytes(manifest_bytes)
        checksums = [f"{item['sha256']}  {item['path']}" for item in files]
        checksums.append(f"{hashlib.sha256(manifest_bytes).hexdigest()}  manifest.json")
        (staging / "checksums.sha256").write_text("\n".join(checksums) + "\n", encoding="utf-8", newline="\n")
        if encrypted:
            if secret is None:
                raise AppError("Backup secret is required or expired", code=ErrorCode.INVALID_REQUEST, status=409)
            backup_crypto.encrypt_stream(
                target,
                lambda output: _write_zip_tree(staging, output),
                mode=normalized_protection["mode"],
                secret=secret if normalized_protection["mode"] == "passphrase" else None,
                recipients=tuple(normalized_protection.get("recipients") or ()),
                cancel_event=cancel_event,
            )
        else:
            with temporary.open("wb") as output:
                _write_zip_tree(staging, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        _fsync_directory(target.parent)
        verification_archive = target
        if encrypted:
            assert secret is not None
            verification_archive = decrypted_verification
            backup_crypto.decrypt_file(
                target,
                verification_archive,
                kind="passphrase" if normalized_protection["mode"] == "passphrase" else "age-identity",
                secret=secret,
                cancel_event=cancel_event,
            )
        _safe_extract_and_verify(verification_archive, verification_dir)
        result = {
            "filename": filename,
            "path": str(target),
            "size": target.stat().st_size,
            "manifestDigest": hashlib.sha256(manifest_bytes).hexdigest(),
            "protection": {"mode": normalized_protection["mode"]},
            "downloadUrl": f"/api/workspace/backups/{backup_id}/download",
        }
        published = True
        return result
    finally:
        temporary.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(verification_dir, ignore_errors=True)
        decrypted_verification.unlink(missing_ok=True)
        if not published:
            target.unlink(missing_ok=True)


def _safe_extract_and_verify(archive_path: Path, destination: Path) -> dict[str, Any]:
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    seen: set[str] = set()
    declared_total = 0
    actual_total = 0
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
            declared_total += info.file_size
            if declared_total > MAX_EXPANDED_BYTES:
                raise AppError("Expanded backup is too large", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
            if info.compress_size == 0 and info.file_size > 0:
                raise AppError("Suspicious compression ratio", code=ErrorCode.INVALID_PAYLOAD)
            if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise AppError("Suspicious compression ratio", code=ErrorCode.INVALID_PAYLOAD)
            target = destination.joinpath(*PurePosixPath(normalized).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not info.is_dir():
                with archive.open(info) as source, target.open("wb") as output:
                    actual_entry = 0
                    while chunk := source.read(1024 * 1024):
                        actual_entry += len(chunk)
                        actual_total += len(chunk)
                        if actual_entry > MAX_EXPANDED_BYTES or actual_total > MAX_EXPANDED_BYTES:
                            raise AppError("Expanded backup is too large", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
                        output.write(chunk)
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


def _selected_contributors(context: BackupContext) -> tuple[RegisteredContributor, ...]:
    local: tuple[RegisteredContributor, ...] = tuple(
        item
        for item in _registered_contributors()
        if item.data_class == "durable"
        or (item.data_class == "optional-history" and context.include_history)
        or (item.data_class == "rebuildable" and context.include_rebuildable_indexes)
    )
    if context.mode != "full" or not context.include_external_state or not os.environ.get("STATELESS_MCP_BACKUP_URL", "").strip():
        return local
    external = StatelessMcpContributor()
    status = external.capabilities()
    if not status.get("available"):
        if context.coverage_policy == "strict":
            raise AppError("Strict backup coverage requires Stateless MCP durable state", code=ErrorCode.INVALID_REQUEST, status=409)
        return local
    return (*local, external)


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
    coverage_policy = str(payload.get("coveragePolicy") or "strict")
    if coverage_policy not in {"strict", "best-effort"}:
        raise AppError("Coverage policy must be strict or best-effort", code=ErrorCode.INVALID_PAYLOAD)
    return BackupContext(
        mode=mode,
        project_ids=ids,
        include_history=bool(payload.get("includeHistory", False)),
        include_drafts=bool(payload.get("includeDrafts", False)),
        include_rebuildable_indexes=bool(payload.get("includeRebuildableIndexes", False)),
        coverage_policy=cast(CoveragePolicy, coverage_policy),
        include_external_state=bool(payload.get("includeExternalState", True)),
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
        "coveragePolicy": context.coverage_policy,
        "includeExternalState": context.include_external_state,
    }


def _protection_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("protection")
    value = raw if isinstance(raw, dict) else {"mode": "none"}
    mode = str(value.get("mode") or "none")
    if mode not in {"none", "passphrase", "age-recipient"}:
        raise AppError("Unsupported backup protection mode", code=ErrorCode.INVALID_PAYLOAD)
    if mode != "none" and not bool(backup_crypto.capabilities()["encryptedBackupAvailable"]):
        raise AppError("Backup crypto helper unavailable", code=ErrorCode.INVALID_REQUEST, status=501)
    result: dict[str, Any] = {"mode": mode}
    if mode == "age-recipient":
        raw_recipients = value.get("recipients")
        recipients = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in raw_recipients
                if isinstance(item, str) and str(item).strip()
            )
        ) if isinstance(raw_recipients, list | tuple) else ()
        if not recipients or len(recipients) > 16 or any(not item.startswith("age1") or len(item) > 200 for item in recipients):
            raise AppError("One or more valid age recipients are required", code=ErrorCode.INVALID_PAYLOAD)
        result["recipients"] = list(recipients)
    return result


def _coverage_status(context: BackupContext) -> dict[str, Any]:
    local = [
        item.contributor_id
        for item in _registered_contributors()
        if item.data_class == "durable" or (item.data_class == "optional-history" and context.include_history)
    ]
    configured = bool(os.environ.get("STATELESS_MCP_BACKUP_URL", "").strip())
    external: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    if configured and context.include_external_state:
        status = StatelessMcpContributor().capabilities()
        if status.get("available"):
            external.append({"id": "stateless-mcp", "status": "available", "schemaVersion": 1})
        else:
            unavailable.append({"id": "stateless-mcp", "reason": str(status.get("reason") or "service unavailable")})
    elif configured:
        unavailable.append({"id": "stateless-mcp", "reason": "excluded by backup request"})
    return {
        "policy": context.coverage_policy,
        "localContributors": local,
        "externalContributors": external,
        "unavailableDurableSources": unavailable,
        "complete": not unavailable,
    }


def _jsonl_task_count(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("type") == "task":
                count += 1
    return count


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
    known = {item.contributor_id: item for item in _registered_contributors()}
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
        contributor = known.get(str(operation.get("contributorId") or ""))
        source_objects = (
            contributor.build_identity_map(source, destination)
            if contributor
            else {}
        )
        for original_id, source_digest in source_objects.items():
            if original_id not in result:
                result[original_id] = _restored_id(original_id, backup_id, source_digest)
    return result


_REFERENCE_ID_FIELDS = frozenset({
    "id",
    "projectId",
    "conversationId",
    "messageId",
    "savedId",
    "artifactId",
    "mediaId",
    "automationId",
    "traceId",
})


def _json_identity_index(root: Path, identity_fields: frozenset[str] = _REFERENCE_ID_FIELDS) -> dict[str, str]:
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
        for key in identity_fields:
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


def _rewrite_typed_json_references(
    raw: bytes,
    identity_map: dict[str, str],
    backup_id: str,
    *,
    identity_fields: frozenset[str],
    reference_fields: frozenset[str],
    path_fields: frozenset[str],
) -> bytes:
    """Rewrite only fields declared by a contributor schema.

    User messages, prompts, skill bodies, and arbitrary strings are traversed
    but never modified merely because they happen to contain a colliding id.
    """

    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw

    def rewrite_path(item: str) -> str:
        parts = item.replace("\\", "/").split("/")
        rewritten_parts = [
            identity_map.get(part, identity_map.get(Path(part).stem, Path(part).stem) + Path(part).suffix)
            for part in parts
        ]
        return "/".join(rewritten_parts)

    def rewrite(item: Any) -> Any:
        if isinstance(item, list):
            return [rewrite(value) for value in item]
        if isinstance(item, dict):
            result: dict[str, Any] = {}
            original_id: str | None = None
            for key, child in item.items():
                if isinstance(child, str) and key in identity_fields | reference_fields:
                    if key in identity_fields and child in identity_map:
                        original_id = child
                    result[key] = identity_map.get(child, child)
                elif isinstance(child, str) and key in path_fields:
                    result[key] = rewrite_path(child)
                else:
                    result[key] = rewrite(child)
            if original_id:
                result["importedFrom"] = {"originalId": original_id, "sourceBackupId": backup_id}
            return result
        return item

    return _stable_json(rewrite(value))


def _rewrite_json_references(raw: bytes, identity_map: dict[str, str], backup_id: str) -> bytes:
    """Legacy test/helper surface; restore contributors never call this."""

    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw

    def rewrite(item: Any) -> Any:
        if isinstance(item, str):
            return identity_map.get(item, item)
        if isinstance(item, list):
            return [rewrite(child) for child in item]
        if isinstance(item, dict):
            result = {key: rewrite(child) for key, child in item.items()}
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
    if name.lower().endswith(".dsibackup.age"):
        return name[:120]
    if not name.lower().endswith(".dsibackup"):
        name += ".dsibackup"
    return name[:120]


def _is_age_archive(path: Path) -> bool:
    try:
        with path.open("rb") as source:
            return source.read(22).startswith(b"age-encryption.org/v1")
    except OSError:
        return False


def _version_compatible(source: str, target: str) -> bool:
    def parse(value: str) -> tuple[int, int, int] | None:
        try:
            parts = value.split("-", 1)[0].split(".")
            if len(parts) != 3:
                return None
            return int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            return None

    source_version = parse(source)
    target_version = parse(target)
    if source_version is None or target_version is None:
        return False
    return source_version[0] == target_version[0] and source_version <= target_version


def _public_restore(
    metadata: dict[str, Any],
    root: Path,
    *,
    include_frontend: bool = True,
) -> dict[str, Any]:
    identity_map = metadata.get("identityMap")
    if not isinstance(identity_map, dict):
        identity_map = {}
    manifest = metadata.get("manifest")
    if not isinstance(manifest, dict):
        try:
            manifest = _read_json(root / "plan.json").get("manifest", {})
        except AppError:
            manifest = {}
    frontend_path = root / "verified" / "frontend" / "state.json"
    if not frontend_path.is_file():
        frontend_path = root / "extracted" / "frontend" / "state.json"
    result = {
        "ok": True,
        **{
            key: value
            for key, value in metadata.items()
            if key not in {"contributors", "identityMap", "manifest", "operations"}
        },
        "restoreId": str(metadata.get("restoreId") or root.name),
        "applied": [
            item.get("id")
            for item in metadata.get("contributors", [])
            if isinstance(item, dict) and item.get("swapped")
        ],
        "restoredIdentities": [
            {
                "originalId": original,
                "restoredId": restored,
                "reason": "collision",
                "sourceBackupId": str(manifest.get("backupId") or ""),
            }
            for original, restored in sorted(identity_map.items())
            if isinstance(original, str) and isinstance(restored, str)
        ],
    }
    if include_frontend:
        result["frontend"] = _read_json(frontend_path) if frontend_path.is_file() else None
    return result


def _update_server_fence(transaction: dict[str, Any], phase: str) -> None:
    gate_root = RESTORE_DIR.parent
    fence = mutation_gate.read_fence(gate_root)
    if fence is None or fence.get("restoreId") != transaction.get("restoreId"):
        fence = {
            "schemaVersion": 1,
            "restoreId": transaction["restoreId"],
            "previousEpoch": transaction.get("previousEpoch", "unknown"),
            "targetEpoch": transaction.get("targetEpoch", "unknown"),
            "ownerDocumentId": transaction.get("ownerDocumentId", "server"),
            "createdAt": transaction.get("createdAt", int(time.time() * 1000)),
            "expiresAt": transaction.get("expiresAt", 2**63 - 1),
        }
    fence["phase"] = phase
    mutation_gate.write_fence(fence, gate_root)


def _cleanup_restore_payload(root: Path, *, remove_upload: bool) -> None:
    """Remove staged plaintext and, for terminal restores, the uploaded package."""

    backup_crypto.clear_secret(root.name)
    for name in ("extracted", "verified", "staged", "rollback"):
        shutil.rmtree(root / name, ignore_errors=True)
    for name in ("unlocked.dsibackup",):
        (root / name).unlink(missing_ok=True)
    if remove_upload:
        for path in root.iterdir():
            if path.is_file() and path.name.casefold().endswith((".dsibackup", ".dsibackup.age")):
                path.unlink(missing_ok=True)
        (root / "upload.json").unlink(missing_ok=True)


def _rollback_transaction(transaction: dict[str, Any], root: Path) -> None:
    transaction["phase"] = "aborting"
    transaction["updatedAt"] = _utc_iso()
    journal_path = root / "transaction.json"
    _write_json(journal_path, transaction)
    contributors = transaction.get("contributors", [])
    if not isinstance(contributors, list):
        raise AppError("Restore contributor journal is invalid", code=ErrorCode.INVALID_PAYLOAD)
    for contributor in reversed(contributors):
        if not isinstance(contributor, dict):
            raise AppError("Restore contributor journal is invalid", code=ErrorCode.INVALID_PAYLOAD)
        if bool(contributor.get("external")):
            # External restore is non-overwriting and idempotent. Imported tasks
            # remain inert, so rollback never deletes pre-existing Redis state.
            contributor["swapState"] = "external-retained"
            _write_json(journal_path, transaction)
            continue
        destination = Path(str(contributor.get("destination") or ""))
        rollback = Path(str(contributor.get("rollbackPath") or ""))
        state = str(contributor.get("swapState") or "")
        installed = bool(contributor.get("swapped")) or state in {"installing-staged", "swapped"}
        old_moved = state in {"old-moved", "installing-staged", "swapped"} or rollback.exists()
        if installed and destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        if bool(contributor.get("hadDestination")) and old_moved and rollback.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(rollback, destination)
        contributor["swapped"] = False
        contributor["swapState"] = "rolled-back"
        _write_json(journal_path, transaction)
    shutil.rmtree(root / "staged", ignore_errors=True)
    transaction["phase"] = "rolled-back"
    transaction["rolledBackAt"] = _utc_iso()
    transaction["updatedAt"] = _utc_iso()
    _write_json(journal_path, transaction)
    mutation_gate.clear_fence(str(transaction["restoreId"]), RESTORE_DIR.parent)
    mutation_gate.bump_generation(RESTORE_DIR.parent)


def _iso_age_seconds(value: Any, now: float) -> float | None:
    if isinstance(value, (int, float)):
        timestamp = float(value) / 1000 if value > 10_000_000_000 else float(value)
        return max(0.0, now - timestamp)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0.0, now - parsed.timestamp())


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
    temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(_stable_json(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


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


def _fsync_file(path: Path) -> None:
    """Flush a regular file using a writable handle where Windows requires it."""

    try:
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())
    except OSError:
        # Read-only imported files cannot always be reopened writable.  Their
        # copy handle has already been closed; keep directory/journal fsync as
        # the portable durability boundary instead of failing the restore.
        return


def _fsync_directory(path: Path) -> None:
    """Best-effort directory entry durability (supported on POSIX)."""

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    """Flush a prepared contributor before its digest is journaled."""

    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
        _fsync_file(path)
    for directory in sorted((item for item in root.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(directory)
    _fsync_directory(root)


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

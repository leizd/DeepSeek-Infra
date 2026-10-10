"""Artifact Hub store and preview helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.data import projects as legacy_projects
from deepseek_infra.infra.workspace.schema import (
    new_id,
    normalize_artifact_type,
    normalize_source_ref,
    normalize_title,
    now_ms,
    redact_sensitive_text,
    resolve_runtime_path,
    runtime_relative_path,
    safe_filename,
    timestamp_ms_to_iso,
    validate_project_id,
    validate_workspace_id,
    write_json_atomic,
    read_json_file,
)

MAX_ARTIFACTS = 500
MAX_ARTIFACT_PREVIEW_CHARS = 100_000
STORE_NAME = "artifacts.json"
TEXT_PREVIEW_TYPES = {"svg", "markdown", "md", "csv", "json", "html", "txt"}


def list_artifacts(project_id: str) -> list[dict[str, Any]]:
    safe_project_id = validate_project_id(project_id)
    return sorted(_load_artifacts(safe_project_id), key=lambda item: int(item.get("updatedAtMs") or item.get("createdAtMs") or 0), reverse=True)


def register_artifact(
    project_id: str,
    *,
    artifact_type: str,
    title: str,
    path: str,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    safe_project_id = validate_project_id(project_id)
    legacy_projects.require_project(safe_project_id)
    artifacts = _load_artifacts(safe_project_id)
    if len(artifacts) >= MAX_ARTIFACTS:
        raise AppError("Too many artifacts", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
    rel_path = runtime_relative_path(path)
    created_at = now_ms()
    artifact = {
        "artifactId": new_id("art"),
        "projectId": safe_project_id,
        "type": normalize_artifact_type(artifact_type, path=rel_path),
        "title": normalize_title(title, default="Artifact"),
        "path": rel_path,
        "source": normalize_source_ref(source or {}),
        "version": 1,
        "versions": [{"version": 1, "path": rel_path, "createdAt": timestamp_ms_to_iso(created_at), "createdAtMs": created_at}],
        "createdAt": timestamp_ms_to_iso(created_at),
        "updatedAt": timestamp_ms_to_iso(created_at),
        "createdAtMs": created_at,
        "updatedAtMs": created_at,
        "downloadUrl": "",
    }
    artifact["downloadUrl"] = artifact_download_url(artifact)
    artifacts.append(artifact)
    _write_artifacts(safe_project_id, artifacts)
    _touch_project(safe_project_id)
    return artifact


def rename_artifact(project_id: str, artifact_id: str, title: str) -> dict[str, Any]:
    return update_artifact(project_id, artifact_id, {"title": title})


def add_artifact_version(project_id: str, artifact_id: str, *, path: str, source: dict[str, Any] | None = None) -> dict[str, Any]:
    safe_project_id = validate_project_id(project_id)
    safe_artifact_id = validate_workspace_id(artifact_id, label="artifact id")
    artifacts = _load_artifacts(safe_project_id)
    for index, artifact in enumerate(artifacts):
        if artifact.get("artifactId") != safe_artifact_id:
            continue
        rel_path = runtime_relative_path(path)
        now = now_ms()
        version = int(artifact.get("version") or 0) + 1
        raw_versions = artifact.get("versions")
        versions = list(raw_versions) if isinstance(raw_versions, list) else []
        versions.append({"version": version, "path": rel_path, "createdAt": timestamp_ms_to_iso(now), "createdAtMs": now})
        artifact = {
            **artifact,
            "path": rel_path,
            "source": normalize_source_ref(source or artifact.get("source") or {}),
            "version": version,
            "versions": versions,
            "updatedAt": timestamp_ms_to_iso(now),
            "updatedAtMs": now,
        }
        artifact["downloadUrl"] = artifact_download_url(artifact)
        artifacts[index] = artifact
        _write_artifacts(safe_project_id, artifacts)
        _touch_project(safe_project_id)
        return artifact
    raise AppError("Artifact not found", code=ErrorCode.NOT_FOUND, status=404)


def update_artifact(project_id: str, artifact_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    safe_project_id = validate_project_id(project_id)
    safe_artifact_id = validate_workspace_id(artifact_id, label="artifact id")
    artifacts = _load_artifacts(safe_project_id)
    for index, artifact in enumerate(artifacts):
        if artifact.get("artifactId") != safe_artifact_id:
            continue
        updated = dict(artifact)
        if "title" in updates:
            updated["title"] = normalize_title(updates.get("title"), default=str(artifact.get("title") or "Artifact"))
        if "source" in updates:
            updated["source"] = normalize_source_ref(updates.get("source"))
        now = now_ms()
        updated["updatedAt"] = timestamp_ms_to_iso(now)
        updated["updatedAtMs"] = now
        updated["downloadUrl"] = artifact_download_url(updated)
        artifacts[index] = updated
        _write_artifacts(safe_project_id, artifacts)
        _touch_project(safe_project_id)
        return updated
    raise AppError("Artifact not found", code=ErrorCode.NOT_FOUND, status=404)


def delete_artifact(project_id: str, artifact_id: str) -> int:
    safe_project_id = validate_project_id(project_id)
    safe_artifact_id = validate_workspace_id(artifact_id, label="artifact id")
    artifacts = _load_artifacts(safe_project_id)
    kept = [artifact for artifact in artifacts if artifact.get("artifactId") != safe_artifact_id]
    if len(kept) == len(artifacts):
        return 0
    _write_artifacts(safe_project_id, kept)
    _touch_project(safe_project_id)
    return 1


def require_artifact(artifact_id: str, *, project_id: str = "") -> dict[str, Any]:
    safe_artifact_id = validate_workspace_id(artifact_id, label="artifact id")
    projects = [validate_project_id(project_id)] if project_id else [str(item.get("id") or "") for item in legacy_projects.list_projects()]
    for candidate_project_id in projects:
        if not candidate_project_id:
            continue
        for artifact in _load_artifacts(candidate_project_id):
            if artifact.get("artifactId") == safe_artifact_id:
                return artifact
    raise AppError("Artifact not found", code=ErrorCode.NOT_FOUND, status=404)


def preview_artifact(artifact_id: str, *, project_id: str = "") -> dict[str, Any]:
    artifact = require_artifact(artifact_id, project_id=project_id)
    path = artifact_path(artifact)
    if not path.is_file():
        raise AppError("Artifact file not found", code=ErrorCode.NOT_FOUND, status=404)
    artifact_type = str(artifact.get("type") or "").lower()
    if artifact_type not in TEXT_PREVIEW_TYPES and path.suffix.lower().lstrip(".") not in TEXT_PREVIEW_TYPES:
        return {"artifact": artifact, "previewAvailable": False, "content": "", "bytes": path.stat().st_size}
    text = path.read_text(encoding="utf-8", errors="replace")[:MAX_ARTIFACT_PREVIEW_CHARS]
    return {"artifact": artifact, "previewAvailable": True, "content": redact_sensitive_text(text), "bytes": path.stat().st_size}


def artifact_path(artifact: dict[str, Any]) -> Path:
    return resolve_runtime_path(str(artifact.get("path") or ""))


def artifact_filename(artifact: dict[str, Any]) -> str:
    path = Path(str(artifact.get("path") or ""))
    ext = path.suffix.lower() or f".{str(artifact.get('type') or 'artifact')}"
    return safe_filename(str(artifact.get("title") or path.stem or "artifact")) + ext


def artifact_download_url(artifact: dict[str, Any]) -> str:
    project_id = str(artifact.get("projectId") or "")
    artifact_id = str(artifact.get("artifactId") or "")
    return f"/api/workspace/artifacts/{artifact_id}/download?projectId={project_id}"


def _store_path(project_id: str) -> Path:
    safe_project_id = validate_project_id(project_id)
    return legacy_projects.PROJECTS_DIR / safe_project_id / STORE_NAME


def _load_artifacts(project_id: str) -> list[dict[str, Any]]:
    data = read_json_file(_store_path(project_id), default={"artifacts": []})
    raw_artifacts = data.get("artifacts")
    if not isinstance(raw_artifacts, list):
        return []
    artifacts: list[dict[str, Any]] = []
    for raw in raw_artifacts:
        if not isinstance(raw, dict):
            continue
        try:
            created_at = int(raw.get("createdAtMs") or 0)
            updated_at = int(raw.get("updatedAtMs") or created_at)
            version = int(raw.get("version") or 1)
        except (TypeError, ValueError):
            created_at = updated_at = 0
            version = 1
        artifact_id = str(raw.get("artifactId") or raw.get("id") or "")
        if not artifact_id:
            continue
        rel_path = runtime_relative_path(str(raw.get("path") or ""))
        artifact = {
            "artifactId": artifact_id,
            "projectId": validate_project_id(str(raw.get("projectId") or project_id)),
            "type": normalize_artifact_type(raw.get("type"), path=rel_path),
            "title": normalize_title(raw.get("title"), default="Artifact"),
            "path": rel_path,
            "source": normalize_source_ref(raw.get("source")),
            "version": version,
            "versions": _normalize_versions(raw.get("versions"), rel_path=rel_path, version=version, created_at=created_at),
            "createdAt": str(raw.get("createdAt") or timestamp_ms_to_iso(created_at)),
            "updatedAt": str(raw.get("updatedAt") or timestamp_ms_to_iso(updated_at)),
            "createdAtMs": created_at,
            "updatedAtMs": updated_at,
        }
        artifact["downloadUrl"] = artifact_download_url(artifact)
        artifacts.append(artifact)
    return artifacts[-MAX_ARTIFACTS:]


def _normalize_versions(value: Any, *, rel_path: str, version: int, created_at: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        return [{"version": version, "path": rel_path, "createdAt": timestamp_ms_to_iso(created_at), "createdAtMs": created_at}]
    versions: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        try:
            raw_version = int(raw.get("version") or 1)
            raw_created_at = int(raw.get("createdAtMs") or created_at)
        except (TypeError, ValueError):
            raw_version = 1
            raw_created_at = created_at
        versions.append(
            {
                "version": raw_version,
                "path": runtime_relative_path(str(raw.get("path") or rel_path)),
                "createdAt": str(raw.get("createdAt") or timestamp_ms_to_iso(raw_created_at)),
                "createdAtMs": raw_created_at,
            }
        )
    return versions


def _write_artifacts(project_id: str, artifacts: list[dict[str, Any]]) -> None:
    write_json_atomic(_store_path(project_id), {"artifacts": artifacts[-MAX_ARTIFACTS:]})


def _touch_project(project_id: str) -> None:
    from deepseek_infra.infra.workspace.projects import touch_project

    touch_project(project_id)

"""Persistent local project spaces and document libraries."""

from __future__ import annotations

import json
import re
import secrets
import shutil
import time
from typing import Any

from deepseek_infra.core.config import PROJECTS_DIR
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import utc_now_iso
from deepseek_infra.infra.rag.files import extract_uploaded_file

MAX_PROJECTS = 40
MAX_PROJECT_DOCUMENTS = 120
MAX_PROJECT_SKILL_RUNS = 200
MAX_PROJECT_SAVED_ITEMS = 200
MAX_PROJECT_ARTIFACTS = 200


def list_projects() -> list[dict[str, Any]]:
    if not PROJECTS_DIR.exists():
        return []
    projects = []
    for path in PROJECTS_DIR.iterdir():
        if not path.is_dir():
            continue
        project = read_project(path.name)
        if project:
            projects.append(public_project(project))
    return sorted(projects, key=lambda item: int(item.get("updatedAt") or 0), reverse=True)


def create_project(name: str) -> dict[str, Any]:
    if len(list_projects()) >= MAX_PROJECTS:
        raise AppError("Too many projects", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
    now = int(time.time() * 1000)
    project = {
        "id": f"proj-{secrets.token_hex(6)}",
        "name": normalize_project_name(name),
        "documents": [],
        "skills": {"enabledPacks": [], "enabledPackVersions": [], "enabledSkills": [], "defaultSkill": "", "recentSkills": []},
        "skillRuns": [],
        "savedItems": [],
        "artifacts": [],
        "createdAt": now,
        "updatedAt": now,
    }
    write_project(project)
    return public_project(project)


def delete_project(project_id: str) -> int:
    safe_id = validate_project_id(project_id)
    path = PROJECTS_DIR / safe_id
    if not path.exists():
        return 0
    try:
        from deepseek_infra.infra.rag import local_rag

        local_rag.delete_items(collection=local_rag.COLLECTION_FILES, project_id=safe_id)
    except Exception:
        pass
    shutil.rmtree(path)
    return 1


def project_skill_binding(project_id: str) -> dict[str, Any]:
    project = require_project(project_id)
    return normalize_project_skills(project.get("skills"))


def set_project_skill_binding(
    project_id: str,
    enabled_skills: list[str],
    *,
    default_skill: str = "",
    enabled_packs: list[str] | None = None,
    enabled_pack_versions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    project = require_project(project_id)
    enabled = [skill for skill in (normalize_skill_id_for_project(item) for item in enabled_skills) if skill]
    default = normalize_skill_id_for_project(default_skill)
    if default and default not in enabled:
        enabled.insert(0, default)
    existing = normalize_project_skills(project.get("skills"))
    packs = unique_strings(normalize_pack_id_for_project(item) for item in (enabled_packs if enabled_packs is not None else existing.get("enabledPacks", [])))
    pack_versions = (
        normalize_project_pack_versions(enabled_pack_versions)
        if enabled_pack_versions is not None
        else [item for item in existing.get("enabledPackVersions", []) if item.get("packId") in packs]
    )
    project["skills"] = {
        "enabledPacks": packs[:40],
        "enabledPackVersions": pack_versions[:40],
        "enabledSkills": unique_strings(enabled)[:40],
        "defaultSkill": default if default in enabled else "",
        "recentSkills": existing.get("recentSkills", []),
    }
    project["updatedAt"] = int(time.time() * 1000)
    write_project(project)
    return normalize_project_skills(project["skills"])


def enable_pack_for_project(project_id: str, pack_id: str, *, version: str = "") -> dict[str, Any]:
    """Enable all Skills referenced by a Skill Pack on a project.

    Resolves the pack through the Skill registry and adds its skillIds to the
    project's enabledSkills, recording the pack in enabledPacks.
    """
    from deepseek_infra.infra.skills import registry as skill_registry

    pack = skill_registry.get_pack(pack_id)
    pack_skill_ids = [str(entry.get("skillId") or "") for entry in (pack.get("skills") or []) if isinstance(entry, dict)]
    resolved: list[str] = []
    for skill_id in pack_skill_ids:
        if skill_registry._safe_get_skill(skill_id) is not None:  # noqa: SLF001
            resolved.append(skill_id)
    binding = project_skill_binding(project_id)
    enabled = list(binding.get("enabledSkills") or [])
    for skill_id in resolved:
        if skill_id not in enabled:
            enabled.append(skill_id)
    default = str(binding.get("defaultSkill") or "")
    if not default and resolved:
        default = resolved[0]
    packs = list(binding.get("enabledPacks") or [])
    if pack.get("packId") not in packs:
        packs.append(str(pack.get("packId") or ""))
    pack_versions = list(binding.get("enabledPackVersions") or [])
    pack_versions = [item for item in pack_versions if item.get("packId") != pack.get("packId")]
    pack_versions.append(
        {
            "packId": str(pack.get("packId") or ""),
            "version": str(version or pack.get("version") or ""),
            "installedAt": utc_now_iso(),
        }
    )
    return set_project_skill_binding(project_id, enabled, default_skill=default, enabled_packs=packs, enabled_pack_versions=pack_versions)


def append_project_skill_run(project_id: str, run: dict[str, Any]) -> dict[str, Any]:
    project = require_project(project_id)
    run_record = normalize_skill_run(run)
    runs = [item for item in normalize_skill_runs(project.get("skillRuns")) if item.get("skillRunId") != run_record.get("skillRunId")]
    runs.insert(0, run_record)
    project["skillRuns"] = runs[:MAX_PROJECT_SKILL_RUNS]
    touch_project_skill(project, str(run_record.get("skillId") or ""))
    project["updatedAt"] = int(time.time() * 1000)
    write_project(project)
    return run_record


def list_project_skill_runs(project_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    project = require_project(project_id)
    return normalize_skill_runs(project.get("skillRuns"))[: max(1, min(int(limit or 50), MAX_PROJECT_SKILL_RUNS))]


def add_project_saved_item(
    project_id: str,
    *,
    title: str,
    content: str,
    kind: str = "skill_output",
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    project = require_project(project_id)
    now = int(time.time() * 1000)
    item = {
        "id": f"saved-{secrets.token_hex(8)}",
        "title": str(title or "Skill output")[:160],
        "kind": str(kind or "skill_output")[:80],
        "content": str(content or "")[:80_000],
        "source": source or {},
        "createdAt": now,
    }
    saved_items = normalize_saved_items(project.get("savedItems"))
    saved_items.insert(0, item)
    project["savedItems"] = saved_items[:MAX_PROJECT_SAVED_ITEMS]
    project["updatedAt"] = now
    write_project(project)
    return item


def link_project_artifact(project_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
    project = require_project(project_id)
    normalized = normalize_project_artifact(artifact)
    artifacts = [item for item in normalize_project_artifacts(project.get("artifacts")) if item.get("artifactId") != normalized.get("artifactId")]
    artifacts.insert(0, normalized)
    project["artifacts"] = artifacts[:MAX_PROJECT_ARTIFACTS]
    project["updatedAt"] = int(time.time() * 1000)
    write_project(project)
    return normalized


def export_project(project_id: str) -> dict[str, Any]:
    project = require_project(project_id)
    return {
        "project": public_project(project),
        "documents": normalize_documents(project.get("documents")),
        "skills": normalize_project_skills(project.get("skills")),
        "skillRuns": normalize_skill_runs(project.get("skillRuns")),
        "savedItems": normalize_saved_items(project.get("savedItems")),
        "artifacts": normalize_project_artifacts(project.get("artifacts")),
    }


def add_project_files(
    project_id: str,
    files: list[dict[str, Any]],
    *,
    ocr_enabled: bool | None = None,
    ocr_api_key: str | None = None,
) -> list[dict[str, Any]]:
    project = require_project(project_id)
    documents = list(project.get("documents") or [])
    if len(documents) + len(files) > MAX_PROJECT_DOCUMENTS:
        raise AppError("Too many project documents", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)

    added = []
    for file_info in files:
        raw_data = file_info.get("data")
        extracted = extract_uploaded_file(
            str(file_info.get("filename") or "file"),
            str(file_info.get("content_type") or "application/octet-stream"),
            raw_data if isinstance(raw_data, bytes) else b"",
            ocr_enabled=ocr_enabled,
            ocr_api_key=ocr_api_key,
            project_id=str(project["id"]),
        )
        document = project_document_from_extracted(extracted)
        documents = [item for item in documents if item.get("fileId") != document["fileId"]]
        documents.append(document)
        added.append(document)

    project["documents"] = documents[-MAX_PROJECT_DOCUMENTS:]
    project["updatedAt"] = int(time.time() * 1000)
    write_project(project)
    return added


def project_document_from_extracted(extracted: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": secrets.token_hex(8),
        "name": str(extracted.get("name") or "文件"),
        "type": str(extracted.get("type") or ""),
        "size": int(extracted.get("size") or 0),
        "kind": str(extracted.get("kind") or "text"),
        "fileId": str(extracted.get("fileId") or ""),
        "projectId": str(extracted.get("projectId") or ""),
        "sourceAvailable": bool(extracted.get("sourceAvailable")),
        "preview": str(extracted.get("preview") or "")[:1800],
        "pageCount": int(extracted.get("pageCount") or 0),
        "charCount": int(extracted.get("charCount") or 0),
        "chunkCount": int(extracted.get("chunkCount") or 0),
        "chunked": bool(extracted.get("chunked")),
        "createdAt": int(time.time() * 1000),
    }


def require_project(project_id: str) -> dict[str, Any]:
    project = read_project(project_id)
    if project is None:
        raise AppError("Project not found", code=ErrorCode.NOT_FOUND, status=404)
    return project


def read_project(project_id: str) -> dict[str, Any] | None:
    safe_id = validate_project_id(project_id)
    path = PROJECTS_DIR / safe_id / "project.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data["id"] = safe_id
    data["name"] = normalize_project_name(data.get("name"))
    data["documents"] = normalize_documents(data.get("documents"))
    data["skills"] = normalize_project_skills(data.get("skills"))
    data["skillRuns"] = normalize_skill_runs(data.get("skillRuns"))
    data["savedItems"] = normalize_saved_items(data.get("savedItems"))
    data["artifacts"] = normalize_project_artifacts(data.get("artifacts"))
    return data


def write_project(project: dict[str, Any]) -> None:
    safe_id = validate_project_id(str(project.get("id") or ""))
    directory = PROJECTS_DIR / safe_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "project.json"
    tmp = directory / "project.tmp"
    tmp.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def public_project(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(project.get("id") or ""),
        "name": normalize_project_name(project.get("name")),
        "documents": normalize_documents(project.get("documents")),
        "skills": normalize_project_skills(project.get("skills")),
        "skillRuns": normalize_skill_runs(project.get("skillRuns"))[:20],
        "savedItems": normalize_saved_items(project.get("savedItems"))[:20],
        "artifacts": normalize_project_artifacts(project.get("artifacts"))[:20],
        "createdAt": int(project.get("createdAt") or 0),
        "updatedAt": int(project.get("updatedAt") or 0),
    }


def normalize_project_name(value: Any) -> str:
    name = str(value or "").replace("\n", " ").strip()
    return (name[:60] or "新项目")


def normalize_documents(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    documents = []
    for item in value:
        if not isinstance(item, dict):
            continue
        file_id = str(item.get("fileId") or "")
        project_id = str(item.get("projectId") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", file_id) or not project_id:
            continue
        documents.append(
            {
                "id": str(item.get("id") or file_id),
                "name": str(item.get("name") or "文件")[:180],
                "type": str(item.get("type") or ""),
                "size": int(item.get("size") or 0),
                "kind": str(item.get("kind") or "text"),
                "fileId": file_id,
                "projectId": project_id,
                "sourceAvailable": bool(item.get("sourceAvailable")),
                "preview": str(item.get("preview") or "")[:1800],
                "pageCount": int(item.get("pageCount") or 0),
                "charCount": int(item.get("charCount") or 0),
                "chunkCount": int(item.get("chunkCount") or 0),
                "chunked": bool(item.get("chunked")),
                "createdAt": int(item.get("createdAt") or 0),
            }
        )
    return documents


def normalize_project_skills(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    raw_enabled_packs = data.get("enabledPacks") or []
    raw_pack_versions = data.get("enabledPackVersions")
    pack_versions = normalize_project_pack_versions(raw_pack_versions if raw_pack_versions is not None else raw_enabled_packs)
    explicit_packs = unique_strings(normalize_pack_id_for_project(item) for item in raw_enabled_packs)
    packs = unique_strings([*explicit_packs, *[item.get("packId", "") for item in pack_versions]])
    enabled = unique_strings(normalize_skill_id_for_project(item) for item in data.get("enabledSkills") or [])
    recent = unique_strings(normalize_skill_id_for_project(item) for item in data.get("recentSkills") or [])
    default = normalize_skill_id_for_project(data.get("defaultSkill"))
    return {
        "enabledPacks": packs[:40],
        "enabledPackVersions": pack_versions[:40],
        "enabledSkills": enabled[:40],
        "defaultSkill": default if default in enabled else "",
        "recentSkills": recent[:20],
    }


def normalize_skill_runs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    runs = []
    for item in value:
        if not isinstance(item, dict):
            continue
        runs.append(normalize_skill_run(item))
    return runs[:MAX_PROJECT_SKILL_RUNS]


def normalize_skill_run(item: dict[str, Any]) -> dict[str, Any]:
    artifact_ids = unique_strings(str(value or "") for value in item.get("artifactIds") or [])[:40]
    saved_item_ids = unique_strings(str(value or "") for value in item.get("savedItemIds") or [])[:40]
    return {
        "skillRunId": str(item.get("skillRunId") or item.get("runId") or f"run-{secrets.token_hex(8)}")[:80],
        "skillId": normalize_skill_id_for_project(item.get("skillId")),
        "skillVersion": str(item.get("skillVersion") or "")[:40],
        "packId": normalize_pack_id_for_project(item.get("packId")),
        "status": str(item.get("status") or "completed")[:40],
        "projectId": str(item.get("projectId") or "")[:80],
        "input": item.get("input") if isinstance(item.get("input"), dict) else {},
        "inputSummary": str(item.get("inputSummary") or "")[:600],
        "outputSummary": str(item.get("outputSummary") or "")[:1200],
        "artifactIds": artifact_ids,
        "savedItemIds": saved_item_ids,
        "artifactCount": _safe_int(item.get("artifactCount"), default=len(artifact_ids)),
        "savedItemCount": _safe_int(item.get("savedItemCount"), default=len(saved_item_ids)),
        "traceId": str(item.get("traceId") or "")[:80],
        "startedAt": str(item.get("startedAt") or ""),
        "completedAt": str(item.get("completedAt") or ""),
        "latencyMs": max(0, int(item.get("latencyMs") or 0)),
        "offline": bool(item.get("offline")),
        "model": str(item.get("model") or "")[:120],
        "errorReason": str(item.get("errorReason") or "")[:1200],
        "failureCategory": str(item.get("failureCategory") or "")[:80],
        "diagnosticSuggestion": str(item.get("diagnosticSuggestion") or "")[:240],
    }


def _safe_int(value: Any, *, default: int = 0) -> int:
    if value is None:
        return max(0, default)
    try:
        return max(0, int(str(value)))
    except (TypeError, ValueError):
        return max(0, default)


def normalize_saved_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "id": str(item.get("id") or f"saved-{secrets.token_hex(8)}")[:80],
                "title": str(item.get("title") or "Saved item")[:160],
                "kind": str(item.get("kind") or "note")[:80],
                "content": str(item.get("content") or "")[:80_000],
                "source": item.get("source") if isinstance(item.get("source"), dict) else {},
                "createdAt": int(item.get("createdAt") or 0),
            }
        )
    return items[:MAX_PROJECT_SAVED_ITEMS]


def normalize_project_artifacts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    artifacts = []
    for item in value:
        if isinstance(item, dict):
            artifacts.append(normalize_project_artifact(item))
    return artifacts[:MAX_PROJECT_ARTIFACTS]


def normalize_project_artifact(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifactId": str(item.get("artifactId") or "")[:80],
        "fileId": str(item.get("fileId") or "")[:80],
        "filename": str(item.get("filename") or "")[:180],
        "downloadUrl": str(item.get("downloadUrl") or "")[:500],
        "type": str(item.get("type") or "")[:40],
        "source": item.get("source") if isinstance(item.get("source"), dict) else {},
        "createdAt": str(item.get("createdAt") or ""),
    }


def touch_project_skill(project: dict[str, Any], skill_id: str) -> None:
    skill_id = normalize_skill_id_for_project(skill_id)
    if not skill_id:
        return
    skills = normalize_project_skills(project.get("skills"))
    if skill_id not in skills["enabledSkills"]:
        skills["enabledSkills"].insert(0, skill_id)
    skills["recentSkills"] = unique_strings([skill_id, *skills["recentSkills"]])[:20]
    if not skills["defaultSkill"]:
        skills["defaultSkill"] = skill_id
    project["skills"] = skills


def normalize_skill_id_for_project(value: Any) -> str:
    skill_id = str(value or "").strip()
    return skill_id if re.fullmatch(r"[A-Za-z0-9_:-]{3,80}", skill_id) else ""


def normalize_pack_id_for_project(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("packId")
    pack_id = str(value or "").strip()
    return pack_id if re.fullmatch(r"[A-Za-z0-9_:-]{3,80}", pack_id) else ""


def normalize_project_pack_versions(value: Any) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    result: list[dict[str, Any]] = []
    for item in rows:
        if isinstance(item, dict):
            pack_id = normalize_pack_id_for_project(item.get("packId"))
            version = str(item.get("version") or "").strip()[:40]
            installed_at = str(item.get("installedAt") or item.get("updatedAt") or "").strip()[:80]
        else:
            pack_id = normalize_pack_id_for_project(item)
            version = ""
            installed_at = ""
        if not pack_id or any(existing.get("packId") == pack_id for existing in result):
            continue
        result.append({"packId": pack_id, "version": version, "installedAt": installed_at})
    return result


def unique_strings(values: Any) -> list[str]:
    result = []
    for value in values if isinstance(values, list) else list(values):
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def validate_project_id(project_id: str) -> str:
    safe_id = str(project_id or "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{4,64}", safe_id):
        raise AppError("Invalid project id", code=ErrorCode.INVALID_PAYLOAD, status=400)
    return safe_id

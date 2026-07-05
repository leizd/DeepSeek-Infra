"""Workspace Core routes: projects, saved items, artifacts, and exports."""

from __future__ import annotations

import mimetypes
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.data.projects import (
    add_project_files,
    create_project,
    delete_project,
    enable_pack_for_project,
    list_projects,
    list_project_skill_runs,
    project_skill_binding,
    set_project_skill_binding,
)
from deepseek_infra.infra.skills import analytics as skill_analytics
from deepseek_infra.infra.workspace import artifacts as workspace_artifacts
from deepseek_infra.infra.workspace import exports as workspace_exports
from deepseek_infra.infra.workspace import home as workspace_home
from deepseek_infra.infra.workspace import projects as workspace_projects
from deepseek_infra.infra.workspace import provenance as workspace_provenance
from deepseek_infra.infra.workspace import saved_items as workspace_saved_items
from deepseek_infra.web.http_utils import content_disposition_header, json_response, read_json_body, require_api_auth


@dataclass(frozen=True)
class WorkspaceRouteDeps:
    read_multipart_files: Callable[..., Any]


def create_workspace_router(deps: WorkspaceRouteDeps) -> APIRouter:
    router = APIRouter()

    # ── Projects (legacy action API) ──────────────────────────────────────

    @router.post("/api/projects")
    async def api_projects(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        action = str(payload.get("action") or "list").strip().lower()
        if action == "list":
            return json_response({"projects": list_projects()})
        if action == "create":
            return json_response({"ok": True, "project": create_project(str(payload.get("name") or ""))})
        if action == "get":
            pid = str(payload.get("id") or payload.get("projectId") or "")
            return json_response({"ok": True, "project": workspace_projects.get_project(pid)})
        if action == "rename":
            pid = str(payload.get("id") or payload.get("projectId") or "")
            project = workspace_projects.rename_project(
                pid,
                str(payload.get("name") or ""),
                description=str(payload.get("description")) if "description" in payload else None,
            )
            return json_response({"ok": True, "project": project})
        if action == "delete":
            pid = str(payload.get("id") or payload.get("projectId") or "")
            return json_response({"ok": True, "deleted": delete_project(pid)})
        raise AppError("Unsupported project action", code=ErrorCode.INVALID_PAYLOAD)

    # ── Project files ──────────────────────────────────────────────────────

    @router.post("/api/project-files")
    async def api_project_files(request: Request) -> JSONResponse:
        require_api_auth(request)
        project_id = request.query_params.get("projectId", "")
        files, ocr_enabled, ocr_api_key = await deps.read_multipart_files(request)
        if not files:
            raise AppError("No file uploaded", code=ErrorCode.INVALID_PAYLOAD)
        documents = add_project_files(project_id, files, ocr_enabled=ocr_enabled, ocr_api_key=ocr_api_key)
        return json_response({"ok": True, "documents": documents})

    # ── Workspace projects ─────────────────────────────────────────────────

    @router.get("/api/workspace/home")
    async def api_workspace_home(request: Request) -> JSONResponse:
        require_api_auth(request)
        limit = int(request.query_params.get("limit") or 8)
        return json_response(workspace_home.workspace_home(limit=limit))

    @router.get("/api/workspace/projects")
    async def api_workspace_projects_list(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "projects": workspace_projects.list_projects()})

    @router.post("/api/workspace/projects")
    async def api_workspace_projects_create(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        project = workspace_projects.create_project(str(payload.get("name") or ""), description=str(payload.get("description") or ""))
        return json_response({"ok": True, "project": project})

    @router.get("/api/workspace/projects/{project_id}")
    async def api_workspace_project_get(request: Request, project_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "project": workspace_projects.get_project(project_id)})

    @router.get("/api/workspace/projects/{project_id}/skills")
    async def api_workspace_project_skills_get(request: Request, project_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "skills": project_skill_binding(project_id)})

    @router.patch("/api/workspace/projects/{project_id}/skills")
    async def api_workspace_project_skills_update(request: Request, project_id: str) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        raw_enabled = payload.get("enabledSkills")
        enabled = [str(item) for item in raw_enabled] if isinstance(raw_enabled, list) else []
        default = str(payload.get("defaultSkill") or "")
        raw_packs = payload.get("enabledPacks")
        enabled_packs = [str(item.get("packId") if isinstance(item, dict) else item) for item in raw_packs] if isinstance(raw_packs, list) else None
        raw_pack_versions = payload.get("enabledPackVersions")
        enabled_pack_versions = [item for item in raw_pack_versions if isinstance(item, dict)] if isinstance(raw_pack_versions, list) else None
        return json_response(
            {
                "ok": True,
                "skills": set_project_skill_binding(
                    project_id,
                    enabled,
                    default_skill=default,
                    enabled_packs=enabled_packs,
                    enabled_pack_versions=enabled_pack_versions,
                ),
            }
        )

    @router.post("/api/workspace/projects/{project_id}/skill-packs/{pack_id}/install")
    async def api_workspace_project_pack_install(request: Request, project_id: str, pack_id: str) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request) if int(request.headers.get("Content-Length") or "0") > 0 else {}
        return json_response({"ok": True, "skills": enable_pack_for_project(project_id, pack_id, version=str(payload.get("version") or ""))})

    @router.get("/api/workspace/projects/{project_id}/skill-runs")
    async def api_workspace_project_skill_runs_list(request: Request, project_id: str) -> JSONResponse:
        require_api_auth(request)
        limit = int(request.query_params.get("limit") or 50)
        return json_response({"ok": True, "skillRuns": list_project_skill_runs(project_id, limit=limit)})

    @router.get("/api/workspace/projects/{project_id}/skill-analytics")
    async def api_workspace_project_skill_analytics(request: Request, project_id: str) -> JSONResponse:
        require_api_auth(request)
        days = int(request.query_params.get("days") or 7)
        return json_response({"ok": True, "summary": skill_analytics.analytics_summary(scope="project", project_id=project_id, days=days)})

    @router.get("/api/workspace/projects/{project_id}/provenance")
    async def api_workspace_project_provenance(request: Request, project_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response(workspace_provenance.project_provenance(project_id))

    @router.patch("/api/workspace/projects/{project_id}")
    async def api_workspace_project_update(request: Request, project_id: str) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        project = workspace_projects.rename_project(
            project_id,
            str(payload.get("name") or ""),
            description=str(payload.get("description")) if "description" in payload else None,
        )
        return json_response({"ok": True, "project": project})

    @router.delete("/api/workspace/projects/{project_id}")
    async def api_workspace_project_delete(request: Request, project_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "deleted": workspace_projects.delete_project(project_id)})

    @router.get("/api/workspace/projects/{project_id}/conversations")
    async def api_workspace_conversations_list(request: Request, project_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "conversations": workspace_projects.list_project_conversations(project_id)})

    @router.post("/api/workspace/projects/{project_id}/conversations")
    async def api_workspace_conversation_upsert(request: Request, project_id: str) -> JSONResponse:
        require_api_auth(request)
        conversation = workspace_projects.upsert_project_conversation(project_id, await read_json_body(request))
        return json_response({"ok": True, "conversation": conversation})

    # ── Saved items ────────────────────────────────────────────────────────

    @router.get("/api/workspace/projects/{project_id}/saved-items")
    async def api_workspace_saved_items_list(request: Request, project_id: str) -> JSONResponse:
        require_api_auth(request)
        item_type = str(request.query_params.get("type") or "")
        tags = [tag for tag in str(request.query_params.get("tags") or "").split(",") if tag]
        return json_response({"ok": True, "savedItems": workspace_saved_items.list_saved_items(project_id, item_type=item_type, tags=tags)})

    @router.post("/api/workspace/projects/{project_id}/saved-items")
    async def api_workspace_saved_item_create(request: Request, project_id: str) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        item = workspace_saved_items.create_saved_item(
            project_id,
            item_type=str(payload.get("type") or ""),
            title=str(payload.get("title") or ""),
            content=str(payload.get("content") or ""),
            source_ref=payload.get("sourceRef") if isinstance(payload.get("sourceRef"), dict) else {},
            tags=payload.get("tags") if isinstance(payload.get("tags"), list) else [],
            purpose=str(payload.get("purpose") or "reference"),
        )
        return json_response({"ok": True, "savedItem": item})

    @router.patch("/api/workspace/projects/{project_id}/saved-items/{saved_id}")
    async def api_workspace_saved_item_update(request: Request, project_id: str, saved_id: str) -> JSONResponse:
        require_api_auth(request)
        item = workspace_saved_items.update_saved_item(project_id, saved_id, await read_json_body(request))
        return json_response({"ok": True, "savedItem": item})

    @router.delete("/api/workspace/projects/{project_id}/saved-items/{saved_id}")
    async def api_workspace_saved_item_delete(request: Request, project_id: str, saved_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "deleted": workspace_saved_items.delete_saved_item(project_id, saved_id)})

    # ── Artifacts ──────────────────────────────────────────────────────────

    @router.get("/api/workspace/projects/{project_id}/artifacts")
    async def api_workspace_artifacts_list(request: Request, project_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "artifacts": workspace_artifacts.list_artifacts(project_id)})

    @router.post("/api/workspace/projects/{project_id}/artifacts")
    async def api_workspace_artifact_register(request: Request, project_id: str) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        artifact = workspace_artifacts.register_artifact(
            project_id,
            artifact_type=str(payload.get("type") or ""),
            title=str(payload.get("title") or ""),
            path=str(payload.get("path") or ""),
            source=payload.get("source") if isinstance(payload.get("source"), dict) else {},
        )
        return json_response({"ok": True, "artifact": artifact})

    @router.patch("/api/workspace/projects/{project_id}/artifacts/{artifact_id}")
    async def api_workspace_artifact_update(request: Request, project_id: str, artifact_id: str) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        if payload.get("path"):
            artifact = workspace_artifacts.add_artifact_version(
                project_id,
                artifact_id,
                path=str(payload.get("path") or ""),
                source=payload.get("source") if isinstance(payload.get("source"), dict) else None,
            )
        else:
            artifact = workspace_artifacts.update_artifact(project_id, artifact_id, payload)
        return json_response({"ok": True, "artifact": artifact})

    @router.delete("/api/workspace/projects/{project_id}/artifacts/{artifact_id}")
    async def api_workspace_artifact_delete(request: Request, project_id: str, artifact_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "deleted": workspace_artifacts.delete_artifact(project_id, artifact_id)})

    @router.get("/api/workspace/artifacts/{artifact_id}/preview")
    async def api_workspace_artifact_preview(request: Request, artifact_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response(
            {"ok": True, **workspace_artifacts.preview_artifact(artifact_id, project_id=str(request.query_params.get("projectId") or ""))}
        )

    @router.get("/api/workspace/artifacts/{artifact_id}/download")
    async def api_workspace_artifact_download(request: Request, artifact_id: str) -> Response:
        require_api_auth(request)
        artifact = workspace_artifacts.require_artifact(artifact_id, project_id=str(request.query_params.get("projectId") or ""))
        path = workspace_artifacts.artifact_path(artifact)
        if not path.is_file():
            raise AppError("Artifact file not found", code=ErrorCode.NOT_FOUND, status=404)
        data = path.read_bytes()
        media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        headers = {
            "Content-Disposition": content_disposition_header("attachment", workspace_artifacts.artifact_filename(artifact)),
            "Cache-Control": "no-store",
        }
        return Response(content=data, media_type=media_type, headers=headers)

    # ── Exports ────────────────────────────────────────────────────────────

    @router.post("/api/workspace/exports")
    async def api_workspace_export_create(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response(workspace_exports.create_export(await read_json_body(request, max_bytes=16_000_000)))

    @router.get("/api/workspace/exports/{export_id}/download")
    async def api_workspace_export_download(request: Request, export_id: str) -> Response:
        require_api_auth(request)
        export = workspace_exports.resolve_export(export_id, project_id=str(request.query_params.get("projectId") or ""))
        path = workspace_exports.export_path(export)
        if not path.is_file():
            raise AppError("Export file not found", code=ErrorCode.NOT_FOUND, status=404)
        data = path.read_bytes()
        media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        headers = {
            "Content-Disposition": content_disposition_header("attachment", str(export.get("filename") or path.name)),
            "Cache-Control": "no-store",
        }
        return Response(content=data, media_type=media_type, headers=headers)

    return router

"""Automation Runtime routes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.automation import history, registry, runner, scheduler
from deepseek_infra.web.http_utils import json_response, read_json_body, require_api_auth, truthy


def create_automation_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/automation")
    async def api_automation_list(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response(
            {
                "ok": True,
                "automations": registry.list_automations(
                    project_id=str(request.query_params.get("projectId") or ""),
                    include_disabled=truthy(request.query_params.get("includeDisabled", "1")),
                ),
            }
        )

    @router.post("/api/automation")
    async def api_automation_action(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        action = str(payload.get("action") or "create").strip().lower()
        if action == "list":
            return json_response(
                {
                    "ok": True,
                    "automations": registry.list_automations(
                        project_id=str(payload.get("projectId") or ""),
                        include_disabled=_bool(payload, "includeDisabled", default=True),
                    ),
                }
            )
        if action == "create":
            return json_response({"ok": True, "automation": registry.create_automation(_automation_payload(payload))})
        if action == "get":
            return json_response({"ok": True, "automation": registry.get_automation(_automation_id(payload))})
        if action == "update":
            return json_response({"ok": True, "automation": registry.update_automation(_automation_id(payload), _patch_payload(payload))})
        if action == "enable":
            return json_response({"ok": True, "automation": registry.set_automation_enabled(_automation_id(payload), True)})
        if action == "disable":
            return json_response({"ok": True, "automation": registry.set_automation_enabled(_automation_id(payload), False)})
        if action == "delete":
            return json_response({"ok": True, "deleted": registry.delete_automation(_automation_id(payload))})
        if action == "run":
            return json_response(
                {
                    "ok": True,
                    "run": runner.run_once(
                        _automation_id(payload),
                        trigger=_trigger_payload(payload),
                        event=payload.get("event") if isinstance(payload.get("event"), dict) else None,
                        now=_now_payload(payload),
                        confirmed=_bool(payload, "confirmed"),
                        force=_bool(payload, "force"),
                    ),
                }
            )
        if action == "rerun":
            return json_response({"ok": True, "run": runner.rerun(_run_id(payload), confirmed=_bool(payload, "confirmed"))})
        if action == "list_runs":
            return json_response(
                {
                    "ok": True,
                    "runs": history.list_runs(
                        automation_id=str(payload.get("automationId") or ""),
                        project_id=str(payload.get("projectId") or ""),
                        status=str(payload.get("status") or ""),
                        limit=_limit(payload),
                    ),
                }
            )
        if action == "templates":
            return json_response({"ok": True, "templates": registry.list_templates()})
        if action == "create_from_template":
            return json_response(
                {
                    "ok": True,
                    "automation": registry.create_from_template(
                        _template_id(payload),
                        project_id=str(payload.get("projectId") or ""),
                        overrides=payload.get("overrides") if isinstance(payload.get("overrides"), dict) else {},
                    ),
                }
            )
        if action == "simulate":
            return json_response(
                scheduler.simulate_trigger(
                    _automation_id(payload),
                    trigger=_trigger_payload(payload),
                    event=payload.get("event") if isinstance(payload.get("event"), dict) else None,
                    now=_now_payload(payload),
                )
            )
        if action == "run_due":
            return json_response(
                scheduler.run_due(
                    now=_now_payload(payload),
                    event=payload.get("event") if isinstance(payload.get("event"), dict) else None,
                    confirmed=_bool(payload, "confirmed"),
                )
            )
        raise AppError("Unsupported Automation action", code=ErrorCode.INVALID_PAYLOAD)

    @router.get("/api/automation/templates")
    async def api_automation_templates(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "templates": registry.list_templates()})

    @router.post("/api/automation/templates/{template_id}")
    async def api_automation_create_from_template(request: Request, template_id: str) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        automation = registry.create_from_template(
            template_id,
            project_id=str(payload.get("projectId") or ""),
            overrides=payload.get("overrides") if isinstance(payload.get("overrides"), dict) else {},
        )
        return json_response({"ok": True, "automation": automation})

    @router.get("/api/automation/{automation_id}")
    async def api_automation_get(request: Request, automation_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "automation": registry.get_automation(automation_id)})

    @router.patch("/api/automation/{automation_id}")
    async def api_automation_patch(request: Request, automation_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "automation": registry.update_automation(automation_id, await read_json_body(request))})

    @router.delete("/api/automation/{automation_id}")
    async def api_automation_delete(request: Request, automation_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "deleted": registry.delete_automation(automation_id)})

    @router.post("/api/automation/{automation_id}/run")
    async def api_automation_run(request: Request, automation_id: str) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request) if int(request.headers.get("Content-Length") or "0") > 0 else {}
        return json_response(
            {
                "ok": True,
                "run": runner.run_once(
                    automation_id,
                    trigger=_trigger_payload(payload),
                    event=payload.get("event") if isinstance(payload.get("event"), dict) else None,
                    now=_now_payload(payload),
                    confirmed=_bool(payload, "confirmed"),
                    force=_bool(payload, "force"),
                ),
            }
        )

    @router.get("/api/automation/{automation_id}/runs")
    async def api_automation_runs(request: Request, automation_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "runs": history.list_runs(automation_id=automation_id, limit=int(request.query_params.get("limit") or 100))})

    return router


def _automation_id(payload: dict[str, Any]) -> str:
    automation_id = str(payload.get("automationId") or payload.get("id") or "").strip()
    if not automation_id:
        raise AppError("automationId is required", code=ErrorCode.INVALID_PAYLOAD)
    return automation_id


def _run_id(payload: dict[str, Any]) -> str:
    run_id = str(payload.get("runId") or payload.get("id") or "").strip()
    if not run_id:
        raise AppError("runId is required", code=ErrorCode.INVALID_PAYLOAD)
    return run_id


def _template_id(payload: dict[str, Any]) -> str:
    template_id = str(payload.get("templateId") or payload.get("id") or "").strip()
    if not template_id:
        raise AppError("templateId is required", code=ErrorCode.INVALID_PAYLOAD)
    return template_id


def _automation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("automation", "config"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {key: value for key, value in payload.items() if key not in {"action"}}


def _patch_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("patch", "automation", "config"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {key: value for key, value in payload.items() if key not in {"action", "automationId", "id"}}


def _trigger_payload(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("trigger")
    return value if isinstance(value, dict) else {"type": "manual"}


def _now_payload(payload: dict[str, Any]) -> datetime | None:
    value = payload.get("now")
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppError("now must be an ISO timestamp or epoch milliseconds", code=ErrorCode.INVALID_PAYLOAD) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _limit(payload: dict[str, Any]) -> int:
    try:
        return max(0, min(int(str(payload.get("limit") or 100)), 2_000))
    except (TypeError, ValueError):
        return 100


def _bool(payload: dict[str, Any], key: str, *, default: bool = False) -> bool:
    if key not in payload:
        return default
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    return truthy(value)

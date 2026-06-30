"""Skill System registry and runner routes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from deepseek_infra.core.config import APP_VERSION
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import utc_now_iso
from deepseek_infra.infra.skills import eval as skill_eval
from deepseek_infra.infra.skills.pack import tool_permission_summary
from deepseek_infra.infra.skills.permissions import skill_allowed_tools
from deepseek_infra.infra.skills.schema import SkillSchemaError, validate_instance, validate_skill_config
from deepseek_infra.infra.skills.templates import offline_skill_content
from deepseek_infra.web.http_utils import json_response, read_json_body, require_api_auth, truthy


@dataclass(frozen=True)
class SkillsRouteDeps:
    list_skills: Callable[..., list[dict[str, Any]]]
    list_builtin_skills: Callable[..., list[dict[str, Any]]]
    get_skill: Callable[..., dict[str, Any]]
    create_custom_skill: Callable[..., dict[str, Any]]
    update_skill: Callable[[str, dict[str, Any]], dict[str, Any]]
    set_skill_disabled: Callable[[str, bool], dict[str, Any]]
    delete_skill: Callable[[str], dict[str, Any]]
    import_skill_config: Callable[..., dict[str, Any]]
    export_skill_config: Callable[[str], dict[str, Any]]
    run_skill: Callable[..., dict[str, Any]]
    list_packs: Callable[..., list[dict[str, Any]]]
    get_pack: Callable[[str], dict[str, Any]]
    export_pack: Callable[[str], dict[str, Any]]
    import_pack: Callable[..., dict[str, Any]]
    validate_pack: Callable[[dict[str, Any]], dict[str, Any]]
    delete_pack: Callable[[str], dict[str, Any]]


def create_skills_router(deps: SkillsRouteDeps) -> APIRouter:
    router = APIRouter()

    @router.post("/api/skills")
    async def api_skills(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        action = str(payload.get("action") or "list").strip().lower()
        if action == "list":
            return json_response({"ok": True, "skills": deps.list_skills(include_disabled=_bool(payload, "includeDisabled"))})
        if action == "builtin":
            return json_response({"ok": True, "skills": deps.list_builtin_skills(include_disabled=_bool(payload, "includeDisabled", default=True))})
        if action == "get":
            return json_response({"ok": True, "skill": deps.get_skill(_skill_id(payload), include_disabled=True)})
        if action == "create":
            return json_response({"ok": True, "skill": deps.create_custom_skill(_skill_config(payload), overwrite=_bool(payload, "overwrite"))})
        if action == "validate":
            return json_response({"ok": True, "skill": _validate_skill_config(_skill_config(payload))})
        if action == "update":
            return json_response({"ok": True, "skill": deps.update_skill(_skill_id(payload), _skill_patch(payload))})
        if action == "disable":
            return json_response({"ok": True, "skill": deps.set_skill_disabled(_skill_id(payload), True)})
        if action == "enable":
            return json_response({"ok": True, "skill": deps.set_skill_disabled(_skill_id(payload), False)})
        if action == "delete":
            return json_response(deps.delete_skill(_skill_id(payload)))
        if action == "import":
            return json_response({"ok": True, "skill": deps.import_skill_config(_skill_config(payload), overwrite=_bool(payload, "overwrite"))})
        if action == "export":
            return json_response({"ok": True, "skill": deps.export_skill_config(_skill_id(payload))})
        if action == "run":
            return json_response(_run_skill(deps, payload, skill_id=_skill_id(payload)))
        if action == "dry_run":
            return json_response(_dry_run_skill_config(payload))
        if action == "list_packs":
            return json_response({"ok": True, "packs": deps.list_packs(include_builtin=_bool(payload, "includeBuiltin", default=True))})
        if action == "get_pack":
            return json_response({"ok": True, "pack": deps.get_pack(_pack_id(payload))})
        if action == "export_pack":
            return json_response({"ok": True, "pack": deps.export_pack(_pack_id(payload))})
        if action == "validate_pack":
            return json_response({"ok": True, "pack": deps.validate_pack(_pack_config(payload)), "toolPermissions": tool_permission_summary(deps.validate_pack(_pack_config(payload)))})
        if action == "import_pack":
            return json_response(
                deps.import_pack(
                    _pack_config(payload),
                    overwrite=_bool(payload, "overwrite"),
                    on_conflict=str(payload.get("onConflict") or "error"),
                )
            )
        if action == "delete_pack":
            return json_response(deps.delete_pack(_pack_id(payload)))
        if action == "eval_report":
            return json_response(
                {
                    "ok": True,
                    "report": skill_eval.build_skill_eval_report(
                        version=str(payload.get("version") or APP_VERSION),
                        scope=str(payload.get("scope") or "all"),
                        skill_id=str(payload.get("skillId") or ""),
                        pack_id=str(payload.get("packId") or ""),
                        baseline=payload.get("baseline") if isinstance(payload.get("baseline"), dict) else None,
                    ),
                }
            )
        if action == "list_eval_cases":
            return json_response({"ok": True, "cases": skill_eval.load_eval_cases(include_user=True)})
        if action == "create_eval_case":
            return json_response({"ok": True, "case": skill_eval.save_eval_case(_eval_case(payload))})
        if action == "delete_eval_case":
            return json_response(skill_eval.delete_eval_case(_case_id(payload)))
        raise AppError("Unsupported Skill action", code=ErrorCode.INVALID_PAYLOAD)

    @router.post("/api/skills/{skill_id}/run")
    async def api_skill_run(request: Request, skill_id: str) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        return json_response(_run_skill(deps, payload, skill_id=skill_id))

    return router


def _skill_id(payload: dict[str, Any]) -> str:
    skill_id = str(payload.get("skillId") or payload.get("id") or "").strip()
    if not skill_id:
        raise AppError("skillId is required", code=ErrorCode.INVALID_PAYLOAD)
    return skill_id


def _pack_id(payload: dict[str, Any]) -> str:
    pack_id = str(payload.get("packId") or payload.get("id") or "").strip()
    if not pack_id:
        raise AppError("packId is required", code=ErrorCode.INVALID_PAYLOAD)
    return pack_id


def _pack_config(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("pack", "config"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    candidate = {
        key: value
        for key, value in payload.items()
        if key not in {"action", "overwrite", "onConflict"}
    }
    if not candidate:
        raise AppError("Skill Pack config is required", code=ErrorCode.INVALID_PAYLOAD)
    return candidate


def _skill_config(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("skill", "config"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    candidate = {key: value for key, value in payload.items() if key not in {"action", "overwrite"}}
    if not candidate:
        raise AppError("Skill config is required", code=ErrorCode.INVALID_PAYLOAD)
    return candidate


def _eval_case(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("case")
    if isinstance(value, dict):
        return value
    candidate = {key: value for key, value in payload.items() if key not in {"action"}}
    if not candidate:
        raise AppError("Skill eval case is required", code=ErrorCode.INVALID_PAYLOAD)
    return candidate


def _case_id(payload: dict[str, Any]) -> str:
    case_id = str(payload.get("caseId") or payload.get("id") or "").strip()
    if not case_id:
        raise AppError("caseId is required", code=ErrorCode.INVALID_PAYLOAD)
    return case_id


def _skill_patch(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("patch", "skill", "config"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {key: value for key, value in payload.items() if key not in {"action", "skillId", "id"}}


def _run_input(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("input", "inputData", "inputs"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _run_skill(deps: SkillsRouteDeps, payload: dict[str, Any], *, skill_id: str) -> dict[str, Any]:
    return deps.run_skill(
        skill_id,
        _run_input(payload),
        project_id=str(payload.get("projectId") or ""),
        offline=_bool(payload, "offline"),
        api_key=str(payload.get("apiKey") or ""),
        tavily_api_key=str(payload.get("tavilyApiKey") or ""),
        model=str(payload.get("model") or ""),
        persist=_bool(payload, "persist", default=True),
    )


def _validate_skill_config(config: dict[str, Any]) -> dict[str, Any]:
    try:
        return validate_skill_config(config)
    except SkillSchemaError as exc:
        raise AppError(str(exc), code=ErrorCode.INVALID_PAYLOAD) from exc


def _dry_run_skill_config(payload: dict[str, Any]) -> dict[str, Any]:
    skill = _validate_skill_config(_skill_config(payload))
    input_data = _run_input(payload)
    input_violations = validate_instance(input_data, skill.get("inputSchema") or {}, label="input")
    if input_violations:
        raise AppError("Skill input failed schema validation: " + "; ".join(input_violations), code=ErrorCode.INVALID_PAYLOAD)
    output = {
        "content": offline_skill_content(skill, input_data, project_context=""),
        "mode": "offline",
    }
    output_violations = validate_instance(output, skill.get("outputSchema") or {}, label="output")
    if output_violations:
        raise AppError("Skill output failed schema validation: " + "; ".join(output_violations), code=ErrorCode.INVALID_PAYLOAD)
    return {
        "ok": True,
        "skillRunId": "dry-run",
        "skillId": skill["skillId"],
        "projectId": "",
        "status": "completed",
        "input": input_data,
        "output": output,
        "artifacts": [],
        "savedItems": [],
        "traceId": "",
        "startedAt": utc_now_iso(),
        "completedAt": utc_now_iso(),
        "policy": {"allowedTools": skill_allowed_tools(skill)},
        "dryRun": True,
    }


def _bool(payload: dict[str, Any], key: str, *, default: bool = False) -> bool:
    if key not in payload:
        return default
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    return truthy(value)

"""Automation action runner built on existing runtimes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.automation import schema
from deepseek_infra.infra.workspace import artifacts as workspace_artifacts
from deepseek_infra.infra.workspace import exports as workspace_exports
from deepseek_infra.infra.workspace import saved_items as workspace_saved_items
from deepseek_infra.infra.workspace.schema import (
    normalize_content,
    redact_sensitive_text,
    safe_filename,
    utc_now,
    write_json_atomic,
    read_json_file,
)


def run_action(
    automation: dict[str, Any],
    *,
    run_id: str,
    trigger: dict[str, Any],
    event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action = _action(automation)
    action_type = str(action.get("type") or "").strip().lower()
    if action_type == "run_skill":
        return run_skill_action(automation, run_id=run_id)
    if action_type == "browser_snapshot":
        return browser_snapshot_action(automation)
    if action_type == "browser_check":
        return browser_check_action(automation, run_id=run_id)
    if action_type == "project_summary":
        return project_summary_action(automation, run_id=run_id, event=event)
    if action_type == "media_process":
        return media_process_action(automation, run_id=run_id, event=event)
    if action_type == "create_artifact":
        return create_artifact_action(automation, run_id=run_id)
    if action_type == "save_item":
        return save_item_action(automation, run_id=run_id)
    if action_type == "export_conversation":
        return export_conversation_action(automation)
    if action_type == "export_project":
        return export_project_action(automation)
    raise AppError("Unsupported automation action", code=ErrorCode.INVALID_PAYLOAD)


def run_skill_action(automation: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    action = _action(automation)
    skill_id = str(action.get("skillId") or "").strip()
    if not skill_id:
        raise AppError("run_skill action requires skillId", code=ErrorCode.INVALID_PAYLOAD)
    raw_input = action.get("input")
    input_data: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
    from deepseek_infra.infra.skills.runner import run_skill

    result = run_skill(
        skill_id,
        input_data,
        project_id=str(action.get("projectId") or automation.get("projectId") or ""),
        offline=_bool(action.get("offline"), default=True),
        api_key=str(action.get("apiKey") or ""),
        tavily_api_key=str(action.get("tavilyApiKey") or ""),
        model=str(action.get("model") or ""),
        persist=True,
        security_approved=_bool(action.get("securityApproved"), default=False),
    )
    return {
        "outputs": {
            "artifactIds": _artifact_ids(result.get("artifacts")),
            "savedItemIds": _saved_item_ids(result.get("savedItems")),
            "mediaIds": [],
            "exportIds": [],
        },
        "logs": [f"skill:{skill_id}", f"skillRun:{result.get('skillRunId') or ''}"],
        "traceId": str(result.get("traceId") or ""),
        "raw": {"skillRunId": result.get("skillRunId"), "status": result.get("status")},
    }


def browser_snapshot_action(automation: dict[str, Any]) -> dict[str, Any]:
    action = _action(automation)
    url = str(action.get("url") or "").strip()
    if not url:
        raise AppError("browser_snapshot action requires url", code=ErrorCode.INVALID_PAYLOAD)
    from deepseek_infra.infra.browser.actions import execute_browser_action

    project_id = str(action.get("projectId") or automation.get("projectId") or "")
    opened = execute_browser_action({"action": "open_url", "projectId": project_id, "url": url})
    if not opened.get("ok"):
        raise AppError(str(opened.get("error") or "Browser action blocked"), code=ErrorCode.FORBIDDEN, status=403)
    session_id = str(opened.get("session", {}).get("browserSessionId") or "")
    read = execute_browser_action({"action": "read_page", "sessionId": session_id, "selector": str(action.get("selector") or "")})
    if not read.get("ok"):
        raise AppError(str(read.get("error") or "Browser read failed"), code=ErrorCode.FORBIDDEN, status=403)
    media_ids = _media_ids_from_browser_result(read)
    if _bool(action.get("screenshot"), default=False):
        shot = execute_browser_action({"action": "screenshot", "sessionId": session_id, "title": str(action.get("title") or "Automation screenshot")})
        media_ids.extend(_media_ids_from_browser_result(shot))
    raw_read_result = read.get("result")
    read_result: dict[str, Any] = raw_read_result if isinstance(raw_read_result, dict) else {}
    return {
        "outputs": {"artifactIds": [], "savedItemIds": [], "mediaIds": _unique(media_ids), "exportIds": []},
        "logs": [f"browserSnapshot:{url}", f"browserSession:{session_id}"],
        "raw": {"url": url, "sessionId": session_id, "text": str(read_result.get("text") or "")},
    }


def browser_check_action(automation: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    action = _action(automation)
    project_id = str(action.get("projectId") or automation.get("projectId") or "")
    url = str(action.get("url") or action.get("fixturePath") or "inline").strip()
    text = browser_check_text(automation)
    text_hash = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
    state = _load_state(str(automation.get("automationId") or ""))
    previous = str(state.get("textHash") or "")
    if previous == text_hash:
        return {
            "outputs": {"artifactIds": [], "savedItemIds": [], "mediaIds": [], "exportIds": []},
            "logs": [f"browserCheck:{url}", "unchanged"],
            "skippedReason": "url_unchanged",
        }
    state.update({"url": url, "textHash": text_hash, "updatedAt": utc_now()})
    _write_state(str(automation.get("automationId") or ""), state)
    saved_ids: list[str] = []
    if project_id:
        item = workspace_saved_items.create_saved_item(
            project_id,
            item_type="webpage",
            title=str(action.get("title") or "Webpage change detected"),
            content=redact_sensitive_text(text[:20_000]),
            source_ref={"type": "automation", "automationId": automation.get("automationId"), "runId": run_id, "url": url},
            tags=["automation", "webpage-change"],
            purpose="reference",
        )
        saved_ids.append(str(item.get("savedId") or ""))
    return {
        "outputs": {"artifactIds": [], "savedItemIds": saved_ids, "mediaIds": [], "exportIds": []},
        "logs": [f"browserCheck:{url}", "changed"],
        "raw": {"url": url, "changed": True},
    }


def browser_check_text(automation: dict[str, Any]) -> str:
    action = _action(automation)
    for key in ("text", "content", "html"):
        value = action.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_content(value)
    fixture = str(action.get("fixturePath") or "").strip()
    if fixture:
        path = Path(fixture)
        if not path.is_absolute():
            path = Path.cwd() / path
        return normalize_content(path.read_text(encoding="utf-8", errors="replace"))
    result = browser_snapshot_action(automation)
    raw_result = result.get("raw")
    raw: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
    return normalize_content(str(raw.get("text") or json.dumps(raw, ensure_ascii=False, sort_keys=True)))


def project_summary_action(automation: dict[str, Any], *, run_id: str, event: dict[str, Any] | None = None) -> dict[str, Any]:
    project_id = str(automation.get("projectId") or "")
    if not project_id:
        raise AppError("project_summary action requires projectId", code=ErrorCode.INVALID_PAYLOAD)
    bundle = workspace_exports.project_bundle(project_id)
    raw_metadata = bundle.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    lines = [
        f"# Automation Summary: {metadata.get('name') or project_id}",
        "",
        f"- Automation: `{automation.get('automationId')}`",
        f"- Run: `{run_id}`",
        f"- Generated: `{utc_now()}`",
        f"- Files: `{len(metadata.get('files') or metadata.get('documents') or [])}`",
        f"- Saved items: `{len(bundle.get('savedItems') or [])}`",
        f"- Artifacts: `{len(bundle.get('artifacts') or [])}`",
        f"- Media: `{len(bundle.get('media') or [])}`",
        "",
    ]
    if event:
        lines += ["## Trigger Event", "", "```json", json.dumps(event, ensure_ascii=False, indent=2), "```", ""]
    saved_items = bundle.get("savedItems")
    if isinstance(saved_items, list) and saved_items:
        lines += ["## Recent Saved Items", ""]
        for item in saved_items[:10]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('title') or item.get('savedId')}")
        lines.append("")
    content = "\n".join(lines).strip() + "\n"
    created = _persist_content_outputs(automation, run_id=run_id, title="Automation Project Summary", content=content, item_type="assistant_answer")
    return {**created, "logs": [*created.get("logs", []), "projectSummary"]}


def media_process_action(automation: dict[str, Any], *, run_id: str, event: dict[str, Any] | None = None) -> dict[str, Any]:
    action = _action(automation)
    raw_input = action.get("input")
    input_data: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
    raw_media_ids = input_data.get("mediaIds")
    media_ids = [str(media_id) for media_id in raw_media_ids if str(media_id or "").strip()] if isinstance(raw_media_ids, list) else []
    event_media_id = str((event or {}).get("mediaId") or "")
    if event_media_id and event_media_id not in media_ids:
        media_ids = [*media_ids, event_media_id]
    if not media_ids:
        raise AppError("media_process action requires mediaIds", code=ErrorCode.INVALID_PAYLOAD)
    synthetic = {
        **automation,
        "action": {
            "type": "run_skill",
            "skillId": str(action.get("skillId") or "media_to_report"),
            "input": {**input_data, "mediaIds": media_ids, "task": str(input_data.get("task") or "Write a cited media digest.")},
            "offline": _bool(action.get("offline"), default=True),
        },
    }
    return run_skill_action(synthetic, run_id=run_id)


def create_artifact_action(automation: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    action = _action(automation)
    content = str(action.get("content") or action.get("text") or "")
    if not content:
        raise AppError("create_artifact action requires content", code=ErrorCode.INVALID_PAYLOAD)
    return _persist_content_outputs(
        automation,
        run_id=run_id,
        title=str(action.get("title") or automation.get("name") or "Automation artifact"),
        content=content,
        item_type=str(action.get("itemType") or "assistant_answer"),
        save_item=False,
    )


def save_item_action(automation: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    action = _action(automation)
    project_id = str(action.get("projectId") or automation.get("projectId") or "")
    if not project_id:
        raise AppError("save_item action requires projectId", code=ErrorCode.INVALID_PAYLOAD)
    item = workspace_saved_items.create_saved_item(
        project_id,
        item_type=str(action.get("itemType") or "assistant_answer"),
        title=str(action.get("title") or automation.get("name") or "Automation saved item"),
        content=str(action.get("content") or ""),
        source_ref={"type": "automation", "automationId": automation.get("automationId"), "runId": run_id},
        tags=["automation", *[str(tag) for tag in action.get("tags", []) if isinstance(tag, str)]] if isinstance(action.get("tags"), list) else ["automation"],
        purpose=str(action.get("purpose") or "reference"),
    )
    return {
        "outputs": {"artifactIds": [], "savedItemIds": [str(item.get("savedId") or "")], "mediaIds": [], "exportIds": []},
        "logs": ["saveItem"],
        "raw": {"savedItem": item},
    }


def export_conversation_action(automation: dict[str, Any]) -> dict[str, Any]:
    action = _action(automation)
    raw_conversation = action.get("conversation")
    conversation: dict[str, Any] = raw_conversation if isinstance(raw_conversation, dict) else {}
    result = workspace_exports.export_conversation(
        conversation,
        project_id=str(action.get("projectId") or automation.get("projectId") or ""),
        export_format=str(action.get("format") or "markdown"),
    )
    raw_export = result.get("export")
    export: dict[str, Any] = raw_export if isinstance(raw_export, dict) else {}
    return {"outputs": {"artifactIds": [], "savedItemIds": [], "mediaIds": [], "exportIds": [str(export.get("exportId") or "")]}, "logs": ["exportConversation"], "raw": result}


def export_project_action(automation: dict[str, Any]) -> dict[str, Any]:
    action = _action(automation)
    project_id = str(action.get("projectId") or automation.get("projectId") or "")
    if not project_id:
        raise AppError("export_project action requires projectId", code=ErrorCode.INVALID_PAYLOAD)
    result = workspace_exports.export_project(project_id, export_format=str(action.get("format") or "zip"))
    raw_export = result.get("export")
    export: dict[str, Any] = raw_export if isinstance(raw_export, dict) else {}
    return {"outputs": {"artifactIds": [], "savedItemIds": [], "mediaIds": [], "exportIds": [str(export.get("exportId") or "")]}, "logs": ["exportProject"], "raw": result}


def _persist_content_outputs(
    automation: dict[str, Any],
    *,
    run_id: str,
    title: str,
    content: str,
    item_type: str,
    save_item: bool = True,
) -> dict[str, Any]:
    project_id = str(automation.get("projectId") or "")
    raw_output = automation.get("output")
    output: dict[str, Any] = raw_output if isinstance(raw_output, dict) else {}
    artifact_ids: list[str] = []
    saved_ids: list[str] = []
    if project_id and output.get("createArtifact", True):
        artifact_type = str(output.get("artifactType") or "markdown")
        suffix = "md" if artifact_type == "markdown" else artifact_type
        target_dir = config.GENERATED_DIR / "automation" / project_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{safe_filename(title, 'automation-artifact')}-{run_id}.{suffix}"
        target.write_text(redact_sensitive_text(content).rstrip() + "\n", encoding="utf-8")
        artifact = workspace_artifacts.register_artifact(
            project_id,
            artifact_type=artifact_type,
            title=title,
            path=str(target),
            source={"type": "automation", "automationId": automation.get("automationId"), "runId": run_id},
        )
        artifact_ids.append(str(artifact.get("artifactId") or ""))
    if project_id and save_item and output.get("saveToProject", True):
        item = workspace_saved_items.create_saved_item(
            project_id,
            item_type=item_type,
            title=title,
            content=redact_sensitive_text(content),
            source_ref={"type": "automation", "automationId": automation.get("automationId"), "runId": run_id},
            tags=["automation"],
            purpose="reference",
        )
        saved_ids.append(str(item.get("savedId") or ""))
    return {
        "outputs": {"artifactIds": artifact_ids, "savedItemIds": saved_ids, "mediaIds": [], "exportIds": []},
        "logs": ["persistContent"],
        "raw": {"title": title, "bytes": len(content.encode("utf-8"))},
    }


def _load_state(automation_id: str) -> dict[str, Any]:
    return read_json_file(_state_path(automation_id), default={})


def _write_state(automation_id: str, state: dict[str, Any]) -> None:
    write_json_atomic(_state_path(automation_id), state)


def _state_path(automation_id: str) -> Path:
    return config.AUTOMATION_DIR / "state" / f"{schema.validate_automation_id(automation_id)}.json"


def _action(automation: dict[str, Any]) -> dict[str, Any]:
    action = automation.get("action")
    return action if isinstance(action, dict) else {}


def _artifact_ids(value: Any) -> list[str]:
    return _unique(str(item.get("artifactId") or "") for item in value if isinstance(item, dict)) if isinstance(value, list) else []


def _saved_item_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return _unique(str(item.get("savedId") or item.get("id") or "") for item in value if isinstance(item, dict))


def _media_ids_from_browser_result(value: dict[str, Any]) -> list[str]:
    raw_result = value.get("result")
    result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
    ids: list[str] = []
    for key in ("snapshot", "screenshot", "media"):
        item = result.get(key)
        if isinstance(item, dict) and item.get("mediaId"):
            ids.append(str(item["mediaId"]))
    return ids


def _unique(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}

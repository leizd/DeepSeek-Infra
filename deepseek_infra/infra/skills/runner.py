"""Capability-based Skill runner."""

from __future__ import annotations

import inspect
import re
import secrets
from typing import Any, Callable

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import utc_now_iso
from deepseek_infra.infra.data import projects
from deepseek_infra.infra.observability.observability import finish_trace, start_span, start_trace
from deepseek_infra.infra.skills import analytics, evidence, registry, security
from deepseek_infra.infra.skills.permissions import skill_allowed_tools
from deepseek_infra.infra.skills.schema import validate_instance
from deepseek_infra.infra.skills.templates import format_project_context, offline_skill_content, skill_system_prompt, skill_user_message

LLMCallable = Callable[..., dict[str, Any]]
MEDIA_CONTEXT_MAX_CHARS = 24_000
MEDIA_SEGMENT_MAX_CHARS = 1_600
MEDIA_CONTEXT_MAX_MEDIA = 12
MEDIA_CONTEXT_MAX_SEGMENTS_PER_MEDIA = 12


def run_skill(
    skill_id: str,
    input_data: dict[str, Any],
    *,
    project_id: str = "",
    offline: bool = False,
    api_key: str = "",
    tavily_api_key: str = "",
    model: str = "",
    llm_callable: LLMCallable | None = None,
    persist: bool = True,
    security_approved: bool = False,
) -> dict[str, Any]:
    skill = registry.get_skill(skill_id)
    run_id = f"run-{secrets.token_hex(8)}"
    started_at = utc_now_iso()
    security_context = security.security_context_for_skill(skill, approved=security_approved, persist_review=persist)
    security_metadata = security.run_security_metadata(security_context)
    if security_context["blocked"]:
        exc = AppError(str(security_context["blockedReason"]), code=ErrorCode.FORBIDDEN, status=403)
        if persist:
            analytics.record_failure(
                skill=skill,
                run_id=run_id,
                input_data=input_data,
                project_id=project_id,
                started_at=started_at,
                error=exc,
                offline=offline,
                model=model,
                category="security_review_blocked",
                security_metadata=security_metadata,
            )
        raise exc
    if not isinstance(input_data, dict):
        exc = AppError("Skill input must be an object", code=ErrorCode.INVALID_PAYLOAD)
        if persist:
            analytics.record_failure(
                skill=skill,
                run_id=run_id,
                input_data=input_data,
                project_id=project_id,
                started_at=started_at,
                error=exc,
                offline=offline,
                model=model,
                category="schema_validation_failed",
                security_metadata=security_metadata,
            )
        raise exc
    input_violations = validate_instance(input_data, skill.get("inputSchema") or {}, label="input")
    if input_violations:
        exc = AppError("Skill input failed schema validation: " + "; ".join(input_violations), code=ErrorCode.INVALID_PAYLOAD)
        if persist:
            analytics.record_failure(
                skill=skill,
                run_id=run_id,
                input_data=input_data,
                project_id=project_id,
                started_at=started_at,
                error=exc,
                offline=offline,
                model=model,
                category="schema_validation_failed",
                security_metadata=security_metadata,
            )
        raise exc

    binding_enabled = bool((skill.get("projectBinding") or {}).get("enabled"))
    try:
        project = projects.require_project(project_id) if project_id and binding_enabled else None
    except Exception as exc:
        if persist:
            analytics.record_failure(
                skill=skill,
                run_id=run_id,
                input_data=input_data,
                project_id=project_id,
                started_at=started_at,
                error=exc,
                offline=offline,
                model=model,
                category="project_binding_failed",
                security_metadata=security_metadata,
            )
        raise
    project_context = _combined_context(format_project_context(project), _media_context(input_data, project_id=project_id))
    trace_id = start_trace(
        kind="skill",
        title=str(skill.get("name") or skill_id),
        metadata={"skillId": skill["skillId"], "skillRunId": run_id, "skillVersion": skill.get("version"), "projectId": project_id, "offline": offline},
    )
    run_span = start_span(
        trace_id,
        name=f"skill.run:{skill['skillId']}",
        kind="skill_run",
        input_data={"skillId": skill["skillId"], "input": input_data, "projectId": project_id, "offline": offline},
    )
    try:
        output = _offline_output(skill, input_data, project_context=project_context) if offline else _llm_output(
            skill,
            input_data,
            project_id=project_id,
            api_key=api_key,
            tavily_api_key=tavily_api_key,
            model=model,
            project_context=project_context,
            llm_callable=llm_callable,
            parent_span_id=run_span.span_id,
        )
        output_violations = validate_instance(output, skill.get("outputSchema") or {}, label="output")
        if output_violations:
            raise AppError("Skill output failed schema validation: " + "; ".join(output_violations), code=ErrorCode.INTERNAL, status=500)
        artifacts, saved_items = _apply_artifact_policy(skill, output, project_id=project_id if binding_enabled else "", run_id=run_id, persist=persist)
        completed_at = utc_now_iso()
        result = {
            "ok": True,
            "skillRunId": run_id,
            "skillId": skill["skillId"],
            "skillVersion": skill.get("version") or "",
            "projectId": project_id if binding_enabled else "",
            "status": "completed",
            "input": input_data,
            "output": output,
            "artifacts": artifacts,
            "savedItems": saved_items,
            "traceId": trace_id,
            "startedAt": started_at,
            "completedAt": completed_at,
            "policy": {"allowedTools": skill_allowed_tools(skill)},
            "security": security_metadata,
        }
        if persist:
            run_record = analytics.record_success(skill=skill, result=result, offline=offline, model=model, security_metadata=security_metadata)
            result["packId"] = run_record.get("packId")
            result["latencyMs"] = run_record.get("latencyMs")
            result["analytics"] = run_record
        else:
            run_record = {}
        if persist and project_id and binding_enabled:
            projects.append_project_skill_run(project_id, analytics.project_run_record(run_record or _project_run_record(result), input_data=input_data))
        run_span.finish(status="ok", output_data={"artifactCount": len(artifacts), "savedItemCount": len(saved_items)})
        finish_trace(trace_id, metadata={"skillId": skill["skillId"], "skillRunId": run_id, "projectId": project_id})
        return result
    except Exception as exc:
        if persist:
            failed_record = analytics.record_failure(
                skill=skill,
                run_id=run_id,
                input_data=input_data,
                project_id=project_id if binding_enabled else "",
                started_at=started_at,
                trace_id=trace_id,
                error=exc,
                offline=offline,
                model=model,
                security_metadata=security_metadata,
            )
            if project_id and binding_enabled:
                try:
                    projects.append_project_skill_run(project_id, analytics.project_run_record(failed_record, input_data=input_data))
                except Exception:
                    pass
        run_span.finish(status="error", error=str(exc))
        finish_trace(trace_id, status="error", error=str(exc))
        raise


def _offline_output(skill: dict[str, Any], input_data: dict[str, Any], *, project_context: str = "") -> dict[str, Any]:
    return {
        "content": offline_skill_content(skill, input_data, project_context=project_context),
        "mode": "offline",
    }


def _combined_context(*parts: str) -> str:
    return "\n\n".join(part for part in parts if str(part or "").strip())


def _media_context(input_data: dict[str, Any], *, project_id: str = "") -> str:
    raw_ids = input_data.get("mediaIds")
    if not isinstance(raw_ids, list):
        raw_single = input_data.get("mediaId")
        raw_ids = [raw_single] if raw_single else []
    media_ids = [str(item or "").strip() for item in raw_ids if str(item or "").strip()][:MEDIA_CONTEXT_MAX_MEDIA]
    if not media_ids:
        return ""
    try:
        from deepseek_infra.infra.media import library as media_library
    except Exception:
        return ""
    query_terms = _media_context_terms(input_data)
    lines = ["[Media context]"]
    used_chars = len(lines[0])
    for position, media_id in enumerate(media_ids, start=1):
        try:
            media = media_library.get_media(media_id)
        except Exception:
            line = f"- M{position}: warning: mediaId={media_id} was not found"
            if not _append_context_line(lines, line, used_chars):
                break
            used_chars += len(line) + 1
            continue
        media_project = str(media.get("projectId") or "")
        if project_id and media_project and media_project != project_id:
            line = f"- M{position}: warning: mediaId={media_id} belongs to a different project"
            if not _append_context_line(lines, line, used_chars):
                break
            used_chars += len(line) + 1
            continue
        header = f"- M{position}: {media.get('title')} ({media.get('type')}, mediaId={media.get('mediaId')}, status={media.get('status')})"
        if not _append_context_line(lines, header, used_chars):
            break
        used_chars += len(header) + 1
        ranked_segments = sorted(media_library.list_segments(media_id), key=lambda segment: _media_segment_rank(segment, query_terms))
        for segment in ranked_segments[:MEDIA_CONTEXT_MAX_SEGMENTS_PER_MEDIA]:
            raw_citation = segment.get("citation")
            citation: dict[str, Any] = raw_citation if isinstance(raw_citation, dict) else {}
            locator = str(citation.get("markdown") or citation.get("uri") or segment.get("segmentId") or "")
            text = str(segment.get("text") or "").strip()
            if len(text) > MEDIA_SEGMENT_MAX_CHARS:
                text = text[:MEDIA_SEGMENT_MAX_CHARS].rstrip() + "\n[truncated]"
            block = f"  segment {segment.get('type')} {locator}:\n{text}"
            if used_chars + len(block) + 1 > MEDIA_CONTEXT_MAX_CHARS:
                if not _append_context_line(lines, "  [media context truncated]", used_chars):
                    pass
                return "\n".join(lines)
            lines.append(block)
            used_chars += len(block) + 1
    return "\n".join(lines)


def _append_context_line(lines: list[str], line: str, used_chars: int) -> bool:
    if used_chars + len(line) + 1 > MEDIA_CONTEXT_MAX_CHARS:
        return False
    lines.append(line)
    return True


def _media_context_terms(input_data: dict[str, Any]) -> set[str]:
    values = []
    for key in ("task", "query", "question", "prompt", "goal"):
        value = input_data.get(key)
        if isinstance(value, str):
            values.append(value)
    text = " ".join(values).lower()
    return {word for word in re.findall(r"[a-z0-9_\u4e00-\u9fff]{2,}", text) if len(word) >= 2}


def _media_segment_rank(segment: dict[str, Any], query_terms: set[str]) -> tuple[int, int, int]:
    raw_citation = segment.get("citation")
    citation: dict[str, Any] = raw_citation if isinstance(raw_citation, dict) else {}
    haystack = " ".join(
        [
            str(segment.get("text") or ""),
            str(citation.get("label") or ""),
            str(citation.get("markdown") or ""),
            str(citation.get("uri") or ""),
        ]
    ).lower()
    score = sum(1 for term in query_terms if term and term in haystack)
    citation_bonus = 1 if citation else 0
    return (-score, -citation_bonus, int(segment.get("index") or 0))


def _llm_output(
    skill: dict[str, Any],
    input_data: dict[str, Any],
    *,
    project_id: str = "",
    api_key: str = "",
    tavily_api_key: str = "",
    model: str = "",
    project_context: str = "",
    llm_callable: LLMCallable | None = None,
    parent_span_id: str = "",
) -> dict[str, Any]:
    if llm_callable is None:
        from deepseek_infra.infra.gateway.deepseek_client import call_deepseek_cascade

        llm_callable = call_deepseek_cascade
    raw_memory_policy = skill.get("memoryPolicy")
    memory_policy: dict[str, Any] = raw_memory_policy if isinstance(raw_memory_policy, dict) else {}
    memory_scope = "global"
    if str(memory_policy.get("scope") or "") == "project" and project_id:
        memory_scope = f"project:{project_id}"
    payload = {
        "apiKey": api_key,
        "tavilyApiKey": tavily_api_key,
        "model": model,
        "systemPrompt": skill_system_prompt(skill, project_context=project_context),
        "messages": [{"role": "user", "content": skill_user_message(input_data), "projectId": project_id}],
        "allowedTools": skill_allowed_tools(skill),
        "searchEnabled": "web_search" in skill_allowed_tools(skill) or "compare_search_results" in skill_allowed_tools(skill),
        "memoryEnabled": bool(memory_policy.get("read")),
        "memoryScope": memory_scope,
        "skillRun": {"skillId": skill["skillId"], "projectId": project_id},
    }
    if not payload["model"]:
        payload.pop("model", None)
    response = _call_llm(llm_callable, payload, parent_span_id=parent_span_id)
    return {
        "content": str(response.get("content") or ""),
        "model": response.get("model"),
        "usage": response.get("usage") if isinstance(response.get("usage"), dict) else {},
        "diagnostics": response.get("diagnostics") if isinstance(response.get("diagnostics"), dict) else {},
    }


def _call_llm(llm_callable: LLMCallable, payload: dict[str, Any], *, parent_span_id: str) -> dict[str, Any]:
    try:
        supports_parent_span = "parent_span_id" in inspect.signature(llm_callable).parameters
    except (TypeError, ValueError):
        supports_parent_span = False
    if supports_parent_span:
        return llm_callable(payload, parent_span_id=parent_span_id)
    return llm_callable(payload)


def _apply_artifact_policy(
    skill: dict[str, Any],
    output: dict[str, Any],
    *,
    project_id: str,
    run_id: str,
    persist: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_policy = skill.get("artifactPolicy")
    policy: dict[str, Any] = raw_policy if isinstance(raw_policy, dict) else {}
    if not policy.get("autoSave"):
        return [], []
    artifacts: list[dict[str, Any]] = []
    saved_items: list[dict[str, Any]] = []
    content = str(output.get("content") or "")
    source = {"type": "skill_run", "skillId": skill["skillId"], "skillRunId": run_id, "projectId": project_id}
    if persist and project_id and content:
        saved_items.append(
            projects.add_project_saved_item(
                project_id,
                title=str(output.get("title") or skill.get("name") or "Skill output"),
                content=content,
                kind="skill_output",
                source=source,
            )
        )
    raw_types = policy.get("types")
    artifact_types = raw_types if isinstance(raw_types, list) else []
    if persist and "md" in artifact_types and content:
        artifact = evidence.save_markdown_artifact(
            title=str(output.get("title") or skill.get("name") or "Skill output"),
            content=content,
            skill_id=skill["skillId"],
            skill_run_id=run_id,
            project_id=project_id,
        )
        if artifact is not None:
            artifacts.append(artifact)
            if project_id:
                projects.link_project_artifact(project_id, artifact)
    if persist:
        for file_result in _find_file_results(output):
            artifact = evidence.register_generated_artifact(
                file_result,
                skill_id=skill["skillId"],
                skill_run_id=run_id,
                project_id=project_id,
                tool=str(file_result.get("tool") or ""),
            )
            if artifact is not None and artifact["artifactId"] not in {item.get("artifactId") for item in artifacts}:
                artifacts.append(artifact)
                if project_id:
                    projects.link_project_artifact(project_id, artifact)
    return artifacts, saved_items


def _find_file_results(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any, tool: str = "") -> None:
        if isinstance(node, dict):
            next_tool = str(node.get("tool") or tool)
            if _looks_like_file_result(node):
                item = dict(node)
                if next_tool:
                    item["tool"] = next_tool
                found.append(item)
            for child in node.values():
                walk(child, next_tool)
        elif isinstance(node, list):
            for child in node:
                walk(child, tool)

    walk(value)
    return found


def _looks_like_file_result(value: dict[str, Any]) -> bool:
    file_id = str(value.get("fileId") or "")
    return bool(re.fullmatch(r"[0-9a-f]{32}", file_id) and (value.get("downloadUrl") or value.get("filename")))


def _project_run_record(result: dict[str, Any]) -> dict[str, Any]:
    raw_output = result.get("output")
    output: dict[str, Any] = raw_output if isinstance(raw_output, dict) else {}
    record: dict[str, Any] = {
        "skillRunId": result.get("skillRunId"),
        "skillId": result.get("skillId"),
        "skillVersion": result.get("skillVersion"),
        "packId": result.get("packId"),
        "status": result.get("status"),
        "projectId": result.get("projectId"),
        "input": result.get("input") if isinstance(result.get("input"), dict) else {},
        "inputSummary": analytics.summarize_payload(result.get("input")),
        "outputSummary": str(output.get("content") or "")[:1200],
        "artifactIds": [str(item.get("artifactId") or "") for item in result.get("artifacts") or [] if isinstance(item, dict)],
        "savedItemIds": [str(item.get("id") or "") for item in result.get("savedItems") or [] if isinstance(item, dict)],
        "artifactCount": len([item for item in result.get("artifacts") or [] if isinstance(item, dict)]),
        "savedItemCount": len([item for item in result.get("savedItems") or [] if isinstance(item, dict)]),
        "traceId": result.get("traceId"),
        "startedAt": result.get("startedAt"),
        "completedAt": result.get("completedAt"),
        "latencyMs": result.get("latencyMs"),
        "offline": (output.get("mode") == "offline") if output else None,
        "model": output.get("model") if output else "",
    }
    security_meta = result.get("security")
    if isinstance(security_meta, dict):
        record.update(security_meta)
    return record

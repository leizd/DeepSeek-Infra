"""Workspace export builders for conversations, projects, saved items, artifacts, and evidence."""

from __future__ import annotations

import html
import json
import os
import platform
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.data import projects as legacy_projects
from deepseek_infra.infra.media import library as media_store
from deepseek_infra.infra.rag.files import load_cached_file
from deepseek_infra.infra.workspace import artifacts as artifact_store
from deepseek_infra.infra.workspace import projects as project_store
from deepseek_infra.infra.workspace import saved_items as saved_item_store
from deepseek_infra.infra.workspace.schema import (
    contains_secret,
    new_id,
    normalize_export_format,
    normalize_source_ref,
    normalize_title,
    now_ms,
    redact_sensitive_text,
    redact_value,
    safe_filename,
    timestamp_ms_to_iso,
    utc_now,
    validate_project_id,
    validate_workspace_id,
    write_json_atomic,
    read_json_file,
)

EXPORT_SCHEMA_VERSION = "workspace-export.v1"
STORE_NAME = "exports.json"


def create_export(payload: dict[str, Any]) -> dict[str, Any]:
    kind = str(payload.get("kind") or payload.get("type") or "project").strip().lower()
    export_format = normalize_export_format(payload.get("format") or "zip")
    project_id = str(payload.get("projectId") or "").strip()
    if kind == "project":
        return export_project(project_id, export_format=export_format)
    if kind == "conversation":
        raw_conversation = payload.get("conversation")
        conversation = raw_conversation if isinstance(raw_conversation, dict) else {}
        return export_conversation(
            conversation,
            project_id=project_id,
            export_format=export_format,
        )
    if kind in {"saved_items", "saved-items", "saved"}:
        ids = payload.get("savedIds")
        saved_ids = [str(item) for item in ids] if isinstance(ids, list) else []
        return export_saved_items(project_id, saved_ids=saved_ids, export_format=export_format)
    if kind in {"artifacts", "artifact_package"}:
        ids = payload.get("artifactIds")
        artifact_ids = [str(item) for item in ids] if isinstance(ids, list) else []
        return export_artifacts(project_id, artifact_ids=artifact_ids, export_format=export_format)
    if kind in {"evidence", "trace_eval", "trace-eval"}:
        return export_evidence(payload, project_id=project_id, export_format=export_format)
    raise AppError("Unsupported export kind", code=ErrorCode.INVALID_PAYLOAD, status=400)


def export_project(project_id: str, *, export_format: str = "zip") -> dict[str, Any]:
    safe_project_id = validate_project_id(project_id)
    project = project_store.get_project(safe_project_id)
    export_format = normalize_export_format(export_format)
    if export_format == "zip":
        return _write_export(
            safe_project_id,
            "project",
            "zip",
            f"{safe_filename(project['name'], 'project')}-project-export.zip",
            lambda path: _write_project_zip(path, safe_project_id),
        )
    bundle = project_bundle(safe_project_id)
    if export_format == "json":
        content = json.dumps(redact_value(bundle), ensure_ascii=False, indent=2).encode("utf-8")
        return _write_export(safe_project_id, "project", "json", f"{safe_filename(project['name'], 'project')}-project-export.json", lambda _: content)
    markdown = project_markdown(bundle)
    if export_format == "html":
        return _write_export(safe_project_id, "project", "html", f"{safe_filename(project['name'], 'project')}-project-export.html", lambda _: markdown_to_html(markdown).encode("utf-8"))
    return _write_export(safe_project_id, "project", "markdown", f"{safe_filename(project['name'], 'project')}-project-export.md", lambda _: markdown.encode("utf-8"))


def export_conversation(conversation: dict[str, Any], *, project_id: str = "", export_format: str = "markdown") -> dict[str, Any]:
    safe_project_id = validate_project_id(project_id) if project_id else ""
    normalized = project_store.normalize_conversation(conversation)
    export_format = normalize_export_format(export_format)
    filename_base = safe_filename(str(normalized.get("title") or "conversation"), "conversation")
    if export_format == "json":
        content = json.dumps(redact_value({"schemaVersion": EXPORT_SCHEMA_VERSION, "conversation": normalized}), ensure_ascii=False, indent=2).encode("utf-8")
        return _write_export(safe_project_id, "conversation", "json", f"{filename_base}.json", lambda _: content)
    markdown = conversation_markdown(normalized)
    if export_format == "html":
        return _write_export(safe_project_id, "conversation", "html", f"{filename_base}.html", lambda _: markdown_to_html(markdown).encode("utf-8"))
    if export_format == "zip":
        def writer(path: Path) -> bytes:
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("metadata.json", json.dumps(redact_value({"schemaVersion": EXPORT_SCHEMA_VERSION, "kind": "conversation", "conversationId": normalized["conversationId"]}), ensure_ascii=False, indent=2))
                archive.writestr(f"conversations/{filename_base}.md", markdown)
            return b""

        return _write_export(safe_project_id, "conversation", "zip", f"{filename_base}.zip", writer)
    return _write_export(safe_project_id, "conversation", "markdown", f"{filename_base}.md", lambda _: markdown.encode("utf-8"))


def export_saved_items(project_id: str, *, saved_ids: list[str] | None = None, export_format: str = "json") -> dict[str, Any]:
    safe_project_id = validate_project_id(project_id)
    export_format = normalize_export_format(export_format)
    ids = set(saved_ids or [])
    items = saved_item_store.list_saved_items(safe_project_id)
    if ids:
        items = [item for item in items if str(item.get("savedId") or "") in ids]
    filename_base = "saved-items"
    if export_format == "zip":
        def writer(path: Path) -> bytes:
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("saved-items/saved-items.json", json.dumps(redact_value({"items": items}), ensure_ascii=False, indent=2))
                archive.writestr("saved-items/saved-items.md", saved_items_markdown(items))
            return b""

        return _write_export(safe_project_id, "saved_items", "zip", f"{filename_base}.zip", writer)
    if export_format == "html":
        return _write_export(safe_project_id, "saved_items", "html", f"{filename_base}.html", lambda _: markdown_to_html(saved_items_markdown(items)).encode("utf-8"))
    if export_format == "markdown":
        return _write_export(safe_project_id, "saved_items", "markdown", f"{filename_base}.md", lambda _: saved_items_markdown(items).encode("utf-8"))
    content = json.dumps(redact_value({"schemaVersion": EXPORT_SCHEMA_VERSION, "items": items}), ensure_ascii=False, indent=2).encode("utf-8")
    return _write_export(safe_project_id, "saved_items", "json", f"{filename_base}.json", lambda _: content)


def export_artifacts(project_id: str, *, artifact_ids: list[str] | None = None, export_format: str = "zip") -> dict[str, Any]:
    safe_project_id = validate_project_id(project_id)
    export_format = normalize_export_format(export_format)
    ids = set(artifact_ids or [])
    artifacts = artifact_store.list_artifacts(safe_project_id)
    if ids:
        artifacts = [artifact for artifact in artifacts if str(artifact.get("artifactId") or "") in ids]
    if export_format == "json":
        content = json.dumps(redact_value({"schemaVersion": EXPORT_SCHEMA_VERSION, "artifacts": artifacts}), ensure_ascii=False, indent=2).encode("utf-8")
        return _write_export(safe_project_id, "artifacts", "json", "artifacts.json", lambda _: content)
    if export_format in {"markdown", "html"}:
        markdown = artifacts_markdown(artifacts)
        if export_format == "html":
            return _write_export(safe_project_id, "artifacts", "html", "artifacts.html", lambda _: markdown_to_html(markdown).encode("utf-8"))
        return _write_export(safe_project_id, "artifacts", "markdown", "artifacts.md", lambda _: markdown.encode("utf-8"))

    def writer(path: Path) -> bytes:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("metadata.json", json.dumps(redact_value({"schemaVersion": EXPORT_SCHEMA_VERSION, "kind": "artifacts", "artifacts": artifacts}), ensure_ascii=False, indent=2))
            write_artifacts_to_zip(archive, artifacts)
        return b""

    return _write_export(safe_project_id, "artifacts", "zip", "artifacts.zip", writer)


def export_evidence(payload: dict[str, Any], *, project_id: str = "", export_format: str = "zip") -> dict[str, Any]:
    safe_project_id = validate_project_id(project_id) if project_id else ""
    export_format = normalize_export_format(export_format)
    evidence: dict[str, Any] = {
        "schemaVersion": EXPORT_SCHEMA_VERSION,
        "kind": "evidence",
        "traces": payload.get("traces") if isinstance(payload.get("traces"), list) else [],
        "evals": payload.get("evals") if isinstance(payload.get("evals"), list) else [],
        "sourceRef": normalize_source_ref(payload.get("sourceRef")),
        "exportedAt": utc_now(),
    }
    if export_format == "zip":
        def writer(path: Path) -> bytes:
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("metadata.json", json.dumps(redact_value(evidence), ensure_ascii=False, indent=2))
                archive.writestr("traces/traces.json", json.dumps(redact_value(evidence["traces"]), ensure_ascii=False, indent=2))
                archive.writestr("evals/evals.json", json.dumps(redact_value(evidence["evals"]), ensure_ascii=False, indent=2))
            return b""

        return _write_export(safe_project_id, "evidence", "zip", "workspace-evidence.zip", writer)
    content = json.dumps(redact_value(evidence), ensure_ascii=False, indent=2).encode("utf-8")
    if export_format == "html":
        return _write_export(safe_project_id, "evidence", "html", "workspace-evidence.html", lambda _: markdown_to_html("```json\n" + content.decode("utf-8") + "\n```").encode("utf-8"))
    if export_format == "markdown":
        return _write_export(safe_project_id, "evidence", "markdown", "workspace-evidence.md", lambda _: ("```json\n" + content.decode("utf-8") + "\n```").encode("utf-8"))
    return _write_export(safe_project_id, "evidence", "json", "workspace-evidence.json", lambda _: content)


def project_bundle(project_id: str) -> dict[str, Any]:
    safe_project_id = validate_project_id(project_id)
    project = project_store.get_project(safe_project_id)
    saved_items = saved_item_store.list_saved_items(safe_project_id)
    artifacts = artifact_store.list_artifacts(safe_project_id)
    media_items = media_store.list_media(project_id=safe_project_id)
    return {
        "schemaVersion": EXPORT_SCHEMA_VERSION,
        "kind": "project",
        "metadata": project,
        "conversations": project.get("conversations", []),
        "savedItems": saved_items,
        "artifacts": artifacts,
        "media": media_items,
        "exportedAt": utc_now(),
    }


def resolve_export(export_id: str, *, project_id: str = "") -> dict[str, Any]:
    safe_export_id = validate_workspace_id(export_id, label="export id")
    project_ids = [validate_project_id(project_id)] if project_id else [""] + [str(item.get("id") or "") for item in legacy_projects.list_projects()]
    for candidate_project_id in project_ids:
        for export in _load_exports(candidate_project_id):
            if export.get("exportId") == safe_export_id:
                return export
    raise AppError("Export not found", code=ErrorCode.NOT_FOUND, status=404)


def list_exports(project_id: str = "") -> list[dict[str, Any]]:
    if project_id:
        safe_project_id = validate_project_id(project_id)
        return sorted(_load_exports(safe_project_id), key=lambda item: str(item.get("createdAt") or ""), reverse=True)
    items: list[dict[str, Any]] = []
    items.extend(_load_exports(""))
    for project in legacy_projects.list_projects():
        candidate_project_id = str(project.get("id") or "")
        if candidate_project_id:
            items.extend(_load_exports(candidate_project_id))
    return sorted(items, key=lambda item: str(item.get("createdAt") or ""), reverse=True)


def export_path(export: dict[str, Any]) -> Path:
    raw_path = str(export.get("path") or "")
    path = Path(raw_path)
    if not path.is_absolute():
        project_id = str(export.get("projectId") or "")
        path = _export_dir(project_id) / raw_path
    return path.resolve()


def project_markdown(bundle: dict[str, Any]) -> str:
    raw_metadata = bundle.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    title = normalize_title(metadata.get("name"), default="Project")
    lines = [
        f"# {redact_sensitive_text(title)}",
        "",
        f"- Project ID: `{metadata.get('projectId') or metadata.get('id') or ''}`",
        f"- Exported: `{bundle.get('exportedAt') or utc_now()}`",
        "",
    ]
    description = str(metadata.get("description") or "").strip()
    if description:
        lines += ["## Description", "", redact_sensitive_text(description), ""]
    conversations = bundle.get("conversations")
    if isinstance(conversations, list) and conversations:
        lines += ["## Conversations", ""]
        for conversation in conversations:
            if isinstance(conversation, dict):
                lines += [f"- [{redact_sensitive_text(str(conversation.get('title') or 'Conversation'))}](conversations/conversation-{safe_filename(str(conversation.get('conversationId') or conversation.get('id') or 'conversation'))}.md)"]
        lines.append("")
    saved_items = bundle.get("savedItems")
    if isinstance(saved_items, list) and saved_items:
        lines += ["## Saved Items", "", saved_items_markdown(saved_items), ""]
    artifacts = bundle.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        lines += ["## Artifacts", "", artifacts_markdown(artifacts), ""]
    media_items = bundle.get("media")
    if isinstance(media_items, list) and media_items:
        lines += ["## Media", "", media_markdown(media_items), ""]
    return "\n".join(lines).strip() + "\n"


def conversation_markdown(conversation: dict[str, Any]) -> str:
    title = redact_sensitive_text(str(conversation.get("title") or "Conversation"))
    lines = [f"# {title}", "", f"- Conversation ID: `{conversation.get('conversationId') or conversation.get('id') or ''}`", ""]
    source_ref = conversation.get("sourceRef")
    if isinstance(source_ref, dict) and source_ref:
        lines += ["## Source", "", "```json", json.dumps(redact_value(source_ref), ensure_ascii=False, indent=2), "```", ""]
    messages = conversation.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "message").strip().title()
            content = redact_sensitive_text(str(message.get("content") or ""))
            lines += [f"## {role}", "", content or "_No content_", ""]
            msg_source = message.get("sourceRef")
            if isinstance(msg_source, dict) and msg_source:
                lines += ["```json", json.dumps(redact_value(msg_source), ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(lines).strip() + "\n"


def saved_items_markdown(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in items:
        title = redact_sensitive_text(str(item.get("title") or "Saved item"))
        lines += [f"### {title}", "", f"- Type: `{item.get('type') or ''}`", f"- Saved ID: `{item.get('savedId') or ''}`"]
        tags = item.get("tags")
        if isinstance(tags, list) and tags:
            lines.append("- Tags: " + ", ".join(f"`{redact_sensitive_text(str(tag))}`" for tag in tags))
        source_ref = item.get("sourceRef")
        if isinstance(source_ref, dict) and source_ref:
            lines += ["- Source: `" + redact_sensitive_text(json.dumps(source_ref, ensure_ascii=False, sort_keys=True)) + "`"]
        lines += ["", redact_sensitive_text(str(item.get("content") or "")), ""]
    return "\n".join(lines).strip() + ("\n" if lines else "")


def artifacts_markdown(artifacts: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for artifact in artifacts:
        filename = artifact_store.artifact_filename(artifact)
        lines.append(
            f"- [{redact_sensitive_text(str(artifact.get('title') or filename))}](artifacts/{filename}) "
            f"`{artifact.get('type') or ''}` v{artifact.get('version') or 1} source=`{redact_sensitive_text(json.dumps(artifact.get('source') or {}, ensure_ascii=False, sort_keys=True))}`"
        )
    return "\n".join(lines) + ("\n" if lines else "")


def media_markdown(media_items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for media in media_items:
        citation_hint = ""
        segments = media_store.list_segments(str(media.get("mediaId") or ""))
        for segment in segments:
            raw_citation = segment.get("citation")
            citation: dict[str, Any] = raw_citation if isinstance(raw_citation, dict) else {}
            if citation.get("markdown"):
                citation_hint = f" citation=`{citation.get('markdown')}`"
                break
        lines.append(
            f"- {redact_sensitive_text(str(media.get('title') or 'Media'))} "
            f"`{media.get('type') or ''}` id=`{media.get('mediaId') or ''}` status=`{media.get('status') or ''}`{citation_hint}"
        )
    return "\n".join(lines) + ("\n" if lines else "")


def markdown_to_html(markdown: str) -> str:
    escaped = html.escape(redact_sensitive_text(markdown))
    return "<!doctype html><meta charset=\"utf-8\"><title>Workspace Export</title><pre>" + escaped + "</pre>\n"


def _write_project_zip(path: Path, project_id: str) -> bytes:
    bundle = project_bundle(project_id)
    raw_metadata = bundle.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    raw_conversations = bundle.get("conversations")
    conversations = raw_conversations if isinstance(raw_conversations, list) else []
    raw_artifacts = bundle.get("artifacts")
    artifacts = raw_artifacts if isinstance(raw_artifacts, list) else []
    raw_media = bundle.get("media")
    media_items = raw_media if isinstance(raw_media, list) else []
    raw_saved_items = bundle.get("savedItems")
    saved_items = raw_saved_items if isinstance(raw_saved_items, list) else []
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("metadata.json", json.dumps(redact_value(metadata), ensure_ascii=False, indent=2))
        archive.writestr("saved-items/saved-items.json", json.dumps(redact_value({"items": bundle.get("savedItems") or []}), ensure_ascii=False, indent=2))
        archive.writestr("project.md", project_markdown(bundle))
        for conversation in conversations:
            if isinstance(conversation, dict):
                filename = safe_filename(str(conversation.get("conversationId") or conversation.get("id") or "conversation"), "conversation")
                archive.writestr(f"conversations/conversation-{filename}.md", conversation_markdown(conversation))
        write_project_files_to_zip(archive, project_id, metadata)
        write_artifacts_to_zip(archive, artifacts)
        write_media_to_zip(archive, media_items)
        write_trace_items_to_zip(archive, saved_items)
    return b""


def write_project_files_to_zip(archive: zipfile.ZipFile, project_id: str, metadata: dict[str, Any]) -> None:
    files = metadata.get("files") or metadata.get("documents")
    if not isinstance(files, list):
        return
    for document in files:
        if not isinstance(document, dict):
            continue
        file_id = str(document.get("fileId") or "")
        if not file_id:
            continue
        try:
            cached = load_cached_file(file_id, project_id=project_id)
        except AppError:
            continue
        chunks = cached.get("chunks")
        text = "\n\n".join(str(chunk.get("text") or "") for chunk in chunks if isinstance(chunk, dict)) if isinstance(chunks, list) else ""
        if not text:
            text = str(cached.get("preview") or "")
        filename = safe_filename(str(cached.get("name") or file_id), "source-file") + ".txt"
        archive.writestr(f"files/source-files/{filename}", redact_sensitive_text(text))


def write_artifacts_to_zip(archive: zipfile.ZipFile, artifacts: list[Any]) -> None:
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        try:
            path = artifact_store.artifact_path(artifact)
        except AppError:
            continue
        if not path.is_file():
            continue
        filename = artifact_store.artifact_filename(artifact)
        data = path.read_bytes()
        artifact_type = str(artifact.get("type") or "").lower()
        if artifact_type in artifact_store.TEXT_PREVIEW_TYPES or contains_secret(data):
            archive.writestr(f"artifacts/{filename}", redact_sensitive_text(data.decode("utf-8", errors="replace")))
        else:
            archive.writestr(f"artifacts/{filename}", data)


def write_media_to_zip(archive: zipfile.ZipFile, media_items: list[Any]) -> None:
    normalized = [media_store.redacted_media_payload(item) for item in media_items if isinstance(item, dict)]
    archive.writestr("media/media.json", json.dumps(redact_value({"items": normalized}), ensure_ascii=False, indent=2))
    for media in media_items:
        if not isinstance(media, dict):
            continue
        media_id = str(media.get("mediaId") or "")
        if not media_id:
            continue
        segments = media_store.list_segments(media_id)
        archive.writestr(
            f"media/segments/{safe_filename(media_id, 'media')}.json",
            json.dumps(redact_value({"segments": segments}), ensure_ascii=False, indent=2),
        )
        try:
            path = media_store.media_file_path(media)
        except AppError:
            continue
        if not path.is_file():
            continue
        filename = safe_filename(str(media.get("title") or path.name), "media")
        suffix = path.suffix.lower()
        if suffix and not filename.lower().endswith(suffix):
            filename += suffix
        data = path.read_bytes()
        if contains_secret(data) or suffix in {".txt", ".md", ".html", ".htm", ".json", ".csv", ".xml"}:
            archive.writestr(f"media/source/{filename}", redact_sensitive_text(data.decode("utf-8", errors="replace")))
        else:
            archive.writestr(f"media/source/{filename}", data)


def write_trace_items_to_zip(archive: zipfile.ZipFile, items: list[Any]) -> None:
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "trace":
            continue
        name = safe_filename(str(item.get("savedId") or "trace"), "trace")
        archive.writestr(f"traces/trace-{name}.json", json.dumps(redact_value(item), ensure_ascii=False, indent=2))


def _write_export(project_id: str, kind: str, export_format: str, filename: str, writer: Any) -> dict[str, Any]:
    safe_project_id = validate_project_id(project_id) if project_id else ""
    export_id = new_id("export")
    export_dir = _export_dir(safe_project_id)
    export_dir.mkdir(parents=True, exist_ok=True)
    target = export_dir / f"{export_id}-{safe_filename(filename, 'workspace-export')}"
    result = writer(target)
    if isinstance(result, bytes) and result:
        target.write_bytes(result)
    export = {
        "exportId": export_id,
        "projectId": safe_project_id,
        "kind": kind,
        "format": export_format,
        "filename": filename,
        "path": str(target),
        "size": target.stat().st_size,
        "includes": _export_includes(safe_project_id, kind),
        "createdAt": timestamp_ms_to_iso(now_ms()),
        "downloadUrl": f"/api/workspace/exports/{export_id}/download" + (f"projectId={safe_project_id}" if safe_project_id else ""),
    }
    _record_export(safe_project_id, export)
    return {"ok": True, "export": export}


def _export_dir(project_id: str) -> Path:
    if project_id:
        return legacy_projects.PROJECTS_DIR / validate_project_id(project_id) / "exports"
    return config.GENERATED_DIR / "workspace-exports"


def _export_store_path(project_id: str) -> Path:
    return _export_dir(project_id) / STORE_NAME


def _load_exports(project_id: str) -> list[dict[str, Any]]:
    data = read_json_file(_export_store_path(project_id), default={"exports": []})
    exports = data.get("exports")
    return [item for item in exports if isinstance(item, dict)] if isinstance(exports, list) else []


def _record_export(project_id: str, export: dict[str, Any]) -> None:
    exports = _load_exports(project_id)
    exports.append(export)
    write_json_atomic(_export_store_path(project_id), {"exports": exports[-100:]})


def _export_includes(project_id: str, kind: str) -> dict[str, Any]:
    includes: dict[str, Any] = {"projectId": project_id} if project_id else {}
    if not project_id:
        return includes
    try:
        bundle = project_bundle(project_id)
    except Exception:
        return includes
    if kind == "project":
        includes.update(
            {
                "conversationIds": [str(item.get("conversationId") or item.get("id") or "") for item in bundle.get("conversations", []) if isinstance(item, dict)],
                "savedItemIds": [str(item.get("savedId") or "") for item in bundle.get("savedItems", []) if isinstance(item, dict)],
                "artifactIds": [str(item.get("artifactId") or "") for item in bundle.get("artifacts", []) if isinstance(item, dict)],
                "mediaIds": [str(item.get("mediaId") or "") for item in bundle.get("media", []) if isinstance(item, dict)],
            }
        )
    elif kind == "saved_items":
        includes["savedItemIds"] = [str(item.get("savedId") or "") for item in bundle.get("savedItems", []) if isinstance(item, dict)]
    elif kind == "artifacts":
        includes["artifactIds"] = [str(item.get("artifactId") or "") for item in bundle.get("artifacts", []) if isinstance(item, dict)]
    return includes


def evidence_metadata(version: str, *, status: str, checks: dict[str, str]) -> dict[str, Any]:
    return {
        "version": version,
        "commit": git_short_sha(),
        "generatedAt": utc_now(),
        "environment": {"os": platform.platform(), "python": platform.python_version(), "ci": bool(os.environ.get("CI"))},
        "status": status,
        "checks": checks,
    }


def git_short_sha() -> str:
    result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).resolve().parents[3], check=False, capture_output=True, text=True)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unknown"

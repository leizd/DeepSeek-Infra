"""Workspace Provenance Graph built from local metadata links."""

from __future__ import annotations

from typing import Any

from deepseek_infra.infra.automation import history as automation_history
from deepseek_infra.infra.automation import registry as automation_registry
from deepseek_infra.infra.media import library as media_library
from deepseek_infra.infra.memory import store as memory_store
from deepseek_infra.infra.workspace import artifacts as artifact_store
from deepseek_infra.infra.workspace import exports as export_store
from deepseek_infra.infra.workspace import projects as project_store
from deepseek_infra.infra.workspace import saved_items as saved_item_store
from deepseek_infra.infra.workspace.schema import validate_project_id


def project_provenance(project_id: str) -> dict[str, Any]:
    safe_project_id = validate_project_id(project_id)
    project = project_store.get_project(safe_project_id)
    graph = _Graph()
    project_node = graph.add_node("project", safe_project_id, _title(project, "Project"), project)

    for conversation in project.get("conversations", []):
        if not isinstance(conversation, dict):
            continue
        conversation_id = str(conversation.get("conversationId") or conversation.get("id") or "")
        if not conversation_id:
            continue
        conversation_node = graph.add_node("conversation", conversation_id, _title(conversation, "Conversation"), conversation)
        graph.add_edge(project_node, conversation_node, "contains")
        graph.add_source_edge(conversation.get("sourceRef"), conversation_node, "sourced_from")
        for message in conversation.get("messages", []) if isinstance(conversation.get("messages"), list) else []:
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("id") or "")
            if not message_id:
                continue
            message_node = graph.add_node("message", message_id, str(message.get("role") or "message"), message)
            graph.add_edge(conversation_node, message_node, "contains")
            graph.add_source_edge(message.get("sourceRef"), message_node, "sourced_from")

    for memory in memory_store.list_memories(scope="project", project_id=safe_project_id):
        memory_id = str(memory.get("memoryId") or memory.get("id") or "")
        if not memory_id:
            continue
        memory_node = graph.add_node("memory", memory_id, str(memory.get("type") or "memory"), memory)
        graph.add_edge(project_node, memory_node, "contains")
        graph.add_source_edge(memory.get("source"), memory_node, "sourced_from")

    for media in media_library.list_media(project_id=safe_project_id):
        media_id = str(media.get("mediaId") or "")
        if not media_id:
            continue
        media_node = graph.add_node("media", media_id, _title(media, "Media"), media)
        graph.add_edge(project_node, media_node, "contains")
        graph.add_source_edge(media.get("source"), media_node, "sourced_from")

    for saved in saved_item_store.list_saved_items(safe_project_id):
        saved_id = str(saved.get("savedId") or "")
        if not saved_id:
            continue
        saved_node = graph.add_node("saved_item", saved_id, _title(saved, "Saved item"), saved)
        graph.add_edge(project_node, saved_node, "contains")
        graph.add_source_edge(saved.get("sourceRef"), saved_node, "sourced_from")

    for artifact in artifact_store.list_artifacts(safe_project_id):
        artifact_id = str(artifact.get("artifactId") or "")
        if not artifact_id:
            continue
        artifact_node = graph.add_node("artifact", artifact_id, _title(artifact, "Artifact"), artifact)
        graph.add_edge(project_node, artifact_node, "contains")
        graph.add_source_edge(artifact.get("source"), artifact_node, "sourced_from")

    for automation in automation_registry.list_automations(project_id=safe_project_id):
        automation_id = str(automation.get("automationId") or "")
        if not automation_id:
            continue
        automation_node = graph.add_node("automation", automation_id, _title(automation, "Automation"), automation)
        graph.add_edge(project_node, automation_node, "contains")

    for run in automation_history.list_runs(project_id=safe_project_id, limit=0):
        run_id = str(run.get("runId") or "")
        if not run_id:
            continue
        run_node = graph.add_node("automation_run", run_id, str(run.get("status") or "run"), run)
        automation_id = str(run.get("automationId") or "")
        automation_node = graph.node_id("automation", automation_id) if automation_id else project_node
        graph.add_edge(automation_node, run_node, "ran")
        raw_outputs = run.get("outputs")
        outputs: dict[str, Any] = raw_outputs if isinstance(raw_outputs, dict) else {}
        for field, object_type in (
            ("artifactIds", "artifact"),
            ("savedItemIds", "saved_item"),
            ("mediaIds", "media"),
            ("exportIds", "export"),
        ):
            output_ids = outputs.get(field)
            if not isinstance(output_ids, list):
                continue
            for object_id in output_ids:
                target = graph.add_node(object_type, str(object_id), object_type.replace("_", " ").title(), {"id": str(object_id)})
                graph.add_edge(run_node, target, "produced")

    for export in export_store.list_exports(safe_project_id):
        export_id = str(export.get("exportId") or "")
        if not export_id:
            continue
        export_node = graph.add_node("export", export_id, str(export.get("filename") or "Export"), export)
        graph.add_edge(project_node, export_node, "contains")
        raw_includes = export.get("includes")
        includes: dict[str, Any] = raw_includes if isinstance(raw_includes, dict) else {}
        for field, object_type in (
            ("conversationIds", "conversation"),
            ("savedItemIds", "saved_item"),
            ("artifactIds", "artifact"),
            ("mediaIds", "media"),
        ):
            included_ids = includes.get(field)
            if not isinstance(included_ids, list):
                continue
            for object_id in included_ids:
                target = graph.add_node(object_type, str(object_id), object_type.replace("_", " ").title(), {"id": str(object_id)})
                graph.add_edge(export_node, target, "includes")

    return {
        "ok": True,
        "projectId": safe_project_id,
        "nodes": graph.nodes,
        "edges": graph.edges,
        "summary": {
            "nodes": len(graph.nodes),
            "edges": len(graph.edges),
            "types": graph.type_counts(),
        },
    }


class _Graph:
    def __init__(self) -> None:
        self.nodes: list[dict[str, Any]] = []
        self.edges: list[dict[str, str]] = []
        self._node_ids: set[str] = set()
        self._edge_ids: set[tuple[str, str, str]] = set()

    def node_id(self, kind: str, object_id: str) -> str:
        return f"{kind}:{object_id}"

    def add_node(self, kind: str, object_id: str, title: str, data: dict[str, Any]) -> str:
        node_id = self.node_id(kind, object_id)
        if node_id not in self._node_ids:
            self._node_ids.add(node_id)
            self.nodes.append(
                {
                    "id": node_id,
                    "type": kind,
                    "objectId": object_id,
                    "title": title,
                    "source": _compact_source(data),
                }
            )
        return node_id

    def add_edge(self, source: str, target: str, relation: str) -> None:
        if not source or not target or source == target:
            return
        edge_id = (source, target, relation)
        if edge_id in self._edge_ids:
            return
        self._edge_ids.add(edge_id)
        self.edges.append({"source": source, "target": target, "relation": relation})

    def add_source_edge(self, source_ref: Any, target: str, relation: str) -> None:
        if not isinstance(source_ref, dict) or not source_ref:
            return
        for source_node in self._source_nodes(source_ref):
            self.add_edge(source_node, target, relation)

    def type_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for node in self.nodes:
            node_type = str(node.get("type") or "unknown")
            counts[node_type] = counts.get(node_type, 0) + 1
        return counts

    def _source_nodes(self, source_ref: dict[str, Any]) -> list[str]:
        nodes: list[str] = []
        for key, kind in (
            ("projectId", "project"),
            ("conversationId", "conversation"),
            ("messageId", "message"),
            ("savedId", "saved_item"),
            ("savedItemId", "saved_item"),
            ("artifactId", "artifact"),
            ("mediaId", "media"),
            ("automationId", "automation"),
            ("runId", "automation_run"),
            ("automationRunId", "automation_run"),
            ("skillRunId", "skill_run"),
            ("exportId", "export"),
        ):
            value = str(source_ref.get(key) or "").strip()
            if value:
                nodes.append(self.add_node(kind, value, kind.replace("_", " ").title(), source_ref))
        ref_id = str(source_ref.get("refId") or "").strip()
        if ref_id:
            kind = str(source_ref.get("kind") or "source").strip().lower() or "source"
            nodes.append(self.add_node(f"source_{kind}", ref_id, kind.replace("_", " ").title(), source_ref))
        return nodes


def _title(item: dict[str, Any], default: str) -> str:
    return str(item.get("title") or item.get("name") or default)


def _compact_source(item: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("createdAt", "updatedAt", "status", "kind", "format", "type", "source", "sourceRef", "path", "downloadUrl"):
        if key in item:
            result[key] = item.get(key)
    return result

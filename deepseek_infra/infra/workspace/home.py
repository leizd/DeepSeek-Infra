"""Unified Workspace Home aggregation for the Personal AI Runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepseek_infra.core.config import APP_VERSION, ROOT
from deepseek_infra.infra.automation import history as automation_history
from deepseek_infra.infra.automation import registry as automation_registry
from deepseek_infra.infra.data import projects as legacy_projects
from deepseek_infra.infra.media import library as media_library
from deepseek_infra.infra.memory import store as memory_store
from deepseek_infra.infra.workspace import artifacts as artifact_store
from deepseek_infra.infra.workspace import exports as export_store
from deepseek_infra.infra.workspace import projects as project_store
from deepseek_infra.infra.workspace import saved_items as saved_item_store

MODULES: tuple[dict[str, str], ...] = (
    {"id": "projects", "label": "Projects", "status": "ready"},
    {"id": "memory", "label": "Memory", "status": "ready"},
    {"id": "skills", "label": "Skills", "status": "ready"},
    {"id": "media", "label": "Media", "status": "ready"},
    {"id": "browser", "label": "Browser", "status": "ready"},
    {"id": "automations", "label": "Automations", "status": "ready"},
    {"id": "artifacts", "label": "Artifacts", "status": "ready"},
    {"id": "saved_items", "label": "Saved Items", "status": "ready"},
    {"id": "exports", "label": "Exports", "status": "ready"},
    {"id": "settings", "label": "Settings", "status": "ready"},
)


def workspace_home(*, limit: int = 8) -> dict[str, Any]:
    safe_limit = max(1, min(int(limit or 8), 50))
    projects = project_store.list_projects()
    project_ids = [str(item.get("projectId") or item.get("id") or "") for item in projects]
    saved_items = _collect_project_items(project_ids, saved_item_store.list_saved_items)
    artifacts = _collect_project_items(project_ids, artifact_store.list_artifacts)
    skill_runs = _collect_skill_runs(project_ids)
    automations = automation_registry.list_automations()
    automation_runs = automation_history.list_runs(limit=safe_limit)
    media = media_library.list_media()
    exports = export_store.list_exports()
    memories = memory_store.list_memories()
    return {
        "ok": True,
        "version": APP_VERSION,
        "modules": [dict(item) for item in MODULES],
        "recent": {
            "projects": _take(projects, safe_limit),
            "memories": _take(memories, safe_limit),
            "skills": _take(skill_runs, safe_limit),
            "media": _take(media, safe_limit),
            "automations": _take(automation_runs, safe_limit),
            "artifacts": _take(artifacts, safe_limit),
            "savedItems": _take(saved_items, safe_limit),
            "exports": _take(exports, safe_limit),
        },
        "counts": {
            "projects": len(projects),
            "memories": len(memories),
            "skills": len(skill_runs),
            "media": len(media),
            "automations": len(automations),
            "automationRuns": len(automation_runs),
            "artifacts": len(artifacts),
            "savedItems": len(saved_items),
            "exports": len(exports),
        },
        "status": {
            "doctor": "ok",
            "evidence": _ga_evidence_status(),
            "runtime": "local",
        },
    }


def _collect_project_items(project_ids: list[str], loader: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for project_id in project_ids:
        if not project_id:
            continue
        try:
            items.extend(loader(project_id))
        except Exception:
            continue
    return sorted(items, key=_sort_timestamp, reverse=True)


def _collect_skill_runs(project_ids: list[str]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for project_id in project_ids:
        if not project_id:
            continue
        try:
            runs.extend(legacy_projects.list_project_skill_runs(project_id, limit=50))
        except Exception:
            continue
    return sorted(runs, key=_sort_timestamp, reverse=True)


def _take(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return items[:limit]


def _sort_timestamp(item: dict[str, Any]) -> str:
    return str(item.get("updatedAt") or item.get("createdAt") or item.get("finishedAt") or item.get("startedAt") or "")


def _ga_evidence_status() -> dict[str, Any]:
    path = Path(ROOT) / "docs" / "evidence" / f"ga-v{APP_VERSION}.json"
    return {
        "path": path.as_posix(),
        "present": path.exists(),
        "status": "present" if path.exists() else "missing",
    }

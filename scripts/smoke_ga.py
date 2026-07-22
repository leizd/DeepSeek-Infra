#!/usr/bin/env python3
"""Offline GA smoke for the v3.0 Personal AI Runtime."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.infra.diagnostics.evidence_revision import evidence_revision  # noqa: E402


def app_version() -> str:
    from deepseek_infra.core.config import settings

    return settings.app_version


def git_short_sha() -> str:
    result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, check=False, capture_output=True, text=True)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unknown"


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def configure_runtime_root(root: Path) -> None:
    from deepseek_infra.core import config
    from deepseek_infra.infra.automation import history as automation_history
    from deepseek_infra.infra.automation import registry as automation_registry
    from deepseek_infra.infra.data import memory as legacy_memory
    from deepseek_infra.infra.data import projects as legacy_projects
    from deepseek_infra.infra.media import library as media_library
    from deepseek_infra.infra.observability import observability
    from deepseek_infra.infra.rag import files, local_rag
    from deepseek_infra.infra.skills import evidence as skill_evidence
    from deepseek_infra.infra.skills import registry as skill_registry
    from deepseek_infra.infra.tool_runtime import generated_files
    from deepseek_infra.infra.workspace import home as workspace_home

    projects_dir = root / ".projects"
    generated_dir = root / ".generated"
    memory_dir = root / ".memory"
    media_dir = root / ".media"
    automation_dir = root / ".automation"
    local_rag_dir = root / ".local-rag"
    traces_dir = root / ".traces"
    skills_dir = root / ".skills"

    config.ROOT = root
    config.PROJECTS_DIR = projects_dir
    config.GENERATED_DIR = generated_dir
    config.MEMORY_DIR = memory_dir
    config.MEMORY_FILE = memory_dir / "memories.json"
    config.MEDIA_DIR = media_dir
    config.AUTOMATION_DIR = automation_dir
    config.LOCAL_RAG_DIR = local_rag_dir
    config.LOCAL_RAG_DB = local_rag_dir / "rag.sqlite3"
    config.TRACE_DIR = traces_dir
    config.TRACE_DB = traces_dir / "traces.sqlite3"
    config.SKILLS_DIR = skills_dir

    legacy_projects.PROJECTS_DIR = projects_dir
    legacy_memory.MEMORY_DIR = memory_dir
    legacy_memory.MEMORY_FILE = memory_dir / "memories.json"
    media_library.MEDIA_DIR = media_dir
    automation_registry.AUTOMATION_DIR = automation_dir
    automation_history.AUTOMATION_DIR = automation_dir
    local_rag.PROJECTS_DIR = projects_dir
    local_rag.MEMORY_FILE = memory_dir / "memories.json"
    local_rag.LOCAL_RAG_DIR = local_rag_dir
    local_rag.LOCAL_RAG_DB = local_rag_dir / "rag.sqlite3"
    files.PROJECTS_DIR = projects_dir
    generated_files.GENERATED_DIR = generated_dir
    skill_evidence.GENERATED_DIR = generated_dir
    skill_registry.SKILLS_DIR = skills_dir
    observability.TRACE_DIR = traces_dir
    observability.TRACE_DB = traces_dir / "traces.sqlite3"
    workspace_home.ROOT = root


def run_ga_smoke(root: Path) -> tuple[dict[str, str], dict[str, Any]]:
    from deepseek_infra.infra.automation import registry as automation_registry
    from deepseek_infra.infra.automation import runner as automation_runner
    from deepseek_infra.infra.media import library as media_library
    from deepseek_infra.infra.memory import search as memory_search
    from deepseek_infra.infra.memory import store as memory_store
    from deepseek_infra.infra.skills.runner import run_skill
    from deepseek_infra.infra.workspace import artifacts, exports, home, projects, provenance, saved_items

    checks: dict[str, str] = {
        "workspaceHome": "FAIL",
        "project": "FAIL",
        "memory": "FAIL",
        "skill": "FAIL",
        "media": "FAIL",
        "browserSnapshot": "FAIL",
        "savedItem": "FAIL",
        "artifact": "FAIL",
        "automation": "FAIL",
        "export": "FAIL",
        "provenance": "FAIL",
        "exportRedaction": "FAIL",
    }
    details: dict[str, Any] = {"runtimeRoot": str(root)}

    project = projects.create_project("GA Personal Runtime", description="v3.0 local-first runtime smoke")
    project_id = str(project["projectId"])
    checks["project"] = "PASS" if project_id else "FAIL"
    projects.upsert_project_conversation(
        project_id,
        {
            "conversationId": "conv-ga-smoke",
            "title": "GA smoke conversation",
            "messages": [{"id": "msg-ga-smoke", "role": "user", "content": "Assemble local runtime evidence."}],
        },
    )

    memory = memory_store.add_memory(
        "GA runtime keeps project evidence, media, artifacts and exports local.",
        scope="project",
        project_id=project_id,
        memory_type="summary",
        source={"kind": "chat", "refId": "msg-ga-smoke", "messageId": "msg-ga-smoke"},
        confidence=0.91,
    )
    memory_hits = memory_search.search_memories("runtime evidence local", project_id=project_id)
    checks["memory"] = "PASS" if memory.get("memoryId") and memory_hits else "FAIL"

    skill_result = run_skill(
        "skill_research_brief",
        {"topic": "Personal AI Runtime GA", "depth": "quick"},
        project_id=project_id,
        offline=True,
    )
    skill_run_id = str(skill_result.get("skillRunId") or "")
    checks["skill"] = "PASS" if skill_run_id else "FAIL"

    media = media_library.register_media(
        project_id=project_id,
        media_type="webpage",
        title="Offline Browser Snapshot",
        source={"kind": "browser", "refId": "browser-ga-smoke", "browserSessionId": "browser-ga-smoke", "url": "https://example.com"},
        metadata={"sourceUrl": "https://example.com", "capturedBy": "smoke_ga"},
        status="ready",
    )
    segments = media_library.save_segments(
        str(media["mediaId"]),
        [{"type": "page_text", "text": "Example Domain snapshot for GA smoke.", "citation": {"uri": "https://example.com", "label": "Example Domain"}}],
    )
    checks["media"] = "PASS" if media.get("mediaId") and segments else "FAIL"
    checks["browserSnapshot"] = "PASS" if (media.get("source") or {}).get("browserSessionId") == "browser-ga-smoke" else "FAIL"

    saved = saved_items.create_saved_item(
        project_id,
        item_type="webpage",
        title="Browser snapshot note",
        content="Snapshot retained for local GA evidence. Authorization: Bearer ga-secret-token",
        source_ref={"messageId": "msg-ga-smoke", "mediaId": media["mediaId"]},
        tags=["ga", "browser"],
    )
    checks["savedItem"] = "PASS" if saved.get("savedId") else "FAIL"

    artifact_path = root / ".generated" / "ga-runtime-report.md"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("# GA Runtime Report\napi_key=sk-ga-artifact-secret\n", encoding="utf-8")
    artifact = artifacts.register_artifact(
        project_id,
        artifact_type="markdown",
        title="GA Runtime Report",
        path=str(artifact_path),
        source={"type": "skill_run", "skillId": "skill_research_brief", "skillRunId": skill_run_id, "mediaId": media["mediaId"]},
    )
    checks["artifact"] = "PASS" if artifact.get("artifactId") else "FAIL"

    automation = automation_registry.create_automation(
        {
            "name": "GA Project Summary",
            "projectId": project_id,
            "trigger": {"type": "manual"},
            "condition": {"type": "always"},
            "action": {"type": "project_summary"},
            "output": {"saveToProject": True, "createArtifact": True, "artifactType": "markdown"},
            "policy": {"maxRunsPerDay": 5, "allowBrowser": False, "allowNetwork": False},
        }
    )
    run = automation_runner.run_once(str(automation["automationId"]), force=True)
    raw_outputs = run.get("outputs")
    outputs: dict[str, Any] = raw_outputs if isinstance(raw_outputs, dict) else {}
    checks["automation"] = "PASS" if run.get("status") == "success" and outputs.get("artifactIds") and outputs.get("savedItemIds") else "FAIL"

    export = exports.export_project(project_id, export_format="zip")["export"]
    export_path = Path(str(export["path"]))
    combined = ""
    zip_names: set[str] = set()
    if export_path.is_file():
        with zipfile.ZipFile(export_path) as archive:
            zip_names = set(archive.namelist())
            combined = "\n".join(archive.read(name).decode("utf-8", errors="ignore") for name in zip_names)
    checks["export"] = "PASS" if export_path.is_file() and {"metadata.json", "project.md", "saved-items/saved-items.json"}.issubset(zip_names) else "FAIL"
    checks["exportRedaction"] = "PASS" if "ga-secret-token" not in combined and "sk-ga-artifact-secret" not in combined else "FAIL"

    overview = home.workspace_home(limit=10)
    expected_modules = {"Projects", "Memory", "Skills", "Media", "Browser", "Automations", "Artifacts", "Saved Items", "Exports", "Settings"}
    checks["workspaceHome"] = "PASS" if expected_modules.issubset({str(item.get("label") or "") for item in overview.get("modules", [])}) else "FAIL"
    graph = provenance.project_provenance(project_id)
    graph_nodes = {str(node.get("id") or "") for node in graph.get("nodes", [])}
    graph_edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    required_nodes = {
        f"project:{project_id}",
        f"memory:{memory['memoryId']}",
        f"media:{media['mediaId']}",
        f"saved_item:{saved['savedId']}",
        f"artifact:{artifact['artifactId']}",
        f"automation_run:{run['runId']}",
        f"export:{export['exportId']}",
    }
    checks["provenance"] = "PASS" if required_nodes.issubset(graph_nodes) and graph_edges else "FAIL"

    details.update(
        {
            "projectId": project_id,
            "memoryId": memory.get("memoryId"),
            "skillRunId": skill_run_id,
            "mediaId": media.get("mediaId"),
            "savedId": saved.get("savedId"),
            "artifactId": artifact.get("artifactId"),
            "automationRunId": run.get("runId"),
            "export": {"exportId": export.get("exportId"), "path": str(export_path), "entries": sorted(zip_names), "includes": export.get("includes")},
            "homeCounts": overview.get("counts"),
            "provenanceSummary": graph.get("summary"),
        }
    )
    return checks, details


def build_evidence(checks: dict[str, str], details: dict[str, Any], *, version: str) -> dict[str, Any]:
    status = "PASS" if all(value == "PASS" for value in checks.values()) else "FAIL"
    revision = evidence_revision(REPO_ROOT)
    return {
        "schemaVersion": "ga-smoke.v1",
        "version": version,
        "commit": revision["testedRevision"],
        **revision,
        "generatedAt": utc_now(),
        "environment": {"os": platform.platform(), "python": platform.python_version(), "ci": bool(os.environ.get("CI"))},
        "status": status,
        "checks": checks,
        "details": details,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline GA smoke for the Personal AI Runtime")
    parser.add_argument("--offline", action="store_true", help="Run without API keys or network. This smoke is always offline.")
    parser.add_argument("--version", default=app_version())
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "evidence" / f"ga-v{app_version()}.json")
    parser.add_argument("--json", action="store_true", help="Print evidence JSON to stdout.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="deepseek-ga-smoke-", ignore_cleanup_errors=True) as tmp:
        os.environ["DEEPSEEK_INFRA_ROOT"] = tmp
        runtime_root = Path(tmp)
        configure_runtime_root(runtime_root)
        checks, details = run_ga_smoke(runtime_root)
        evidence = build_evidence(checks, details, version=str(args.version))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if evidence["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

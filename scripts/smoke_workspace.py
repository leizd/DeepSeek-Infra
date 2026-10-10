#!/usr/bin/env python3
"""Offline Workspace Core smoke for the current release.

Creates an isolated local workspace, exercises projects, saved items, artifact
registration, conversation export, project ZIP export, and secret redaction, then
writes structured evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.infra.diagnostics.evidence_revision import evidence_revision  # noqa: E402


def git_short_sha() -> str:
    result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, check=False, capture_output=True, text=True)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unknown"


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def app_version() -> str:
    from deepseek_infra.core.config import settings

    return settings.app_version


def configure_runtime_root(root: Path) -> None:
    from deepseek_infra.core import config
    from deepseek_infra.infra.data import projects as legacy_projects
    from deepseek_infra.infra.rag import files, local_rag
    from deepseek_infra.infra.tool_runtime import generated_files

    projects_dir = root / ".projects"
    generated_dir = root / ".generated"
    local_rag_dir = root / ".local-rag"
    config.ROOT = root
    config.PROJECTS_DIR = projects_dir
    config.GENERATED_DIR = generated_dir
    config.LOCAL_RAG_DIR = local_rag_dir
    config.LOCAL_RAG_DB = local_rag_dir / "rag.sqlite3"
    legacy_projects.PROJECTS_DIR = projects_dir
    files.PROJECTS_DIR = projects_dir
    local_rag.PROJECTS_DIR = projects_dir
    local_rag.LOCAL_RAG_DIR = local_rag_dir
    local_rag.LOCAL_RAG_DB = local_rag_dir / "rag.sqlite3"
    generated_files.GENERATED_DIR = generated_dir


def run_workspace_smoke(root: Path) -> tuple[dict[str, str], dict[str, Any]]:
    from deepseek_infra.infra.data import projects as legacy_projects
    from deepseek_infra.infra.workspace import artifacts, exports, projects, saved_items

    checks: dict[str, str] = {
        "projectCreate": "FAIL",
        "projectRename": "FAIL",
        "savedItemCreate": "FAIL",
        "artifactList": "FAIL",
        "conversationExport": "FAIL",
        "projectExportZip": "FAIL",
        "secretRedaction": "FAIL",
        "projectDeleteScope": "FAIL",
    }
    details: dict[str, Any] = {"runtimeRoot": str(root)}

    project = projects.create_project("Workspace Smoke", description="Workspace Core object model")
    checks["projectCreate"] = "PASS" if project.get("projectId") else "FAIL"
    renamed = projects.rename_project(str(project["projectId"]), "Workspace Smoke Renamed")
    checks["projectRename"] = "PASS" if renamed.get("name") == "Workspace Smoke Renamed" else "FAIL"

    legacy_projects.add_project_files(
        str(project["projectId"]),
        [{"filename": "source.txt", "content_type": "text/plain", "data": b"source note\nDEEPSEEK_API_KEY=sk-source-secret"}],
    )
    saved = saved_items.create_saved_item(
        str(project["projectId"]),
        item_type="chat_snippet",
        title="Saved answer",
        content="Keep this answer. Authorization: Bearer workspace-secret-token",
        source_ref={"conversationId": "conv-smoke", "messageId": "msg-smoke"},
        tags=["workspace"],
        purpose="export_fragment",
    )
    checks["savedItemCreate"] = "PASS" if saved.get("savedId") else "FAIL"

    generated_dir = root / ".generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    artifact_file = generated_dir / "workspace-summary.md"
    artifact_file.write_text("# Workspace Summary\napi_key=sk-artifact-secret\n", encoding="utf-8")
    artifact = artifacts.register_artifact(
        str(project["projectId"]),
        artifact_type="markdown",
        title="Workspace Summary",
        path=str(artifact_file),
        source={"conversationId": "conv-smoke", "messageId": "msg-artifact"},
    )
    listed_artifacts = artifacts.list_artifacts(str(project["projectId"]))
    checks["artifactList"] = "PASS" if listed_artifacts and listed_artifacts[0]["artifactId"] == artifact["artifactId"] else "FAIL"

    conversation = {
        "id": "conv-smoke",
        "title": "Workspace smoke conversation",
        "messages": [
            {"id": "u1", "role": "user", "content": "Export this project"},
            {"id": "a1", "role": "assistant", "content": "Generated artifact link: /api/workspace/artifacts/" + artifact["artifactId"]},
        ],
    }
    projects.upsert_project_conversation(str(project["projectId"]), conversation)
    conversation_export = exports.export_conversation(conversation, project_id=str(project["projectId"]), export_format="markdown")["export"]
    checks["conversationExport"] = "PASS" if Path(str(conversation_export["path"])).is_file() else "FAIL"

    project_export = exports.export_project(str(project["projectId"]), export_format="zip")["export"]
    zip_path = Path(str(project_export["path"]))
    required_entries = {"metadata.json", "saved-items/saved-items.json", "project.md"}
    combined = ""
    zip_names: set[str] = set()
    if zip_path.is_file():
        with zipfile.ZipFile(zip_path) as archive:
            zip_names = set(archive.namelist())
            combined = "\n".join(archive.read(name).decode("utf-8", errors="ignore") for name in zip_names)
    has_structure = required_entries.issubset(zip_names)
    has_structure = has_structure and any(name.startswith("conversations/") for name in zip_names)
    has_structure = has_structure and any(name.startswith("artifacts/") for name in zip_names)
    has_structure = has_structure and any(name.startswith("files/source-files/") for name in zip_names)
    checks["projectExportZip"] = "PASS" if has_structure else "FAIL"
    checks["secretRedaction"] = "PASS" if all(secret not in combined for secret in ("sk-source-secret", "workspace-secret-token", "sk-artifact-secret")) else "FAIL"

    global_artifact = generated_dir / "global-keep.md"
    global_artifact.write_text("keep", encoding="utf-8")
    projects.delete_project(str(project["projectId"]))
    checks["projectDeleteScope"] = "PASS" if global_artifact.exists() and not (root / ".projects" / str(project["projectId"])).exists() else "FAIL"

    details["projectId"] = project["projectId"]
    details["projectExport"] = {"path": str(zip_path), "entries": sorted(zip_names)}
    details["conversationExport"] = conversation_export
    return checks, details


def build_evidence(checks: dict[str, str], details: dict[str, Any]) -> dict[str, Any]:
    status = "PASS" if all(value == "PASS" for value in checks.values()) else "FAIL"
    revision = evidence_revision(REPO_ROOT)
    return {
        "version": app_version(),
        "commit": revision["testedRevision"],
        **revision,
        "generatedAt": utc_now(),
        "environment": {"os": platform.platform(), "python": platform.python_version(), "ci": bool(os.environ.get("CI"))},
        "status": status,
        "checks": checks,
        "details": details,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline Workspace Core smoke")
    parser.add_argument("--offline", action="store_true", help="Kept for release-smoke symmetry; this smoke is always offline.")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "evidence" / f"workspace-v{app_version()}.json")
    parser.add_argument("--json", action="store_true", help="Print evidence JSON to stdout.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="deepseek-workspace-smoke-") as tmp:
        os.environ["DEEPSEEK_INFRA_ROOT"] = tmp
        runtime_root = Path(tmp)
        configure_runtime_root(runtime_root)
        checks, details = run_workspace_smoke(runtime_root)
        evidence = build_evidence(checks, details)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if evidence["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

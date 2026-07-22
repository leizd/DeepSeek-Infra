"""Skill run artifact metadata and release evidence helpers."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Any

from deepseek_infra.infra.diagnostics.evidence_revision import evidence_revision, load_source_context

from deepseek_infra.core.config import APP_VERSION, GENERATED_DIR
from deepseek_infra.core.utils import utc_now_iso
from deepseek_infra.infra.tool_runtime.generated_files import resolve_generated_file, store_generated_file

ARTIFACT_INDEX_NAME = "artifacts.json"
FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def artifact_index_path() -> Path:
    return GENERATED_DIR / ARTIFACT_INDEX_NAME


def load_artifact_index() -> list[dict[str, Any]]:
    path = artifact_index_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def save_artifact_index(items: list[dict[str, Any]]) -> None:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    artifact_index_path().write_text(json.dumps(items[-1000:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def register_generated_artifact(
    file_result: dict[str, Any],
    *,
    skill_id: str,
    skill_run_id: str,
    project_id: str = "",
    tool: str = "",
) -> dict[str, Any] | None:
    file_id = str(file_result.get("fileId") or "").strip()
    if not FILE_ID_RE.fullmatch(file_id):
        return None
    path = resolve_generated_file(file_id)
    artifact_type = path.suffix.lower().lstrip(".") if path is not None else str(file_result.get("format") or "").lower()
    artifact = {
        "artifactId": f"art-{file_id[:16]}",
        "fileId": file_id,
        "filename": str(file_result.get("filename") or (path.name if path else file_id)),
        "downloadUrl": str(file_result.get("downloadUrl") or f"/api/download?id={file_id}"),
        "type": artifact_type,
        "tool": tool,
        "createdAt": utc_now_iso(),
        "source": {
            "type": "skill_run",
            "skillId": skill_id,
            "skillRunId": skill_run_id,
            "projectId": project_id,
        },
    }
    items = [item for item in load_artifact_index() if item.get("artifactId") != artifact["artifactId"]]
    items.append(artifact)
    save_artifact_index(items)
    return artifact


def save_markdown_artifact(
    *,
    title: str,
    content: str,
    skill_id: str,
    skill_run_id: str,
    project_id: str = "",
) -> dict[str, Any] | None:
    text = str(content or "").strip()
    if not text:
        return None

    def writer(path: Path) -> None:
        path.write_text(text + "\n", encoding="utf-8")

    result = store_generated_file(title or "skill-output", "md", writer)
    return register_generated_artifact(result, skill_id=skill_id, skill_run_id=skill_run_id, project_id=project_id, tool="skill_markdown")


def artifacts_for_project(project_id: str) -> list[dict[str, Any]]:
    project = str(project_id or "")
    return [item for item in load_artifact_index() if (item.get("source") or {}).get("projectId") == project]


def artifacts_for_skill_run(skill_run_id: str) -> list[dict[str, Any]]:
    run_id = str(skill_run_id or "")
    return [item for item in load_artifact_index() if (item.get("source") or {}).get("skillRunId") == run_id]


def release_evidence_payload(*, checks: dict[str, str], version: str = APP_VERSION, details: dict[str, Any] | None = None) -> dict[str, Any]:
    status = "PASS" if checks and all(str(value).upper() == "PASS" for value in checks.values()) else "FAIL"
    return {
        "version": version,
        "commit": git_commit(),
        **evidence_revision(),
        "generatedAt": utc_now_iso(),
        "environment": {"os": platform.system() or platform.platform(), "python": platform.python_version(), "ci": bool(_env_ci())},
        "status": status,
        "checks": checks,
        "details": details or {},
    }


def write_release_evidence(path: Path, payload: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def git_commit() -> str:
    context = load_source_context()
    if context is not None:
        return str(context["testedRevision"])
    try:
        completed = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], check=False, capture_output=True, text=True)
    except OSError:
        return "unknown"
    value = completed.stdout.strip()
    return value or "unknown"


def _env_ci() -> bool:
    return any(os.environ.get(name) for name in ("CI", "GITHUB_ACTIONS"))

#!/usr/bin/env python3
"""Offline smoke for the v2.6 Skill System."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.core.config import APP_VERSION  # noqa: E402
from deepseek_infra.core.errors import AppError  # noqa: E402
from deepseek_infra.infra.data import projects  # noqa: E402
from deepseek_infra.infra.media import library as media_library  # noqa: E402
from deepseek_infra.infra.observability import observability  # noqa: E402
from deepseek_infra.infra.rag import local_rag  # noqa: E402
from deepseek_infra.infra.skills import evidence, permissions, registry  # noqa: E402
from deepseek_infra.infra.skills.runner import run_skill  # noqa: E402
from deepseek_infra.infra.tool_runtime import generated_files  # noqa: E402


def patch_runtime(root: Path) -> None:
    generated_dir = root / ".generated"
    skills_dir = root / ".skills"
    projects_dir = root / ".projects"
    media_dir = root / ".media"
    local_rag_dir = root / ".local-rag"
    traces_dir = root / ".traces"
    projects.PROJECTS_DIR = projects_dir
    registry.SKILLS_DIR = skills_dir
    media_library.MEDIA_DIR = media_dir
    local_rag.LOCAL_RAG_DIR = local_rag_dir
    local_rag.LOCAL_RAG_DB = local_rag_dir / "rag.sqlite3"
    local_rag.PROJECTS_DIR = projects_dir
    evidence.GENERATED_DIR = generated_dir
    generated_files.GENERATED_DIR = generated_dir
    observability.TRACE_DIR = traces_dir
    observability.TRACE_DB = traces_dir / "traces.sqlite3"


def custom_skill_config() -> dict[str, Any]:
    return {
        "skillId": "skill_smoke_custom",
        "name": "Smoke Custom Skill",
        "description": "Custom Skill used by the offline smoke.",
        "version": "1.0.0",
        "systemPrompt": "Return a concise markdown note.",
        "inputSchema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"], "additionalProperties": False},
        "outputSchema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"], "additionalProperties": True},
        "allowedTools": ["search_files"],
        "memoryPolicy": {"scope": "project", "read": True, "write": False},
        "artifactPolicy": {"autoSave": True, "types": ["md"]},
        "projectBinding": {"enabled": True},
        "exampleInputs": [{"topic": "offline smoke"}],
    }


def collect_route_paths(routes: list[Any]) -> set[str]:
    paths: set[str] = set()
    for route in routes:
        path = getattr(route, "path", "")
        if path:
            paths.add(path)
        original = getattr(route, "original_router", None)
        if original is not None:
            paths |= collect_route_paths(getattr(original, "routes", []))
    return paths


def run_checks(runtime_root: Path) -> tuple[dict[str, str], dict[str, Any]]:
    patch_runtime(runtime_root)
    checks: dict[str, str] = {}
    details: dict[str, Any] = {}

    from deepseek_infra.web.server import create_app

    app = create_app()
    routes = collect_route_paths(getattr(app, "routes", []))
    api_routes = {"/api/skills", "/api/skills/{skill_id}/run"}.issubset(routes)
    checks["skillApiRoutes"] = "PASS" if api_routes else "FAIL"
    details["skillApiRoutes"] = sorted(route for route in routes if route.startswith("/api/skills"))

    builtins = registry.list_builtin_skills()
    checks["builtinSkillsLoad"] = "PASS" if len(builtins) >= 6 else "FAIL"
    details["builtinSkillIds"] = [item["skillId"] for item in builtins]

    custom = registry.create_custom_skill(custom_skill_config())
    checks["customSkillCreate"] = "PASS" if custom["skillId"] == "skill_smoke_custom" else "FAIL"

    try:
        run_skill("skill_research_brief", {}, offline=True)
    except AppError:
        checks["inputSchemaValidation"] = "PASS"
    else:
        checks["inputSchemaValidation"] = "FAIL"

    code_review = registry.get_skill("skill_code_review")
    decision = permissions.evaluate_skill_tool(code_review, "fetch_url", {"url": "https://example.com"})
    checks["toolPermissionGate"] = "PASS" if not decision.allowed else "FAIL"
    details["toolPermissionGate"] = decision.to_dict()

    project = projects.create_project("Skill Smoke Project")
    binding = projects.set_project_skill_binding(project["id"], ["skill_study_tutor"], default_skill="skill_study_tutor")
    run = run_skill("skill_research_brief", {"topic": "Skill System smoke", "depth": "quick"}, project_id=project["id"], offline=True)
    exported = projects.export_project(project["id"])
    raw_artifacts = run.get("artifacts")
    artifacts: list[Any] = raw_artifacts if isinstance(raw_artifacts, list) else []
    raw_source = artifacts[0].get("source") if artifacts and isinstance(artifacts[0], dict) else {}
    source: dict[str, Any] = raw_source if isinstance(raw_source, dict) else {}
    raw_saved_items = run.get("savedItems")
    saved_items: list[Any] = raw_saved_items if isinstance(raw_saved_items, list) else []
    checks["artifactPolicy"] = "PASS" if artifacts and source.get("skillRunId") == run.get("skillRunId") else "FAIL"
    checks["projectBinding"] = "PASS" if binding.get("defaultSkill") == "skill_study_tutor" and exported["skillRuns"] else "FAIL"
    checks["skillExport"] = "PASS" if registry.export_skill_config("skill_smoke_custom").get("skillId") == "skill_smoke_custom" else "FAIL"
    details["skillRun"] = {
        "skillRunId": run.get("skillRunId"),
        "projectId": project["id"],
        "artifactIds": [item.get("artifactId") for item in artifacts if isinstance(item, dict)],
        "savedItemIds": [item.get("id") for item in saved_items if isinstance(item, dict)],
    }
    return checks, details


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline Skill System smoke")
    parser.add_argument("--offline", action="store_true", help="Run without API keys. This is the only supported smoke mode.")
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--out", default=str(REPO_ROOT / "docs" / "evidence" / f"skills-v{APP_VERSION}.json"))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="deepseek-skills-smoke-", ignore_cleanup_errors=True) as tmp:
        checks, details = run_checks(Path(tmp))
    payload = evidence.release_evidence_payload(checks=checks, version=args.version, details=details)
    evidence.write_release_evidence(Path(args.out), payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for name, status in checks.items():
            print(f"[{status}] {name}")
        print(f"Skill smoke summary: {payload['status']} -> {args.out}")
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

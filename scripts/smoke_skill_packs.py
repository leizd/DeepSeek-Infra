#!/usr/bin/env python3
"""Offline smoke for the Skill Packs / Skill Template Library."""

from __future__ import annotations

import argparse
import json
import subprocess
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
from deepseek_infra.infra.skills import evidence, pack, registry  # noqa: E402
from deepseek_infra.infra.tool_runtime import generated_files  # noqa: E402


def patch_runtime(root: Path) -> None:
    skills_dir = root / ".skills"
    projects_dir = root / ".projects"
    generated_dir = root / ".generated"
    registry.SKILLS_DIR = skills_dir
    projects.PROJECTS_DIR = projects_dir
    evidence.GENERATED_DIR = generated_dir
    generated_files.GENERATED_DIR = generated_dir


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _contains_all(text: str, needles: tuple[str, ...]) -> bool:
    return all(needle in text for needle in needles)


def _embedded_skill(skill_id: str) -> dict[str, Any]:
    return {
        "skillId": skill_id,
        "name": f"Pack smoke {skill_id}",
        "description": "Embedded Skill used by the Skill Pack smoke.",
        "version": "1.0.0",
        "systemPrompt": "Return markdown.",
        "inputSchema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"], "additionalProperties": False},
        "outputSchema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"], "additionalProperties": True},
        "allowedTools": ["search_files", "fetch_url"],
        "memoryPolicy": {"scope": "project", "read": True, "write": False},
        "artifactPolicy": {"autoSave": True, "types": ["md"]},
        "projectBinding": {"enabled": True},
        "exampleInputs": [{"topic": "pack"}],
    }


def _pack_config() -> dict[str, Any]:
    return {
        "packId": "pack_smoke_packs",
        "name": "Smoke Pack",
        "description": "Skill Pack used by the offline smoke.",
        "version": "1.0.0",
        "author": "local",
        "skills": [_embedded_skill("skill_pack_smoke_a"), _embedded_skill("skill_pack_smoke_b")],
    }


def run_checks(runtime_root: Path) -> tuple[dict[str, str], dict[str, Any]]:
    patch_runtime(runtime_root)
    checks: dict[str, str] = {}
    details: dict[str, Any] = {}

    # Schema validation
    try:
        validated = pack.validate_pack_config(_pack_config())
        checks["packSchemaValidation"] = "PASS" if validated["packId"] == "pack_smoke_packs" else "FAIL"
    except pack.PackSchemaError:
        checks["packSchemaValidation"] = "FAIL"

    # Builtin packs load
    builtin = registry.list_builtin_packs()
    builtin_ids = {item["packId"] for item in builtin}
    checks["builtinPacksLoad"] = "PASS" if {"pack_study", "pack_research", "pack_code", "pack_office"}.issubset(builtin_ids) else "FAIL"
    details["builtinPackIds"] = sorted(builtin_ids)

    # Pack import
    try:
        summary = registry.import_pack(_pack_config())
        checks["packImport"] = "PASS" if summary["installedSkills"] == ["skill_pack_smoke_a", "skill_pack_smoke_b"] else "FAIL"
        details["packImport"] = {"installedSkills": summary["installedSkills"], "conflicts": summary["conflicts"]}
    except AppError:
        checks["packImport"] = "FAIL"

    # Pack export embeds full configs
    try:
        exported = registry.export_pack("pack_study")
        checks["packExport"] = "PASS" if all("systemPrompt" in skill for skill in exported["skills"]) else "FAIL"
        details["packExport"] = {"packId": exported["packId"], "skillIds": [skill["skillId"] for skill in exported["skills"]]}
    except AppError:
        checks["packExport"] = "FAIL"

    # Skill id conflict handling (error / skip / overwrite)
    conflict_ok = False
    try:
        registry.import_pack(_pack_config(), on_conflict="error")
    except AppError:
        skipped = registry.import_pack(_pack_config(), on_conflict="skip")
        overwritten = registry.import_pack(_pack_config(), overwrite=True)
        conflict_ok = skipped["skippedSkills"] == ["skill_pack_smoke_a", "skill_pack_smoke_b"] and overwritten["installedSkills"] == [
            "skill_pack_smoke_a",
            "skill_pack_smoke_b",
        ]
    checks["skillIdConflictHandling"] = "PASS" if conflict_ok else "FAIL"

    # Tool permission diff
    perm = pack.tool_permission_summary(pack.validate_pack_config(_pack_config()))
    fetch_risk = ""
    for item in perm:
        for tool in item.get("allowedTools", []):
            if tool.get("tool") == "fetch_url":
                fetch_risk = tool.get("risk", "")
    checks["toolPermissionDiff"] = "PASS" if fetch_risk and "fetch_url" in pack.high_risk_tools(pack.validate_pack_config(_pack_config())) else "FAIL"
    details["toolPermissionDiff"] = {"fetchUrlRisk": fetch_risk}

    # Project pack binding
    try:
        project = projects.create_project("Skill Packs Smoke Project")
        binding = projects.enable_pack_for_project(project["id"], "pack_smoke_packs")
        checks["projectPackBinding"] = "PASS" if "pack_smoke_packs" in binding["enabledPacks"] and binding["enabledSkills"] else "FAIL"
        details["projectPackBinding"] = {"enabledPacks": binding["enabledPacks"], "enabledSkills": binding["enabledSkills"]}
    except AppError:
        checks["projectPackBinding"] = "FAIL"

    # Pack install dry run (install builtin pack to a project)
    try:
        project = projects.create_project("Skill Packs Install Project")
        install_binding = projects.enable_pack_for_project(project["id"], "pack_code")
        checks["packInstallDryRun"] = "PASS" if "pack_code" in install_binding["enabledPacks"] else "FAIL"
    except AppError:
        checks["packInstallDryRun"] = "FAIL"

    # UI tab presence
    index = _read("static/index.html")
    skills_js = _read("static/modules/skills.js")
    styles = _read("static/styles.css")
    routes = _read("deepseek_infra/web/routes/skills.py")
    ci = _read(".github/workflows/ci.yml")
    checks["packUiTab"] = "PASS" if _contains_all(
        index + skills_js + styles,
        (
            'id="skillPacksButton"',
            'id="skillPacksHost"',
            'id="skillBuiltinPackList"',
            'id="skillCustomPackList"',
            "openPacksHost",
            "renderPackCard",
            "importPackFromFile",
            ".skill-packs-host",
            ".skill-pack-card",
        ),
    ) else "FAIL"
    details["packUiEntrypoints"] = ["#skillPacksButton", "#skillPacksHost", "#skillBuiltinPackList", "#skillCustomPackList"]

    checks["packApiActions"] = "PASS" if _contains_all(
        routes,
        (
            'action == "list_packs"',
            'action == "export_pack"',
            'action == "import_pack"',
            'action == "validate_pack"',
            'action == "delete_pack"',
        ),
    ) else "FAIL"

    syntax = subprocess.run(["node", "--check", "static/modules/skills.js"], cwd=REPO_ROOT, capture_output=True, text=True)
    checks["packJsSyntax"] = "PASS" if syntax.returncode == 0 else "FAIL"
    details["packJsSyntax"] = {"returnCode": syntax.returncode, "stderr": syntax.stderr.strip()}

    checks["ciSyntaxGate"] = "PASS" if "node --check static/modules/skills.js" in ci else "FAIL"

    asset_paths = ("docs/assets/skill-packs.png", "docs/assets/skill-pack-import.png")
    checks["packAssets"] = "PASS" if all((REPO_ROOT / path).is_file() for path in asset_paths) else "FAIL"
    details["packAssets"] = list(asset_paths)

    return checks, details


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline Skill Packs smoke")
    parser.add_argument("--offline", action="store_true", help="Kept for release-smoke symmetry; this smoke is always offline.")
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--out", default=str(REPO_ROOT / "docs" / "evidence" / f"skill-packs-v{APP_VERSION}.json"))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="deepseek-skill-packs-smoke-", ignore_cleanup_errors=True) as tmp:
        checks, details = run_checks(Path(tmp))
    payload = evidence.release_evidence_payload(checks=checks, version=args.version, details=details)
    evidence.write_release_evidence(Path(args.out), payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for name, status in checks.items():
            print(f"[{status}] {name}")
        print(f"Skill Packs smoke summary: {payload['status']} -> {args.out}")
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

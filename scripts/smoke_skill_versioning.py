#!/usr/bin/env python3
"""Offline smoke for Skill Versioning & Migration lifecycle checks."""

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
from deepseek_infra.infra.data import projects  # noqa: E402
from deepseek_infra.infra.skills import evidence, registry, versioning  # noqa: E402
from scripts.smoke_skills import patch_runtime  # noqa: E402


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _contains_all(text: str, needles: tuple[str, ...]) -> bool:
    return all(needle in text for needle in needles)


def _skill_config(version: str = "1.0.0") -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {"topic": {"type": "string", "title": "Topic", "default": "lifecycle"}},
        "required": ["topic"],
        "additionalProperties": False,
    }
    if version != "1.0.0":
        schema = {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "title": "Subject", "default": "lifecycle"},
                "level": {"type": "string", "title": "Level", "default": "beginner"},
            },
            "required": ["subject", "level"],
            "additionalProperties": False,
        }
    return {
        "skillId": "skill_version_smoke",
        "name": "Version Smoke Skill",
        "description": "Used by Skill Versioning smoke.",
        "version": version,
        "systemPrompt": "Return markdown.",
        "inputSchema": schema,
        "outputSchema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"], "additionalProperties": True},
        "allowedTools": ["search_files"] if version == "1.0.0" else ["search_files", "fetch_url"],
        "memoryPolicy": {"scope": "project", "read": True, "write": False},
        "artifactPolicy": {"autoSave": True, "types": ["md"]},
        "projectBinding": {"enabled": True},
        "exampleInputs": [{"topic": "lifecycle"}],
    }


def _pack_config(version: str = "1.0.0") -> dict[str, Any]:
    return {
        "packId": "pack_version_smoke",
        "name": "Version Smoke Pack",
        "description": "Used by Skill Pack versioning smoke.",
        "version": version,
        "author": "local",
        "skills": [_skill_config("1.0.0")],
    }


def run_checks(runtime_root: Path) -> tuple[dict[str, str], dict[str, Any]]:
    patch_runtime(runtime_root)
    checks: dict[str, str] = {}
    details: dict[str, Any] = {}

    project = projects.create_project("Skill Versioning Smoke Project")
    created = registry.create_custom_skill(_skill_config("1.0.0"), overwrite=True)
    projects.set_project_skill_binding(project["id"], [created["skillId"]], default_skill=created["skillId"])
    updated = registry.update_skill(created["skillId"], {**_skill_config("1.1.0"), "changeSummary": "Smoke schema migration"})
    versions = versioning.list_skill_versions(created["skillId"])
    diff = versioning.diff_skill_versions(created["skillId"], "1.0.0", "1.1.0")
    plan = versioning.migration_plan(created["skillId"], "1.0.0", "1.1.0")
    rolled = versioning.rollback_skill(created["skillId"], "1.0.0")

    checks["skillVersionSnapshot"] = "PASS" if {"1.0.0", "1.1.0"} <= {item["version"] for item in versions} else "FAIL"
    checks["skillDiff"] = "PASS" if diff["toolGrantDiff"]["added"] == ["fetch_url"] and diff["changed"] else "FAIL"
    checks["schemaMigrationPlan"] = "PASS" if plan["safe"] and any(item["type"] == "inputFieldRenamed" for item in plan["changes"]) else "FAIL"
    checks["skillRollback"] = "PASS" if rolled["skill"]["version"] == "1.0.0" else "FAIL"
    checks["projectBindingMigration"] = "PASS" if plan["migrationTargets"]["projectBindings"] >= 1 else "FAIL"
    details["skill"] = {"updatedVersion": updated["version"], "versions": versions, "migrationPlan": plan}

    registry.import_pack(_pack_config("1.0.0"), overwrite=True, on_conflict="overwrite")
    registry.import_pack(_pack_config("1.1.0"), overwrite=True, on_conflict="overwrite")
    pack_versions = versioning.list_pack_versions("pack_version_smoke")
    pack_diff = versioning.diff_pack_versions("pack_version_smoke", "1.0.0", "1.1.0")
    pack_project = projects.create_project("Pack Versioning Smoke Project")
    upgrade = versioning.upgrade_pack("pack_version_smoke", "1.1.0", project_id=pack_project["id"])
    pack_rollback = versioning.rollback_pack("pack_version_smoke", "1.0.0", project_id=pack_project["id"])

    checks["packVersionInstall"] = "PASS" if any(item["version"] == "1.1.0" for item in pack_versions) and upgrade["projectBinding"]["enabledPackVersions"] else "FAIL"
    checks["packRollback"] = "PASS" if pack_rollback["pack"]["version"] == "1.0.0" else "FAIL"
    checks["evalAwareUpgradeGate"] = "PASS" if upgrade["evalAwareUpgradeGate"]["status"] in {"PASS", "REVIEW"} else "FAIL"
    details["pack"] = {"versions": pack_versions, "diffChanged": pack_diff["changed"], "upgradeGate": upgrade["evalAwareUpgradeGate"]}

    routes = _read("deepseek_infra/web/routes/skills.py")
    drawer = _read("frontend/src/features/skills/SkillsDrawer.tsx")
    skills_api = _read("frontend/src/api/skillsApi.ts")
    styles = _read("frontend/src/shared/styles/app.css")
    ci = _read(".github/workflows/ci.yml")

    checks["versioningApiActions"] = "PASS" if _contains_all(
        routes,
        (
            'action == "list_versions"',
            'action == "diff_versions"',
            'action == "rollback_skill"',
            'action == "migration_plan"',
            'action == "upgrade_pack"',
            'action == "rollback_pack"',
        ),
    ) else "FAIL"
    checks["reactSkillSurface"] = "PASS" if _contains_all(
        drawer + skills_api + styles,
        (
            "export function SkillsDrawer",
            "buildSimpleSkillConfig",
            ".skill-card",
        ),
    ) else "FAIL"
    asset_paths = ("docs/assets/skill-version-history.png", "docs/assets/skill-version-diff.png")
    checks["versioningAssets"] = "PASS" if all((REPO_ROOT / path).is_file() for path in asset_paths) else "FAIL"
    checks["frontendTypecheckGate"] = "PASS" if "npm run typecheck --prefix frontend" in ci else "FAIL"
    checks["ciReleaseGate"] = "PASS" if "smoke_skill_versioning.py" in ci else "FAIL"
    details["assets"] = list(asset_paths)
    return checks, details


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline Skill Versioning smoke")
    parser.add_argument("--offline", action="store_true", help="Kept for release-smoke symmetry; this smoke is always offline.")
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--out", default=str(REPO_ROOT / "docs" / "evidence" / f"skill-versioning-v{APP_VERSION}.json"))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="deepseek-skill-versioning-", ignore_cleanup_errors=True) as tmp:
        checks, details = run_checks(Path(tmp))
    payload = evidence.release_evidence_payload(checks=checks, version=args.version, details=details)
    evidence.write_release_evidence(Path(args.out), payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for name, status in checks.items():
            print(f"[{status}] {name}")
        print(f"Skill Versioning smoke summary: {payload['status']} -> {args.out}")
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Offline smoke for the Custom Skill Builder / Skill Authoring Studio."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.core.config import APP_VERSION  # noqa: E402
from deepseek_infra.infra.skills import evidence  # noqa: E402


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _contains_all(text: str, needles: tuple[str, ...]) -> bool:
    return all(needle in text for needle in needles)


def run_checks() -> tuple[dict[str, str], dict[str, Any]]:
    index = _read("static/index.html")
    skills = _read("static/modules/skills.js")
    skill_builder = _read("static/modules/skill_builder.js")
    styles = _read("static/styles.css")
    routes = _read("deepseek_infra/web/routes/skills.py")
    ci = _read(".github/workflows/ci.yml")

    checks: dict[str, str] = {}
    details: dict[str, Any] = {}

    checks["builderOpen"] = "PASS" if _contains_all(
        index + skills + skill_builder,
        (
            'id="skillNewButton"',
            'id="skillBuilderHost"',
            'id="skillBuilderForm"',
            "openSkillBuilder",
            "Custom Skill Builder",
        ),
    ) else "FAIL"

    checks["cloneBuiltinSkill"] = "PASS" if _contains_all(
        skills,
        (
            "data-skill-clone",
            "cloneSkill",
            'mode: "clone"',
            "nextCloneSkillId",
        ),
    ) else "FAIL"

    checks["visualInputSchemaEdit"] = "PASS" if _contains_all(
        index + skills + skill_builder,
        (
            "skillBuilderFieldList",
            "addBuilderField",
            "data-builder-field-key",
            "FIELD_TYPES",
            "buildInputSchema",
            "builderFieldToSchema",
        ),
    ) else "FAIL"

    checks["toolPermissionPicker"] = "PASS" if _contains_all(
        index + skills,
        (
            "TOOL_OPTIONS",
            "skillBuilderToolPicker",
            "data-builder-tool",
            "allowedTools",
            "requires approval",
        ),
    ) else "FAIL"

    checks["schemaValidation"] = "PASS" if _contains_all(
        skills + routes,
        (
            'action: "validate"',
            "validateBuilderConfig",
            "validate_skill_config",
        ),
    ) else "FAIL"

    checks["offlineDryRun"] = "PASS" if _contains_all(
        skills + routes,
        (
            'action: "dry_run"',
            "dryRunBuilderConfig",
            "_dry_run_skill_config",
            "offline_skill_content",
        ),
    ) else "FAIL"

    checks["saveCustomSkill"] = "PASS" if _contains_all(
        skills,
        (
            "saveBuilderConfig",
            'const action = mode === "edit" ? "update" : "create"',
            'action === "update"',
            "{ action, skill: config",
            "loadSkills()",
        ),
    ) and "window.prompt" not in skills else "FAIL"

    checks["exportCreatedSkill"] = "PASS" if _contains_all(
        skills,
        (
            "exportSkill",
            "exportAllCustomSkills",
            "downloadTextFile",
            'action: "export"',
        ),
    ) else "FAIL"

    checks["skillBuilderStyles"] = "PASS" if _contains_all(
        styles,
        (
            ".skill-builder-host",
            ".skill-builder-form",
            ".skill-tool-picker",
            ".skill-builder-preview",
        ),
    ) else "FAIL"

    asset_paths = (
        "docs/assets/skill-builder.png",
        "docs/assets/skill-builder-dry-run.png",
    )
    checks["skillBuilderAssets"] = "PASS" if all((REPO_ROOT / path).is_file() for path in asset_paths) else "FAIL"
    details["skillBuilderAssets"] = list(asset_paths)

    syntax_results = [
        subprocess.run(["node", "--check", path], cwd=REPO_ROOT, capture_output=True, text=True)
        for path in ("static/modules/skill_builder.js", "static/modules/skills.js")
    ]
    checks["skillBuilderJsSyntax"] = "PASS" if all(result.returncode == 0 for result in syntax_results) else "FAIL"
    details["skillBuilderJsSyntax"] = [
        {"returnCode": result.returncode, "stderr": result.stderr.strip()} for result in syntax_results
    ]

    checks["ciSyntaxGate"] = "PASS" if _contains_all(
        ci,
        ("node --check static/modules/skill_builder.js", "node --check static/modules/skills.js"),
    ) else "FAIL"

    details["builderEntrypoints"] = ["#skillNewButton", "#skillBuilderHost", "#skillBuilderForm", "#skillBuilderToolPicker"]
    details["toolOptions"] = [
        "search_files",
        "read_file_chunk",
        "web_search",
        "fetch_url",
        "create_document",
        "create_pptx",
        "create_mindmap",
        "python_eval",
    ]
    return checks, details


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline Custom Skill Builder smoke")
    parser.add_argument("--offline", action="store_true", help="Kept for release-smoke symmetry; this smoke is always offline.")
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--out", default=str(REPO_ROOT / "docs" / "evidence" / f"skill-builder-v{APP_VERSION}.json"))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    checks, details = run_checks()
    payload = evidence.release_evidence_payload(checks=checks, version=args.version, details=details)
    evidence.write_release_evidence(Path(args.out), payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for name, status in checks.items():
            print(f"[{status}] {name}")
        print(f"Skill Builder smoke summary: {payload['status']} -> {args.out}")
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

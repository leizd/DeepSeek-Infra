#!/usr/bin/env python3
"""Offline smoke for the Custom Skill Builder / Skill Authoring Studio."""

from __future__ import annotations

import argparse
import json
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
    drawer = _read("frontend/src/features/skills/SkillsDrawer.tsx")
    controller = _read("frontend/src/features/skills/useSkillController.ts")
    api = _read("frontend/src/api/skillsApi.ts")
    styles = _read("frontend/src/shared/styles/app.css")
    routes = _read("deepseek_infra/web/routes/skills.py")
    ci = _read(".github/workflows/ci.yml")

    checks: dict[str, str] = {}
    details: dict[str, Any] = {}

    checks["builderOpen"] = "PASS" if _contains_all(
        drawer,
        (
            "function SkillForm",
            "setCreating",
            "emptyDraft",
            'className="skill-form"',
        ),
    ) else "FAIL"

    checks["simpleDraftSchema"] = "PASS" if _contains_all(
        api,
        (
            "export interface SimpleSkillDraft",
            "buildSimpleSkillConfig",
            "systemPrompt",
            'version: "1.0.0"',
        ),
    ) else "FAIL"

    checks["createCustomSkill"] = "PASS" if _contains_all(
        drawer + controller + api,
        (
            "skills.create(draft)",
            "createSkill",
            'action: "create"',
            "buildSimpleSkillConfig(draft)",
        ),
    ) else "FAIL"

    checks["updateCustomSkill"] = "PASS" if _contains_all(
        drawer + controller + api,
        (
            "skills.update({ ...draft",
            "updateSkillPrompt",
            'action: "update"',
        ),
    ) else "FAIL"

    checks["schemaValidation"] = "PASS" if _contains_all(
        routes,
        (
            'action == "validate"',
            "validate_skill_config",
        ),
    ) else "FAIL"

    checks["offlineDryRun"] = "PASS" if _contains_all(
        routes,
        (
            'action == "dry_run"',
            "_dry_run_skill_config",
            "offline_skill_content",
        ),
    ) else "FAIL"

    checks["builderInputValidation"] = "PASS" if _contains_all(
        drawer,
        (
            "!draft.name.trim()",
            "!draft.systemPrompt.trim()",
            "maxLength={20_000}",
        ),
    ) else "FAIL"

    checks["exportApi"] = "PASS" if _contains_all(
        routes,
        (
            'action == "export"',
            'action == "import"',
        ),
    ) else "FAIL"

    checks["skillBuilderStyles"] = "PASS" if _contains_all(
        styles,
        (
            ".skill-form",
            ".skill-form input",
            ".skill-form textarea",
            ".skill-card",
        ),
    ) else "FAIL"

    asset_paths = (
        "docs/assets/skill-builder.png",
        "docs/assets/skill-builder-dry-run.png",
    )
    checks["skillBuilderAssets"] = "PASS" if all((REPO_ROOT / path).is_file() for path in asset_paths) else "FAIL"
    details["skillBuilderAssets"] = list(asset_paths)

    checks["frontendTypecheckGate"] = "PASS" if _contains_all(
        ci,
        ("npm run typecheck --prefix frontend", "npm test --prefix frontend", "npm run build --prefix frontend"),
    ) else "FAIL"

    details["builderSources"] = [
        "frontend/src/features/skills/SkillsDrawer.tsx",
        "frontend/src/features/skills/useSkillController.ts",
        "frontend/src/api/skillsApi.ts",
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

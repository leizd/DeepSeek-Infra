#!/usr/bin/env python3
"""Offline source-contract smoke for the React Skill workspace."""

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
    projects = _read("frontend/src/features/projects/ProjectsDrawer.tsx")
    history = _read("frontend/src/features/history/HistoryDrawer.tsx")
    skill_feature = _read("frontend/src/features/skills/SkillsFeature.tsx")
    styles = "\n".join(
        (
            _read("frontend/src/shared/styles/workspace-drawer-frame.css"),
            _read("frontend/src/features/workspace/workspace-optional.css"),
            _read("frontend/src/features/skills/skills.css"),
        )
    )
    main = _read("frontend/src/main.tsx")
    root_worker = _read("frontend/public/sw-root.js")
    ci = _read(".github/workflows/ci.yml")

    checks: dict[str, str] = {}
    details: dict[str, Any] = {}

    checks["skillWorkbenchEntrypoint"] = "PASS" if _contains_all(
        drawer + history,
        (
            'openOverlay("skills")',
            'activeOverlay !== "skills"',
            'className="settings-drawer workspace-drawer"',
            'className="skill-list"',
        ),
    ) else "FAIL"
    checks["skillCreateEditDelete"] = "PASS" if _contains_all(
        drawer + controller,
        ("onSubmit={skills.create}", "onCommitted={() => setCreating(false)}", "skills.update({ ...draft", "skills.remove(skill.skillId)", "createSkill", "updateSkillPrompt", "deleteSkill"),
    ) else "FAIL"
    checks["skillApiActions"] = "PASS" if _contains_all(
        api,
        ('action: "list"', 'action: "create"', 'action: "update"', 'disabled ? "disable" : "enable"', 'action: "delete"'),
    ) else "FAIL"
    checks["projectSkillBindingUi"] = "PASS" if _contains_all(
        projects + controller + api,
        ("useProjectSkillBinding(project.id)", ".save({", "fetchProjectSkillBinding", "saveProjectSkillBinding", "enabledSkills", "defaultSkill"),
    ) else "FAIL"
    checks["skillPanelLifecycle"] = "PASS" if _contains_all(
        drawer,
        ('activeOverlay !== "skills"', "overlay.closeOverlay", 'role="dialog"', 'aria-modal="true"'),
    ) else "FAIL"
    checks["skillPanelStyles"] = "PASS" if _contains_all(
        styles + skill_feature,
        (
            ".settings-drawer",
            ".workspace-toolbar",
            ".skill-list",
            ".skill-card",
            ".skill-form",
            'import "../workspace/workspace-optional.css"',
            'import "./skills.css"',
        ),
    ) else "FAIL"
    checks["reactPwaOwnership"] = "PASS" if _contains_all(
        main + root_worker,
        (
            "startWorkspaceServiceWorkerRuntime",
            'const CACHE_PREFIX = "deepseek-react-root-',
            'const WORKER_BUILD_ID = "__DEEPSEEK_WORKER_BUILD_ID__"',
            'const ASSET_MANIFEST_URL = "__DEEPSEEK_WORKER_MANIFEST_URL__"',
            "cacheFirstByBuild",
        ),
    ) else "FAIL"
    checks["frontendTypecheckGate"] = "PASS" if _contains_all(
        ci,
        ("npm run typecheck --prefix frontend", "npm test --prefix frontend", "npm run build --prefix frontend"),
    ) else "FAIL"

    assets = ["docs/assets/skill-workbench.png", "docs/assets/skill-run-result.png"]
    missing_assets = [asset for asset in assets if not (REPO_ROOT / asset).is_file() or (REPO_ROOT / asset).stat().st_size <= 0]
    checks["skillUiAssets"] = "PASS" if not missing_assets else "FAIL"
    details["skillUiAssets"] = {"assets": assets, "missingOrEmpty": missing_assets}
    details["uiSources"] = [
        "frontend/src/features/skills/SkillsDrawer.tsx",
        "frontend/src/features/skills/useSkillController.ts",
        "frontend/src/api/skillsApi.ts",
        "frontend/src/features/projects/ProjectsDrawer.tsx",
    ]
    return checks, details


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline React Skill workspace smoke")
    parser.add_argument("--offline", action="store_true", help="Kept for release-smoke symmetry; this smoke is always offline.")
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--out", default=str(REPO_ROOT / "docs" / "evidence" / f"skills-ui-v{APP_VERSION}.json"))
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
        print(f"React Skill UI smoke summary: {payload['status']} -> {args.out}")
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

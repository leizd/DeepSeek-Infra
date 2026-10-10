#!/usr/bin/env python3
"""One-command release smoke — orchestrate doctor + offline evals + protocol smokes.

Instead of remembering five scripts, run one. Offline mode is CI-safe (no
network, no API key, no running server):

    python scripts/smoke_release.py --offline

Live mode additionally drives the MCP and A2A compatibility smokes against a
running server:

    python scripts/smoke_release.py --with-server --base-url http://127.0.0.1:8000 --token <token>

The orchestrator only composes existing scripts; it holds no logic of its own.
"""

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

BROWSER_CONTROL_VERSION = APP_VERSION
AUTOMATION_RUNTIME_VERSION = APP_VERSION


def _py() -> str:
    return sys.executable


def build_stages(args: argparse.Namespace) -> list[tuple[str, list[str]]]:
    stages: list[tuple[str, list[str]]] = [
        ("frontend_build", [_py(), str(REPO_ROOT / "scripts" / "build_frontend.py")]),
    ]
    if not args.skip_doctor:
        doctor_cmd = [_py(), str(REPO_ROOT / "scripts" / "doctor.py")]
        if args.offline:
            doctor_cmd.append("--offline")
        if args.with_server:
            doctor_cmd += ["--with-server", "--base-url", args.base_url]
        stages.append(("doctor", doctor_cmd))

    if not args.skip_workspace:
        workspace_cmd = [_py(), str(REPO_ROOT / "scripts" / "smoke_workspace.py"), "--offline", "--out", args.workspace_out]
        stages.append(("workspace_core", workspace_cmd))

    if not args.skip_edge:
        edge_cmd = [_py(), str(REPO_ROOT / "scripts" / "smoke_edge_router.py"), "--offline", "--out", args.edge_out]
        stages.append(("edge_router", edge_cmd))

    if not args.skip_media:
        media_cmd = [_py(), str(REPO_ROOT / "scripts" / "smoke_media.py"), "--offline", "--out", args.media_out]
        stages.append(("media_layer", media_cmd))
        media_eval_cmd = [_py(), str(REPO_ROOT / "evals" / "runners" / "run_media_eval.py"), "--strict", "--out", args.media_eval_out]
        stages.append(("media_eval", media_eval_cmd))

    if not args.skip_browser:
        browser_cmd = [
            _py(),
            str(REPO_ROOT / "scripts" / "smoke_browser.py"),
            "--offline",
            "--out",
            args.browser_out,
            "--version",
            BROWSER_CONTROL_VERSION,
        ]
        stages.append(("browser_control", browser_cmd))
        browser_eval_cmd = [
            _py(),
            str(REPO_ROOT / "evals" / "runners" / "run_browser_eval.py"),
            "--strict",
            "--out",
            args.browser_eval_out,
            "--version",
            BROWSER_CONTROL_VERSION,
        ]
        stages.append(("browser_eval", browser_eval_cmd))

    if not args.skip_automation:
        automation_cmd = [
            _py(),
            str(REPO_ROOT / "scripts" / "smoke_automation.py"),
            "--offline",
            "--out",
            args.automation_out,
            "--version",
            AUTOMATION_RUNTIME_VERSION,
        ]
        stages.append(("automation_runtime", automation_cmd))
        automation_eval_cmd = [
            _py(),
            str(REPO_ROOT / "evals" / "runners" / "run_automation_eval.py"),
            "--strict",
            "--out",
            args.automation_eval_out,
            "--version",
            AUTOMATION_RUNTIME_VERSION,
        ]
        stages.append(("automation_eval", automation_eval_cmd))

    if not args.skip_skills:
        skills_cmd = [_py(), str(REPO_ROOT / "scripts" / "smoke_skills.py"), "--offline", "--out", args.skills_out]
        stages.append(("skill_system", skills_cmd))

    if not args.skip_skills_ui:
        skills_ui_cmd = [_py(), str(REPO_ROOT / "scripts" / "smoke_skills_ui.py"), "--offline", "--out", args.skills_ui_out]
        stages.append(("skill_workbench_ui", skills_ui_cmd))

    if not args.skip_skill_builder:
        skill_builder_cmd = [_py(), str(REPO_ROOT / "scripts" / "smoke_skill_builder.py"), "--offline", "--out", args.skill_builder_out]
        stages.append(("skill_builder", skill_builder_cmd))

    if not args.skip_skill_packs:
        skill_packs_cmd = [_py(), str(REPO_ROOT / "scripts" / "smoke_skill_packs.py"), "--offline", "--out", args.skill_packs_out]
        stages.append(("skill_packs", skill_packs_cmd))

    if not args.skip_skill_eval_dashboard:
        skill_eval_cmd = [
            _py(),
            str(REPO_ROOT / "scripts" / "smoke_skill_eval_dashboard.py"),
            "--offline",
            "--out",
            args.skill_eval_dashboard_out,
            "--report-out",
            args.skill_eval_report_out,
        ]
        stages.append(("skill_eval_dashboard", skill_eval_cmd))

    if not args.skip_skill_versioning:
        skill_versioning_cmd = [_py(), str(REPO_ROOT / "scripts" / "smoke_skill_versioning.py"), "--offline", "--out", args.skill_versioning_out]
        stages.append(("skill_versioning", skill_versioning_cmd))

    if not args.skip_skill_analytics:
        skill_analytics_cmd = [_py(), str(REPO_ROOT / "scripts" / "smoke_skill_analytics.py"), "--offline", "--out", args.skill_analytics_out]
        stages.append(("skill_analytics", skill_analytics_cmd))

    if not args.skip_skill_security:
        skill_security_cmd = [_py(), str(REPO_ROOT / "scripts" / "smoke_skill_security.py"), "--offline", "--out", args.skill_security_out]
        stages.append(("skill_security", skill_security_cmd))

    if not args.skip_skill_catalog:
        skill_catalog_cmd = [_py(), str(REPO_ROOT / "scripts" / "smoke_skill_catalog.py"), "--offline", "--out", args.skill_catalog_out]
        stages.append(("skill_catalog", skill_catalog_cmd))

    if not args.skip_evals:
        stages.append(
            (
                "offline_eval_suite",
                [
                    _py(),
                    str(REPO_ROOT / "evals" / "runners" / "run_offline_eval_suite.py"),
                    "--include-agent",
                    "--strict",
                    "--out",
                    args.out,
                    "--markdown",
                    args.markdown,
                ],
            )
        )

    if not args.skip_security:
        stages.append(
            (
                "security_corpus",
                [
                    _py(),
                    str(REPO_ROOT / "evals" / "runners" / "run_security_corpus.py"),
                    "--strict",
                    "--out",
                    args.security_out,
                    "--markdown",
                    args.security_markdown,
                ],
            )
        )

    if not args.skip_agent:
        stages.append(
            (
                "agent_eval",
                [
                    _py(),
                    str(REPO_ROOT / "evals" / "runners" / "run_agent_eval.py"),
                    "--report-dir",
                    args.report_dir,
                    "--strict",
                ],
            )
        )

    if not args.skip_compare:
        stages.append(
            (
                "baseline_compare",
                [
                    _py(),
                    str(REPO_ROOT / "evals" / "runners" / "compare_eval_baseline.py"),
                    "--strict",
                    "--baseline",
                    args.baseline,
                    "--current",
                    args.out,
                    "--agent-baseline",
                    args.agent_baseline,
                    "--out",
                    args.compare_out,
                ],
            )
        )

    if args.with_server and not args.skip_mcp:
        stages.append(
            (
                "mcp_smoke",
                [
                    _py(),
                    str(REPO_ROOT / "scripts" / "smoke_mcp_compat.py"),
                    "--mcp-url",
                    args.base_url.rstrip("/") + "/mcp",
                    "--token",
                    args.token,
                ],
            )
        )

    if args.with_server and not args.skip_a2a:
        stages.append(
            (
                "a2a_smoke",
                [
                    _py(),
                    str(REPO_ROOT / "scripts" / "smoke_a2a_compat.py"),
                    "--base-url",
                    args.base_url,
                    "--token",
                    args.token,
                ],
            )
        )

    return stages


def run_stages(stages: list[tuple[str, list[str]]], *, cwd: Path) -> list[tuple[str, int]]:
    results: list[tuple[str, int]] = []
    for name, command in stages:
        print(f"\n=== smoke_release :: {name} ===", flush=True)
        print("$ " + " ".join(command), flush=True)
        completed = subprocess.run(command, cwd=cwd, check=False)
        results.append((name, completed.returncode))
        if completed.returncode != 0:
            print(f"[{name}] exited {completed.returncode}", flush=True)
    return results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="One-command release smoke orchestrator")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="Run doctor + offline evals only; skip protocol smokes.")
    mode.add_argument("--with-server", action="store_true", help="Also run MCP / A2A smokes against --base-url.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Local service root for live protocol smokes.")
    parser.add_argument("--token", default="", help="Local auth token for protocol smokes.")
    parser.add_argument("--out", default=str(REPO_ROOT / "evals" / "reports" / "latest.json"))
    parser.add_argument("--markdown", default=str(REPO_ROOT / "evals" / "reports" / "latest.md"))
    parser.add_argument("--report-dir", default=str(REPO_ROOT / "evals" / "reports"))
    parser.add_argument("--baseline", default=str(REPO_ROOT / "evals" / "baselines" / "v2.2.6.json"))
    parser.add_argument("--agent-baseline", default=str(REPO_ROOT / "evals" / "baselines" / "agent-v2.2.8.json"))
    parser.add_argument("--compare-out", default=str(REPO_ROOT / "evals" / "reports" / "baseline-compare-latest.json"))
    parser.add_argument("--security-out", default=str(REPO_ROOT / "evals" / "reports" / "security-latest.json"))
    parser.add_argument("--security-markdown", default=str(REPO_ROOT / "evals" / "reports" / "security-latest.md"))
    parser.add_argument("--workspace-out", default=str(REPO_ROOT / "docs" / "evidence" / f"workspace-v{APP_VERSION}.json"))
    parser.add_argument("--edge-out", default=str(REPO_ROOT / "docs" / "evidence" / f"edge-router-v{APP_VERSION}.json"))
    parser.add_argument("--media-out", default=str(REPO_ROOT / "docs" / "evidence" / f"media-v{APP_VERSION}.json"))
    parser.add_argument("--media-eval-out", default=str(REPO_ROOT / "evals" / "reports" / f"media-v{APP_VERSION}.json"))
    parser.add_argument("--browser-out", default=str(REPO_ROOT / "docs" / "evidence" / f"browser-v{BROWSER_CONTROL_VERSION}.json"))
    parser.add_argument("--browser-eval-out", default=str(REPO_ROOT / "evals" / "reports" / f"browser-v{BROWSER_CONTROL_VERSION}.json"))
    parser.add_argument("--automation-out", default=str(REPO_ROOT / "docs" / "evidence" / f"automation-v{AUTOMATION_RUNTIME_VERSION}.json"))
    parser.add_argument("--automation-eval-out", default=str(REPO_ROOT / "evals" / "reports" / f"automation-v{AUTOMATION_RUNTIME_VERSION}.json"))
    parser.add_argument("--skills-out", default=str(REPO_ROOT / "docs" / "evidence" / f"skills-v{APP_VERSION}.json"))
    parser.add_argument("--skills-ui-out", default=str(REPO_ROOT / "docs" / "evidence" / f"skills-ui-v{APP_VERSION}.json"))
    parser.add_argument("--skill-builder-out", default=str(REPO_ROOT / "docs" / "evidence" / f"skill-builder-v{APP_VERSION}.json"))
    parser.add_argument("--skill-packs-out", default=str(REPO_ROOT / "docs" / "evidence" / f"skill-packs-v{APP_VERSION}.json"))
    parser.add_argument("--skill-eval-dashboard-out", default=str(REPO_ROOT / "docs" / "evidence" / f"skill-eval-dashboard-v{APP_VERSION}.json"))
    parser.add_argument("--skill-eval-report-out", default=str(REPO_ROOT / "evals" / "reports" / f"skills-v{APP_VERSION}.json"))
    parser.add_argument("--skill-versioning-out", default=str(REPO_ROOT / "docs" / "evidence" / f"skill-versioning-v{APP_VERSION}.json"))
    parser.add_argument("--skill-analytics-out", default=str(REPO_ROOT / "docs" / "evidence" / f"skill-analytics-v{APP_VERSION}.json"))
    parser.add_argument("--skill-security-out", default=str(REPO_ROOT / "docs" / "evidence" / f"skill-security-v{APP_VERSION}.json"))
    parser.add_argument("--skill-catalog-out", default=str(REPO_ROOT / "docs" / "evidence" / f"skill-catalog-v{APP_VERSION}.json"))
    parser.add_argument("--skip-doctor", action="store_true")
    parser.add_argument("--skip-workspace", action="store_true")
    parser.add_argument("--skip-edge", action="store_true")
    parser.add_argument("--skip-media", action="store_true")
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument("--skip-automation", action="store_true")
    parser.add_argument("--skip-evals", action="store_true")
    parser.add_argument("--skip-security", action="store_true")
    parser.add_argument("--skip-agent", action="store_true")
    parser.add_argument("--skip-compare", action="store_true")
    parser.add_argument("--skip-skills", action="store_true")
    parser.add_argument("--skip-skills-ui", action="store_true")
    parser.add_argument("--skip-skill-builder", action="store_true")
    parser.add_argument("--skip-skill-packs", action="store_true")
    parser.add_argument("--skip-skill-eval-dashboard", action="store_true")
    parser.add_argument("--skip-skill-versioning", action="store_true")
    parser.add_argument("--skip-skill-analytics", action="store_true")
    parser.add_argument("--skip-skill-security", action="store_true")
    parser.add_argument("--skip-skill-catalog", action="store_true")
    parser.add_argument("--skip-mcp", action="store_true")
    parser.add_argument("--skip-a2a", action="store_true")
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable JSON summary instead of running shells verbosely.")
    parsed = parser.parse_args(argv)
    if not parsed.offline and not parsed.with_server:
        parsed.offline = True
    return parsed


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    stages = build_stages(args)
    if args.json:
        payload: dict[str, Any] = {
            "mode": "with-server" if args.with_server else "offline",
            "stages": [{"name": name, "command": command} for name, command in stages],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    results = run_stages(stages, cwd=REPO_ROOT)
    print("\n=== smoke_release :: summary ===")
    for name, code in results:
        marker = "PASS" if code == 0 else "FAIL"
        print(f"[{marker}] {name} (exit {code})")
    return 1 if any(code != 0 for _, code in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

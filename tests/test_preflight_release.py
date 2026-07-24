from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any


def _load_preflight() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "preflight_release.py"
    spec = importlib.util.spec_from_file_location("preflight_release_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _skeleton(tmp_path: Path, version: str, *, release_exclusions: bool = True) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    frontend_build = root / "static" / "ui"
    frontend_build.mkdir(parents=True)
    (frontend_build / "index.html").write_text("<!doctype html><title>React</title>\n", encoding="utf-8")
    (root / "README.md").write_text(f"![版本](https://img.shields.io/badge/version-{version}-blue)\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text(f"## [{version}] - Release Readiness\n\nbody\n", encoding="utf-8")
    (root / "Dockerfile").write_text(f"docker build -t deepseek-infra:{version} .\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[tool.coverage.report]\nfail_under = 95\n", encoding="utf-8")
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("      - run: pytest --cov --cov-fail-under=95\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "IMPLEMENTATION_STATUS.md").write_text(f"适用版本：v{version}。\n", encoding="utf-8")
    (root / "docs" / "AGENT_EVAL.md").write_text("agent eval\n", encoding="utf-8")
    (root / "docs" / "EVAL_REPORTS.md").write_text("eval reports\n", encoding="utf-8")
    (root / "docs" / "SECURITY_SMOKE.md").write_text("security smoke\n", encoding="utf-8")
    (root / "docs" / "COMPATIBILITY.md").write_text(f"适用版本：v{version}。\n", encoding="utf-8")
    (root / "docs" / "IMPLEMENTATION_STATUS.md").write_text(f"适用版本：v{version}。\n", encoding="utf-8")
    (root / "docs" / "RELEASE_READINESS.md").write_text(f"适用版本：v{version}。\n", encoding="utf-8")
    (root / "docs" / "EVIDENCE_INDEX.md").write_text("evidence index\n", encoding="utf-8")
    (root / "docs" / "integrations").mkdir()
    (root / "docs" / "integrations" / "headless-mcp-client.md").write_text("headless mcp\n", encoding="utf-8")
    (root / "docs" / "integrations" / "a2a-external-peer.md").write_text("a2a external peer\n", encoding="utf-8")
    evidence_dir = root / "docs" / "evidence"
    evidence_dir.mkdir()
    _write_headless_evidence(evidence_dir / "headless-mcp-bridge.json", version)
    _write_a2a_evidence(evidence_dir / "a2a-external-peer.json", version)
    _write_a2a_evidence(evidence_dir / "a2a-third-party-peer.json", version, peer_type="third-party")
    _write_edge_router_evidence(evidence_dir / "edge-router-smoke.json", version)
    _write_edge_router_stabilization_evidence(evidence_dir / f"edge-router-v{version}.json", version)
    _write_continue_dev_evidence(evidence_dir / "continue-dev-mcp.json", version)
    _write_openai_compatible_sdk_evidence(evidence_dir / "openai-compatible-sdks.json", version)
    _write_workspace_evidence(evidence_dir / f"workspace-v{version}.json", version)
    _write_context_taint_evidence(evidence_dir / f"context-taint-v{version}.json", version)
    _write_media_evidence(evidence_dir / f"media-v{version}.json", version)
    _write_browser_evidence(evidence_dir / f"browser-v{version}.json", version)
    _write_automation_evidence(evidence_dir / f"automation-v{version}.json", version)
    _write_semantic_cache_onnx_evidence(evidence_dir / f"semantic-cache-onnx-v{version}.json", version)
    _write_skill_system_evidence(evidence_dir / f"skills-v{version}.json", version)
    _write_skill_ui_evidence(evidence_dir / f"skills-ui-v{version}.json", version)
    _write_skill_builder_evidence(evidence_dir / f"skill-builder-v{version}.json", version)
    _write_skill_packs_evidence(evidence_dir / f"skill-packs-v{version}.json", version)
    _write_skill_eval_dashboard_evidence(evidence_dir / f"skill-eval-dashboard-v{version}.json", version)
    _write_skill_versioning_evidence(evidence_dir / f"skill-versioning-v{version}.json", version)
    _write_skill_analytics_evidence(evidence_dir / f"skill-analytics-v{version}.json", version)
    _write_skill_security_evidence(evidence_dir / f"skill-security-v{version}.json", version)
    _write_skill_catalog_evidence(evidence_dir / f"skill-catalog-v{version}.json", version)
    (root / "evals").mkdir()
    (root / "evals" / "README.md").write_text(f"适用版本：v{version}。\n", encoding="utf-8")
    reports = root / "evals" / "reports"
    reports.mkdir()
    (reports / "latest.json").write_text(
        json.dumps(
            {
                "version": version,
                "commit": "abc1234",
                "generatedAt": "2026-06-27T00:00:00Z",
                "environment": {"os": "Linux", "python": "3.12", "ci": True},
                "status": "PASS",
                "injection": {"status": "PASS", "gateMode": "hard"},
            }
        ),
        encoding="utf-8",
    )
    (reports / "agent-latest.json").write_text(
        json.dumps(
            {
                "version": version,
                "commit": "abc1234",
                "generatedAt": "2026-06-27T00:00:00Z",
                "environment": {"os": "Linux", "python": "3.12", "ci": True},
                "status": "PASS",
            }
        ),
        encoding="utf-8",
    )
    for name in ("baseline-compare-latest.json", "security-latest.json"):
        (reports / name).write_text(
            json.dumps(
                {
                    "version": version,
                    "commit": "abc1234",
                    "generatedAt": "2026-06-27T00:00:00Z",
                    "environment": {"os": "Linux", "python": "3.12", "ci": True},
                    "status": "PASS",
                }
            ),
            encoding="utf-8",
        )
    (reports / f"skills-v{version}.json").write_text(
        json.dumps(
            {
                "version": version,
                "commit": "abc1234",
                "generatedAt": "2026-06-30T00:00:00Z",
                "environment": {"os": "Linux", "python": "3.12", "ci": True},
                "status": "PASS",
                "summary": {"caseCount": 4, "overallScore": 100.0, "regressionCount": 0},
                "checks": {"packLevelEval": "PASS", "regressionCompare": "PASS"},
            }
        ),
        encoding="utf-8",
    )
    (reports / f"automation-v{version}.json").write_text(
        json.dumps(
            {
                "version": version,
                "commit": "abc1234",
                "generatedAt": "2026-07-04T00:00:00Z",
                "environment": {"os": "Linux", "python": "3.12", "ci": True},
                "status": "PASS",
                "summary": {"caseCount": 3, "checksPassed": 13},
                "checks": {"goldenCasesLoaded": "PASS", "coreActionsCovered": "PASS"},
            }
        ),
        encoding="utf-8",
    )
    scripts = root / "scripts"
    scripts.mkdir()
    if release_exclusions:
        (scripts / "release.py").write_text(
            'EXCLUDED = [".traces", ".local-rag", ".media", ".browser-audit", ".browser-downloads", ".browser-profiles", ".automation"]\n'
            'SECRET = [".auth-token", ".env"]\nLOGS = ["server*.log"]\n',
            encoding="utf-8",
        )
    else:
        (scripts / "release.py").write_text("print('no exclusions here')\n", encoding="utf-8")
    return root


def _write_ga_support(root: Path, version: str) -> None:
    docs = root / "docs"
    for name in (
        "GETTING_STARTED.md",
        "WORKSPACE.md",
        "MEMORY.md",
        "SKILLS.md",
        "MEDIA.md",
        "BROWSER_CONTROL.md",
        "AUTOMATION.md",
        "EXPORTS.md",
        "SECURITY.md",
        "DEPLOYMENT.md",
        "DEMO_3_0.md",
        "EVIDENCE_INDEX.md",
    ):
        (docs / name).write_text(f"# {name}\n\nSee docs/evidence/ga-v{version}.json\n", encoding="utf-8")
    assets = docs / "assets"
    assets.mkdir(exist_ok=True)
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    for name in ("3.0-workspace-overview.png", "3.0-skill-run.png", "3.0-automation-run.png", "3.0-project-export.png"):
        (assets / name).write_bytes(png)
    _write_ga_evidence(root / "docs" / "evidence" / f"ga-v{version}.json", version)
    manifest = root / "deepseek_infra" / "infra" / "diagnostics"
    manifest.mkdir(parents=True)
    (manifest / "release_manifest.py").write_text(f'GA = {{"gaEvidence": "docs/evidence/ga-v{version}.json"}}\n', encoding="utf-8")
    (root / "scripts" / "release.py").write_text(
        'EXCLUDED = [".file-cache", ".projects", ".local-rag", ".traces", ".semantic-cache", ".request-queue", ".generated", ".tool-audit", ".scheduler", ".a2a", ".budget", ".memory", ".reminders", ".agent-runs", ".search-cache", ".auth-token", ".media", ".browser-audit", ".browser-downloads", ".browser-profiles", ".automation", ".skills"]\n'
        'SECRET = [".env"]\nLOGS = ["server*.log"]\n',
        encoding="utf-8",
    )


def _write_ga_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", omit_metadata: str = "") -> None:
    checks = {
        "workspaceHome": "PASS",
        "project": "PASS",
        "memory": "PASS",
        "skill": "PASS",
        "media": "PASS",
        "browserSnapshot": "PASS",
        "savedItem": "PASS",
        "artifact": "PASS",
        "automation": "PASS",
        "export": "PASS",
        "provenance": "PASS",
        "exportRedaction": "PASS",
    }
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "schemaVersion": "ga-smoke.v1",
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-07-05T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_headless_evidence(path: Path, version: str, *, status: str = "PASS", omit_step: str = "", omit_metadata: str = "") -> None:
    steps = [
        {"name": "bridge.start", "status": "pass"},
        {"name": "mcp.initialize", "status": "pass"},
        {"name": "mcp.tools_list", "status": "pass"},
        {"name": "mcp.tools_call", "status": "pass"},
        {"name": "mcp.policy_denial", "status": "pass"},
    ]
    if omit_step:
        steps = [step for step in steps if step["name"] != omit_step]
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-06-27T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "steps": steps,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_a2a_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", peer_type: str = "independent-process", omit_metadata: str = "") -> None:
    checks = {
        "agentCard": "pass",
        "messageSend": "pass",
        "messageStream": "pass",
        "tasksGet": "pass",
        "tasksCancel": "pass",
        "tasksList": "pass",
        "artifactChunks": "pass",
        "sseFinalEvent": "pass",
    }
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-06-27T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "peer": {"name": "peer", "type": peer_type},
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_edge_router_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", omit_metadata: str = "") -> None:
    checks = {
        "ollamaModelsListed": "PASS",
        "openaiCompatibleLocalCall": "PASS",
        "edgeStatusEndpoint": "PASS",
        "fallbackReady": "PASS",
    }
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-06-27T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_edge_router_stabilization_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", omit_metadata: str = "") -> None:
    checks = {
        "edgeDoctor": "PASS",
        "statusShape": "PASS",
        "routePreviewApi": "PASS",
        "fakeProvider": "PASS",
        "routingPolicy": "PASS",
        "fallbackPolicy": "PASS",
        "forcedLocalUnavailable": "PASS",
    }
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-07-03T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_continue_dev_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", omit_metadata: str = "") -> None:
    checks = {
        "configLoaded": "PASS",
        "mcpInitialize": "PASS",
        "toolsList": "PASS",
        "lowRiskToolCall": "PASS",
        "policyDenial": "PASS",
        "promptInjectionClean": "PASS",
    }
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-06-27T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "client": "Continue.dev",
        "clientVersion": "1.2.0",
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_openai_compatible_sdk_evidence(path: Path, version: str, *, status: str = "PASS", omit_sdk_check: str = "", omit_sdk_entirely: str = "", omit_metadata: str = "") -> None:
    sdks = {
        "langchain": {"modelsList": "PASS", "chatCompletion": "PASS", "streaming": "PASS"},
        "litellm": {"modelsList": "PASS", "chatCompletion": "PASS", "streaming": "PASS"},
        "llamaindex": {"chatCompletion": "PASS"},
    }
    if omit_sdk_check:
        parts = omit_sdk_check.split(".", 1)
        if len(parts) == 2 and parts[0] in sdks:
            sdks[parts[0]].pop(parts[1], None)
    if omit_sdk_entirely:
        sdks.pop(omit_sdk_entirely, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-06-27T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "baseUrl": "http://127.0.0.1:8000/v1",
        "model": "deepseek-v4-pro",
        "sdks": sdks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_workspace_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", omit_metadata: str = "") -> None:
    checks = {
        "projectCreate": "PASS",
        "projectRename": "PASS",
        "savedItemCreate": "PASS",
        "artifactList": "PASS",
        "conversationExport": "PASS",
        "projectExportZip": "PASS",
        "secretRedaction": "PASS",
    }
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-06-28T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_context_taint_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", omit_metadata: str = "") -> None:
    checks = {
        "webInjectionScanned": "PASS",
        "fileInjectionScanned": "PASS",
        "mediaTranscriptInjectionScanned": "PASS",
        "toolDirectiveRecognized": "PASS",
        "taintedTurnEscalation": "PASS",
        "riskDiagnosticsPresent": "PASS",
    }
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-07-03T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_skill_system_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", omit_metadata: str = "") -> None:
    checks = {
        "skillApiRoutes": "PASS",
        "builtinSkillsLoad": "PASS",
        "customSkillCreate": "PASS",
        "inputSchemaValidation": "PASS",
        "toolPermissionGate": "PASS",
        "artifactPolicy": "PASS",
        "projectBinding": "PASS",
        "skillExport": "PASS",
    }
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-06-29T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_skill_ui_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", omit_metadata: str = "") -> None:
    checks = {
        "skillWorkbenchEntrypoint": "PASS",
        "skillRunSchemaForm": "PASS",
        "skillCreateEditDelete": "PASS",
        "skillApiActions": "PASS",
        "projectSkillBindingUi": "PASS",
        "skillRunResultLinks": "PASS",
        "skillPanelLifecycle": "PASS",
        "skillPanelStyles": "PASS",
        "reactPwaOwnership": "PASS",
        "skillAppShellCache": "PASS",
        "skillUiAssets": "PASS",
        "skillJsSyntax": "PASS",
        "frontendTypecheckGate": "PASS",
        "ciSyntaxGate": "PASS",
    }
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-06-30T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_media_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", omit_metadata: str = "") -> None:
    checks = {
        "imageImport": "PASS",
        "pdfPageIndex": "PASS",
        "webpageSnapshot": "PASS",
        "mediaSegments": "PASS",
        "mediaToRag": "PASS",
        "mediaCitations": "PASS",
        "mediaUploadLimits": "PASS",
        "projectExportIncludesMedia": "PASS",
        "secretRedaction": "PASS",
    }
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-07-01T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_browser_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", omit_metadata: str = "") -> None:
    checks = {
        "browserSessionCreate": "PASS",
        "readPage": "PASS",
        "screenshot": "PASS",
        "extractLinks": "PASS",
        "unsafeActionBlocked": "PASS",
        "confirmationRequired": "PASS",
        "snapshotToMedia": "PASS",
        "snapshotToRag": "PASS",
        "auditLog": "PASS",
        "redactSecrets": "PASS",
    }
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-07-03T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_automation_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", omit_metadata: str = "") -> None:
    checks = {
        "automationCreate": "PASS",
        "manualRun": "PASS",
        "scheduleTrigger": "PASS",
        "eventTrigger": "PASS",
        "runSkillAction": "PASS",
        "browserReadOnlyAction": "PASS",
        "projectExportAction": "PASS",
        "unsafeActionBlocked": "PASS",
        "runHistory": "PASS",
        "traceLinked": "PASS",
        "artifactOutput": "PASS",
        "templates": "PASS",
        "evidenceGenerated": "PASS",
    }
    if tuple(int(part) for part in version.split(".")[:3]) >= (2, 9, 1):
        checks.update(
            {
                "browserCheckChanged": "PASS",
                "browserCheckUnchanged": "PASS",
                "fixturePathBlocked": "PASS",
                "cronStepRange": "PASS",
                "maxRunsPerDay": "PASS",
                "retryBackoff": "PASS",
                "timeoutEvidence": "PASS",
                "rerun": "PASS",
                "templateCreate": "PASS",
            }
        )
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-07-04T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_skill_builder_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", omit_metadata: str = "") -> None:
    checks = {
        "builderOpen": "PASS",
        "simpleDraftSchema": "PASS",
        "createCustomSkill": "PASS",
        "updateCustomSkill": "PASS",
        "cloneBuiltinSkill": "PASS",
        "visualInputSchemaEdit": "PASS",
        "toolPermissionPicker": "PASS",
        "schemaValidation": "PASS",
        "offlineDryRun": "PASS",
        "builderInputValidation": "PASS",
        "exportApi": "PASS",
        "saveCustomSkill": "PASS",
        "exportCreatedSkill": "PASS",
        "skillBuilderStyles": "PASS",
        "skillBuilderAssets": "PASS",
        "skillBuilderJsSyntax": "PASS",
        "frontendTypecheckGate": "PASS",
        "ciSyntaxGate": "PASS",
    }
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-06-30T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_skill_packs_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", omit_metadata: str = "") -> None:
    checks = {
        "packSchemaValidation": "PASS",
        "builtinPacksLoad": "PASS",
        "packImport": "PASS",
        "packExport": "PASS",
        "skillIdConflictHandling": "PASS",
        "toolPermissionDiff": "PASS",
        "projectPackBinding": "PASS",
        "packInstallDryRun": "PASS",
        "reactSkillSurface": "PASS",
        "frontendTypecheckGate": "PASS",
        "packUiTab": "PASS",
        "packJsSyntax": "PASS",
        "ciSyntaxGate": "PASS",
        "packAssets": "PASS",
    }
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-06-30T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_skill_eval_dashboard_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", omit_metadata: str = "") -> None:
    checks = {
        "evalDashboardEntrypoint": "PASS",
        "reactSkillSurface": "PASS",
        "evalCaseBuilder": "PASS",
        "skillEvalApiActions": "PASS",
        "skillEvalReport": "PASS",
        "packLevelEval": "PASS",
        "regressionCompare": "PASS",
        "evalExportActions": "PASS",
        "skillEvalStyles": "PASS",
        "skillEvalAssets": "PASS",
        "skillEvalRunner": "PASS",
        "frontendTypecheckGate": "PASS",
        "skillEvalJsSyntax": "PASS",
        "ciReleaseGate": "PASS",
    }
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-06-30T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_skill_versioning_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", omit_metadata: str = "") -> None:
    checks = {
        "skillVersionSnapshot": "PASS",
        "skillDiff": "PASS",
        "skillRollback": "PASS",
        "schemaMigrationPlan": "PASS",
        "packVersionInstall": "PASS",
        "packRollback": "PASS",
        "evalAwareUpgradeGate": "PASS",
        "projectBindingMigration": "PASS",
        "versioningApiActions": "PASS",
        "reactSkillSurface": "PASS",
        "versioningUi": "PASS",
        "versioningAssets": "PASS",
        "versioningJsSyntax": "PASS",
        "frontendTypecheckGate": "PASS",
        "ciReleaseGate": "PASS",
    }
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-06-30T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_skill_analytics_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", omit_metadata: str = "") -> None:
    checks = {
        "skillRunHistory": "PASS",
        "runMetadataPersist": "PASS",
        "analyticsSummary": "PASS",
        "failureDiagnostics": "PASS",
        "projectRunHistory": "PASS",
        "traceLink": "PASS",
        "artifactLink": "PASS",
        "retentionCleanup": "PASS",
        "privacyRedaction": "PASS",
        "analyticsApiActions": "PASS",
        "reactSkillSurface": "PASS",
        "analyticsUi": "PASS",
        "analyticsAssets": "PASS",
        "analyticsJsSyntax": "PASS",
        "frontendTypecheckGate": "PASS",
        "ciReleaseGate": "PASS",
    }
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-07-01T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_skill_security_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", omit_metadata: str = "") -> None:
    checks = {
        "securityReview": "PASS",
        "promptInjectionScan": "PASS",
        "secretExfiltrationScan": "PASS",
        "toolGrantRiskDiff": "PASS",
        "trustSkill": "PASS",
        "blockSkill": "PASS",
        "tamperDetection": "PASS",
        "securityManifestExport": "PASS",
        "runSecurityMetadata": "PASS",
        "securityApiActions": "PASS",
        "reactSkillSurface": "PASS",
        "securityUi": "PASS",
        "securityAssets": "PASS",
        "securityJsSyntax": "PASS",
        "frontendTypecheckGate": "PASS",
        "ciReleaseGate": "PASS",
    }
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-07-01T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_skill_catalog_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", omit_metadata: str = "") -> None:
    checks = {
        "catalogManifest": "PASS",
        "catalogList": "PASS",
        "catalogSearch": "PASS",
        "catalogInstallPreview": "PASS",
        "catalogInstall": "PASS",
        "catalogUninstall": "PASS",
        "securityGateBeforeInstall": "PASS",
        "evalScoreShown": "PASS",
        "toolPermissionSummary": "PASS",
        "catalogExport": "PASS",
        "catalogApiActions": "PASS",
        "reactSkillSurface": "PASS",
        "catalogUi": "PASS",
        "catalogAssets": "PASS",
        "catalogJsSyntax": "PASS",
        "frontendTypecheckGate": "PASS",
        "ciReleaseGate": "PASS",
    }
    if omit_check:
        checks.pop(omit_check, None)
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-07-01T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "checks": checks,
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_preflight_all_pass(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.2.9")
    results = preflight.run_preflight(root, "2.2.9")
    assert all(r.status == "pass" for r in results), [r.to_dict() for r in results if r.status != "pass"]
    assert preflight.main(["--root", str(root), "--version", "2.2.9", "--json"]) == 0


def test_preflight_ga_checks_pass(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "3.0.0")
    _write_ga_support(root, "3.0.0")

    results = preflight.run_preflight(root, "3.0.0", ga=True)
    ga_results = {r.name: r for r in results if r.name.startswith("ga_")}

    assert set(ga_results) == {
        "ga_evidence",
        "ga_demo_assets",
        "ga_docs_roster",
        "ga_evidence_index",
        "ga_release_manifest",
        "ga_release_exclusions",
    }
    assert all(r.status == "pass" for r in ga_results.values()), [r.to_dict() for r in ga_results.values() if r.status != "pass"]
    assert preflight.main(["--root", str(root), "--version", "3.0.0", "--ga", "--json"]) == 0


def test_ga_release_manifest_accepts_version_parameterized_path(tmp_path: Path) -> None:
    preflight = _load_preflight()
    manifest = tmp_path / "deepseek_infra" / "infra" / "diagnostics" / "release_manifest.py"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        'GA = {"gaEvidence": f"docs/evidence/ga-v{version}.json"}\n',
        encoding="utf-8",
    )

    result = preflight.check_ga_release_manifest(tmp_path, preflight.APP_VERSION)

    assert result.status == "pass"
    assert result.data["checked"][1] == 'f"docs/evidence/ga-v{version}.json"'


def test_preflight_ga_fails_on_missing_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "3.0.0")
    _write_ga_support(root, "3.0.0")
    (root / "docs" / "evidence" / "ga-v3.0.0.json").unlink()

    result = next(r for r in preflight.run_preflight(root, "3.0.0", ga=True) if r.name == "ga_evidence")

    assert result.status == "fail"
    assert "smoke_ga.py" in result.detail


def test_preflight_ga_fails_on_missing_demo_asset(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "3.0.0")
    _write_ga_support(root, "3.0.0")
    (root / "docs" / "assets" / "3.0-project-export.png").unlink()

    result = next(r for r in preflight.run_preflight(root, "3.0.0", ga=True) if r.name == "ga_demo_assets")

    assert result.status == "fail"
    assert any("3.0-project-export.png" in item for item in result.data["missing"])


def test_preflight_fails_on_badge_mismatch(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.2.8")
    results = preflight.run_preflight(root, "2.2.9")
    badge = next(r for r in results if r.name == "readme_badge")
    assert badge.status == "fail"
    assert preflight.main(["--root", str(root), "--version", "2.2.9"]) == 1


def test_preflight_fails_on_missing_changelog(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.2.9")
    (root / "CHANGELOG.md").write_text("## [2.2.8] - old\n", encoding="utf-8")
    changelog = next(r for r in preflight.run_preflight(root, "2.2.9") if r.name == "changelog")
    assert changelog.status == "fail"


def test_preflight_fails_on_dockerfile_tag(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.2.9")
    (root / "Dockerfile").write_text("docker build -t deepseek-infra:2.2.8 .\n", encoding="utf-8")
    docker = next(r for r in preflight.run_preflight(root, "2.2.9") if r.name == "dockerfile_tag")
    assert docker.status == "fail"


def test_preflight_requires_react_frontend_build(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.2.9")
    (root / "static" / "ui" / "index.html").unlink()
    result = preflight.check_react_frontend_build(root)
    assert result.status == "fail"
    assert "scripts/build_frontend.py" in result.detail


def test_preflight_accepts_react_frontend_build(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.2.9")
    result = preflight.check_react_frontend_build(root)
    assert result.status == "pass"


def test_preflight_fails_on_doc_version(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.2.9")
    (root / "docs" / "IMPLEMENTATION_STATUS.md").write_text("适用版本：v2.2.8。\n", encoding="utf-8")
    doc = next(r for r in preflight.run_preflight(root, "2.2.9") if r.name == "doc_version:docs/IMPLEMENTATION_STATUS.md")
    assert doc.status == "fail"


def test_preflight_fails_on_eval_report_version(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.2.9")
    (root / "evals" / "reports" / "latest.json").write_text(
        json.dumps(
            {
                "version": "2.2.8",
                "commit": "abc1234",
                "generatedAt": "2026-06-27T00:00:00Z",
                "environment": {"os": "Linux", "python": "3.12", "ci": True},
                "status": "PASS",
            }
        ),
        encoding="utf-8",
    )
    report = next(r for r in preflight.run_preflight(root, "2.2.9") if r.name == "eval_report")
    assert report.status == "fail"


def test_preflight_warns_on_missing_eval_report(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.2.9")
    (root / "evals" / "reports" / "latest.json").unlink()
    report = next(r for r in preflight.run_preflight(root, "2.2.9") if r.name == "eval_report")
    assert report.status == "warn"


def test_preflight_fails_on_agent_report_version(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.2.9")
    (root / "evals" / "reports" / "agent-latest.json").write_text(
        json.dumps(
            {
                "version": "2.2.8",
                "commit": "abc1234",
                "generatedAt": "2026-06-27T00:00:00Z",
                "environment": {"os": "Linux", "python": "3.12", "ci": True},
                "status": "PASS",
            }
        ),
        encoding="utf-8",
    )
    report = next(r for r in preflight.run_preflight(root, "2.2.9") if r.name == "agent_report")
    assert report.status == "fail"


def test_preflight_fails_on_release_exclusions_removed(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.2.9", release_exclusions=False)
    exclusions = next(r for r in preflight.run_preflight(root, "2.2.9") if r.name == "release_exclusions")
    assert exclusions.status == "fail"


def test_preflight_fails_on_unparsable_agent_report(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.2.9")
    (root / "evals" / "reports" / "agent-latest.json").write_text("{not json", encoding="utf-8")
    report = next(r for r in preflight.run_preflight(root, "2.2.9") if r.name == "agent_report")
    assert report.status == "fail"


def test_preflight_fails_on_missing_docs(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.2.9")
    (root / "docs" / "AGENT_EVAL.md").unlink()
    links = next(r for r in preflight.run_preflight(root, "2.2.9") if r.name == "doc_links")
    assert links.status == "fail"


def test_preflight_fails_on_missing_headless_mcp_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.3.2")
    (root / "docs" / "evidence" / "headless-mcp-bridge.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.3.2") if r.name == "headless_mcp_bridge_evidence")
    assert result.status == "fail"
    assert preflight.main(["--root", str(root), "--version", "2.3.2"]) == 1


def test_preflight_fails_on_incomplete_headless_mcp_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.3.2")
    _write_headless_evidence(root / "docs" / "evidence" / "headless-mcp-bridge.json", "2.3.2", omit_step="mcp.policy_denial")
    result = next(r for r in preflight.run_preflight(root, "2.3.2") if r.name == "headless_mcp_bridge_evidence")
    assert result.status == "fail"
    assert "mcp.policy_denial" in result.detail


def test_preflight_fails_on_missing_a2a_external_peer_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.3.3")
    (root / "docs" / "evidence" / "a2a-external-peer.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.3.3") if r.name == "a2a_external_peer_evidence")
    assert result.status == "fail"
    assert preflight.main(["--root", str(root), "--version", "2.3.3"]) == 1


def test_preflight_fails_on_incomplete_a2a_external_peer_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.3.3")
    _write_a2a_evidence(root / "docs" / "evidence" / "a2a-external-peer.json", "2.3.3", omit_check="artifactChunks")
    result = next(r for r in preflight.run_preflight(root, "2.3.3") if r.name == "a2a_external_peer_evidence")
    assert result.status == "fail"
    assert "artifactChunks" in result.detail


def test_preflight_warns_on_missing_a2a_third_party_peer_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.3.3")
    (root / "docs" / "evidence" / "a2a-third-party-peer.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.3.3") if r.name == "a2a_third_party_peer_evidence")
    assert result.status == "warn"
    assert preflight.main(["--root", str(root), "--version", "2.3.3"]) == 0


def test_preflight_fails_on_a2a_third_party_peer_non_pass_status(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.5")
    _write_a2a_evidence(root / "docs" / "evidence" / "a2a-third-party-peer.json", "2.4.5", status="FAIL", peer_type="third-party")
    result = next(r for r in preflight.run_preflight(root, "2.4.5") if r.name == "a2a_third_party_peer_evidence")
    assert result.status == "fail"
    assert "expected PASS" in result.detail


def test_preflight_fails_on_a2a_third_party_peer_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.5")
    _write_a2a_evidence(root / "docs" / "evidence" / "a2a-third-party-peer.json", "2.4.5", omit_check="sseFinalEvent", peer_type="third-party")
    result = next(r for r in preflight.run_preflight(root, "2.4.5") if r.name == "a2a_third_party_peer_evidence")
    assert result.status == "fail"
    assert "sseFinalEvent" in result.detail


def test_preflight_fails_on_a2a_third_party_peer_missing_metadata(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.5")
    _write_a2a_evidence(root / "docs" / "evidence" / "a2a-third-party-peer.json", "2.4.5", peer_type="third-party", omit_metadata="environment")
    result = next(r for r in preflight.run_preflight(root, "2.4.5") if r.name == "evidence_metadata:a2a_third_party_peer")
    assert result.status == "fail"
    assert "environment" in result.detail


def test_preflight_fails_on_a2a_third_party_peer_wrong_type(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.5")
    _write_a2a_evidence(root / "docs" / "evidence" / "a2a-third-party-peer.json", "2.4.5", peer_type="adapter")
    result = next(r for r in preflight.run_preflight(root, "2.4.5") if r.name == "a2a_third_party_peer_evidence")
    assert result.status == "fail"
    assert "peerType" in result.detail


def test_preflight_warns_on_missing_edge_router_smoke_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.3")
    (root / "docs" / "evidence" / "edge-router-smoke.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.4.3") if r.name == "edge_router_smoke_evidence")
    assert result.status == "warn"
    assert preflight.main(["--root", str(root), "--version", "2.4.3"]) == 0


def test_preflight_fails_on_edge_router_smoke_non_pass_status(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.3")
    _write_edge_router_evidence(root / "docs" / "evidence" / "edge-router-smoke.json", "2.4.3", status="WARNING")
    result = next(r for r in preflight.run_preflight(root, "2.4.3") if r.name == "edge_router_smoke_evidence")
    assert result.status == "fail"
    assert "expected PASS" in result.detail


def test_preflight_fails_on_edge_router_smoke_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.3")
    _write_edge_router_evidence(root / "docs" / "evidence" / "edge-router-smoke.json", "2.4.3", omit_check="fallbackReady")
    result = next(r for r in preflight.run_preflight(root, "2.4.3") if r.name == "edge_router_smoke_evidence")
    assert result.status == "fail"
    assert "fallbackReady" in result.detail


def test_preflight_fails_on_edge_router_smoke_missing_metadata(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.3")
    _write_edge_router_evidence(root / "docs" / "evidence" / "edge-router-smoke.json", "2.4.3", omit_metadata="environment")
    result = next(r for r in preflight.run_preflight(root, "2.4.3") if r.name == "evidence_metadata:edge_router_smoke")
    assert result.status == "fail"
    assert "environment" in result.detail


def test_preflight_warns_on_stale_optional_evidence_version(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.3")
    _write_edge_router_evidence(root / "docs" / "evidence" / "edge-router-smoke.json", "2.6.0")
    result = next(r for r in preflight.run_preflight(root, "2.6.3") if r.name == "edge_router_smoke_evidence")
    assert result.status == "warn"
    assert "refresh this optional evidence" in result.detail
    assert preflight.main(["--root", str(root), "--version", "2.6.3"]) == 0


def test_preflight_fails_on_missing_edge_router_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.7.3")
    (root / "docs" / "evidence" / "edge-router-v2.7.3.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.7.3") if r.name == "edge_router_evidence")
    assert result.status == "fail"
    assert "smoke_edge_router.py" in result.detail


def test_preflight_fails_on_edge_router_evidence_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.7.3")
    _write_edge_router_stabilization_evidence(root / "docs" / "evidence" / "edge-router-v2.7.3.json", "2.7.3", omit_check="routingPolicy")
    result = next(r for r in preflight.run_preflight(root, "2.7.3") if r.name == "edge_router_evidence")
    assert result.status == "fail"
    assert "routingPolicy" in result.detail


def test_preflight_passes_on_edge_router_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.7.3")
    result = next(r for r in preflight.run_preflight(root, "2.7.3") if r.name == "edge_router_evidence")
    assert result.status == "pass"


def _skeleton_with_compat(tmp_path: Path, version: str, *, claude_status: str, cursor_status: str) -> Path:
    root = _skeleton(tmp_path, version)
    compat_lines = [
        "# Compatibility Matrix",
        "",
        f"适用版本：v{version}。",
        "",
        "## MCP Client Compatibility",
        "",
        "| Client / Path | Status | Evidence | Notes |",
        "| --- | --- | --- | --- |",
        f"| Claude Desktop | {claude_status} | integrations/claude-desktop.md | notes |",
        f"| Cursor | {cursor_status} | integrations/cursor.md | notes |",
        "",
    ]
    (root / "docs" / "COMPATIBILITY.md").write_text("\n".join(compat_lines), encoding="utf-8")
    return root


def test_preflight_warns_on_pending_gui_interop_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton_with_compat(tmp_path, "2.3.1", claude_status="🟡 Config documented", cursor_status="🟡 Config documented")
    result = next(r for r in preflight.run_preflight(root, "2.3.1") if r.name == "gui_interop_evidence")
    assert result.status == "warn"
    assert "Claude Desktop" in result.detail and "Cursor" in result.detail
    # WARNING does not fail the preflight exit code
    assert preflight.main(["--root", str(root), "--version", "2.3.1", "--json"]) == 0


def test_preflight_passes_on_completed_gui_interop_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton_with_compat(tmp_path, "2.3.1", claude_status="✅ GUI tested", cursor_status="✅ GUI tested")
    result = next(r for r in preflight.run_preflight(root, "2.3.1") if r.name == "gui_interop_evidence")
    assert result.status == "pass"


def test_preflight_warns_when_only_one_gui_evidence_filled(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton_with_compat(tmp_path, "2.3.1", claude_status="✅ GUI tested", cursor_status="🟡 Config documented")
    result = next(r for r in preflight.run_preflight(root, "2.3.1") if r.name == "gui_interop_evidence")
    assert result.status == "warn"
    assert "Cursor" in result.detail
    assert "Claude Desktop" not in result.detail


def test_preflight_fails_when_docs_encoding_is_corrupt(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.3.4")
    (root / "CHANGELOG.md").write_text("## [2.3.3]\n\n**???A2A ?? peer**\n", encoding="utf-8")
    result = next(r for r in preflight.run_preflight(root, "2.3.4") if r.name == "docs_encoding_sanity")
    assert result.status == "fail"
    assert preflight.main(["--root", str(root), "--version", "2.3.4"]) == 1


def test_preflight_passes_when_docs_encoding_is_clean(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.3.4")
    result = next(r for r in preflight.run_preflight(root, "2.3.4") if r.name == "docs_encoding_sanity")
    assert result.status == "pass"


def test_preflight_fails_when_headless_mcp_evidence_missing_metadata(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.3.4")
    _write_headless_evidence(root / "docs" / "evidence" / "headless-mcp-bridge.json", "2.3.4", omit_metadata="environment")
    result = next(r for r in preflight.run_preflight(root, "2.3.4") if r.name == "evidence_metadata:headless_mcp_bridge")
    assert result.status == "fail"
    assert "environment" in result.detail


def test_preflight_fails_when_a2a_external_peer_evidence_missing_metadata(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.3.4")
    _write_a2a_evidence(root / "docs" / "evidence" / "a2a-external-peer.json", "2.3.4", omit_metadata="commit")
    result = next(r for r in preflight.run_preflight(root, "2.3.4") if r.name == "evidence_metadata:a2a_external_peer")
    assert result.status == "fail"
    assert "commit" in result.detail


def test_preflight_accepts_source_revision_block_as_revision_identity(tmp_path: Path) -> None:
    preflight = _load_preflight()
    path = tmp_path / "docs" / "evidence" / "frontend-bundle-v2.3.4.json"
    path.parent.mkdir(parents=True)
    payload = {
        "version": "2.3.4",
        "sourceRevision": "abc1234def",
        "sourceTreeDirty": True,
        "releaseRevision": None,
        "ciRevision": None,
        "generatedAt": "2026-07-19T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": "PASS",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert preflight._check_evidence_metadata("frontend_bundle", payload, path) is None

    payload.pop("sourceRevision")
    payload.pop("commit", None)
    result = preflight._check_evidence_metadata("frontend_bundle", payload, path)
    assert result is not None and result.status == "fail"
    assert "sourceRevision|commit" in result.detail


def test_preflight_fails_when_eval_report_missing_metadata(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.3.4")
    (root / "evals" / "reports" / "latest.json").write_text(
        json.dumps({"version": "2.3.4", "status": "PASS"}), encoding="utf-8"
    )
    result = next(r for r in preflight.run_preflight(root, "2.3.4") if r.name == "evidence_metadata:eval_report")
    assert result.status == "fail"


def test_preflight_fails_when_agent_report_missing_metadata(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.3.4")
    (root / "evals" / "reports" / "agent-latest.json").write_text(
        json.dumps({"version": "2.3.4", "status": "PASS"}), encoding="utf-8"
    )
    result = next(r for r in preflight.run_preflight(root, "2.3.4") if r.name == "evidence_metadata:agent_report")
    assert result.status == "fail"


def test_preflight_fails_when_security_corpus_report_is_missing(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.2")
    (root / "evals" / "reports" / "security-latest.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.4.2") if r.name == "security_corpus_report")
    assert result.status == "fail"


def test_preflight_fails_when_quality_gate_evidence_regresses(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.2")
    (root / "pyproject.toml").write_text("[tool.coverage.report]\nfail_under = 94\n", encoding="utf-8")
    result = next(r for r in preflight.run_preflight(root, "2.4.2") if r.name == "quality_gate_evidence")
    assert result.status == "fail"
    assert "coverage fail_under" in result.detail


def test_preflight_warns_on_missing_continue_dev_mcp_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.5")
    (root / "docs" / "evidence" / "continue-dev-mcp.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.4.5") if r.name == "continue_dev_mcp_evidence")
    assert result.status == "warn"
    assert preflight.main(["--root", str(root), "--version", "2.4.5"]) == 0


def test_preflight_fails_on_continue_dev_mcp_non_pass_status(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.5")
    _write_continue_dev_evidence(root / "docs" / "evidence" / "continue-dev-mcp.json", "2.4.5", status="FAIL")
    result = next(r for r in preflight.run_preflight(root, "2.4.5") if r.name == "continue_dev_mcp_evidence")
    assert result.status == "fail"
    assert "expected PASS" in result.detail


def test_preflight_fails_on_continue_dev_mcp_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.5")
    _write_continue_dev_evidence(root / "docs" / "evidence" / "continue-dev-mcp.json", "2.4.5", omit_check="policyDenial")
    result = next(r for r in preflight.run_preflight(root, "2.4.5") if r.name == "continue_dev_mcp_evidence")
    assert result.status == "fail"
    assert "policyDenial" in result.detail


def test_preflight_fails_on_continue_dev_mcp_missing_metadata(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.5")
    _write_continue_dev_evidence(root / "docs" / "evidence" / "continue-dev-mcp.json", "2.4.5", omit_metadata="environment")
    result = next(r for r in preflight.run_preflight(root, "2.4.5") if r.name == "evidence_metadata:continue_dev_mcp")
    assert result.status == "fail"
    assert "environment" in result.detail


def test_preflight_passes_on_continue_dev_mcp_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.5")
    result = next(r for r in preflight.run_preflight(root, "2.4.5") if r.name == "continue_dev_mcp_evidence")
    assert result.status == "pass"


def test_preflight_warns_on_missing_openai_compatible_sdk_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.6")
    (root / "docs" / "evidence" / "openai-compatible-sdks.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.4.6") if r.name == "openai_compatible_sdk_evidence")
    assert result.status == "warn"
    assert preflight.main(["--root", str(root), "--version", "2.4.6"]) == 0


def test_preflight_fails_on_openai_compatible_sdk_non_pass_status(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.6")
    _write_openai_compatible_sdk_evidence(root / "docs" / "evidence" / "openai-compatible-sdks.json", "2.4.6", status="FAIL")
    result = next(r for r in preflight.run_preflight(root, "2.4.6") if r.name == "openai_compatible_sdk_evidence")
    assert result.status == "fail"
    assert "expected PASS" in result.detail


def test_preflight_fails_on_openai_compatible_sdk_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.6")
    _write_openai_compatible_sdk_evidence(root / "docs" / "evidence" / "openai-compatible-sdks.json", "2.4.6", omit_sdk_check="langchain.streaming")
    result = next(r for r in preflight.run_preflight(root, "2.4.6") if r.name == "openai_compatible_sdk_evidence")
    assert result.status == "fail"
    assert "streaming" in result.detail


def test_preflight_fails_on_openai_compatible_sdk_missing_metadata(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.6")
    _write_openai_compatible_sdk_evidence(root / "docs" / "evidence" / "openai-compatible-sdks.json", "2.4.6", omit_metadata="environment")
    result = next(r for r in preflight.run_preflight(root, "2.4.6") if r.name == "evidence_metadata:openai_compatible_sdk")
    assert result.status == "fail"
    assert "environment" in result.detail


def test_preflight_passes_on_openai_compatible_sdk_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.4.6")
    result = next(r for r in preflight.run_preflight(root, "2.4.6") if r.name == "openai_compatible_sdk_evidence")
    assert result.status == "pass"


def test_preflight_fails_on_missing_workspace_core_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.3")
    (root / "docs" / "evidence" / "workspace-v2.6.3.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.6.3") if r.name == "workspace_core_evidence")
    assert result.status == "fail"
    assert "smoke_workspace.py" in result.detail


def test_preflight_fails_on_workspace_core_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.3")
    _write_workspace_evidence(root / "docs" / "evidence" / "workspace-v2.6.3.json", "2.6.3", omit_check="projectExportZip")
    result = next(r for r in preflight.run_preflight(root, "2.6.3") if r.name == "workspace_core_evidence")
    assert result.status == "fail"
    assert "projectExportZip" in result.detail


def test_preflight_passes_on_workspace_core_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.3")
    result = next(r for r in preflight.run_preflight(root, "2.6.3") if r.name == "workspace_core_evidence")
    assert result.status == "pass"


def test_preflight_fails_on_missing_context_taint_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.8.0")
    (root / "docs" / "evidence" / "context-taint-v2.8.0.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.8.0") if r.name == "context_taint_evidence")
    assert result.status == "fail"
    assert "smoke_context_taint.py" in result.detail


def test_preflight_fails_on_context_taint_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.8.0")
    _write_context_taint_evidence(root / "docs" / "evidence" / "context-taint-v2.8.0.json", "2.8.0", omit_check="mediaTranscriptInjectionScanned")
    result = next(r for r in preflight.run_preflight(root, "2.8.0") if r.name == "context_taint_evidence")
    assert result.status == "fail"
    assert "mediaTranscriptInjectionScanned" in result.detail


def test_preflight_passes_on_context_taint_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.8.0")
    result = next(r for r in preflight.run_preflight(root, "2.8.0") if r.name == "context_taint_evidence")
    assert result.status == "pass"


def test_preflight_fails_on_missing_media_layer_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.7.3")
    (root / "docs" / "evidence" / "media-v2.7.3.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.7.3") if r.name == "media_layer_evidence")
    assert result.status == "fail"
    assert "smoke_media.py" in result.detail


def test_preflight_fails_on_media_layer_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.7.3")
    _write_media_evidence(root / "docs" / "evidence" / "media-v2.7.3.json", "2.7.3", omit_check="mediaToRag")
    result = next(r for r in preflight.run_preflight(root, "2.7.3") if r.name == "media_layer_evidence")
    assert result.status == "fail"
    assert "mediaToRag" in result.detail


def test_preflight_passes_on_media_layer_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.7.3")
    result = next(r for r in preflight.run_preflight(root, "2.7.3") if r.name == "media_layer_evidence")
    assert result.status == "pass"


def test_preflight_fails_on_missing_browser_control_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.8.0")
    (root / "docs" / "evidence" / "browser-v2.8.0.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.8.0") if r.name == "browser_control_evidence")
    assert result.status == "fail"
    assert "smoke_browser.py" in result.detail


def test_preflight_fails_on_browser_control_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.8.0")
    _write_browser_evidence(root / "docs" / "evidence" / "browser-v2.8.0.json", "2.8.0", omit_check="snapshotToRag")
    result = next(r for r in preflight.run_preflight(root, "2.8.0") if r.name == "browser_control_evidence")
    assert result.status == "fail"
    assert "snapshotToRag" in result.detail


def test_preflight_passes_on_browser_control_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.8.0")
    result = next(r for r in preflight.run_preflight(root, "2.8.0") if r.name == "browser_control_evidence")
    assert result.status == "pass"


def test_preflight_fails_on_missing_automation_runtime_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.9.0")
    (root / "docs" / "evidence" / "automation-v2.9.0.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.9.0") if r.name == "automation_runtime_evidence")
    assert result.status == "fail"
    assert "smoke_automation.py" in result.detail


def test_preflight_fails_on_automation_runtime_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.9.0")
    _write_automation_evidence(root / "docs" / "evidence" / "automation-v2.9.0.json", "2.9.0", omit_check="unsafeActionBlocked")
    result = next(r for r in preflight.run_preflight(root, "2.9.0") if r.name == "automation_runtime_evidence")
    assert result.status == "fail"
    assert "unsafeActionBlocked" in result.detail


def test_preflight_passes_on_automation_runtime_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.9.0")
    result = next(r for r in preflight.run_preflight(root, "2.9.0") if r.name == "automation_runtime_evidence")
    assert result.status == "pass"


def test_preflight_requires_automation_hardening_evidence_for_2_9_1(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.9.1")
    _write_automation_evidence(root / "docs" / "evidence" / "automation-v2.9.1.json", "2.9.1", omit_check="fixturePathBlocked")
    result = next(r for r in preflight.run_preflight(root, "2.9.1") if r.name == "automation_runtime_evidence")
    assert result.status == "fail"
    assert "fixturePathBlocked" in result.detail


def test_preflight_fails_on_missing_skill_system_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.3")
    (root / "docs" / "evidence" / "skills-v2.6.3.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.6.3") if r.name == "skill_system_evidence")
    assert result.status == "fail"
    assert "smoke_skills.py" in result.detail


def test_preflight_fails_on_skill_system_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.3")
    _write_skill_system_evidence(root / "docs" / "evidence" / "skills-v2.6.3.json", "2.6.3", omit_check="skillApiRoutes")
    result = next(r for r in preflight.run_preflight(root, "2.6.3") if r.name == "skill_system_evidence")
    assert result.status == "fail"
    assert "skillApiRoutes" in result.detail


def test_preflight_passes_on_skill_system_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.3")
    result = next(r for r in preflight.run_preflight(root, "2.6.3") if r.name == "skill_system_evidence")
    assert result.status == "pass"


def test_preflight_fails_on_missing_skill_ui_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.3")
    (root / "docs" / "evidence" / "skills-ui-v2.6.3.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.6.3") if r.name == "skill_ui_evidence")
    assert result.status == "fail"
    assert "smoke_skills_ui.py" in result.detail


def test_preflight_fails_on_skill_ui_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.3")
    _write_skill_ui_evidence(root / "docs" / "evidence" / "skills-ui-v2.6.3.json", "2.6.3", omit_check="skillCreateEditDelete")
    result = next(r for r in preflight.run_preflight(root, "2.6.3") if r.name == "skill_ui_evidence")
    assert result.status == "fail"
    assert "skillCreateEditDelete" in result.detail


def test_preflight_passes_on_skill_ui_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.3")
    result = next(r for r in preflight.run_preflight(root, "2.6.3") if r.name == "skill_ui_evidence")
    assert result.status == "pass"


def test_preflight_fails_on_missing_skill_builder_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.3")
    (root / "docs" / "evidence" / "skill-builder-v2.6.3.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.6.3") if r.name == "skill_builder_evidence")
    assert result.status == "fail"
    assert "smoke_skill_builder.py" in result.detail


def test_preflight_fails_on_skill_builder_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.3")
    _write_skill_builder_evidence(root / "docs" / "evidence" / "skill-builder-v2.6.3.json", "2.6.3", omit_check="offlineDryRun")
    result = next(r for r in preflight.run_preflight(root, "2.6.3") if r.name == "skill_builder_evidence")
    assert result.status == "fail"
    assert "offlineDryRun" in result.detail


def test_preflight_passes_on_skill_builder_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.3")
    result = next(r for r in preflight.run_preflight(root, "2.6.3") if r.name == "skill_builder_evidence")
    assert result.status == "pass"


def test_preflight_fails_on_missing_skill_packs_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.6")
    (root / "docs" / "evidence" / "skill-packs-v2.6.6.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.6.6") if r.name == "skill_packs_evidence")
    assert result.status == "fail"
    assert "smoke_skill_packs.py" in result.detail


def test_preflight_fails_on_skill_packs_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.6")
    _write_skill_packs_evidence(root / "docs" / "evidence" / "skill-packs-v2.6.6.json", "2.6.6", omit_check="skillIdConflictHandling")
    result = next(r for r in preflight.run_preflight(root, "2.6.6") if r.name == "skill_packs_evidence")
    assert result.status == "fail"
    assert "skillIdConflictHandling" in result.detail


def test_preflight_passes_on_skill_packs_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.6")
    result = next(r for r in preflight.run_preflight(root, "2.6.6") if r.name == "skill_packs_evidence")
    assert result.status == "pass"


def test_preflight_fails_on_missing_skill_eval_dashboard_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.6")
    (root / "docs" / "evidence" / "skill-eval-dashboard-v2.6.6.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.6.6") if r.name == "skill_eval_dashboard_evidence")
    assert result.status == "fail"
    assert "smoke_skill_eval_dashboard.py" in result.detail


def test_preflight_fails_on_skill_eval_dashboard_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.6")
    _write_skill_eval_dashboard_evidence(
        root / "docs" / "evidence" / "skill-eval-dashboard-v2.6.6.json",
        "2.6.6",
        omit_check="regressionCompare",
    )
    result = next(r for r in preflight.run_preflight(root, "2.6.6") if r.name == "skill_eval_dashboard_evidence")
    assert result.status == "fail"
    assert "regressionCompare" in result.detail


def test_preflight_passes_on_skill_eval_dashboard_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.6")
    result = next(r for r in preflight.run_preflight(root, "2.6.6") if r.name == "skill_eval_dashboard_evidence")
    assert result.status == "pass"


def test_preflight_fails_on_missing_skill_eval_report(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.6")
    (root / "evals" / "reports" / "skills-v2.6.6.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.6.6") if r.name == "quality_gate_evidence")
    assert result.status == "fail"
    assert "skills-v2.6.6.json" in result.detail


def test_preflight_fails_on_missing_skill_versioning_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.6")
    (root / "docs" / "evidence" / "skill-versioning-v2.6.6.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.6.6") if r.name == "skill_versioning_evidence")
    assert result.status == "fail"
    assert "smoke_skill_versioning.py" in result.detail


def test_preflight_fails_on_skill_versioning_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.6")
    _write_skill_versioning_evidence(
        root / "docs" / "evidence" / "skill-versioning-v2.6.6.json",
        "2.6.6",
        omit_check="evalAwareUpgradeGate",
    )
    result = next(r for r in preflight.run_preflight(root, "2.6.6") if r.name == "skill_versioning_evidence")
    assert result.status == "fail"
    assert "evalAwareUpgradeGate" in result.detail


def test_preflight_passes_on_skill_versioning_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.6")
    result = next(r for r in preflight.run_preflight(root, "2.6.6") if r.name == "skill_versioning_evidence")
    assert result.status == "pass"


def test_preflight_fails_on_missing_skill_analytics_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.7")
    (root / "docs" / "evidence" / "skill-analytics-v2.6.7.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.6.7") if r.name == "skill_analytics_evidence")
    assert result.status == "fail"
    assert "smoke_skill_analytics.py" in result.detail


def test_preflight_fails_on_skill_analytics_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.7")
    _write_skill_analytics_evidence(
        root / "docs" / "evidence" / "skill-analytics-v2.6.7.json",
        "2.6.7",
        omit_check="privacyRedaction",
    )
    result = next(r for r in preflight.run_preflight(root, "2.6.7") if r.name == "skill_analytics_evidence")
    assert result.status == "fail"
    assert "privacyRedaction" in result.detail


def test_preflight_passes_on_skill_analytics_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.7")
    result = next(r for r in preflight.run_preflight(root, "2.6.7") if r.name == "skill_analytics_evidence")
    assert result.status == "pass"


def test_preflight_fails_on_missing_skill_security_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.8")
    (root / "docs" / "evidence" / "skill-security-v2.6.8.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.6.8") if r.name == "skill_security_evidence")
    assert result.status == "fail"
    assert "smoke_skill_security.py" in result.detail


def test_preflight_fails_on_skill_security_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.8")
    _write_skill_security_evidence(
        root / "docs" / "evidence" / "skill-security-v2.6.8.json",
        "2.6.8",
        omit_check="tamperDetection",
    )
    result = next(r for r in preflight.run_preflight(root, "2.6.8") if r.name == "skill_security_evidence")
    assert result.status == "fail"
    assert "tamperDetection" in result.detail


def test_preflight_passes_on_skill_security_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.8")
    result = next(r for r in preflight.run_preflight(root, "2.6.8") if r.name == "skill_security_evidence")
    assert result.status == "pass"


def test_preflight_fails_on_missing_skill_catalog_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.9")
    (root / "docs" / "evidence" / "skill-catalog-v2.6.9.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.6.9") if r.name == "skill_catalog_evidence")
    assert result.status == "fail"
    assert "smoke_skill_catalog.py" in result.detail


def test_preflight_fails_on_skill_catalog_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.9")
    _write_skill_catalog_evidence(
        root / "docs" / "evidence" / "skill-catalog-v2.6.9.json",
        "2.6.9",
        omit_check="securityGateBeforeInstall",
    )
    result = next(r for r in preflight.run_preflight(root, "2.6.9") if r.name == "skill_catalog_evidence")
    assert result.status == "fail"
    assert "securityGateBeforeInstall" in result.detail


def test_preflight_passes_on_skill_catalog_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.9")
    result = next(r for r in preflight.run_preflight(root, "2.6.9") if r.name == "skill_catalog_evidence")
    assert result.status == "pass"


def _write_semantic_cache_onnx_evidence(path: Path, version: str, *, status: str = "PASS", omit_metadata: str = "") -> None:
    payload: dict[str, Any] = {
        "version": version,
        "commit": "abc1234",
        "generatedAt": "2026-06-27T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": status,
        "hash": {"exactHitRate": 1.0, "paraphraseHitRate": 0.0, "unrelatedFalseHitRate": 0.0},
        "onnxAvailable": False,
        "decision": "hash is zero-dependency default",
    }
    if omit_metadata:
        payload.pop(omit_metadata, None)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_preflight_warns_on_missing_semantic_cache_onnx_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.3")
    (root / "docs" / "evidence" / "semantic-cache-onnx-v2.6.3.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.6.3") if r.name == "semantic_cache_onnx_evidence")
    assert result.status == "warn"
    assert preflight.main(["--root", str(root), "--version", "2.6.3"]) == 0


def test_preflight_fails_on_semantic_cache_onnx_non_pass_status(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.3")
    _write_semantic_cache_onnx_evidence(root / "docs" / "evidence" / "semantic-cache-onnx-v2.6.3.json", "2.6.3", status="FAIL")
    result = next(r for r in preflight.run_preflight(root, "2.6.3") if r.name == "semantic_cache_onnx_evidence")
    assert result.status == "fail"
    assert "expected PASS" in result.detail


def test_preflight_fails_on_semantic_cache_onnx_missing_metadata(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.3")
    _write_semantic_cache_onnx_evidence(root / "docs" / "evidence" / "semantic-cache-onnx-v2.6.3.json", "2.6.3", omit_metadata="environment")
    result = next(r for r in preflight.run_preflight(root, "2.6.3") if r.name == "evidence_metadata:semantic_cache_onnx")
    assert result.status == "fail"
    assert "environment" in result.detail


def test_preflight_passes_on_semantic_cache_onnx_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.6.3")
    result = next(r for r in preflight.run_preflight(root, "2.6.3") if r.name == "semantic_cache_onnx_evidence")
    assert result.status == "pass"


def test_frontend_browser_evidence_requires_complete_chromium_checks(tmp_path: Path) -> None:
    preflight = _load_preflight()
    evidence = tmp_path / "docs" / "evidence"
    evidence.mkdir(parents=True)
    path = evidence / "frontend-browser-v4.1.0.json"
    checks = {
        "cspHeader": "PASS",
        "reactOnlyRoot": "PASS",
        "legacyRouteRetired": "PASS",
        "uploadCancel": "PASS",
        "rootSpaDeepLink": "PASS",
        "reactChatVerticalSlice": "PASS",
        "reactHistoryPersistence": "PASS",
        "reactStopGeneration": "PASS",
        "completeAppShell": "PASS",
        "offlineRefresh": "PASS",
        "noCspConsoleErrors": "PASS",
        "reactTraceRouteRefresh": "PASS",
        "traceChunkDeferred": "PASS",
        "traceRouteProviderIsolation": "PASS",
    }
    path.write_text(
        json.dumps(
            {
                "version": "4.1.0",
                "commit": "abc1234",
                "generatedAt": "2026-07-16T00:00:00Z",
                "environment": {"os": "Linux", "python": "3.12", "ci": True},
                "status": "PASS",
                "browser": "chromium",
                "checks": checks,
            }
        ),
        encoding="utf-8",
    )

    result = preflight.check_frontend_browser_evidence(tmp_path, "4.1.0")
    assert result.status == "pass"

    checks["reactStopGeneration"] = "FAIL"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checks"] = checks
    path.write_text(json.dumps(data), encoding="utf-8")
    result = preflight.check_frontend_browser_evidence(tmp_path, "4.1.0")
    assert result.status == "fail"
    assert "reactStopGeneration" in result.detail


def test_frontend_browser_evidence_requires_runtime_decomposition_checks_from_4_1_0(tmp_path: Path) -> None:
    preflight = _load_preflight()
    evidence = tmp_path / "docs" / "evidence"
    evidence.mkdir(parents=True)
    path = evidence / "frontend-browser-v4.1.0.json"
    path.write_text(
        json.dumps(
            {
                "version": "4.1.0",
                "commit": "abc1234",
                "generatedAt": "2026-07-19T00:00:00Z",
                "environment": {"os": "Linux", "python": "3.12", "ci": True},
                "status": "PASS",
                "browser": "chromium",
                "checks": {
                    "cspHeader": "PASS",
                    "reactOnlyRoot": "PASS",
                    "legacyRouteRetired": "PASS",
                    "uploadCancel": "PASS",
                    "rootSpaDeepLink": "PASS",
                    "reactChatVerticalSlice": "PASS",
                    "reactHistoryPersistence": "PASS",
                    "reactStopGeneration": "PASS",
                    "completeAppShell": "PASS",
                    "offlineRefresh": "PASS",
                    "noCspConsoleErrors": "PASS",
                    "reactTraceRouteRefresh": "PASS",
                    "traceChunkDeferred": "PASS",
                },
            }
        ),
        encoding="utf-8",
    )

    result = preflight.check_frontend_browser_evidence(tmp_path, "4.1.0")
    assert result.status == "fail"
    assert "traceRouteProviderIsolation" in result.detail


def test_frontend_browser_evidence_requires_trace_retry_recovery_from_4_1_1(tmp_path: Path) -> None:
    preflight = _load_preflight()
    evidence = tmp_path / "docs" / "evidence"
    evidence.mkdir(parents=True)
    path = evidence / "frontend-browser-v4.1.1.json"
    path.write_text(
        json.dumps(
            {
                "version": "4.1.1",
                "commit": "abc1234",
                "generatedAt": "2026-07-19T00:00:00Z",
                "environment": {"os": "Linux", "python": "3.12", "ci": True},
                "status": "PASS",
                "browser": "chromium",
                "checks": {
                    "cspHeader": "PASS",
                    "reactOnlyRoot": "PASS",
                    "legacyRouteRetired": "PASS",
                    "uploadCancel": "PASS",
                    "rootSpaDeepLink": "PASS",
                    "reactChatVerticalSlice": "PASS",
                    "reactHistoryPersistence": "PASS",
                    "reactStopGeneration": "PASS",
                    "completeAppShell": "PASS",
                    "offlineRefresh": "PASS",
                    "noCspConsoleErrors": "PASS",
                    "reactTraceRouteRefresh": "PASS",
                    "traceChunkDeferred": "PASS",
                    "traceRouteProviderIsolation": "PASS",
                },
            }
        ),
        encoding="utf-8",
    )

    result = preflight.check_frontend_browser_evidence(tmp_path, "4.1.1")
    assert result.status == "fail"
    assert "traceRetryRecovery" in result.detail

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["checks"]["traceRetryRecovery"] = "PASS"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = preflight.check_frontend_browser_evidence(tmp_path, "4.1.1")
    assert result.status == "pass"


def test_frontend_bundle_evidence_requires_all_decomposition_checks(tmp_path: Path) -> None:
    preflight = _load_preflight()
    evidence = tmp_path / "docs" / "evidence"
    evidence.mkdir(parents=True)
    path = evidence / "frontend-bundle-v4.1.0.json"
    payload: dict[str, Any] = {
        "version": "4.1.0",
        "commit": "abc1234",
        "generatedAt": "2026-07-19T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": "PASS",
        "checks": {
            "tracePageDynamicEntry": "PASS",
            "traceDetailDynamicEntry": "PASS",
            "traceImplementationDeferred": "PASS",
            "traceCssDeferred": "PASS",
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = preflight.check_frontend_bundle_evidence(tmp_path, "4.1.0")
    assert result.status == "pass"

    payload["checks"]["traceCssDeferred"] = "FAIL"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = preflight.check_frontend_bundle_evidence(tmp_path, "4.1.0")
    assert result.status == "fail"
    assert "traceCssDeferred" in result.detail


def test_frontend_browser_evidence_requires_workspace_demand_loading_checks_from_4_3_0(tmp_path: Path) -> None:
    preflight = _load_preflight()
    evidence = tmp_path / "docs" / "evidence"
    evidence.mkdir(parents=True)
    path = evidence / "frontend-browser-v4.3.0.json"
    required = {
        "cspHeader",
        "reactOnlyRoot",
        "legacyRouteRetired",
        "uploadCancel",
        "rootSpaDeepLink",
        "reactChatVerticalSlice",
        "reactHistoryPersistence",
        "reactStopGeneration",
        "completeAppShell",
        "offlineRefresh",
        "noCspConsoleErrors",
        "reactTraceRouteRefresh",
        "traceChunkDeferred",
        "traceRouteProviderIsolation",
        "traceRetryRecovery",
        "crossEntityBlockerAttributed",
        "crossEntityConflictPersists",
        "exactBlockerSettlementClears",
        "projectBindingBlocksDeletion",
        "projectDeletionBlocksBinding",
        "workspaceOptionalChunksDeferred",
        "workspaceFeatureLoadsOnDemand",
        "workspaceFeaturePreloadsOnIntent",
        "preloadDoesNotStartQueries",
        "skillsQueryDeferred",
        "memoryListQueryDeferred",
        "latestOverlayWinsDuringLoad",
        "lazyMutationSurvivesClose",
        "workspaceChunkFailureContained",
        "offlineUnopenedFeatureAvailable",
    }
    payload: dict[str, Any] = {
        "version": "4.3.0",
        "commit": "abc1234",
        "generatedAt": "2026-07-23T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": "PASS",
        "browser": "chromium",
        "checks": {name: "PASS" for name in required if name != "preloadDoesNotStartQueries"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = preflight.check_frontend_browser_evidence(tmp_path, "4.3.0")
    assert result.status == "fail"
    assert "preloadDoesNotStartQueries" in result.detail

    payload["checks"]["preloadDoesNotStartQueries"] = "PASS"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert preflight.check_frontend_browser_evidence(tmp_path, "4.3.0").status == "pass"


def test_frontend_browser_evidence_requires_lazy_runtime_continuity_checks_from_4_3_1(tmp_path: Path) -> None:
    preflight = _load_preflight()
    evidence = tmp_path / "docs" / "evidence"
    evidence.mkdir(parents=True)
    path = evidence / "frontend-browser-v4.3.1.json"
    source = Path(preflight.__file__).read_text(encoding="utf-8")
    required = set(re.findall(r'"([A-Za-z0-9]+)"', source[source.index("def check_frontend_browser_evidence"):source.index("def check_frontend_bundle_evidence")]))
    required.discard("frontend_browser_evidence")
    required.discard("PASS")
    required.discard("chromium")
    required.discard("status")
    required.discard("browser")
    payload: dict[str, Any] = {
        "version": "4.3.1",
        "commit": "abc1234",
        "generatedAt": "2026-07-23T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": "PASS",
        "browser": "chromium",
        "checks": {name: "PASS" for name in required if name != "currentBuildShellWinsOffline"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = preflight.check_frontend_browser_evidence(tmp_path, "4.3.1")
    assert result.status == "fail"
    assert "currentBuildShellWinsOffline" in result.detail
    payload["checks"]["currentBuildShellWinsOffline"] = "PASS"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert preflight.check_frontend_browser_evidence(tmp_path, "4.3.1").status == "pass"


def test_frontend_browser_evidence_requires_build_handoff_checks_from_4_3_2(tmp_path: Path) -> None:
    preflight = _load_preflight()
    evidence = tmp_path / "docs" / "evidence"
    evidence.mkdir(parents=True)
    path = evidence / "frontend-browser-v4.3.2.json"
    source = Path(preflight.__file__).read_text(encoding="utf-8")
    required = set(re.findall(
        r'"([A-Za-z0-9]+)"',
        source[source.index("def check_frontend_browser_evidence"):source.index("def check_frontend_bundle_evidence")],
    ))
    required -= {"frontend_browser_evidence", "PASS", "chromium", "status", "browser"}
    payload: dict[str, Any] = {
        "version": "4.3.2",
        "commit": "abc1234",
        "generatedAt": "2026-07-23T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": "PASS",
        "browser": "chromium",
        "checks": {name: "PASS" for name in required if name != "controllerHandshakeRequired"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = preflight.check_frontend_browser_evidence(tmp_path, "4.3.2")
    assert result.status == "fail"
    assert "controllerHandshakeRequired" in result.detail
    payload["checks"]["controllerHandshakeRequired"] = "PASS"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert preflight.check_frontend_browser_evidence(tmp_path, "4.3.2").status == "pass"


def test_frontend_browser_evidence_requires_quiescent_reload_checks_from_4_3_3(tmp_path: Path) -> None:
    preflight = _load_preflight()
    evidence = tmp_path / "docs" / "evidence"
    evidence.mkdir(parents=True)
    path = evidence / "frontend-browser-v4.3.3.json"
    source = Path(preflight.__file__).read_text(encoding="utf-8")
    required = set(re.findall(
        r'"([A-Za-z0-9]+)"',
        source[source.index("def check_frontend_browser_evidence"):source.index("def check_frontend_bundle_evidence")],
    ))
    required -= {"frontend_browser_evidence", "PASS", "chromium", "status", "browser"}
    payload: dict[str, Any] = {
        "version": "4.3.3",
        "commit": "abc1234",
        "generatedAt": "2026-07-24T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": "PASS",
        "browser": "chromium",
        "checks": {name: "PASS" for name in required if name != "reloadBlockerPreventsActivation"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = preflight.check_frontend_browser_evidence(tmp_path, "4.3.3")
    assert result.status == "fail"
    assert "reloadBlockerPreventsActivation" in result.detail
    payload["checks"]["reloadBlockerPreventsActivation"] = "PASS"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert preflight.check_frontend_browser_evidence(tmp_path, "4.3.3").status == "pass"


def test_frontend_bundle_evidence_requires_workspace_budgets_from_4_3_0(tmp_path: Path) -> None:
    preflight = _load_preflight()
    evidence = tmp_path / "docs" / "evidence"
    evidence.mkdir(parents=True)
    path = evidence / "frontend-bundle-v4.3.0.json"
    required = {
        "tracePageDynamicEntry",
        "traceDetailDynamicEntry",
        "traceImplementationDeferred",
        "traceCssDeferred",
        "workspaceProjectsDynamicEntry",
        "workspaceSkillsDynamicEntry",
        "workspaceMemoryDynamicEntry",
        "workspaceSettingsDynamicEntry",
        "workspaceUtilitiesDynamicEntry",
        "workspaceOptionalCssDeferred",
        "initialBundleReducedFrom428",
        "initialBundleBudget",
        "initialCssBudget",
        "optionalFeatureChunkBudget",
        "workspaceOfflineAssetManifest",
    }
    payload: dict[str, Any] = {
        "version": "4.3.0",
        "commit": "abc1234",
        "generatedAt": "2026-07-23T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": "PASS",
        "checks": {name: "PASS" for name in required if name != "initialBundleBudget"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = preflight.check_frontend_bundle_evidence(tmp_path, "4.3.0")
    assert result.status == "fail"
    assert "initialBundleBudget" in result.detail

    payload["checks"]["initialBundleBudget"] = "PASS"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert preflight.check_frontend_bundle_evidence(tmp_path, "4.3.0").status == "pass"


def test_frontend_bundle_evidence_requires_layered_offline_manifest_from_4_3_1(tmp_path: Path) -> None:
    preflight = _load_preflight()
    evidence = tmp_path / "docs" / "evidence"
    evidence.mkdir(parents=True)
    path = evidence / "frontend-bundle-v4.3.1.json"
    required = {
        "tracePageDynamicEntry",
        "traceDetailDynamicEntry",
        "traceImplementationDeferred",
        "traceCssDeferred",
        "workspaceProjectsDynamicEntry",
        "workspaceSkillsDynamicEntry",
        "workspaceMemoryDynamicEntry",
        "workspaceSettingsDynamicEntry",
        "workspaceUtilitiesDynamicEntry",
        "workspaceOptionalCssDeferred",
        "initialBundleReducedFrom428",
        "initialBundleBudget",
        "initialCssBudget",
        "optionalFeatureChunkBudget",
        "workspaceOfflineAssetManifest",
        "workspacePrimaryWarmLayer",
        "workspaceRecoveryChunksDeferred",
        "routeOptionalChunksSeparated",
    }
    payload: dict[str, Any] = {
        "version": "4.3.1",
        "commit": "abc1234",
        "generatedAt": "2026-07-23T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": "PASS",
        "checks": {name: "PASS" for name in required if name != "workspaceRecoveryChunksDeferred"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = preflight.check_frontend_bundle_evidence(tmp_path, "4.3.1")
    assert result.status == "fail"
    assert "workspaceRecoveryChunksDeferred" in result.detail
    payload["checks"]["workspaceRecoveryChunksDeferred"] = "PASS"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert preflight.check_frontend_bundle_evidence(tmp_path, "4.3.1").status == "pass"


def test_frontend_bundle_evidence_requires_immutable_identity_from_4_3_2(tmp_path: Path) -> None:
    preflight = _load_preflight()
    evidence = tmp_path / "docs" / "evidence"
    evidence.mkdir(parents=True)
    path = evidence / "frontend-bundle-v4.3.2.json"
    required = {
        "tracePageDynamicEntry",
        "traceDetailDynamicEntry",
        "traceImplementationDeferred",
        "traceCssDeferred",
        "workspaceProjectsDynamicEntry",
        "workspaceSkillsDynamicEntry",
        "workspaceMemoryDynamicEntry",
        "workspaceSettingsDynamicEntry",
        "workspaceUtilitiesDynamicEntry",
        "workspaceOptionalCssDeferred",
        "initialBundleReducedFrom428",
        "initialBundleBudget",
        "initialCssBudget",
        "optionalFeatureChunkBudget",
        "workspaceOfflineAssetManifest",
        "workspacePrimaryWarmLayer",
        "workspaceRecoveryChunksDeferred",
        "routeOptionalChunksSeparated",
        "immutableWorkerBuildIdentity",
        "workerManifestIdentityBound",
    }
    payload: dict[str, Any] = {
        "version": "4.3.2",
        "commit": "abc1234",
        "generatedAt": "2026-07-23T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": "PASS",
        "workspaceBuildId": "0123456789abcdef",
        "workspaceAssetSetDigest": "a" * 64,
        "workspaceImmutableManifest": "static/ui/workspace-assets-0123456789abcdef.json",
        "workspaceWorker": "static/ui/sw-0123456789abcdef.js",
        "workspaceRootWorker": "static/ui/sw-root-0123456789abcdef.js",
        "checks": {name: "PASS" for name in required if name != "workerManifestIdentityBound"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = preflight.check_frontend_bundle_evidence(tmp_path, "4.3.2")
    assert result.status == "fail"
    assert "workerManifestIdentityBound" in result.detail
    payload["checks"]["workerManifestIdentityBound"] = "PASS"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert preflight.check_frontend_bundle_evidence(tmp_path, "4.3.2").status == "pass"


def test_frontend_bundle_evidence_requires_update_runtime_from_4_3_3(tmp_path: Path) -> None:
    preflight = _load_preflight()
    evidence = tmp_path / "docs" / "evidence"
    evidence.mkdir(parents=True)
    path = evidence / "frontend-bundle-v4.3.3.json"
    required = {
        "tracePageDynamicEntry",
        "traceDetailDynamicEntry",
        "traceImplementationDeferred",
        "traceCssDeferred",
        "workspaceProjectsDynamicEntry",
        "workspaceSkillsDynamicEntry",
        "workspaceMemoryDynamicEntry",
        "workspaceSettingsDynamicEntry",
        "workspaceUtilitiesDynamicEntry",
        "workspaceOptionalCssDeferred",
        "initialBundleReducedFrom428",
        "initialBundleBudget",
        "initialCssBudget",
        "optionalFeatureChunkBudget",
        "workspaceOfflineAssetManifest",
        "workspacePrimaryWarmLayer",
        "workspaceRecoveryChunksDeferred",
        "routeOptionalChunksSeparated",
        "immutableWorkerBuildIdentity",
        "workerManifestIdentityBound",
        "stableBuildDiscoveryRuntime",
        "stagedWorkerActivationProtocol",
        "reloadCoordinationRuntime",
    }
    payload: dict[str, Any] = {
        "version": "4.3.3",
        "commit": "abc1234",
        "generatedAt": "2026-07-24T00:00:00Z",
        "environment": {"os": "Linux", "python": "3.12", "ci": True},
        "status": "PASS",
        "workspaceBuildId": "0123456789abcdef",
        "workspaceAssetSetDigest": "a" * 64,
        "workspaceImmutableManifest": "static/ui/workspace-assets-0123456789abcdef.json",
        "workspaceWorker": "static/ui/sw-0123456789abcdef.js",
        "workspaceRootWorker": "static/ui/sw-root-0123456789abcdef.js",
        "checks": {name: "PASS" for name in required if name != "stagedWorkerActivationProtocol"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = preflight.check_frontend_bundle_evidence(tmp_path, "4.3.3")
    assert result.status == "fail"
    assert "stagedWorkerActivationProtocol" in result.detail
    payload["checks"]["stagedWorkerActivationProtocol"] = "PASS"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert preflight.check_frontend_bundle_evidence(tmp_path, "4.3.3").status == "pass"

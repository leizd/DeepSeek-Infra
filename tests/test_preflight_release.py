from __future__ import annotations

import importlib.util
import json
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
    (root / "README.md").write_text(f"![版本](https://img.shields.io/badge/version-{version}-blue)\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text(f"## [{version}] - Release Readiness\n\nbody\n", encoding="utf-8")
    (root / "Dockerfile").write_text(f"docker build -t deepseek-infra:{version} .\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[tool.coverage.report]\nfail_under = 80\n", encoding="utf-8")
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("      - run: pytest --cov --cov-fail-under=80\n", encoding="utf-8")
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
    _write_continue_dev_evidence(evidence_dir / "continue-dev-mcp.json", version)
    _write_openai_compatible_sdk_evidence(evidence_dir / "openai-compatible-sdks.json", version)
    _write_workspace_evidence(evidence_dir / "workspace-v2.5.1.json", version)
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
    scripts = root / "scripts"
    scripts.mkdir()
    if release_exclusions:
        (scripts / "release.py").write_text('EXCLUDED = [".traces", ".local-rag"]\nSECRET = [".auth-token", ".env"]\nLOGS = ["server*.log"]\n', encoding="utf-8")
    else:
        (scripts / "release.py").write_text("print('no exclusions here')\n", encoding="utf-8")
    return root


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


def test_preflight_all_pass(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.2.9")
    results = preflight.run_preflight(root, "2.2.9")
    assert all(r.status == "pass" for r in results), [r.to_dict() for r in results if r.status != "pass"]
    assert preflight.main(["--root", str(root), "--version", "2.2.9", "--json"]) == 0


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
    (root / "pyproject.toml").write_text("[tool.coverage.report]\nfail_under = 75\n", encoding="utf-8")
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
    root = _skeleton(tmp_path, "2.5.1")
    (root / "docs" / "evidence" / "workspace-v2.5.1.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.5.1") if r.name == "workspace_core_evidence")
    assert result.status == "fail"
    assert "smoke_workspace.py" in result.detail


def test_preflight_fails_on_workspace_core_missing_required_check(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.5.1")
    _write_workspace_evidence(root / "docs" / "evidence" / "workspace-v2.5.1.json", "2.5.1", omit_check="projectExportZip")
    result = next(r for r in preflight.run_preflight(root, "2.5.1") if r.name == "workspace_core_evidence")
    assert result.status == "fail"
    assert "projectExportZip" in result.detail


def test_preflight_passes_on_workspace_core_evidence_complete(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.5.1")
    result = next(r for r in preflight.run_preflight(root, "2.5.1") if r.name == "workspace_core_evidence")
    assert result.status == "pass"

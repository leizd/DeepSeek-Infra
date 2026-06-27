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
    (root / "docs").mkdir()
    (root / "docs" / "IMPLEMENTATION_STATUS.md").write_text(f"适用版本：v{version}。\n", encoding="utf-8")
    (root / "docs" / "AGENT_EVAL.md").write_text("agent eval\n", encoding="utf-8")
    (root / "docs" / "EVAL_REPORTS.md").write_text("eval reports\n", encoding="utf-8")
    (root / "docs" / "SECURITY_SMOKE.md").write_text("security smoke\n", encoding="utf-8")
    (root / "docs" / "integrations").mkdir()
    (root / "docs" / "integrations" / "headless-mcp-client.md").write_text("headless mcp\n", encoding="utf-8")
    (root / "docs" / "integrations" / "a2a-external-peer.md").write_text("a2a external peer\n", encoding="utf-8")
    evidence_dir = root / "docs" / "evidence"
    evidence_dir.mkdir()
    _write_headless_evidence(evidence_dir / "headless-mcp-bridge.json", version)
    _write_a2a_evidence(evidence_dir / "a2a-external-peer.json", version)
    _write_a2a_evidence(evidence_dir / "a2a-third-party-peer.json", version, peer_type="third-party")
    (root / "evals").mkdir()
    (root / "evals" / "README.md").write_text(f"适用版本：v{version}。\n", encoding="utf-8")
    reports = root / "evals" / "reports"
    reports.mkdir()
    (reports / "latest.json").write_text(json.dumps({"version": version, "status": "PASS"}), encoding="utf-8")
    (reports / "agent-latest.json").write_text(json.dumps({"version": version, "status": "PASS"}), encoding="utf-8")
    scripts = root / "scripts"
    scripts.mkdir()
    if release_exclusions:
        (scripts / "release.py").write_text('EXCLUDED = [".traces", ".local-rag"]\nSECRET = [".auth-token", ".env"]\nLOGS = ["server*.log"]\n', encoding="utf-8")
    else:
        (scripts / "release.py").write_text("print('no exclusions here')\n", encoding="utf-8")
    return root


def _write_headless_evidence(path: Path, version: str, *, status: str = "PASS", omit_step: str = "") -> None:
    steps = [
        {"name": "bridge.start", "status": "pass"},
        {"name": "mcp.initialize", "status": "pass"},
        {"name": "mcp.tools_list", "status": "pass"},
        {"name": "mcp.tools_call", "status": "pass"},
        {"name": "mcp.policy_denial", "status": "pass"},
    ]
    if omit_step:
        steps = [step for step in steps if step["name"] != omit_step]
    path.write_text(json.dumps({"version": version, "status": status, "steps": steps}), encoding="utf-8")


def _write_a2a_evidence(path: Path, version: str, *, status: str = "PASS", omit_check: str = "", peer_type: str = "independent-process") -> None:
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
    path.write_text(
        json.dumps(
            {
                "version": version,
                "status": status,
                "peer": {"name": "peer", "type": peer_type},
                "checks": checks,
            }
        ),
        encoding="utf-8",
    )


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
    (root / "evals" / "reports" / "latest.json").write_text(json.dumps({"version": "2.2.8"}), encoding="utf-8")
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
    (root / "evals" / "reports" / "agent-latest.json").write_text(json.dumps({"version": "2.2.8"}), encoding="utf-8")
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


def test_preflight_warns_on_missing_a2a_third_party_evidence(tmp_path: Path) -> None:
    preflight = _load_preflight()
    root = _skeleton(tmp_path, "2.3.3")
    (root / "docs" / "evidence" / "a2a-third-party-peer.json").unlink()
    result = next(r for r in preflight.run_preflight(root, "2.3.3") if r.name == "a2a_third_party_evidence")
    assert result.status == "warn"
    assert preflight.main(["--root", str(root), "--version", "2.3.3"]) == 0


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

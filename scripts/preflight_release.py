#!/usr/bin/env python3
"""Release Preflight — verify version sync and release evidence before tagging.

Checks that the version string is consistent across the README badge,
CHANGELOG, Dockerfile tag, Implementation Status / evals README headers, that
the eval / agent reports are current, that the smoke / eval docs exist, that
``scripts/release.py`` still excludes runtime caches and logs, that headless MCP
bridge and A2A external peer evidence are present, and (since v2.3.1) that GUI
interop evidence for Claude Desktop / Cursor has been recorded in
``docs/COMPATIBILITY.md``.

    python scripts/preflight_release.py --version 2.3.3

Exits 1 on any FAIL; WARNINGs do not fail. Version defaults to
``settings.app_version``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.infra.diagnostics.runtime_doctor import (  # noqa: E402
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_WARN,
    CheckResult,
)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"__PREFLIGHT_READ_ERROR__: {exc}"


def check_readme_badge(root: Path, version: str) -> CheckResult:
    text = _read(root / "README.md")
    needle = f"version-{version}-blue"
    if needle in text:
        return CheckResult("readme_badge", STATUS_PASS, f"README badge is {version}", {"needle": needle})
    return CheckResult("readme_badge", STATUS_FAIL, f"README badge is not {version} (missing '{needle}')", {"needle": needle})


def check_changelog_entry(root: Path, version: str) -> CheckResult:
    text = _read(root / "CHANGELOG.md")
    needle = f"## [{version}]"
    if needle in text:
        return CheckResult("changelog", STATUS_PASS, f"CHANGELOG has {needle}", {"needle": needle})
    return CheckResult("changelog", STATUS_FAIL, f"CHANGELOG missing {needle}", {"needle": needle})


def check_dockerfile_tag(root: Path, version: str) -> CheckResult:
    text = _read(root / "Dockerfile")
    needle = f"deepseek-infra:{version}"
    if needle in text:
        return CheckResult("dockerfile_tag", STATUS_PASS, f"Dockerfile tag is {version}", {"needle": needle})
    return CheckResult("dockerfile_tag", STATUS_FAIL, f"Dockerfile tag is not {version} (missing '{needle}')", {"needle": needle})


def check_doc_version(root: Path, doc_rel: str, version: str) -> CheckResult:
    text = _read(root / doc_rel)
    needle = f"适用版本：v{version}。"
    if needle in text:
        return CheckResult(f"doc_version:{doc_rel}", STATUS_PASS, f"{doc_rel} 适用版本 is v{version}", {"needle": needle})
    return CheckResult(f"doc_version:{doc_rel}", STATUS_FAIL, f"{doc_rel} 适用版本 is not v{version} (missing '{needle}')", {"needle": needle})


def check_doc_links_exist(root: Path) -> CheckResult:
    missing: list[str] = []
    for rel in (
        "docs/AGENT_EVAL.md",
        "docs/EVAL_REPORTS.md",
        "docs/SECURITY_SMOKE.md",
        "docs/integrations/headless-mcp-client.md",
        "docs/integrations/a2a-external-peer.md",
    ):
        if not (root / rel).is_file():
            missing.append(rel)
    if missing:
        return CheckResult("doc_links", STATUS_FAIL, f"missing docs: {', '.join(missing)}", {"missing": missing})
    return CheckResult("doc_links", STATUS_PASS, "AGENT_EVAL / EVAL_REPORTS / SECURITY_SMOKE / headless MCP / A2A external docs present", {})


def check_eval_report_version(root: Path, version: str) -> CheckResult:
    path = root / "evals" / "reports" / "latest.json"
    if not path.is_file():
        return CheckResult("eval_report", STATUS_WARN, "evals/reports/latest.json missing; run run_offline_eval_suite.py", {"path": str(path)})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("eval_report", STATUS_FAIL, f"cannot parse latest.json: {exc}", {"path": str(path)})
    reported = str(data.get("version") or "")
    if reported == version:
        return CheckResult("eval_report", STATUS_PASS, f"latest.json version is {version}", {"version": reported})
    return CheckResult("eval_report", STATUS_FAIL, f"latest.json version is {reported!r}, expected {version!r}", {"version": reported, "expected": version})


def check_agent_report(root: Path, version: str) -> CheckResult:
    path = root / "evals" / "reports" / "agent-latest.json"
    if not path.is_file():
        return CheckResult("agent_report", STATUS_WARN, "evals/reports/agent-latest.json missing; run run_agent_eval.py", {"path": str(path)})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("agent_report", STATUS_FAIL, f"cannot parse agent-latest.json: {exc}", {"path": str(path)})
    reported = str(data.get("version") or "")
    if reported == version:
        return CheckResult("agent_report", STATUS_PASS, f"agent-latest.json version is {version}", {"version": reported})
    return CheckResult("agent_report", STATUS_FAIL, f"agent-latest.json version is {reported!r}, expected {version!r}", {"version": reported, "expected": version})


def check_release_exclusions(root: Path) -> CheckResult:
    text = _read(root / "scripts" / "release.py")
    required = (".traces", ".local-rag", ".auth-token", ".env", "server*.log")
    missing = [token for token in required if token not in text]
    if missing:
        return CheckResult("release_exclusions", STATUS_FAIL, f"release.py no longer excludes: {', '.join(missing)}", {"missing": missing})
    return CheckResult("release_exclusions", STATUS_PASS, "release.py excludes runtime caches, secrets and logs", {"checked": list(required)})


def check_headless_mcp_bridge_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / "headless-mcp-bridge.json"
    if not path.is_file():
        return CheckResult(
            "headless_mcp_bridge_evidence",
            STATUS_FAIL,
            "headless MCP bridge evidence missing; run scripts/smoke_mcp_headless_bridge.py",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("headless_mcp_bridge_evidence", STATUS_FAIL, f"cannot parse headless MCP bridge evidence: {exc}", {"path": str(path)})
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult(
            "headless_mcp_bridge_evidence",
            STATUS_FAIL,
            f"headless MCP bridge evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
    if data.get("status") != "PASS":
        return CheckResult(
            "headless_mcp_bridge_evidence",
            STATUS_FAIL,
            f"headless MCP bridge evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    steps = data.get("steps")
    step_status = {str(step.get("name")): str(step.get("status")) for step in steps if isinstance(step, dict)} if isinstance(steps, list) else {}
    required = ("bridge.start", "mcp.initialize", "mcp.tools_list", "mcp.tools_call", "mcp.policy_denial")
    missing_or_failed = [name for name in required if step_status.get(name) != "pass"]
    if missing_or_failed:
        return CheckResult(
            "headless_mcp_bridge_evidence",
            STATUS_FAIL,
            f"headless MCP bridge evidence missing PASS steps: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    return CheckResult(
        "headless_mcp_bridge_evidence",
        STATUS_PASS,
        "headless MCP stdio bridge evidence recorded",
        {"path": str(path), "steps": list(required)},
    )


def check_a2a_external_peer_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / "a2a-external-peer.json"
    if not path.is_file():
        return CheckResult(
            "a2a_external_peer_evidence",
            STATUS_FAIL,
            "A2A external peer evidence missing; run scripts/smoke_a2a_external_peer.py",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("a2a_external_peer_evidence", STATUS_FAIL, f"cannot parse A2A external peer evidence: {exc}", {"path": str(path)})
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult(
            "a2a_external_peer_evidence",
            STATUS_FAIL,
            f"A2A external peer evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
    if data.get("status") != "PASS":
        return CheckResult(
            "a2a_external_peer_evidence",
            STATUS_FAIL,
            f"A2A external peer evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v) for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = ("agentCard", "messageSend", "messageStream", "tasksGet", "tasksCancel", "tasksList", "artifactChunks", "sseFinalEvent")
    missing_or_failed = [name for name in required if check_status.get(name) != "pass"]
    if missing_or_failed:
        return CheckResult(
            "a2a_external_peer_evidence",
            STATUS_FAIL,
            f"A2A external peer evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    peer = data.get("peer")
    peer_data = peer if isinstance(peer, dict) else {}
    return CheckResult(
        "a2a_external_peer_evidence",
        STATUS_PASS,
        "A2A external peer evidence recorded",
        {"path": str(path), "peer": peer_data.get("name"), "checks": list(required)},
    )


def check_a2a_third_party_evidence(root: Path) -> CheckResult:
    path = root / "docs" / "evidence" / "a2a-third-party-peer.json"
    if not path.is_file():
        return CheckResult(
            "a2a_third_party_evidence",
            STATUS_WARN,
            "third-party A2A ecosystem evidence still pending; adapter path is documented",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("a2a_third_party_evidence", STATUS_WARN, f"third-party A2A evidence is present but unparsable: {exc}", {"path": str(path)})
    peer = data.get("peer")
    peer_data = peer if isinstance(peer, dict) else {}
    checks = data.get("checks")
    check_status = {str(k): str(v) for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = ("agentCard", "messageSend", "messageStream", "tasksGet", "tasksCancel", "tasksList", "artifactChunks", "sseFinalEvent")
    if data.get("status") == "PASS" and peer_data.get("type") in {"third-party", "adapter"} and all(check_status.get(name) == "pass" for name in required):
        return CheckResult(
            "a2a_third_party_evidence",
            STATUS_PASS,
            "third-party / adapter A2A ecosystem evidence recorded",
            {"path": str(path), "peer": peer_data.get("name"), "type": peer_data.get("type")},
        )
    return CheckResult(
        "a2a_third_party_evidence",
        STATUS_WARN,
        "third-party A2A ecosystem evidence is not PASS yet",
        {"path": str(path), "status": data.get("status"), "type": peer_data.get("type")},
    )


def check_gui_interop_evidence(root: Path) -> CheckResult:
    """Verify Claude Desktop / Cursor GUI evidence is recorded in COMPATIBILITY.md.

    A WARNING (not FAIL) is emitted while GUI testing is still pending — the
    check scans the MCP Client Compatibility table for ``✅ GUI tested`` markers.
    Once a human runs the GUI verification runbook and updates the matrix, this
    check flips to PASS automatically.
    """
    text = _read(root / "docs" / "COMPATIBILITY.md")
    pending: list[str] = []
    for client in ("Claude Desktop", "Cursor"):
        # Look for the row: | <client> | <status> | ...
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("|") and client in stripped:
                if "✅ GUI tested" in stripped or "✅ GUI verified" in stripped:
                    break
                if "🟡" in stripped:
                    pending.append(client)
                break
    if not pending:
        return CheckResult(
            "gui_interop_evidence",
            STATUS_PASS,
            "Claude Desktop / Cursor GUI evidence recorded in COMPATIBILITY.md",
            {"pending": []},
        )
    return CheckResult(
        "gui_interop_evidence",
        STATUS_WARN,
        f"GUI interop evidence still pending for: {', '.join(pending)} (fill the runbook in docs/integrations/ then update COMPATIBILITY.md)",
        {"pending": pending},
    )


def run_preflight(root: Path, version: str) -> list[CheckResult]:
    return [
        check_readme_badge(root, version),
        check_changelog_entry(root, version),
        check_dockerfile_tag(root, version),
        check_doc_version(root, "docs/IMPLEMENTATION_STATUS.md", version),
        check_doc_version(root, "evals/README.md", version),
        check_doc_links_exist(root),
        check_eval_report_version(root, version),
        check_agent_report(root, version),
        check_release_exclusions(root),
        check_headless_mcp_bridge_evidence(root, version),
        check_a2a_external_peer_evidence(root, version),
        check_a2a_third_party_evidence(root),
        check_gui_interop_evidence(root),
    ]


def render_text(results: list[CheckResult]) -> str:
    lines = [f"[{r.label}] {r.name}: {r.detail}" for r in results]
    fails = sum(1 for r in results if r.status == STATUS_FAIL)
    warns = sum(1 for r in results if r.status == STATUS_WARN)
    overall = "FAIL" if fails else ("WARNING" if warns else "PASS")
    lines.append("")
    lines.append(f"Preflight summary: {overall} — {len(results)} checks, {fails} fail, {warns} warning")
    return "\n".join(lines)


def dump_json(results: list[CheckResult], version: str) -> str:
    payload: dict[str, Any] = {
        "version": version,
        "overall": "FAIL" if any(r.status == STATUS_FAIL for r in results) else ("WARNING" if any(r.status == STATUS_WARN for r in results) else "PASS"),
        "checks": [r.to_dict() for r in results],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Release preflight version-sync checks")
    parser.add_argument("--version", default="", help="Expected version. Defaults to settings.app_version.")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="Project root to check.")
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable JSON summary.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    version = args.version
    if not version:
        from deepseek_infra.core.config import settings

        version = settings.app_version
    results = run_preflight(args.root.resolve(), version)
    if args.json:
        print(dump_json(results, version))
    else:
        print(render_text(results))
    return 1 if any(r.status == STATUS_FAIL for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Release Preflight - verify version sync and release evidence before tagging.

Checks that the version string is consistent across the README badge,
CHANGELOG, Dockerfile tag, Implementation Status / evals README headers, that
the eval / agent reports are current, that the smoke / eval docs exist, that
``scripts/release.py`` still excludes runtime caches and logs, that headless MCP
bridge and A2A external peer evidence are present, that optional third-party A2A
and Edge Router evidence is strict when submitted, that key docs do not contain
encoding corruption (expanded in v2.7.2), that Edge Router dry-run evidence is
present for v2.7.3, and (since v2.3.1) that GUI interop evidence for Claude
Desktop / Cursor has been recorded in ``docs/COMPATIBILITY.md``.

    python scripts/preflight_release.py --version 2.7.3

Exits 1 on any FAIL; WARNINGs do not fail. Version defaults to
``settings.app_version``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

# Common mojibake / encoding-corruption signatures that should never ship.
GARBLED_PATTERNS = (
    re.compile(r"\?{3,}"),  # three or more question marks (encoding fallback)
    re.compile("\u951f\u65a4\u62f7"),  # classic GBK/UTF-8 mojibake
    re.compile(r"\ufffd"),  # Unicode replacement character
)
GARBLED_EXACT_PATHS = (
    "CHANGELOG.md",
    "Dockerfile",
    "README.md",
)
GARBLED_GLOBS = (
    ".github/workflows/*.yml",
    "scripts/*.py",
    "docs/**/*.md",
    "deepseek_infra/**/*.py",
)
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


_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_FENCE_RE = re.compile(r"(```|~~~)[^\n]*\n.*?\n\1", re.DOTALL)


def _strip_code_spans(text: str) -> str:
    """Remove inline code and fenced code blocks so literal examples of
    garbled patterns inside documentation do not trigger the sanity check.
    """
    text = _FENCE_RE.sub("", text)
    return _INLINE_CODE_RE.sub("", text)


def _encoding_scan_paths(root: Path) -> list[tuple[str, Path]]:
    """Return release-facing files covered by the encoding sanity gate."""
    seen: set[str] = set()
    paths: list[tuple[str, Path]] = []

    def add(rel: str, path: Path) -> None:
        if rel in seen or not path.is_file():
            return
        seen.add(rel)
        paths.append((rel, path))

    for rel in GARBLED_EXACT_PATHS:
        add(rel, root / rel)

    for pattern in GARBLED_GLOBS:
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                add(path.relative_to(root).as_posix(), path)

    return paths


def check_docs_encoding_sanity(root: Path) -> CheckResult:
    """Detect encoding corruption in release-facing files.

    Catches the kind of mojibake that appeared in CHANGELOG.md for v2.3.3
    before it was polished in v2.3.4. Inline code spans and fenced blocks are
    ignored for Markdown because they may intentionally document the checked
    patterns.
    """
    findings: list[dict[str, Any]] = []
    checked_paths: list[str] = []
    for rel, path in _encoding_scan_paths(root):
        checked_paths.append(rel)
        text = _read(path)
        if path.suffix.lower() == ".md":
            text = _strip_code_spans(text)
        for pattern in GARBLED_PATTERNS:
            for match in pattern.finditer(text):
                findings.append({"path": rel, "pattern": pattern.pattern, "snippet": text[max(0, match.start() - 20):match.end() + 20]})
    if findings:
        paths = sorted({f["path"] for f in findings})
        return CheckResult(
            "docs_encoding_sanity",
            STATUS_FAIL,
            f"encoding corruption detected in {', '.join(paths)}; fix mojibake before release",
            {"findings": findings[:10]},
        )
    return CheckResult("docs_encoding_sanity", STATUS_PASS, "no encoding corruption in release-facing files", {"checked": checked_paths})


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
    metadata_fail = _check_evidence_metadata("eval_report", data, path)
    if metadata_fail:
        return metadata_fail
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
    metadata_fail = _check_evidence_metadata("agent_report", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported == version:
        return CheckResult("agent_report", STATUS_PASS, f"agent-latest.json version is {version}", {"version": reported})
    return CheckResult("agent_report", STATUS_FAIL, f"agent-latest.json version is {reported!r}, expected {version!r}", {"version": reported, "expected": version})


def _load_json_report(root: Path, rel: str) -> tuple[dict[str, Any] | None, str]:
    path = root / rel
    if not path.is_file():
        return None, f"{rel} missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"cannot parse {rel}: {exc}"
    if not isinstance(data, dict):
        return None, f"{rel} must contain a JSON object"
    return data, ""


def _check_versioned_report(root: Path, rel: str, name: str, version: str) -> CheckResult:
    data, error = _load_json_report(root, rel)
    path = root / rel
    if data is None:
        return CheckResult(name, STATUS_FAIL, error, {"path": str(path)})
    metadata_fail = _check_evidence_metadata(name, data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult(name, STATUS_FAIL, f"{rel} version is {reported!r}, expected {version!r}", {"version": reported, "expected": version})
    if data.get("status") != "PASS":
        return CheckResult(name, STATUS_FAIL, f"{rel} status is {data.get('status')!r}, expected PASS", {"status": data.get("status")})
    return CheckResult(name, STATUS_PASS, f"{rel} version/status evidence is PASS", {"path": str(path), "version": reported})


def check_baseline_compare_report(root: Path, version: str) -> CheckResult:
    return _check_versioned_report(root, "evals/reports/baseline-compare-latest.json", "baseline_compare_report", version)


def check_security_corpus_report(root: Path, version: str) -> CheckResult:
    return _check_versioned_report(root, "evals/reports/security-latest.json", "security_corpus_report", version)


def _coverage_fail_under(root: Path) -> float:
    text = _read(root / "pyproject.toml")
    match = re.search(r"(?m)^\s*fail_under\s*=\s*(\d+(?:\.\d+)?)\s*$", text)
    return float(match.group(1)) if match else 0.0


def check_quality_gate_evidence(root: Path, version: str) -> CheckResult:
    failures: list[str] = []
    details: dict[str, Any] = {"version": version}
    coverage_gate = _coverage_fail_under(root)
    details["coverageFailUnder"] = coverage_gate
    if coverage_gate < 90:
        failures.append(f"coverage fail_under is {coverage_gate:g}, expected >= 90")
    ci_text = _read(root / ".github" / "workflows" / "ci.yml")
    if "--cov-fail-under=90" not in ci_text:
        failures.append("CI pytest coverage gate is not --cov-fail-under=90")
    report_specs = [
        ("evals/reports/latest.json", "offlineEval"),
        ("evals/reports/agent-latest.json", "agentEval"),
        ("evals/reports/baseline-compare-latest.json", "baselineCompare"),
        ("evals/reports/security-latest.json", "securityCorpus"),
        (f"evals/reports/skills-v{version}.json", "skillEval"),
    ]
    if _version_tuple(version) >= (2, 9, 0):
        report_specs.append((f"evals/reports/automation-v{version}.json", "automationEval"))
    for rel, label in report_specs:
        data, error = _load_json_report(root, rel)
        if data is None:
            failures.append(error)
            continue
        status = data.get("status")
        details[label] = status
        if status != "PASS":
            failures.append(f"{rel} status is {status!r}, expected PASS")
        if str(data.get("version") or "") != version:
            failures.append(f"{rel} version is {data.get('version')!r}, expected {version!r}")
    latest, error = _load_json_report(root, "evals/reports/latest.json")
    if latest is None:
        failures.append(error)
    else:
        raw_injection = latest.get("injection")
        injection: dict[str, Any] = raw_injection if isinstance(raw_injection, dict) else {}
        details["injectionStrict"] = injection.get("status")
        if injection.get("status") != "PASS" or injection.get("gateMode") != "hard":
            failures.append("latest.json injection gate is not PASS/hard")
    if failures:
        return CheckResult("quality_gate_evidence", STATUS_FAIL, "; ".join(failures), details)
    return CheckResult("quality_gate_evidence", STATUS_PASS, "quality gate evidence is complete", details)


def check_release_exclusions(root: Path) -> CheckResult:
    text = _read(root / "scripts" / "release.py")
    required: tuple[str, ...] = (
        ".traces",
        ".local-rag",
        ".media",
        ".browser-audit",
        ".browser-downloads",
        ".browser-profiles",
        ".automation",
        ".auth-token",
        ".env",
        "server*.log",
    )
    missing = [token for token in required if token not in text]
    if missing:
        return CheckResult("release_exclusions", STATUS_FAIL, f"release.py no longer excludes: {', '.join(missing)}", {"missing": missing})
    return CheckResult("release_exclusions", STATUS_PASS, "release.py excludes runtime caches, secrets and logs", {"checked": list(required)})


def check_ga_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / f"ga-v{version}.json"
    if not path.is_file():
        return CheckResult("ga_evidence", STATUS_FAIL, "GA evidence missing; run scripts/smoke_ga.py --offline", {"path": str(path)})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("ga_evidence", STATUS_FAIL, f"cannot parse GA evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("ga", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult("ga_evidence", STATUS_FAIL, f"GA evidence version is {reported!r}, expected {version!r}", {"version": reported, "expected": version})
    if data.get("status") != "PASS":
        return CheckResult("ga_evidence", STATUS_FAIL, f"GA evidence status is {data.get('status')!r}, expected PASS", {"status": data.get("status")})
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = (
        "workspaceHome",
        "project",
        "memory",
        "skill",
        "media",
        "browserSnapshot",
        "savedItem",
        "artifact",
        "automation",
        "export",
        "provenance",
        "exportRedaction",
    )
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult("ga_evidence", STATUS_FAIL, f"GA evidence missing PASS checks: {', '.join(missing_or_failed)}", {"missingOrFailed": missing_or_failed})
    return CheckResult("ga_evidence", STATUS_PASS, "GA evidence recorded", {"path": str(path), "checks": list(required)})


def check_ga_demo_assets(root: Path) -> CheckResult:
    required = (
        "docs/DEMO_3_0.md",
        "docs/assets/3.0-workspace-overview.png",
        "docs/assets/3.0-skill-run.png",
        "docs/assets/3.0-automation-run.png",
        "docs/assets/3.0-project-export.png",
    )
    missing = [rel for rel in required if not (root / rel).is_file()]
    empty = [rel for rel in required if (root / rel).is_file() and (root / rel).stat().st_size == 0]
    if missing or empty:
        return CheckResult("ga_demo_assets", STATUS_FAIL, "GA demo doc/assets incomplete", {"missing": missing, "empty": empty})
    return CheckResult("ga_demo_assets", STATUS_PASS, "GA demo doc and screenshots present", {"checked": list(required)})


def check_ga_docs_roster(root: Path) -> CheckResult:
    required = (
        "docs/GETTING_STARTED.md",
        "docs/WORKSPACE.md",
        "docs/MEMORY.md",
        "docs/SKILLS.md",
        "docs/MEDIA.md",
        "docs/BROWSER_CONTROL.md",
        "docs/AUTOMATION.md",
        "docs/EXPORTS.md",
        "docs/SECURITY.md",
        "docs/DEPLOYMENT.md",
        "docs/DEMO_3_0.md",
        "docs/EVIDENCE_INDEX.md",
    )
    missing = [rel for rel in required if not (root / rel).is_file()]
    if missing:
        return CheckResult("ga_docs_roster", STATUS_FAIL, f"GA docs roster missing: {', '.join(missing)}", {"missing": missing})
    return CheckResult("ga_docs_roster", STATUS_PASS, "GA docs roster present", {"checked": list(required)})


def check_ga_evidence_index(root: Path, version: str) -> CheckResult:
    text = _read(root / "docs" / "EVIDENCE_INDEX.md")
    needle = f"ga-v{version}.json"
    if needle in text:
        return CheckResult("ga_evidence_index", STATUS_PASS, f"EVIDENCE_INDEX lists {needle}", {"needle": needle})
    return CheckResult("ga_evidence_index", STATUS_FAIL, f"EVIDENCE_INDEX missing {needle}", {"needle": needle})


def check_ga_release_manifest(root: Path, version: str) -> CheckResult:
    text = _read(root / "deepseek_infra" / "infra" / "diagnostics" / "release_manifest.py")
    needles = ("gaEvidence", f"docs/evidence/ga-v{version}.json")
    missing = [needle for needle in needles if needle not in text]
    if missing:
        return CheckResult("ga_release_manifest", STATUS_FAIL, "release manifest missing GA evidence fields", {"missing": missing})
    return CheckResult("ga_release_manifest", STATUS_PASS, "release manifest includes gaEvidence", {"checked": list(needles)})


def check_ga_release_exclusions(root: Path) -> CheckResult:
    text = _read(root / "scripts" / "release.py")
    required = (
        ".file-cache",
        ".projects",
        ".local-rag",
        ".traces",
        ".semantic-cache",
        ".request-queue",
        ".generated",
        ".tool-audit",
        ".scheduler",
        ".a2a",
        ".budget",
        ".memory",
        ".reminders",
        ".agent-runs",
        ".search-cache",
        ".auth-token",
        ".media",
        ".browser-audit",
        ".browser-downloads",
        ".browser-profiles",
        ".automation",
        ".skills",
        ".env",
        "server*.log",
    )
    missing = [token for token in required if token not in text]
    if missing:
        return CheckResult("ga_release_exclusions", STATUS_FAIL, f"release.py missing GA runtime exclusions: {', '.join(missing)}", {"missing": missing})
    return CheckResult("ga_release_exclusions", STATUS_PASS, "release.py excludes all GA runtime data dirs", {"checked": list(required)})


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
    metadata_fail = _check_evidence_metadata("headless_mcp_bridge", data, path)
    if metadata_fail:
        return metadata_fail
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
    metadata_fail = _check_evidence_metadata("a2a_external_peer", data, path)
    if metadata_fail:
        return metadata_fail
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
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = ("agentCard", "messageSend", "messageStream", "tasksGet", "tasksCancel", "tasksList", "artifactChunks", "sseFinalEvent")
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
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


def _optional_stale_evidence(name: str, label: str, reported: str, version: str, path: Path) -> CheckResult:
    return CheckResult(
        name,
        STATUS_WARN,
        f"{label} evidence version is {reported!r}, expected {version!r}; refresh this optional evidence when validating that ecosystem path for the current release",
        {"path": str(path), "version": reported, "expected": version},
    )


def check_a2a_third_party_peer_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / "a2a-third-party-peer.json"
    if not path.is_file():
        return CheckResult(
            "a2a_third_party_peer_evidence",
            STATUS_WARN,
            "third-party A2A ecosystem evidence still pending; adapter path is documented",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("a2a_third_party_peer_evidence", STATUS_FAIL, f"cannot parse third-party A2A evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("a2a_third_party_peer", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return _optional_stale_evidence("a2a_third_party_peer_evidence", "third-party A2A", reported, version, path)
    if data.get("status") != "PASS":
        return CheckResult(
            "a2a_third_party_peer_evidence",
            STATUS_FAIL,
            f"third-party A2A evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    peer = data.get("peer")
    peer_data = peer if isinstance(peer, dict) else {}
    peer_type = str(data.get("peerType") or peer_data.get("type") or "")
    if peer_type != "third-party":
        return CheckResult(
            "a2a_third_party_peer_evidence",
            STATUS_FAIL,
            f"third-party A2A evidence peerType is {peer_type!r}, expected 'third-party'",
            {"path": str(path), "peerType": peer_type},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = ("agentCard", "messageSend", "messageStream", "tasksGet", "tasksCancel", "tasksList", "artifactChunks", "sseFinalEvent")
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult(
            "a2a_third_party_peer_evidence",
            STATUS_FAIL,
            f"third-party A2A evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    return CheckResult(
        "a2a_third_party_peer_evidence",
        STATUS_PASS,
        "third-party A2A ecosystem evidence recorded",
        {"path": str(path), "peer": peer_data.get("name"), "type": peer_type, "checks": list(required)},
    )


def check_edge_router_smoke_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / "edge-router-smoke.json"
    if not path.is_file():
        return CheckResult(
            "edge_router_smoke_evidence",
            STATUS_WARN,
            "Edge Router smoke evidence missing; run examples/edge_router_smoke.py --out docs/evidence/edge-router-smoke.json --markdown docs/evidence/edge-router-smoke.md",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("edge_router_smoke_evidence", STATUS_FAIL, f"cannot parse Edge Router smoke evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("edge_router_smoke", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return _optional_stale_evidence("edge_router_smoke_evidence", "Edge Router smoke", reported, version, path)
    if data.get("status") != "PASS":
        return CheckResult(
            "edge_router_smoke_evidence",
            STATUS_FAIL,
            f"Edge Router smoke evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = ("ollamaModelsListed", "openaiCompatibleLocalCall", "edgeStatusEndpoint", "fallbackReady")
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult(
            "edge_router_smoke_evidence",
            STATUS_FAIL,
            f"Edge Router smoke evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    return CheckResult(
        "edge_router_smoke_evidence",
        STATUS_PASS,
        "Edge Router smoke evidence recorded",
        {"path": str(path), "checks": list(required)},
    )


def check_edge_router_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / f"edge-router-v{version}.json"
    if not path.is_file():
        return CheckResult(
            "edge_router_evidence",
            STATUS_FAIL,
            "Edge Router evidence missing; run scripts/smoke_edge_router.py --offline",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("edge_router_evidence", STATUS_FAIL, f"cannot parse Edge Router evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("edge_router", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult(
            "edge_router_evidence",
            STATUS_FAIL,
            f"Edge Router evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
    if data.get("status") != "PASS":
        return CheckResult(
            "edge_router_evidence",
            STATUS_FAIL,
            f"Edge Router evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = (
        "edgeDoctor",
        "statusShape",
        "routePreviewApi",
        "fakeProvider",
        "routingPolicy",
        "fallbackPolicy",
        "forcedLocalUnavailable",
    )
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult(
            "edge_router_evidence",
            STATUS_FAIL,
            f"Edge Router evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    return CheckResult("edge_router_evidence", STATUS_PASS, "Edge Router evidence recorded", {"path": str(path), "checks": list(required)})


def check_continue_dev_mcp_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / "continue-dev-mcp.json"
    if not path.is_file():
        return CheckResult(
            "continue_dev_mcp_evidence",
            STATUS_WARN,
            "Continue.dev MCP evidence missing; fill the runbook in docs/integrations/continue-dev.md and record evidence",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("continue_dev_mcp_evidence", STATUS_FAIL, f"cannot parse Continue.dev MCP evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("continue_dev_mcp", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return _optional_stale_evidence("continue_dev_mcp_evidence", "Continue.dev MCP", reported, version, path)
    if data.get("status") != "PASS":
        return CheckResult(
            "continue_dev_mcp_evidence",
            STATUS_FAIL,
            f"Continue.dev MCP evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = ("configLoaded", "mcpInitialize", "toolsList", "lowRiskToolCall", "policyDenial", "promptInjectionClean")
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult(
            "continue_dev_mcp_evidence",
            STATUS_FAIL,
            f"Continue.dev MCP evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    client = data.get("client", "")
    return CheckResult(
        "continue_dev_mcp_evidence",
        STATUS_PASS,
        f"Continue.dev MCP evidence recorded for client={client}",
        {"path": str(path), "client": client, "checks": list(required)},
    )


def check_openai_compatible_sdk_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / "openai-compatible-sdks.json"
    if not path.is_file():
        return CheckResult(
            "openai_compatible_sdk_evidence",
            STATUS_WARN,
            "OpenAI-compatible SDK evidence missing; run scripts/smoke_openai_compatible_sdks.py --out docs/evidence/openai-compatible-sdks.json --markdown docs/evidence/openai-compatible-sdks.md",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("openai_compatible_sdk_evidence", STATUS_FAIL, f"cannot parse OpenAI-compatible SDK evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("openai_compatible_sdk", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return _optional_stale_evidence("openai_compatible_sdk_evidence", "OpenAI-compatible SDK", reported, version, path)
    if data.get("status") != "PASS":
        return CheckResult(
            "openai_compatible_sdk_evidence",
            STATUS_FAIL,
            f"OpenAI-compatible SDK evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    sdks = data.get("sdks")
    if not isinstance(sdks, dict) or not sdks:
        return CheckResult(
            "openai_compatible_sdk_evidence",
            STATUS_FAIL,
            "OpenAI-compatible SDK evidence is missing the 'sdks' object",
            {"path": str(path)},
        )
    required_sdks = {"langchain": ("modelsList", "chatCompletion", "streaming"), "litellm": ("modelsList", "chatCompletion", "streaming"), "llamaindex": ("chatCompletion",)}
    failures: list[str] = []
    for sdk_name, required_checks in required_sdks.items():
        sdk_data = sdks.get(sdk_name)
        if not isinstance(sdk_data, dict):
            failures.append(f"sdks.{sdk_name} missing")
            continue
        for check in required_checks:
            value = str(sdk_data.get(check, "")).upper()
            if value != "PASS":
                failures.append(f"sdks.{sdk_name}.{check}={value}")
    if failures:
        return CheckResult(
            "openai_compatible_sdk_evidence",
            STATUS_FAIL,
            f"OpenAI-compatible SDK evidence missing PASS checks: {', '.join(failures)}",
            {"missingOrFailed": failures},
        )
    return CheckResult(
        "openai_compatible_sdk_evidence",
        STATUS_PASS,
        "OpenAI-compatible SDK evidence recorded",
        {"path": str(path), "sdks": list(required_sdks.keys())},
    )


def check_context_taint_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / f"context-taint-v{version}.json"
    if not path.is_file():
        return CheckResult(
            "context_taint_evidence",
            STATUS_FAIL,
            "Context Taint evidence missing; run scripts/smoke_context_taint.py --offline",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("context_taint_evidence", STATUS_FAIL, f"cannot parse Context Taint evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("context_taint", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult(
            "context_taint_evidence",
            STATUS_FAIL,
            f"Context Taint evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
    if data.get("status") != "PASS":
        return CheckResult(
            "context_taint_evidence",
            STATUS_FAIL,
            f"Context Taint evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = (
        "webInjectionScanned",
        "fileInjectionScanned",
        "mediaTranscriptInjectionScanned",
        "toolDirectiveRecognized",
        "taintedTurnEscalation",
        "riskDiagnosticsPresent",
    )
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult(
            "context_taint_evidence",
            STATUS_FAIL,
            f"Context Taint evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    return CheckResult(
        "context_taint_evidence",
        STATUS_PASS,
        "Context Taint evidence recorded",
        {"path": str(path), "checks": list(required)},
    )


def check_workspace_core_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / f"workspace-v{version}.json"
    if not path.is_file():
        return CheckResult(
            "workspace_core_evidence",
            STATUS_FAIL,
            "Workspace Core evidence missing; run scripts/smoke_workspace.py --offline",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("workspace_core_evidence", STATUS_FAIL, f"cannot parse Workspace Core evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("workspace_core", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult(
            "workspace_core_evidence",
            STATUS_FAIL,
            f"Workspace Core evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
    if data.get("status") != "PASS":
        return CheckResult(
            "workspace_core_evidence",
            STATUS_FAIL,
            f"Workspace Core evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = (
        "projectCreate",
        "savedItemCreate",
        "artifactList",
        "conversationExport",
        "projectExportZip",
        "secretRedaction",
    )
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult(
            "workspace_core_evidence",
            STATUS_FAIL,
            f"Workspace Core evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    return CheckResult(
        "workspace_core_evidence",
        STATUS_PASS,
        "Workspace Core evidence recorded",
        {"path": str(path), "checks": list(required)},
    )


def check_media_layer_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / f"media-v{version}.json"
    if not path.is_file():
        return CheckResult(
            "media_layer_evidence",
            STATUS_FAIL,
            "Media Layer evidence missing; run scripts/smoke_media.py --offline",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("media_layer_evidence", STATUS_FAIL, f"cannot parse Media Layer evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("media_layer", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult(
            "media_layer_evidence",
            STATUS_FAIL,
            f"Media Layer evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
    if data.get("status") != "PASS":
        return CheckResult(
            "media_layer_evidence",
            STATUS_FAIL,
            f"Media Layer evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = (
        "imageImport",
        "pdfPageIndex",
        "webpageSnapshot",
        "mediaSegments",
        "mediaToRag",
        "mediaCitations",
        "mediaUploadLimits",
        "projectExportIncludesMedia",
        "secretRedaction",
    )
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult(
            "media_layer_evidence",
            STATUS_FAIL,
            f"Media Layer evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    return CheckResult(
        "media_layer_evidence",
        STATUS_PASS,
        "Media Layer evidence recorded",
        {"path": str(path), "checks": list(required)},
    )


def _version_tuple(version: str) -> tuple[int, int, int]:
    parts = [int(part) if part.isdigit() else 0 for part in version.split(".")]
    major, minor, patch = (parts + [0, 0, 0])[:3]
    return major, minor, patch


def check_browser_control_evidence(root: Path, version: str) -> CheckResult:
    if _version_tuple(version) < (2, 8, 0):
        return CheckResult(
            "browser_control_evidence",
            STATUS_PASS,
            "Browser Control evidence not required before v2.8.0",
            {"version": version, "requiredFrom": "2.8.0"},
        )
    path = root / "docs" / "evidence" / f"browser-v{version}.json"
    if not path.is_file():
        return CheckResult(
            "browser_control_evidence",
            STATUS_FAIL,
            "Browser Control evidence missing; run scripts/smoke_browser.py --offline",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("browser_control_evidence", STATUS_FAIL, f"cannot parse Browser Control evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("browser_control", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult(
            "browser_control_evidence",
            STATUS_FAIL,
            f"Browser Control evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
    if data.get("status") != "PASS":
        return CheckResult(
            "browser_control_evidence",
            STATUS_FAIL,
            f"Browser Control evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required: tuple[str, ...] = (
        "browserSessionCreate",
        "readPage",
        "screenshot",
        "extractLinks",
        "unsafeActionBlocked",
        "confirmationRequired",
        "snapshotToMedia",
        "snapshotToRag",
        "auditLog",
    )
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult(
            "browser_control_evidence",
            STATUS_FAIL,
            f"Browser Control evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    return CheckResult(
        "browser_control_evidence",
        STATUS_PASS,
        "Browser Control evidence recorded",
        {"path": str(path), "checks": list(required)},
    )


def check_automation_runtime_evidence(root: Path, version: str) -> CheckResult:
    if _version_tuple(version) < (2, 9, 0):
        return CheckResult(
            "automation_runtime_evidence",
            STATUS_PASS,
            "Automation Runtime evidence not required before v2.9.0",
            {"version": version, "requiredFrom": "2.9.0"},
        )
    path = root / "docs" / "evidence" / f"automation-v{version}.json"
    if not path.is_file():
        return CheckResult(
            "automation_runtime_evidence",
            STATUS_FAIL,
            "Automation Runtime evidence missing; run scripts/smoke_automation.py --offline",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("automation_runtime_evidence", STATUS_FAIL, f"cannot parse Automation Runtime evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("automation_runtime", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult(
            "automation_runtime_evidence",
            STATUS_FAIL,
            f"Automation Runtime evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
    if data.get("status") != "PASS":
        return CheckResult(
            "automation_runtime_evidence",
            STATUS_FAIL,
            f"Automation Runtime evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required: tuple[str, ...] = (
        "automationCreate",
        "manualRun",
        "scheduleTrigger",
        "eventTrigger",
        "runSkillAction",
        "browserReadOnlyAction",
        "projectExportAction",
        "unsafeActionBlocked",
        "runHistory",
        "traceLinked",
        "artifactOutput",
        "templates",
        "evidenceGenerated",
    )
    if _version_tuple(version) >= (2, 9, 1):
        required = (
            *required,
            "browserCheckChanged",
            "browserCheckUnchanged",
            "fixturePathBlocked",
            "cronStepRange",
            "maxRunsPerDay",
            "retryBackoff",
            "timeoutEvidence",
            "rerun",
            "templateCreate",
        )
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult(
            "automation_runtime_evidence",
            STATUS_FAIL,
            f"Automation Runtime evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    return CheckResult(
        "automation_runtime_evidence",
        STATUS_PASS,
        "Automation Runtime evidence recorded",
        {"path": str(path), "checks": list(required)},
    )


def check_semantic_cache_onnx_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / f"semantic-cache-onnx-v{version}.json"
    if not path.is_file():
        return CheckResult(
            "semantic_cache_onnx_evidence",
            STATUS_WARN,
            f"Semantic Cache ONNX evidence missing; run benchmarks/bench_semantic_cache.py --compare --out docs/evidence/semantic-cache-onnx-v{version}.json --markdown docs/evidence/semantic-cache-onnx-v{version}.md",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("semantic_cache_onnx_evidence", STATUS_FAIL, f"cannot parse Semantic Cache ONNX evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("semantic_cache_onnx", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return _optional_stale_evidence("semantic_cache_onnx_evidence", "Semantic Cache ONNX", reported, version, path)
    if data.get("status") != "PASS":
        return CheckResult(
            "semantic_cache_onnx_evidence",
            STATUS_FAIL,
            f"Semantic Cache ONNX evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    hash_data = data.get("hash")
    if isinstance(hash_data, dict):
        if float(hash_data.get("exactHitRate", 0)) < 1.0:
            return CheckResult("semantic_cache_onnx_evidence", STATUS_FAIL, "hash exactHitRate < 1.0", {"hash": hash_data})
        if float(hash_data.get("unrelatedFalseHitRate", 0)) > 0.0:
            return CheckResult("semantic_cache_onnx_evidence", STATUS_FAIL, "hash unrelatedFalseHitRate > 0.0", {"hash": hash_data})
    onnx_data = data.get("onnx")
    if isinstance(onnx_data, dict):
        if float(onnx_data.get("exactHitRate", 0)) < 1.0:
            return CheckResult("semantic_cache_onnx_evidence", STATUS_FAIL, "onnx exactHitRate < 1.0", {"onnx": onnx_data})
        if float(onnx_data.get("unrelatedFalseHitRate", 0)) > 0.0:
            return CheckResult("semantic_cache_onnx_evidence", STATUS_FAIL, "onnx unrelatedFalseHitRate > 0.0", {"onnx": onnx_data})
    return CheckResult(
        "semantic_cache_onnx_evidence",
        STATUS_PASS,
        "Semantic Cache ONNX evidence recorded",
        {"path": str(path), "onnxAvailable": data.get("onnxAvailable")},
    )


def check_skill_system_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / f"skills-v{version}.json"
    if not path.is_file():
        return CheckResult(
            "skill_system_evidence",
            STATUS_FAIL,
            "Skill System evidence missing; run scripts/smoke_skills.py --offline",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("skill_system_evidence", STATUS_FAIL, f"cannot parse Skill System evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("skill_system", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult(
            "skill_system_evidence",
            STATUS_FAIL,
            f"Skill System evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
    if data.get("status") != "PASS":
        return CheckResult(
            "skill_system_evidence",
            STATUS_FAIL,
            f"Skill System evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = (
        "skillApiRoutes",
        "builtinSkillsLoad",
        "customSkillCreate",
        "inputSchemaValidation",
        "toolPermissionGate",
        "artifactPolicy",
        "projectBinding",
        "skillExport",
    )
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult(
            "skill_system_evidence",
            STATUS_FAIL,
            f"Skill System evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    return CheckResult(
        "skill_system_evidence",
        STATUS_PASS,
        "Skill System evidence recorded",
        {"path": str(path), "checks": list(required)},
    )


def check_skill_ui_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / f"skills-ui-v{version}.json"
    if not path.is_file():
        return CheckResult(
            "skill_ui_evidence",
            STATUS_FAIL,
            "Skill Workbench UI evidence missing; run scripts/smoke_skills_ui.py --offline",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("skill_ui_evidence", STATUS_FAIL, f"cannot parse Skill UI evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("skill_ui", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult(
            "skill_ui_evidence",
            STATUS_FAIL,
            f"Skill UI evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
    if data.get("status") != "PASS":
        return CheckResult(
            "skill_ui_evidence",
            STATUS_FAIL,
            f"Skill UI evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = (
        "skillWorkbenchEntrypoint",
        "skillRunSchemaForm",
        "skillApiActions",
        "projectSkillBindingUi",
        "skillRunResultLinks",
        "skillPanelLifecycle",
        "skillPanelStyles",
        "skillAppShellCache",
        "skillUiAssets",
        "skillJsSyntax",
        "ciSyntaxGate",
    )
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult(
            "skill_ui_evidence",
            STATUS_FAIL,
            f"Skill UI evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    return CheckResult(
        "skill_ui_evidence",
        STATUS_PASS,
        "Skill Workbench UI evidence recorded",
        {"path": str(path), "checks": list(required)},
    )


def check_skill_builder_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / f"skill-builder-v{version}.json"
    if not path.is_file():
        return CheckResult(
            "skill_builder_evidence",
            STATUS_FAIL,
            "Skill Builder evidence missing; run scripts/smoke_skill_builder.py --offline",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("skill_builder_evidence", STATUS_FAIL, f"cannot parse Skill Builder evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("skill_builder", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult(
            "skill_builder_evidence",
            STATUS_FAIL,
            f"Skill Builder evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
    if data.get("status") != "PASS":
        return CheckResult(
            "skill_builder_evidence",
            STATUS_FAIL,
            f"Skill Builder evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = (
        "builderOpen",
        "cloneBuiltinSkill",
        "visualInputSchemaEdit",
        "toolPermissionPicker",
        "schemaValidation",
        "offlineDryRun",
        "saveCustomSkill",
        "exportCreatedSkill",
        "skillBuilderStyles",
        "skillBuilderAssets",
        "skillBuilderJsSyntax",
        "ciSyntaxGate",
    )
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult(
            "skill_builder_evidence",
            STATUS_FAIL,
            f"Skill Builder evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    return CheckResult(
        "skill_builder_evidence",
        STATUS_PASS,
        "Skill Builder evidence recorded",
        {"path": str(path), "checks": list(required)},
    )


def check_skill_packs_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / f"skill-packs-v{version}.json"
    if not path.is_file():
        return CheckResult(
            "skill_packs_evidence",
            STATUS_FAIL,
            "Skill Packs evidence missing; run scripts/smoke_skill_packs.py --offline",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("skill_packs_evidence", STATUS_FAIL, f"cannot parse Skill Packs evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("skill_packs", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult(
            "skill_packs_evidence",
            STATUS_FAIL,
            f"Skill Packs evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
    if data.get("status") != "PASS":
        return CheckResult(
            "skill_packs_evidence",
            STATUS_FAIL,
            f"Skill Packs evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = (
        "packSchemaValidation",
        "builtinPacksLoad",
        "packImport",
        "packExport",
        "skillIdConflictHandling",
        "toolPermissionDiff",
        "projectPackBinding",
        "packInstallDryRun",
        "packUiTab",
        "packJsSyntax",
        "ciSyntaxGate",
        "packAssets",
    )
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult(
            "skill_packs_evidence",
            STATUS_FAIL,
            f"Skill Packs evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    return CheckResult(
        "skill_packs_evidence",
        STATUS_PASS,
        "Skill Packs evidence recorded",
        {"path": str(path), "checks": list(required)},
    )


def check_skill_eval_dashboard_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / f"skill-eval-dashboard-v{version}.json"
    report_path = root / "evals" / "reports" / f"skills-v{version}.json"
    if not path.is_file():
        return CheckResult(
            "skill_eval_dashboard_evidence",
            STATUS_FAIL,
            "Skill Eval Dashboard evidence missing; run scripts/smoke_skill_eval_dashboard.py --offline",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("skill_eval_dashboard_evidence", STATUS_FAIL, f"cannot parse Skill Eval Dashboard evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("skill_eval_dashboard", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult(
            "skill_eval_dashboard_evidence",
            STATUS_FAIL,
            f"Skill Eval Dashboard evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
    if data.get("status") != "PASS":
        return CheckResult(
            "skill_eval_dashboard_evidence",
            STATUS_FAIL,
            f"Skill Eval Dashboard evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = (
        "evalDashboardEntrypoint",
        "evalCaseBuilder",
        "skillEvalApiActions",
        "skillEvalReport",
        "packLevelEval",
        "regressionCompare",
        "evalExportActions",
        "skillEvalStyles",
        "skillEvalAssets",
        "skillEvalRunner",
        "skillEvalJsSyntax",
        "ciReleaseGate",
    )
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult(
            "skill_eval_dashboard_evidence",
            STATUS_FAIL,
            f"Skill Eval Dashboard evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    report, error = _load_json_report(root, f"evals/reports/skills-v{version}.json")
    if report is None:
        return CheckResult("skill_eval_dashboard_evidence", STATUS_FAIL, error, {"path": str(report_path)})
    if report.get("status") != "PASS":
        return CheckResult(
            "skill_eval_dashboard_evidence",
            STATUS_FAIL,
            f"Skill eval report status is {report.get('status')!r}, expected PASS",
            {"path": str(report_path), "status": report.get("status")},
        )
    return CheckResult(
        "skill_eval_dashboard_evidence",
        STATUS_PASS,
        "Skill Eval Dashboard evidence recorded",
        {"path": str(path), "report": str(report_path), "checks": list(required)},
    )


def check_skill_versioning_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / f"skill-versioning-v{version}.json"
    if not path.is_file():
        return CheckResult(
            "skill_versioning_evidence",
            STATUS_FAIL,
            "Skill Versioning evidence missing; run scripts/smoke_skill_versioning.py --offline",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("skill_versioning_evidence", STATUS_FAIL, f"cannot parse Skill Versioning evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("skill_versioning", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult(
            "skill_versioning_evidence",
            STATUS_FAIL,
            f"Skill Versioning evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
    if data.get("status") != "PASS":
        return CheckResult(
            "skill_versioning_evidence",
            STATUS_FAIL,
            f"Skill Versioning evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = (
        "skillVersionSnapshot",
        "skillDiff",
        "skillRollback",
        "schemaMigrationPlan",
        "packVersionInstall",
        "packRollback",
        "evalAwareUpgradeGate",
        "projectBindingMigration",
        "versioningApiActions",
        "versioningUi",
        "versioningAssets",
        "versioningJsSyntax",
        "ciReleaseGate",
    )
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult(
            "skill_versioning_evidence",
            STATUS_FAIL,
            f"Skill Versioning evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    return CheckResult(
        "skill_versioning_evidence",
        STATUS_PASS,
        "Skill Versioning evidence recorded",
        {"path": str(path), "checks": list(required)},
    )


def check_skill_analytics_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / f"skill-analytics-v{version}.json"
    if not path.is_file():
        return CheckResult(
            "skill_analytics_evidence",
            STATUS_FAIL,
            "Skill Analytics evidence missing; run scripts/smoke_skill_analytics.py --offline",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("skill_analytics_evidence", STATUS_FAIL, f"cannot parse Skill Analytics evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("skill_analytics", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult(
            "skill_analytics_evidence",
            STATUS_FAIL,
            f"Skill Analytics evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
    if data.get("status") != "PASS":
        return CheckResult(
            "skill_analytics_evidence",
            STATUS_FAIL,
            f"Skill Analytics evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = (
        "skillRunHistory",
        "runMetadataPersist",
        "analyticsSummary",
        "failureDiagnostics",
        "projectRunHistory",
        "traceLink",
        "artifactLink",
        "retentionCleanup",
        "privacyRedaction",
        "analyticsApiActions",
        "analyticsUi",
        "analyticsAssets",
        "analyticsJsSyntax",
        "ciReleaseGate",
    )
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult(
            "skill_analytics_evidence",
            STATUS_FAIL,
            f"Skill Analytics evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    return CheckResult(
        "skill_analytics_evidence",
        STATUS_PASS,
        "Skill Analytics evidence recorded",
        {"path": str(path), "checks": list(required)},
    )


def check_skill_security_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / f"skill-security-v{version}.json"
    if not path.is_file():
        return CheckResult(
            "skill_security_evidence",
            STATUS_FAIL,
            "Skill Security evidence missing; run scripts/smoke_skill_security.py --offline",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("skill_security_evidence", STATUS_FAIL, f"cannot parse Skill Security evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("skill_security", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult(
            "skill_security_evidence",
            STATUS_FAIL,
            f"Skill Security evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
    if data.get("status") != "PASS":
        return CheckResult(
            "skill_security_evidence",
            STATUS_FAIL,
            f"Skill Security evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = (
        "securityReview",
        "promptInjectionScan",
        "secretExfiltrationScan",
        "toolGrantRiskDiff",
        "trustSkill",
        "blockSkill",
        "tamperDetection",
        "securityManifestExport",
        "runSecurityMetadata",
        "securityApiActions",
        "securityUi",
        "securityAssets",
        "securityJsSyntax",
        "ciReleaseGate",
    )
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult(
            "skill_security_evidence",
            STATUS_FAIL,
            f"Skill Security evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    return CheckResult(
        "skill_security_evidence",
        STATUS_PASS,
        "Skill Security evidence recorded",
        {"path": str(path), "checks": list(required)},
    )


def check_skill_catalog_evidence(root: Path, version: str) -> CheckResult:
    path = root / "docs" / "evidence" / f"skill-catalog-v{version}.json"
    if not path.is_file():
        return CheckResult(
            "skill_catalog_evidence",
            STATUS_FAIL,
            "Skill Catalog evidence missing; run scripts/smoke_skill_catalog.py --offline",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("skill_catalog_evidence", STATUS_FAIL, f"cannot parse Skill Catalog evidence: {exc}", {"path": str(path)})
    metadata_fail = _check_evidence_metadata("skill_catalog", data, path)
    if metadata_fail:
        return metadata_fail
    reported = str(data.get("version") or "")
    if reported != version:
        return CheckResult(
            "skill_catalog_evidence",
            STATUS_FAIL,
            f"Skill Catalog evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
    if data.get("status") != "PASS":
        return CheckResult(
            "skill_catalog_evidence",
            STATUS_FAIL,
            f"Skill Catalog evidence status is {data.get('status')!r}, expected PASS",
            {"status": data.get("status")},
        )
    checks = data.get("checks")
    check_status = {str(k): str(v).upper() for k, v in checks.items()} if isinstance(checks, dict) else {}
    required = (
        "catalogManifest",
        "catalogList",
        "catalogSearch",
        "catalogInstallPreview",
        "catalogInstall",
        "catalogUninstall",
        "securityGateBeforeInstall",
        "evalScoreShown",
        "toolPermissionSummary",
        "catalogExport",
        "catalogApiActions",
        "catalogUi",
        "catalogAssets",
        "catalogJsSyntax",
        "ciReleaseGate",
    )
    missing_or_failed = [name for name in required if check_status.get(name) != "PASS"]
    if missing_or_failed:
        return CheckResult(
            "skill_catalog_evidence",
            STATUS_FAIL,
            f"Skill Catalog evidence missing PASS checks: {', '.join(missing_or_failed)}",
            {"missingOrFailed": missing_or_failed},
        )
    return CheckResult(
        "skill_catalog_evidence",
        STATUS_PASS,
        "Skill Catalog evidence recorded",
        {"path": str(path), "checks": list(required)},
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


def _check_evidence_metadata(name: str, data: dict[str, Any], path: Path) -> CheckResult | None:
    """Validate unified evidence metadata fields.

    Returns None if all required fields are present, otherwise a FAIL result.
    """
    required = ("version", "commit", "generatedAt", "environment", "status")
    missing = [key for key in required if not data.get(key)]
    if missing:
        return CheckResult(
            f"evidence_metadata:{name}",
            STATUS_FAIL,
            f"{path.name} missing unified metadata fields: {', '.join(missing)}",
            {"path": str(path), "missing": missing},
        )
    env = data.get("environment")
    if not isinstance(env, dict) or not all(k in env for k in ("os", "python", "ci")):
        return CheckResult(
            f"evidence_metadata:{name}",
            STATUS_FAIL,
            f"{path.name} environment metadata incomplete (expected os/python/ci)",
            {"path": str(path), "environment": env},
        )
    return None


def run_preflight(root: Path, version: str, *, ga: bool = False) -> list[CheckResult]:
    results = [
        check_readme_badge(root, version),
        check_changelog_entry(root, version),
        check_dockerfile_tag(root, version),
        check_doc_version(root, "docs/IMPLEMENTATION_STATUS.md", version),
        check_doc_version(root, "evals/README.md", version),
        check_docs_encoding_sanity(root),
        check_doc_links_exist(root),
        check_eval_report_version(root, version),
        check_agent_report(root, version),
        check_baseline_compare_report(root, version),
        check_security_corpus_report(root, version),
        check_quality_gate_evidence(root, version),
        check_release_exclusions(root),
        check_headless_mcp_bridge_evidence(root, version),
        check_a2a_external_peer_evidence(root, version),
        check_a2a_third_party_peer_evidence(root, version),
        check_edge_router_smoke_evidence(root, version),
        check_edge_router_evidence(root, version),
        check_continue_dev_mcp_evidence(root, version),
        check_openai_compatible_sdk_evidence(root, version),
        check_semantic_cache_onnx_evidence(root, version),
        check_workspace_core_evidence(root, version),
        check_context_taint_evidence(root, version),
        check_media_layer_evidence(root, version),
        check_browser_control_evidence(root, version),
        check_automation_runtime_evidence(root, version),
        check_skill_system_evidence(root, version),
        check_skill_ui_evidence(root, version),
        check_skill_builder_evidence(root, version),
        check_skill_packs_evidence(root, version),
        check_skill_eval_dashboard_evidence(root, version),
        check_skill_versioning_evidence(root, version),
        check_skill_analytics_evidence(root, version),
        check_skill_security_evidence(root, version),
        check_skill_catalog_evidence(root, version),
        check_gui_interop_evidence(root),
    ]
    if ga:
        results.extend(
            [
                check_ga_evidence(root, version),
                check_ga_demo_assets(root),
                check_ga_docs_roster(root),
                check_ga_evidence_index(root, version),
                check_ga_release_manifest(root, version),
                check_ga_release_exclusions(root),
            ]
        )
    return results


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
    parser.add_argument("--ga", action="store_true", help="Run GA release gates for v3.0 Personal AI Runtime.")
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable JSON summary.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    version = args.version
    if not version:
        from deepseek_infra.core.config import settings

        version = settings.app_version
    results = run_preflight(args.root.resolve(), version, ga=bool(args.ga))
    if args.json:
        print(dump_json(results, version))
    else:
        print(render_text(results))
    return 1 if any(r.status == STATUS_FAIL for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

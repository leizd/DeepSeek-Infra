#!/usr/bin/env python3
"""Release Preflight — verify version sync and release evidence before tagging.

Checks that the version string is consistent across the README badge,
CHANGELOG, Dockerfile tag, Implementation Status / evals README headers, that
the eval / agent reports are current, that the smoke / eval docs exist, that
``scripts/release.py`` still excludes runtime caches and logs, that headless MCP
bridge and A2A external peer evidence are present, that optional third-party A2A
and Edge Router evidence is strict when submitted, that key docs do not contain
encoding corruption (since v2.3.4), and (since v2.3.1) that GUI interop evidence
for Claude Desktop / Cursor has been recorded in ``docs/COMPATIBILITY.md``.

    python scripts/preflight_release.py --version 2.4.6

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
    re.compile(r"锟斤拷"),  # classic GBK/UTF-8 mojibake
    re.compile(r"\ufffd"),  # Unicode replacement character
)
GARBLED_DOC_PATHS = (
    "CHANGELOG.md",
    "README.md",
    "docs/COMPATIBILITY.md",
    "docs/IMPLEMENTATION_STATUS.md",
    "docs/RELEASE_READINESS.md",
    "docs/EVIDENCE_INDEX.md",
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
    garbled patterns (e.g. `` `???` ``) inside documentation do not trigger
    the sanity check.
    """
    text = _FENCE_RE.sub("", text)
    return _INLINE_CODE_RE.sub("", text)


def check_docs_encoding_sanity(root: Path) -> CheckResult:
    """Detect encoding corruption in key human-readable docs.

    Catches the kind of mojibake that appeared in CHANGELOG.md for v2.3.3
    before it was polished in v2.3.4. Inline code spans and fenced blocks are
    ignored because they may intentionally document the checked patterns.
    """
    findings: list[dict[str, Any]] = []
    checked_paths: list[str] = []
    for rel in GARBLED_DOC_PATHS:
        path = root / rel
        if not path.is_file():
            continue
        checked_paths.append(rel)
        text = _strip_code_spans(_read(path))
        for pattern in GARBLED_PATTERNS:
            for match in pattern.finditer(text):
                findings.append({"path": rel, "pattern": pattern.pattern, "snippet": text[max(0, match.start() - 20):match.end() + 20]})
    # Also scan docs/integrations/*.md which are the primary interoperability runbooks.
    integrations_dir = root / "docs" / "integrations"
    if integrations_dir.is_dir():
        checked_paths.append("docs/integrations/*.md")
        for path in integrations_dir.glob("*.md"):
            text = _strip_code_spans(_read(path))
            for pattern in GARBLED_PATTERNS:
                for match in pattern.finditer(text):
                    findings.append({"path": f"docs/integrations/{path.name}", "pattern": pattern.pattern, "snippet": text[max(0, match.start() - 20):match.end() + 20]})
    if findings:
        paths = sorted({f["path"] for f in findings})
        return CheckResult(
            "docs_encoding_sanity",
            STATUS_FAIL,
            f"encoding corruption detected in {', '.join(paths)}; fix mojibake before release",
            {"findings": findings[:10]},
        )
    return CheckResult("docs_encoding_sanity", STATUS_PASS, "no encoding corruption in key docs", {"checked": checked_paths})


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
    if coverage_gate < 80:
        failures.append(f"coverage fail_under is {coverage_gate:g}, expected >= 80")
    ci_text = _read(root / ".github" / "workflows" / "ci.yml")
    if "pytest --cov --cov-fail-under=80" not in ci_text:
        failures.append("CI pytest coverage gate is not --cov-fail-under=80")
    report_specs = (
        ("evals/reports/latest.json", "offlineEval"),
        ("evals/reports/agent-latest.json", "agentEval"),
        ("evals/reports/baseline-compare-latest.json", "baselineCompare"),
        ("evals/reports/security-latest.json", "securityCorpus"),
    )
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
    return CheckResult("quality_gate_evidence", STATUS_PASS, "v2.4 quality gate evidence is complete", details)


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
        return CheckResult(
            "a2a_third_party_peer_evidence",
            STATUS_FAIL,
            f"third-party A2A evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
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
        return CheckResult(
            "edge_router_smoke_evidence",
            STATUS_FAIL,
            f"Edge Router smoke evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
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
        return CheckResult(
            "continue_dev_mcp_evidence",
            STATUS_FAIL,
            f"Continue.dev MCP evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
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
        return CheckResult(
            "openai_compatible_sdk_evidence",
            STATUS_FAIL,
            f"OpenAI-compatible SDK evidence version is {reported!r}, expected {version!r}",
            {"version": reported, "expected": version},
        )
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


def run_preflight(root: Path, version: str) -> list[CheckResult]:
    return [
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
        check_continue_dev_mcp_evidence(root, version),
        check_openai_compatible_sdk_evidence(root, version),
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

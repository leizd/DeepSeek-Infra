"""Release manifest & checksum — verifiable release evidence.

For every release zip we emit two sibling artifacts so a downstream consumer can
verify what was built and that the bytes match:

- ``deepseek-infra-<version>.zip.sha256`` — hex digest of the zip.
- ``deepseek-infra-<version>.manifest.json`` — version, commit, build time,
  Python, coverage gate, eval / agent report paths, artifact name, sha256 and
  byte size.

This is the release-side counterpart to the eval evidence (``latest.json`` /
``agent-latest.json``): v2.2.7 / v2.2.8 produced *eval* evidence; v2.2.9 adds
*release* evidence so a release is self-describing and tamper-evident.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "release-manifest.v1"
_CHUNK = 1024 * 1024


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


DEFAULT_EVIDENCE_PATHS = (
    "docs/evidence/headless-mcp-bridge.json",
    "docs/evidence/a2a-external-peer.json",
    "docs/evidence/a2a-third-party-peer.json",
    "docs/evidence/edge-router-smoke.json",
    "docs/evidence/edge-router-v3.3.0.json",
    "docs/evidence/continue-dev-mcp.json",
    "docs/evidence/openai-compatible-sdks.json",
    "docs/evidence/ga-v3.3.0.json",
    "docs/evidence/workspace-v3.3.0.json",
    "docs/evidence/media-v3.3.0.json",
    "docs/evidence/browser-v3.3.0.json",
    "docs/evidence/automation-v3.3.0.json",
    "docs/evidence/skills-v3.3.0.json",
    "docs/evidence/skills-ui-v3.3.0.json",
    "docs/evidence/skill-builder-v3.3.0.json",
    "docs/evidence/skill-packs-v3.3.0.json",
    "docs/evidence/skill-eval-dashboard-v3.3.0.json",
    "docs/evidence/skill-versioning-v3.3.0.json",
    "docs/evidence/skill-analytics-v3.3.0.json",
    "docs/evidence/skill-security-v3.3.0.json",
    "docs/evidence/skill-catalog-v3.3.0.json",
    "docs/evidence/context-taint-v3.3.0.json",
    "evals/reports/latest.json",
    "evals/reports/agent-latest.json",
    "evals/reports/baseline-compare-latest.json",
    "evals/reports/security-latest.json",
    "evals/reports/skills-v3.3.0.json",
    "evals/reports/media-v3.3.0.json",
    "evals/reports/browser-v3.3.0.json",
    "evals/reports/automation-v3.3.0.json",
    "docs/EVIDENCE_INDEX.md",
)

DEFAULT_QUALITY_GATES = {
    "coverage": "80%",
    "offlineEval": "PASS",
    "agentEval": "PASS",
    "injectionStrict": "PASS",
    "baselineCompare": "PASS",
    "securityCorpus": "PASS",
    "gaEvidence": "PASS",
    "edgeRouter": "PASS",
    "workspaceCore": "PASS",
    "mediaLayer": "PASS",
    "browserControl": "PASS",
    "automationRuntime": "PASS",
    "contextTaint": "PASS",
    "skillSystem": "PASS",
    "skillWorkbench": "PASS",
    "skillBuilder": "PASS",
    "skillPacks": "PASS",
    "skillEvalDashboard": "PASS",
    "skillVersioning": "PASS",
    "skillAnalytics": "PASS",
    "skillSecurity": "PASS",
    "skillCatalog": "PASS",
}


def build_manifest(
    *,
    version: str,
    commit: str,
    built_at: str | None = None,
    python_version: str,
    coverage_gate: str,
    eval_report: str,
    agent_report: str,
    artifact: Path,
    sha256: str,
    evidence: list[str] | None = None,
    quality_gates: dict[str, str] | None = None,
) -> dict[str, Any]:
    evidence_paths = list(evidence) if evidence is not None else list(DEFAULT_EVIDENCE_PATHS)
    ga_evidence = next((path for path in evidence_paths if path.startswith("docs/evidence/ga-v")), f"docs/evidence/ga-v{version}.json")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "version": version,
        "commit": commit,
        "builtAt": built_at or utc_now(),
        "python": python_version,
        "coverageGate": coverage_gate,
        "qualityGates": dict(quality_gates) if quality_gates is not None else dict(DEFAULT_QUALITY_GATES),
        "evalReport": eval_report,
        "agentReport": agent_report,
        "gaEvidence": ga_evidence,
        "evidence": evidence_paths,
        "artifact": artifact.name,
        "sha256": sha256,
        "bytes": artifact.stat().st_size,
    }


def checksum_path_for(artifact: Path) -> Path:
    return artifact.with_suffix(artifact.suffix + ".sha256")


def manifest_path_for(artifact: Path) -> Path:
    return artifact.with_name(artifact.stem + ".manifest.json")


def write_checksum(artifact: Path, sha256: str) -> Path:
    target = checksum_path_for(artifact)
    target.write_text(f"{sha256}  {artifact.name}\n", encoding="utf-8")
    return target


def write_manifest(artifact: Path, manifest: dict[str, Any]) -> Path:
    target = manifest_path_for(artifact)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def verify_checksum(artifact: Path, expected_sha256: str) -> bool:
    return sha256_of(artifact).lower() == expected_sha256.strip().lower()

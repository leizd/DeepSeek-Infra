"""Release manifest & checksum — verifiable release evidence.

For every release zip we emit two sibling artifacts so a downstream consumer can
verify what was built and that the bytes match:

- ``deepseek-infra-<version>.zip.sha256`` — hex digest of the zip.
- ``deepseek-infra-<version>.zip.manifest.json`` — version, commit, build time,
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

from deepseek_infra.core.config import APP_VERSION

SCHEMA_VERSION = "release-manifest.v2"
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
    f"docs/evidence/edge-router-v{APP_VERSION}.json",
    "docs/evidence/continue-dev-mcp.json",
    "docs/evidence/openai-compatible-sdks.json",
    f"docs/evidence/ga-v{APP_VERSION}.json",
    f"docs/evidence/workspace-v{APP_VERSION}.json",
    f"docs/evidence/media-v{APP_VERSION}.json",
    f"docs/evidence/browser-v{APP_VERSION}.json",
    f"docs/evidence/frontend-browser-v{APP_VERSION}.json",
    f"docs/evidence/frontend-bundle-v{APP_VERSION}.json",
    f"docs/evidence/automation-v{APP_VERSION}.json",
    f"docs/evidence/skills-v{APP_VERSION}.json",
    f"docs/evidence/skills-ui-v{APP_VERSION}.json",
    f"docs/evidence/skill-builder-v{APP_VERSION}.json",
    f"docs/evidence/skill-packs-v{APP_VERSION}.json",
    f"docs/evidence/skill-eval-dashboard-v{APP_VERSION}.json",
    f"docs/evidence/skill-versioning-v{APP_VERSION}.json",
    f"docs/evidence/skill-analytics-v{APP_VERSION}.json",
    f"docs/evidence/skill-security-v{APP_VERSION}.json",
    f"docs/evidence/skill-catalog-v{APP_VERSION}.json",
    f"docs/evidence/context-taint-v{APP_VERSION}.json",
    f"docs/evidence/semantic-cache-onnx-v{APP_VERSION}.json",
    f"docs/evidence/gateway-request-parity-v{APP_VERSION}.json",
    f"docs/evidence/mcp-protocol-parity-v{APP_VERSION}.json",
    f"docs/evidence/rag-parity-v{APP_VERSION}.json",
    f"docs/evidence/rag-document-preparation-parity-v{APP_VERSION}.json",
    f"docs/evidence/rag-vector-binary-parity-v{APP_VERSION}.json",
    f"docs/evidence/rust-sidecar-performance-v{APP_VERSION}.json",
    f"docs/evidence/rust-coverage-v{APP_VERSION}.json",
    f"docs/evidence/python-coverage-stability-v{APP_VERSION}.json",
    f"docs/evidence/rust-sidecar-image-v{APP_VERSION}.json",
    f"docs/evidence/hybrid-runtime-e2e-v{APP_VERSION}.json",
    f"docs/evidence/upgrade-rollback-v{APP_VERSION}.json",
    f"docs/evidence/protocol-contract-v{APP_VERSION}.json",
    "evals/reports/latest.json",
    "evals/reports/agent-latest.json",
    "evals/reports/baseline-compare-latest.json",
    "evals/reports/security-latest.json",
    f"evals/reports/skills-v{APP_VERSION}.json",
    f"evals/reports/media-v{APP_VERSION}.json",
    f"evals/reports/browser-v{APP_VERSION}.json",
    f"evals/reports/automation-v{APP_VERSION}.json",
    "docs/EVIDENCE_INDEX.md",
    "docs/releases/4.1.1.md",
    "docs/releases/4.1.0.md",
    "docs/releases/4.0.3.md",
    "docs/releases/4.0.1.md",
    "docs/UPGRADING_TO_4_0.md",
    "docs/4_0_SUPPORT_POLICY.md",
    "release/4_0_runtime_decision.json",
    "release/4_0_protocol_contract.json",
    "docs/RUST_CANDIDATE_AUDIT_3_4.md",
    "docs/GATEWAY_REQUEST_PREPARATION_PARITY.md",
    "docs/MCP_PROTOCOL_PREPARATION_PARITY.md",
    "docs/RAG_DOCUMENT_PREPARATION_PARITY.md",
    "docs/RUST_SIDECAR_PERFORMANCE.md",
    "docs/RAG_VECTOR_BINARY_TRANSPORT.md",
    "docs/SEMANTIC_CACHE_BINARY_EMBEDDINGS.md",
)

DEFAULT_QUALITY_GATES = {
    "coverage": "95%",
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
    "frontendBrowser": "PASS",
    "frontendBundle": "PASS",
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
    "gatewayRequestParity": "PASS",
    "mcpProtocolParity": "PASS",
    "ragDocumentPreparationParity": "PASS",
    "rustSidecarPerformance": "PASS",
    "ragVectorBinaryParity": "PASS",
    "semanticCacheBinaryEmbeddings": "PASS",
    "rustCoverage": "PASS",
    "upgradeRollback": "PASS",
    "protocolFreeze": "PASS",
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
    python_coverage: float | None = None,
    rust_coverage: float | None = None,
    rust_test_count: int | None = None,
    parity_counts: dict[str, Any] | None = None,
    architecture_decision_sha256: str = "",
    protocol_contract_sha256: str = "",
    rust_sidecar_image_tag: str = "",
    rust_sidecar_image_digest: str = "",
) -> dict[str, Any]:
    evidence_paths = list(evidence) if evidence is not None else list(DEFAULT_EVIDENCE_PATHS)
    ga_evidence = next((path for path in evidence_paths if path.startswith("docs/evidence/ga-v")), f"docs/evidence/ga-v{version}.json")
    gates = dict(quality_gates) if quality_gates is not None else dict(DEFAULT_QUALITY_GATES)
    gates["coverage"] = coverage_gate
    return {
        "schemaVersion": SCHEMA_VERSION,
        "version": version,
        "commit": commit,
        "builtAt": built_at or utc_now(),
        "python": python_version,
        "coverageGate": coverage_gate,
        "pythonCoverage": {"percent": python_coverage, "gate": coverage_gate},
        "rustCoverage": {"linePercent": rust_coverage, "minimumPercent": 80.0},
        "rustTestCount": rust_test_count,
        "parityCounts": dict(parity_counts or {}),
        "architectureDecisionSha256": architecture_decision_sha256,
        "protocolContractSha256": protocol_contract_sha256,
        "archiveSha256": sha256,
        "rustSidecarImage": {
            "tag": rust_sidecar_image_tag or f"deepseek-rust-gateway:{version}",
            "digest": rust_sidecar_image_digest or "not-published",
        },
        "runtimeDefaults": {
            "authoritativeRuntime": "python",
            "defaultCompose": "python-only",
            "rustDelegates": "disabled",
            "pythonFallback": "supported-through-4.x",
        },
        "qualityGates": gates,
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
    return artifact.with_suffix(artifact.suffix + ".manifest.json")


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

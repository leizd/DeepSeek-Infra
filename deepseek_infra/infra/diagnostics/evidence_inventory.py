"""Single source of truth for release Evidence ownership and release tier."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EvidenceTier = Literal["candidate", "exact-merge", "optional"]


@dataclass(frozen=True)
class EvidenceSpec:
    path_template: str
    producer: str
    tier: EvidenceTier
    required_for_ga: bool = True

    def path(self, version: str) -> str:
        return self.path_template.format(version=version)


EVIDENCE_SPECS = (
    EvidenceSpec("docs/evidence/headless-mcp-bridge.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/a2a-external-peer.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/ga-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/workspace-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/edge-router-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/media-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/browser-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/automation-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/skills-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/skills-ui-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/skill-builder-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/skill-packs-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/skill-eval-dashboard-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/skill-versioning-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/skill-analytics-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/skill-security-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/skill-catalog-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/context-taint-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/semantic-cache-onnx-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/upgrade-rollback-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/protocol-contract-v{version}.json", "release-readiness", "candidate"),
    EvidenceSpec("docs/evidence/frontend-bundle-v{version}.json", "frontend", "candidate"),
    EvidenceSpec("docs/evidence/frontend-browser-v{version}.json", "frontend-browser", "candidate"),
    EvidenceSpec("evals/reports/latest.json", "eval", "candidate"),
    EvidenceSpec("evals/reports/agent-latest.json", "eval", "candidate"),
    EvidenceSpec("evals/reports/baseline-compare-latest.json", "eval", "candidate"),
    EvidenceSpec("evals/reports/security-latest.json", "eval", "candidate"),
    EvidenceSpec("evals/reports/skills-v{version}.json", "eval", "candidate"),
    EvidenceSpec("evals/reports/media-v{version}.json", "eval", "candidate"),
    EvidenceSpec("evals/reports/browser-v{version}.json", "eval", "candidate"),
    EvidenceSpec("evals/reports/automation-v{version}.json", "eval", "candidate"),
    EvidenceSpec("docs/evidence/rust-sidecar-image-v{version}.json", "rust-docker", "exact-merge"),
    EvidenceSpec("docs/evidence/hybrid-runtime-e2e-v{version}.json", "hybrid-runtime-e2e", "exact-merge"),
    EvidenceSpec("docs/evidence/gateway-request-parity-v{version}.json", "gateway-request-parity", "exact-merge"),
    EvidenceSpec("docs/evidence/mcp-protocol-parity-v{version}.json", "mcp-protocol-parity", "exact-merge"),
    EvidenceSpec("docs/evidence/rag-parity-v{version}.json", "rag-parity", "exact-merge"),
    EvidenceSpec(
        "docs/evidence/rag-document-preparation-parity-v{version}.json",
        "rag-document-preparation-parity",
        "exact-merge",
    ),
    EvidenceSpec("docs/evidence/rag-vector-binary-parity-v{version}.json", "rag-vector-binary-parity", "exact-merge"),
    EvidenceSpec("docs/evidence/rust-coverage-v{version}.json", "rust-coverage", "exact-merge"),
    EvidenceSpec("docs/evidence/rust-sidecar-performance-v{version}.json", "rust-sidecar-performance", "exact-merge"),
    EvidenceSpec("docs/evidence/python-coverage-stability-v{version}.json", "test", "optional", required_for_ga=False),
)


def evidence_specs(*, required_only: bool = True) -> tuple[EvidenceSpec, ...]:
    if not required_only:
        return EVIDENCE_SPECS
    return tuple(spec for spec in EVIDENCE_SPECS if spec.required_for_ga)


def evidence_specs_for_producer(producer: str, *, required_only: bool = True) -> tuple[EvidenceSpec, ...]:
    return tuple(spec for spec in evidence_specs(required_only=required_only) if spec.producer == producer)


def evidence_paths(version: str, *, required_only: bool = True) -> tuple[str, ...]:
    return tuple(spec.path(version) for spec in evidence_specs(required_only=required_only))


def evidence_paths_for_producer(producer: str, version: str, *, required_only: bool = True) -> tuple[str, ...]:
    return tuple(spec.path(version) for spec in evidence_specs_for_producer(producer, required_only=required_only))


def evidence_producers(*, required_only: bool = True) -> tuple[str, ...]:
    return tuple(dict.fromkeys(spec.producer for spec in evidence_specs(required_only=required_only)))


def evidence_spec_by_path(version: str, *, required_only: bool = True) -> dict[str, EvidenceSpec]:
    return {spec.path(version): spec for spec in evidence_specs(required_only=required_only)}

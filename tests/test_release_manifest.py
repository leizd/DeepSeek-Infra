from __future__ import annotations

import hashlib
import json
from pathlib import Path

from deepseek_infra.infra.diagnostics import release_manifest


def test_sha256_of_matches_hashlib(tmp_path: Path) -> None:
    payload = b"deepseek-infra-release-bytes" * 4096
    path = tmp_path / "artifact.zip"
    path.write_bytes(payload)
    assert release_manifest.sha256_of(path) == hashlib.sha256(payload).hexdigest()


def test_build_manifest_has_required_fields(tmp_path: Path) -> None:
    artifact = tmp_path / "deepseek-infra-2.2.9.zip"
    artifact.write_bytes(b"zip-bytes")
    manifest = release_manifest.build_manifest(
        version="2.2.9",
        commit="abc1234",
        built_at="2026-06-27T00:00:00Z",
        python_version="3.12",
        coverage_gate="80%",
        eval_report="evals/reports/latest.json",
        agent_report="evals/reports/agent-latest.json",
        artifact=artifact,
        sha256="deadbeef",
        python_coverage=95.25,
        rust_coverage=80.27,
        rust_test_count=172,
        parity_counts={"gateway": 68, "mcp": 105},
        architecture_decision_sha256="aa" * 32,
        protocol_contract_sha256="bb" * 32,
        rust_sidecar_image_tag="deepseek-rust-gateway:4.0.2",
        rust_sidecar_image_digest="sha256:1234",
    )
    assert manifest["schemaVersion"] == release_manifest.SCHEMA_VERSION
    assert manifest["version"] == "2.2.9"
    assert manifest["commit"] == "abc1234"
    assert manifest["builtAt"] == "2026-06-27T00:00:00Z"
    assert manifest["python"] == "3.12"
    assert manifest["coverageGate"] == "80%"
    assert manifest["qualityGates"]["coverage"] == "80%"
    assert manifest["qualityGates"]["agentEval"] == "PASS"
    assert manifest["qualityGates"]["gaEvidence"] == "PASS"
    assert manifest["qualityGates"]["edgeRouter"] == "PASS"
    assert manifest["qualityGates"]["workspaceCore"] == "PASS"
    assert manifest["qualityGates"]["mediaLayer"] == "PASS"
    assert manifest["qualityGates"]["browserControl"] == "PASS"
    assert manifest["qualityGates"]["frontendBrowser"] == "PASS"
    assert manifest["qualityGates"]["automationRuntime"] == "PASS"
    assert manifest["qualityGates"]["contextTaint"] == "PASS"
    assert manifest["qualityGates"]["skillSystem"] == "PASS"
    assert manifest["qualityGates"]["skillWorkbench"] == "PASS"
    assert manifest["qualityGates"]["skillBuilder"] == "PASS"
    assert manifest["qualityGates"]["skillPacks"] == "PASS"
    assert manifest["qualityGates"]["skillEvalDashboard"] == "PASS"
    assert manifest["qualityGates"]["skillVersioning"] == "PASS"
    assert manifest["qualityGates"]["skillAnalytics"] == "PASS"
    assert manifest["qualityGates"]["skillSecurity"] == "PASS"
    assert manifest["qualityGates"]["skillCatalog"] == "PASS"
    assert manifest["qualityGates"]["gatewayRequestParity"] == "PASS"
    assert manifest["artifact"] == "deepseek-infra-2.2.9.zip"
    assert manifest["sha256"] == "deadbeef"
    assert manifest["bytes"] == len(b"zip-bytes")
    assert manifest["pythonCoverage"] == {"percent": 95.25, "gate": "80%"}
    assert manifest["rustCoverage"] == {"linePercent": 80.27, "minimumPercent": 80.0}
    assert manifest["rustTestCount"] == 172
    assert manifest["parityCounts"] == {"gateway": 68, "mcp": 105}
    assert manifest["architectureDecisionSha256"] == "aa" * 32
    assert manifest["protocolContractSha256"] == "bb" * 32
    assert manifest["archiveSha256"] == "deadbeef"
    assert manifest["rustSidecarImage"] == {
        "tag": "deepseek-rust-gateway:4.0.2",
        "digest": "sha256:1234",
    }
    assert manifest["runtimeDefaults"]["authoritativeRuntime"] == "python"
    assert manifest["runtimeDefaults"]["defaultCompose"] == "python-only"
    assert "evidence" in manifest
    assert isinstance(manifest["evidence"], list)
    assert manifest["gaEvidence"] == "docs/evidence/ga-v4.0.2.json"
    assert "docs/evidence/headless-mcp-bridge.json" in manifest["evidence"]
    assert "docs/evidence/a2a-third-party-peer.json" in manifest["evidence"]
    assert "docs/evidence/edge-router-smoke.json" in manifest["evidence"]
    assert "docs/evidence/edge-router-v4.0.2.json" in manifest["evidence"]
    assert "docs/evidence/ga-v4.0.2.json" in manifest["evidence"]
    assert "docs/evidence/workspace-v4.0.2.json" in manifest["evidence"]
    assert "docs/evidence/context-taint-v4.0.2.json" in manifest["evidence"]
    assert "docs/evidence/semantic-cache-onnx-v4.0.2.json" in manifest["evidence"]
    assert "docs/RUST_CANDIDATE_AUDIT_3_4.md" in manifest["evidence"]
    assert "docs/evidence/media-v4.0.2.json" in manifest["evidence"]
    assert "docs/evidence/browser-v4.0.2.json" in manifest["evidence"]
    assert "docs/evidence/frontend-browser-v4.0.2.json" in manifest["evidence"]
    assert "docs/evidence/automation-v4.0.2.json" in manifest["evidence"]
    assert "docs/evidence/skills-v4.0.2.json" in manifest["evidence"]
    assert "docs/evidence/skills-ui-v4.0.2.json" in manifest["evidence"]
    assert "docs/evidence/skill-builder-v4.0.2.json" in manifest["evidence"]
    assert "docs/evidence/skill-packs-v4.0.2.json" in manifest["evidence"]
    assert "docs/evidence/skill-eval-dashboard-v4.0.2.json" in manifest["evidence"]
    assert "docs/evidence/skill-versioning-v4.0.2.json" in manifest["evidence"]
    assert "docs/evidence/skill-analytics-v4.0.2.json" in manifest["evidence"]
    assert "docs/evidence/skill-security-v4.0.2.json" in manifest["evidence"]
    assert "docs/evidence/skill-catalog-v4.0.2.json" in manifest["evidence"]
    assert "evals/reports/skills-v4.0.2.json" in manifest["evidence"]
    assert "evals/reports/media-v4.0.2.json" in manifest["evidence"]
    assert "evals/reports/browser-v4.0.2.json" in manifest["evidence"]
    assert "evals/reports/automation-v4.0.2.json" in manifest["evidence"]
    assert "docs/MCP_PROTOCOL_PREPARATION_PARITY.md" in manifest["evidence"]
    assert manifest["qualityGates"]["mcpProtocolParity"] == "PASS"
    assert manifest["qualityGates"]["ragDocumentPreparationParity"] == "PASS"
    assert "docs/RAG_DOCUMENT_PREPARATION_PARITY.md" in manifest["evidence"]
    assert "docs/evidence/rust-sidecar-performance-v4.0.2.json" in manifest["evidence"]
    assert "docs/evidence/rag-vector-binary-parity-v4.0.2.json" in manifest["evidence"]
    assert "docs/RUST_SIDECAR_PERFORMANCE.md" in manifest["evidence"]
    assert "docs/RAG_VECTOR_BINARY_TRANSPORT.md" in manifest["evidence"]
    assert "docs/SEMANTIC_CACHE_BINARY_EMBEDDINGS.md" in manifest["evidence"]
    assert manifest["qualityGates"]["rustSidecarPerformance"] == "PASS"
    assert manifest["qualityGates"]["ragVectorBinaryParity"] == "PASS"
    assert manifest["qualityGates"]["semanticCacheBinaryEmbeddings"] == "PASS"
    assert "evals/reports/security-latest.json" in manifest["evidence"]
    assert "docs/EVIDENCE_INDEX.md" in manifest["evidence"]


def test_build_manifest_uses_custom_evidence_when_provided(tmp_path: Path) -> None:
    artifact = tmp_path / "deepseek-infra-2.3.4.zip"
    artifact.write_bytes(b"zip-bytes")
    manifest = release_manifest.build_manifest(
        version="2.3.4",
        commit="abc1234",
        python_version="3.12",
        coverage_gate="80%",
        eval_report="evals/reports/latest.json",
        agent_report="evals/reports/agent-latest.json",
        artifact=artifact,
        sha256="deadbeef",
        evidence=["docs/evidence/custom.json"],
    )
    assert manifest["evidence"] == ["docs/evidence/custom.json"]


def test_write_checksum_format(tmp_path: Path) -> None:
    artifact = tmp_path / "deepseek-infra-2.2.9.zip"
    artifact.write_bytes(b"zip")
    path = release_manifest.write_checksum(artifact, "abc123")
    assert path == artifact.with_suffix(".zip.sha256")
    line = path.read_text(encoding="utf-8")
    assert line.startswith("abc123  ")
    assert line.rstrip().endswith("deepseek-infra-2.2.9.zip")


def test_write_manifest_roundtrip(tmp_path: Path) -> None:
    artifact = tmp_path / "deepseek-infra-2.2.9.zip"
    artifact.write_bytes(b"zip")
    manifest = release_manifest.build_manifest(
        version="2.2.9",
        commit="abc",
        python_version="3.12",
        coverage_gate="80%",
        eval_report="evals/reports/latest.json",
        agent_report="evals/reports/agent-latest.json",
        artifact=artifact,
        sha256="abc123",
    )
    path = release_manifest.write_manifest(artifact, manifest)
    assert path == artifact.with_name("deepseek-infra-2.2.9.zip.manifest.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == "2.2.9"
    assert data["sha256"] == "abc123"


def test_checksum_and_manifest_path_helpers(tmp_path: Path) -> None:
    artifact = tmp_path / "deepseek-infra-2.2.9.zip"
    assert release_manifest.checksum_path_for(artifact).name == "deepseek-infra-2.2.9.zip.sha256"
    assert release_manifest.manifest_path_for(artifact).name == "deepseek-infra-2.2.9.zip.manifest.json"


def test_verify_checksum(tmp_path: Path) -> None:
    payload = b"verify-me" * 100
    artifact = tmp_path / "a.zip"
    artifact.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    assert release_manifest.verify_checksum(artifact, digest) is True
    assert release_manifest.verify_checksum(artifact, "00" * 32) is False


def test_release_script_emits_manifest_and_checksum(tmp_path: Path) -> None:
    import subprocess
    import sys

    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "README.md").write_text("ok", encoding="utf-8")
    (workspace / "static").mkdir()
    (workspace / "static" / "app.js").write_text("console.log('ok');", encoding="utf-8")
    out = tmp_path / "dist"
    script = (Path(__file__).resolve().parents[1] / "scripts" / "release.py")
    result = subprocess.run(
        [sys.executable, str(script), "--root", str(workspace), "--output-dir", str(out), "--version", "2.2.9"],
        check=True,
        capture_output=True,
        text=True,
    )
    artifact = Path(result.stdout.strip())
    assert artifact.is_file()
    checksum = release_manifest.checksum_path_for(artifact)
    manifest = release_manifest.manifest_path_for(artifact)
    assert checksum.is_file()
    assert manifest.is_file()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["version"] == "2.2.9"
    assert data["coverageGate"] == "95%"
    assert data["artifact"] == artifact.name
    recorded = data["sha256"]
    assert release_manifest.verify_checksum(artifact, recorded) is True
    assert checksum.read_text(encoding="utf-8").startswith(recorded)
    assert "evidence" in data
    assert "docs/evidence/headless-mcp-bridge.json" in data["evidence"]


def test_release_script_dry_run_writes_nothing(tmp_path: Path) -> None:
    import subprocess
    import sys

    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "README.md").write_text("ok", encoding="utf-8")
    out = tmp_path / "dist"
    script = (Path(__file__).resolve().parents[1] / "scripts" / "release.py")
    result = subprocess.run(
        [sys.executable, str(script), "--root", str(workspace), "--output-dir", str(out), "--version", "2.2.9", "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "dry-run" in result.stdout
    assert not out.exists() or not any(out.iterdir())


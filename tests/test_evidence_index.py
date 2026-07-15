from __future__ import annotations

from pathlib import Path


def test_evidence_index_lists_headless_mcp_bridge() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/headless-mcp-bridge.json" in index
    assert "scripts/smoke_mcp_headless_bridge.py" in index


def test_evidence_index_lists_a2a_external_peer() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/a2a-external-peer.json" in index
    assert "scripts/smoke_a2a_external_peer.py" in index


def test_evidence_index_lists_eval_reports() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "evals/reports/latest.json" in index
    assert "evals/reports/agent-latest.json" in index
    assert "evals/reports/baseline-compare-latest.json" in index
    assert "evals/reports/security-latest.json" in index


def test_evidence_index_lists_gui_and_third_party_entries() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "Claude Desktop" in index
    assert "Cursor" in index
    assert "Third-party A2A ecosystem" in index


def test_evidence_index_lists_edge_router_stabilization() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/edge-router-v4.0.0-rc.2.json" in index
    assert "scripts/smoke_edge_router.py" in index
    assert "Edge Router stabilization" in index


def test_evidence_index_lists_context_taint() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/context-taint-v4.0.0-rc.2.json" in index
    assert "scripts/smoke_context_taint.py" in index
    assert "Context Taint" in index


def test_evidence_index_lists_semantic_cache_benchmark() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/semantic-cache-onnx-v4.0.0-rc.2.json" in index
    assert "benchmarks/bench_semantic_cache.py" in index
    assert "RUST_CANDIDATE_AUDIT_3_4.md" in index


def test_evidence_index_lists_semantic_cache_binary_embeddings() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "SEMANTIC_CACHE_BINARY_EMBEDDINGS.md" in index
    assert "tests/test_semantic_cache_binary_embeddings.py" in index
    assert "tests/test_semantic_cache_embedding_migration.py" in index
    assert "direct BLOB assembly" in index


def test_evidence_index_lists_gateway_request_parity() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "GATEWAY_REQUEST_PREPARATION_PARITY.md" in index
    assert "fixtures/gateway/request_preparation_cases.json" in index
    assert "scripts/check_gateway_request_parity.py" in index


def test_evidence_index_lists_rag_document_preparation() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "RAG_DOCUMENT_PREPARATION_PARITY.md" in index
    assert "fixtures/rag/document_preparation_cases.json" in index
    assert "scripts/check_rag_document_preparation_parity.py" in index
    assert "docs/evidence/rust-sidecar-performance-v4.0.0-rc.2.json" in index
    assert "scripts/run_rust_sidecar_benchmarks.py" in index
    assert "RUST_SIDECAR_PERFORMANCE.md" in index


def test_evidence_index_lists_rag_vector_binary_transport() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/rag-vector-binary-parity-v4.0.0-rc.2.json" in index
    assert "scripts/check_rag_vector_binary_parity.py" in index
    assert "RAG_VECTOR_BINARY_TRANSPORT.md" in index


def test_evidence_index_lists_workspace_core() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/workspace-v4.0.0-rc.2.json" in index
    assert "scripts/smoke_workspace.py" in index
    assert "Workspace Core" in index


def test_evidence_index_lists_media_layer() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/media-v4.0.0-rc.2.json" in index
    assert "evals/reports/media-v4.0.0-rc.2.json" in index
    assert "scripts/smoke_media.py" in index
    assert "run_media_eval.py" in index
    assert "Media Layer" in index


def test_evidence_index_lists_browser_control() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/browser-v4.0.0-rc.2.json" in index
    assert "evals/reports/browser-v4.0.0-rc.2.json" in index
    assert "scripts/smoke_browser.py" in index
    assert "run_browser_eval.py" in index
    assert "Browser Control" in index


def test_evidence_index_lists_automation_runtime() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/automation-v4.0.0-rc.2.json" in index
    assert "evals/reports/automation-v4.0.0-rc.2.json" in index
    assert "scripts/smoke_automation.py" in index
    assert "run_automation_eval.py" in index
    assert "Automation Runtime" in index


def test_evidence_index_lists_skill_system() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/skills-v4.0.0-rc.2.json" in index
    assert "scripts/smoke_skills.py" in index
    assert "Skill System" in index


def test_evidence_index_lists_skill_workbench_ui() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/skills-ui-v4.0.0-rc.2.json" in index
    assert "scripts/smoke_skills_ui.py" in index
    assert "Skill Workbench UI" in index


def test_evidence_index_lists_skill_builder() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/skill-builder-v4.0.0-rc.2.json" in index
    assert "scripts/smoke_skill_builder.py" in index
    assert "Skill Builder" in index


def test_evidence_index_lists_skill_packs() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/skill-packs-v4.0.0-rc.2.json" in index
    assert "scripts/smoke_skill_packs.py" in index
    assert "Skill Packs" in index


def test_evidence_index_lists_skill_eval_dashboard() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/skill-eval-dashboard-v4.0.0-rc.2.json" in index
    assert "evals/reports/skills-v4.0.0-rc.2.json" in index
    assert "scripts/smoke_skill_eval_dashboard.py" in index
    assert "Skill Eval Dashboard" in index


def test_evidence_index_lists_skill_versioning() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/skill-versioning-v4.0.0-rc.2.json" in index
    assert "scripts/smoke_skill_versioning.py" in index
    assert "Skill Versioning" in index


def test_evidence_index_lists_skill_analytics() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/skill-analytics-v4.0.0-rc.2.json" in index
    assert "scripts/smoke_skill_analytics.py" in index
    assert "Skill Analytics" in index


def test_evidence_index_lists_skill_security() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/skill-security-v4.0.0-rc.2.json" in index
    assert "scripts/smoke_skill_security.py" in index
    assert "Skill Security" in index


def test_evidence_index_lists_skill_catalog() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/skill-catalog-v4.0.0-rc.2.json" in index
    assert "scripts/smoke_skill_catalog.py" in index
    assert "Skill Catalog" in index

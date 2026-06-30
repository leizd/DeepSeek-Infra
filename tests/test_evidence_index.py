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


def test_evidence_index_lists_workspace_core() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/workspace-v2.6.5.json" in index
    assert "scripts/smoke_workspace.py" in index
    assert "Workspace Core" in index


def test_evidence_index_lists_skill_system() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/skills-v2.6.5.json" in index
    assert "scripts/smoke_skills.py" in index
    assert "Skill System" in index


def test_evidence_index_lists_skill_workbench_ui() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/skills-ui-v2.6.5.json" in index
    assert "scripts/smoke_skills_ui.py" in index
    assert "Skill Workbench UI" in index


def test_evidence_index_lists_skill_builder() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/skill-builder-v2.6.5.json" in index
    assert "scripts/smoke_skill_builder.py" in index
    assert "Skill Builder" in index


def test_evidence_index_lists_skill_packs() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/skill-packs-v2.6.5.json" in index
    assert "scripts/smoke_skill_packs.py" in index
    assert "Skill Packs" in index


def test_evidence_index_lists_skill_eval_dashboard() -> None:
    index = Path("docs/EVIDENCE_INDEX.md").read_text(encoding="utf-8")
    assert "docs/evidence/skill-eval-dashboard-v2.6.5.json" in index
    assert "evals/reports/skills-v2.6.5.json" in index
    assert "scripts/smoke_skill_eval_dashboard.py" in index
    assert "Skill Eval Dashboard" in index

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

import deepseek_infra.core.config as config
import deepseek_infra.infra.rag.files as files
import deepseek_infra.infra.agent_runtime.a2a as a2a
import deepseek_infra.infra.agent_runtime.agent_runs as agent_runs
import deepseek_infra.infra.data.memory as memory
import deepseek_infra.infra.rag.local_rag as local_rag
import deepseek_infra.infra.observability.observability as observability
import deepseek_infra.infra.data.projects as projects
import deepseek_infra.infra.data.reminders as reminders
import deepseek_infra.infra.tool_runtime.generated_files as generated_files
import deepseek_infra.infra.tool_runtime.search as search
import deepseek_infra.infra.gateway.budget_manager as budget_manager
import deepseek_infra.infra.gateway.resiliency as resiliency
import deepseek_infra.infra.gateway.scheduler as scheduler
import deepseek_infra.infra.gateway.semantic_cache as semantic_cache
import deepseek_infra.infra.tool_runtime.tools as tools
import deepseek_infra.infra.workspace.artifacts as workspace_artifacts
import deepseek_infra.infra.workspace.backups as workspace_backups
import deepseek_infra.infra.workspace.backup_policies as workspace_backup_policies
import deepseek_infra.infra.workspace.backup_mirror as workspace_backup_mirror
import deepseek_infra.infra.workspace.backup_scheduler as workspace_backup_scheduler
import deepseek_infra.infra.workspace.backup_targets as workspace_backup_targets
import deepseek_infra.infra.workspace.backup_retention as workspace_backup_retention
import deepseek_infra.infra.workspace.backup_component_cache as workspace_backup_component_cache
import deepseek_infra.infra.workspace.exports as workspace_exports
import deepseek_infra.infra.workspace.saved_items as workspace_saved_items
import deepseek_infra.infra.skills.evidence as skill_evidence
import deepseek_infra.infra.skills.registry as skill_registry
import deepseek_infra.infra.media.library as media_library
import deepseek_infra.infra.browser.session as browser_session
import deepseek_infra.infra.automation.registry as automation_registry
import deepseek_infra.infra.automation.history as automation_history
from real_storage_environment import MANAGED_ENV_NAMES, RealStorageEnvironment, ensure_native_backup_helpers


@pytest.fixture(scope="session")
def native_backup_helpers() -> Iterator[None]:
    repository_root = Path(__file__).resolve().parents[1]
    ensure_native_backup_helpers(repository_root)
    yield


@pytest.fixture(scope="session")
def real_storage_environment(
    tmp_path_factory: pytest.TempPathFactory,
    native_backup_helpers: None,
) -> Iterator[RealStorageEnvironment]:
    del native_backup_helpers
    repository_root = Path(__file__).resolve().parents[1]
    environment = RealStorageEnvironment.acquire(repository_root, tmp_path_factory.mktemp("real-minio"))
    previous = {name: os.environ.get(name) for name in MANAGED_ENV_NAMES}
    os.environ.update(environment.values)
    try:
        yield environment
    finally:
        environment.close()
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture
def tmp_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point local state directories at a fresh temporary workspace."""
    file_cache_dir = tmp_path / ".file-cache"
    agent_runs_dir = tmp_path / ".agent-runs"
    memory_dir = tmp_path / ".memory"
    search_cache_dir = tmp_path / ".search-cache"
    generated_dir = tmp_path / ".generated"
    reminders_dir = tmp_path / ".reminders"
    projects_dir = tmp_path / ".projects"
    media_dir = tmp_path / ".media"
    local_rag_dir = tmp_path / ".local-rag"
    traces_dir = tmp_path / ".traces"
    semantic_cache_dir = tmp_path / ".semantic-cache"
    request_queue_dir = tmp_path / ".request-queue"
    budget_dir = tmp_path / ".budget"
    scheduler_dir = tmp_path / ".scheduler"
    browser_audit_dir = tmp_path / ".browser-audit"
    browser_downloads_dir = tmp_path / ".browser-downloads"
    browser_profiles_dir = tmp_path / ".browser-profiles"
    automation_dir = tmp_path / ".automation"
    backups_dir = tmp_path / ".backups"
    restore_dir = tmp_path / ".restore-staging"

    monkeypatch.setattr(config, "FILE_CACHE_DIR", file_cache_dir)
    monkeypatch.setattr(config, "AGENT_RUNS_DIR", agent_runs_dir)
    monkeypatch.setattr(config, "MEMORY_DIR", memory_dir)
    monkeypatch.setattr(config, "MEMORY_FILE", memory_dir / "memories.json")
    monkeypatch.setattr(config, "SEARCH_CACHE_DIR", search_cache_dir)
    monkeypatch.setattr(config, "GENERATED_DIR", generated_dir)
    monkeypatch.setattr(config, "REMINDERS_DIR", reminders_dir)
    monkeypatch.setattr(config, "REMINDERS_FILE", reminders_dir / "reminders.json")
    monkeypatch.setattr(config, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(config, "MEDIA_DIR", media_dir)
    monkeypatch.setattr(config, "LOCAL_RAG_DIR", local_rag_dir)
    monkeypatch.setattr(config, "LOCAL_RAG_DB", local_rag_dir / "rag.sqlite3")
    monkeypatch.setattr(config, "TRACE_DIR", traces_dir)
    monkeypatch.setattr(config, "TRACE_DB", traces_dir / "traces.sqlite3")
    monkeypatch.setattr(config, "SEMANTIC_CACHE_DIR", semantic_cache_dir)
    monkeypatch.setattr(config, "SEMANTIC_CACHE_DB", semantic_cache_dir / "cache.sqlite3")
    monkeypatch.setattr(config, "GATEWAY_REQUEST_QUEUE_DIR", request_queue_dir)
    monkeypatch.setattr(config, "GATEWAY_REQUEST_QUEUE_DB", request_queue_dir / "queue.sqlite3")
    monkeypatch.setattr(config, "BUDGET_DIR", budget_dir)
    monkeypatch.setattr(config, "BUDGET_DB", budget_dir / "budget.sqlite3")
    monkeypatch.setattr(config, "BROWSER_AUDIT_DIR", browser_audit_dir)
    monkeypatch.setattr(config, "BROWSER_AUDIT_LOG", browser_audit_dir / "audit.jsonl")
    monkeypatch.setattr(config, "BROWSER_DOWNLOADS_DIR", browser_downloads_dir)
    monkeypatch.setattr(config, "BROWSER_PROFILES_DIR", browser_profiles_dir)
    monkeypatch.setattr(config, "AUTOMATION_DIR", automation_dir)

    monkeypatch.setattr(files, "FILE_CACHE_DIR", file_cache_dir)
    monkeypatch.setattr(agent_runs, "AGENT_RUNS_DIR", agent_runs_dir)
    monkeypatch.setattr(memory, "MEMORY_DIR", memory_dir)
    monkeypatch.setattr(memory, "MEMORY_FILE", memory_dir / "memories.json")
    monkeypatch.setattr(search, "SEARCH_CACHE_DIR", search_cache_dir)
    monkeypatch.setattr(generated_files, "GENERATED_DIR", generated_dir)
    monkeypatch.setattr(reminders, "REMINDERS_DIR", reminders_dir)
    monkeypatch.setattr(reminders, "REMINDERS_FILE", reminders_dir / "reminders.json")
    monkeypatch.setattr(projects, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(media_library, "MEDIA_DIR", media_dir)
    monkeypatch.setattr(files, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(local_rag, "FILE_CACHE_DIR", file_cache_dir)
    monkeypatch.setattr(local_rag, "MEMORY_FILE", memory_dir / "memories.json")
    monkeypatch.setattr(local_rag, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(local_rag, "LOCAL_RAG_DIR", local_rag_dir)
    monkeypatch.setattr(local_rag, "LOCAL_RAG_DB", local_rag_dir / "rag.sqlite3")
    monkeypatch.setattr(observability, "TRACE_DIR", traces_dir)
    monkeypatch.setattr(observability, "TRACE_DB", traces_dir / "traces.sqlite3")
    monkeypatch.setattr(semantic_cache, "SEMANTIC_CACHE_DIR", semantic_cache_dir)
    monkeypatch.setattr(semantic_cache, "SEMANTIC_CACHE_DB", semantic_cache_dir / "cache.sqlite3")
    monkeypatch.setattr(resiliency, "GATEWAY_REQUEST_QUEUE_DIR", request_queue_dir)
    monkeypatch.setattr(resiliency, "GATEWAY_REQUEST_QUEUE_DB", request_queue_dir / "queue.sqlite3")
    monkeypatch.setattr(budget_manager, "BUDGET_DIR", budget_dir)
    monkeypatch.setattr(budget_manager, "BUDGET_DB", budget_dir / "budget.sqlite3")
    monkeypatch.setattr(config, "SCHEDULER_DIR", scheduler_dir)
    monkeypatch.setattr(config, "SCHEDULER_DB", scheduler_dir / "scheduler.sqlite3")
    monkeypatch.setattr(scheduler, "SCHEDULER_DIR", scheduler_dir)
    monkeypatch.setattr(scheduler, "SCHEDULER_DB", scheduler_dir / "scheduler.sqlite3")
    a2a_tasks_dir = tmp_path / ".a2a"
    monkeypatch.setattr(config, "A2A_TASKS_DIR", a2a_tasks_dir)
    monkeypatch.setattr(a2a, "A2A_TASKS_DIR", a2a_tasks_dir)
    monkeypatch.setattr(tools, "FILE_CACHE_DIR", file_cache_dir)
    monkeypatch.setattr(tools, "SEARCH_CACHE_DIR", search_cache_dir)
    monkeypatch.setattr(tools, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(workspace_artifacts.legacy_projects, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(workspace_saved_items.legacy_projects, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(workspace_exports.legacy_projects, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(workspace_backups, "BACKUP_DIR", backups_dir)
    monkeypatch.setattr(workspace_backups, "RESTORE_DIR", restore_dir)
    monkeypatch.setattr(workspace_backup_policies, "BACKUP_POLICY_DIR", tmp_path / ".backup-policies")
    monkeypatch.setattr(workspace_backup_mirror, "BACKUP_MIRROR_DIR", tmp_path / ".backup-mirror")
    monkeypatch.setattr(workspace_backup_scheduler, "BACKUP_SCHEDULER_DIR", tmp_path / ".backup-scheduler")
    monkeypatch.setattr(workspace_backup_targets, "BACKUP_TARGET_DIR", tmp_path / ".backup-targets")
    monkeypatch.setattr(workspace_backup_retention, "BACKUP_RETENTION_DIR", tmp_path / ".backup-retention")
    monkeypatch.setattr(workspace_backup_component_cache, "CACHE_DIR", tmp_path / ".backup-component-cache")

    from deepseek_infra.infra.workspace import backup_dr_ledger as workspace_backup_dr_ledger
    from deepseek_infra.infra.workspace import backup_spool as workspace_backup_spool
    from deepseek_infra.infra.workspace import backup_run_plan as workspace_backup_run_plan
    from deepseek_infra.infra.workspace import backup_incremental as workspace_backup_incremental
    from deepseek_infra.infra.workspace import backup_recovery_keeper as workspace_backup_recovery_keeper
    from deepseek_infra.infra.workspace import backup_control as workspace_backup_control

    monkeypatch.setattr(workspace_backup_dr_ledger, "BACKUP_DR_DIR", tmp_path / ".backup-dr")
    monkeypatch.setattr(workspace_backup_dr_ledger, "EVIDENCE_DB", tmp_path / ".backup-dr" / "evidence.sqlite3")
    monkeypatch.setattr(workspace_backup_spool, "SPOOL_DIR", tmp_path / ".backup-spool")
    monkeypatch.setattr(workspace_backup_run_plan, "RUN_PLAN_DIR", tmp_path / ".backup-run-plans")
    monkeypatch.setattr(workspace_backup_incremental, "INDEX_DIR", tmp_path / ".backup-index")
    monkeypatch.setattr(workspace_backup_incremental, "INDEX_DB", tmp_path / ".backup-index" / "index.db")
    monkeypatch.setattr(workspace_backup_recovery_keeper, "STAGING_ROOT", restore_dir)
    monkeypatch.setattr(workspace_backup_control, "CONTROL_DIR", tmp_path / ".backup-control")
    monkeypatch.setattr(workspace_backup_control, "CONTROL_DB", tmp_path / ".backup-control" / "control.sqlite3")

    from deepseek_infra.infra.workspace import backup_authority_provider as workspace_backup_authority_provider
    from deepseek_infra.infra.workspace import backup_control_authority as workspace_backup_control_authority
    from deepseek_infra.infra.workspace import authority_retention as workspace_authority_retention

    monkeypatch.setattr(workspace_authority_retention, "AUTHORITY_RETENTION_DIR", tmp_path / ".backup-authority-retention")

    # Process-local authority replica handles must not leak across tests.
    workspace_backup_control_authority.configure_authority_anchor_roots(None)
    workspace_backup_control_authority.configure_authority_anchor_stores(None)
    workspace_backup_authority_provider.reset_authority_replica_provider()
    # Unit tests default to explicit local-only; production default remains replicated.
    monkeypatch.setenv(workspace_backup_authority_provider.ENV_AUTHORITY_MODE, "local-only")
    try:
        from deepseek_infra.infra.workspace import backup_control_recovery as _bcr

        _bcr.clear_formal_truth_attestations()
    except Exception:
        pass

    from deepseek_infra.infra.workspace import backup_replication as workspace_backup_replication
    from deepseek_infra.infra.workspace import backup_transfer_budget as workspace_backup_transfer_budget
    from deepseek_infra.infra.workspace import backup_write_continuity as workspace_backup_write_continuity
    from deepseek_infra.infra.workspace import backup_retirement as workspace_backup_retirement
    from deepseek_infra.infra.workspace import backup_drain as workspace_backup_drain

    repl_dir = tmp_path / ".backup-replication"
    retire_dir = tmp_path / ".backup-retirements"
    drain_dir = tmp_path / ".backup-drains"
    monkeypatch.setattr(workspace_backup_replication, "REPLICATION_DIR", repl_dir)
    monkeypatch.setattr(workspace_backup_replication, "HOLDS_DIR", repl_dir / "holds")
    monkeypatch.setattr(workspace_backup_replication, "REPAIRS_DIR", repl_dir / "repairs")
    monkeypatch.setattr(workspace_backup_replication, "REBALANCE_DIR", tmp_path / ".backup-rebalance")
    monkeypatch.setattr(workspace_backup_replication, "CURSORS_PATH", repl_dir / "cursors.json")
    monkeypatch.setattr(workspace_backup_write_continuity, "CONTINUITY_DIR", tmp_path / ".backup-continuity")
    monkeypatch.setattr(workspace_backup_retirement, "RETIREMENT_DIR", retire_dir)
    monkeypatch.setattr(workspace_backup_retirement, "RETIREMENT_DB", retire_dir / "retirements.sqlite3")
    monkeypatch.setattr(workspace_backup_retirement, "RETIREMENTS_DIR", retire_dir)
    monkeypatch.setattr(workspace_backup_retirement, "RETIREMENTS_DB", retire_dir / "retirements.sqlite3")
    monkeypatch.setattr(workspace_backup_drain, "DRAIN_DIR", drain_dir)
    monkeypatch.setattr(workspace_backup_drain, "DRAIN_DB", drain_dir / "drains.sqlite3")
    monkeypatch.setattr(workspace_backup_drain, "DRAINS_DIR", drain_dir)
    monkeypatch.setattr(workspace_backup_drain, "DRAINS_DB", drain_dir / "drains.sqlite3")
    workspace_backup_transfer_budget.reset_global_transfer_budget_manager()

    skills_dir = tmp_path / ".skills"
    monkeypatch.setattr(config, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(skill_registry, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(skill_evidence, "GENERATED_DIR", generated_dir)
    monkeypatch.setattr(automation_registry, "AUTOMATION_DIR", automation_dir)
    monkeypatch.setattr(automation_history, "AUTOMATION_DIR", automation_dir)

    from deepseek_infra.infra.workspace import (
        autonomous_action_policy as workspace_autonomous_action_policy,
        federation_peer_trust as workspace_federation_peer_trust,
        resilience_action_journal as workspace_resilience_action_journal,
        resilience_capacity_history as workspace_resilience_capacity_history,
        resilience_cost_model as workspace_resilience_cost_model,
        resilience_forecast_backtest as workspace_resilience_forecast_backtest,
        resilience_forecast_registry as workspace_resilience_forecast_registry,
        resilience_placement_optimizer as workspace_resilience_placement_optimizer,
        resilience_risk_observations as workspace_resilience_risk_observations,
        resilience_scheduler_service as workspace_resilience_scheduler_service,
        resilience_slo_ledger as workspace_resilience_slo_ledger,
        resilience_wave_executor as workspace_resilience_wave_executor,
    )

    resilience_journal_dir = tmp_path / ".resilience-journal"
    resilience_policy_dir = tmp_path / ".resilience-policy"
    monkeypatch.setattr(workspace_resilience_action_journal, "JOURNAL_DIR", resilience_journal_dir)
    monkeypatch.setattr(workspace_resilience_action_journal, "JOURNAL_DB", resilience_journal_dir / "journal.sqlite3")
    monkeypatch.setattr(workspace_autonomous_action_policy, "POLICY_DIR", resilience_policy_dir)
    monkeypatch.setattr(workspace_autonomous_action_policy, "POLICY_FILE", resilience_policy_dir / "autonomous_policy.json")
    resilience_risk_dir = tmp_path / ".resilience-risk"
    monkeypatch.setattr(workspace_resilience_risk_observations, "RISK_LEDGER_DIR", resilience_risk_dir)
    monkeypatch.setattr(workspace_resilience_risk_observations, "RISK_LEDGER_DB", resilience_risk_dir / "risk.sqlite3")
    resilience_scheduler_dir = tmp_path / ".resilience-scheduler"
    monkeypatch.setattr(workspace_resilience_scheduler_service, "SCHEDULER_SERVICE_DIR", resilience_scheduler_dir)
    monkeypatch.setattr(workspace_resilience_scheduler_service, "SCHEDULER_SERVICE_DB", resilience_scheduler_dir / "service.sqlite3")
    resilience_slo_dir = tmp_path / ".resilience-slo"
    monkeypatch.setattr(workspace_resilience_slo_ledger, "SLO_LEDGER_DIR", resilience_slo_dir)
    monkeypatch.setattr(workspace_resilience_slo_ledger, "SLO_LEDGER_DB", resilience_slo_dir / "slo.sqlite3")
    resilience_waves_dir = tmp_path / ".resilience-waves"
    monkeypatch.setattr(workspace_resilience_wave_executor, "WAVE_EXECUTOR_DIR", resilience_waves_dir)
    monkeypatch.setattr(workspace_resilience_wave_executor, "WAVE_EXECUTOR_DB", resilience_waves_dir / "waves.sqlite3")
    resilience_capacity_dir = tmp_path / ".resilience-capacity"
    monkeypatch.setattr(workspace_resilience_capacity_history, "CAPACITY_HISTORY_DIR", resilience_capacity_dir)
    monkeypatch.setattr(workspace_resilience_capacity_history, "CAPACITY_HISTORY_DB", resilience_capacity_dir / "capacity.sqlite3")
    monkeypatch.setattr(workspace_resilience_forecast_backtest, "CAPACITY_HISTORY_DIR", resilience_capacity_dir)
    monkeypatch.setattr(workspace_resilience_forecast_backtest, "CAPACITY_HISTORY_DB", resilience_capacity_dir / "capacity.sqlite3")
    monkeypatch.setattr(workspace_resilience_forecast_registry, "CAPACITY_HISTORY_DIR", resilience_capacity_dir)
    monkeypatch.setattr(workspace_resilience_forecast_registry, "CAPACITY_HISTORY_DB", resilience_capacity_dir / "capacity.sqlite3")
    resilience_cost_dir = tmp_path / ".resilience-cost"
    monkeypatch.setattr(workspace_resilience_cost_model, "COST_MODEL_DIR", resilience_cost_dir)
    monkeypatch.setattr(workspace_resilience_cost_model, "COST_MODEL_DB", resilience_cost_dir / "cost.sqlite3")
    resilience_optimizer_dir = tmp_path / ".resilience-optimizer"
    monkeypatch.setattr(workspace_resilience_placement_optimizer, "OPTIMIZER_DIR", resilience_optimizer_dir)
    monkeypatch.setattr(workspace_resilience_placement_optimizer, "OPTIMIZER_DB", resilience_optimizer_dir / "optimizer.sqlite3")
    federation_dir = tmp_path / ".federation"
    monkeypatch.setattr(workspace_federation_peer_trust, "FEDERATION_DIR", federation_dir)
    monkeypatch.setattr(workspace_federation_peer_trust, "PEER_TRUST_DB", federation_dir / "peer-trust.sqlite3")

    browser_session.reset_sessions_for_tests()
    files._load_cached_file_cached.cache_clear()
    yield tmp_path
    workspace_backup_control_authority.configure_authority_anchor_roots(None)
    workspace_backup_control_authority.configure_authority_anchor_stores(None)
    workspace_backup_authority_provider.reset_authority_replica_provider()
    workspace_backup_transfer_budget.reset_global_transfer_budget_manager()
    browser_session.reset_sessions_for_tests()
    files._load_cached_file_cached.cache_clear()


@pytest.fixture
def fake_deepseek() -> Callable[[str, str, dict[str, int] | None], dict[str, object]]:
    def _make(content: str = "hello", reasoning: str = "", usage: dict[str, int] | None = None) -> dict[str, object]:
        return {
            "id": "chatcmpl-test",
            "model": "deepseek-v4-pro",
            "choices": [{"message": {"content": content, "reasoning_content": reasoning}}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    return _make


@pytest.fixture
def mock_urlopen() -> object:
    with patch("urllib.request.urlopen") as mocked, patch(
        "deepseek_infra.infra.rust_core.transport.urlopen", mocked
    ):
        yield mocked


def deepseek_response_bytes(content: str = "hello", usage: dict[str, int] | None = None) -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-test",
            "model": "deepseek-v4-pro",
            "choices": [{"message": {"content": content}}],
            "usage": usage or {},
        }
    ).encode("utf-8")

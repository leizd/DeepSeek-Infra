"""Authoritative optimizer inputs and write-deny simulation (4.7.6 Gates H-I)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from deepseek_infra.core.config import settings
from deepseek_infra.infra.workspace import (
    backup_control,
    backup_control_authority,
    backup_control_recovery,
    backup_dr_ledger,
    backup_policies,
    backup_target_store,
    backup_targets,
    resilience_cost_model,
    resilience_forecast_registry,
    resilience_fresh_state,
    resilience_optimizer_inputs,
    resilience_action_journal,
    resilience_simulation_capability,
    resilience_state_digests,
    resilience_whatif,
)
from deepseek_infra.web.server import create_server


def _authoritative_sources(monkeypatch: Any) -> None:
    policy = {
        "policyId": "policy-a",
        "targetId": "target-a",
        "replication": {"enabled": True, "minCommittedCopies": 3, "minFailureDomains": 2},
        "placement": {"minFreeBytes": 100},
    }
    monkeypatch.setattr(backup_control, "get_policy", lambda _policy_id: policy)
    monkeypatch.setattr(
        backup_dr_ledger,
        "latest_recovery_point",
        lambda **_kwargs: {"policyId": "policy-a", "backupId": "backup-a"},
    )
    monkeypatch.setattr(
        backup_dr_ledger,
        "list_logical_recovery_copies",
        lambda **_kwargs: [
            {"targetId": "target-a", "recoverable": True, "state": "healthy"},
            {"targetId": "target-b", "recoverable": True, "state": "healthy"},
            {"targetId": "target-c", "recoverable": True, "state": "healthy"},
        ],
    )
    monkeypatch.setattr(
        backup_control,
        "list_targets",
        lambda: [
            {"targetId": "target-a", "failureDomain": "fd-a"},
            {"targetId": "target-b", "failureDomain": "fd-b"},
            {"targetId": "target-c", "failureDomain": "fd-c"},
        ],
    )
    forecast = {
        "forecastId": "forecast-a",
        "targetId": "target-a",
        "targetIncarnation": "inc-a",
        "capacityRevision": "revision-a",
        "horizonDays": 90,
        "p50FreeBytes": 900,
        "p90FreeBytes": 700,
        "forecastDigest": "f" * 64,
        "capacityObservationSetDigest": "o" * 64,
        "status": "ACTIVE",
    }
    monkeypatch.setattr(
        resilience_forecast_registry,
        "get_current_forecast",
        lambda _target_id, *, horizon_days: forecast,
    )
    monkeypatch.setattr(
        resilience_cost_model,
        "get_price_catalog",
        lambda: {
            "priceCatalogVersion": 7,
            "priceCatalogDigest": "p" * 64,
            "targets": {"target-a": {"storagePerGiBMonth": 0.02, "egressPerGiB": 0.01}},
        },
    )
    monkeypatch.setattr(
        resilience_fresh_state,
        "build_fresh_state_bundle",
        lambda _schedule, _actions, *, now=None: {
            "authorityHeadDigest": "a" * 64,
            "riskDigest": "r" * 64,
            "freshStateBundleDigest": "b" * 64,
            "riskSnapshot": {"riskDigest": "r" * 64, "overallRisk": "warning"},
            "runningEffects": [{"actionId": "running-real"}],
            "maintenanceDecisions": [{"actionId": "candidate", "allowed": True}],
            "budgets": {"admitted": True, "transferBudget": {"availableBytes": 1_000}},
            "blastSimulation": {"passed": True},
            "capacitySnapshot": {"targets": [{"targetId": "target-a"}]},
            "observedAt": (now or datetime.now(tz=timezone.utc)).isoformat().replace("+00:00", "Z"),
        },
    )


def _candidate(**overrides: Any) -> dict[str, Any]:
    return {
        "policyId": "policy-a",
        "targetId": "target-a",
        "operation": "KEEP",
        "committedCopiesDelta": 0,
        "failureDomainsDelta": 0,
        "storedBytes": 1024,
        **overrides,
    }


def _inputs() -> dict[str, Any]:
    return {
        "candidate": {
            "targetId": "target-a",
            "committedCopies": 3,
            "failureDomains": 3,
            "storedBytes": 1024,
            "forecastFreeBytes": 700,
        },
        "baseline": {
            "minCommittedCopies": 3,
            "minFailureDomains": 2,
            "committedCopies": 3,
            "failureDomains": 3,
            "forecastSafetyHeadroomBytes": 100,
        },
        "observedSnapshot": {"riskDigest": "r" * 64, "overallRisk": "warning"},
        "forecast": {"forecastDigest": "f" * 64, "forecastStatus": "OK", "p50FreeBytes": 900, "p90FreeBytes": 700},
        "forecastRecord": {"forecastId": "forecast-a", "capacityObservationSetDigest": "o" * 64},
        "priceCatalog": {
            "priceCatalogDigest": "p" * 64,
            "targets": {"target-a": {"storagePerGiBMonth": 0.02, "egressPerGiB": 0.01}},
        },
        "authorityHeadDigest": "a" * 64,
        "freshStateBundleDigest": "b" * 64,
        "runningEffects": [{"actionId": "running-real"}],
        "maintenanceWindows": [{"actionId": "candidate", "allowed": True}],
        "targetIds": ["target-a"],
        "optimizerInputDigest": "i" * 64,
    }


def test_optimizer_builds_present_truth_from_authoritative_sources(tmp_settings: Path, monkeypatch: Any) -> None:
    _authoritative_sources(monkeypatch)

    inputs = resilience_optimizer_inputs.build_authoritative_optimizer_inputs(
        _candidate(committedCopiesDelta=-1, failureDomainsDelta=-1),
        now=datetime(2026, 8, 30, tzinfo=timezone.utc),
    )

    assert inputs["baseline"]["committedCopies"] == 3
    assert inputs["baseline"]["failureDomains"] == 3
    assert inputs["baseline"]["minCommittedCopies"] == 3
    assert inputs["candidate"]["committedCopies"] == 2
    assert inputs["candidate"]["failureDomains"] == 2
    assert inputs["candidate"]["forecastFreeBytes"] == 700
    assert inputs["forecastRecord"]["forecastId"] == "forecast-a"
    assert inputs["priceCatalog"]["priceCatalogVersion"] == 7
    assert inputs["runningEffects"] == [{"actionId": "running-real"}]
    assert inputs["authorityHeadDigest"] == "a" * 64
    assert inputs["capacitySnapshot"] == {"targets": [{"targetId": "target-a"}]}
    assert [item["targetId"] for item in inputs["targetMetadata"]] == ["target-a", "target-b", "target-c"]
    assert inputs["targetIds"] == ["target-a", "target-b", "target-c"]
    assert inputs["optimizerInputDigest"]


def test_caller_cannot_override_authoritative_optimizer_fields(tmp_settings: Path, monkeypatch: Any) -> None:
    _authoritative_sources(monkeypatch)
    forbidden = (
        "committedCopies",
        "failureDomains",
        "forecastFreeBytes",
        "runningEffects",
        "authorityHeadDigest",
        "priceCatalog",
    )
    for field in forbidden:
        try:
            resilience_optimizer_inputs.build_authoritative_optimizer_inputs(_candidate(**{field: 0}))
        except ValueError as exc:
            assert "caller-controlled present truth" in str(exc)
        else:  # pragma: no cover - assertion branch
            raise AssertionError(f"{field} override was accepted")


@pytest.mark.parametrize(
    ("source", "expected_reason"),
    (
        ("policy", "POLICY_TRUTH_UNAVAILABLE"),
        ("forecast", "FORECAST_REGISTRY_UNAVAILABLE"),
        ("catalog", "PRICE_CATALOG_UNAVAILABLE"),
        ("fresh-state", "FRESH_STATE_BUNDLE_UNAVAILABLE"),
    ),
)
def test_missing_authoritative_optimizer_source_fails_closed(
    tmp_settings: Path,
    monkeypatch: Any,
    source: str,
    expected_reason: str,
) -> None:
    _authoritative_sources(monkeypatch)
    if source == "policy":
        monkeypatch.setattr(backup_control, "get_policy", lambda _policy_id: None)
    elif source == "forecast":
        monkeypatch.setattr(resilience_forecast_registry, "get_current_forecast", lambda *_args, **_kwargs: None)
    elif source == "catalog":
        monkeypatch.setattr(resilience_cost_model, "get_price_catalog", lambda: {})
    else:
        monkeypatch.setattr(resilience_fresh_state, "build_fresh_state_bundle", lambda *_args, **_kwargs: {})

    with pytest.raises(resilience_optimizer_inputs.AuthoritativeInputUnavailable, match=expected_reason):
        resilience_optimizer_inputs.build_authoritative_optimizer_inputs(_candidate())


def test_whatif_api_accepts_candidate_only(tmp_settings: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        resilience_whatif,
        "simulate_candidate",
        lambda candidate: {"status": "OK", "candidatePolicyId": candidate["policyId"]},
    )
    server, _ = create_server(0, host="127.0.0.1")
    client = TestClient(server.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {settings.auth.token}", "X-DeepSeek-Client": "test"}

    rejected = client.post(
        "/api/workspace/resilience/whatif",
        headers=headers,
        json={"candidate": _candidate(), "baseline": {"committedCopies": 99}},
    )

    assert rejected.status_code == 400
    assert "candidate" in rejected.text.lower()
    accepted = client.post("/api/workspace/resilience/whatif", headers=headers, json={"candidate": _candidate()})
    assert accepted.status_code == 200
    assert accepted.json()["candidatePolicyId"] == "policy-a"


def test_whatif_api_fails_closed_when_present_truth_is_unavailable(tmp_settings: Path, monkeypatch: Any) -> None:
    def unavailable(_candidate_value: dict[str, Any]) -> dict[str, Any]:
        raise resilience_optimizer_inputs.AuthoritativeInputUnavailable("FORECAST_REGISTRY_UNAVAILABLE")

    monkeypatch.setattr(resilience_whatif, "simulate_candidate", unavailable)
    server, _ = create_server(0, host="127.0.0.1")
    client = TestClient(server.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {settings.auth.token}", "X-DeepSeek-Client": "test"}

    response = client.post("/api/workspace/resilience/whatif", headers=headers, json={"candidate": _candidate()})

    assert response.status_code == 503
    assert "FORECAST_REGISTRY_UNAVAILABLE" in response.text


def test_whatif_uses_read_only_capability_and_pre_post_digests(tmp_settings: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(resilience_optimizer_inputs, "build_authoritative_optimizer_inputs", lambda *_args, **_kwargs: _inputs())
    states = [
        {"stateDigest": "same", "digests": {"storage": "s", "authority": "a", "actionJournal": "j", "policy": "p", "target": "t"}},
        {"stateDigest": "same", "digests": {"storage": "s", "authority": "a", "actionJournal": "j", "policy": "p", "target": "t"}},
    ]
    monkeypatch.setattr(resilience_state_digests, "capture_mutation_state", lambda *_args, **_kwargs: states.pop(0))

    simulation = resilience_whatif.simulate_candidate(_candidate())

    assert simulation["status"] == "OK"
    assert simulation["simulation"]["attemptedMutationCount"] == 0
    assert simulation["simulation"]["attemptedWrites"] == []
    assert simulation["simulation"]["blockedWrites"] == []
    assert simulation["simulation"]["preStateDigest"] == "same"
    assert simulation["simulation"]["postStateDigest"] == "same"
    assert simulation["simulation"]["stateUnchanged"] is True


def test_whatif_write_attempt_fails_closed_before_real_storage_mutation(tmp_settings: Path, monkeypatch: Any) -> None:
    states = [
        {"stateDigest": "same", "digests": {}},
        {"stateDigest": "same", "digests": {}},
    ]
    monkeypatch.setattr(resilience_state_digests, "capture_mutation_state", lambda *_args, **_kwargs: states.pop(0))
    store = backup_target_store.MemoryTargetStore()

    def attempts_write(_inputs_value: dict[str, Any]) -> dict[str, Any]:
        store.put_if_absent("objects/blocked.age", b"blocked")
        return {}

    simulation = resilience_whatif.simulate_authoritative_inputs(_inputs(), evaluator=attempts_write)

    assert simulation["status"] == "SIMULATION_VIOLATION"
    assert simulation["simulation"]["attemptedMutationCount"] == 1
    assert simulation["simulation"]["blockedWrites"][0]["domain"] == "storage"
    assert store.stat("objects/blocked.age") is None


def test_whatif_capability_covers_authoritative_input_builder(tmp_settings: Path, monkeypatch: Any) -> None:
    states = [
        {"stateDigest": "same", "digests": {}},
        {"stateDigest": "same", "digests": {}},
    ]
    monkeypatch.setattr(resilience_state_digests, "capture_mutation_state", lambda *_args, **_kwargs: states.pop(0))
    store = backup_target_store.MemoryTargetStore()

    def mutating_builder(_candidate_value: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        del now
        store.put_if_absent("objects/builder-write.age", b"blocked")
        return _inputs()

    monkeypatch.setattr(resilience_optimizer_inputs, "build_authoritative_optimizer_inputs", mutating_builder)

    simulation = resilience_whatif.simulate_candidate(_candidate())

    assert simulation["status"] == "SIMULATION_VIOLATION"
    assert simulation["simulation"]["attemptedMutationCount"] == 1
    assert simulation["simulation"]["blockedWrites"][0]["domain"] == "storage"
    assert store.stat("objects/builder-write.age") is None


def test_whatif_detects_uninstrumented_state_change(tmp_settings: Path, monkeypatch: Any) -> None:
    states = [
        {"stateDigest": "before", "digests": {"policy": "p1"}},
        {"stateDigest": "after", "digests": {"policy": "p2"}},
    ]
    monkeypatch.setattr(resilience_state_digests, "capture_mutation_state", lambda *_args, **_kwargs: states.pop(0))

    simulation = resilience_whatif.simulate_authoritative_inputs(_inputs())

    assert simulation["status"] == "SIMULATION_VIOLATION"
    assert simulation["simulation"]["stateUnchanged"] is False
    assert simulation["simulation"]["changedDomains"] == ["policy"]


def test_every_production_mutation_domain_is_guarded(tmp_settings: Path) -> None:
    capability = resilience_simulation_capability.SimulationCapability(_inputs())
    attempts: list[tuple[str, Callable[[], Any]]] = [
        ("storage", lambda: backup_target_store.MemoryTargetStore().put_if_absent("blocked", b"x")),
        ("authority", lambda: backup_control_authority.prepare_authority_mutation_in_tx(None, kind="blocked")),
        ("action-journal", lambda: resilience_action_journal.record_action_intent({"actionId": "blocked", "type": "NOOP"})),
        ("policy", lambda: backup_policies.create_policy({"policyId": "blocked"})),
        ("target", lambda: backup_targets.init_target("Z:\\blocked-simulation-target")),
    ]

    with capability.activate():
        for _domain, attempt in attempts:
            try:
                attempt()
            except resilience_simulation_capability.SimulationViolation:
                pass
            else:  # pragma: no cover - assertion branch
                raise AssertionError("simulation mutation was not blocked")

    assert [item["domain"] for item in capability.audit()["blockedWrites"]] == [item[0] for item in attempts]


def test_storage_inventory_digest_comes_from_target_listing(tmp_settings: Path, monkeypatch: Any) -> None:
    store = backup_target_store.MemoryTargetStore()
    store.put_if_absent("objects/a.age", b"a")
    monkeypatch.setattr(backup_targets, "open_target_store", lambda _target_id, *, write_intent: store)
    monkeypatch.setattr(backup_control, "list_targets", lambda: [{"targetId": "target-a"}])
    monkeypatch.setattr(backup_control, "list_policies", lambda: [])
    monkeypatch.setattr(backup_control_recovery, "authority_health_snapshot", lambda: {"canonicalDigest": "a" * 64})
    monkeypatch.setattr(resilience_action_journal, "list_actions", lambda **_kwargs: [])

    before = resilience_state_digests.capture_mutation_state(["target-a"])
    store.put_if_absent("objects/b.age", b"b")
    after = resilience_state_digests.capture_mutation_state(["target-a"])

    assert before["storageInventory"][0]["objectCount"] == 1
    assert after["storageInventory"][0]["objectCount"] == 2
    assert before["digests"]["storage"] != after["digests"]["storage"]

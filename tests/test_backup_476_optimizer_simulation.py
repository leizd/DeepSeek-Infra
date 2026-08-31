"""Authoritative optimizer inputs and write-deny simulation (release Gates H-I)."""

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


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ({}, "candidate is required"),
        ({"targetId": "target-a"}, "policyId is required"),
        ({"policyId": "policy-a"}, "targetId or candidate.destTargetId is required"),
        (_candidate(operation="DELETE_REPLICA"), "candidate.operation"),
        (_candidate(forecastHorizonDays=7), "forecastHorizonDays"),
        (_candidate(storedBytes=True), "storedBytes must be an integer"),
        (_candidate(storedBytes=-1), "storedBytes must be non-negative"),
    ],
)
def test_optimizer_candidate_validation_rejects_unsafe_or_malformed_hypotheses(
    candidate: dict[str, Any],
    expected: str,
) -> None:
    with pytest.raises(ValueError, match=expected):
        resilience_optimizer_inputs._validate_candidate(candidate)  # noqa: SLF001


def test_optimizer_candidate_action_maps_only_supported_hypotheses() -> None:
    add = resilience_optimizer_inputs._candidate_action(_candidate(operation="ADD_REPLICA"))  # noqa: SLF001
    rebalance = resilience_optimizer_inputs._candidate_action(_candidate(operation="REBALANCE"))  # noqa: SLF001

    assert add["type"] == "CREATE_REPAIR_JOB"
    assert rebalance["type"] == "CREATE_REBALANCE_JOB"


def test_optimizer_baseline_fails_closed_on_ledger_and_topology_gaps(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = {"policyId": "policy-a", "replication": {"minCommittedCopies": 1, "minFailureDomains": 1}}

    def ledger_unavailable(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("ledger offline")

    monkeypatch.setattr(backup_dr_ledger, "latest_recovery_point", ledger_unavailable)
    with pytest.raises(resilience_optimizer_inputs.AuthoritativeInputUnavailable, match="BASELINE_TRUTH_UNAVAILABLE"):
        resilience_optimizer_inputs._read_baseline(policy)  # noqa: SLF001

    monkeypatch.setattr(backup_dr_ledger, "latest_recovery_point", lambda **_kwargs: {"backupId": "backup-a"})
    monkeypatch.setattr(
        backup_dr_ledger,
        "list_logical_recovery_copies",
        lambda **_kwargs: [{"targetId": "target-missing", "recoverable": True, "state": "healthy"}],
    )
    monkeypatch.setattr(backup_control, "list_targets", lambda: [])
    with pytest.raises(resilience_optimizer_inputs.AuthoritativeInputUnavailable, match="BASELINE_TARGET_UNAVAILABLE"):
        resilience_optimizer_inputs._read_baseline(policy)  # noqa: SLF001

    monkeypatch.setattr(backup_control, "list_targets", lambda: [{"targetId": "target-missing"}])
    with pytest.raises(resilience_optimizer_inputs.AuthoritativeInputUnavailable, match="BASELINE_FAILURE_DOMAIN_UNAVAILABLE"):
        resilience_optimizer_inputs._read_baseline(policy)  # noqa: SLF001


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("policy-exception", "POLICY_TRUTH_UNAVAILABLE"),
        ("forecast-invalid", "FORECAST_REGISTRY_UNAVAILABLE"),
        ("target-price", "TARGET_PRICE_UNAVAILABLE"),
        ("fresh-exception", "RISK_SNAPSHOT_UNAVAILABLE"),
        ("target-exception", "TARGET_TRUTH_UNAVAILABLE"),
    ],
)
def test_optimizer_propagates_authoritative_source_errors(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    expected: str,
) -> None:
    _authoritative_sources(monkeypatch)
    if source == "policy-exception":
        monkeypatch.setattr(backup_control, "get_policy", lambda _policy_id: (_ for _ in ()).throw(RuntimeError("offline")))
    elif source == "forecast-invalid":
        monkeypatch.setattr(
            resilience_forecast_registry,
            "get_current_forecast",
            lambda *_args, **_kwargs: {
                "status": "ACTIVE",
                "forecastDigest": "f" * 64,
                "forecast": {"forecastStatus": "INSUFFICIENT_DATA", "forecastDigest": "f" * 64},
            },
        )
    elif source == "target-price":
        monkeypatch.setattr(
            resilience_cost_model,
            "get_price_catalog",
            lambda: {"priceCatalogDigest": "p" * 64, "targets": {}},
        )
    elif source == "fresh-exception":
        def fresh_unavailable(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise resilience_fresh_state.FreshStateUnavailable("RISK_SNAPSHOT_UNAVAILABLE", source="risk")

        monkeypatch.setattr(resilience_fresh_state, "build_fresh_state_bundle", fresh_unavailable)
    else:
        authoritative_targets = backup_control.list_targets()
        calls = 0

        def target_truth() -> list[dict[str, Any]]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return authoritative_targets
            raise RuntimeError("target truth offline")

        monkeypatch.setattr(backup_control, "list_targets", target_truth)

    with pytest.raises(resilience_optimizer_inputs.AuthoritativeInputUnavailable, match=expected):
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


def test_whatif_can_return_exact_authoritative_inputs_used_for_evaluation(tmp_settings: Path, monkeypatch: Any) -> None:
    authoritative = _inputs()
    monkeypatch.setattr(resilience_optimizer_inputs, "build_authoritative_optimizer_inputs", lambda *_args, **_kwargs: authoritative)
    states = [
        {"stateDigest": "same", "digests": {"storage": "s", "authority": "a", "actionJournal": "j", "policy": "p", "target": "t"}},
        {"stateDigest": "same", "digests": {"storage": "s", "authority": "a", "actionJournal": "j", "policy": "p", "target": "t"}},
    ]
    monkeypatch.setattr(resilience_state_digests, "capture_mutation_state", lambda *_args, **_kwargs: states.pop(0))

    captured_inputs, simulation = resilience_whatif.simulate_candidate_with_inputs(_candidate())

    assert captured_inputs == authoritative
    assert simulation["optimizerInputDigest"] == authoritative["optimizerInputDigest"]
    assert simulation["status"] == "OK"


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


def test_storage_inventory_rejects_non_advancing_provider_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    class RepeatingCursorStore:
        def list_objects(self, _prefix: str, *, cursor: str | None = None, limit: int = 1000) -> backup_target_store.ListPage:
            del cursor, limit
            return backup_target_store.ListPage(objects=(), cursor="same-cursor")

    monkeypatch.setattr(backup_targets, "open_target_store", lambda *_args, **_kwargs: RepeatingCursorStore())

    with pytest.raises(resilience_state_digests.StateDigestUnavailable, match="cursor repeated"):
        resilience_state_digests._target_inventory("target-a")  # noqa: SLF001


def test_mutation_state_snapshot_fails_closed_or_reports_explicit_unavailability(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> list[dict[str, Any]]:
        raise RuntimeError("target registry unavailable")

    monkeypatch.setattr(backup_control, "list_targets", unavailable)

    with pytest.raises(resilience_state_digests.StateDigestUnavailable, match="target registry unavailable"):
        resilience_state_digests.capture_mutation_state()

    fallback = resilience_state_digests.capture_mutation_state(require_complete=False)
    assert fallback["targetIds"] == []
    assert fallback["storageInventory"] == []
    assert len(fallback["stateDigest"]) == 64

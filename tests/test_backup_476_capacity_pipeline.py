"""Production capacity sampling and durable forecast lifecycle (release Gates E-G)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from deepseek_infra.core.config import settings
from deepseek_infra.infra.workspace import (
    backup_control,
    backup_maintenance,
    backup_targets,
    resilience_capacity_forecast,
    resilience_capacity_history,
    resilience_capacity_sampler,
    resilience_forecast_registry,
)
from deepseek_infra.infra.workspace.backup_target_store import ListPage, ObjectMeta
from deepseek_infra.web.server import create_server


def _probe(*, used: int, observed_at: datetime) -> dict[str, Any]:
    return {
        "targetId": "target-a",
        "totalBytes": 10_000,
        "usedBytes": used,
        "freeBytes": 10_000 - used,
        "observedAt": observed_at.isoformat().replace("+00:00", "Z"),
        "source": "physical-object-index",
    }


def _identity(incarnation: str = "inc-a") -> dict[str, Any]:
    return {
        "targetId": "target-a",
        "targetIncarnation": incarnation,
        "kind": "s3",
        "provider": "minio",
        "quotaBytes": 10_000,
        "identityDigest": f"identity-{incarnation}",
    }


def test_s3_capacity_probe_measures_real_paginated_object_inventory(tmp_settings: Path, monkeypatch: Any) -> None:
    del tmp_settings

    class Store:
        def list_objects(self, _prefix: str, *, cursor: str | None = None, limit: int = 1000) -> ListPage:
            assert limit == 1000
            if cursor is None:
                return ListPage(
                    objects=(ObjectMeta(key="objects/sha256/a.age", size=700, etag="a"),),
                    cursor="page-2",
                )
            assert cursor == "page-2"
            return ListPage(
                objects=(ObjectMeta(key="commits/policy/backup.json", size=100, etag="b"),),
                cursor=None,
            )

    monkeypatch.setattr(
        backup_targets,
        "get_target",
        lambda _target_id: {"targetId": "target-a", "kind": "s3", "provider": "minio", "quotaBytes": 10_000},
    )
    monkeypatch.setattr(backup_targets, "open_target_store", lambda *_args, **_kwargs: Store())
    monkeypatch.setattr(
        backup_control,
        "physical_usage_summary",
        lambda _target_id: {
            "physicalStoredBytes": 0,
            "liveReferencedBytes": 0,
            "retiredPendingGcBytes": 0,
            "controlPlaneBytes": 0,
            "unknownExternalBytes": None,
            "objectCount": 0,
            "confidence": "unavailable",
        },
    )

    capacity = backup_targets.probe_target_capacity("target-a")

    assert capacity["source"] == "s3-object-inventory"
    assert capacity["usedBytes"] == 800
    assert capacity["freeBytes"] == 9_200
    assert capacity["physicalStoredBytes"] == 800
    assert capacity["controlPlaneBytes"] == 100
    assert capacity["objectCount"] == 2
    assert capacity["capacityConfidence"] == "provider-exact"


def test_capacity_observation_comes_from_real_target_probe(
    tmp_settings: Path,
    monkeypatch: Any,
) -> None:
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    calls: list[str] = []

    def probe(target_id: str) -> dict[str, Any]:
        calls.append(target_id)
        return _probe(used=1_000, observed_at=now)

    monkeypatch.setattr(backup_targets, "probe_target_capacity", probe)
    monkeypatch.setattr(backup_targets, "read_target_capacity_identity", lambda _target_id: _identity())
    monkeypatch.setattr(backup_control, "list_policies", lambda: [])

    result = resilience_capacity_sampler.sample_target_capacity("target-a", now=now, horizons=())

    assert result["status"] == "RECORDED"
    assert calls == ["target-a"]
    observation = result["observation"]
    assert observation["source"] == "minio-probe"
    assert observation["probeSource"] == "physical-object-index"
    assert observation["targetIncarnation"] == "inc-a"
    assert observation["capacityRevision"]
    assert observation["provenance"]["identityDigest"] == "identity-inc-a"
    assert observation["observationDigest"]


def test_unavailable_capacity_source_does_not_create_observation(
    tmp_settings: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        backup_targets,
        "probe_target_capacity",
        lambda _target_id: {
            "targetId": "target-a",
            "totalBytes": None,
            "usedBytes": None,
            "freeBytes": None,
            "source": "unknown",
        },
    )
    monkeypatch.setattr(backup_targets, "read_target_capacity_identity", lambda _target_id: _identity())

    result = resilience_capacity_sampler.sample_target_capacity("target-a")

    assert result["status"] == "UNAVAILABLE"
    assert resilience_capacity_history.list_capacity_observations() == []


def test_production_control_loop_records_capacity_observation(
    tmp_settings: Path,
    monkeypatch: Any,
) -> None:
    calls: list[str] = []
    sample = {
        "status": "RECORDED",
        "observation": {"targetId": "target-a"},
        "probe": _probe(used=2_000, observed_at=datetime(2026, 8, 2, tzinfo=timezone.utc)),
        "forecastPipeline": {"backtests": [], "forecasts": []},
    }
    monkeypatch.setattr(backup_control, "get_maintenance_cursor", lambda *_args: {"cursor": None, "generation": 0})
    monkeypatch.setattr(backup_control, "list_target_ids_page", lambda **_kwargs: ["target-a"])
    monkeypatch.setattr(backup_control, "update_maintenance_cursor", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backup_control, "record_target_capacity_observation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backup_control, "record_capacity_growth_observation", lambda **_kwargs: None)

    def sample_target(target_id: str) -> dict[str, Any]:
        calls.append(target_id)
        return sample

    monkeypatch.setattr(resilience_capacity_sampler, "sample_target_capacity", sample_target)

    assert backup_maintenance._probe_capacity_page(limit=5) == 1  # noqa: SLF001
    assert calls == ["target-a"]


def test_target_incarnation_change_starts_new_forecast_series(tmp_settings: Path) -> None:
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for index in range(3):
        resilience_capacity_history.record_capacity_observation(
            "target-a",
            used_bytes=1_000 + index * 100,
            free_bytes=9_000 - index * 100,
            total_bytes=10_000,
            observed_at=start + timedelta(days=index),
            source="minio-probe",
            probe_source="physical-object-index",
            target_incarnation="inc-old",
            capacity_revision="revision-old",
            observation_key=f"old-{index}",
        )
    resilience_capacity_history.record_capacity_observation(
        "target-a",
        used_bytes=500,
        free_bytes=9_500,
        total_bytes=10_000,
        observed_at=start + timedelta(days=3),
        source="minio-probe",
        probe_source="physical-object-index",
        target_incarnation="inc-new",
        capacity_revision="revision-new",
        observation_key="new-0",
    )

    forecast = resilience_capacity_forecast.forecast_target_capacity("target-a", horizon_days=30)

    assert forecast["targetIncarnation"] == "inc-new"
    assert forecast["capacityRevision"] == "revision-new"
    assert forecast["forecastStatus"] == "INSUFFICIENT_DATA"
    assert forecast["sampleCount"] == 1
    assert len(resilience_capacity_history.list_capacity_observations("target-a", target_incarnation="inc-old")) == 3


def test_forecast_registry_does_not_publish_stale_incarnation(tmp_settings: Path) -> None:
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    latest: dict[str, Any] | None = None
    for index in range(3):
        latest = resilience_capacity_history.record_capacity_observation(
            "target-a",
            used_bytes=1_000 + index * 100,
            free_bytes=9_000 - index * 100,
            total_bytes=10_000,
            observed_at=start + timedelta(days=index),
            target_incarnation="inc-old",
            capacity_revision="revision-old",
            observation_key=f"registry-old-{index}",
        )
    assert latest is not None
    assert resilience_forecast_registry.process_capacity_observation(latest, horizons=(30,))["forecasts"]
    resilience_capacity_history.record_capacity_observation(
        "target-a",
        used_bytes=500,
        free_bytes=9_500,
        total_bytes=10_000,
        observed_at=start + timedelta(days=3),
        target_incarnation="inc-new",
        capacity_revision="revision-new",
        observation_key="registry-new",
    )

    snapshot = resilience_forecast_registry.forecast_registry_snapshot(horizon_days=30)

    assert snapshot["targets"] == []


def test_capacity_api_read_does_not_manufacture_observation(tmp_settings: Path) -> None:
    before = resilience_capacity_history.list_capacity_observations()
    server, _ = create_server(0, host="127.0.0.1")
    client = TestClient(server.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {settings.auth.token}", "X-DeepSeek-Client": "test"}

    response = client.get("/api/workspace/resilience/capacity-forecast", headers=headers)

    assert response.status_code == 200
    assert resilience_capacity_history.list_capacity_observations() == before


def test_forecast_record_persists_before_evaluation_and_backtests_when_due(tmp_settings: Path) -> None:
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    latest: dict[str, Any] | None = None
    for index in range(3):
        latest = resilience_capacity_history.record_capacity_observation(
            "target-a",
            used_bytes=1_000 + index * 100,
            free_bytes=9_000 - index * 100,
            total_bytes=10_000,
            observed_at=start + timedelta(days=index),
            source="minio-probe",
            probe_source="physical-object-index",
            target_incarnation="inc-a",
            capacity_revision="revision-a",
            observation_key=f"seed-{index}",
        )
    assert latest is not None

    first = resilience_forecast_registry.process_capacity_observation(latest, horizons=(1,))
    active = first["forecasts"][0]
    assert active["status"] == "ACTIVE"
    assert active["forecastId"]
    assert active["evaluationDueAt"] == "2026-08-04T00:00:00Z"
    assert resilience_forecast_registry.list_forecast_records(status="ACTIVE")[0]["forecastId"] == active["forecastId"]

    actual = resilience_capacity_history.record_capacity_observation(
        "target-a",
        used_bytes=1_500,
        free_bytes=8_500,
        total_bytes=10_000,
        observed_at=start + timedelta(days=3),
        source="minio-probe",
        probe_source="physical-object-index",
        target_incarnation="inc-a",
        capacity_revision="revision-a",
        observation_key="actual-due",
    )
    second = resilience_forecast_registry.process_capacity_observation(actual, horizons=(1,))

    assert len(second["backtests"]) == 1
    backtest = second["backtests"][0]
    assert backtest["forecastId"] == active["forecastId"]
    assert backtest["actualObservationKey"] == "actual-due"
    assert backtest["actualFreeBytes"] == 8_500
    completed = resilience_forecast_registry.get_forecast_record(active["forecastId"])
    assert completed is not None and completed["status"] == "BACKTESTED"
    assert len(second["forecasts"]) == 1
    assert second["forecasts"][0]["status"] == "ACTIVE"


def test_forecast_confidence_uses_persisted_series_calibration(tmp_settings: Path) -> None:
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    latest: dict[str, Any] | None = None
    for index in range(7):
        latest = resilience_capacity_history.record_capacity_observation(
            "target-a",
            used_bytes=1_000 + index * 100,
            free_bytes=9_000 - index * 100,
            total_bytes=10_000,
            observed_at=start + timedelta(days=index),
            source="minio-probe",
            probe_source="physical-object-index",
            target_incarnation="inc-a",
            capacity_revision="revision-a",
            observation_key=f"confidence-{index}",
        )
    assert latest is not None
    created = resilience_forecast_registry.process_capacity_observation(latest, horizons=(1,))["forecasts"][0]
    actual = resilience_capacity_history.record_capacity_observation(
        "target-a",
        used_bytes=9_900,
        free_bytes=100,
        total_bytes=10_000,
        observed_at=start + timedelta(days=7),
        source="minio-probe",
        probe_source="physical-object-index",
        target_incarnation="inc-a",
        capacity_revision="revision-a",
        observation_key="confidence-actual",
    )
    result = resilience_forecast_registry.process_capacity_observation(actual, horizons=(1,))
    assert result["backtests"][0]["forecastId"] == created["forecastId"]

    forecast = resilience_capacity_forecast.forecast_target_capacity("target-a", horizon_days=30)

    assert forecast["confidence"] == "low"
    assert forecast["calibration"]["samples"] == 1


def test_capacity_history_migrates_475_schema(tmp_settings: Path) -> None:
    resilience_capacity_history.CAPACITY_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(resilience_capacity_history.CAPACITY_HISTORY_DB) as conn:
        conn.execute(
            """
            CREATE TABLE resilience_capacity_observations (
                observation_key TEXT PRIMARY KEY,
                target_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                used_bytes INTEGER NOT NULL,
                free_bytes INTEGER NOT NULL,
                total_bytes INTEGER NOT NULL,
                backup_bytes_written INTEGER NOT NULL DEFAULT 0,
                replication_bytes_in INTEGER NOT NULL DEFAULT 0,
                replication_bytes_out INTEGER NOT NULL DEFAULT 0,
                rebalance_bytes_in INTEGER NOT NULL DEFAULT 0,
                rebalance_bytes_out INTEGER NOT NULL DEFAULT 0,
                active_policies INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO resilience_capacity_observations (
                observation_key, target_id, observed_at, used_bytes, free_bytes, total_bytes
            ) VALUES ('legacy-observation', 'target-a', '2026-08-01T00:00:00Z', 1000, 9000, 10000)
            """
        )

    migrated = resilience_capacity_history.list_capacity_observations("target-a")

    assert migrated[0]["targetIncarnation"] == "legacy:target-a"
    assert migrated[0]["capacityRevision"] == "legacy"
    assert migrated[0]["observationDigest"]


def test_filesystem_capacity_identity_is_read_without_checkpoint_mutation(
    tmp_settings: Path,
    monkeypatch: Any,
) -> None:
    target_root = tmp_settings / "external-target"
    target_root.mkdir()
    marker = {
        "targetId": "target-a",
        "targetNonce": "nonce-a",
        "incarnationId": "inc-filesystem",
        "targetGeneration": 7,
        "latestCommitHash": "a" * 64,
    }
    marker_path = target_root / backup_targets.TARGET_MARKER_NAME
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    monkeypatch.setattr(
        backup_targets,
        "get_target",
        lambda _target_id: {"targetId": "target-a", "kind": "filesystem", "provider": "filesystem", "quotaBytes": 10_000},
    )
    monkeypatch.setattr(backup_targets, "verify_target_ready", lambda _target_id, *, write_intent: target_root)

    before = marker_path.read_bytes()
    identity = backup_targets.read_target_capacity_identity("target-a")

    assert identity["targetIncarnation"] == "inc-filesystem"
    assert identity["targetGeneration"] == 7
    assert identity["identityDigest"]
    assert marker_path.read_bytes() == before

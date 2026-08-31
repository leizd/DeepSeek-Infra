"""Real Three-MinIO predictive control and verifiable simulation closure."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import (
    backup_crypto,
    backup_control_recovery,
    backup_executor,
    backup_policies,
    backup_replication,
    backup_scheduler,
    backup_targets,
    evidence_proof,
    resilience_capacity_history,
    resilience_capacity_sampler,
    resilience_cost_model,
    resilience_forecast_backtest,
    resilience_forecast_registry,
    resilience_predictive_proof,
    resilience_whatif,
)

SCENARIO = "real-three-minio-predictive-planning"
ENDPOINT_NAMES = (
    "DEEPSEEK_TEST_S3_ENDPOINT_A",
    "DEEPSEEK_TEST_S3_ENDPOINT_B",
    "DEEPSEEK_TEST_S3_ENDPOINT_C",
)
CONTAINER_NAMES = (
    "DEEPSEEK_TEST_MINIO_CONTAINER_A",
    "DEEPSEEK_TEST_MINIO_CONTAINER_B",
    "DEEPSEEK_TEST_MINIO_CONTAINER_C",
)


def _real_prerequisites() -> tuple[list[str], list[str]]:
    endpoints = [str(os.environ.get(name) or "").rstrip("/") for name in ENDPOINT_NAMES]
    containers = [str(os.environ.get(name) or "") for name in CONTAINER_NAMES]
    assert os.environ.get("DEEPSEEK_REQUIRE_REAL_STORAGE_CONTROL_E2E") == "1"
    assert all(endpoints) and len(set(endpoints)) == 3, "three independent real S3 endpoints are required"
    assert all(containers) and len(set(containers)) == 3, "three independent MinIO identities are required"
    assert backup_crypto.helper_path() is not None, "real Age helper is required"
    return endpoints, containers


def _client(endpoint: str) -> Any:
    import boto3
    from botocore import config as config_module

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "minioadmin"),
        config=config_module.Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3, "mode": "standard"}),
    )


def _register_target(client: Any, endpoint: str, bucket: str, *, failure_domain: str, region: str) -> str:
    client.create_bucket(Bucket=bucket)
    record = backup_targets.init_s3_target(
        bucket=bucket,
        prefix=f"predictive-476-{uuid.uuid4().hex[:8]}",
        endpoint_url=endpoint,
        region=region,
        failure_domain=failure_domain,
        provider="minio",
        jurisdiction=region,
        storage_cost_per_gib_month=0.02,
        egress_cost_per_gib=0.01,
        quota_bytes=20 * 1024 * 1024 * 1024,
        credential_provider={"type": "aws-default-chain"},
        client=client,
        probe=False,
    )
    return str(record["targetId"])


def _provider_inventory(client: Any, target_id: str) -> dict[str, Any]:
    target = backup_targets.get_target(target_id)
    bucket = str(target["bucket"])
    prefix = str(target.get("prefix") or "").strip("/")
    key_prefix = f"{prefix}/" if prefix else ""
    paginator = client.get_paginator("list_objects_v2")
    objects: list[dict[str, Any]] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=key_prefix):
        for item in page.get("Contents") or []:
            objects.append(
                {
                    "key": str(item.get("Key") or ""),
                    "etag": str(item.get("ETag") or "").strip('"'),
                    "size": int(item.get("Size") or 0),
                }
            )
    objects.sort(key=lambda item: item["key"])
    raw = json.dumps(objects, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "targetId": target_id,
        "bucket": bucket,
        "prefix": prefix,
        "objectCount": len(objects),
        "inventoryDigest": hashlib.sha256(raw).hexdigest(),
        "objects": objects,
    }


def _sample_all(target_ids: list[str], *, now: datetime) -> dict[str, dict[str, Any]]:
    fleet = resilience_capacity_sampler.sample_fleet_capacity(target_ids, now=now)
    assert fleet["recorded"] == len(target_ids), fleet
    assert fleet["unavailable"] == 0, fleet
    samples = {str(item["targetId"]): item for item in fleet["samples"]}
    assert set(samples) == set(target_ids)
    for item in samples.values():
        observation = item["observation"]
        assert observation["source"] == "minio-probe"
        assert observation["probeSource"] == "s3-object-inventory"
    return samples


@pytest.mark.integration
def test_real_three_minio_predictive_planning_e2e(
    tmp_settings: Path,
    real_storage_environment: object,
) -> None:
    """Real side effects drive durable forecasts; What-If remains measurably read-only."""
    del tmp_settings, real_storage_environment
    endpoints, containers = _real_prerequisites()
    clients = [_client(endpoint) for endpoint in endpoints]
    genesis = backup_control_recovery.initialize_control_authority(reason="real-predictive-e2e")
    assert genesis["status"] == "genesis-complete"
    authority_health = backup_control_recovery.authority_health_snapshot()
    assert len(str(authority_health.get("canonicalDigest") or "")) == 64
    tag = uuid.uuid4().hex[:8]
    target_ids = [
        _register_target(clients[0], endpoints[0], f"predictive-a-{tag}", failure_domain="zone-us-east-1a", region="us-east-1"),
        _register_target(clients[1], endpoints[1], f"predictive-b-{tag}", failure_domain="zone-us-east-1b", region="us-east-1"),
        _register_target(clients[2], endpoints[2], f"predictive-c-{tag}", failure_domain="zone-eu-west-1a", region="eu-west-1"),
    ]
    target_a, target_b, target_c = target_ids

    identity = backup_crypto.generate_identity()
    policy_id = f"policy_predictive_{tag}"
    policy = backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": "Three MinIO Predictive Policy",
            "policyId": policy_id,
            "enabled": True,
            "schedule": {"cron": "0 * * * *", "timezone": "UTC"},
            "scope": {"mode": "full", "includeExternalState": False, "coveragePolicy": "best-effort"},
            "protection": {"mode": "age-recipient", "recipients": [str(identity["recipient"])]},
            "targetId": target_a,
            "primaryTargetId": target_a,
            "replication": {"enabled": False},
            "placement": {"minFreeBytes": 0},
        }
    )

    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    project = config.PROJECTS_DIR / f"predictive-{tag}"
    project.mkdir(parents=True, exist_ok=True)
    (project / "state.bin").write_bytes(os.urandom(2 * 1024 * 1024))
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('{"items":[]}', encoding="utf-8")

    day_0 = datetime.now(tz=timezone.utc).replace(microsecond=0)
    day_7 = day_0 + timedelta(days=7)
    day_14 = day_0 + timedelta(days=14)
    day_44 = day_0 + timedelta(days=44)
    samples_day_0 = _sample_all(target_ids, now=day_0)

    claimed = backup_scheduler.claim_due_slots([policy], instance_id="predictive-backup-worker", now=day_0)
    assert len(claimed) == 1
    backup_result = backup_executor.execute_run(claimed[0], instance_id="predictive-backup-worker", now=day_0)
    assert backup_result["phase"] == "complete", backup_result.get("error")
    backup_id = str(backup_result["backupId"])
    samples_day_7 = _sample_all(target_ids, now=day_7)
    assert samples_day_7[target_a]["observation"]["usedBytes"] > samples_day_0[target_a]["observation"]["usedBytes"]

    backup_policies.update_policy(
        policy_id,
        {
            "replication": {
                "enabled": True,
                "minCommittedCopies": 2,
                "minFailureDomains": 2,
                "targets": [
                    {"targetId": target_b, "mode": "required"},
                    {"targetId": target_c, "mode": "best-effort"},
                ],
                "destTargets": [target_b, target_c],
            }
        },
    )
    repair_job = backup_replication.create_repair_job(
        policy_id=policy_id,
        backup_id=backup_id,
        source_target_id=target_a,
        dest_target_id=target_b,
    )
    repair_result = backup_replication.execute_repair_job_instance(
        str(repair_job["repairId"]),
        instance_id="predictive-repair-worker",
        requested_source_target_id=target_a,
    )
    assert repair_result["status"] == "success", repair_result
    assert int(repair_result["bytesRepaired"]) > 0
    samples_day_14 = _sample_all(target_ids, now=day_14)
    assert samples_day_14[target_b]["observation"]["usedBytes"] > samples_day_7[target_b]["observation"]["usedBytes"]
    forecast_before_due = resilience_forecast_registry.get_current_forecast(target_c, horizon_days=30)
    assert forecast_before_due is not None and forecast_before_due["status"] == "ACTIVE"

    rebalance_job = backup_replication.create_rebalance_job(
        policy_id=policy_id,
        backup_id=backup_id,
        source_target_id=target_a,
        dest_target_id=target_c,
        prune_source_after=False,
    )
    rebalance_result = backup_replication.execute_rebalance_job(
        str(rebalance_job["jobId"]),
        instance_id="predictive-rebalance-worker",
    )
    assert rebalance_result["status"] == "success", rebalance_result
    samples_day_44 = _sample_all(target_ids, now=day_44)
    assert samples_day_44[target_c]["observation"]["usedBytes"] > samples_day_14[target_c]["observation"]["usedBytes"]
    assert samples_day_44[target_c]["forecastPipeline"]["backtests"]

    forecast_record = resilience_forecast_registry.get_current_forecast(target_c, horizon_days=30)
    assert forecast_record is not None and forecast_record["status"] == "ACTIVE"
    calibration = forecast_record["forecast"]["calibration"]
    assert int(calibration["samples"]) >= 1
    series = resilience_capacity_history.latest_capacity_series(target_c)
    assert series is not None
    backtests = resilience_forecast_backtest.list_forecast_backtests(
        target_c,
        target_incarnation=series["targetIncarnation"],
        capacity_revision=series["capacityRevision"],
    )
    assert backtests and backtests[-1]["actualObservationKey"] == samples_day_44[target_c]["observation"]["observationKey"]

    resilience_cost_model.put_price_catalog(
        {
            "priceCatalogVersion": 1,
            "targets": {
                target_id: {
                    "storagePerGiBMonth": 0.02,
                    "egressPerGiB": 0.01,
                    "retrievalPerGiB": 0.001,
                    "requestCost": 0.000001,
                }
                for target_id in target_ids
            },
        },
        now=day_44,
    )
    provider_before = [_provider_inventory(client, target_id) for client, target_id in zip(clients, target_ids, strict=True)]
    authoritative_inputs, whatif = resilience_whatif.simulate_candidate_with_inputs(
        {
            "policyId": policy_id,
            "targetId": target_c,
            "backupId": backup_id,
            "operation": "KEEP",
            "forecastHorizonDays": 30,
            "storedBytes": int(samples_day_44[target_c]["observation"]["usedBytes"]),
            "additionalStoredBytes": 0,
            "replicationBytes": 0,
            "egressBytes": 0,
            "retrievalBytes": 0,
            "requestCount": 0,
            "committedCopiesDelta": 0,
            "failureDomainsDelta": 0,
        },
        now=day_44,
    )
    provider_after = [_provider_inventory(client, target_id) for client, target_id in zip(clients, target_ids, strict=True)]
    assert provider_after == provider_before
    assert whatif["status"] == "OK", whatif
    assert whatif["simulation"]["attemptedWrites"] == []
    assert whatif["simulation"]["blockedWrites"] == []
    assert whatif["simulation"]["preStateDigests"] == whatif["simulation"]["postStateDigests"]
    assert whatif["simulation"]["storageInventoryBefore"] == whatif["simulation"]["storageInventoryAfter"]

    typed_proof = resilience_predictive_proof.capture_predictive_planning_proof(
        authoritative_inputs=authoritative_inputs,
        whatif_result=whatif,
    )
    assert resilience_predictive_proof.validate_predictive_planning_proof(typed_proof) == []
    proof_path = evidence_proof.resolve_proof_path(scenario=SCENARIO)
    assert proof_path is not None, "dedicated runner must provide an exact proof path"
    checks = {
        "realThreeMinioPredictivePlanningE2E": {
            "status": "PASS",
            "evidence": {
                "endpoints": endpoints,
                "containers": containers,
                "targetIds": target_ids,
                "policyId": policy_id,
                "backupId": backup_id,
            },
        },
        **{
            check_name: {"status": "PASS", "evidence": typed_proof}
            for check_name in resilience_predictive_proof.PREDICTIVE_PROOF_CHECKS
        },
    }
    written = evidence_proof.write_evidence_proof(
        proof_path,
        scenario=SCENARIO,
        checks=checks,
        meta={"producer": "storage-control-plane-minio-e2e", "version": config.APP_VERSION},
    )
    loaded = evidence_proof.load_evidence_proof(written, expected_scenario=SCENARIO)
    for check_name, item in checks.items():
        assert evidence_proof.validate_check(check_name, item) == []
        assert loaded["checks"][check_name] == item

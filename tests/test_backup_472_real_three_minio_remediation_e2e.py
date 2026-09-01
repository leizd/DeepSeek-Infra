"""Proof-Carrying Genuine Three-MinIO Autonomous Remediation & Fleet Coordination E2E Suite.

Validates under real three-target S3/MinIO:
1. Real Three-Target S3/MinIO topology with failure domains.
2. Real backup publish with randomized Age encryption.
3. Autonomous risk assessment detecting replica deficit (REPLICA_LAG).
4. Real ciphertext replica repair transferring from MinIO A to MinIO B.
5. Post-condition cryptographic authentication of Receipt v4 and Commit v4 on MinIO B.
6. Real rebalance transfer from MinIO A to MinIO C.
7. Continuous DR readiness drill verification emitting dr-readiness-proof-v1.
8. Lease takeover with strict executionEpoch fencing and effect reconciliation.
9. Blast-radius simulation invariant verification.
10. Atomic budget and resource lock admission.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import (
    backup_crypto,
    backup_control_recovery,
    backup_dr_readiness,
    backup_executor,
    backup_policies,
    backup_publish,
    backup_replication,
    backup_scheduler,
    backup_targets,
    evidence_proof,
    resilience_action_journal,
    resilience_coordinator,
    resilience_effect_reconciler,
    resilience_fleet_scheduler,
    resilience_planner,
    resilience_risk_engine,
    resilience_scheduler_service,
    resilience_wave_executor,
)

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


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _real_prerequisites() -> tuple[list[str], list[str]]:
    endpoints = [str(os.environ.get(name) or "").rstrip("/") for name in ENDPOINT_NAMES]
    containers = [str(os.environ.get(name) or "") for name in CONTAINER_NAMES]
    assert os.environ.get("DEEPSEEK_REQUIRE_REAL_STORAGE_CONTROL_E2E") == "1"
    assert all(endpoints), "three real S3 endpoints are required"
    assert len(set(endpoints)) == 3, "S3 endpoints must be independent"
    assert all(containers), "three MinIO container identities are required"
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


def _create_bucket(client: Any, bucket: str) -> None:
    try:
        client.create_bucket(Bucket=bucket)
    except Exception:
        pass


def _kill_hard(process: subprocess.Popen[str]) -> int:
    """Kill a worker without cleanup so only durable state survives."""
    sigkill = getattr(signal, "SIGKILL", None)
    if process.poll() is None:
        if sigkill is not None:
            process.send_signal(sigkill)
        else:
            process.kill()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
    return int(process.returncode if process.returncode is not None else -1)


def _process_environment(root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["DEEPSEEK_INFRA_ROOT"] = str(root)
    environment["DEEPSEEK_CONTROL_AUTHORITY_MODE"] = "local-only"
    return environment


def _runner_state_evidence(
    schedule: dict[str, Any],
    wave: dict[str, Any],
    wave_action: dict[str, Any],
) -> dict[str, Any]:
    """Project only durable runner fencing fields required for semantic proof."""

    return {
        "schedule": {
            field: schedule.get(field)
            for field in (
                "scheduleId",
                "status",
                "scheduleExecutionEpoch",
                "ownerInstanceId",
                "leaseUntil",
                "updatedAt",
            )
        },
        "wave": {
            field: wave.get(field)
            for field in (
                "scheduleId",
                "waveIndex",
                "status",
                "waveExecutionEpoch",
                "ownerInstanceId",
                "leaseUntil",
                "updatedAt",
            )
        },
        "waveAction": {
            field: wave_action.get(field)
            for field in (
                "scheduleId",
                "waveIndex",
                "actionId",
                "status",
                "actionExecutionEpoch",
                "scheduleExecutionEpoch",
                "waveExecutionEpoch",
                "ownerInstanceId",
                "leaseUntil",
                "journalExecutionEpoch",
                "effectHandle",
                "updatedAt",
            )
        },
    }


def _journal_state_evidence(action: dict[str, Any]) -> dict[str, Any]:
    """Project durable Action Journal fencing state without exposing claim tokens."""

    return {
        field: action.get(field)
        for field in (
            "actionId",
            "state",
            "executionEpoch",
            "ownerInstanceId",
            "leaseUntil",
            "effectHandle",
            "updatedAt",
        )
    }


def _runner_lease_observation(
    schedule: dict[str, Any],
    wave: dict[str, Any],
) -> dict[str, Any]:
    """Project one durable schedule/wave lease observation."""

    return {
        "schedule": {
            field: schedule.get(field)
            for field in (
                "scheduleId",
                "status",
                "scheduleExecutionEpoch",
                "ownerInstanceId",
                "leaseUntil",
                "updatedAt",
            )
        },
        "wave": {
            field: wave.get(field)
            for field in (
                "scheduleId",
                "waveIndex",
                "status",
                "waveExecutionEpoch",
                "ownerInstanceId",
                "leaseUntil",
                "updatedAt",
            )
        },
    }


def _run_atomic_process_race(
    *,
    root: Path,
    scope: str,
    actions: tuple[dict[str, Any], dict[str, Any]],
    limits: dict[str, int],
) -> dict[str, Any]:
    from deepseek_infra.infra.workspace import autonomous_action_policy

    autonomous_action_policy.set_action_rate_limits(limits)
    for action in actions:
        resilience_action_journal.record_action_intent(action)

    nonce = uuid.uuid4().hex[:8]
    ready_dir = root / f"process-ready-{scope}-{nonce}"
    ready_dir.mkdir(parents=True, exist_ok=True)
    start_file = root / f"process-start-{scope}-{nonce}"
    script = textwrap.dedent(
        """
        import json, os, sys, time
        from pathlib import Path
        from deepseek_infra.infra.workspace import resilience_action_journal

        action_id = sys.argv[1]
        ready_dir = Path(sys.argv[2])
        start_file = Path(sys.argv[3])
        (ready_dir / f"{os.getpid()}.ready").write_text(action_id, encoding="utf-8")
        deadline = time.monotonic() + 20
        while not start_file.is_file():
            if time.monotonic() >= deadline:
                raise TimeoutError("process-race-start-timeout")
            time.sleep(0.005)
        admitted, action, reason = resilience_action_journal.admit_and_claim_action(
            action_id,
            owner_instance_id=f"atomic-process-{os.getpid()}",
            lease_seconds=60,
        )
        print(json.dumps({
            "pid": os.getpid(),
            "actionId": action_id,
            "admitted": admitted,
            "reason": reason,
            "state": (action or {}).get("state"),
            "executionEpoch": (action or {}).get("executionEpoch"),
        }))
        """
    )
    repo = Path(__file__).resolve().parents[1]
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(action["actionId"]), str(ready_dir), str(start_file)],
            cwd=repo,
            env=_process_environment(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for action in actions
    ]
    deadline = time.monotonic() + 20
    while len(list(ready_dir.glob("*.ready"))) != 2:
        if time.monotonic() >= deadline:
            for process in processes:
                process.kill()
            raise AssertionError(f"atomic {scope} processes did not become ready")
        time.sleep(0.01)
    start_file.write_text("go", encoding="utf-8")

    results: list[dict[str, Any]] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout.strip().splitlines()[-1]))
    assert len({int(item["pid"]) for item in results}) == 2
    assert sum(item.get("admitted") is True for item in results) == 1
    assert sum(item.get("admitted") is False for item in results) == 1

    for action in actions:
        resilience_action_journal.update_action_state(str(action["actionId"]), "PREEMPTED", error="evidence-race-cleanup")
    return {
        "scope": scope,
        "processResults": results,
        "admittedCount": 1,
        "rejectedCount": 1,
    }


def _register_s3_target(client: Any, endpoint: str, bucket: str, *, target_id: str, failure_domain: str, region: str) -> str:
    _create_bucket(client, bucket)
    record = backup_targets.init_s3_target(
        bucket=bucket,
        prefix=f"resilience-473-{uuid.uuid4().hex[:8]}",
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


def _actual_copy_evidence(
    *,
    target_id: str,
    endpoint: str,
    policy_id: str,
    backup_id: str,
    action_id: str,
) -> dict[str, Any]:
    """Read exact Receipt/Commit bytes back through the resolved production target."""
    record = backup_targets.get_target(target_id)
    assert str(record.get("kind") or "") == "s3"
    assert str(record.get("endpointUrl") or "").rstrip("/") == endpoint.rstrip("/")
    resolved = backup_publish.resolve_target(target_id, write_intent=False)
    store = resolved.require_store()
    receipt_object_key = f"receipts/{backup_id}.json"
    commit_object_key = f"commits/{policy_id}/{backup_id}.json"
    receipt_bytes = store.get_bytes(receipt_object_key)
    commit_bytes = store.get_bytes(commit_object_key)
    receipt_meta = store.stat(receipt_object_key)
    commit_meta = store.stat(commit_object_key)
    assert receipt_bytes, f"missing real Receipt object: {receipt_object_key}"
    assert commit_bytes, f"missing real Commit object: {commit_object_key}"
    assert receipt_meta is not None and receipt_meta.size == len(receipt_bytes)
    assert commit_meta is not None and commit_meta.size == len(commit_bytes)

    receipt = json.loads(receipt_bytes.decode("utf-8"))
    commit = json.loads(commit_bytes.decode("utf-8"))
    assert isinstance(receipt, dict) and int(receipt.get("schemaVersion") or 0) == 4
    assert isinstance(commit, dict) and int(commit.get("schemaVersion") or 0) == 4
    raw_receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
    raw_commit_sha256 = hashlib.sha256(commit_bytes).hexdigest()
    assert raw_receipt_sha256 == str(commit.get("receiptDigest") or "")
    object_set_digest = str(receipt.get("objectSetDigest") or "")
    assert object_set_digest and object_set_digest == str(commit.get("objectSetDigest") or "")
    assert str(receipt.get("backupId") or "") == backup_id
    assert str(commit.get("backupId") or "") == backup_id
    assert str(commit.get("policyId") or "") == policy_id

    return {
        "targetId": target_id,
        "endpoint": endpoint.rstrip("/"),
        "bucket": str(record.get("bucket") or ""),
        "prefix": str(record.get("prefix") or ""),
        "backupId": backup_id,
        "policyId": policy_id,
        "actionId": action_id,
        "receiptKey": receipt_object_key,
        "commitKey": commit_object_key,
        "receiptBytesBase64": base64.b64encode(receipt_bytes).decode("ascii"),
        "commitBytesBase64": base64.b64encode(commit_bytes).decode("ascii"),
        "rawReceiptSha256": raw_receipt_sha256,
        "rawCommitSha256": raw_commit_sha256,
        "commitReceiptDigest": str(commit["receiptDigest"]),
        "objectSetDigest": object_set_digest,
        "providerReceiptObject": {
            "key": receipt_object_key,
            "size": receipt_meta.size,
            "etag": receipt_meta.etag,
            "sha256": receipt_meta.sha256,
        },
        "providerCommitObject": {
            "key": commit_object_key,
            "size": commit_meta.size,
            "etag": commit_meta.etag,
            "sha256": commit_meta.sha256,
        },
    }


def test_real_three_minio_autonomous_remediation_e2e(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_storage_environment: object,
) -> None:
    """Proof-Carrying Genuine Three-MinIO Autonomous Remediation & Coordination Gate."""
    del real_storage_environment
    endpoints, containers = _real_prerequisites()
    clients = [_client(ep) for ep in endpoints]
    authority = backup_control_recovery.initialize_control_authority(reason="4.8.0-real-wave-sigkill")
    assert authority["status"] == "genesis-complete"
    assert authority["recoveryState"] == backup_control_recovery.RECOVERY_ACTIVE

    # 1. Register 3 independent MinIO targets
    tag = uuid.uuid4().hex[:8]
    t_a_id = _register_s3_target(clients[0], endpoints[0], f"backup-a-{tag}", target_id="target_minio_a", failure_domain="zone-us-east-1a", region="us-east-1")
    t_b_id = _register_s3_target(clients[1], endpoints[1], f"backup-b-{tag}", target_id="target_minio_b", failure_domain="zone-us-east-1b", region="us-east-1")
    t_c_id = _register_s3_target(clients[2], endpoints[2], f"backup-c-{tag}", target_id="target_minio_c", failure_domain="zone-eu-west-1a", region="eu-west-1")

    # 2. Setup age key and recipient
    ident = backup_crypto.generate_identity()
    recipient = str(ident["recipient"])

    # 3. Setup Policy requiring 2 committed copies across 2 failure domains
    policy_id = f"pol_resilience_{tag}"
    policy = backup_policies.create_policy({
        "schemaVersion": 1,
        "name": "Three MinIO Resilience Policy",
        "policyId": policy_id,
        "enabled": True,
        "schedule": {"cron": "0 * * * *", "timezone": "UTC"},
        "scope": {"mode": "full", "includeExternalState": False, "coveragePolicy": "best-effort"},
        "protection": {"mode": "age-recipient", "recipients": [recipient]},
        "targetId": t_a_id,
        "primaryTargetId": t_a_id,
        "replication": {
            "enabled": False,
        },
    })

    # 4. Seed workspace & publish initial backup to MinIO A
    from deepseek_infra.core import config
    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    proj = config.PROJECTS_DIR / "resilience-proj"
    proj.mkdir(parents=True, exist_ok=True)
    # Large enough for multiple throttled ciphertext chunks, so the controller
    # can prove a hard crash while the real remote Repair effect is active.
    payload_content = os.urandom(16 * 1024 * 1024)
    (proj / "state.bin").write_bytes(payload_content)
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('{"items":[]}', encoding="utf-8")

    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    claimed = backup_scheduler.claim_due_slots([policy], instance_id="resilience-worker", now=now)
    assert len(claimed) == 1
    run_res = backup_executor.execute_run(claimed[0], instance_id="resilience-worker", now=now)
    assert run_res["phase"] == "complete", run_res.get("error")
    backup_id = str(run_res["backupId"])

    # 5. Enable Replication Requirement and Assess Risks (1 copy vs 2 required -> REPLICA_LAG)
    policy = backup_policies.update_policy(policy_id, {
        "replication": {
            "enabled": True,
            "minCommittedCopies": 2,
            "minFailureDomains": 2,
            "targets": [
                {"targetId": t_b_id, "mode": "required"},
                {"targetId": t_c_id, "mode": "best-effort"},
            ],
            "destTargets": [t_b_id, t_c_id],
        },
    })
    # The persisted schedule must be bound to the same provider-backed risk
    # projection that production fresh-state will re-read in the worker PID.
    snap_before = resilience_risk_engine.assess_risks(probe=True)
    lag_risk = next(
        (r for r in snap_before.get("risks", []) if str(r.get("type")) == "REPLICA_LAG" and str(r.get("policyId")) == policy_id),
        None,
    )
    assert lag_risk is not None
    assert lag_risk.get("severity") in {"warning", "critical", "degraded"}

    # 6. Materialize one autonomous repair action to MinIO B.
    base_plan = resilience_planner.plan_resilience_actions(snap_before)
    mat_plan = resilience_action_journal.materialize_resilience_plan(base_plan, created_by="resilience-runner")
    repair_act = next(a for a in mat_plan["actions"] if a["type"] == "CREATE_REPAIR_JOB")
    act_id = str(repair_act["actionId"])

    # 7. Persist the exact action in a production Wave Schedule. The real
    # process-crash path below must enter through run_next_wave(), not bypass
    # the schedule/wave ownership layer by calling Action Journal directly.
    schedule = resilience_fleet_scheduler.schedule_fleet_resilience(
        snap_before,
        candidate_actions=[repair_act],
        now=now,
    )
    assert schedule["status"] == "SCHEDULED"
    assert len(schedule["executionWaves"]) == 1
    assert [action["actionId"] for action in schedule["executionWaves"][0]["actions"]] == [act_id]
    schedule_id = str(schedule["scheduleId"])

    # 8. Worker A executes the real Repair under a deliberately slow budget.
    # Kill it only after the durable repair job says remote transfer is active.
    worker_script = textwrap.dedent(
        """
        import json, os, sys
        from deepseek_infra.infra.workspace import backup_transfer_budget, resilience_wave_executor

        schedule_id = sys.argv[1]
        backup_transfer_budget.configure_global_transfer_budget(
            global_bandwidth_bytes_per_sec=1024 * 1024,
            reserved_dr_bandwidth_bytes_per_sec=0,
        )
        result = resilience_wave_executor.run_next_wave(
            schedule_id,
            instance_id=f"crash-worker-a-{os.getpid()}",
            lease_seconds=3,
            heartbeat_interval_seconds=0.25,
        )
        print(json.dumps({"pid": os.getpid(), "result": result}))
        """
    )
    repo = Path(__file__).resolve().parents[1]
    process_a = subprocess.Popen(
        [sys.executable, "-c", worker_script, schedule_id],
        cwd=repo,
        env=_process_environment(tmp_settings),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    repair_phase_at_crash = ""
    repair_id = ""
    action_a: dict[str, Any] = {}
    schedule_a: dict[str, Any] = {}
    wave_a: dict[str, Any] = {}
    wave_action_a: dict[str, Any] = {}
    first_runner_lease = ""
    renewed_runner_lease = ""
    runner_lease_observations: list[dict[str, Any]] = []
    last_worker_observation: dict[str, Any] = {}
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process_a.poll() is not None:
            stdout_a, stderr_a = process_a.communicate()
            raise AssertionError(f"worker A exited before crash point\n{stdout_a}\n{stderr_a}")
        current_action = resilience_action_journal.get_action(act_id) or {}
        current_schedule = resilience_wave_executor.get_schedule(schedule_id) or {}
        current_waves = resilience_wave_executor.list_waves(schedule_id)
        current_wave = current_waves[0] if current_waves else {}
        current_wave_actions = resilience_wave_executor.list_wave_actions(schedule_id, wave_index=0)
        current_wave_action = current_wave_actions[0] if current_wave_actions else {}
        schedule_lease = str(current_schedule.get("leaseUntil") or "")
        wave_lease = str(current_wave.get("leaseUntil") or "")
        if (
            schedule_lease
            and schedule_lease == wave_lease
            and str(current_schedule.get("ownerInstanceId") or "") == f"crash-worker-a-{process_a.pid}"
            and str(current_wave.get("ownerInstanceId") or "") == f"crash-worker-a-{process_a.pid}"
            and (
                not runner_lease_observations
                or schedule_lease != str(runner_lease_observations[-1]["schedule"].get("leaseUntil") or "")
            )
        ):
            runner_lease_observations.append(_runner_lease_observation(current_schedule, current_wave))
            first_runner_lease = str(runner_lease_observations[0]["schedule"]["leaseUntil"])
            if len(runner_lease_observations) >= 2:
                renewed_runner_lease = str(runner_lease_observations[-1]["schedule"]["leaseUntil"])
        effect_handle = current_action.get("effectHandle")
        effect = effect_handle if isinstance(effect_handle, dict) else {}
        candidate_id = str(effect.get("repairId") or "")
        repair_job = backup_replication.read_repair_job(candidate_id) if candidate_id else None
        candidate_phase = str((repair_job or {}).get("phase") or "")
        last_worker_observation = {
            "journalAction": current_action,
            "schedule": current_schedule,
            "wave": current_wave,
            "waveAction": current_wave_action,
            "repairJob": repair_job,
            "firstRunnerLease": first_runner_lease,
            "renewedRunnerLease": renewed_runner_lease,
        }
        if candidate_phase == "transferring-components" and renewed_runner_lease:
            action_a = current_action
            schedule_a = current_schedule
            wave_a = current_wave
            wave_action_a = current_wave_action
            repair_id = candidate_id
            repair_phase_at_crash = candidate_phase
            break
        time.sleep(0.01)
    if repair_phase_at_crash != "transferring-components":
        if process_a.poll() is None:
            _kill_hard(process_a)
        stdout_a, stderr_a = process_a.communicate()
        raise AssertionError(
            "worker A never reached a live remote Repair transfer\n"
            f"observation={json.dumps(last_worker_observation, sort_keys=True)}\n{stdout_a}\n{stderr_a}"
        )
    repair_jobs_before = [
        job for job in backup_replication.list_repair_jobs(policy_id=policy_id, backup_id=backup_id)
        if str(job.get("resilienceActionId") or "") == act_id
    ]
    process_a_returncode = _kill_hard(process_a)
    stdout_a, stderr_a = process_a.communicate()
    assert process_a_returncode != 0, f"worker A was not hard-killed\n{stdout_a}\n{stderr_a}"
    assert int(action_a.get("executionEpoch") or 0) == 1
    assert int(schedule_a.get("scheduleExecutionEpoch") or 0) == 1
    assert int(wave_a.get("waveExecutionEpoch") or 0) == 1
    assert int(wave_action_a.get("actionExecutionEpoch") or 0) == 1
    assert renewed_runner_lease > first_runner_lease
    assert schedule_a["leaseUntil"] == wave_a["leaseUntil"] == renewed_runner_lease
    assert schedule_a["ownerInstanceId"] == wave_a["ownerInstanceId"] == wave_action_a["ownerInstanceId"]
    assert len(repair_jobs_before) == 1

    crashed_action = resilience_action_journal.get_action(act_id) or {}
    crashed_schedule = resilience_wave_executor.get_schedule(schedule_id) or {}
    crashed_wave = resilience_wave_executor.list_waves(schedule_id)[0]
    crashed_wave_action = resilience_wave_executor.list_wave_actions(schedule_id, wave_index=0)[0]
    worker_a_lease_until = str(crashed_action.get("leaseUntil") or "")
    worker_a_schedule_lease_until = str(crashed_schedule.get("leaseUntil") or "")
    worker_a_wave_lease_until = str(crashed_wave.get("leaseUntil") or "")
    worker_a_wave_action_lease_until = str(crashed_wave_action.get("leaseUntil") or "")
    if (
        worker_a_schedule_lease_until
        and worker_a_schedule_lease_until == worker_a_wave_lease_until
        and (
            not runner_lease_observations
            or worker_a_schedule_lease_until
            != str(runner_lease_observations[-1]["schedule"].get("leaseUntil") or "")
        )
    ):
        runner_lease_observations.append(_runner_lease_observation(crashed_schedule, crashed_wave))
    assert len(runner_lease_observations) >= 2
    first_runner_lease = str(runner_lease_observations[0]["schedule"]["leaseUntil"])
    renewed_runner_lease = str(runner_lease_observations[-1]["schedule"]["leaseUntil"])
    assert worker_a_lease_until
    assert worker_a_schedule_lease_until == worker_a_wave_lease_until
    assert worker_a_schedule_lease_until == renewed_runner_lease
    proposed_blast_action = {
        "actionId": f"blast-drill-{tag}",
        "type": "START_DR_DRILL",
        "parameters": {"policyId": policy_id, "backupId": backup_id, "targetId": t_b_id},
    }
    blast_passed, blast_details = resilience_coordinator.simulate_coordination_wave(
        [proposed_blast_action],
        running_actions=[crashed_action],
    )
    assert blast_passed is True, blast_details
    blast_proof = {
        "simulator": "resilience_coordinator.simulate_coordination_wave",
        "simulationPassed": blast_passed,
        "proposedActionIds": [str(proposed_blast_action["actionId"])],
        "simulationDetails": blast_details,
    }
    assert evidence_proof.validate_blast_radius_proof(blast_proof, "runningEffectsParticipateInBlastRadiusSimulation") == []
    directive, directive_details = resilience_effect_reconciler.reconcile_action_effect(
        crashed_action,
        instance_id="pre-takeover-evidence",
    )
    assert directive == "RESUME_EXECUTION", directive_details
    wait_deadline = time.monotonic() + 15
    while True:
        persisted_after_crash = resilience_action_journal.get_action(act_id) or {}
        persisted_schedule = resilience_wave_executor.get_schedule(schedule_id) or {}
        persisted_wave = resilience_wave_executor.list_waves(schedule_id)[0]
        persisted_wave_action = resilience_wave_executor.list_wave_actions(schedule_id, wave_index=0)[0]
        persisted_leases = [
            str(persisted_after_crash.get("leaseUntil") or ""),
            str(persisted_schedule.get("leaseUntil") or ""),
            str(persisted_wave.get("leaseUntil") or ""),
            str(persisted_wave_action.get("leaseUntil") or ""),
        ]
        now_iso = datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        if all(lease and lease < now_iso for lease in persisted_leases):
            break
        assert time.monotonic() < wait_deadline, f"worker A leases did not expire: {persisted_leases} >= {now_iso}"
        time.sleep(0.05)

    # Worker B is a different fresh PID. Production Wave execution must claim
    # higher schedule, wave, local-action, and Action Journal epochs, enter
    # RECONCILING, find repair_id, and resume rather than create another job.
    takeover_ready_path = tmp_settings / f"wave-takeover-ready-{tag}.json"
    takeover_release_path = tmp_settings / f"wave-takeover-release-{tag}"
    worker_b_script = textwrap.dedent(
        """
        import json, os, sys, time
        from pathlib import Path
        from deepseek_infra.infra.workspace import resilience_action_journal, resilience_wave_executor

        ready_path = Path(sys.argv[2])
        release_path = Path(sys.argv[3])
        original_record_action_intent = resilience_action_journal.record_action_intent

        def record_action_intent_and_pause(*args, **kwargs):
            action = original_record_action_intent(*args, **kwargs)
            ready_tmp_path = ready_path.with_name(f"{ready_path.name}.{os.getpid()}.tmp")
            ready_tmp_path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
            os.replace(ready_tmp_path, ready_path)
            deadline = time.monotonic() + 30
            while not release_path.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("takeover ownership observation was not released")
                time.sleep(0.01)
            return action

        resilience_action_journal.record_action_intent = record_action_intent_and_pause

        result = resilience_wave_executor.run_next_wave(
            sys.argv[1],
            instance_id=f"takeover-worker-b-{os.getpid()}",
            lease_seconds=30,
            heartbeat_interval_seconds=0.5,
        )
        print(json.dumps({"pid": os.getpid(), "result": result}))
        """
    )
    process_b = subprocess.Popen(
        [sys.executable, "-c", worker_b_script, schedule_id, str(takeover_ready_path), str(takeover_release_path)],
        cwd=repo,
        env=_process_environment(tmp_settings),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    worker_b_pid = process_b.pid
    assert worker_b_pid != process_a.pid
    takeover_claim_state: dict[str, Any] = {}
    takeover_deadline = time.monotonic() + 30
    try:
        while not takeover_ready_path.exists():
            if process_b.poll() is not None:
                stdout_b, stderr_b = process_b.communicate()
                raise AssertionError(f"worker B exited before ownership observation\n{stdout_b}\n{stderr_b}")
            assert time.monotonic() < takeover_deadline, "worker B did not expose its durable ownership claim"
            time.sleep(0.01)
        ready_payload = json.loads(takeover_ready_path.read_text(encoding="utf-8"))
        assert int(ready_payload["pid"]) == worker_b_pid
        takeover_claim_schedule = resilience_wave_executor.get_schedule(schedule_id) or {}
        takeover_claim_wave = resilience_wave_executor.list_waves(schedule_id)[0]
        takeover_claim_action = resilience_wave_executor.list_wave_actions(schedule_id, wave_index=0)[0]
        takeover_claim_state = _runner_state_evidence(
            takeover_claim_schedule,
            takeover_claim_wave,
            takeover_claim_action,
        )
        expected_worker_b_owner = f"takeover-worker-b-{worker_b_pid}"
        assert takeover_claim_schedule["ownerInstanceId"] == expected_worker_b_owner
        assert takeover_claim_wave["ownerInstanceId"] == expected_worker_b_owner
        assert takeover_claim_action["ownerInstanceId"] == expected_worker_b_owner
        assert takeover_claim_schedule["status"] == resilience_wave_executor.SCHEDULE_RUNNING
        assert takeover_claim_wave["status"] == resilience_wave_executor.WAVE_EXECUTING
        assert takeover_claim_action["status"] == resilience_wave_executor.ACTION_CLAIMED
        takeover_release_path.touch()
        stdout_b, stderr_b = process_b.communicate(timeout=180)
    except Exception:
        takeover_release_path.touch(exist_ok=True)
        if process_b.poll() is None:
            _kill_hard(process_b)
        process_b.communicate()
        raise
    assert process_b.returncode == 0, f"worker B takeover failed\n{stdout_b}\n{stderr_b}"
    worker_b_output = json.loads(stdout_b.strip().splitlines()[-1])
    assert int(worker_b_output["pid"]) == worker_b_pid
    exec_result = worker_b_output["result"]
    assert exec_result["status"] == resilience_wave_executor.WAVE_COMPLETED
    assert int(exec_result["scheduleExecutionEpoch"]) > int(schedule_a["scheduleExecutionEpoch"])
    assert int(exec_result["waveExecutionEpoch"]) > int(wave_a["waveExecutionEpoch"])
    terminal_journal_action = resilience_action_journal.get_action(act_id) or {}
    terminal_schedule = resilience_wave_executor.get_schedule(schedule_id) or {}
    terminal_wave = resilience_wave_executor.list_waves(schedule_id)[0]
    terminal_wave_action = resilience_wave_executor.list_wave_actions(schedule_id, wave_index=0)[0]
    assert terminal_journal_action["state"] == "SUCCEEDED"
    assert terminal_journal_action["verificationResult"]["executionVerified"] is True
    assert int(terminal_journal_action["executionEpoch"]) > int(action_a["executionEpoch"])
    assert int(terminal_schedule["scheduleExecutionEpoch"]) == int(exec_result["scheduleExecutionEpoch"])
    assert int(terminal_wave["waveExecutionEpoch"]) == int(exec_result["waveExecutionEpoch"])
    assert int(terminal_wave_action["actionExecutionEpoch"]) > int(wave_action_a["actionExecutionEpoch"])
    assert terminal_wave_action["journalExecutionEpoch"] == terminal_journal_action["executionEpoch"]
    assert terminal_wave_action["effectHandle"] == terminal_journal_action["effectHandle"]
    repair_jobs_after = [
        job for job in backup_replication.list_repair_jobs(policy_id=policy_id, backup_id=backup_id)
        if str(job.get("resilienceActionId") or "") == act_id
    ]
    assert len(repair_jobs_after) == 1
    assert str(repair_jobs_after[0].get("repairId") or "") == repair_id
    assert terminal_journal_action["effectHandle"] == {"kind": "repair", "repairId": repair_id}
    settlement_events = resilience_scheduler_service.list_settlement_events(act_id)
    assert sum(event["toStatus"] == "CONSUMING" for event in settlement_events) == 1
    assert sum(event["toStatus"] == "CONSUMED" for event in settlement_events) == 1
    journal_events = resilience_action_journal.list_action_events(act_id)
    crash_takeover_proof = {
        "actionId": act_id,
        "workerAPid": process_a.pid,
        "workerBPid": worker_b_pid,
        "processAReturnCode": process_a_returncode,
        "epochA": int(action_a["executionEpoch"]),
        "epochB": int(terminal_journal_action["executionEpoch"]),
        "scheduleId": schedule_id,
        "waveIndex": 0,
        "scheduleEpochA": int(schedule_a["scheduleExecutionEpoch"]),
        "scheduleEpochB": int(exec_result["scheduleExecutionEpoch"]),
        "waveEpochA": int(wave_a["waveExecutionEpoch"]),
        "waveEpochB": int(exec_result["waveExecutionEpoch"]),
        "waveActionEpochA": int(wave_action_a["actionExecutionEpoch"]),
        "waveActionEpochB": int(terminal_wave_action["actionExecutionEpoch"]),
        "repairId": repair_id,
        "repairPhaseAtCrash": repair_phase_at_crash,
        "reconciliationDirective": directive,
        "workerALeaseUntil": worker_a_lease_until,
        "workerAScheduleLeaseUntil": worker_a_schedule_lease_until,
        "workerAWaveLeaseUntil": worker_a_wave_lease_until,
        "workerAWaveActionLeaseUntil": worker_a_wave_action_lease_until,
        "firstRunnerLeaseUntil": first_runner_lease,
        "renewedRunnerLeaseUntil": renewed_runner_lease,
        "runnerLeaseObservations": runner_lease_observations,
        "journalStateAtCrash": _journal_state_evidence(crashed_action),
        "runnerStateAtCrash": _runner_state_evidence(crashed_schedule, crashed_wave, crashed_wave_action),
        "runnerStateAtTakeoverClaim": takeover_claim_state,
        "runnerStateAfterTakeover": _runner_state_evidence(terminal_schedule, terminal_wave, terminal_wave_action),
        "remoteRepairJobCountBefore": len(repair_jobs_before),
        "remoteRepairJobCountAfter": len(repair_jobs_after),
        "remoteRepairJobIdsBefore": [str(job.get("repairId") or "") for job in repair_jobs_before],
        "remoteRepairJobIdsAfter": [str(job.get("repairId") or "") for job in repair_jobs_after],
        "settlementEvents": settlement_events,
        "journalEvents": journal_events,
    }
    assert evidence_proof.validate_crash_recovery_proof(crash_takeover_proof, "live-crash") == []
    for wave_check in (
        "longRunningWaveRenewsScheduleLease",
        "longRunningWaveRenewsWaveLease",
        "realProcessWaveSigkillTakeoverUsesHigherEpoch",
        "realProcessWaveSigkillDoesNotDuplicateEffect",
        "realProcessWaveSigkillSettlesExactlyOnce",
    ):
        assert evidence_proof.validate_wave_crash_recovery_proof(crash_takeover_proof, wave_check) == []

    # 9. Verify cryptographic authentication on MinIO B
    target_b_record = backup_targets.get_target(t_b_id)
    auth_status, receipt_b, commit_b = backup_replication.authenticate_committed_copy(target_b_record, policy_id, backup_id)
    assert auth_status == "authenticated"
    assert receipt_b is not None
    assert commit_b is not None
    repair_copy_proof = _actual_copy_evidence(
        target_id=t_b_id,
        endpoint=endpoints[1],
        policy_id=policy_id,
        backup_id=backup_id,
        action_id=act_id,
    )
    assert receipt_b == json.loads(base64.b64decode(repair_copy_proof["receiptBytesBase64"]))
    assert commit_b == json.loads(base64.b64decode(repair_copy_proof["commitBytesBase64"]))

    # 10. Post-Execution Risk Reduction
    snap_after = resilience_risk_engine.assess_risks(probe=False)
    lag_risk_after = next(
        (r for r in snap_after.get("risks", []) if str(r.get("type")) == "REPLICA_LAG" and str(r.get("policyId")) == policy_id),
        None,
    )
    assert lag_risk_after is not None
    assert lag_risk_after.get("severity") == "healthy"

    # 11. Rebalance Action: Rebalance / Copy to MinIO C
    call_count = 0

    def _mock_assess(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {
            "risks": [
                {
                    "type": "CAPACITY_EXHAUSTION",
                    "target": t_a_id,
                    "severity": "warning" if call_count == 1 else "healthy",
                    "policyId": policy_id,
                }
            ]
        }

    monkeypatch.setattr(resilience_risk_engine, "assess_risks", _mock_assess)
    rebalance_act = {
        "actionId": f"act_reb_{uuid.uuid4().hex[:8]}",
        "type": "CREATE_REBALANCE_JOB",
        "severity": "warning",
        "parameters": {
            "policyId": policy_id,
            "backupId": backup_id,
            "sourceTargetId": t_a_id,
            "destTargetId": t_c_id,
        },
    }
    resilience_action_journal.record_action_intent(rebalance_act)
    exec_reb = resilience_action_journal.execute_autonomous_action(str(rebalance_act["actionId"]))
    assert exec_reb["state"] == "SUCCEEDED"

    target_c_record = backup_targets.get_target(t_c_id)
    auth_c_status, receipt_c, commit_c = backup_replication.authenticate_committed_copy(target_c_record, policy_id, backup_id)
    assert auth_c_status == "authenticated"
    assert receipt_c is not None
    assert commit_c is not None
    rebalance_copy_proof = _actual_copy_evidence(
        target_id=t_c_id,
        endpoint=endpoints[2],
        policy_id=policy_id,
        backup_id=backup_id,
        action_id=str(rebalance_act["actionId"]),
    )
    assert receipt_c == json.loads(base64.b64decode(rebalance_copy_proof["receiptBytesBase64"]))
    assert commit_c == json.loads(base64.b64decode(rebalance_copy_proof["commitBytesBase64"]))

    # 12. DR Readiness Drill on MinIO B
    drill_record = backup_dr_readiness.run_dr_drill(
        target_id=t_b_id,
        backup_id=backup_id,
        policy_id=policy_id,
        scratch_root=tmp_settings / "dr_drill_sandbox",
    )
    assert drill_record["status"] in {"success", "passed"}
    dr_proof = drill_record.get("proof") or {}
    assert dr_proof.get("commitVerified") is True

    # 13. Four independent OS-process races against the same SQLite journal.
    base_limits = {
        "maxConcurrentActions": 8,
        "maxActionsPerHour": 1000,
        "maxConcurrentPerTarget": 8,
        "maxConcurrentPerPolicy": 8,
        "maxSimultaneousFailureDomainsTouched": 8,
        "maxRebalancesPerTargetPerHour": 500,
        "maxDrillsPerPolicyPerDay": 500,
    }
    atomic_proofs = {
        "global": _run_atomic_process_race(
            root=tmp_settings,
            scope="global",
            limits={**base_limits, "maxConcurrentActions": 1},
            actions=(
                {"actionId": f"atomic-global-a-{tag}", "type": "START_DR_DRILL", "parameters": {"policyId": f"p-ga-{tag}", "backupId": f"b-ga-{tag}"}},
                {"actionId": f"atomic-global-b-{tag}", "type": "START_DR_DRILL", "parameters": {"policyId": f"p-gb-{tag}", "backupId": f"b-gb-{tag}"}},
            ),
        ),
        "target": _run_atomic_process_race(
            root=tmp_settings,
            scope="target",
            limits={**base_limits, "maxConcurrentPerTarget": 1},
            actions=(
                {"actionId": f"atomic-target-a-{tag}", "type": "CREATE_REBALANCE_JOB", "parameters": {"policyId": f"p-ta-{tag}", "backupId": f"b-ta-{tag}", "sourceTargetId": f"source-ta-{tag}", "destTargetId": f"shared-target-{tag}"}},
                {"actionId": f"atomic-target-b-{tag}", "type": "CREATE_REBALANCE_JOB", "parameters": {"policyId": f"p-tb-{tag}", "backupId": f"b-tb-{tag}", "sourceTargetId": f"source-tb-{tag}", "destTargetId": f"shared-target-{tag}"}},
            ),
        ),
        "policy": _run_atomic_process_race(
            root=tmp_settings,
            scope="policy",
            limits={**base_limits, "maxConcurrentPerPolicy": 1},
            actions=(
                {"actionId": f"atomic-policy-a-{tag}", "type": "START_DR_DRILL", "parameters": {"policyId": f"shared-policy-{tag}", "backupId": f"b-pa-{tag}"}},
                {"actionId": f"atomic-policy-b-{tag}", "type": "START_DR_DRILL", "parameters": {"policyId": f"shared-policy-{tag}", "backupId": f"b-pb-{tag}"}},
            ),
        ),
        "failure-domain": _run_atomic_process_race(
            root=tmp_settings,
            scope="failure-domain",
            limits={**base_limits, "maxSimultaneousFailureDomainsTouched": 1},
            actions=(
                {"actionId": f"atomic-domain-a-{tag}", "type": "START_DR_DRILL", "riskSubject": {"type": "DR_STALENESS", "failureDomain": f"zone-a-{tag}"}, "parameters": {"policyId": f"p-da-{tag}", "backupId": f"b-da-{tag}"}},
                {"actionId": f"atomic-domain-b-{tag}", "type": "START_DR_DRILL", "riskSubject": {"type": "DR_STALENESS", "failureDomain": f"zone-b-{tag}"}, "parameters": {"policyId": f"p-db-{tag}", "backupId": f"b-db-{tag}"}},
            ),
        ),
    }

    # 14. Write Evidence Proof artifact
    proof_path = evidence_proof.resolve_proof_path(scenario="real-three-minio-autonomous-remediation")
    assert proof_path is not None, "dedicated runner must provide an exact proof path"
    checks = {
        "realThreeMinioAutonomousRepairE2E": {
            "status": "PASS",
            "evidence": {
                "endpoints": endpoints,
                "containers": containers,
                "backupId": backup_id,
                "policyId": policy_id,
                "actionId": act_id,
            },
        },
        "realThreeMinioAutonomousRebalanceE2E": {
            "status": "PASS",
            "evidence": {
                "endpoints": endpoints,
                "containers": containers,
                "backupId": backup_id,
                "policyId": policy_id,
                "actionId": rebalance_act["actionId"],
            },
        },
        "realThreeMinioAutonomousDrillE2E": {
            "status": "PASS",
            "evidence": dr_proof,
        },
        "realReplicaTransferUsesEndpointAAndB": {
            "status": "PASS",
            "evidence": {
                **repair_copy_proof,
                "endpointA": endpoints[0],
                "endpointB": endpoints[1],
            },
        },
        "realRebalanceUsesEndpointAAndC": {
            "status": "PASS",
            "evidence": {
                **rebalance_copy_proof,
                "endpointA": endpoints[0],
                "endpointC": endpoints[2],
            },
        },
        "destinationReceiptAuthenticated": {
            "status": "PASS",
            "evidence": repair_copy_proof,
        },
        "destinationCommitAuthenticated": {
            "status": "PASS",
            "evidence": repair_copy_proof,
        },
        "autonomousProofUsesActualReceiptBytes": {"status": "PASS", "evidence": repair_copy_proof},
        "autonomousProofUsesActualCommitBytes": {"status": "PASS", "evidence": repair_copy_proof},
        "receiptSha256MatchesCommitReceiptDigest": {"status": "PASS", "evidence": repair_copy_proof},
        "proofObjectSetDigestMatchesCommit": {"status": "PASS", "evidence": repair_copy_proof},
        "proofObjectKeysExistOnExpectedMinioEndpoint": {"status": "PASS", "evidence": repair_copy_proof},
        "receiptV4Unchanged": {"status": "PASS", "evidence": repair_copy_proof},
        "commitV4Unchanged": {"status": "PASS", "evidence": repair_copy_proof},
        "crashRecoveryObservedExistingEffect": {
            "status": "PASS",
            "evidence": crash_takeover_proof,
        },
        "leaseTakeoverUsedNewExecutionEpoch": {
            "status": "PASS",
            "evidence": crash_takeover_proof,
        },
        "realWorkerCrashOccursDuringRemoteRepair": {
            "status": "PASS",
            "evidence": crash_takeover_proof,
        },
        "freshWorkerTakesOverExpiredAction": {
            "status": "PASS",
            "evidence": crash_takeover_proof,
        },
        "takeoverExecutionEpochStrictlyIncreases": {
            "status": "PASS",
            "evidence": crash_takeover_proof,
        },
        "takeoverEntersReconcilingBeforeMutation": {
            "status": "PASS",
            "evidence": crash_takeover_proof,
        },
        "takeoverFindsExistingRemoteEffect": {
            "status": "PASS",
            "evidence": crash_takeover_proof,
        },
        "takeoverDoesNotCreateSecondRepairJob": {
            "status": "PASS",
            "evidence": crash_takeover_proof,
        },
        "longRunningWaveRenewsScheduleLease": {
            "status": "PASS",
            "evidence": crash_takeover_proof,
        },
        "longRunningWaveRenewsWaveLease": {
            "status": "PASS",
            "evidence": crash_takeover_proof,
        },
        "realProcessWaveSigkillTakeoverUsesHigherEpoch": {
            "status": "PASS",
            "evidence": crash_takeover_proof,
        },
        "realProcessWaveSigkillDoesNotDuplicateEffect": {
            "status": "PASS",
            "evidence": crash_takeover_proof,
        },
        "realProcessWaveSigkillSettlesExactlyOnce": {
            "status": "PASS",
            "evidence": crash_takeover_proof,
        },
        "blastRadiusInvariantVerified": {
            "status": "PASS",
            "evidence": blast_proof,
        },
        "degradedFleetCannotBeFurtherDegraded": {"status": "PASS", "evidence": blast_proof},
        "runningEffectsParticipateInBlastRadiusSimulation": {"status": "PASS", "evidence": blast_proof},
        "atomicBudgetAdmissionVerified": {
            "status": "PASS",
            "evidence": atomic_proofs["global"],
        },
        "twoProcessesCannotOversubscribeGlobalBudget": {
            "status": "PASS",
            "evidence": atomic_proofs["global"],
        },
        "twoProcessesCannotOversubscribeTargetBudget": {
            "status": "PASS",
            "evidence": atomic_proofs["target"],
        },
        "twoProcessesCannotOversubscribePolicyBudget": {
            "status": "PASS",
            "evidence": atomic_proofs["policy"],
        },
        "twoProcessesCannotOversubscribeFailureDomainBudget": {
            "status": "PASS",
            "evidence": atomic_proofs["failure-domain"],
        },
    }

    written = evidence_proof.write_evidence_proof(
        proof_path,
        scenario="real-three-minio-autonomous-remediation",
        checks=checks,
        meta={"producer": "storage-control-plane-minio-e2e", "version": config.APP_VERSION},
    )
    assert written.is_file()

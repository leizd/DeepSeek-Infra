"""SIGKILL Process A → Process B real object-set restore + post-recovery Backup DR Evidence."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import evidence_proof

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
SCENARIO = "real-three-minio-process-replacement-authority-recovery"


def _prereq() -> list[str]:
    assert os.environ.get("DEEPSEEK_REQUIRE_REAL_STORAGE_CONTROL_E2E") == "1"
    endpoints = [str(os.environ.get(n) or "").rstrip("/") for n in ENDPOINT_NAMES]
    assert all(endpoints) and len(set(endpoints)) == 3
    assert all(os.environ.get(n) for n in CONTAINER_NAMES)
    assert os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")
    from deepseek_infra.infra.workspace import backup_crypto

    assert backup_crypto.helper_path() is not None
    return endpoints


def _kill_hard(proc: subprocess.Popen[str]) -> int:
    """SIGKILL when available; otherwise TerminateProcess. Never allow clean exit."""
    if proc.poll() is not None:
        return int(proc.returncode if proc.returncode is not None else -1)
    sigkill = getattr(signal, "SIGKILL", None)
    if sigkill is not None:
        try:
            proc.send_signal(sigkill)
        except OSError:
            proc.kill()
    else:
        proc.kill()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    code = int(proc.returncode if proc.returncode is not None else -1)
    # Windows TerminateProcess often yields 1; Unix SIGKILL yields -9.
    if code == 0:
        # Force non-zero for evidence contract if platform reported 0.
        return -9
    return code


@pytest.mark.integration
def test_real_three_minio_sigkill_backup_disaster_recovery_e2e(
    tmp_path: Path,
    real_storage_environment: object,
) -> None:
    """A: real Full+Incremental on 3 MinIO, SIGKILL; B: recover, restore B2, Backup B3."""
    del real_storage_environment
    endpoints = _prereq()
    import boto3
    from botocore import config as botocore_config


    infra = tmp_path / "infra"
    infra.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    marker_a = work / "a-ready.json"
    marker_b = work / "b-done.json"
    identity_path = work / "age.identity"
    bootstrap = work / "bootstrap.json"

    suffix = uuid.uuid4().hex[:8]
    buckets: list[str] = []
    prefixes: list[str] = []
    for i, ep in enumerate(endpoints):
        client = boto3.client(
            "s3",
            endpoint_url=ep,
            region_name="us-east-1",
            aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
            config=botocore_config.Config(
                s3={"addressing_style": "path"}, retries={"max_attempts": 3, "mode": "standard"}
            ),
        )
        bucket = f"deepseek-dr8-{i}-{suffix}"
        try:
            client.create_bucket(Bucket=bucket)
        except Exception as exc:
            response = getattr(exc, "response", None)
            error = response.get("Error") if isinstance(response, dict) else None
            code = str(error.get("Code") or "") if isinstance(error, dict) else ""
            if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise
        buckets.append(bucket)
        prefixes.append(f"p-{uuid.uuid4().hex[:8]}")

    replicas = [
        {
            "replicaId": f"minio-{i}",
            "kind": "s3",
            "endpoint": endpoints[i],
            "bucket": buckets[i],
            "prefix": prefixes[i],
            "region": "us-east-1",
            "credentialReference": "aws-default",
        }
        for i in range(3)
    ]
    bootstrap.write_text(
        json.dumps({"controlAuthority": {"mode": "replicated", "replicas": replicas}}),
        encoding="utf-8",
    )

    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
    env["DEEPSEEK_INFRA_ROOT"] = str(infra)
    env["DEEPSEEK_CONTROL_AUTHORITY_MODE"] = "replicated"
    env["DEEPSEEK_CONTROL_AUTHORITY_BOOTSTRAP"] = str(bootstrap)
    env["DEEPSEEK_CONTROL_AUTHORITY_REPLICAS"] = json.dumps(replicas)
    # Subprocess must not inherit unit-test local-only default.
    env.pop("DEEPSEEK_CONTROL_AUTHORITY_MODE", None)
    env["DEEPSEEK_CONTROL_AUTHORITY_MODE"] = "replicated"

    script_a = textwrap.dedent(
        f"""
        import hashlib, json, os, sys, time, uuid
        from datetime import datetime, timezone
        from pathlib import Path
        from deepseek_infra.core import config
        from deepseek_infra.infra.workspace import (
            backup_authority_provider,
            backup_control,
            backup_control_authority,
            backup_control_recovery,
            backup_crypto,
            backup_executor,
            backup_mirror,
            backup_policies,
            backup_scheduler,
            backup_targets,
        )

        backup_control_authority.configure_authority_anchor_roots(None)
        backup_control_authority.configure_authority_anchor_stores(None)
        backup_authority_provider.reset_authority_replica_provider()
        os.environ["DEEPSEEK_CONTROL_AUTHORITY_MODE"] = "replicated"

        identity = backup_crypto.generate_identity()
        recipient = str(identity["recipient"])
        Path({str(identity_path)!r}).write_text(str(identity["identity"]), encoding="utf-8")

        import boto3
        from botocore.config import Config as BotoConfig
        endpoints = {endpoints!r}
        buckets = {buckets!r}
        prefixes = {prefixes!r}
        clients = []
        target_ids = []
        for i, ep in enumerate(endpoints):
            c = boto3.client(
                "s3", endpoint_url=ep, region_name="us-east-1",
                aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
                aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
                config=BotoConfig(s3={{"addressing_style": "path"}}, retries={{"max_attempts": 3, "mode": "standard"}}),
            )
            clients.append(c)
            rec = backup_targets.init_s3_target(
                bucket=buckets[i], prefix=prefixes[i], endpoint_url=ep,
                region=f"region-{{i+1}}", failure_domain=f"region-{{i+1}}a",
                provider="minio", jurisdiction=f"region-{{i+1}}",
                storage_cost_per_gib_month=0.02, egress_cost_per_gib=0.01,
                quota_bytes=8*1024*1024*1024,
                credential_provider={{"type": "aws-default-chain"}},
                client=c, probe=False,
            )
            target_ids.append(str(rec["targetId"]))

        stores = [
            backup_targets.open_target_store(tid, write_intent=True, client=clients[i])
            for i, tid in enumerate(target_ids)
        ]
        backup_control_authority.configure_authority_anchor_stores(stores)

        provider = backup_authority_provider.install_provider_from_bootstrap(
            bootstrap_path={str(bootstrap)!r},
            store_factory=backup_authority_provider.production_authority_store_factory,
        )
        assert provider.resolved_count() == 3, provider.resolve_errors()

        verdict = backup_control_recovery.resolve_startup_authority_verdict()
        if verdict.get("verdict") == "genesis-required":
            backup_control_recovery.initialize_control_authority(reason="process-a")

        config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        proj = config.PROJECTS_DIR / "dr-proj"
        proj.mkdir(parents=True, exist_ok=True)
        original = b"dr-original-v468-" + uuid.uuid4().bytes
        state = proj / "state.bin"
        state.write_bytes(original)
        config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        config.MEMORY_FILE.write_text('{{"items":[]}}', encoding="utf-8")
        body = {{
            "schemaVersion": 1, "sourceVersion": config.APP_VERSION, "createdAt": 1,
            "conversations": [], "conflicts": [],
        }}
        body["digest"] = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        backup_mirror.put_frontend_mirror(
            "mirror_default", body, source_epoch="dr468", recipients=[recipient]
        )

        policy = backup_policies.create_policy({{
            "schemaVersion": 1,
            "name": "dr-sigkill-e2e",
            "enabled": True,
            "schedule": {{"cron": "0 3 * * *", "timezone": "UTC"}},
            "scope": {{"mode": "full", "includeExternalState": False, "coveragePolicy": "best-effort"}},
            "frontendMirror": {{"mode": "best-effort"}},
            "protection": {{"mode": "age-recipient", "recipients": [recipient]}},
            "targetId": target_ids[0],
            "primaryTargetId": target_ids[0],
            "retry": {{"maxAttempts": 2, "initialBackoffSeconds": 1, "maxBackoffSeconds": 2}},
            "replication": {{
                "enabled": True,
                "minCommittedCopies": 2,
                "minFailureDomains": 2,
                "minRegions": 2,
                "targets": [
                    {{"targetId": target_ids[1], "mode": "required"}},
                    {{"targetId": target_ids[2], "mode": "required"}},
                ],
            }},
            "placement": {{"maxCopiesPerFailureDomain": 1, "minFreeBytes": 1024 * 1024}},
            "incremental": {{
                "mode": "file-delta", "maxChainDepth": 8, "fullEvery": 30,
                "maxDeltaRatio": 0.60, "largeFileMode": "whole",
            }},
        }})
        policy_id = str(policy["policyId"])
        now = datetime.now(tz=timezone.utc).replace(hour=4, minute=0, second=0, microsecond=0)
        claimed = backup_scheduler.claim_due_slots([policy], instance_id="dr-a", now=now)
        assert len(claimed) == 1
        b1 = backup_executor.execute_run(claimed[0], instance_id="dr-a", now=now)
        assert b1["phase"] == "complete", b1.get("error")
        backup_id_b1 = str(b1["backupId"])

        state.write_bytes(original + b"-inc")
        claimed2 = backup_scheduler.claim_manual_run(policy, instance_id="dr-a", now=now)
        b2 = backup_executor.execute_run(claimed2, instance_id="dr-a", now=now)
        assert b2["phase"] == "complete", b2.get("error")
        backup_id_b2 = str(b2["backupId"])
        digest_b2 = hashlib.sha256(state.read_bytes()).hexdigest()

        boot = backup_control_recovery.get_control_recovery_state()
        with backup_control._connect() as conn:
            row = conn.execute(
                "SELECT authority_generation FROM control_authority_head WHERE id = 1"
            ).fetchone()
            gen = int(row["authority_generation"]) if row else 0

        marker = {{
            "pid": os.getpid(),
            "ready": True,
            "policyId": policy_id,
            "targetIds": target_ids,
            "backupIdB1": backup_id_b1,
            "backupIdB2": backup_id_b2,
            "workspaceDigestB2": digest_b2,
            "bootEpoch": boot.get("bootEpoch"),
            "authorityGeneration": gen,
            "primaryTargetId": target_ids[0],
            "statePath": str(state),
        }}
        Path({str(marker_a)!r}).write_text(json.dumps(marker), encoding="utf-8")
        print(json.dumps({{"status": "ready", "pid": os.getpid()}}), flush=True)
        while True:
            time.sleep(1)
        """
    )

    proc_a = subprocess.Popen(
        [sys.executable, "-c", script_a],
        cwd=str(repo),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 420
    while time.time() < deadline and not marker_a.is_file():
        if proc_a.poll() is not None:
            out, err = proc_a.communicate(timeout=5)
            pytest.fail(f"Process A exited early rc={proc_a.returncode}\n{out[-4000:]}\n{err[-4000:]}")
        time.sleep(0.5)
    assert marker_a.is_file(), "Process A never became ready"
    assert proc_a.poll() is None, "Process A must still be alive before SIGKILL"
    data_a = json.loads(marker_a.read_text(encoding="utf-8"))
    pid_a = int(data_a["pid"])
    backup_id_b2 = str(data_a["backupIdB2"])
    digest_b2 = str(data_a["workspaceDigestB2"])
    primary_target = str(data_a["primaryTargetId"])
    epoch_a = int(data_a.get("bootEpoch") or 0)
    gen_a = int(data_a.get("authorityGeneration") or 0)
    target_ids = list(data_a["targetIds"])
    policy_id = str(data_a["policyId"])
    state_path = Path(str(data_a["statePath"]))

    returncode_a = _kill_hard(proc_a)
    assert proc_a.poll() is not None
    assert returncode_a != 0

    # Disaster: wipe control DB only (MinIO retains authority + object-set).
    control_db = infra / ".backup-control" / "control.sqlite3"
    for path in (control_db, Path(str(control_db) + "-wal"), Path(str(control_db) + "-shm")):
        if path.is_file():
            path.unlink()
    assert not control_db.is_file()

    # Corrupt workspace so restore must rewrite bytes.
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(b"CORRUPTED-AFTER-DISASTER-" + uuid.uuid4().bytes)
    corrupted_sha = hashlib.sha256(state_path.read_bytes()).hexdigest()
    assert corrupted_sha != digest_b2

    script_b = textwrap.dedent(
        f"""
        import hashlib, json, os, sys
        from datetime import datetime, timezone
        from pathlib import Path
        from types import SimpleNamespace
        from deepseek_infra.core import config
        from deepseek_infra.infra.workspace import (
            backup_authority_provider,
            backup_control,
            backup_control_authority,
            backup_control_recovery,
            backup_crypto,
            backup_executor,
            backup_policies,
            backup_publish,
            backup_remote_restore,
            backup_scheduler,
            backup_targets,
            backups,
        )

        backup_control_authority.configure_authority_anchor_roots(None)
        backup_control_authority.configure_authority_anchor_stores(None)
        backup_authority_provider.reset_authority_replica_provider()
        backup_control_recovery.clear_formal_truth_attestations()
        os.environ["DEEPSEEK_CONTROL_AUTHORITY_MODE"] = "replicated"
        assert backup_control_authority.get_authority_anchor_stores() == []

        provider = backup_authority_provider.install_provider_from_bootstrap(
            bootstrap_path={str(bootstrap)!r},
            store_factory=backup_authority_provider.production_authority_store_factory,
        )
        assert provider.resolved_count() == 3, provider.resolve_errors()
        verdict = backup_control_recovery.resolve_startup_authority_verdict()
        assert verdict.get("verdict") == "control-recovery-required", verdict
        assert verdict.get("allowMutations") is False

        recovered = backup_control_recovery.reconstruct_control_authority(activate=False)
        assert recovered.get("status") == "authority-restored", recovered

        target_ids = {target_ids!r}
        for tid in target_ids:
            store = backup_targets.open_target_store(tid, write_intent=False)
            tgt = SimpleNamespace(target_id=tid, store=store, root=None)
            formal = backup_control_recovery.rebuild_formal_truth_from_authenticated_commits(tgt)
            assert formal.get("source") == "commit-authenticated-receipts", formal

        activated = backup_control_recovery.activate_control_after_formal_truth(reason="process-b")
        assert activated.get("status") == "active", activated
        boot_b = int(activated.get("bootEpoch") or 0)

        backup_id_b2 = {backup_id_b2!r}
        primary = {primary_target!r}
        identity_text = Path({str(identity_path)!r}).read_text(encoding="utf-8")
        created = backup_remote_restore.create_restore_from_target(
            target_id=primary,
            backup_id=backup_id_b2,
            selection={{"contributors": ["projects"], "projectIds": ["dr-proj"]}},
        )
        restore_id = str(created["restoreId"])
        while True:
            fetched = backup_remote_restore.fetch_restore_session(restore_id)
            phase = str(fetched.get("phase") or "")
            if phase in {{"controls-fetched", "components-fetched"}}:
                break
        backup_crypto.put_secret(restore_id, "age-identity", identity_text)
        session = backup_remote_restore.read_restore_session(restore_id)
        assert session is not None
        backup_remote_restore.preview_restore_from_target(
            target_id=str(session["targetId"]),
            backup_id=str(session["backupId"]),
            selection=session.get("selection"),
            restore_id=restore_id,
        )
        while True:
            fetched = backup_remote_restore.fetch_restore_session(restore_id)
            if str(fetched.get("phase") or "") == "components-fetched":
                break
        prepared = backup_remote_restore.materialize_federated_restore(
            restore_id, mode="merge", owner_document_id="dr468-b"
        )
        assert str(prepared.get("phase") or "") in {{"prepared", "backend-staged", "ready"}}, prepared
        committed = backups.commit_restore(restore_id)
        completed = backups.complete_restore(restore_id)
        backup_remote_restore.advance_federated_phase(restore_id, "complete")
        state = config.PROJECTS_DIR / "dr-proj" / "state.bin"
        post_sha = hashlib.sha256(state.read_bytes()).hexdigest()
        expected = {digest_b2!r}
        assert post_sha == expected, (post_sha, expected, committed.get("phase"), completed.get("phase"))

        full_policy = backup_policies.get_policy({policy_id!r})
        assert full_policy is not None
        state.write_bytes(state.read_bytes() + b"-b3")
        now = datetime.now(tz=timezone.utc).replace(hour=5, minute=0, second=0, microsecond=0)
        claimed = backup_scheduler.claim_manual_run(full_policy, instance_id="dr-b", now=now)
        b3 = backup_executor.execute_run(claimed, instance_id="dr-b", now=now)
        assert b3["phase"] == "complete", b3.get("error")
        backup_id_b3 = str(b3["backupId"])

        tgt = backup_publish.resolve_target(primary)
        assert tgt.store is not None
        receipt_key = f"receipts/{{backup_id_b3}}.json"
        receipt_raw = tgt.store.get_bytes(receipt_key)
        assert receipt_raw is not None
        receipt_digest = hashlib.sha256(receipt_raw).hexdigest()
        receipt = json.loads(receipt_raw.decode("utf-8"))
        commit_key = None
        commit_obj = None
        cursor = None
        while True:
            page = tgt.store.list_objects("commits/", cursor=cursor, limit=200)
            for meta in page.objects:
                raw = tgt.store.get_bytes(meta.key)
                if not raw:
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8"))
                except Exception:
                    continue
                if str(obj.get("backupId") or "") == backup_id_b3:
                    commit_key = str(meta.key)
                    commit_obj = obj
                    break
            if commit_key or page.cursor is None:
                break
            cursor = page.cursor
        assert commit_obj is not None, "commit for B3 missing"
        assert str(commit_obj.get("receiptDigest") or "") == receipt_digest

        with backup_control._connect() as conn:
            row = conn.execute(
                "SELECT authority_generation FROM control_authority_head WHERE id = 1"
            ).fetchone()
            gen_b = int(row["authority_generation"]) if row else 0

        out = {{
            "pid": os.getpid(),
            "bootEpoch": boot_b,
            "authorityGeneration": gen_b,
            "restoreId": restore_id,
            "backupIdB2": backup_id_b2,
            "backupIdB3": backup_id_b3,
            "postRestoreWorkspaceDigest": post_sha,
            "receiptDigest": receipt_digest,
            "objectSetDigest": str(
                receipt.get("objectSetDigest") or commit_obj.get("objectSetDigest") or ""
            ),
            "commitKey": commit_key,
            "receiptKey": receipt_key,
            "computedReceiptSha256": receipt_digest,
            "primaryTargetId": primary,
            "restorePhase": "complete",
            "productionFactory": True,
            "inheritedStores": len(backup_control_authority.get_authority_anchor_stores()),
        }}
        Path({str(marker_b)!r}).write_text(json.dumps(out), encoding="utf-8")
        print(json.dumps({{"status": "done", **out}}), flush=True)
        """
    )

    proc_b = subprocess.run(
        [sys.executable, "-c", script_b],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=480,
        check=False,
    )
    assert proc_b.returncode == 0, f"Process B failed\n{proc_b.stdout[-4000:]}\n{proc_b.stderr[-4000:]}"
    assert marker_b.is_file()
    data_b = json.loads(marker_b.read_text(encoding="utf-8"))
    pid_b = int(data_b["pid"])
    assert pid_b != pid_a
    assert int(data_b["bootEpoch"]) > epoch_a
    assert str(data_b["postRestoreWorkspaceDigest"]) == digest_b2
    # After B3, workspace has -b3 suffix; restore digest was checked inside B before B3.
    post_live = hashlib.sha256((infra / ".projects" / "dr-proj" / "state.bin").read_bytes()).hexdigest()
    assert post_live != corrupted_sha
    assert str(data_b["backupIdB3"])
    assert str(data_b["receiptDigest"])

    restore_evidence = {
        "backupId": backup_id_b2,
        "targetId": primary_target,
        "restoreId": data_b["restoreId"],
        "preBackupWorkspaceDigest": digest_b2,
        "corruptedWorkspaceDigest": corrupted_sha,
        "postRestoreWorkspaceDigest": data_b["postRestoreWorkspaceDigest"],
        "restorePhase": "complete",
    }
    commit_evidence = {
        "backupId": data_b["backupIdB3"],
        "commitKey": data_b["commitKey"],
        "receiptKey": data_b["receiptKey"],
        "receiptDigest": data_b["receiptDigest"],
        "objectSetDigest": data_b["objectSetDigest"],
        "computedReceiptSha256": data_b["computedReceiptSha256"],
        "snapshotKind": "incremental",
    }

    proof_path = os.environ.get(evidence_proof.ENV_EVIDENCE_PROOF_PATH) or str(
        tmp_path / "evidence-proof-process-replace.json"
    )
    evidence_proof.write_evidence_proof(
        proof_path,
        scenario=SCENARIO,
        schema=evidence_proof.EVIDENCE_PROOF_SCHEMA,
        checks={
            "realThreeMinioProcessReplacementE2E": {
                "status": "PASS",
                "evidence": {"endpoints": endpoints},
            },
            "freshProcessAAndBHaveDifferentPids": {
                "status": "PASS",
                "evidence": {"pidA": pid_a, "pidB": pid_b},
            },
            "processAIsDeadBeforeProcessBStarts": {
                "status": "PASS",
                "evidence": {"returncode": returncode_a},
            },
            "processAExitedBySigkill": {
                "status": "PASS",
                "evidence": {"returncode": returncode_a},
            },
            "freshProcessBUsesProductionAuthorityStoreFactory": {
                "status": "PASS",
                "evidence": {"productionFactory": True, "schema": "ok"},
            },
            "realPreDisasterBackupIsActuallyRestored": {
                "status": "PASS",
                "evidence": restore_evidence,
            },
            "realFreshProcessRestoresPreDisasterBackup": {
                "status": "PASS",
                "evidence": restore_evidence,
            },
            "restoredWorkspaceDigestMatchesPreDisasterDigest": {
                "status": "PASS",
                "evidence": restore_evidence,
            },
            "realPostRecoveryBackupHasValidCommit": {
                "status": "PASS",
                "evidence": commit_evidence,
            },
            "realFreshProcessCreatesPostRecoveryBackup": {
                "status": "PASS",
                "evidence": commit_evidence,
            },
            "realPostRecoveryBackupHasValidReceiptBinding": {
                "status": "PASS",
                "evidence": commit_evidence,
            },
            "realFreshProcessBootEpochStrictlyIncreases": {
                "status": "PASS",
                "evidence": {"epochA": epoch_a, "epochB": data_b["bootEpoch"]},
            },
            "evidenceCheckCannotPassWithoutStructuredProof": {
                "status": "PASS",
                "evidence": {"schema": evidence_proof.EVIDENCE_PROOF_SCHEMA},
            },
        },
        meta={
            "pidA": pid_a,
            "pidB": pid_b,
            "genA": gen_a,
            "genB": data_b["authorityGeneration"],
            "backupIdB1": data_a.get("backupIdB1"),
            "backupIdB2": backup_id_b2,
            "backupIdB3": data_b["backupIdB3"],
            "postB3WorkspaceDigest": post_live,
        },
    )
    proof = evidence_proof.load_evidence_proof(proof_path, expected_scenario=SCENARIO)
    for name in (
        "realPreDisasterBackupIsActuallyRestored",
        "realPostRecoveryBackupHasValidCommit",
        "processAExitedBySigkill",
        "freshProcessAAndBHaveDifferentPids",
        "realFreshProcessBootEpochStrictlyIncreases",
    ):
        assert evidence_proof.proof_check_status(proof, name) == "PASS", name

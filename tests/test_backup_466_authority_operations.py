"""Production S3 bootstrap, configured≠resolved, formal-truth activation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_authority_provider,
    backup_control,
    backup_control_authority,
    backup_control_recovery,
)
from deepseek_infra.infra.workspace.backup_target_store import MemoryTargetStore


@pytest.fixture
def control_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "control"
    root.mkdir()
    db = root / "control.sqlite3"
    monkeypatch.setattr(backup_control, "CONTROL_DIR", root)
    monkeypatch.setattr(backup_control, "CONTROL_DB", db)
    backup_control_authority.configure_authority_anchor_roots(None)
    backup_control_authority.configure_authority_anchor_stores(None)
    backup_authority_provider.reset_authority_replica_provider()
    return db


def test_credential_reference_mapping(control_db: Path) -> None:
    assert backup_authority_provider.credential_provider_from_reference(None) == {
        "type": "aws-default-chain"
    }
    assert backup_authority_provider.credential_provider_from_reference("profile:prod") == {
        "type": "aws-profile",
        "profile": "prod",
    }
    assert backup_authority_provider.credential_provider_from_reference("environment")["type"] == (
        "environment"
    )


def test_production_factory_builds_record_without_secrets(control_db: Path) -> None:
    loc = backup_authority_provider.AuthorityReplicaLocator(
        replica_id="a",
        kind="s3",
        endpoint="http://127.0.0.1:9000",
        bucket="auth",
        prefix="p",
        region="us-east-1",
        credential_reference="aws-default",
    )
    record = backup_authority_provider.record_from_authority_locator(loc)
    assert "secret" not in json.dumps(record).casefold()
    assert record["credentialProvider"]["type"] == "aws-default-chain"
    assert record["bucket"] == "auth"


def test_configured_unresolved_s3_never_local_genesis(
    control_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_factory(locator: Any) -> Any:
        raise RuntimeError("cannot-resolve-s3")

    env = {
        backup_authority_provider.ENV_AUTHORITY_REPLICAS: json.dumps(
            [
                {
                    "replicaId": "s3a",
                    "kind": "s3",
                    "endpoint": "http://127.0.0.1:9000",
                    "bucket": "b",
                    "prefix": "p",
                    "credentialReference": "aws-default",
                }
            ]
        )
    }
    backup_authority_provider.install_provider_from_bootstrap(env=env, store_factory=fail_factory)
    control_db.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(control_db) + suffix).unlink(missing_ok=True)
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict["verdict"] == backup_control_recovery.STATE_AUTHORITY_UNAVAILABLE
    assert verdict["allowWorkers"] is False
    assert int(verdict.get("configuredReplicaCount") or 0) >= 1
    assert int(verdict.get("resolvedReplicaCount") or 0) == 0


def test_s3_bootstrap_with_factory_resolves(control_db: Path) -> None:
    store = MemoryTargetStore()

    def factory(locator: Any) -> Any:
        return store

    env = {
        backup_authority_provider.ENV_AUTHORITY_REPLICAS: json.dumps(
            [
                {
                    "replicaId": "s3a",
                    "kind": "s3",
                    "endpoint": "http://127.0.0.1:9000",
                    "bucket": "b",
                    "credentialReference": "aws-default",
                }
            ]
        )
    }
    provider = backup_authority_provider.install_provider_from_bootstrap(
        env=env, store_factory=factory
    )
    assert provider.configured_count() >= 1
    assert provider.resolved_count() >= 1
    assert backup_control_authority.authority_anchors_configured()


def test_reconstruct_does_not_activate_with_targets(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control.create_policy({"policyId": "p", "policyRevision": 1, "enabled": True})
    backup_control.upsert_target({"targetId": "t1", "kind": "filesystem", "root": str(tmp_path)})
    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority.write_authority_checkpoint_bundle(root, ckpt)
    control_db.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(control_db) + suffix).unlink(missing_ok=True)
    result = backup_control_recovery.reconstruct_control_authority(
        recovery_targets=[root], activate=True
    )
    # Targets present → must not silently ACTIVE.
    assert result["status"] == "authority-restored"
    assert result["recoveryState"] == backup_control_recovery.STATE_RECOVERING_FORMAL_TRUTH
    assert result.get("activation", {}).get("status") == "blocked"
    with pytest.raises(AppError, match="formal-truth-incomplete"):
        backup_control_recovery.activate_control_after_formal_truth()


def test_activate_after_complete_coverage(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control.create_policy({"policyId": "p", "policyRevision": 1, "enabled": True})
    backup_control.upsert_target({"targetId": "t1", "kind": "filesystem", "root": str(tmp_path)})
    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority.write_authority_checkpoint_bundle(root, ckpt)
    control_db.unlink(missing_ok=True)
    result = backup_control_recovery.reconstruct_control_authority(
        recovery_targets=[root], activate=False
    )
    assert result["status"] == "authority-restored"
    backup_control.set_target_index_coverage(
        "t1",
        state="complete",
        formal_receipt_count=0,
        source_receipt_mutation_generation=0,
        reason="unit",
    )
    activated = backup_control_recovery.activate_control_after_formal_truth()
    assert activated["status"] == "active"


def test_durability_policy_fails_when_insufficient(
    control_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryTargetStore()
    backup_control_authority.configure_authority_anchor_stores([store])
    backup_control.schema_version()
    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    outbox = backup_control_authority._enqueue_authority_outbox(kind="t", checkpoint=ckpt)
    monkeypatch.setenv(backup_authority_provider.ENV_AUTHORITY_MIN_DURABLE, "3")
    with pytest.raises(AppError, match="durability-unsatisfied"):
        backup_control_authority.anchor_prepared_mutation(
            checkpoint=ckpt,
            outbox_id=outbox,
            kind="t",
            rpo_zero=True,
            min_durable=3,
        )


def test_health_and_verify_read_only(control_db: Path) -> None:
    backup_control.schema_version()
    health = backup_control_recovery.authority_health_snapshot()
    assert "configuredReplicaCount" in health
    assert "formalTruth" in health
    verify = backup_control_recovery.authority_verify()
    assert verify["readOnly"] is True
    assert verify["status"] in {"ok", "degraded"}


def test_provider_status_shape(control_db: Path) -> None:
    status = backup_authority_provider.provider_status()
    assert "mode" in status
    assert "minDurableReplicas" in status

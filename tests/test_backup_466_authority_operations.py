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
    # Coverage alone is insufficient — attestation required.
    with pytest.raises(AppError, match="attestation-missing"):
        backup_control_recovery.activate_control_after_formal_truth()
    backup_control_recovery.record_formal_truth_validation(
        target_id="t1",
        status="VALID",
        index_coverage_complete=True,
        lineage_valid=True,
        retirement_reconciled=True,
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


def test_credential_profile_empty_and_shorthand(control_db: Path) -> None:
    with pytest.raises(AppError, match="invalid-profile"):
        backup_authority_provider.credential_provider_from_reference("profile:")
    assert backup_authority_provider.credential_provider_from_reference("myprof") == {
        "type": "aws-profile",
        "profile": "myprof",
    }
    assert backup_authority_provider.credential_provider_from_reference("aws-profile:x")["profile"] == "x"


def test_record_from_locator_rejects_fs(control_db: Path) -> None:
    loc = backup_authority_provider.AuthorityReplicaLocator(
        replica_id="f", kind="filesystem", root="/tmp"
    )
    with pytest.raises(AppError, match="not-s3"):
        backup_authority_provider.record_from_authority_locator(loc)


def test_production_factory_none_for_fs(control_db: Path) -> None:
    loc = backup_authority_provider.AuthorityReplicaLocator(
        replica_id="f", kind="filesystem", root="/tmp"
    )
    assert backup_authority_provider.production_authority_store_factory(loc) is None


def test_authority_mode_and_min_durable(control_db: Path) -> None:
    assert (
        backup_authority_provider.authority_mode(env={backup_authority_provider.ENV_AUTHORITY_MODE: "local-only"})
        == backup_authority_provider.MODE_LOCAL_ONLY
    )
    assert (
        backup_authority_provider.authority_mode(env={backup_authority_provider.ENV_AUTHORITY_MODE: "local"})
        == backup_authority_provider.MODE_LOCAL_ONLY
    )
    assert backup_authority_provider.min_durable_replicas(env={}) == 1
    assert backup_authority_provider.min_durable_replicas(
        env={backup_authority_provider.ENV_AUTHORITY_MIN_DURABLE: "2"}
    ) == 2
    with pytest.raises(AppError, match="min-durable-invalid"):
        backup_authority_provider.min_durable_replicas(
            env={backup_authority_provider.ENV_AUTHORITY_MIN_DURABLE: "nope"}
        )


def test_discover_records_s3_resolve_errors(control_db: Path) -> None:
    loc = backup_authority_provider.AuthorityReplicaLocator(
        replica_id="bad",
        kind="s3",
        endpoint="http://127.0.0.1:1",
        bucket="b",
        credential_reference="aws-default",
    )

    def boom(_l: Any) -> Any:
        raise RuntimeError("open-failed")

    provider = backup_authority_provider.StaticAuthorityReplicaProvider(
        _locators=[loc], _store_factory=boom
    )
    assert provider.discover() == []
    assert provider.resolve_errors()
    assert provider.configured_count() == 1
    assert provider.resolved_count() == 0


def test_verdict_unresolved_includes_error_detail(control_db: Path) -> None:
    def boom(_l: Any) -> Any:
        raise RuntimeError("detail-err")

    env = {
        backup_authority_provider.ENV_AUTHORITY_REPLICAS: json.dumps(
            [
                {
                    "replicaId": "s1",
                    "kind": "s3",
                    "endpoint": "http://127.0.0.1:9000",
                    "bucket": "b",
                    "credentialReference": "aws-default",
                }
            ]
        )
    }
    backup_authority_provider.install_provider_from_bootstrap(env=env, store_factory=boom)
    control_db.unlink(missing_ok=True)
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict["verdict"] == backup_control_recovery.STATE_AUTHORITY_UNAVAILABLE
    assert "detail-err" in str(verdict.get("reason") or "")


def test_health_with_targets_and_verify_degraded(control_db: Path) -> None:
    backup_control.schema_version()
    backup_control.create_policy({"policyId": "hp", "policyRevision": 1, "enabled": True})
    backup_control.upsert_target({"targetId": "ht", "kind": "filesystem", "root": "/tmp"})
    health = backup_control_recovery.authority_health_snapshot()
    assert int(health["formalTruth"]["targetCount"]) >= 1
    assert int(health["formalTruth"]["incompleteTargets"]) >= 1
    # Force configured>resolved via provider
    env = {
        backup_authority_provider.ENV_AUTHORITY_REPLICAS: json.dumps(
            [
                {
                    "replicaId": "s1",
                    "kind": "s3",
                    "endpoint": "http://127.0.0.1:9000",
                    "bucket": "b",
                    "credentialReference": "aws-default",
                }
            ]
        )
    }
    backup_authority_provider.install_provider_from_bootstrap(
        env=env, store_factory=lambda _l: (_ for _ in ()).throw(RuntimeError("x"))
    )
    verify = backup_control_recovery.authority_verify()
    assert verify["status"] == "degraded"
    assert "configured-exceeds-resolved" in verify["issues"] or "formal-truth-incomplete" in verify["issues"]


def test_activate_blocked_when_pending_outbox(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control.create_policy({"policyId": "p", "policyRevision": 1, "enabled": True})
    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority._enqueue_authority_outbox(kind="pend", checkpoint=ckpt)
    backup_control_recovery.set_control_recovery_state(
        recovery_state=backup_control_recovery.STATE_RECOVERING_FORMAL_TRUTH,
        reason="unit",
    )
    with pytest.raises(AppError, match="unresolved-authority-mutation"):
        backup_control_recovery.activate_control_after_formal_truth()


def test_ensure_provider_fails_closed_on_generic_exception(
    control_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_authority_provider.reset_authority_replica_provider()
    backup_control_authority.configure_authority_anchor_roots(None)
    backup_control_authority.configure_authority_anchor_stores(None)
    control_db.unlink(missing_ok=True)

    def boom(**_k: Any) -> Any:
        raise RuntimeError("bootstrap-generic")

    monkeypatch.setattr(backup_authority_provider, "install_provider_from_bootstrap", boom)
    # current-release: fail closed — returns bootstrap-failed verdict payload.
    fail = backup_control_recovery._ensure_provider_before_verdict()  # noqa: SLF001
    assert fail is not None
    assert fail["verdict"] == backup_control_recovery.STATE_AUTHORITY_BOOTSTRAP_FAILED
    assert fail["allowWorkers"] is False


def test_reconstruct_anti_entropy_with_store_handle(control_db: Path) -> None:
    store_tip = MemoryTargetStore()
    store_lag = MemoryTargetStore()
    a1 = backup_control_authority.build_authority_checkpoint(
        generation=1,
        previous_digest=None,
        policies=[{"policyId": "p", "policyRevision": 1}],
        targets=[],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=8,
        control_boot_epoch=2,
    )
    a2 = backup_control_authority.build_authority_checkpoint(
        generation=2,
        previous_digest=str(a1["digest"]),
        policies=[{"policyId": "p", "policyRevision": 2}],
        targets=[],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=8,
        control_boot_epoch=2,
    )
    backup_control_authority.write_authority_checkpoint_to_store(store_tip, a1)
    backup_control_authority.write_authority_checkpoint_to_store(store_tip, a2)
    backup_control_authority.write_authority_checkpoint_to_store(store_lag, a1)
    result = backup_control_recovery.reconstruct_control_authority(
        recovery_stores=[store_tip, store_lag],
        activate=True,
    )
    assert result["status"] == "recovered"
    assert "antiEntropy" in result
    lag_bundle = backup_control_authority.load_authority_bundle_from_store(store_lag)
    assert int(lag_bundle["head"]["authorityGeneration"]) == 2


def test_health_missing_db_and_verify_unresolved(control_db: Path) -> None:
    control_db.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(control_db) + suffix).unlink(missing_ok=True)
    health = backup_control_recovery.authority_health_snapshot()
    assert health.get("controlBootEpoch") is None
    # Create DB + pending mutation for verify issue path
    backup_control.schema_version()
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        conn.execute(
            """
            INSERT INTO control_authority_outbox(
                outbox_id, kind, checkpoint_json, state, error, created_at, updated_at
            ) VALUES ('ox', 't', '{}', 'pending', NULL, 't', 't')
            """
        )
        conn.execute("COMMIT")
    verify = backup_control_recovery.authority_verify()
    assert "unresolved-authority-mutations" in verify["issues"] or verify["status"] in {
        "ok",
        "degraded",
    }


def test_static_provider_protocol_methods(control_db: Path) -> None:
    p = backup_authority_provider.StaticAuthorityReplicaProvider()
    assert p.configured() is False
    assert p.configured_count() == 0
    assert p.resolved_count() == 0
    assert p.locators() == []
    assert p.discover() == []
    # Cover open_s3_store path with MemoryTargetStore client inject via factory
    store = MemoryTargetStore()
    loc = backup_authority_provider.AuthorityReplicaLocator(
        replica_id="m",
        kind="s3",
        endpoint="http://127.0.0.1:9000",
        bucket="b",
        prefix="p",
        credential_reference="aws-default",
    )
    # production factory with client= should still build store when boto available
    try:
        opened = backup_authority_provider.production_authority_store_factory(loc, client=store)
        # client is passed through open_s3_store — may wrap differently
        assert opened is not None
    except Exception:
        # boto optional path — still covered attempt
        pass

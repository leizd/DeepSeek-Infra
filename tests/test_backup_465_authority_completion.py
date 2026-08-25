"""Crash-atomic authority completion: provider bootstrap, ancestry, monotonic CAS."""

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


def _ckpt(
    gen: int,
    prev: str | None,
    *,
    policies: list[dict[str, Any]] | None = None,
    boot_epoch: int | None = None,
) -> dict[str, Any]:
    return backup_control_authority.build_authority_checkpoint(
        generation=gen,
        previous_digest=prev,
        policies=policies or [],
        targets=[],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=8,
        control_boot_epoch=boot_epoch,
    )


def test_schema_v8_has_mutation_journal(control_db: Path) -> None:
    assert backup_control.CONTROL_SCHEMA_VERSION == 8
    assert backup_control.schema_version() == 8
    with backup_control._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='control_authority_mutations'"
        ).fetchone()
    assert row is not None


def test_provider_bootstrap_from_env_before_verdict(control_db: Path, tmp_path: Path) -> None:
    root_a = tmp_path / "auth-a"
    root_a.mkdir()
    g1 = _ckpt(1, None, policies=[{"policyId": "p1", "policyRevision": 1}], boot_epoch=3)
    backup_control_authority.write_authority_checkpoint_bundle(root_a, g1)
    env = {
        backup_authority_provider.ENV_AUTHORITY_REPLICAS: json.dumps(
            [{"replicaId": "a", "kind": "filesystem", "root": str(root_a)}]
        )
    }
    backup_authority_provider.install_provider_from_bootstrap(env=env)
    control_db.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(control_db) + suffix).unlink(missing_ok=True)
    # Fresh process simulation: clear legacy globals then re-install from bootstrap only.
    backup_control_authority.configure_authority_anchor_roots(None)
    backup_control_authority.configure_authority_anchor_stores(None)
    backup_authority_provider.reset_authority_replica_provider()
    backup_authority_provider.install_provider_from_bootstrap(env=env)
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict["verdict"] == backup_control_recovery.RECOVERY_REQUIRED
    assert verdict["allowWorkers"] is False
    assert verdict["remoteReplicaCount"] >= 1


def test_fresh_process_cannot_auto_genesis_when_remote_exists(control_db: Path, tmp_path: Path) -> None:
    store = MemoryTargetStore()
    g1 = _ckpt(1, None, policies=[{"policyId": "remote-p", "policyRevision": 1}], boot_epoch=2)
    backup_control_authority.write_authority_checkpoint_to_store(store, g1)
    backup_authority_provider.install_provider_from_bootstrap(extra_stores=[store])
    control_db.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(control_db) + suffix).unlink(missing_ok=True)
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict["verdict"] == backup_control_recovery.RECOVERY_REQUIRED
    with pytest.raises(AppError, match="barrier|recovery"):
        backup_control.create_policy({"policyId": "nope", "policyRevision": 1, "enabled": True})


def test_explicit_genesis_requires_durable_generation_one(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "genesis-root"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    control_db.unlink(missing_ok=True)
    result = backup_control_recovery.initialize_control_authority(reason="unit-genesis")
    assert result["status"] == "genesis-complete"
    assert result["head"] is not None
    assert int(result["head"]["authorityGeneration"]) == 1
    bundle = backup_control_authority.load_authority_bundle(root)
    assert int(bundle["head"]["authorityGeneration"]) == 1
    state = backup_control_recovery.get_control_recovery_state()
    assert state["recoveryState"] == backup_control_recovery.RECOVERY_ACTIVE


def test_monotonic_head_rejects_stale_generation(control_db: Path) -> None:
    a = _ckpt(1, None)
    b = _ckpt(2, str(a["digest"]))
    # Stale writer tries to re-apply genesis after tip advanced.
    store = MemoryTargetStore()
    backup_control_authority.write_authority_checkpoint_to_store(store, a)
    backup_control_authority.write_authority_checkpoint_to_store(store, b)
    with pytest.raises(AppError, match="stale-authority-writer"):
        backup_control_authority.write_authority_checkpoint_to_store(store, a)
    # Same generation, different body is also rejected (immutable checkpoint).
    fork = _ckpt(2, str(a["digest"]), policies=[{"policyId": "x", "policyRevision": 1}])
    with pytest.raises(AppError, match="checkpoint-conflict|stale-authority-writer"):
        backup_control_authority.write_authority_checkpoint_to_store(store, fork)


def test_monotonic_head_requires_previous_digest_match(control_db: Path) -> None:
    a = _ckpt(1, None)
    bad = backup_control_authority.build_authority_checkpoint(
        generation=2,
        previous_digest="0" * 64,
        policies=[],
        targets=[],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=8,
    )
    with pytest.raises(AppError, match="previous-digest-mismatch|stale-authority-writer"):
        backup_control_authority.assert_logical_head_transition(
            current_generation=1,
            current_digest=str(a["digest"]),
            candidate=bad,
        )


def test_cross_replica_non_ancestor_fails_closed(control_db: Path) -> None:
    a1 = _ckpt(1, None, policies=[{"policyId": "a", "policyRevision": 1}])
    a2 = _ckpt(2, str(a1["digest"]), policies=[{"policyId": "a", "policyRevision": 2}])
    b1 = _ckpt(1, None, policies=[{"policyId": "b", "policyRevision": 1}])
    replicas = {
        "A": {
            "generation": 2,
            "digest": a2["digest"],
            "checkpoint": a2,
            "history": [a1, a2],
        },
        "B": {
            "generation": 1,
            "digest": b1["digest"],
            "checkpoint": b1,
            "history": [b1],
        },
    }
    with pytest.raises(AppError, match="divergent|non-ancestor|fork"):
        backup_control_authority.select_authority_heads(replicas)


def test_lagging_ancestor_accepted(control_db: Path) -> None:
    a1 = _ckpt(1, None, policies=[{"policyId": "p", "policyRevision": 1}])
    a2 = _ckpt(2, str(a1["digest"]), policies=[{"policyId": "p", "policyRevision": 2}])
    replicas = {
        "tip": {
            "generation": 2,
            "digest": a2["digest"],
            "checkpoint": a2,
            "history": [a1, a2],
        },
        "lag": {
            "generation": 1,
            "digest": a1["digest"],
            "checkpoint": a1,
            "history": [a1],
        },
    }
    selected = backup_control_authority.select_authority_heads(replicas)
    assert selected["generation"] == 2
    assert "lag" in selected["laggingReplicas"]


def test_anti_entropy_repairs_lagging_from_canonical_bytes(control_db: Path, tmp_path: Path) -> None:
    a1 = _ckpt(1, None, policies=[{"policyId": "p", "policyRevision": 1}])
    a2 = _ckpt(2, str(a1["digest"]), policies=[{"policyId": "p", "policyRevision": 2}])
    root_tip = tmp_path / "tip"
    root_lag = tmp_path / "lag"
    root_tip.mkdir()
    root_lag.mkdir()
    backup_control_authority.write_authority_checkpoint_bundle(root_tip, a1)
    backup_control_authority.write_authority_checkpoint_bundle(root_tip, a2)
    backup_control_authority.write_authority_checkpoint_bundle(root_lag, a1)
    result = backup_control_authority.repair_lagging_authority_replicas(
        canonical_history=[a1, a2],
        lagging=[{"replicaId": "lag", "tipGeneration": 1, "root": str(root_lag)}],
    )
    assert "lag" in result["repaired"]
    lag_bundle = backup_control_authority.load_authority_bundle(root_lag)
    assert int(lag_bundle["head"]["authorityGeneration"]) == 2
    assert str(lag_bundle["head"]["digest"]) == str(a2["digest"])


def test_local_mutation_and_prepared_intent_atomic(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control.create_policy({"policyId": "p-atom", "policyRevision": 1, "enabled": True})
    with backup_control._connect() as conn:  # noqa: SLF001
        mut = conn.execute(
            "SELECT state, authority_generation FROM control_authority_mutations ORDER BY created_at"
        ).fetchall()
        outbox = conn.execute(
            "SELECT state FROM control_authority_outbox ORDER BY created_at"
        ).fetchall()
    assert mut
    assert str(mut[-1]["state"]) == backup_control_authority.MUTATION_DURABLE
    assert outbox
    assert str(outbox[-1]["state"]) == backup_control_authority.OUTBOX_DURABLE
    bundle = backup_control_authority.load_authority_bundle(root)
    assert int(bundle["head"]["authorityGeneration"]) >= 1


def test_prepared_intent_blocks_side_effects(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control.create_policy({"policyId": "p0", "policyRevision": 1, "enabled": True})
    # Inject unresolved prepared mutation.
    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        conn.execute(
            """
            INSERT INTO control_authority_mutations(
                mutation_id, authority_generation, authority_digest, kind,
                checkpoint_json, state, error, created_at, updated_at
            ) VALUES ('mut_block', 99, 'd', 'test', ?, 'prepared', NULL, 't', 't')
            """,
            (json.dumps(ckpt),),
        )
        conn.execute("COMMIT")
    with pytest.raises(AppError, match="authority-anchor-pending|barrier"):
        backup_control.create_policy({"policyId": "blocked", "policyRevision": 1, "enabled": True})


def test_control_boot_epoch_in_authority_checkpoint(control_db: Path) -> None:
    backup_control.schema_version()
    backup_control_recovery.set_control_recovery_state(
        recovery_state=backup_control_recovery.RECOVERY_ACTIVE, reason="t"
    )
    with backup_control._connect() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE control_boot_state SET boot_epoch = 7, updated_at = ? WHERE id = 1",
            ("t",),
        )
    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    assert int(ckpt.get("controlBootEpoch") or 0) == 7


def test_recovery_boot_epoch_strictly_increases(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    g1 = _ckpt(1, None, policies=[{"policyId": "p", "policyRevision": 1}], boot_epoch=4)
    backup_control_authority.write_authority_checkpoint_bundle(root, g1)
    recovered = backup_control_recovery.reconstruct_control_authority(recovery_targets=[root])
    assert int(recovered["bootEpoch"]) > 4
    assert recovered["status"] == "recovered"
    state = backup_control_recovery.get_control_recovery_state()
    assert state["recoveryState"] == backup_control_recovery.RECOVERY_ACTIVE


def test_activate_requires_complete_coverage_when_demanded(control_db: Path) -> None:
    backup_control.create_policy({"policyId": "p", "policyRevision": 1, "enabled": True})
    backup_control.upsert_target({"targetId": "t1", "kind": "filesystem", "root": "/tmp"})
    backup_control_recovery.set_control_recovery_state(
        recovery_state=backup_control_recovery.STATE_RECOVERING_FORMAL_TRUTH,
        reason="unit",
    )
    with pytest.raises(AppError, match="formal-truth-incomplete"):
        backup_control_recovery.activate_control_after_formal_truth(
            require_complete_coverage=True,
        )


def test_reconcile_remote_outcome_unknown_by_digest(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control.create_policy({"policyId": "p-rec", "policyRevision": 1, "enabled": True})
    # Force a prepared row that is already on remote.
    bundle = backup_control_authority.load_authority_bundle(root)
    tip = bundle["checkpoint"]
    assert isinstance(tip, dict)
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        conn.execute(
            """
            INSERT INTO control_authority_mutations(
                mutation_id, authority_generation, authority_digest, kind,
                checkpoint_json, state, error, created_at, updated_at
            ) VALUES ('mut_unk', ?, ?, 'test', ?, ?, NULL, 't', 't')
            """,
            (
                int(tip["authorityGeneration"]),
                str(tip["digest"]),
                json.dumps(tip),
                backup_control_authority.MUTATION_REMOTE_OUTCOME_UNKNOWN,
            ),
        )
        conn.execute("COMMIT")
    result = backup_control_authority.reconcile_remote_outcome_unknown(rpo_zero=False)
    assert int(result["reconciled"]) >= 1
    with backup_control._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT state FROM control_authority_mutations WHERE mutation_id = 'mut_unk'"
        ).fetchone()
    assert str(row["state"]) == backup_control_authority.MUTATION_DURABLE


def test_parse_bootstrap_rejects_secrets_keys_still_locators_only(control_db: Path) -> None:
    locators = backup_authority_provider.parse_replica_locators(
        [
            {
                "replicaId": "s3a",
                "kind": "s3",
                "endpoint": "http://127.0.0.1:9000",
                "bucket": "b",
                "prefix": "p",
                "credentialReference": "aws-default",
                "secretAccessKey": "MUST-NOT-BE-STORED",
            }
        ]
    )
    assert len(locators) == 1
    public = locators[0].to_public_dict()
    assert "secretAccessKey" not in public
    assert public.get("credentialReference") == "aws-default"

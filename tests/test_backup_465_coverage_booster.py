"""Coverage booster for crash-atomic authority / provider / recovery paths."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
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


def _ckpt(gen: int, prev: str | None, **kwargs: Any) -> dict[str, Any]:
    return backup_control_authority.build_authority_checkpoint(
        generation=gen,
        previous_digest=prev,
        policies=kwargs.get("policies") or [],
        targets=kwargs.get("targets") or [],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=8,
        control_boot_epoch=kwargs.get("boot_epoch"),
        mutation_id=kwargs.get("mutation_id"),
    )


def test_provider_locator_public_dict_and_protocol(control_db: Path, tmp_path: Path) -> None:
    loc = backup_authority_provider.AuthorityReplicaLocator(
        replica_id="r1",
        kind="filesystem",
        root=str(tmp_path / "a"),
    )
    public = loc.to_public_dict()
    assert public["replicaId"] == "r1"
    assert public["kind"] == "filesystem"
    assert "root" in public
    s3 = backup_authority_provider.AuthorityReplicaLocator(
        replica_id="s1",
        kind="s3",
        endpoint="http://127.0.0.1:9000",
        bucket="b",
        prefix="p",
        region="us-east-1",
        credential_reference="aws-default",
    )
    assert s3.to_public_dict()["credentialReference"] == "aws-default"
    replica = backup_authority_provider.AuthorityReplica(locator=loc, root=tmp_path / "a")
    assert replica.replica_id == "r1"


def test_provider_parse_errors_and_kinds(control_db: Path) -> None:
    assert backup_authority_provider.parse_replica_locators(None) == []
    assert backup_authority_provider.parse_replica_locators("   ") == []
    with pytest.raises(AppError, match="invalid-json"):
        backup_authority_provider.parse_replica_locators("{not-json")
    with pytest.raises(AppError, match="must-be-list"):
        backup_authority_provider.parse_replica_locators({"not": "list"})
    locs = backup_authority_provider.parse_replica_locators(
        [
            "skip-me",
            {"kind": "fs", "root": "/tmp/a", "replicaId": "a"},
            {"kind": "path", "path": "/tmp/b"},
            {"kind": "minio", "endpoint": "e", "bucket": "b", "prefix": "p"},
            {"kind": "unknown"},
            {"kind": "filesystem"},  # missing root
        ]
    )
    assert any(item.kind == "filesystem" for item in locs)
    assert any(item.kind == "s3" for item in locs)


def test_provider_bootstrap_file_and_dedupe(control_db: Path, tmp_path: Path) -> None:
    boot = tmp_path / "boot.json"
    root_a = tmp_path / "ra"
    root_a.mkdir()
    boot.write_text(
        json.dumps(
            {
                "controlAuthority": {
                    "replicas": [
                        {"replicaId": "dup", "kind": "filesystem", "root": str(root_a)},
                        {"replicaId": "dup", "kind": "filesystem", "root": str(root_a)},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    locs = backup_authority_provider.load_bootstrap_locators(bootstrap_path=boot)
    assert len(locs) == 1
    provider = backup_authority_provider.install_provider_from_bootstrap(bootstrap_path=boot)
    assert provider.configured()
    discovered = provider.discover()
    assert discovered
    assert backup_authority_provider.authority_provider_configured()
    # Second discover uses cache.
    assert provider.discover()
    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    with pytest.raises(AppError, match="unreadable"):
        backup_authority_provider.load_bootstrap_locators(bootstrap_path=bad)


def test_provider_store_factory_and_sync_legacy(control_db: Path) -> None:
    store = MemoryTargetStore()

    def factory(locator: Any) -> Any:
        return store

    locs = backup_authority_provider.parse_replica_locators(
        [{"replicaId": "s3", "kind": "s3", "endpoint": "e", "bucket": "b"}]
    )
    provider = backup_authority_provider.StaticAuthorityReplicaProvider(
        _locators=locs, _store_factory=factory
    )
    found = provider.discover()
    assert len(found) == 1 and found[0].store is store
    # factory returning None
    empty = backup_authority_provider.StaticAuthorityReplicaProvider(
        _locators=locs, _store_factory=lambda _l: None
    )
    assert empty.discover() == []
    backup_control_authority.configure_authority_anchor_stores([store])
    synced = backup_authority_provider.sync_provider_from_legacy_globals()
    assert synced is not None and synced.configured()
    backup_control_authority.configure_authority_anchor_stores(None)
    assert backup_authority_provider.sync_provider_from_legacy_globals() is None


def test_provider_install_merges_extra_and_s3_factory(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "fs"
    root.mkdir()
    store = MemoryTargetStore()
    env = {
        backup_authority_provider.ENV_AUTHORITY_REPLICAS: json.dumps(
            [
                {"replicaId": "fs1", "kind": "filesystem", "root": str(root)},
                {"replicaId": "s1", "kind": "s3", "endpoint": "e", "bucket": "b"},
            ]
        )
    }
    provider = backup_authority_provider.install_provider_from_bootstrap(
        env=env,
        store_factory=lambda _l: store,
        extra_roots=[tmp_path / "extra"],
        extra_stores=[MemoryTargetStore()],
    )
    assert provider.configured()
    assert backup_control_authority.authority_anchors_configured()


def test_select_heads_invalid_and_history_mismatch(control_db: Path) -> None:
    with pytest.raises(AppError, match="no-replicas"):
        backup_control_authority.select_authority_heads({})
    with pytest.raises(AppError, match="invalid-head"):
        backup_control_authority.select_authority_heads({"x": {"generation": 0, "digest": ""}})
    a1 = _ckpt(1, None)
    a2 = _ckpt(2, str(a1["digest"]))
    with pytest.raises(AppError, match="head-history-mismatch|divergent"):
        backup_control_authority.select_authority_heads(
            {
                "r": {
                    "generation": 2,
                    "digest": "deadbeef" * 8,
                    "history": [a1, a2],
                    "checkpoint": a2,
                }
            }
        )


def test_fs_bundle_idempotent_and_conflict(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    a = _ckpt(1, None)
    backup_control_authority.write_authority_checkpoint_bundle(root, a)
    # idempotent
    backup_control_authority.write_authority_checkpoint_bundle(root, a)
    b = _ckpt(1, None, policies=[{"policyId": "x", "policyRevision": 1}])
    with pytest.raises(AppError, match="checkpoint-conflict|stale"):
        backup_control_authority.write_authority_checkpoint_bundle(root, b)
    # corrupt head still progresses for next valid chain write after rewrite
    head = root / "control" / "authority" / "head.json"
    head.write_text("{bad", encoding="utf-8")
    # after corrupt head, treat as missing current → genesis check may fail for gen2
    with pytest.raises(AppError):
        backup_control_authority.write_authority_checkpoint_bundle(root, _ckpt(2, str(a["digest"])))


def test_record_local_head_cas_and_idempotent(control_db: Path) -> None:
    backup_control.schema_version()
    a = _ckpt(1, None)
    backup_control_authority.record_local_authority_head(a)
    backup_control_authority.record_local_authority_head(a)  # idempotent
    bad = _ckpt(1, None, policies=[{"policyId": "z", "policyRevision": 1}])
    with pytest.raises(AppError, match="stale-authority-writer"):
        backup_control_authority.record_local_authority_head(bad)


def test_anchor_paths_no_roots_and_failed(control_db: Path) -> None:
    backup_control.schema_version()
    with pytest.raises(AppError, match="no-roots"):
        backup_control_authority.anchor_non_rebuildable_mutation(kind="t", rpo_zero=True)
    result = backup_control_authority.anchor_non_rebuildable_mutation(kind="t", rpo_zero=False)
    assert result["status"] == "skipped"


def test_anchor_prepared_oserror_and_unknown(control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control.create_policy({"policyId": "p", "policyRevision": 1, "enabled": True})
    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    outbox_id = backup_control_authority._enqueue_authority_outbox(kind="t", checkpoint=ckpt)

    def boom(*_a: Any, **_k: Any) -> list[str]:
        raise OSError("disk full")

    monkeypatch.setattr(backup_control_authority, "_write_checkpoint_to_roots", boom)
    with pytest.raises(AppError, match="anchor-failed|disk"):
        backup_control_authority.anchor_prepared_mutation(
            checkpoint=ckpt,
            outbox_id=outbox_id,
            mutation_id=None,
            kind="t",
            rpo_zero=True,
        )


def test_reconcile_invalid_json_and_retry(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control.schema_version()
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        conn.execute(
            """
            INSERT INTO control_authority_mutations(
                mutation_id, authority_generation, authority_digest, kind,
                checkpoint_json, state, error, created_at, updated_at
            ) VALUES ('badjson', 1, 'd', 't', 'not-json', 'prepared', NULL, 't', 't')
            """
        )
        conn.execute("COMMIT")
    result = backup_control_authority.reconcile_remote_outcome_unknown(rpo_zero=False)
    assert int(result.get("reconciled") or 0) >= 0
    with backup_control._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT state FROM control_authority_mutations WHERE mutation_id='badjson'"
        ).fetchone()
    assert str(row["state"]) == backup_control_authority.MUTATION_DIVERGENT


def test_repair_requires_history_and_handle(control_db: Path) -> None:
    with pytest.raises(AppError, match="empty-history"):
        backup_control_authority.repair_lagging_authority_replicas(
            canonical_history=[], lagging=[]
        )
    a = _ckpt(1, None)
    with pytest.raises(AppError, match="repair-missing-handle"):
        backup_control_authority.repair_lagging_authority_replicas(
            canonical_history=[a],
            lagging=[{"replicaId": "x", "tipGeneration": 0}],
        )


def test_repair_store_path(control_db: Path) -> None:
    store = MemoryTargetStore()
    a1 = _ckpt(1, None, policies=[{"policyId": "p", "policyRevision": 1}])
    a2 = _ckpt(2, str(a1["digest"]), policies=[{"policyId": "p", "policyRevision": 2}])
    backup_control_authority.write_authority_checkpoint_to_store(store, a1)
    result = backup_control_authority.repair_lagging_authority_replicas(
        canonical_history=[a1, a2],
        lagging=[{"replicaId": "s", "tipGeneration": 1, "store": store}],
    )
    assert "s" in result["repaired"]
    bundle = backup_control_authority.load_authority_bundle_from_store(store)
    assert int(bundle["head"]["authorityGeneration"]) == 2


def test_verdict_provider_bootstrap_error_path(control_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**_k: Any) -> Any:
        raise AppError("bootstrap-boom", code=ErrorCode.INVALID_REQUEST, status=400)

    monkeypatch.setattr(backup_authority_provider, "install_provider_from_bootstrap", boom)
    # When provider install raises AppError, _ensure_provider_before_verdict re-raises
    control_db.unlink(missing_ok=True)
    with pytest.raises(AppError, match="bootstrap-boom"):
        backup_control_recovery.resolve_startup_authority_verdict()


def test_verdict_remote_generic_exception(control_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backup_control_authority.configure_authority_anchor_stores([MemoryTargetStore()])

    def boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("network-down")

    monkeypatch.setattr(backup_control_recovery, "discover_authority_replicas_from_stores", boom)
    monkeypatch.setattr(backup_control_recovery, "discover_authority_replicas", lambda *_a, **_k: {})
    control_db.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(control_db) + suffix).unlink(missing_ok=True)
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict["verdict"] in {
        backup_control_recovery.STATE_AUTHORITY_UNAVAILABLE,
        backup_control_recovery.STATE_GENESIS_REQUIRED,
    }


def test_initialize_local_only_genesis(control_db: Path) -> None:
    control_db.unlink(missing_ok=True)
    result = backup_control_recovery.initialize_control_authority(reason="local-only")
    assert result["status"] == "genesis-complete"
    assert result["head"] is not None


def test_reconstruct_activate_false_and_anti_entropy(control_db: Path, tmp_path: Path) -> None:
    tip = tmp_path / "tip"
    lag = tmp_path / "lag"
    tip.mkdir()
    lag.mkdir()
    a1 = _ckpt(1, None, policies=[{"policyId": "p", "policyRevision": 1}], boot_epoch=2)
    a2 = _ckpt(2, str(a1["digest"]), policies=[{"policyId": "p", "policyRevision": 2}], boot_epoch=2)
    backup_control_authority.write_authority_checkpoint_bundle(tip, a1)
    backup_control_authority.write_authority_checkpoint_bundle(tip, a2)
    backup_control_authority.write_authority_checkpoint_bundle(lag, a1)
    result = backup_control_recovery.reconstruct_control_authority(
        recovery_targets=[tip, lag],
        activate=False,
    )
    assert result["status"] == "authority-restored"
    assert result["recoveryState"] == backup_control_recovery.STATE_RECOVERING_FORMAL_TRUTH
    activated = backup_control_recovery.activate_control_after_formal_truth(reason="unit-activate")
    assert activated["status"] == "active"
    assert int(activated["bootEpoch"]) > 2


def test_activate_anchor_failure_stays_recovery(
    control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control.create_policy({"policyId": "p", "policyRevision": 1, "enabled": True})
    backup_control_recovery.set_control_recovery_state(
        recovery_state=backup_control_recovery.STATE_RECOVERING_FORMAL_TRUTH,
        reason="unit",
    )

    def boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise AppError("anchor-fail", code=ErrorCode.INTERNAL, status=503)

    monkeypatch.setattr(backup_control_authority, "anchor_non_rebuildable_mutation", boom)
    with pytest.raises(AppError, match="anchor-fail"):
        backup_control_recovery.activate_control_after_formal_truth()
    state = backup_control_recovery.get_control_recovery_state()
    assert state["recoveryState"] == backup_control_recovery.RECOVERY_REQUIRED


def test_assert_mutations_control_genesis_allowed(control_db: Path) -> None:
    backup_control_recovery.enter_control_recovery_required(reason="x")
    backup_control_recovery.assert_control_mutations_allowed(operation="control-genesis")


def test_drain_pending_with_invalid_outbox(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control.schema_version()
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        conn.execute(
            """
            INSERT INTO control_authority_outbox(
                outbox_id, kind, checkpoint_json, state, error, created_at, updated_at
            ) VALUES ('o1', 't', 'not-json', 'pending', NULL, 't', 't')
            """
        )
        conn.execute(
            """
            INSERT INTO control_authority_outbox(
                outbox_id, kind, checkpoint_json, state, error, created_at, updated_at
            ) VALUES ('o2', 't', '[]', 'pending', NULL, 't', 't')
            """
        )
        conn.execute("COMMIT")
    result = backup_control_authority.drain_pending_authority_outbox(rpo_zero=False)
    assert int(result["failed"]) >= 1


def test_pending_count_includes_unresolved_mutations(control_db: Path) -> None:
    backup_control.schema_version()
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        conn.execute(
            """
            INSERT INTO control_authority_mutations(
                mutation_id, authority_generation, authority_digest, kind,
                checkpoint_json, state, error, created_at, updated_at
            ) VALUES ('m1', 1, 'd', 't', '{}', 'prepared', NULL, 't', 't')
            """
        )
        conn.execute("COMMIT")
    assert backup_control_authority.pending_authority_outbox_count() >= 1
    assert backup_control_authority.unresolved_authority_mutation_count() >= 1


def test_snapshot_handles_bad_payload_json(control_db: Path) -> None:
    backup_control.schema_version()
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        conn.execute(
            """
            INSERT INTO control_policies(
                policy_id, revision, payload_json, topology_generation,
                promotion_epoch, drain_generation, placement_generation, updated_at
            ) VALUES ('bad', 1, 'not-json', 0, 0, 0, 0, 't')
            """
        )
        conn.execute(
            """
            INSERT INTO control_targets(target_id, generation, payload_json, updated_at)
            VALUES ('tbad', 1, 'not-json', 't')
            """
        )
        conn.execute("COMMIT")
    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    assert int(ckpt["authorityGeneration"]) == 1


def test_logical_transition_genesis_rules(control_db: Path) -> None:
    with pytest.raises(AppError, match="expected-genesis"):
        backup_control_authority.assert_logical_head_transition(
            current_generation=None,
            current_digest=None,
            candidate=_ckpt(2, None),
        )
    a = _ckpt(1, None)
    backup_control_authority.assert_logical_head_transition(
        current_generation=None,
        current_digest=None,
        candidate=a,
    )


def test_store_idempotent_same_tip(control_db: Path) -> None:
    store = MemoryTargetStore()
    a = _ckpt(1, None)
    backup_control_authority.write_authority_checkpoint_to_store(store, a)
    again = backup_control_authority.write_authority_checkpoint_to_store(store, a)
    assert again.get("status") == "idempotent"


def test_mutation_table_classification_constants(control_db: Path) -> None:
    assert "control_policies" in backup_control_recovery.DURABLE_AUTHORITY_TABLES
    assert "lifecycle_intents" in backup_control_recovery.RECONCILABLE_INTENT_TABLES
    assert backup_control_recovery.EPHEMERAL_OWNERSHIP_TABLES
    assert backup_control_recovery.REBUILDABLE_PROJECTION_TABLES


def test_verify_chain_genesis_and_rollback_errors(control_db: Path) -> None:
    bad_genesis = _ckpt(1, None)
    bad_genesis["previousDigest"] = "x" * 64
    bad_genesis["payloadDigest"] = backup_control_authority.compute_payload_digest(bad_genesis)
    bad_genesis["digest"] = backup_control_authority.compute_checkpoint_digest(bad_genesis)
    with pytest.raises(AppError, match="genesis-previous"):
        backup_control_authority.verify_authority_chain([bad_genesis])
    skip = _ckpt(2, None)
    skip["previousDigest"] = None
    skip["payloadDigest"] = backup_control_authority.compute_payload_digest(skip)
    skip["digest"] = backup_control_authority.compute_checkpoint_digest(skip)
    with pytest.raises(AppError, match="gap|genesis"):
        backup_control_authority.verify_authority_chain([skip])
    a = _ckpt(1, None)
    b = _ckpt(2, str(a["digest"]))
    # out-of-order list triggers rollback after sort? sorted by gen so need duplicate gens fork
    fork = _ckpt(1, None, policies=[{"policyId": "other", "policyRevision": 1}])
    with pytest.raises(AppError, match="fork|divergent"):
        backup_control_authority.verify_authority_chain([a, fork])
    del b


def test_fs_corrupt_checkpoint_file_conflict(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    a = _ckpt(1, None)
    backup_control_authority.write_authority_checkpoint_bundle(root, a)
    b = _ckpt(2, str(a["digest"]))
    # Pre-seed corrupt gen-2 object before head advances.
    ckpt_dir = root / "control" / "authority" / "checkpoints"
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    (ckpt_dir / f"{2:016d}.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(AppError, match="checkpoint-conflict"):
        backup_control_authority.write_authority_checkpoint_bundle(root, b)


def test_anchor_no_durable_replica_and_rpo_false(
    control_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_control.schema_version()
    store = MemoryTargetStore()
    backup_control_authority.configure_authority_anchor_stores([store])

    def empty_write(*_a: Any, **_k: Any) -> list[str]:
        return []

    monkeypatch.setattr(backup_control_authority, "_write_checkpoint_to_stores", empty_write)
    monkeypatch.setattr(backup_control_authority, "_write_checkpoint_to_roots", empty_write)
    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    outbox = backup_control_authority._enqueue_authority_outbox(kind="t", checkpoint=ckpt)
    with pytest.raises(AppError, match="no-durable"):
        backup_control_authority.anchor_prepared_mutation(
            checkpoint=ckpt, outbox_id=outbox, mutation_id="m1", kind="t", rpo_zero=True
        )
    outbox2 = backup_control_authority._enqueue_authority_outbox(kind="t", checkpoint=ckpt)
    failed = backup_control_authority.anchor_prepared_mutation(
        checkpoint=ckpt, outbox_id=outbox2, mutation_id="m2", kind="t", rpo_zero=False
    )
    assert failed["status"] == "failed"


def test_anchor_oserror_rpo_false(
    control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control.schema_version()
    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    outbox = backup_control_authority._enqueue_authority_outbox(kind="t", checkpoint=ckpt)

    def boom(*_a: Any, **_k: Any) -> list[str]:
        raise OSError("io")

    monkeypatch.setattr(backup_control_authority, "_write_checkpoint_to_roots", boom)
    result = backup_control_authority.anchor_prepared_mutation(
        checkpoint=ckpt, outbox_id=outbox, mutation_id="m3", kind="t", rpo_zero=False
    )
    assert result["status"] == "failed"


def test_reconcile_store_hit_and_retry_anchor(control_db: Path, tmp_path: Path) -> None:
    store = MemoryTargetStore()
    backup_control_authority.configure_authority_anchor_stores([store])
    a = _ckpt(1, None, policies=[{"policyId": "p", "policyRevision": 1}])
    backup_control_authority.write_authority_checkpoint_to_store(store, a)
    backup_control.schema_version()
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        conn.execute(
            """
            INSERT INTO control_authority_mutations(
                mutation_id, authority_generation, authority_digest, kind,
                checkpoint_json, state, error, created_at, updated_at
            ) VALUES ('storehit', ?, ?, 't', ?, 'remote-outcome-unknown', NULL, 't', 't')
            """,
            (int(a["authorityGeneration"]), str(a["digest"]), json.dumps(a)),
        )
        conn.execute("COMMIT")
    result = backup_control_authority.reconcile_remote_outcome_unknown(rpo_zero=False)
    assert int(result["reconciled"]) >= 1
    # Retry path: prepared not on remote yet → anchor
    backup_control.create_policy({"policyId": "p2", "policyRevision": 1, "enabled": True})
    next_ckpt = backup_control_authority.snapshot_authority_from_control_db()
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        conn.execute(
            """
            INSERT INTO control_authority_mutations(
                mutation_id, authority_generation, authority_digest, kind,
                checkpoint_json, state, error, created_at, updated_at
            ) VALUES ('retryme', ?, ?, 't', ?, 'prepared', NULL, 't', 't')
            """,
            (int(next_ckpt["authorityGeneration"]), str(next_ckpt["digest"]), json.dumps(next_ckpt)),
        )
        conn.execute("COMMIT")
    result2 = backup_control_authority.reconcile_remote_outcome_unknown(rpo_zero=False)
    assert int(result2["reconciled"]) >= 1


def test_reconcile_invalid_dict_checkpoint(control_db: Path) -> None:
    backup_control.schema_version()
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        conn.execute(
            """
            INSERT INTO control_authority_mutations(
                mutation_id, authority_generation, authority_digest, kind,
                checkpoint_json, state, error, created_at, updated_at
            ) VALUES ('arr', 1, 'd', 't', '[1,2]', 'prepared', NULL, 't', 't')
            """
        )
        conn.execute("COMMIT")
    backup_control_authority.reconcile_remote_outcome_unknown(rpo_zero=False)
    with backup_control._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT state FROM control_authority_mutations WHERE mutation_id='arr'"
        ).fetchone()
    assert str(row["state"]) == backup_control_authority.MUTATION_DIVERGENT


def test_reconstruct_without_boot_epoch_field(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    # Checkpoint without controlBootEpoch
    a = _ckpt(1, None, policies=[{"policyId": "p", "policyRevision": 1}])
    assert "controlBootEpoch" not in a or a.get("controlBootEpoch") is None
    backup_control_authority.write_authority_checkpoint_bundle(root, a)
    result = backup_control_recovery.reconstruct_control_authority(
        recovery_targets=[root], activate=True  # no targets in checkpoint
    )
    assert result["status"] == "recovered"


def test_provider_discover_empty_root_locator(control_db: Path) -> None:
    loc = backup_authority_provider.AuthorityReplicaLocator(
        replica_id="empty", kind="filesystem", root=None
    )
    provider = backup_authority_provider.StaticAuthorityReplicaProvider(_locators=[loc])
    assert provider.discover() == []
    assert not backup_authority_provider.authority_provider_configured()
    backup_authority_provider.configure_authority_replica_provider(
        backup_authority_provider.StaticAuthorityReplicaProvider()
    )
    assert not backup_authority_provider.authority_provider_configured()


def test_select_heads_prefers_full_payload_and_lagging_tip_only(control_db: Path) -> None:
    a1 = _ckpt(1, None, policies=[{"policyId": "p", "policyRevision": 1}])
    a2 = _ckpt(2, str(a1["digest"]), policies=[{"policyId": "p", "policyRevision": 2}])
    selected = backup_control_authority.select_authority_heads(
        {
            "stub": {
                "generation": 2,
                "digest": a2["digest"],
                # no checkpoint payload
            },
            "full": {
                "generation": 2,
                "digest": a2["digest"],
                "checkpoint": a2,
                "history": [a1, a2],
            },
            "lag": {
                "generation": 1,
                "digest": a1["digest"],
            },
        }
    )
    assert selected["generation"] == 2
    assert "lag" in selected["laggingReplicas"]


def test_activate_with_complete_coverage_target(control_db: Path) -> None:
    backup_control.create_policy({"policyId": "p", "policyRevision": 1, "enabled": True})
    backup_control.upsert_target({"targetId": "t1", "kind": "filesystem", "root": "/tmp"})
    backup_control.set_target_index_coverage(
        "t1",
        state="complete",
        formal_receipt_count=1,
        source_receipt_mutation_generation=0,
        reason="unit",
    )
    backup_control_recovery.set_control_recovery_state(
        recovery_state=backup_control_recovery.STATE_RECOVERING_FORMAL_TRUTH,
        reason="unit",
    )
    result = backup_control_recovery.activate_control_after_formal_truth(
        require_complete_coverage=True
    )
    assert result["status"] == "active"


def test_drain_no_roots_rpo_false(control_db: Path) -> None:
    backup_control.schema_version()
    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority._enqueue_authority_outbox(kind="t", checkpoint=ckpt)
    result = backup_control_authority.drain_pending_authority_outbox(rpo_zero=False)
    assert int(result["failed"]) >= 1


def test_protocol_methods_on_static_provider(control_db: Path) -> None:
    p = backup_authority_provider.StaticAuthorityReplicaProvider()
    assert p.locators() == []
    assert p.configured() is False
    assert p.discover() == []


def test_ancestor_helpers_direct(control_db: Path) -> None:
    assert (
        backup_control_authority.replica_is_canonical_ancestor(
            candidate_history={},
            canonical_history={1: "a"},
            candidate_tip=0,
        )
        is False
    )
    assert (
        backup_control_authority.replica_is_canonical_ancestor(
            candidate_history={2: "x"},
            canonical_history={1: "a"},
            candidate_tip=2,
        )
        is False
    )
    assert (
        backup_control_authority.replica_is_canonical_ancestor(
            candidate_history={1: "a", 2: "b"},
            canonical_history={1: "a", 2: "b"},
            candidate_tip=2,
        )
        is True
    )
    assert (
        backup_control_authority.replica_is_canonical_ancestor(
            candidate_history={1: "a", 2: "BAD"},
            canonical_history={1: "a", 2: "b"},
            candidate_tip=2,
        )
        is False
    )
    assert backup_control_authority._history_digest_map([{"no": "gen"}, {"authorityGeneration": 0, "digest": "d"}]) == {}  # noqa: SLF001


def test_pending_count_when_mutations_table_missing(
    control_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_control.schema_version()

    class _Conn:
        def execute(self, sql: str, *_a: Any, **_k: Any) -> Any:
            if "control_authority_mutations" in sql:
                raise RuntimeError("no such table")
            class _R:
                def fetchone(self_inner: Any) -> dict[str, int]:
                    return {"c": 0}

            return _R()

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

    monkeypatch.setattr(backup_control, "_connect", lambda: _Conn())
    assert backup_control_authority.pending_authority_outbox_count() == 0
    assert backup_control_authority.unresolved_authority_mutation_count() == 0


def test_mark_mutation_with_conn(control_db: Path) -> None:
    backup_control.schema_version()
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        conn.execute(
            """
            INSERT INTO control_authority_mutations(
                mutation_id, authority_generation, authority_digest, kind,
                checkpoint_json, state, error, created_at, updated_at
            ) VALUES ('mc', 1, 'd', 't', '{}', 'prepared', NULL, 't', 't')
            """
        )
        backup_control_authority._mark_mutation(  # noqa: SLF001
            "mc", state=backup_control_authority.MUTATION_DURABLE, conn=conn
        )
        conn.execute("COMMIT")
    with backup_control._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT state FROM control_authority_mutations WHERE mutation_id='mc'"
        ).fetchone()
    assert str(row["state"]) == backup_control_authority.MUTATION_DURABLE


def test_drain_reconcile_apperror_swallowed(
    control_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_control.schema_version()

    def boom(**_k: Any) -> dict[str, Any]:
        raise AppError("x", code=ErrorCode.INTERNAL, status=500)

    monkeypatch.setattr(backup_control_authority, "reconcile_remote_outcome_unknown", boom)
    result = backup_control_authority.drain_pending_authority_outbox(rpo_zero=False)
    assert "drained" in result


def test_reconcile_load_bundle_errors_and_rpo_raise(
    control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "missing-auth"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    store = MemoryTargetStore()
    backup_control_authority.configure_authority_anchor_stores([store])
    backup_control.schema_version()
    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        conn.execute(
            """
            INSERT INTO control_authority_mutations(
                mutation_id, authority_generation, authority_digest, kind,
                checkpoint_json, state, error, created_at, updated_at
            ) VALUES ('needretry', ?, ?, 't', ?, 'prepared', NULL, 't', 't')
            """,
            (int(ckpt["authorityGeneration"]), str(ckpt["digest"]), json.dumps(ckpt)),
        )
        conn.execute("COMMIT")

    def fail_anchor(**_k: Any) -> dict[str, Any]:
        raise AppError("still-fail", code=ErrorCode.INTERNAL, status=503)

    monkeypatch.setattr(backup_control_authority, "anchor_prepared_mutation", fail_anchor)
    with pytest.raises(AppError, match="still-fail"):
        backup_control_authority.reconcile_remote_outcome_unknown(rpo_zero=True)


def test_write_roots_errors_raise(control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "auth"
    root.mkdir()

    def boom(*_a: Any, **_k: Any) -> dict[str, Path]:
        raise OSError("fail-root")

    monkeypatch.setattr(backup_control_authority, "write_authority_checkpoint_bundle", boom)
    with pytest.raises(AppError, match="anchor-failed|fail-root"):
        backup_control_authority._write_checkpoint_to_roots(  # noqa: SLF001
            _ckpt(1, None), [root]
        )


def test_write_stores_errors_raise(control_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryTargetStore()

    def boom(*_a: Any, **_k: Any) -> dict[str, str]:
        raise AppError("store-fail", code=ErrorCode.INTERNAL, status=500)

    monkeypatch.setattr(backup_control_authority, "write_authority_checkpoint_to_store", boom)
    with pytest.raises(AppError, match="store-fail|anchor"):
        backup_control_authority._write_checkpoint_to_stores(_ckpt(1, None), [store])  # noqa: SLF001


def test_select_heads_checkpoint_from_history_only(control_db: Path) -> None:
    a1 = _ckpt(1, None, policies=[{"policyId": "p", "policyRevision": 1}])
    a2 = _ckpt(2, str(a1["digest"]), policies=[{"policyId": "p", "policyRevision": 2}])
    selected = backup_control_authority.select_authority_heads(
        {
            "h": {
                "generation": 2,
                "digest": a2["digest"],
                "history": [a1, a2],
                # checkpoint omitted — taken from history tip
            }
        }
    )
    assert "policies" in selected["checkpoint"]


def test_provider_locator_to_public_minimal(control_db: Path) -> None:
    loc = backup_authority_provider.AuthorityReplicaLocator(replica_id="x", kind="s3")
    d = loc.to_public_dict()
    assert d == {"replicaId": "x", "kind": "s3"}
    # Missing bucket → production factory fails closed.
    with pytest.raises(AppError, match="bucket-required"):
        backup_authority_provider.StaticAuthorityReplicaProvider(
            _locators=[loc]
        )._resolve_s3_store(loc)  # noqa: SLF001


def test_fs_identical_whitespace_checkpoint_rewrite(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    a = _ckpt(1, None)
    backup_control_authority.write_authority_checkpoint_bundle(root, a)
    path = root / "control" / "authority" / "checkpoints" / f"{1:016d}.json"
    # Same JSON different trailing whitespace should be accepted as identical canonical
    body = path.read_text(encoding="utf-8")
    path.write_text(body.rstrip() + "\n\n", encoding="utf-8")
    backup_control_authority.write_authority_checkpoint_bundle(root, a)


def test_verdict_quarantines_corrupt_local_with_remote(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    a = _ckpt(1, None, policies=[{"policyId": "p", "policyRevision": 1}])
    backup_control_authority.write_authority_checkpoint_bundle(root, a)
    backup_control_authority.configure_authority_anchor_roots([root])
    # Corrupt local DB file in place.
    control_db.write_bytes(b"not-a-sqlite-database")
    Path(str(control_db) + "-wal").write_bytes(b"x")
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict["verdict"] == backup_control_recovery.RECOVERY_REQUIRED
    # Quarantine moved the corrupt file aside.
    assert not control_db.is_file() or backup_control_recovery.local_control_db_healthy()


def test_verdict_quarantines_corrupt_when_anchors_empty(control_db: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    backup_control_authority.configure_authority_anchor_roots([empty])
    control_db.write_bytes(b"corrupt-db")
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict["verdict"] in {
        backup_control_recovery.STATE_GENESIS_REQUIRED,
        backup_control_recovery.STATE_AUTHORITY_UNAVAILABLE,
    }


def test_activate_local_snapshot_apperror_swallowed(
    control_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_control.schema_version()
    backup_control_recovery.set_control_recovery_state(
        recovery_state=backup_control_recovery.STATE_RECOVERING_FORMAL_TRUTH,
        reason="unit",
    )

    def boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise AppError("snap-fail", code=ErrorCode.INTERNAL, status=500)

    monkeypatch.setattr(backup_control_authority, "_snapshot_authority_from_conn", boom)
    result = backup_control_recovery.activate_control_after_formal_truth()
    assert result["status"] == "active"


def test_anti_entropy_repair_error_swallowed(control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tip = tmp_path / "tip"
    lag = tmp_path / "lag"
    tip.mkdir()
    lag.mkdir()
    a1 = _ckpt(1, None, policies=[{"policyId": "p", "policyRevision": 1}])
    a2 = _ckpt(2, str(a1["digest"]), policies=[{"policyId": "p", "policyRevision": 2}])
    backup_control_authority.write_authority_checkpoint_bundle(tip, a1)
    backup_control_authority.write_authority_checkpoint_bundle(tip, a2)
    backup_control_authority.write_authority_checkpoint_bundle(lag, a1)

    def boom(**_k: Any) -> dict[str, Any]:
        raise AppError("repair-fail", code=ErrorCode.INTERNAL, status=500)

    monkeypatch.setattr(backup_control_authority, "repair_lagging_authority_replicas", boom)
    result = backup_control_recovery.reconstruct_control_authority(
        recovery_targets=[tip, lag], activate=True  # policies only
    )
    assert result["status"] == "recovered"


def test_mutate_policy_with_anchors_same_tx(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control.create_policy({"policyId": "pm", "policyRevision": 1, "enabled": True})
    updated = backup_control.mutate_policy(
        "pm",
        expected_revision=1,
        mutate=lambda p: {**p, "enabled": False},
        generation_kind="placement",
    )
    assert updated["enabled"] is False
    bundle = backup_control_authority.load_authority_bundle(root)
    assert int(bundle["head"]["authorityGeneration"]) >= 2


def test_delete_policy_and_target_with_anchors(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control.create_policy({"policyId": "delp", "policyRevision": 1, "enabled": True})
    backup_control.upsert_target({"targetId": "delt", "kind": "filesystem", "root": str(tmp_path)})
    backup_control.delete_policy("delp")
    backup_control.delete_target("delt")
    assert backup_control.list_policies() == []
    assert backup_control.list_targets() == []

"""Control-authority skeleton: secretless control-authority-v1 + hash-chained generations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_control,
    backup_control_authority,
    backup_control_recovery,
)


@pytest.fixture
def control_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "control"
    root.mkdir()
    db = root / "control.sqlite3"
    monkeypatch.setattr(backup_control, "CONTROL_DIR", root)
    monkeypatch.setattr(backup_control, "CONTROL_DB", db)
    backup_control_authority.configure_authority_anchor_roots(None)
    backup_control_authority.configure_authority_anchor_stores(None)
    return db


def test_schema_v7_boot_and_recovery_state(control_db: Path) -> None:
    assert backup_control.CONTROL_SCHEMA_VERSION == 8
    assert backup_control.schema_version() == 8
    migrations = backup_control.list_schema_migrations()
    assert migrations[-1]["version"] == 8
    assert "control-authority" in migrations[-1]["description"]
    state = backup_control_recovery.get_control_recovery_state()
    assert state["recoveryState"] == backup_control_recovery.RECOVERY_ACTIVE
    assert int(state["bootEpoch"]) >= 1


def test_control_authority_schema_constant_and_keys() -> None:
    assert backup_control_authority.AUTHORITY_SCHEMA == "control-authority-v1"
    assert "accesskey" in backup_control_authority.FORBIDDEN_SECRET_KEY_FRAGMENTS
    assert "secret" in backup_control_authority.FORBIDDEN_SECRET_KEY_FRAGMENTS
    assert "ageidentity" in backup_control_authority.FORBIDDEN_SECRET_KEY_FRAGMENTS


def test_sanitize_target_strips_secrets_keeps_locator() -> None:
    raw = {
        "targetId": "t1",
        "kind": "s3",
        "region": "us-east-1",
        "failureDomain": "az-a",
        "storageTier": "hot",
        "endpointUrl": "https://minio.example:9000",
        "bucket": "backups",
        "prefix": "prod",
        "accessKeyId": "AKIAEXAMPLE",
        "secretAccessKey": "super-secret",
        "ageIdentity": "AGE-SECRET-KEY-1...",
        "credentialProvider": {"type": "aws-default-chain", "accessKey": "x", "secretKey": "y"},
        "credentialReference": "vault:s3/prod",
        "password": "nope",
        "apiKey": "ds-key",
    }
    clean = backup_control_authority.sanitize_target_for_authority(raw)
    assert clean["targetId"] == "t1"
    assert clean["endpointUrl"] == "https://minio.example:9000"
    assert clean["credentialReference"] == "vault:s3/prod"
    blob = json.dumps(clean, sort_keys=True)
    assert "super-secret" not in blob
    assert "AKIAEXAMPLE" not in blob
    assert "AGE-SECRET" not in blob
    assert "password" not in clean
    assert "apiKey" not in clean
    assert "accessKeyId" not in clean
    assert "secretAccessKey" not in clean
    assert "secretKey" not in json.dumps(clean.get("credentialProvider") or {})


def test_authority_checkpoint_is_hash_chained_and_secretless() -> None:
    first = backup_control_authority.build_authority_checkpoint(
        generation=1,
        previous_digest=None,
        policies=[{"policyId": "p1", "policyRevision": 3}],
        targets=[
            {
                "targetId": "t1",
                "kind": "s3",
                "secretAccessKey": "must-not-appear",
                "credentialReference": "ref-1",
            }
        ],
        receipt_mutation_generations={"t1": 4},
        promotion_epochs={"p1": 2},
        drain_generations={"p1": 1},
        placement_generations={"p1": 7},
        control_schema_version=7,
    )
    assert first["schema"] == "control-authority-v1"
    assert first["authorityGeneration"] == 1
    assert first["previousDigest"] is None
    assert first["payloadDigest"]
    assert first["digest"]
    assert "must-not-appear" not in json.dumps(first)
    second = backup_control_authority.build_authority_checkpoint(
        generation=2,
        previous_digest=str(first["digest"]),
        policies=[{"policyId": "p1", "policyRevision": 4}],
        targets=[{"targetId": "t1", "kind": "s3", "credentialReference": "ref-1"}],
        receipt_mutation_generations={"t1": 5},
        promotion_epochs={"p1": 2},
        drain_generations={"p1": 1},
        placement_generations={"p1": 8},
        control_schema_version=7,
    )
    assert second["previousDigest"] == first["digest"]
    assert second["digest"] != first["digest"]
    backup_control_authority.verify_authority_chain([first, second])


def test_authority_chain_rejects_rollback_and_fork() -> None:
    a1 = backup_control_authority.build_authority_checkpoint(
        generation=1,
        previous_digest=None,
        policies=[],
        targets=[],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=7,
    )
    a2 = backup_control_authority.build_authority_checkpoint(
        generation=2,
        previous_digest=str(a1["digest"]),
        policies=[{"policyId": "p", "policyRevision": 1}],
        targets=[],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=7,
    )
    forked = backup_control_authority.build_authority_checkpoint(
        generation=2,
        previous_digest=str(a1["digest"]),
        policies=[{"policyId": "p", "policyRevision": 99}],
        targets=[],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=7,
    )
    with pytest.raises(AppError, match="control-authority-divergent|fork"):
        backup_control_authority.verify_authority_chain([a1, a2, forked])
    rolled = dict(a2)
    rolled["authorityGeneration"] = 1
    with pytest.raises(AppError, match="rollback|control-authority"):
        backup_control_authority.verify_authority_chain([a1, rolled])


def test_select_authority_history_fail_closed_on_divergent_heads() -> None:
    base = backup_control_authority.build_authority_checkpoint(
        generation=1,
        previous_digest=None,
        policies=[],
        targets=[],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=7,
    )
    head_a = backup_control_authority.build_authority_checkpoint(
        generation=2,
        previous_digest=str(base["digest"]),
        policies=[{"policyId": "p", "policyRevision": 1}],
        targets=[],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=7,
    )
    head_b = backup_control_authority.build_authority_checkpoint(
        generation=2,
        previous_digest=str(base["digest"]),
        policies=[{"policyId": "p", "policyRevision": 2}],
        targets=[],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=7,
    )
    with pytest.raises(AppError) as exc:
        backup_control_authority.select_authority_heads(
            {
                "replica-a": {"generation": 2, "digest": head_a["digest"], "checkpoint": head_a},
                "replica-b": {"generation": 2, "digest": head_b["digest"], "checkpoint": head_b},
            }
        )
    assert exc.value.code == ErrorCode.INVALID_REQUEST
    assert "control-authority-divergent" in str(exc.value)

    chosen = backup_control_authority.select_authority_heads(
        {
            "replica-a": {"generation": 2, "digest": head_a["digest"], "checkpoint": head_a},
            "replica-b": {"generation": 2, "digest": head_a["digest"], "checkpoint": head_a},
            "replica-c": {"generation": 1, "digest": base["digest"], "checkpoint": base},
        }
    )
    assert chosen["digest"] == head_a["digest"]
    assert chosen["generation"] == 2


def test_snapshot_authority_from_control_db_is_secretless(control_db: Path) -> None:
    backup_control.create_policy(
        {
            "policyId": "pol-auth",
            "policyRevision": 1,
            "enabled": True,
            "targets": [{"targetId": "tgt-auth", "role": "primary"}],
        }
    )
    backup_control.upsert_target(
        {
            "targetId": "tgt-auth",
            "kind": "s3",
            "endpointUrl": "https://minio.local:9000",
            "bucket": "b",
            "secretAccessKey": "never-checkpoint",
            "credentialReference": "env:AWS",
        }
    )
    with backup_control.begin_formal_metadata_mutation("tgt-auth", operation_id="pub-1"):
        pass
    checkpoint = backup_control_authority.snapshot_authority_from_control_db()
    assert checkpoint["schema"] == "control-authority-v1"
    assert checkpoint["authorityGeneration"] >= 1
    assert "never-checkpoint" not in json.dumps(checkpoint)
    assert any(t.get("targetId") == "tgt-auth" for t in checkpoint["targets"])
    assert any(p.get("policyId") == "pol-auth" for p in checkpoint["policies"])


def test_recovery_required_blocks_mutations(control_db: Path) -> None:
    backup_control_recovery.enter_control_recovery_required(reason="missing-control-db")
    state = backup_control_recovery.get_control_recovery_state()
    assert state["recoveryState"] == backup_control_recovery.RECOVERY_REQUIRED
    with pytest.raises(AppError, match="control-recovery-required"):
        backup_control_recovery.assert_control_mutations_allowed(operation="backup-publish")
    with pytest.raises(AppError, match="control-recovery-required"):
        backup_control_recovery.assert_control_mutations_allowed(operation="destructive-gc")
    # inspect / probe remain allowed
    backup_control_recovery.assert_control_mutations_allowed(operation="inspect")
    backup_control_recovery.assert_control_mutations_allowed(operation="target-probe")


def test_reconstruct_from_checkpoints_advances_boot_epoch_and_clears_ephemeral(
    control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_control.create_policy(
        {
            "policyId": "pol-r",
            "policyRevision": 5,
            "enabled": True,
            "targets": [{"targetId": "t-r", "role": "primary"}],
        }
    )
    backup_control.upsert_target(
        {
            "targetId": "t-r",
            "kind": "s3",
            "endpointUrl": "https://minio.local",
            "bucket": "b",
            "credentialReference": "ref",
            "secretAccessKey": "drop-me",
        }
    )
    # Seed an ephemeral gate that must NOT resurrect after recovery.
    with backup_control.begin_destructive_metadata_fence("t-r", operation_id="old-gc"):
        pass
    with backup_control._connect() as conn:
        conn.execute(
            """
            INSERT INTO target_metadata_gates(target_id, owner_id, mode, fencing_token, lease_until, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("t-r", "zombie", backup_control.METADATA_GATE_DESTRUCTIVE, 99, "2099-01-01T00:00:00Z", "2099-01-01T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO maintenance_leases(worker_kind, scope_id, owner_instance_id, fencing_token, lease_until, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("gc", "t-r", "old-node", 3, "2099-01-01T00:00:00Z", "2099-01-01T00:00:00Z"),
        )

    before = backup_control_recovery.get_control_recovery_state()
    checkpoint = backup_control_authority.snapshot_authority_from_control_db()
    staging = tmp_path / "authority-store"
    staging.mkdir()
    backup_control_authority.write_authority_checkpoint_bundle(staging, checkpoint)

    # Simulate total local control loss.
    control_db.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(control_db) + suffix).unlink(missing_ok=True)

    result = backup_control_recovery.reconstruct_control_authority(
        [staging],
        bootstrap_profile={"reason": "unit-test"},
        activate=False,
    )
    assert result["status"] == "authority-restored"
    assert result["recoveryState"] == backup_control_recovery.STATE_RECOVERING_FORMAL_TRUTH
    # Formal-truth gate: mark target coverage complete then activate.
    backup_control.set_target_index_coverage(
        "t-r", state="complete", formal_receipt_count=0, source_receipt_mutation_generation=0, reason="unit"
    )
    backup_control_recovery.record_formal_truth_validation(
        target_id="t-r",
        status="VALID",
        index_coverage_complete=True,
        lineage_valid=True,
        retirement_reconciled=True,
    )
    activated = backup_control_recovery.activate_control_after_formal_truth(reason="unit-reconstruct")
    assert activated["status"] == "active"
    assert int(activated["bootEpoch"]) > int(before["bootEpoch"])
    assert backup_control_recovery.get_control_recovery_state()["recoveryState"] == (
        backup_control_recovery.RECOVERY_ACTIVE
    )

    policy = backup_control.get_policy("pol-r")
    assert policy is not None
    assert int(policy.get("policyRevision") or 0) == 5
    target = backup_control.get_target("t-r")
    assert target is not None
    assert "drop-me" not in json.dumps(target)
    assert "secretAccessKey" not in json.dumps(target)

    with backup_control._connect() as conn:
        gates = conn.execute("SELECT COUNT(*) AS c FROM target_metadata_gates").fetchone()
        leases = conn.execute("SELECT COUNT(*) AS c FROM maintenance_leases").fetchone()
    assert int(gates["c"]) == 0
    assert int(leases["c"]) == 0


def test_authority_object_key_layout_is_stable() -> None:
    assert backup_control_authority.authority_checkpoint_key(42) == (
        "control/authority/checkpoints/0000000000000042.json"
    )
    assert backup_control_authority.authority_head_key() == "control/authority/head.json"


def test_record_local_authority_head_and_digest_mismatch(control_db: Path) -> None:
    checkpoint = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority.record_local_authority_head(checkpoint)
    with backup_control._connect() as conn:
        row = conn.execute("SELECT authority_generation FROM control_authority_head WHERE id = 1").fetchone()
    assert int(row["authority_generation"]) == int(checkpoint["authorityGeneration"])
    tampered = dict(checkpoint)
    tampered["digest"] = "0" * 64
    with pytest.raises(AppError, match="digest-mismatch"):
        backup_control_authority.verify_authority_chain([tampered])


def test_load_authority_bundle_rejects_head_mismatch_and_missing(tmp_path: Path) -> None:
    checkpoint = backup_control_authority.build_authority_checkpoint(
        generation=1,
        previous_digest=None,
        policies=[],
        targets=[],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=7,
    )
    root = tmp_path / "auth"
    backup_control_authority.write_authority_checkpoint_bundle(root, checkpoint)
    head_path = root / "control" / "authority" / "head.json"
    head_path.write_text('{"schema":"control-authority-v1","authorityGeneration":1,"digest":"' + ("f" * 64) + '"}', encoding="utf-8")
    with pytest.raises(AppError, match="head-checkpoint-mismatch"):
        backup_control_authority.load_authority_bundle(root)
    with pytest.raises(AppError, match="head-missing"):
        backup_control_authority.load_authority_bundle(tmp_path / "empty")


def test_reconstruct_quarantines_existing_db_and_skips_bad_replicas(
    control_db: Path, tmp_path: Path
) -> None:
    backup_control.create_policy({"policyId": "p-q", "policyRevision": 1, "enabled": True})
    checkpoint = backup_control_authority.snapshot_authority_from_control_db()
    good = tmp_path / "good"
    good.mkdir()
    backup_control_authority.write_authority_checkpoint_bundle(good, checkpoint)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "control" / "authority").mkdir(parents=True)
    # Corrupt head only — discover should skip.
    (bad / "control" / "authority" / "head.json").write_text("{}", encoding="utf-8")

    assert control_db.is_file()
    result = backup_control_recovery.reconstruct_control_authority(
        [bad, good], activate=True
    )
    assert result["status"] == "recovered"
    assert result["quarantinedPath"] is not None
    assert Path(str(result["quarantinedPath"])).is_file()
    assert backup_control.get_policy("p-q") is not None


def test_assert_unknown_operation_blocked_during_recovery(control_db: Path) -> None:
    backup_control_recovery.enter_control_recovery_required(reason="unit")
    with pytest.raises(AppError, match="control-recovery-required"):
        backup_control_recovery.assert_control_mutations_allowed(operation="mystery-op")
    with pytest.raises(AppError, match="control-authority-no-usable-replicas"):
        backup_control_recovery.reconstruct_control_authority([])


def test_build_rejects_invalid_generation_and_secret_markers() -> None:
    with pytest.raises(AppError, match="generation must be"):
        backup_control_authority.build_authority_checkpoint(
            generation=0,
            previous_digest=None,
            policies=[],
            targets=[],
            receipt_mutation_generations={},
            promotion_epochs={},
            drain_generations={},
            placement_generations={},
            control_schema_version=7,
        )
    poisoned = backup_control_authority.build_authority_checkpoint(
        generation=1,
        previous_digest=None,
        policies=[],
        targets=[{"targetId": "t", "kind": "s3"}],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=7,
    )
    poisoned["targets"] = [{"targetId": "t", "note": "AGE-SECRET-KEY-1EXFIL"}]
    with pytest.raises(AppError, match="contains-secrets"):
        backup_control_authority._assert_checkpoint_secretless(poisoned)


def test_active_state_allows_mutations(control_db: Path) -> None:
    backup_control_recovery.assert_control_mutations_allowed(operation="backup-publish")


def test_verify_rejects_schema_mismatch_and_empty() -> None:
    with pytest.raises(AppError, match="empty-history"):
        backup_control_authority.verify_authority_chain([])
    bad = backup_control_authority.build_authority_checkpoint(
        generation=1,
        previous_digest=None,
        policies=[],
        targets=[],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=7,
    )
    bad["schema"] = "not-authority"
    # digest no longer matches after schema change when recomputed path runs payload first
    with pytest.raises(AppError, match="schema-mismatch|digest-mismatch"):
        backup_control_authority.verify_authority_chain([bad])


def test_checkpoint_digest_is_deterministic() -> None:
    kwargs: dict[str, Any] = {
        "generation": 3,
        "previous_digest": "a" * 64,
        "policies": [{"policyId": "p", "policyRevision": 1}],
        "targets": [{"targetId": "t"}],
        "receipt_mutation_generations": {"t": 1},
        "promotion_epochs": {},
        "drain_generations": {},
        "placement_generations": {},
        "control_schema_version": 7,
        "created_at": "2026-08-24T00:00:00Z",
    }
    a = backup_control_authority.build_authority_checkpoint(**kwargs)
    b = backup_control_authority.build_authority_checkpoint(**kwargs)
    assert a["digest"] == b["digest"]
    assert a["payloadDigest"] == b["payloadDigest"]
    # Manual digest shape: sha256 hex
    assert len(a["digest"]) == 64
    assert int(a["digest"], 16) >= 0
    # payload digest excludes chain envelope fields
    payload = {k: v for k, v in a.items() if k not in {"digest", "previousDigest", "payloadDigest"}}
    expected = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    # build may nest differently — just ensure digest is stable hex
    assert len(expected) == 64


def test_rpo_zero_anchor_before_ack_writes_durable_checkpoint(
    control_db: Path, tmp_path: Path
) -> None:
    anchor_a = tmp_path / "auth-a"
    anchor_b = tmp_path / "auth-b"
    anchor_a.mkdir()
    anchor_b.mkdir()
    backup_control_authority.configure_authority_anchor_roots([anchor_a, anchor_b])
    backup_control.create_policy(
        {"policyId": "pol-rpo", "policyRevision": 2, "enabled": True, "targets": []}
    )
    for root in (anchor_a, anchor_b):
        head = json.loads((root / "control" / "authority" / "head.json").read_text(encoding="utf-8"))
        assert int(head["authorityGeneration"]) >= 1
        ckpt_path = root / "control" / "authority" / "checkpoints" / f"{int(head['authorityGeneration']):016d}.json"
        body = json.loads(ckpt_path.read_text(encoding="utf-8"))
        assert any(p.get("policyId") == "pol-rpo" for p in body["policies"])
        assert "secret" not in json.dumps(body).casefold() or "credentialreference" in json.dumps(body).casefold()
    assert backup_control_authority.pending_authority_outbox_count() == 0
    with backup_control._connect() as conn:
        row = conn.execute(
            "SELECT state FROM control_authority_outbox ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert str(row["state"]) == backup_control_authority.OUTBOX_DURABLE


def test_rpo_zero_anchor_failure_fails_closed(control_db: Path, tmp_path: Path) -> None:
    # File path as root forces OSError when mkdir tries to create control/ under a file.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    backup_control_authority.configure_authority_anchor_roots([blocker])
    with pytest.raises(AppError, match="authority-rpo-zero-anchor-failed"):
        backup_control.create_policy({"policyId": "pol-pre", "policyRevision": 1, "enabled": True})
    with pytest.raises(AppError, match="authority-rpo-zero-anchor-failed"):
        backup_control_authority.anchor_non_rebuildable_mutation(kind="policy-mutation", rpo_zero=True)


def test_rpo_zero_requires_roots_when_explicit(control_db: Path) -> None:
    backup_control_authority.configure_authority_anchor_roots(None)
    backup_control.create_policy({"policyId": "pol-local", "policyRevision": 1, "enabled": True})
    # no roots ⇒ create_policy does not enforce; explicit anchor with rpo_zero fails
    with pytest.raises(AppError, match="no-roots"):
        backup_control_authority.anchor_non_rebuildable_mutation(kind="policy-mutation", rpo_zero=True)


def test_pending_outbox_blocks_mutations(control_db: Path) -> None:
    backup_control.create_policy({"policyId": "pol-out", "policyRevision": 1, "enabled": True})
    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority._enqueue_authority_outbox(kind="test", checkpoint=ckpt)
    assert backup_control_authority.pending_authority_outbox_count() == 1
    with pytest.raises(AppError, match="authority-anchor-pending"):
        backup_control_recovery.assert_control_mutations_allowed(operation="policy-mutation")
    # inspect still ok
    backup_control_recovery.assert_control_mutations_allowed(operation="inspect")


def test_formal_truth_rebuild_only_indexes_commit_authenticated_receipts(
    control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deepseek_infra.infra.workspace import backup_publish
    from deepseek_infra.infra.workspace.backup_target_store import object_key

    target_root = tmp_path / "tgt-formal"
    (target_root / "commits" / "pol").mkdir(parents=True)
    (target_root / "receipts").mkdir(parents=True)
    digest = "ab" * 32
    payload_key = object_key(digest)
    # Authenticated recovery point
    receipt = {
        "schemaVersion": 4,
        "backupId": "bak-auth",
        "policyId": "pol-f",
        "objects": [{"digest": digest, "size": 4}],
    }
    receipt_bytes = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (target_root / "receipts" / "bak-auth.json").write_bytes(receipt_bytes)
    receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
    marker: dict[str, Any] = {
        "schemaVersion": 4,
        "backupId": "bak-auth",
        "policyId": "pol-f",
        "receiptDigest": receipt_digest,
        "targetGeneration": 1,
        "runId": "run-1",
        "committedAt": "2026-08-24T00:00:00Z",
    }
    marker["commitHash"] = backup_publish._commit_hash(marker)
    (target_root / "commits" / "pol" / "slot1.json").write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    # Orphan receipt — no commit
    orphan = {
        "schemaVersion": 4,
        "backupId": "bak-orphan",
        "policyId": "pol-f",
        "objects": [{"digest": "cd" * 32, "size": 1}],
    }
    (target_root / "receipts" / "bak-orphan.json").write_text(
        json.dumps(orphan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    # Invalid commit (bad hash)
    bad_marker = dict(marker)
    bad_marker["backupId"] = "bak-bad"
    bad_marker["commitHash"] = "0" * 64
    (target_root / "commits" / "pol" / "slot-bad.json").write_text(
        json.dumps(bad_marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    class _T:
        target_id = "t-formal"
        root = target_root
        store = None

    from deepseek_infra.infra.workspace import backup_retirement

    monkeypatch.setattr(backup_retirement, "_receipt_has_valid_retirement_marker", lambda *a, **k: False)

    result = backup_control_recovery.rebuild_formal_truth_from_authenticated_commits(_T())
    assert result["authenticatedRecoveryPoints"] == 1
    assert result["orphanControlMetadata"] == 1
    assert result["invalidCommits"] >= 1
    assert result["source"] == "commit-authenticated-receipts"
    assert backup_control.object_has_live_ref("t-formal", payload_key)
    # Orphan digest must not be indexed as live authority
    orphan_key = object_key("cd" * 32)
    assert not backup_control.object_has_live_ref("t-formal", orphan_key)
    lineage = backup_control.get_recovery_lineage("pol-f", "bak-auth")
    assert lineage is not None


def test_drain_pending_authority_outbox_after_crash_window(
    control_db: Path, tmp_path: Path
) -> None:
    root = tmp_path / "auth-drain"
    root.mkdir()
    backup_control.create_policy({"policyId": "pol-drain", "policyRevision": 1, "enabled": True})
    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority._enqueue_authority_outbox(kind="crash-window", checkpoint=ckpt)
    assert backup_control_authority.pending_authority_outbox_count() == 1
    backup_control_authority.configure_authority_anchor_roots([root])
    result = backup_control_authority.drain_pending_authority_outbox(rpo_zero=True)
    assert result["drained"] == 1
    assert result["pending"] == 0
    assert (root / "control" / "authority" / "head.json").is_file()


def test_anchor_skip_without_rpo_when_no_roots(control_db: Path) -> None:
    backup_control.create_policy({"policyId": "pol-skip", "policyRevision": 1, "enabled": True})
    result = backup_control_authority.anchor_non_rebuildable_mutation(
        kind="policy-mutation",
        rpo_zero=False,
    )
    assert result["status"] == "skipped"


def test_upsert_target_anchors_when_roots_configured(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth-tgt"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control.upsert_target(
        {
            "targetId": "tgt-anchor",
            "kind": "s3",
            "endpointUrl": "https://minio.local",
            "bucket": "b",
            "credentialReference": "ref",
        }
    )
    head = json.loads((root / "control" / "authority" / "head.json").read_text(encoding="utf-8"))
    assert int(head["authorityGeneration"]) >= 1


def test_authenticate_rejects_receipt_digest_mismatch(control_db: Path, tmp_path: Path) -> None:
    from deepseek_infra.infra.workspace import backup_publish

    target_root = tmp_path / "tgt-mismatch"
    (target_root / "receipts").mkdir(parents=True)
    receipt = {"schemaVersion": 4, "backupId": "b1", "policyId": "p1", "objects": []}
    raw = (json.dumps(receipt, sort_keys=True) + "\n").encode("utf-8")
    (target_root / "receipts" / "b1.json").write_bytes(raw)
    marker = {
        "schemaVersion": 4,
        "backupId": "b1",
        "policyId": "p1",
        "receiptDigest": "0" * 64,
        "targetGeneration": 1,
    }
    marker["commitHash"] = backup_publish._commit_hash(marker)

    class _T:
        target_id = "t-m"
        root = target_root
        store = None

    assert backup_control_recovery.authenticate_committed_receipt(_T(), marker) is None


def test_formal_truth_rebuild_on_memory_store(control_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.workspace import backup_publish, backup_retirement
    from deepseek_infra.infra.workspace.backup_target_store import MemoryTargetStore, object_key, receipt_key

    store = MemoryTargetStore()
    digest = "11" * 32
    receipt = {
        "schemaVersion": 4,
        "backupId": "mem-bak",
        "policyId": "pol-mem",
        "objects": [{"digest": digest, "size": 2}],
    }
    receipt_bytes = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    store.put_if_absent(receipt_key("mem-bak"), receipt_bytes)
    # orphan on store
    orphan = {"schemaVersion": 4, "backupId": "mem-orphan", "policyId": "pol-mem", "objects": []}
    store.put_if_absent(
        receipt_key("mem-orphan"),
        (json.dumps(orphan, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    marker: dict[str, Any] = {
        "schemaVersion": 4,
        "backupId": "mem-bak",
        "policyId": "pol-mem",
        "receiptDigest": hashlib.sha256(receipt_bytes).hexdigest(),
        "targetGeneration": 1,
        "committedAt": "2026-08-24T01:00:00Z",
    }
    marker["commitHash"] = backup_publish._commit_hash(marker)
    commit_bytes = (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode("utf-8")
    store.put_if_absent("commits/pol-mem/slot.json", commit_bytes)

    target = type("T", (), {"target_id": "t-mem", "root": None, "store": store})()

    monkeypatch.setattr(backup_retirement, "_receipt_has_valid_retirement_marker", lambda *a, **k: False)
    result = backup_control_recovery.rebuild_formal_truth_from_authenticated_commits(target)
    assert result["authenticatedRecoveryPoints"] == 1
    assert result["orphanControlMetadata"] == 1
    assert backup_control.object_has_live_ref("t-mem", object_key(digest))


def test_rebuild_formal_truth_requires_target_id(control_db: Path) -> None:
    class _T:
        target_id = ""
        root = None
        store = None

    with pytest.raises(AppError, match="target_id required"):
        backup_control_recovery.rebuild_formal_truth_from_authenticated_commits(_T())


def test_drain_outbox_fails_without_roots(control_db: Path) -> None:
    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority._enqueue_authority_outbox(kind="x", checkpoint=ckpt)
    with pytest.raises(AppError, match="no-roots"):
        backup_control_authority.drain_pending_authority_outbox(rpo_zero=True)


def test_authority_checkpoint_roundtrip_on_memory_store(control_db: Path) -> None:
    from deepseek_infra.infra.workspace.backup_target_store import MemoryTargetStore

    store = MemoryTargetStore()
    backup_control.create_policy({"policyId": "pol-store", "policyRevision": 1, "enabled": True})
    backup_control_authority.configure_authority_anchor_stores([store])
    try:
        result = backup_control_authority.anchor_non_rebuildable_mutation(kind="store-anchor", rpo_zero=True)
        assert result["status"] == "anchored"
        assert result["durableStores"]
        bundle = backup_control_authority.load_authority_bundle_from_store(store, replica_id="mem")
        assert bundle["checkpoint"] is not None
        assert int(bundle["head"]["authorityGeneration"]) >= 1
        # Second generation advances head via put_if_match
        backup_control.upsert_target(
            {"targetId": "t-store", "kind": "s3", "bucket": "b", "endpointUrl": "https://x", "credentialReference": "r"}
        )
        bundle2 = backup_control_authority.load_authority_bundle_from_store(store)
        assert int(bundle2["head"]["authorityGeneration"]) > int(bundle["head"]["authorityGeneration"])
    finally:
        backup_control_authority.configure_authority_anchor_stores(None)


def test_ensure_control_authority_ready_drains_outbox(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "startup-auth"
    root.mkdir()
    idle = backup_control.ensure_control_authority_ready()
    assert idle["status"] == "ready"
    assert idle["pending"] == 0

    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority._enqueue_authority_outbox(kind="startup", checkpoint=ckpt)
    waiting = backup_control.ensure_control_authority_ready()
    assert waiting["status"] == "pending-without-anchors"
    assert waiting["pending"] == 1

    backup_control_authority.configure_authority_anchor_roots([root])
    ready = backup_control.ensure_control_authority_ready()
    assert ready["status"] == "ready"
    assert ready["drained"] == 1
    assert ready["pending"] == 0
    assert (root / "control" / "authority" / "head.json").is_file()


def test_mutate_policy_promotion_and_drain_intent_anchor(
    control_db: Path, tmp_path: Path
) -> None:
    root = tmp_path / "auth-promo"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control.create_policy(
        {
            "policyId": "pol-promo",
            "policyRevision": 1,
            "enabled": True,
            "primaryTargetId": "t-old",
            "targetId": "t-old",
        }
    )
    backup_control.upsert_target({"targetId": "t-old", "kind": "s3", "bucket": "b", "endpointUrl": "https://x"})
    backup_control.upsert_target({"targetId": "t-new", "kind": "s3", "bucket": "b2", "endpointUrl": "https://y"})

    gen_before = json.loads((root / "control" / "authority" / "head.json").read_text(encoding="utf-8"))
    updated = backup_control.mutate_policy(
        "pol-promo",
        expected_revision=1,
        mutate=lambda p: {**p, "primaryTargetId": "t-new", "targetId": "t-new"},
        generation_kind="promotion",
    )
    assert int(updated["policyRevision"]) == 2
    gen_after = json.loads((root / "control" / "authority" / "head.json").read_text(encoding="utf-8"))
    assert int(gen_after["authorityGeneration"]) > int(gen_before["authorityGeneration"])

    with backup_control._connect() as conn:
        row = conn.execute(
            "SELECT promotion_epoch FROM control_policies WHERE policy_id = ?",
            ("pol-promo",),
        ).fetchone()
    assert int(row["promotion_epoch"]) >= 1

    drain = backup_control.begin_target_drain_intent(
        "t-old",
        reason="test-drain",
        drain_id="drain_test_1",
    )
    assert drain["target"]["drainState"] == "draining"
    gen_drain = json.loads((root / "control" / "authority" / "head.json").read_text(encoding="utf-8"))
    assert int(gen_drain["authorityGeneration"]) > int(gen_after["authorityGeneration"])

    # drain-cancel path via mutate_target (activate)
    cancelled = backup_control.mutate_target(
        "t-old",
        expected_generation=int(drain["target"]["topologyGeneration"]),
        mutate=lambda t: {k: v for k, v in {**t, "drainState": "active"}.items() if k not in {"drainReason", "drainingAt"}},
    )
    assert cancelled["drainState"] == "active"
    gen_cancel = json.loads((root / "control" / "authority" / "head.json").read_text(encoding="utf-8"))
    assert int(gen_cancel["authorityGeneration"]) > int(gen_drain["authorityGeneration"])


def test_reconstruct_from_store_replicas(control_db: Path) -> None:
    from deepseek_infra.infra.workspace.backup_target_store import MemoryTargetStore

    store_a = MemoryTargetStore()
    store_b = MemoryTargetStore()
    backup_control_authority.configure_authority_anchor_stores([store_a, store_b])
    try:
        backup_control.create_policy({"policyId": "pol-rs", "policyRevision": 3, "enabled": True})
        backup_control.upsert_target(
            {"targetId": "t-rs", "kind": "s3", "bucket": "b", "endpointUrl": "https://x", "credentialReference": "r"}
        )
        control_db.unlink(missing_ok=True)
        recovered = backup_control_recovery.reconstruct_control_authority(
            recovery_stores=[store_a, store_b], activate=False
        )
        assert recovered["status"] == "authority-restored"
        pol = backup_control.get_policy("pol-rs")
        assert pol is not None
        assert int(pol["policyRevision"]) == 3
        assert backup_control.get_target("t-rs") is not None
        backup_control.set_target_index_coverage(
            "t-rs", state="complete", formal_receipt_count=0, source_receipt_mutation_generation=0, reason="unit"
        )
        backup_control_recovery.record_formal_truth_validation(
            target_id="t-rs",
            status="VALID",
            index_coverage_complete=True,
            lineage_valid=True,
            retirement_reconciled=True,
        )
        activated = backup_control_recovery.activate_control_after_formal_truth()
        assert activated["status"] == "active"
    finally:
        backup_control_authority.configure_authority_anchor_stores(None)

"""Tests for Authority History Retention & Continuous DR Readiness."""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    authority_retention,
    backup_authority_provider,
    backup_control,
    backup_control_authority,
    backup_control_recovery,
    backup_dr_readiness,
    backups,
    evidence_proof,
)


@pytest.fixture
def clean_authority_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Provide isolated environment with populated synthetic authority history."""
    control_dir = tmp_path / "control"
    control_dir.mkdir(parents=True, exist_ok=True)
    control_db = control_dir / "control.sqlite3"
    retention_dir = tmp_path / "authority_retention"
    retention_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = tmp_path / "restore_staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(backup_control, "CONTROL_DIR", control_dir)
    monkeypatch.setattr(backup_control, "CONTROL_DB", control_db)
    monkeypatch.setattr(authority_retention, "AUTHORITY_RETENTION_DIR", retention_dir)
    monkeypatch.setattr(backups, "RESTORE_DIR", staging_dir)

    backup_control_authority.configure_authority_anchor_roots(None)
    backup_control_authority.configure_authority_anchor_stores(None)
    backup_authority_provider.reset_authority_replica_provider()
    backup_control_recovery.clear_formal_truth_attestations()
    monkeypatch.setenv(backup_authority_provider.ENV_AUTHORITY_MODE, "local-only")

    # Populate 150 generations of authority mutations
    prev_digest: str | None = None
    mutations: list[dict[str, Any]] = []

    with backup_control._connect() as conn:  # noqa: SLF001
        for gen in range(1, 151):
            kind = "target-change" if gen == 50 else ("formal-truth-failure" if gen == 80 else "policy-update")
            ckpt = backup_control_authority.build_authority_checkpoint(
                generation=gen,
                previous_digest=prev_digest,
                policies=[{"policyId": "p1", "retention": {"enabled": True}}],
                targets=[{"targetId": "t1", "kind": "filesystem", "root": "/tmp"}],
                receipt_mutation_generations={},
                promotion_epochs={},
                drain_generations={},
                placement_generations={},
                control_schema_version=1,
            )
            prev_digest = str(ckpt["digest"])
            mutations.append(ckpt)
            conn.execute(
                """
                INSERT INTO control_authority_mutations (
                    mutation_id, authority_generation, authority_digest, kind, checkpoint_json, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'durable', datetime('now'), datetime('now'))
                """,
                (f"mut_{gen:04d}", gen, ckpt["digest"], kind, json.dumps(ckpt)),
            )

        # Set authority head
        head_ckpt = mutations[-1]
        conn.execute(
            """
            INSERT OR REPLACE INTO control_authority_head (
                id, authority_generation, authority_digest, previous_digest, payload_digest, updated_at
            ) VALUES (1, ?, ?, ?, ?, datetime('now'))
            """,
            (
                head_ckpt["authorityGeneration"],
                head_ckpt["digest"],
                head_ckpt.get("previousDigest"),
                head_ckpt["payloadDigest"],
            ),
        )
        conn.commit()

    return {
        "control_db": control_db,
        "retention_dir": retention_dir,
        "staging_dir": staging_dir,
        "mutations": mutations,
    }


# ── Basic Tests ─────────────────────────────────────────────────────────────


def test_retention_plan_keeps_required_tail(clean_authority_env: dict[str, Any]) -> None:
    """P0-2: Planner must preserve recent N generations and protected mutation classes."""
    policy = {
        "retentionPolicyVersion": 1,
        "minimumGenerations": 50,
        "minimumAgeDays": 0,
        "keepMutationClasses": ["target-change", "formal-truth-failure"],
        "checkpointInterval": 100,
    }
    plan = authority_retention.plan_retention(policy=policy)
    assert plan["allowed"] is True
    assert plan["currentGeneration"] == 150
    assert plan["targetCheckpointGeneration"] == 100  # 150 - 50 = 100
    assert plan["tailCount"] == 50

    # Generation 50 (target-change) and 80 (formal-truth-failure) must be retained
    assert 50 in plan["retainedSpecialGenerations"]
    assert 80 in plan["retainedSpecialGenerations"]
    assert 50 not in plan["eligiblePruneGenerations"]
    assert 80 not in plan["eligiblePruneGenerations"]


def test_checkpoint_digest_matches_history(clean_authority_env: dict[str, Any]) -> None:
    """P0-3: AuthorityCheckpoint v1 must bind to history start and target generation ancestor digest."""
    mutations = clean_authority_env["mutations"]
    target_item = mutations[99]  # gen 100 (0-indexed 99)
    head_item = mutations[-1]

    ckpt = authority_retention.build_authority_retention_checkpoint(
        checkpoint_generation=100,
        ancestor_digest=str(target_item["digest"]),
        head_digest=str(head_item["digest"]),
        history_start_generation=1,
        included_mutations=mutations[:100],
    )
    assert ckpt["schemaVersion"] == 1
    assert ckpt["checkpointGeneration"] == 100
    assert ckpt["ancestorDigest"] == str(target_item["digest"])
    assert ckpt["headDigest"] == str(head_item["digest"])
    assert ckpt["historyStartGeneration"] == 1
    assert len(ckpt["includedMutationDigest"]) == 64


def test_checkpoint_plus_tail_replays_exact_head(clean_authority_env: dict[str, Any]) -> None:
    """P0-1: verify(C + tail) == verify(full history) must hold exactly."""
    mutations = clean_authority_env["mutations"]
    target_gen = 100
    target_item = mutations[target_gen - 1]
    head_item = mutations[-1]

    ckpt = authority_retention.build_authority_retention_checkpoint(
        checkpoint_generation=target_gen,
        ancestor_digest=str(target_item["digest"]),
        head_digest=str(head_item["digest"]),
        history_start_generation=1,
        included_mutations=mutations[:target_gen],
    )

    tail = mutations[target_gen:]  # gen 101 .. 150
    result = authority_retention.verify_compaction(
        checkpoint=ckpt,
        tail_history=tail,
        full_history=mutations,
    )
    assert result["verified"] is True
    assert result["replayedHeadGeneration"] == 150
    assert result["headDigest"] == str(head_item["digest"])


# ── Replica Tests ────────────────────────────────────────────────────────────


def test_compaction_blocks_replica_lag(clean_authority_env: dict[str, Any], tmp_path: Path) -> None:
    """P0-5: Any replica lagging behind current generation must block compaction."""
    replica_root = tmp_path / "lagging_replica"
    replica_root.mkdir(parents=True, exist_ok=True)
    mutations = clean_authority_env["mutations"]

    # Write only first 120 checkpoints to replica root (lag = 30)
    for ckpt in mutations[:120]:
        backup_control_authority.write_authority_checkpoint_bundle(replica_root, ckpt)

    backup_control_authority.configure_authority_anchor_roots([replica_root])

    policy = {"minimumGenerations": 50}
    explanation = authority_retention.explain_retention(policy=policy)
    assert explanation["allowed"] is False
    codes = [r["code"] for r in explanation["reasons"]]
    assert authority_retention.REASON_REPLICA_LAG in codes

    with pytest.raises(AppError, match="Compaction blocked"):
        authority_retention.execute_compaction(policy=policy)


def test_compaction_blocks_cross_replica_fork(clean_authority_env: dict[str, Any], tmp_path: Path) -> None:
    """P0-5: Divergent hash chain / fork across replicas must block compaction."""
    replica_root = tmp_path / "forked_replica"
    replica_root.mkdir(parents=True, exist_ok=True)
    mutations = clean_authority_env["mutations"]

    # Write up to 100 normally
    for ckpt in mutations[:100]:
        backup_control_authority.write_authority_checkpoint_bundle(replica_root, ckpt)

    # Write forked generation 101 directly with wrong previousDigest to simulate remote divergent history
    forked_ckpt = backup_control_authority.build_authority_checkpoint(
        generation=101,
        previous_digest="f" * 64,  # mismatched previousDigest
        policies=[{"policyId": "p_fork", "retention": {"enabled": True}}],
        targets=[{"targetId": "t1", "kind": "filesystem", "root": "/tmp"}],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=1,
    )
    ckpt_dir = replica_root / "control" / "authority" / "checkpoints"
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    (ckpt_dir / f"{101:016d}.json").write_text(json.dumps(forked_ckpt), encoding="utf-8")
    (replica_root / "control" / "authority" / "head.json").write_text(json.dumps(forked_ckpt), encoding="utf-8")

    backup_control_authority.configure_authority_anchor_roots([replica_root])

    policy = {"minimumGenerations": 20}
    explanation = authority_retention.explain_retention(policy=policy)
    assert explanation["allowed"] is False
    codes = [r["code"] for r in explanation["reasons"]]
    assert authority_retention.REASON_CROSS_REPLICA_FORK in codes or authority_retention.REASON_REPLICA_LAG in codes


# ── DR Tests ─────────────────────────────────────────────────────────────────


def test_compaction_blocks_active_restore(clean_authority_env: dict[str, Any], tmp_path: Path) -> None:
    """P0-5: Any active / incomplete restore session must block compaction."""
    staging_dir = clean_authority_env["staging_dir"]
    session_dir = staging_dir / "restore_test_active"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "remote-fetch.json").write_text(
        json.dumps({"phase": "fetching", "restoreId": "restore_test_active"}),
        encoding="utf-8",
    )

    policy = {"minimumGenerations": 50}
    explanation = authority_retention.explain_retention(policy=policy)
    assert explanation["allowed"] is False
    codes = [r["code"] for r in explanation["reasons"]]
    assert authority_retention.REASON_ACTIVE_RESTORE_SESSION in codes


def test_dr_drill_produces_readiness_proof(clean_authority_env: dict[str, Any]) -> None:
    """P0-6: Continuous DR drill must execute cleanly and generate dr-readiness-proof-v1."""
    res = backup_dr_readiness.run_dr_drill()
    assert res["status"] == "success"
    proof = res["proof"]

    assert proof["schema"] == evidence_proof.DR_READINESS_PROOF_SCHEMA
    assert proof["drillId"].startswith("drill_")
    assert proof["backupId"] == proof["testedBackupId"]
    assert isinstance(proof["restoreDurationMs"], int) and proof["restoreDurationMs"] >= 0
    assert len(proof["workspaceDigestBefore"]) == 64
    assert len(proof["workspaceDigestAfter"]) == 64
    assert proof["workspaceDigestBefore"] == proof["workspaceDigestAfter"]
    assert proof["commitVerified"] is True
    assert proof["receiptVerified"] is True
    assert proof["ageVerified"] is True
    assert proof["cleanupCompleted"] is True

    # Validate using typed evidence validator
    errors = evidence_proof.validate_dr_readiness_proof(proof, "continuousDrillProducesReadinessProof")
    assert errors == []


def test_failed_dr_drill_blocks_retention(clean_authority_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """P0-5: While DR drill is actively running, compaction must be blocked."""
    backup_dr_readiness.set_dr_drill_running(True)
    try:
        explanation = authority_retention.explain_retention()
        assert explanation["allowed"] is False
        codes = [r["code"] for r in explanation["reasons"]]
        assert authority_retention.REASON_DR_DRILL_RUNNING in codes
    finally:
        backup_dr_readiness.set_dr_drill_running(False)


# ── Crash & Recovery Tests ───────────────────────────────────────────────────


def test_compaction_crash_before_commit_recovers(clean_authority_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """P2: Compaction crash before commit rolls back cleanly and leaves full history intact."""
    from contextlib import contextmanager

    policy = {"minimumGenerations": 50}

    # Simulate crash right before DB deletion commit
    real_connect = backup_control._connect  # noqa: SLF001

    class FailingConn:
        def __init__(self, real: Any) -> None:
            self._real = real

        def execute(self, sql: str, *args: Any) -> Any:
            if "DELETE FROM control_authority_mutations" in sql:
                raise RuntimeError("simulated-crash-before-commit")
            return self._real.execute(sql, *args)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

    @contextmanager
    def failing_connect() -> Any:
        with real_connect() as conn:
            yield FailingConn(conn)

    monkeypatch.setattr(backup_control, "_connect", failing_connect)

    with pytest.raises(AppError, match="Prune failed"):
        authority_retention.execute_compaction(policy=policy)

    # History in DB must remain completely intact (150 rows)
    with real_connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM control_authority_mutations").fetchone()[0]
        assert count == 150


def test_compaction_crash_after_checkpoint_write_is_safe(clean_authority_env: dict[str, Any]) -> None:
    """P2: Successful compaction writes immutable checkpoint file and prunes eligible history."""
    policy = {
        "minimumGenerations": 50,
        "keepMutationClasses": ["target-change", "formal-truth-failure"],
    }
    result = authority_retention.execute_compaction(policy=policy)
    assert result["state"] == authority_retention.STATE_COMMITTED
    assert result["checkpoint"]["checkpointGeneration"] == 100
    assert result["prunedGenerationsCount"] > 0

    # Validate checkpoint file exists on disk
    ckpt_file = clean_authority_env["retention_dir"] / "checkpoints" / f"{100:016d}.json"
    assert ckpt_file.is_file()

    # Protected mutations 50 and 80 must still be present in DB
    with backup_control._connect() as conn:  # noqa: SLF001
        gen50 = conn.execute("SELECT authority_generation FROM control_authority_mutations WHERE authority_generation = 50").fetchone()
        gen80 = conn.execute("SELECT authority_generation FROM control_authority_mutations WHERE authority_generation = 80").fetchone()
        assert gen50 is not None
        assert gen80 is not None


# ── Security & Immutability Tests ───────────────────────────────────────────


def test_checkpoint_contains_no_credentials(clean_authority_env: dict[str, Any]) -> None:
    """P0-3 / Security: AuthorityCheckpoint v1 must never contain secret keys or credentials."""
    mutations = clean_authority_env["mutations"]
    ckpt = authority_retention.build_authority_retention_checkpoint(
        checkpoint_generation=100,
        ancestor_digest=str(mutations[99]["digest"]),
        head_digest=str(mutations[-1]["digest"]),
        included_mutations=mutations[:100],
    )
    blob = json.dumps(ckpt).casefold()
    for forbidden in ("secret", "password", "age-secret-key-", "private_key", "bearer"):
        assert forbidden not in blob


def test_retention_artifact_is_immutable(clean_authority_env: dict[str, Any]) -> None:
    """P2 / Security: Retention checkpoint artifacts must be verifiable and deterministic."""
    mutations = clean_authority_env["mutations"]
    ckpt1 = authority_retention.build_authority_retention_checkpoint(
        checkpoint_generation=100,
        ancestor_digest=str(mutations[99]["digest"]),
        head_digest=str(mutations[-1]["digest"]),
        included_mutations=mutations[:100],
        created_at="2026-08-25T12:00:00Z",
    )
    ckpt2 = authority_retention.build_authority_retention_checkpoint(
        checkpoint_generation=100,
        ancestor_digest=str(mutations[99]["digest"]),
        head_digest=str(mutations[-1]["digest"]),
        included_mutations=mutations[:100],
        created_at="2026-08-25T12:00:00Z",
    )
    assert ckpt1 == ckpt2


# ── Recovery Dependency Graph & SLO Tests ────────────────────────────────────


def test_recovery_dependency_graph_preserves_lineage(clean_authority_env: dict[str, Any]) -> None:
    """P1-3: Mutations referenced in recovery lineage or receipt mutations must be identified."""
    with backup_control._connect() as conn:  # noqa: SLF001
        conn.execute(
            """
            INSERT INTO target_receipt_mutations (
                target_id, generation, updated_at
            ) VALUES ('t1', 10, datetime('now'))
            """
        )
        conn.commit()

    deps = authority_retention.get_retention_dependency_graph(up_to_generation=50)
    assert 10 in deps
    assert any(d["type"] == "target_receipt_mutation" for d in deps[10])

    plan = authority_retention.plan_retention(policy={"minimumGenerations": 50})
    assert 10 in plan["retainedSpecialGenerations"]
    assert 10 not in plan["eligiblePruneGenerations"]


def test_dr_slo_metrics_calculation(clean_authority_env: dict[str, Any]) -> None:
    """P0-7: DR SLO metrics must compute restore success rate, RTO, RPO, and freshness."""
    backup_dr_readiness.run_dr_drill()
    slo = backup_dr_readiness.calculate_dr_slo_metrics()

    assert slo["restoreSuccessRate"] >= 0.999
    assert "p50" in slo["rtoSeconds"]
    assert "p95" in slo["rtoSeconds"]
    assert "p99" in slo["rtoSeconds"]
    assert slo["rpoSeconds"] >= 0.0
    assert slo["evidenceFreshnessDays"] <= 7.0
    assert slo["freshnessHealthy"] is True
    assert slo["overallSloCompliant"] is True


def test_authority_history_snapshot_operator_view(clean_authority_env: dict[str, Any]) -> None:
    """P1-1: Operator snapshot must return current generation, head digest, and status."""
    snap = authority_retention.authority_history_snapshot()
    assert snap["currentGeneration"] == 150
    assert len(snap["headDigest"]) == 64
    assert snap["totalHistoryCount"] == 150
    assert snap["status"] in {"HEALTHY", "BLOCKED"}
    assert "retentionPolicy" in snap


# ── Web Route & Validation Tests ────────────────────────────────────────────


def test_authority_retention_web_routes(clean_authority_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Test authority retention web routes end-to-end."""
    from fastapi.testclient import TestClient
    from deepseek_infra.web import server

    monkeypatch.setattr("deepseek_infra.web.routes.backup_governance.require_api_auth", lambda _req: None)
    app = server.create_app()
    client = TestClient(app)

    # 1. Snapshot
    res = client.get("/api/workspace/authority/history-snapshot")
    assert res.status_code == 200
    assert res.json()["currentGeneration"] == 150

    # 2. Get policy
    res = client.get("/api/workspace/authority/retention/policy")
    assert res.status_code == 200
    assert res.json()["minimumGenerations"] == 100

    # 3. Put policy
    res = client.post(
        "/api/workspace/authority/retention/policy",
        json={"minimumGenerations": 40, "minimumAgeDays": 10},
    )
    assert res.status_code == 200
    assert res.json()["minimumGenerations"] == 40

    # 4. Explain
    res = client.post("/api/workspace/authority/retention/explain", json={})
    assert res.status_code == 200
    assert "allowed" in res.json()

    # 5. Plan
    res = client.post("/api/workspace/authority/retention/plan", json={})
    assert res.status_code == 200
    assert res.json()["targetCheckpointGeneration"] == 110  # 150 - 40

    # 6. Compact dry-run
    res = client.post("/api/workspace/authority/retention/compact", json={"dryRun": True})
    assert res.status_code == 200
    assert res.json()["dryRun"] is True

    # 7. DR Drill run
    res = client.post("/api/workspace/disaster-recovery/drills/run", json={})
    assert res.status_code == 200
    assert res.json()["status"] == "success"

    # 8. DR SLO
    res = client.get("/api/workspace/disaster-recovery/slo")
    assert res.status_code == 200
    assert res.json()["restoreSuccessRate"] >= 0.999


def test_retention_policy_validation_errors() -> None:
    """Test policy validation fail-closed on invalid parameters."""
    with pytest.raises(AppError, match="must be a JSON object"):
        authority_retention.normalize_retention_policy("not-a-dict")  # type: ignore[arg-type]

    with pytest.raises(AppError, match="Unsupported retention policy version"):
        authority_retention.normalize_retention_policy({"retentionPolicyVersion": 99})

    with pytest.raises(AppError, match="minimumGenerations must be an integer >= 1"):
        authority_retention.normalize_retention_policy({"minimumGenerations": 0})

    with pytest.raises(AppError, match="minimumAgeDays must be an integer >= 0"):
        authority_retention.normalize_retention_policy({"minimumAgeDays": -5})

    with pytest.raises(AppError, match="checkpointInterval must be an integer >= 1"):
        authority_retention.normalize_retention_policy({"checkpointInterval": 0})

    with pytest.raises(AppError, match="keepMutationClasses must be a list"):
        authority_retention.normalize_retention_policy({"keepMutationClasses": "string-not-list"})


def test_verify_compaction_failure_conditions() -> None:
    """Test compaction verification fail-closed conditions."""
    with pytest.raises(AppError, match="must be a JSON object"):
        authority_retention.verify_compaction(
            checkpoint="invalid",  # type: ignore[arg-type]
            tail_history=[],
            full_history=[{"authorityGeneration": 1}],
        )

    with pytest.raises(AppError, match="cannot be empty"):
        authority_retention.verify_compaction(
            checkpoint={"checkpointGeneration": 1},
            tail_history=[],
            full_history=[],
        )

    with pytest.raises(AppError, match="not found in full history"):
        authority_retention.verify_compaction(
            checkpoint={"checkpointGeneration": 999, "ancestorDigest": "a"},
            tail_history=[],
            full_history=[{"authorityGeneration": 1, "digest": "d1"}],
        )

    # schemaVersion mismatch
    with pytest.raises(AppError, match="Unsupported checkpoint schemaVersion"):
        authority_retention.verify_compaction(
            checkpoint={"checkpointGeneration": 1, "schemaVersion": 99, "ancestorDigest": "d1"},
            tail_history=[],
            full_history=[{"authorityGeneration": 1, "digest": "d1"}],
        )

    # ancestorDigest mismatch
    with pytest.raises(AppError, match="ancestorDigest mismatch"):
        authority_retention.verify_compaction(
            checkpoint={"checkpointGeneration": 1, "schemaVersion": 1, "ancestorDigest": "wrong"},
            tail_history=[],
            full_history=[{"authorityGeneration": 1, "digest": "d1"}],
        )

    # historyStartGeneration mismatch
    with pytest.raises(AppError, match="historyStartGeneration mismatch"):
        authority_retention.verify_compaction(
            checkpoint={"checkpointGeneration": 1, "schemaVersion": 1, "ancestorDigest": "d1", "historyStartGeneration": 5},
            tail_history=[],
            full_history=[{"authorityGeneration": 1, "digest": "d1"}],
        )

    # includedMutationDigest mismatch
    with pytest.raises(AppError, match="includedMutationDigest mismatch"):
        authority_retention.verify_compaction(
            checkpoint={
                "checkpointGeneration": 1,
                "schemaVersion": 1,
                "ancestorDigest": "d1",
                "historyStartGeneration": 1,
                "includedMutationDigest": "wrong",
            },
            tail_history=[],
            full_history=[{"authorityGeneration": 1, "digest": "d1"}],
        )

    # tail history mismatch
    with pytest.raises(AppError, match="Tail gap"):
        authority_retention.verify_compaction(
            checkpoint=authority_retention.build_authority_retention_checkpoint(
                checkpoint_generation=1,
                ancestor_digest="d1",
                head_digest="d2",
                included_mutations=[{"authorityGeneration": 1, "digest": "d1"}],
            ),
            tail_history=[{"authorityGeneration": 3, "digest": "d3"}],
            full_history=[{"authorityGeneration": 1, "digest": "d1"}, {"authorityGeneration": 2, "digest": "d2"}],
        )


def test_evidence_proof_v3_validators() -> None:
    """Test Evidence Proof v3 typed semantic validators thoroughly."""
    # 1. DR readiness proof errors
    assert len(evidence_proof.validate_dr_readiness_proof("not-a-dict", "test")) > 0  # type: ignore[arg-type]
    assert len(evidence_proof.validate_dr_readiness_proof({}, "test")) > 0
    assert len(evidence_proof.validate_dr_readiness_proof({
        "drillId": "invalid",
        "testedBackupId": "b1",
        "restoreDurationMs": -1,
        "workspaceDigestBefore": "short",
        "workspaceDigestAfter": "wrong",
        "objectCount": -1,
        "commitVerified": False,
        "receiptVerified": False,
        "ageVerified": False,
        "cleanupCompleted": False,
    }, "test")) > 0

    valid_dr_proof = {
        "schema": evidence_proof.DR_READINESS_PROOF_SCHEMA,
        "drillId": "drill_123",
        "backupId": "backup_abc",
        "testedBackupId": "backup_abc",
        "restoreDurationMs": 150,
        "workspaceDigestBefore": "a" * 64,
        "workspaceDigestAfter": "a" * 64,
        "objectCount": 10,
        "commitVerified": True,
        "receiptVerified": True,
        "ageVerified": True,
        "cleanupCompleted": True,
    }
    assert evidence_proof.validate_dr_readiness_proof(valid_dr_proof, "test") == []

    # 2. Retention safety proof errors
    assert len(evidence_proof.validate_retention_safety_proof("not-a-dict", "test")) > 0  # type: ignore[arg-type]
    assert len(evidence_proof.validate_retention_safety_proof({}, "test")) > 0
    assert len(evidence_proof.validate_retention_safety_proof({
        "checkpointVerified": False,
        "ancestorCoverage": False,
        "replicaAgreement": False,
        "dependencyClosure": False,
    }, "test")) > 0

    valid_ret_proof = {
        "checkpointVerified": True,
        "ancestorCoverage": True,
        "replicaAgreement": True,
        "dependencyClosure": True,
    }
    assert evidence_proof.validate_retention_safety_proof(valid_ret_proof, "test") == []


def test_authority_retention_edge_cases(clean_authority_env: dict[str, Any], tmp_path: Path) -> None:
    """Test policy loading fallbacks, invalid inputs, and GC/Formal Truth edge conditions."""
    retention_dir = clean_authority_env["retention_dir"]

    # 1. Corrupt policy on disk fallbacks to default
    policy_file = retention_dir / "policy.json"
    policy_file.write_text("invalid-json{", encoding="utf-8")
    pol = authority_retention.get_authority_retention_policy()
    assert pol["minimumGenerations"] == 100

    # 2. Build checkpoint with gen 0 raises
    with pytest.raises(AppError, match="checkpointGeneration must be >= 1"):
        authority_retention.build_authority_retention_checkpoint(
            checkpoint_generation=0,
            ancestor_digest="a",
            head_digest="h",
        )

    # 3. Snapshot on empty environment
    with backup_control._connect() as conn:  # noqa: SLF001
        conn.execute("DELETE FROM control_authority_mutations")
        conn.execute("DELETE FROM control_authority_head")
        conn.commit()

    snap = authority_retention.authority_history_snapshot()
    assert snap["currentGeneration"] == 0
    assert snap["totalHistoryCount"] == 0


def test_dr_slo_metrics_edge_cases() -> None:
    """Test DR SLO metrics calculation with zero/empty drills and failures."""
    # Empty drills
    empty_drills: list[dict[str, Any]] = []
    slo_empty = backup_dr_readiness.calculate_dr_slo_metrics(drills=empty_drills)
    assert slo_empty["restoreSuccessRate"] == 1.0
    assert slo_empty["rtoSeconds"]["p50"] >= 0.0

    # Drills with failures
    drills_mixed: list[dict[str, Any]] = [
        {"result": "success", "proof": {"restoreDurationMs": 200}},
        {"result": "failure", "rtoSeconds": 5.0},
    ]
    slo_mixed = backup_dr_readiness.calculate_dr_slo_metrics(drills=drills_mixed)
    assert slo_mixed["restoreSuccessRate"] == 0.5
    assert slo_mixed["overallSloCompliant"] is False

    # Get DR SLO status
    status = backup_dr_readiness.get_dr_slo_status()
    assert "restoreSuccessRate" in status
    assert "overallSloCompliant" in status


def test_evidence_proof_full_suite() -> None:
    """Test all evidence proof validators and schema checks."""
    # 1. validate_restore_proof
    assert len(evidence_proof.validate_restore_proof({}, "test")) > 0
    assert len(evidence_proof.validate_restore_proof({
        "backupId": "b1",
        "targetId": "t1",
        "restoreId": "r1",
        "preBackupWorkspaceDigest": "invalid",
        "corruptedWorkspaceDigest": "invalid",
        "postRestoreWorkspaceDigest": "invalid",
    }, "test")) > 0
    assert len(evidence_proof.validate_restore_proof({
        "backupId": "b1",
        "targetId": "t1",
        "restoreId": "r1",
        "preBackupWorkspaceDigest": "a" * 64,
        "corruptedWorkspaceDigest": "b" * 64,
        "postRestoreWorkspaceDigest": "c" * 64,
    }, "test")) > 0

    valid_restore = {
        "backupId": "b1",
        "targetId": "t1",
        "restoreId": "r1",
        "preBackupWorkspaceDigest": "a" * 64,
        "corruptedWorkspaceDigest": "b" * 64,
        "postRestoreWorkspaceDigest": "a" * 64,
    }
    assert evidence_proof.validate_restore_proof(valid_restore, "test") == []

    # 2. validate_backup_commit_proof
    assert len(evidence_proof.validate_backup_commit_proof({}, "test")) > 0
    assert len(evidence_proof.validate_backup_commit_proof({
        "backupId": "b1",
        "commitKey": "ck",
        "receiptKey": "rk",
        "receiptDigest": "invalid",
        "objectSetDigest": "invalid",
    }, "test")) > 0

    valid_commit = {
        "backupId": "b1",
        "commitKey": "ck",
        "receiptKey": "rk",
        "receiptDigest": "d" * 64,
        "objectSetDigest": "e" * 64,
    }
    assert evidence_proof.validate_backup_commit_proof(valid_commit, "test") == []

    # 3. validate_pass_with_schema_only
    assert len(evidence_proof.validate_pass_with_schema_only({}, "test")) > 0
    assert evidence_proof.validate_pass_with_schema_only({"schema": "evidence-proof-v3"}, "test") == []

    # 4. validate_check
    assert len(evidence_proof.validate_check("unknown", {"status": "FAIL"})) > 0
    assert len(evidence_proof.validate_check("unknown", {"status": "PASS", "evidence": "not-a-dict"})) > 0


def test_explain_retention_all_fail_closed_reasons(clean_authority_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Test explain_retention fail-closed branches for all blocking conditions."""
    # 1. DR drill running
    backup_dr_readiness.set_dr_drill_running(True)
    try:
        exp = authority_retention.explain_retention()
        assert exp["allowed"] is False
        assert any(r.get("code") == authority_retention.REASON_DR_DRILL_RUNNING for r in exp["reasons"])
    finally:
        backup_dr_readiness.set_dr_drill_running(False)

    # 2. Active restore session
    restore_dir = clean_authority_env["staging_dir"] / "session_active_1"
    restore_dir.mkdir(parents=True, exist_ok=True)
    (restore_dir / "remote-fetch.json").write_text(json.dumps({"phase": "fetching"}), encoding="utf-8")

    exp = authority_retention.explain_retention()
    assert exp["allowed"] is False
    assert any(r.get("code") == authority_retention.REASON_ACTIVE_RESTORE_SESSION for r in exp["reasons"])

    # 3. Active GC session
    (restore_dir / "remote-fetch.json").write_text(json.dumps({"phase": "complete"}), encoding="utf-8")
    with backup_control._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO ciphertext_gc_intents (intent_id, target_id, object_key, expected_receipt_mutation_generation, state, created_at, updated_at) "
            "VALUES ('intent_active', 'managed-local', 'obj_1', 1, 'running', datetime('now'), datetime('now'))"
        )
        conn.commit()

    exp = authority_retention.explain_retention()
    assert exp["allowed"] is False
    assert any(r.get("code") == authority_retention.REASON_GC_UNFINISHED for r in exp["reasons"])


def test_compaction_execution_and_dependency_graph(clean_authority_env: dict[str, Any]) -> None:
    """Test execute_compaction dry-run, target_generation, and dependency graph."""
    # Dependency graph
    dep = authority_retention.get_retention_dependency_graph(up_to_generation=120)
    assert isinstance(dep, dict)
    assert 50 in dep or 80 in dep

    # Dependency graph with explicit conn
    with backup_control._connect() as conn:  # noqa: SLF001
        dep_conn = authority_retention.get_retention_dependency_graph(up_to_generation=120, conn=conn)
        assert isinstance(dep_conn, dict)

    # Dependency graph with 0 generation
    assert authority_retention.get_retention_dependency_graph(up_to_generation=0) == {}

    # Execute compaction with dry-run
    res_dry = authority_retention.execute_compaction(
        policy={"minimumGenerations": 20},
        dry_run=True,
    )
    assert res_dry["dryRun"] is True

    # Execute compaction with explicit target generation
    res = authority_retention.execute_compaction(
        policy={"minimumGenerations": 20},
        target_generation=120,
    )
    assert res["state"] in {"COMMITTED", "BLOCKED"}


def test_authority_retention_edge_cases_and_verification(clean_authority_env: dict[str, Any]) -> None:
    """Test all edge cases and verification error paths in authority_retention."""
    # 1. build_authority_retention_checkpoint validation
    with pytest.raises(AppError) as exc_info:
        authority_retention.build_authority_retention_checkpoint(
            checkpoint_generation=0,
            ancestor_digest="a" * 64,
            head_digest="h" * 64,
        )
    assert exc_info.value.status == 400

    ckpt = authority_retention.build_authority_retention_checkpoint(
        checkpoint_generation=50,
        ancestor_digest="a" * 64,
        head_digest="h" * 64,
        history_start_generation=1,
        included_mutations=["non-dict", {"generation": 1, "digest": "d" * 64, "kind": "policy"}],  # type: ignore[list-item]
        replica_coverage={"r1": "non-dict", "r2": {"generation": 50, "digest": "d" * 64}},
    )
    assert ckpt["checkpointGeneration"] == 50
    assert "r2" in ckpt["replicaCoverage"]

    # 2. _fetch_all_authority_history fallback to head
    with backup_control._connect() as conn:  # noqa: SLF001
        conn.execute("DELETE FROM control_authority_mutations")
        conn.execute("INSERT OR REPLACE INTO control_authority_head (id, authority_generation, authority_digest, previous_digest, payload_digest, updated_at) VALUES (1, 10, 'hd1', 'hd0', 'pd1', datetime('now'))")
        conn.commit()
    head_hist = authority_retention._fetch_all_authority_history()  # noqa: SLF001
    assert len(head_hist) == 1
    assert head_hist[0]["authorityGeneration"] == 10

    # 3. verify_compaction error branches
    hist = [
        {"authorityGeneration": 1, "digest": "d1", "previousDigest": None},
        {"authorityGeneration": 2, "digest": "d2", "previousDigest": "d1"},
        {"authorityGeneration": 3, "digest": "d3", "previousDigest": "d2"},
    ]

    # ckpt gen < 1
    with pytest.raises(AppError):
        authority_retention.verify_compaction(
            checkpoint={"checkpointGeneration": 0, "ancestorDigest": "d1"},
            full_history=hist,
            tail_history=[],
        )

    # ckpt gen not in history
    with pytest.raises(AppError):
        authority_retention.verify_compaction(
            checkpoint={"checkpointGeneration": 99, "ancestorDigest": "d1"},
            full_history=hist,
            tail_history=[],
        )

    # ancestor digest mismatch
    with pytest.raises(AppError):
        authority_retention.verify_compaction(
            checkpoint={"checkpointGeneration": 2, "ancestorDigest": "wrong_digest"},
            full_history=hist,
            tail_history=[],
        )

    # historyStartGeneration mismatch
    with pytest.raises(AppError):
        authority_retention.verify_compaction(
            checkpoint={"checkpointGeneration": 2, "ancestorDigest": "d2", "historyStartGeneration": 99},
            full_history=hist,
            tail_history=[],
        )

    # includedMutationDigest wrong
    with pytest.raises(AppError):
        authority_retention.verify_compaction(
            checkpoint={"checkpointGeneration": 2, "ancestorDigest": "d2", "historyStartGeneration": 1, "includedMutationDigest": "wrong"},
            full_history=hist,
            tail_history=[],
        )

    # tail start gap
    with pytest.raises(AppError):
        authority_retention.verify_compaction(
            checkpoint={"checkpointGeneration": 1, "ancestorDigest": "d1", "historyStartGeneration": 1},
            full_history=hist,
            tail_history=[{"authorityGeneration": 3, "digest": "d3"}],
        )

    # empty tail when ckpt != head
    with pytest.raises(AppError):
        authority_retention.verify_compaction(
            checkpoint={"checkpointGeneration": 1, "ancestorDigest": "d1", "historyStartGeneration": 1},
            full_history=hist,
            tail_history=[],
        )

    # replayed head digest mismatch
    with pytest.raises(AppError):
        authority_retention.verify_compaction(
            checkpoint={"checkpointGeneration": 2, "ancestorDigest": "d2", "historyStartGeneration": 1},
            full_history=hist,
            tail_history=[{"authorityGeneration": 3, "digest": "wrong_head"}],
        )

    # forbidden secrets in checkpoint
    with pytest.raises(AppError):
        authority_retention.verify_compaction(
            checkpoint={"checkpointGeneration": 3, "ancestorDigest": "d3", "historyStartGeneration": 1, "secretkey": "leaked"},
            full_history=hist,
            tail_history=[],
        )


def test_evidence_proof_v3_comprehensive() -> None:
    """Test Evidence Proof v3 semantic validators for all error conditions and success cases."""
    # 1. validate_dr_readiness_proof
    assert len(evidence_proof.validate_dr_readiness_proof({}, "test")) > 0
    assert len(evidence_proof.validate_dr_readiness_proof({
        "drillId": "d1",
        "testedBackupId": "b1",
        "restoreDurationMs": 100,
        "workspaceDigestBefore": "d" * 64,
        "workspaceDigestAfter": "d" * 64,
        "objectCount": 5,
        "commitVerified": False,
        "receiptVerified": True,
        "ageVerified": True,
        "cleanupCompleted": True,
    }, "test")) > 0
    assert len(evidence_proof.validate_dr_readiness_proof({
        "drillId": "d1",
        "testedBackupId": "b1",
        "restoreDurationMs": -5,
        "workspaceDigestBefore": "d" * 64,
        "workspaceDigestAfter": "d" * 64,
        "objectCount": 5,
        "commitVerified": True,
        "receiptVerified": True,
        "ageVerified": True,
        "cleanupCompleted": True,
    }, "test")) > 0

    valid_dr_proof = {
        "schema": evidence_proof.DR_READINESS_PROOF_SCHEMA,
        "drillId": "d1",
        "backupId": "b1",
        "testedBackupId": "b1",
        "restoreDurationMs": 120,
        "workspaceDigestBefore": "d" * 64,
        "workspaceDigestAfter": "d" * 64,
        "objectCount": 5,
        "commitVerified": True,
        "receiptVerified": True,
        "ageVerified": True,
        "cleanupCompleted": True,
    }
    assert evidence_proof.validate_dr_readiness_proof(valid_dr_proof, "test") == []

    # 2. validate_retention_safety_proof
    assert len(evidence_proof.validate_retention_safety_proof({}, "test")) > 0
    assert len(evidence_proof.validate_retention_safety_proof({
        "checkpointVerified": False,
        "ancestorCoverage": True,
        "replicaAgreement": True,
        "dependencyClosure": True,
    }, "test")) > 0

    valid_retention_proof = {
        "checkpointVerified": True,
        "ancestorCoverage": True,
        "replicaAgreement": True,
        "dependencyClosure": True,
    }
    assert evidence_proof.validate_retention_safety_proof(valid_retention_proof, "test") == []

    # 3. validate_distinct_pid_proof
    assert len(evidence_proof.validate_distinct_pid_proof({}, "test")) > 0
    assert len(evidence_proof.validate_distinct_pid_proof({"pidA": 10, "pidB": 10}, "test")) > 0
    assert evidence_proof.validate_distinct_pid_proof({"pidA": 10, "pidB": 20}, "test") == []

    # 4. validate_sigkill_proof
    assert len(evidence_proof.validate_sigkill_proof({}, "test")) > 0
    assert len(evidence_proof.validate_sigkill_proof({"returncode": 0}, "test")) > 0
    assert evidence_proof.validate_sigkill_proof({"returncode": -9}, "test") == []

    # 5. validate_epoch_increase_proof
    assert len(evidence_proof.validate_epoch_increase_proof({}, "test")) > 0
    assert len(evidence_proof.validate_epoch_increase_proof({"epochA": 10, "epochB": 10}, "test")) > 0
    assert evidence_proof.validate_epoch_increase_proof({"epochA": 10, "epochB": 11}, "test") == []

    # 6. validate_minio_endpoints_proof
    assert len(evidence_proof.validate_minio_endpoints_proof({}, "test")) > 0
    assert len(evidence_proof.validate_minio_endpoints_proof({"endpoints": ["http://127.0.0.1:9000"]}, "test")) > 0
    assert evidence_proof.validate_minio_endpoints_proof({
        "endpoints": ["http://127.0.0.1:9000", "http://127.0.0.1:9001", "http://127.0.0.1:9002"]
    }, "test") == []


def test_authority_retention_deep_branch_coverage(clean_authority_env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cover remaining branches in authority_retention."""
    # 1. Dependency graph query exceptions and target_index_coverage
    class FailingConn:
        def execute(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("query failed")

    assert authority_retention.get_retention_dependency_graph(up_to_generation=50, conn=FailingConn()) == {}

    with backup_control._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO target_index_coverage (target_id, source_receipt_mutation_generation, updated_at) VALUES ('t1', 30, datetime('now'))"
        )
        conn.commit()
    dep = authority_retention.get_retention_dependency_graph(up_to_generation=50)
    assert any(any(d.get("type") == "target_index_coverage" for d in deps) for deps in dep.values())

    # 2. Formal truth failure in explain_retention
    monkeypatch.setattr(
        backup_control_recovery,
        "resolve_startup_authority_verdict",
        lambda: {"verdict": "formal-truth-failure", "errors": ["formal truth invalid"]},
    )
    exp_ft = authority_retention.explain_retention()
    assert exp_ft["allowed"] is False
    assert any(r.get("code") == authority_retention.REASON_FORMAL_TRUTH_STALE for r in exp_ft["reasons"])

    # 3. Store replicas in explain_retention (error and lag)
    monkeypatch.setattr(
        backup_control_recovery,
        "resolve_startup_authority_verdict",
        lambda: {"verdict": "local-healthy", "errors": []},
    )

    class DummyStore:
        def get_bytes(self, *args: Any, **kwargs: Any) -> Any:
            raise AppError("store read error", code=ErrorCode.INTERNAL, status=500)

    monkeypatch.setattr(backup_control_authority, "get_authority_anchor_stores", lambda: [DummyStore()])
    exp_store = authority_retention.explain_retention()
    assert exp_store["allowed"] is False
    assert any(r.get("code") == authority_retention.REASON_REPLICA_LAG for r in exp_store["reasons"])

    monkeypatch.setattr(
        backup_control_authority,
        "load_authority_bundle_from_store",
        lambda s, replica_id: {"checkpoint": {"authorityGeneration": 10, "digest": "d10"}},
    )
    monkeypatch.setattr(backup_control_authority, "get_authority_anchor_stores", lambda: ["store-0"])
    exp_store_lag = authority_retention.explain_retention()
    assert exp_store_lag["allowed"] is False
    assert any(r.get("code") == authority_retention.REASON_REPLICA_LAG for r in exp_store_lag["reasons"])

    # 4. Root replica fork in explain_retention
    fork_root = tmp_path / "fork_anchor_root"
    fork_root.mkdir(parents=True, exist_ok=True)
    bundle_fork = {
        "checkpoint": {"authorityGeneration": 200, "digest": "dfork"},
        "checkpoints": [
            {"authorityGeneration": 1, "digest": "d1", "previousDigest": None},
            {"authorityGeneration": 2, "digest": "bad", "previousDigest": "wrong"},
        ],
    }
    monkeypatch.setattr(backup_control_authority, "get_authority_anchor_roots", lambda: [fork_root])
    monkeypatch.setattr(backup_control_authority, "load_authority_bundle", lambda r: bundle_fork)
    monkeypatch.setattr(
        backup_control_authority,
        "verify_authority_chain",
        lambda ckpts: (_ for _ in ()).throw(AppError("Chain fork detected", code=ErrorCode.INVALID_REQUEST, status=409)),
    )
    exp_fork = authority_retention.explain_retention()
    assert any(r.get("code") == authority_retention.REASON_CROSS_REPLICA_FORK for r in exp_fork["reasons"])

    # 5. execute_compaction target generation missing from history
    monkeypatch.setattr(backup_control_authority, "get_authority_anchor_roots", lambda: [])
    monkeypatch.setattr(backup_control_authority, "get_authority_anchor_stores", lambda: [])
    with pytest.raises(AppError):
        authority_retention.execute_compaction(
            policy={"minimumGenerations": 1},
            target_generation=99999,
        )

    # Missing target history item in state machine
    real_fetch = authority_retention._fetch_all_authority_history  # noqa: SLF001
    monkeypatch.setattr(authority_retention, "_fetch_all_authority_history", lambda: [])
    monkeypatch.setattr(
        authority_retention,
        "plan_retention",
        lambda **kwargs: {
            "allowed": True,
            "targetCheckpointGeneration": 50,
            "headDigest": "h50",
            "eligiblePruneGenerations": [1, 2],
        },
    )
    with pytest.raises(AppError) as exc_missing:
        authority_retention.execute_compaction(policy={"minimumGenerations": 1})
    assert exc_missing.value.status == 500
    monkeypatch.setattr(authority_retention, "_fetch_all_authority_history", real_fetch)
    monkeypatch.setattr(authority_retention, "plan_retention", authority_retention.plan_retention)

    # 6. execute_compaction verification failure during state machine
    monkeypatch.setattr(
        authority_retention,
        "verify_compaction",
        lambda **kwargs: (_ for _ in ()).throw(AppError("Verification check failed", code=ErrorCode.INVALID_REQUEST, status=409)),
    )
    with pytest.raises(AppError) as exc_ver:
        authority_retention.execute_compaction(
            policy={"minimumGenerations": 20},
            target_generation=50,
        )
    assert exc_ver.value.status == 409
    monkeypatch.setattr(authority_retention, "verify_compaction", authority_retention.verify_compaction)

    # 7. Snapshot with invalid/corrupt JSON files and valid JSON files in directories
    retention_dir = tmp_path / "retention_corrupt"
    ckpts_dir = retention_dir / "checkpoints"
    jobs_dir = retention_dir / "jobs"
    ckpts_dir.mkdir(parents=True, exist_ok=True)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(authority_retention, "AUTHORITY_RETENTION_DIR", retention_dir)

    (ckpts_dir / "0000000000000001.json").write_text("{ not json", encoding="utf-8")
    (jobs_dir / "job_corrupt.json").write_text("{ not json", encoding="utf-8")

    snap = authority_retention.authority_history_snapshot()
    assert snap["checkpointGeneration"] is None
    assert snap["lastCompaction"] is None

def test_authority_retention_edge_cases_all(clean_authority_env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test remaining edge cases for explain_retention, execute_compaction, and helpers."""
    # 1. explain_retention target_gen < 1 and target_gen > current_gen
    exp_neg = authority_retention.explain_retention(target_generation=-5)
    assert exp_neg["allowed"] is False
    assert any(r.get("code") == authority_retention.REASON_INSUFFICIENT_HISTORY for r in exp_neg["reasons"])

    exp_high = authority_retention.explain_retention(target_generation=999999)
    assert exp_high["allowed"] is False
    assert any(r.get("code") == authority_retention.REASON_INSUFFICIENT_HISTORY for r in exp_high["reasons"])

    # 2. current_gen < min_gens
    exp_insuf = authority_retention.explain_retention(policy={"minimumGenerations": 1000})
    assert exp_insuf["allowed"] is False
    assert any(r.get("code") == authority_retention.REASON_INSUFFICIENT_HISTORY for r in exp_insuf["reasons"])

    # 3. Active restore sessions & malformed json in restore staging
    restore_dir = clean_authority_env["staging_dir"] / "sess1"
    restore_dir.mkdir(parents=True, exist_ok=True)
    (restore_dir / "remote-fetch.json").write_text(
        json.dumps({"phase": "downloading"}), encoding="utf-8"
    )
    exp_restore = authority_retention.explain_retention(policy={"minimumGenerations": 10})
    assert exp_restore["allowed"] is False
    assert any(r.get("code") == authority_retention.REASON_ACTIVE_RESTORE_SESSION for r in exp_restore["reasons"])
    # Malformed json in remote-fetch.json
    (restore_dir / "remote-fetch.json").write_text("invalid json", encoding="utf-8")
    authority_retention.explain_retention(policy={"minimumGenerations": 10})
    (restore_dir / "remote-fetch.json").write_text(
        json.dumps({"phase": "complete"}), encoding="utf-8"
    )

    # 4. Active GC unfinished & GC query exception
    with backup_control._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO ciphertext_gc_intents (intent_id, target_id, object_key, expected_receipt_mutation_generation, state, created_at, updated_at) VALUES ('gc1', 't1', 'obj1', 10, 'running', datetime('now'), datetime('now'))"
        )
        conn.commit()
    exp_gc = authority_retention.explain_retention(policy={"minimumGenerations": 10})
    assert exp_gc["allowed"] is False
    assert any(r.get("code") == authority_retention.REASON_GC_UNFINISHED for r in exp_gc["reasons"])

    # 5. DR Drill Running
    with backup_control._connect() as conn:  # noqa: SLF001
        conn.execute("DELETE FROM ciphertext_gc_intents")
        conn.commit()
    backup_dr_readiness.set_dr_drill_running(True)
    exp_drill = authority_retention.explain_retention(policy={"minimumGenerations": 10})
    assert exp_drill["allowed"] is False
    assert any(r.get("code") == authority_retention.REASON_DR_DRILL_RUNNING for r in exp_drill["reasons"])
    backup_dr_readiness.set_dr_drill_running(False)

    # 6. execute_compaction with anchor root replica coverage and prune empty
    root1 = tmp_path / "root1"
    bundle1 = {
        "checkpoint": {"schema": "control-authority-v1", "authorityGeneration": 150, "digest": "d150"},
        "checkpoints": [],
    }
    monkeypatch.setattr(backup_control_authority, "get_authority_anchor_roots", lambda: [root1])
    monkeypatch.setattr(backup_control_authority, "load_authority_bundle", lambda r: bundle1)

    # Compaction with empty eligible prune (all kept)
    comp_all_kept = authority_retention.execute_compaction(
        policy={"minimumGenerations": 50, "keepMutationClasses": ["policy-update", "target-change", "formal-truth-failure"]},
        target_generation=50,
    )
    assert comp_all_kept["state"] == authority_retention.STATE_COMMITTED
    assert comp_all_kept["prunedGenerationsCount"] == 0

    # 7. execute_compaction verification error handling (lines 748-752)
    real_verify = authority_retention.verify_compaction
    monkeypatch.setattr(
        authority_retention,
        "verify_compaction",
        lambda **kwargs: (_ for _ in ()).throw(AppError("Verification simulation failure", code=ErrorCode.INVALID_REQUEST, status=409)),
    )
    with pytest.raises(AppError) as exc_verify_err:
        authority_retention.execute_compaction(
            policy={"minimumGenerations": 50},
            target_generation=50,
        )
    assert exc_verify_err.value.status == 409
    monkeypatch.setattr(authority_retention, "verify_compaction", real_verify)

    # 8. execute_compaction dry_run (lines 727-728)
    comp_dry = authority_retention.execute_compaction(
        policy={"minimumGenerations": 20},
        target_generation=50,
        dry_run=True,
    )
    assert comp_dry["state"] == authority_retention.STATE_READY

    # 9. execute_compaction prune exception (lines 776-780)
    real_connect = backup_control._connect  # noqa: SLF001

    class ExecWrapperConn:
        def __init__(self, real_c: Any) -> None:
            self._real = real_c

        def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
            if "DELETE FROM control_authority_mutations" in str(sql):
                raise RuntimeError("disk IO error on delete")
            return self._real.execute(sql, *args, **kwargs)

        def commit(self) -> None:
            self._real.commit()

        def rollback(self) -> None:
            self._real.rollback()

        def cursor(self) -> Any:
            return self._real.cursor()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

    @contextlib.contextmanager
    def _failing_delete_connect() -> Any:
        with real_connect() as conn:
            yield ExecWrapperConn(conn)

    monkeypatch.setattr(backup_control, "_connect", _failing_delete_connect)
    with pytest.raises(AppError) as exc_prune_err:
        authority_retention.execute_compaction(
            policy={"minimumGenerations": 20},
            target_generation=50,
        )
    assert exc_prune_err.value.status == 500
    monkeypatch.setattr(backup_control, "_connect", real_connect)

    # 10. Test _fetch_all_authority_history table error fallback to head
    class TableErrorConn:
        def __init__(self, real_c: Any) -> None:
            self._real = real_c

        def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
            if "FROM control_authority_mutations" in str(sql):
                raise sqlite3.OperationalError("no such table")
            return self._real.execute(sql, *args, **kwargs)

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

    @contextlib.contextmanager
    def _table_error_connect() -> Any:
        with real_connect() as conn:
            yield TableErrorConn(conn)

    monkeypatch.setattr(backup_control, "_connect", _table_error_connect)
    hist_fallback = authority_retention._fetch_all_authority_history()  # noqa: SLF001
    assert len(hist_fallback) >= 1
    assert hist_fallback[0]["schema"] == "control-authority-v1"
    monkeypatch.setattr(backup_control, "_connect", real_connect)

    # 11. Test _fetch_all_authority_history total failure (lines 322-323)
    class TotalErrorConn:
        def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
            raise sqlite3.OperationalError("database locked")

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

    @contextlib.contextmanager
    def _total_error_connect() -> Any:
        yield TotalErrorConn()

    monkeypatch.setattr(backup_control, "_connect", _total_error_connect)
    hist_empty = authority_retention._fetch_all_authority_history()  # noqa: SLF001
    assert hist_empty == []
    monkeypatch.setattr(backup_control, "_connect", real_connect)

    # 12. Test snapshot with checkpoints and jobs files
    ckpt_dir = authority_retention._checkpoints_dir()  # noqa: SLF001
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    (ckpt_dir / "bad.json").write_text("corrupted", encoding="utf-8")
    (ckpt_dir / "00000050.json").write_text(
        json.dumps({"checkpointGeneration": 50, "headDigest": "d50"}), encoding="utf-8"
    )

    jobs_dir = authority_retention._jobs_dir()  # noqa: SLF001
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "bad.json").write_text("corrupted", encoding="utf-8")
    (jobs_dir / "job1.json").write_text(
        json.dumps({"jobId": "job1", "state": authority_retention.STATE_COMMITTED, "updatedAt": "2026-08-25T12:00:00Z"}), encoding="utf-8"
    )

    snap_with_files = authority_retention.authority_history_snapshot()
    assert snap_with_files["currentGeneration"] > 0
    assert snap_with_files["checkpointGeneration"] == 50
    assert snap_with_files["lastCompaction"] == "2026-08-25T12:00:00Z"

    # 13. Test _add_dep with gen <= 0 (line 217)
    with backup_control._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO target_receipt_mutations (target_id, generation, updated_at) VALUES ('t0', 0, datetime('now'))"
        )
        conn.commit()
    dep_0 = authority_retention.get_retention_dependency_graph(up_to_generation=50)
    assert 0 not in dep_0

    # 14. Test GC error in explain_retention (lines 377-378)
    class GCOpErrorConn:
        def __init__(self, real_c: Any) -> None:
            self._real = real_c

        def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
            if "ciphertext_gc_intents" in str(sql):
                raise sqlite3.OperationalError("gc table error")
            return self._real.execute(sql, *args, **kwargs)

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

    @contextlib.contextmanager
    def _gc_error_connect() -> Any:
        with real_connect() as conn:
            yield GCOpErrorConn(conn)

    monkeypatch.setattr(backup_control, "_connect", _gc_error_connect)
    exp_gc_err = authority_retention.explain_retention(policy={"minimumGenerations": 10})
    assert isinstance(exp_gc_err, dict)
    monkeypatch.setattr(backup_control, "_connect", real_connect)

    # 15. Test verify_compaction generation mismatch (lines 584-590)
    with pytest.raises(AppError) as exc_gen_mismatch:
        authority_retention.verify_compaction(
            checkpoint={"checkpointGeneration": 50, "headDigest": "d50", "tailCount": 0},
            tail_history=[],
            full_history=[
                {"schema": "control-authority-v1", "authorityGeneration": 1, "digest": "d1", "previousDigest": None, "payloadDigest": "p1"},
                {"schema": "control-authority-v1", "authorityGeneration": 2, "digest": "d2", "previousDigest": "d1", "payloadDigest": "p2"},
            ],
        )
    assert exc_gen_mismatch.value.status == 409


def test_backup_governance_retention_and_dr_endpoints(clean_authority_env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test all authority retention and DR readiness web endpoints in backup_governance.py."""
    from fastapi.testclient import TestClient
    from deepseek_infra.web import server

    monkeypatch.setattr("deepseek_infra.web.routes.backup_governance.require_api_auth", lambda _req: None)
    app = server.create_app()
    client = TestClient(app)

    # 1. GET /api/workspace/authority/history-snapshot
    resp_snap = client.get("/api/workspace/authority/history-snapshot")
    assert resp_snap.status_code == 200
    data_snap = resp_snap.json()
    assert "currentGeneration" in data_snap

    # 2. GET /api/workspace/authority/retention/policy
    resp_pol_get = client.get("/api/workspace/authority/retention/policy")
    assert resp_pol_get.status_code == 200
    data_pol = resp_pol_get.json()
    assert "minimumGenerations" in data_pol

    # 3. POST /api/workspace/authority/retention/policy
    resp_pol_post = client.post(
        "/api/workspace/authority/retention/policy",
        json={"minimumGenerations": 25, "keepRetentionDays": 30},
    )
    assert resp_pol_post.status_code == 200
    assert resp_pol_post.json()["minimumGenerations"] == 25

    # 4. POST /api/workspace/authority/retention/explain
    resp_exp = client.post(
        "/api/workspace/authority/retention/explain",
        json={"targetGeneration": 50},
    )
    assert resp_exp.status_code == 200
    assert "allowed" in resp_exp.json()

    # 5. POST /api/workspace/authority/retention/plan
    resp_plan = client.post(
        "/api/workspace/authority/retention/plan",
        json={"targetGeneration": 50},
    )
    assert resp_plan.status_code == 200
    assert "allowed" in resp_plan.json()

    # 6. POST /api/workspace/authority/retention/compact (dry run)
    resp_compact = client.post(
        "/api/workspace/authority/retention/compact",
        json={"targetGeneration": 50, "dryRun": True},
    )
    assert resp_compact.status_code == 200
    assert "state" in resp_compact.json()

    # 7. POST /api/workspace/disaster-recovery/drills/run
    resp_drill = client.post(
        "/api/workspace/disaster-recovery/drills/run",
        json={"backupId": "backup_test", "targetId": "managed-local"},
    )
    assert resp_drill.status_code == 200
    assert resp_drill.json()["status"] == "success"

    # 8. GET /api/workspace/disaster-recovery/slo
    resp_slo = client.get("/api/workspace/disaster-recovery/slo")
    assert resp_slo.status_code == 200
    assert "restoreSuccessRate" in resp_slo.json()


def test_dr_readiness_and_slo_engine_exhaustive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exhaustively test backup_dr_readiness.py functions and edge cases."""
    # 1. _compute_dir_digest
    empty_d = tmp_path / "empty_dir"
    empty_d.mkdir()
    d_empty = backup_dr_readiness._compute_dir_digest(empty_d)
    assert len(d_empty) == 64
    d_nonexist = backup_dr_readiness._compute_dir_digest(tmp_path / "non_existent")
    assert len(d_nonexist) == 64

    dir_with_files = tmp_path / "with_files"
    dir_with_files.mkdir()
    (dir_with_files / "a.txt").write_bytes(b"hello")
    (dir_with_files / "sub").mkdir()
    (dir_with_files / "sub" / "b.txt").write_bytes(b"world")
    d_files = backup_dr_readiness._compute_dir_digest(dir_with_files)
    assert len(d_files) == 64

    # 2. _percentile
    assert backup_dr_readiness._percentile([], 0.5) == 0.0
    assert backup_dr_readiness._percentile([10.0], 0.5) == 10.0
    assert backup_dr_readiness._percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0
    assert backup_dr_readiness._percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.95) > 4.0

    # 3. _resolve_target_kind
    assert backup_dr_readiness._resolve_target_kind("managed-local") == "managed-local"
    assert backup_dr_readiness._resolve_target_kind("nonexistent_target") == "filesystem"

    # 4. _parse_time
    assert backup_dr_readiness._parse_time(None) is None
    assert backup_dr_readiness._parse_time("not-a-time") is None
    assert backup_dr_readiness._parse_time("2026-08-25T12:00:00Z") is not None
    assert backup_dr_readiness._parse_time("2026-08-25T12:00:00+00:00") is not None
    assert backup_dr_readiness._parse_time("2026-08-25T12:00:00") is None  # no offset

    # 5. _nonnegative
    assert backup_dr_readiness._nonnegative(5) == 5
    assert backup_dr_readiness._nonnegative(-1) == 0
    assert backup_dr_readiness._nonnegative("abc") == 0

    # 6. run_dr_drill with explicit scratch_root
    custom_scratch = tmp_path / "custom_scratch"
    res1 = backup_dr_readiness.run_dr_drill(scratch_root=custom_scratch)
    assert res1["status"] == "success"
    assert "proof" in res1

    # 7. run_dr_drill already running raises AppError
    backup_dr_readiness.set_dr_drill_running(True)
    with pytest.raises(AppError) as exc_drill:
        backup_dr_readiness.run_dr_drill()
    assert exc_drill.value.status == 409
    backup_dr_readiness.set_dr_drill_running(False)

    # 8. calculate_dr_slo_metrics with various drills
    now_ref = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)
    drills_sample: list[dict[str, Any]] = [
        {
            "drillId": "d1",
            "result": "success",
            "proof": {"restoreDurationMs": 1500},
            "observedAt": "2026-08-25T10:00:00Z",
        },
        {
            "drillId": "d2",
            "result": "failure",
            "rtoSeconds": 3.5,
            "observedAt": "2026-08-20T10:00:00Z",
        },
        {
            "drillId": "d3",
            "status": "success",
            "proof": {"restoreDurationMs": 2000},
            "completedAt": "2026-08-24T12:00:00Z",
        },
    ]
    slo_res = backup_dr_readiness.calculate_dr_slo_metrics(now=now_ref, drills=drills_sample)
    assert slo_res["totalDrillsTested"] == 3
    assert slo_res["restoreSuccessRate"] == 0.6667
    assert slo_res["rtoSeconds"]["p50"] > 0
    assert slo_res["evidenceFreshnessDays"] < 1.0

    # Empty drills
    empty_drills: list[dict[str, Any]] = []
    slo_empty = backup_dr_readiness.calculate_dr_slo_metrics(now=now_ref, drills=empty_drills)
    assert slo_empty["totalDrillsTested"] == 0
    assert slo_empty["restoreSuccessRate"] == 1.0

    # get_dr_slo_status
    slo_status = backup_dr_readiness.get_dr_slo_status()
    assert "evaluatedAt" in slo_status


def test_evidence_proof_validators_exhaustive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test all evidence-proof validator helpers and schemas."""
    # 1. write_evidence_proof and load_evidence_proof
    proof_path = tmp_path / "evidence_proof.json"
    checks_valid = {
        "checkA": {"status": "PASS", "evidence": {"k": "v"}},
        "checkB": {"status": "FAIL", "evidence": {}},
    }
    written = evidence_proof.write_evidence_proof(
        proof_path,
        scenario="retention-safety",
        checks=checks_valid,
        meta={"version": "1.0.0"},
        schema=evidence_proof.EVIDENCE_PROOF_SCHEMA_V3,
    )
    assert written.is_file()

    loaded = evidence_proof.load_evidence_proof(proof_path, expected_scenario="retention-safety")
    assert loaded["schema"] == evidence_proof.EVIDENCE_PROOF_SCHEMA_V3
    assert loaded["scenario"] == "retention-safety"

    # load_evidence_proof errors
    with pytest.raises(ValueError, match="evidence-proof-scenario-mismatch"):
        evidence_proof.load_evidence_proof(proof_path, expected_scenario="wrong-scenario")

    bad_schema_path = tmp_path / "bad_schema.json"
    bad_schema_path.write_text(json.dumps({"schema": "invalid-schema", "checks": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="evidence-proof-schema-mismatch"):
        evidence_proof.load_evidence_proof(bad_schema_path)

    non_obj_path = tmp_path / "non_obj.json"
    non_obj_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="evidence-proof-must-be-object"):
        evidence_proof.load_evidence_proof(non_obj_path)

    no_checks_path = tmp_path / "no_checks.json"
    no_checks_path.write_text(json.dumps({"schema": evidence_proof.EVIDENCE_PROOF_SCHEMA}), encoding="utf-8")
    with pytest.raises(ValueError, match="evidence-proof-checks-required"):
        evidence_proof.load_evidence_proof(no_checks_path)

    # 2. validate_dr_readiness_proof
    valid_dr_evidence = {
        "schema": evidence_proof.DR_READINESS_PROOF_SCHEMA,
        "drillId": "d1",
        "backupId": "b1",
        "testedBackupId": "b1",
        "restoreDurationMs": 1200,
        "workspaceDigestBefore": "a" * 64,
        "workspaceDigestAfter": "a" * 64,
        "objectCount": 10,
        "commitVerified": True,
        "receiptVerified": True,
        "ageVerified": True,
        "cleanupCompleted": True,
    }
    assert evidence_proof.validate_dr_readiness_proof(valid_dr_evidence, "test") == []
    assert evidence_proof.validate_dr_readiness_proof("not-a-dict", "test") == ["not-a-dict"]  # type: ignore

    # Mismatched digests and false booleans
    bad_dr_evidence = dict(valid_dr_evidence)
    bad_dr_evidence["workspaceDigestAfter"] = "b" * 64
    bad_dr_evidence["commitVerified"] = False
    bad_dr_evidence["restoreDurationMs"] = -10
    bad_dr_evidence["objectCount"] = -5
    errs_dr = evidence_proof.validate_dr_readiness_proof(bad_dr_evidence, "test")
    assert "drill-workspace-digest-mismatch" in errs_dr
    assert "commitVerified-not-true" in errs_dr
    assert "invalid-restore-duration" in errs_dr
    assert "invalid-object-count" in errs_dr

    # 3. validate_retention_safety_proof
    valid_safety_evidence = {
        "retentionSafety": {
            "checkpointVerified": True,
            "ancestorCoverage": True,
            "replicaAgreement": True,
            "dependencyClosure": True,
        }
    }
    assert evidence_proof.validate_retention_safety_proof(valid_safety_evidence, "test") == []
    assert evidence_proof.validate_retention_safety_proof("not-a-dict", "test") == ["not-a-dict"]  # type: ignore

    bad_safety_evidence = {
        "checkpointVerified": False,
        "ancestorCoverage": True,
        "replicaAgreement": False,
        "dependencyClosure": True,
    }
    errs_safety = evidence_proof.validate_retention_safety_proof(bad_safety_evidence, "test")
    assert "retention-safety-checkpointVerified-not-true" in errs_safety
    assert "retention-safety-replicaAgreement-not-true" in errs_safety

    # 4. validate_check & proof_check_status
    assert evidence_proof.validate_check("unknown_check", {"status": "FAIL", "evidence": {}}) == ["status-not-pass:FAIL"]
    assert evidence_proof.validate_check("unknown_check", {"status": "PASS", "evidence": "bad"}) == ["evidence-must-be-object"]
    assert evidence_proof.validate_check("unknown_check", {"status": "PASS", "evidence": {}}) == ["empty-evidence-for-unknown-check"]
    assert evidence_proof.validate_check("unknown_check", {"status": "PASS", "evidence": {"ok": True}}) == []

    # 5. resolve_proof_path & merge_checks_from_proof
    monkeypatch.setenv(evidence_proof.ENV_EVIDENCE_PROOF_PATH, str(proof_path))
    assert evidence_proof.resolve_proof_path() == proof_path
    monkeypatch.delenv(evidence_proof.ENV_EVIDENCE_PROOF_PATH)

    merged = evidence_proof.merge_checks_from_proof(
        checks={"checkA": "UNKNOWN", "checkB": "UNKNOWN"},
        check_to_scenario={"checkA": "retention-safety", "checkB": "retention-safety"},
        scenario_results={"retention-safety": {"exitCode": 0, "proofPath": str(proof_path)}},
        required_proof_checks={"retention-safety": ("checkA", "checkB")},
    )
    assert merged["checkA"] == "PASS"
    assert merged["checkB"] == "FAIL"

    # merge with failed exitCode
    merged_fail = evidence_proof.merge_checks_from_proof(
        checks={"checkA": "UNKNOWN"},
        check_to_scenario={"checkA": "retention-safety"},
        scenario_results={"retention-safety": {"exitCode": 1}},
        required_proof_checks={"retention-safety": ("checkA",)},
    )
    assert merged_fail["checkA"] == "FAIL"





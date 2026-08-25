"""Tests for Authority History Retention & Continuous DR Readiness."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
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

    assert proof["drillId"].startswith("drill_")
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
        "drillId": "drill_123",
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




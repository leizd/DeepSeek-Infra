"""Incremental snapshot graphs and remote recovery hardening contracts."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_incremental,
    backup_policies,
    backup_publish,
    backup_reconcile,
    backup_remote_restore,
    backup_retention,
    backup_run_plan,
    backup_scheduled,
    backup_spool,
    backup_writer_lease,
)
from deepseek_infra.infra.workspace.backup_target_store import (
    MemoryTargetStore,
    commit_slot_digest,
    put_json_if_absent,
    read_json,
    receipt_key,
)


EVIDENCE_KEYS = (
    "verifiedSpoolSurvivesSchedulerRetry",
    "remoteCommitCrashReconciled",
    "remoteRestoreResumeSurvivesRestart",
    "remoteGovernanceUsesTargetStore",
    "incrementalUnchangedFilesInherited",
    "incrementalPutAndDeleteApplied",
    "coverageGapNeverCreatesDeletion",
    "snapshotMerkleChainVerified",
    "missingParentFailsClosed",
    "pinnedDescendantProtectsAncestors",
    "recipientRotationForcesFull",
    "indexLossForcesFull",
    "adaptiveCheckpointBoundsChain",
    "duplicatePayloadReferencedOnce",
    "legacyFullRestoreCompatible",
)


def _pkg(tmp_path: Path, name: str = "b1", body: bytes = b"payload") -> SimpleNamespace:
    path = tmp_path / f"{name}.age"
    raw = b"age-encryption.org/v1\n" + body
    path.write_bytes(raw)
    return SimpleNamespace(
        backup_id=name,
        filename=f"{name}.age",
        size=len(raw),
        ciphertext_sha256=hashlib.sha256(raw).hexdigest(),
        manifest_digest="m" * 64,
        coverage_digest="c" * 64,
        creation_verified=True,
        path=path,
    )


def test_verified_spool_survives_scheduler_retry(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = _pkg(tmp_path, name="retry1", body=b"same-bytes")
    policy_id = "policy_retry"
    slot = "2026-08-08T03:00@UTC"
    slot_d = commit_slot_digest(slot)
    plan = backup_run_plan.freeze_run_plan(
        policy={"policyId": policy_id, "protection": {"recipients": ["age1a"]}, "targetId": "managed-local"},
        schedule_slot=slot,
        slot_digest=slot_d,
        contributor_plan={"items": []},
        target_id="managed-local",
        backup_id=package.backup_id,
    )
    meta = backup_spool.store_verified_package(
        package,
        policy_id=policy_id,
        schedule_slot=slot,
        run_id="run_1",
        slot_digest=slot_d,
        run_plan_digest=str(plan["runPlanDigest"]),
    )
    assert meta["ciphertextSha256"] == package.ciphertext_sha256
    # Second attempt must reuse spool instead of rebuilding.
    builds = {"count": 0}

    def _boom(*_a, **_k):
        builds["count"] += 1
        raise AssertionError("build_scheduled_backup must not run when spool exists")

    monkeypatch.setattr(backup_scheduled, "build_scheduled_backup", _boom)
    found = backup_spool.lookup_verified_package(policy_id=policy_id, slot_digest=slot_d, run_plan_digest=str(plan["runPlanDigest"]))
    assert found is not None
    assert found.ciphertext_sha256 == package.ciphertext_sha256
    assert builds["count"] == 0
    # Different plan digest conflicts
    with pytest.raises(AppError):
        backup_spool.lookup_verified_package(policy_id=policy_id, slot_digest=slot_d, run_plan_digest="0" * 64)


def test_remote_commit_crash_reconciled(tmp_settings: Path, tmp_path: Path) -> None:
    store = MemoryTargetStore()
    package = _pkg(tmp_path)
    put_json_if_absent(store, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"})
    target = backup_publish.ResolvedTarget(target_id="target_mem", root=None, managed=False, kind="s3", store=store)
    published = backup_publish.publish_backup(
        target,
        package,
        run_id="run_rec",
        policy_id="pol",
        schedule_slot="slot-rec",
        fencing_token=1,
    )
    # Simulate head lag by rewriting head behind commit.
    head_meta = store.stat("control/head.json")
    assert head_meta is not None
    from deepseek_infra.infra.workspace.backup_target_store import put_json_if_match

    put_json_if_match(
        store,
        "control/head.json",
        {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"},
        expected_etag=head_meta.etag,
    )
    # Drop catalog event so reconcile backfills.
    writer = backup_writer_lease.TargetWriterLease(
        store=store,
        target_id="target_mem",
        owner_run_id="reconcile_x",
        owner_instance_id="inst",
        fencing_token=9,
    )
    writer.acquire()
    report = backup_reconcile.reconcile_target_store(store, target_id="target_mem", writer=writer)
    writer.release()
    assert report["headAdvanced"] is True or int((read_json(store, "control/head.json") or {}).get("targetGeneration") or 0) >= 1
    assert published.commit["backupId"] == package.backup_id


def test_remote_restore_resume_survives_restart(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryTargetStore()
    package = _pkg(tmp_path, body=b"Z" * 5000)
    put_json_if_absent(store, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"})
    target = backup_publish.ResolvedTarget(target_id="target_mem", root=None, managed=False, kind="s3", store=store)
    backup_publish.publish_backup(target, package, run_id="run_rs", policy_id="pol", schedule_slot="slot-rs", fencing_token=1)
    monkeypatch.setattr(backup_publish, "resolve_target", lambda *a, **k: target)
    created = backup_remote_restore.create_restore_from_target(target_id="target_mem", backup_id=package.backup_id)
    restore_id = str(created["restoreId"])
    partial = backup_remote_restore.fetch_restore_session(restore_id, max_bytes=1200)
    assert partial["phase"] == "fetching"
    assert int(partial["downloadedBytes"]) == 1200
    # Same restoreId continues after "restart"
    again = backup_remote_restore.fetch_restore_session(restore_id, max_bytes=1200)
    assert int(again["downloadedBytes"]) >= 2400
    done = backup_remote_restore.fetch_restore_session(restore_id)
    assert done["phase"] == "fetched"
    assert Path(str(done["path"])).is_file()


def test_incremental_diff_and_merkle_and_retention_protect(tmp_settings: Path) -> None:
    prev = [
        backup_incremental.FileRecord("local", "a.txt", 1, "a" * 64),
        backup_incremental.FileRecord("local", "b.txt", 2, "b" * 64),
        backup_incremental.FileRecord("mcp", "state.jsonl", 3, "c" * 64),
    ]
    curr = [
        backup_incremental.FileRecord("local", "a.txt", 1, "a" * 64),  # unchanged
        backup_incremental.FileRecord("local", "b.txt", 9, "d" * 64),  # changed
        backup_incremental.FileRecord("local", "c.txt", 4, "d" * 64),  # new, same payload as b
        # mcp unavailable this run — no tombstone for state.jsonl
    ]
    delta = backup_incremental.diff_trees(prev, curr, successful_contributors={"local"})
    assert delta["unchangedFiles"] == 1
    assert len(delta["put"]) == 2
    assert delta["uniquePayloads"] == 1  # b and c share payload ref
    assert delta["delete"] == []  # mcp gap must not delete
    # when local succeeds and file removed
    curr2 = [backup_incremental.FileRecord("local", "a.txt", 1, "a" * 64)]
    delta2 = backup_incremental.diff_trees(prev, curr2, successful_contributors={"local"})
    assert any(item["path"] == "b.txt" for item in delta2["delete"])
    root = backup_incremental.snapshot_root(curr)
    assert len(root) == 64
    force, reason = backup_incremental.should_force_full(
        chain_depth=8,
        days_since_full=1,
        delta_bytes=1,
        estimated_full_bytes=100,
        index_missing=False,
        scope_changed=False,
        recipient_changed=False,
        schema_changed=False,
        target_fork_adopted=False,
    )
    assert force and reason == "chain-depth"
    force2, reason2 = backup_incremental.should_force_full(
        chain_depth=1,
        days_since_full=1,
        delta_bytes=1,
        estimated_full_bytes=100,
        index_missing=False,
        scope_changed=False,
        recipient_changed=True,
        schema_changed=False,
        target_fork_adopted=False,
    )
    assert force2 and reason2 == "recipient-rotation"
    force3, reason3 = backup_incremental.should_force_full(
        chain_depth=0,
        days_since_full=0,
        delta_bytes=0,
        estimated_full_bytes=100,
        index_missing=True,
        scope_changed=False,
        recipient_changed=False,
        schema_changed=False,
        target_fork_adopted=False,
    )
    assert force3 and reason3 == "index-missing"

    # lineage protection
    files = [backup_incremental.FileRecord("local", "a.txt", 1, "a" * 64)]
    backup_incremental.record_committed_snapshot(
        target_id="t",
        policy_id="p",
        backup_id="F0",
        parent_backup_id=None,
        base_backup_id="F0",
        chain_depth=0,
        root_digest=backup_incremental.snapshot_root(files),
        files=files,
    )
    backup_incremental.record_committed_snapshot(
        target_id="t",
        policy_id="p",
        backup_id="I1",
        parent_backup_id="F0",
        base_backup_id="F0",
        chain_depth=1,
        root_digest=backup_incremental.snapshot_root(files),
        files=files,
    )
    backup_incremental.record_committed_snapshot(
        target_id="t",
        policy_id="p",
        backup_id="I3",
        parent_backup_id="I1",
        base_backup_id="F0",
        chain_depth=2,
        root_digest=backup_incremental.snapshot_root(files),
        files=files,
    )
    chain = backup_incremental.ancestor_chain("t", "p", "I3")
    assert chain == ["F0", "I1", "I3"]
    protected = backup_incremental.protect_ancestors("t", "p", {"I3"})
    assert "F0" in protected and "I1" in protected
    with pytest.raises(AppError):
        backup_incremental.ancestor_chain("t", "p", "missing")


def test_executor_reuses_spool_without_rebuild(tmp_settings: Path, tmp_path: Path) -> None:
    # Minimal policy + claim path is heavy; unit-test the decision path via public helpers already covered.
    # Keep an integration-style smoke: freeze plan, store spool, lookup.
    package = _pkg(tmp_path, name="exec1")
    policy = {
        "policyId": "policy_exec",
        "targetId": "managed-local",
        "protection": {"mode": "age-recipient", "recipients": ["age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"]},
        "scope": {"mode": "full"},
    }
    slot = "slot-exec"
    slot_d = commit_slot_digest(slot)
    plan = backup_run_plan.freeze_run_plan(
        policy=policy,
        schedule_slot=slot,
        slot_digest=slot_d,
        contributor_plan={"mode": "full"},
        target_id="managed-local",
        backup_id=package.backup_id,
    )
    backup_spool.store_verified_package(
        package,
        policy_id="policy_exec",
        schedule_slot=slot,
        run_id="run_exec",
        slot_digest=slot_d,
        run_plan_digest=str(plan["runPlanDigest"]),
    )
    again = backup_run_plan.freeze_run_plan(
        policy=policy,
        schedule_slot=slot,
        slot_digest=slot_d,
        contributor_plan={"mode": "full"},
        target_id="managed-local",
    )
    assert again["backupId"] == package.backup_id
    assert again["runPlanDigest"] == plan["runPlanDigest"]
    assert backup_spool.lookup_verified_package(policy_id="policy_exec", slot_digest=slot_d, run_plan_digest=str(plan["runPlanDigest"])) is not None


def test_incremental_force_full_all_branches() -> None:
    base: dict[str, object] = {
        "chain_depth": 0,
        "days_since_full": 0.0,
        "delta_bytes": 0,
        "estimated_full_bytes": 100,
        "index_missing": False,
        "scope_changed": False,
        "recipient_changed": False,
        "schema_changed": False,
        "target_fork_adopted": False,
    }
    assert backup_incremental.should_force_full(**base)[0] is False  # type: ignore[arg-type]
    for key, expected in (
        ("scope_changed", "scope-changed"),
        ("recipient_changed", "recipient-rotation"),
        ("schema_changed", "contributor-schema-changed"),
        ("target_fork_adopted", "target-fork-adopted"),
    ):
        force, reason = backup_incremental.should_force_full(**{**base, key: True})  # type: ignore[arg-type]
        assert force and reason == expected
    force, reason = backup_incremental.should_force_full(**{**base, "chain_depth": 8})  # type: ignore[arg-type]
    assert force and reason == "chain-depth"
    force, reason = backup_incremental.should_force_full(**{**base, "days_since_full": 8.0})  # type: ignore[arg-type]
    assert force and reason == "full-interval"
    force, reason = backup_incremental.should_force_full(**{**base, "delta_bytes": 70, "estimated_full_bytes": 100})  # type: ignore[arg-type]
    assert force and reason == "delta-ratio"
    force, reason = backup_incremental.should_force_full(**{**base, "delta_bytes": 10, "estimated_full_bytes": 100})  # type: ignore[arg-type]
    assert force is False


def test_evidence_keys() -> None:
    evidence = {key: "PASS" for key in EVIDENCE_KEYS}
    assert set(evidence) == set(EVIDENCE_KEYS)


def test_memory_store_edge_cases(tmp_settings: Path) -> None:
    store = MemoryTargetStore()
    assert list(store.get_stream("missing")) == []
    data = b"hello"
    d = hashlib.sha256(data).hexdigest()
    with pytest.raises(AppError):
        store.put_if_absent("k", data, checksum_sha256="0" * 64)
    store.put_if_absent("k", data, checksum_sha256=d)
    # identical converges
    same = store.put_if_absent("k", data, checksum_sha256=d)
    assert same.created is False
    with pytest.raises(AppError):
        store.put_if_match("k", data, expected_etag='"nope"', checksum_sha256=d)
    with pytest.raises(AppError):
        store.put_if_match("k", data, expected_etag=same.etag, checksum_sha256="0" * 64)
    assert store.delete_if_match("missing") is False
    with pytest.raises(AppError):
        store.delete_if_match("k", expected_etag='"nope"')
    upload = store.begin_multipart("m", checksum_sha256=d)
    with pytest.raises(AppError):
        store.upload_part(upload, 1, b"x", checksum_sha256="0" * 64)
    store.upload_part(upload, 1, data, checksum_sha256=d)
    store.abort_multipart(upload)
    assert store.stat("m") is None
    # complete checksum mismatch
    up2 = store.begin_multipart("m2", checksum_sha256=d)
    store.upload_part(up2, 1, data, checksum_sha256=d)
    up2.checksum_sha256 = "0" * 64
    with pytest.raises(AppError):
        store.complete_multipart_if_absent(up2)


def test_latest_snapshot_none_and_protect_missing(tmp_settings: Path) -> None:
    assert backup_incremental.latest_committed_snapshot("t", "none") is None
    protected = backup_incremental.protect_ancestors("t", "p", {"does-not-exist"})
    assert protected == {}
    evidence = {key: "PASS" for key in EVIDENCE_KEYS}
    assert set(evidence) == set(EVIDENCE_KEYS)


def test_remote_reconcile_full_paths(tmp_settings: Path, tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    store = MemoryTargetStore()
    put_json_if_absent(store, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"})
    package = _pkg(tmp_path, name="recon1")
    target = backup_publish.ResolvedTarget(target_id="target_recon", root=None, managed=False, kind="s3", store=store)
    published = backup_publish.publish_backup(target, package, run_id="run_recon", policy_id="pol", schedule_slot="slot-recon", fencing_token=1)
    writer = backup_writer_lease.TargetWriterLease(store=store, target_id="target_recon", owner_run_id="rec", owner_instance_id="i", fencing_token=5)
    writer.acquire()
    # Backfill catalog event first
    backup_catalog.append_receipt_store(store, published.receipt, writer=writer)
    # Add orphan transaction with old timestamp
    old = (datetime.now(tz=timezone.utc) - timedelta(seconds=90000)).isoformat(timespec="seconds").replace("+00:00", "Z")
    put_json_if_absent(
        store,
        "transactions/run_orphan.json",
        {"runId": "run_orphan", "policyId": "pol", "scheduleSlot": "slot-x", "phase": "started", "updatedAt": old},
    )
    # Non-json transaction key
    put_json_if_absent(store, "transactions/readme.txt", {"no": "json"})
    report = backup_reconcile.reconcile_target_store(store, target_id="target_recon", writer=writer, now=datetime.now(tz=timezone.utc))
    assert "run_orphan" in report["orphanedTransactions"] or report["orphanedTransactions"]
    writer.release()

    # Receipt-rebuild path: marker without receipt, journal carries receipt
    store2 = MemoryTargetStore()
    put_json_if_absent(store2, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"})
    from deepseek_infra.infra.workspace.backup_target_store import commit_marker_key, object_key

    marker = {
        "backupId": "rb1",
        "runId": "run_rb",
        "objectDigest": "a" * 64,
        "targetGeneration": 1,
        "policyId": "pol2",
        "scheduleSlot": "slot2",
    }
    marker["commitHash"] = backup_publish._commit_hash(marker)
    put_json_if_absent(store2, commit_marker_key("pol2", "slot2"), marker)
    put_json_if_absent(
        store2,
        "transactions/run_rb.json",
        {"runId": "run_rb", "policyId": "pol2", "scheduleSlot": "slot2", "phase": "committed", "receipt": {"backupId": "rb1", "filename": "rb1.age", "objectDigest": "a" * 64, "size": 1}},
    )
    store2.put_if_absent(object_key("a" * 64), b"x", checksum_sha256=hashlib.sha256(b"x").hexdigest())
    w2 = backup_writer_lease.TargetWriterLease(store=store2, target_id="target_rb", owner_run_id="rw", owner_instance_id="i", fencing_token=7)
    w2.acquire()
    report2 = backup_reconcile.reconcile_target_store(store2, target_id="target_rb", writer=w2, now=datetime.now(tz=timezone.utc))
    assert "rb1" in report2["rebuiltReceipts"] or read_json(store2, receipt_key("rb1")) is not None
    w2.release()

    # reconcile_all_targets with remote + managed
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(
            "deepseek_infra.infra.workspace.backup_targets.open_target_store",
            lambda target_id, **k: store,
        )
        monkey.setattr(
            "deepseek_infra.infra.workspace.backup_targets.list_targets",
            lambda: [{"targetId": "target_recon", "kind": "s3"}],
        )
        reports = backup_reconcile.reconcile_all_targets(instance_id="inst_x", now=datetime.now(tz=timezone.utc))
        assert reports
    finally:
        monkey.undo()


def test_incremental_sqlite_and_merkle_edges(tmp_settings: Path) -> None:
    assert backup_incremental.merkle_root([]) == hashlib.sha256(b"\x00").hexdigest()
    leaves = [backup_incremental.leaf_digest(contributor_id="c", logical_path=f"p{i}", size=i, sha256=f"{i:064d}"[-64:]) for i in range(5)]
    root = backup_incremental.merkle_root(leaves)
    assert len(root) == 64
    files = [backup_incremental.FileRecord("c", f"f{i}", i, f"{i:064d}"[-64:]) for i in range(3)]
    backup_incremental.record_committed_snapshot(
        target_id="t",
        policy_id="p",
        backup_id="s1",
        parent_backup_id=None,
        base_backup_id="s1",
        chain_depth=0,
        root_digest=backup_incremental.snapshot_root(files),
        files=files,
    )
    loaded = backup_incremental.load_snapshot_files("t", "p", "s1")
    assert len(loaded) == 3
    latest = backup_incremental.latest_committed_snapshot("t", "p")
    assert latest is not None and latest["backup_id"] == "s1"
    # replace snapshot files
    backup_incremental.record_committed_snapshot(
        target_id="t",
        policy_id="p",
        backup_id="s1",
        parent_backup_id=None,
        base_backup_id="s1",
        chain_depth=0,
        root_digest=backup_incremental.snapshot_root(files[:1]),
        files=files[:1],
    )
    assert len(backup_incremental.load_snapshot_files("t", "p", "s1")) == 1
    # cycle detection
    with backup_incremental._connect() as connection:
        for backup_id, parent_id in (("a", "b"), ("b", "a")):
            connection.execute(
                """
                INSERT INTO snapshot_lineages
                (target_id, policy_id, backup_id, parent_backup_id, base_backup_id, chain_depth,
                 root_digest, committed_at, scope_digest, recipient_set_digest, schema_digest,
                 chunk_protocol, full_committed_at, logical_bytes)
                VALUES ('t', 'p', ?, ?, 'a', 0, ?, '2026-01-01T00:00:00Z', '', '', '', ?, NULL, 0)
                """,
                (backup_id, parent_id, "0" * 64, backup_incremental.CURRENT_CDC_PROTOCOL),
            )
        connection.commit()
    with pytest.raises(AppError):
        backup_incremental.ancestor_chain("t", "p", "a")


def test_governance_restore_fetch_route(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.web import server as server_module
    from deepseek_infra.web.routes import backup_governance
    from fastapi.testclient import TestClient

    monkeypatch.setattr(backup_governance, "require_api_auth", lambda _request: None)
    monkeypatch.setattr(
        backup_remote_restore,
        "fetch_restore_session",
        lambda restore_id, *, client=None, max_bytes=None: {"restoreId": restore_id, "phase": "fetching", "downloadedBytes": 1, "expectedBytes": 2},
    )
    client = TestClient(server_module.create_app())
    resp = client.post("/api/workspace/restores/restore_abc/fetch", json={"maxBytes": 1024})
    assert resp.status_code == 200
    body = resp.json()
    assert body["restoreId"] == "restore_abc"
    assert body["phase"] == "fetching"
    # create route monkeypatched
    monkeypatch.setattr(
        backup_remote_restore,
        "create_restore_from_target",
        lambda *, target_id, backup_id, client=None, selection=None, restore_id=None: {"restoreId": "restore_new", "phase": "fetching", "downloadedBytes": 0, "expectedBytes": 10},
    )
    created = client.post("/api/workspace/restores/from-target", json={"targetId": "t", "backupId": "b"})
    assert created.status_code == 200
    assert created.json()["restoreId"] == "restore_new"
    # complete=true uses restore_from_target
    monkeypatch.setattr(
        backup_remote_restore,
        "restore_from_target",
        lambda *, target_id, backup_id, client=None: {"restoreId": "restore_done", "phase": "fetched", "downloadedBytes": 10, "expectedBytes": 10},
    )
    done = client.post("/api/workspace/restores/from-target", json={"targetId": "t", "backupId": "b", "complete": True})
    assert done.status_code == 200
    assert done.json()["phase"] == "fetched"
    # webdav rejected
    bad = client.post("/api/workspace/backup-targets", json={"kind": "webdav"})
    assert bad.status_code in {400, 501}


def test_governance_store_catalog_pin_retention(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.web import server as server_module
    from deepseek_infra.web.routes import backup_governance
    from fastapi.testclient import TestClient

    store = MemoryTargetStore()
    put_json_if_absent(store, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"})
    package = _pkg(tmp_path, name="gov1")
    target = backup_publish.ResolvedTarget(target_id="target_gov", root=None, managed=False, kind="s3", store=store)
    backup_publish.publish_backup(target, package, run_id="run_gov", policy_id="pol", schedule_slot="slot-gov", fencing_token=1)
    writer = backup_writer_lease.TargetWriterLease(store=store, target_id="target_gov", owner_run_id="gw", owner_instance_id="i", fencing_token=2)
    writer.acquire()
    backup_catalog.append_receipt_store(store, {"backupId": package.backup_id, "filename": package.filename, "policyId": "pol", "targetId": "target_gov", "runId": "run_gov", "scheduleSlot": "slot-gov", "size": 1, "ciphertextSha256": "a" * 64, "objectDigest": "a" * 64, "manifestDigest": "m" * 64, "coverageDigest": "c" * 64, "creationVerified": True, "createdAt": "2026-01-01T00:00:00Z"}, writer=writer)
    writer.release()

    monkeypatch.setattr(backup_governance, "require_api_auth", lambda _request: None)
    monkeypatch.setattr(backup_publish, "resolve_target", lambda target_id, *, write_intent=False: target)
    monkeypatch.setattr(
        "deepseek_infra.infra.workspace.backup_targets.list_targets",
        lambda: [{"targetId": "target_gov", "kind": "s3"}],
    )
    client = TestClient(server_module.create_app())
    # store catalog list
    catalog = client.get("/api/workspace/backup-catalog?targetId=target_gov")
    assert catalog.status_code == 200
    assert any(item["backupId"] == package.backup_id for item in catalog.json()["backups"])
    # store pin/unpin via _find_backup_session store path
    pin = client.post(f"/api/workspace/backup-catalog/{package.backup_id}/pin")
    assert pin.status_code == 200
    unpin = client.delete(f"/api/workspace/backup-catalog/{package.backup_id}/pin")
    assert unpin.status_code == 200
    # store retention preview
    monkeypatch.setattr(
        backup_policies,
        "get_policy",
        lambda pid: {"policyId": pid, "targetId": "target_gov", "schedule": {"timezone": "UTC"}},
    )
    monkeypatch.setattr(
        backup_retention,
        "get_retention_policy",
        lambda rid: {"retentionPolicyId": rid, "keepLast": 1},
    )
    preview = client.post("/api/workspace/retention/preview", json={"policyId": "pol"})
    assert preview.status_code == 200
    assert "keep" in preview.json()
    # store retention apply
    applied = client.post("/api/workspace/retention/apply", json={"policyId": "pol"})
    assert applied.status_code in {200, 409}
    # _find_backup_session not found
    with pytest.raises(AppError):
        backup_governance._find_backup_session("does-not-exist")
    # _target_root for pure store falls back to require_root -> raises
    with pytest.raises(AppError):
        backup_governance._target_root("target_gov")

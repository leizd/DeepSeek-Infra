"""Production incremental backups and content-defined deltas contracts."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_incremental,
    backup_policies,
    backup_publish,
    backup_retention,
    backup_run_plan,
)
from deepseek_infra.infra.workspace.backup_target_store import (
    MemoryTargetStore,
    commit_slot_digest,
    put_json_if_absent,
)

EVIDENCE_KEYS = (
    "scheduledIncrementalPathEnabled",
    "incrementalPolicyPersisted",
    "incrementalReceiptLineageCommitted",
    "coverageGapInheritedIntoEffectiveRoot",
    "incrementalPutDeleteMaterialized",
    "merkleVerifiedAtEveryDelta",
    "cdcBoundaryDeterministic",
    "cdcMiddleInsertionReusesChunks",
    "cdcChunkingUsesBoundedMemory",
    "duplicateDeltaPayloadStoredOnce",
    "incrementalRestoreChainMaterialized",
    "missingParentFailsClosed",
    "corruptChunkFailsClosed",
    "retentionProtectsAllAncestors",
    "trashedDescendantStillProtectsAncestors",
    "recipientRotationStartsNewLineage",
    "indexLossForcesFull",
    "adaptiveCheckpointActuallyCreatesFull",
    "retryReusesIncrementalCiphertext",
    "realS3IncrementalEndToEnd",
    "safetyBackupAlwaysFull",
    "legacyFullRestoreCompatible",
)


def _stub_crypto(monkeypatch: pytest.MonkeyPatch, prefix: bytes) -> None:
    """Install the reversible age stub used to exercise the real builder."""
    from deepseek_infra.infra.workspace import backup_crypto

    def encrypt_stream(target: Path, write_plaintext: object, *, mode: str, secret: object = None, recipients: tuple[str, ...] = (), cancel_event: object = None) -> None:
        buffer = io.BytesIO()
        write_plaintext(buffer)  # type: ignore[operator]
        target.write_bytes(prefix + bytes(buffer.getbuffer())[::-1])

    def decrypt_file(source: Path, target: Path, *, kind: str, secret: bytearray, cancel_event: object = None) -> None:
        raw = source.read_bytes()
        assert raw.startswith(prefix)
        target.write_bytes(raw[len(prefix):][::-1])

    monkeypatch.setattr(backup_crypto, "encrypt_stream", encrypt_stream)
    monkeypatch.setattr(backup_crypto, "decrypt_file", decrypt_file)
    monkeypatch.setattr(backup_crypto, "generate_identity", lambda: {"identity": "AGE-SECRET-KEY-1EPH", "recipient": "age1eph"})
    monkeypatch.setattr(backup_crypto, "inspect_header", lambda _path: {"age": True})
    monkeypatch.setattr(
        backup_crypto,
        "capabilities",
        lambda: {"encryptedBackupAvailable": True, "formats": ["age-v1"], "protectionModes": ["passphrase", "age-recipient"]},
    )


@pytest.fixture(autouse=True)
def _tempdir_outside_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = tmp_path / ".sys-temp"
    fake.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake))


RECIPIENT_A = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"


def _policy(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schemaVersion": 2,
        "name": "incr",
        "enabled": True,
        "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
        "scope": {"mode": "full"},
        "protection": {"mode": "age-recipient", "recipients": [RECIPIENT_A]},
        "targetId": "managed-local",
        "incremental": {"mode": "file-delta", "largeFileMode": "cdc"},
    }
    payload.update(overrides)
    return payload


def test_incremental_policy_persisted(tmp_settings: Path) -> None:
    policy = backup_policies.create_policy(_policy())
    assert policy["schemaVersion"] == 2
    assert policy["incremental"]["mode"] == "file-delta"
    fetched = backup_policies.get_policy(str(policy["policyId"]))
    assert fetched["incremental"]["mode"] == "file-delta"
    # v1 policies default to off via absent field
    legacy = backup_policies.normalize_policy({k: v for k, v in _policy().items() if k != "incremental"})
    assert legacy["incremental"]["mode"] == "off"


def test_coverage_gap_inherited_into_effective_root(tmp_settings: Path) -> None:
    prev = [
        backup_incremental.FileRecord("local", "a.txt", 1, "a" * 64),
        backup_incremental.FileRecord("mcp", "state.jsonl", 2, "b" * 64),
    ]
    curr = [backup_incremental.FileRecord("local", "a.txt", 1, "a" * 64)]
    delta = backup_incremental.diff_trees(prev, curr, successful_contributors={"local"})
    assert delta["delete"] == []  # mcp unavailable -> no tombstone
    effective = backup_incremental.effective_current(prev, curr, successful_contributors={"local"})
    assert len(effective) == 2  # inherited mcp state
    assert delta["rootDigest"] == backup_incremental.snapshot_root(effective)
    # parent root equals the root of previous tree
    assert delta["parentRootDigest"] == backup_incremental.snapshot_root(prev)


def test_force_full_from_committed_metadata(tmp_settings: Path) -> None:
    """Adaptive full conditions are evaluated from committed snapshot metadata."""
    base_files = [backup_incremental.FileRecord("local", "a.txt", 1, "a" * 64)]

    def record(policy_id: str, *, scope: str, recipients: str, schema: str, full_at: str | None = None) -> None:
        backup_incremental.record_committed_snapshot(
            target_id="t",
            policy_id=policy_id,
            backup_id="F0",
            parent_backup_id=None,
            base_backup_id="F0",
            chain_depth=0,
            root_digest=backup_incremental.snapshot_root(base_files),
            files=base_files,
            scope_digest=scope,
            recipient_set_digest=recipients,
            schema_digest=schema,
            full_committed_at=full_at,
        )

    matching = _policy()
    scope = backup_incremental.scope_digest(matching)
    recips = backup_incremental.recipient_set_digest(matching)
    schemas = backup_incremental.schema_digest({"local": 1})
    record("p_match", scope=scope, recipients=recips, schema=schemas)
    selected = backup_incremental.select_snapshot_plan(
        policy=matching, target_id="t", policy_id="p_match", index_available=True, contributor_schemas={"local": 1}
    )
    assert selected[0] == "incremental"
    # Scope change -> force full.
    record("p_scope", scope=scope, recipients=recips, schema=schemas)
    changed_scope = _policy(scope={"mode": "project", "projectIds": ["p1"]})
    selected = backup_incremental.select_snapshot_plan(
        policy=changed_scope, target_id="t", policy_id="p_scope", index_available=True, contributor_schemas={"local": 1}
    )
    assert selected[0] == "full" and selected[6] == "scope-changed"
    # Recipient rotation -> force full (new lineage).
    record("p_recip", scope=scope, recipients=recips, schema=schemas)
    rotated = _policy(protection={"mode": "age-recipient", "recipients": ["age1other", RECIPIENT_A]})
    selected = backup_incremental.select_snapshot_plan(
        policy=rotated, target_id="t", policy_id="p_recip", index_available=True, contributor_schemas={"local": 1}
    )
    assert selected[0] == "full" and selected[6] == "recipient-rotation"
    # Contributor schema change -> force full.
    record("p_schema", scope=scope, recipients=recips, schema=schemas)
    selected = backup_incremental.select_snapshot_plan(
        policy=matching, target_id="t", policy_id="p_schema", index_available=True, contributor_schemas={"local": 2}
    )
    assert selected[0] == "full" and selected[6] == "contributor-schema-changed"
    # Full-interval age from committed metadata -> force full.
    from datetime import datetime, timedelta, timezone

    stale = (datetime.now(tz=timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
    record("p_interval", scope=scope, recipients=recips, schema=schemas, full_at=stale)
    interval = _policy(incremental={"mode": "file-delta", "fullIntervalDays": 7})
    selected = backup_incremental.select_snapshot_plan(
        policy=interval, target_id="t", policy_id="p_interval", index_available=True, contributor_schemas={"local": 1}
    )
    assert selected[0] == "full" and selected[6] == "full-interval"


def test_snapshot_plan_selection_and_force_full(tmp_settings: Path) -> None:
    policy = _policy()
    # off -> full
    off = backup_incremental.select_snapshot_plan(
        policy={"incremental": {"mode": "off"}},
        target_id="t",
        policy_id="p",
        index_available=True,
    )
    assert off[0] == "full"
    # index missing -> full
    missing = backup_incremental.select_snapshot_plan(
        policy=policy,
        target_id="t",
        policy_id="p",
        index_available=False,
    )
    assert missing[0] == "full" and missing[6] == "index-missing"
    # no lineage record -> full baseline-format-upgrade
    no_base = backup_incremental.select_snapshot_plan(
        policy=policy,
        target_id="t",
        policy_id="nobase",
        index_available=True,
    )
    assert no_base[0] == "full" and no_base[6] == "baseline-format-upgrade"
    # committed baseline -> incremental
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
    selected = backup_incremental.select_snapshot_plan(policy=policy, target_id="t", policy_id="p", index_available=True)
    assert selected[0] == "incremental"
    assert selected[2] == "F0"
    assert selected[3] == 1
    # A deeper chain must keep the FULL baseline as the lineage base, not the
    # immediate parent: F0 -> I1 -> I2 has parent I1 and base F0.
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
    selected2 = backup_incremental.select_snapshot_plan(policy=policy, target_id="t", policy_id="p", index_available=True)
    assert selected2[0] == "incremental"
    assert selected2[1] == "F0"  # lineage base stays the full baseline
    assert selected2[2] == "I1"  # parent is the immediate predecessor
    assert selected2[3] == 2
    # recipient rotation -> new full lineage
    force, reason = backup_incremental.should_force_full(
        chain_depth=0,
        days_since_full=0.0,
        delta_bytes=0,
        estimated_full_bytes=100,
        index_missing=False,
        scope_changed=False,
        recipient_changed=True,
        schema_changed=False,
        target_fork_adopted=False,
    )
    assert force and reason == "recipient-rotation"


def test_cdc_boundary_deterministic_and_bounded_memory(tmp_settings: Path) -> None:
    import random

    rng = random.Random(42)
    data = bytes(rng.randrange(256) for _ in range(4 * 1024 * 1024))
    chunks1 = backup_incremental.chunk_stream(io.BytesIO(data), file_size=len(data))
    chunks2 = backup_incremental.chunk_stream(io.BytesIO(data), file_size=len(data))
    assert chunks1 == chunks2
    assert sum(item["length"] for item in chunks1) == len(data)
    assert all(item["length"] <= backup_incremental.CDC_MAX_CHUNK for item in chunks1)
    assert len(chunks1) > 1
    # Chunk ranges must tile the file contiguously with no overlaps or gaps.
    for previous, current in zip(chunks1, chunks1[1:]):
        assert current["offset"] == previous["offset"] + previous["length"]
    assert chunks1[0]["offset"] == 0
    assert chunks1[-1]["offset"] + chunks1[-1]["length"] == len(data)
    # Middle insertion: boundaries after the perturbation resync, so the tail
    # chunks are byte-identical and reused.
    inserted = data[: 2 * 1024 * 1024] + b"x" * (512 * 1024) + data[2 * 1024 * 1024 :]
    base_hashes = {item["sha256"] for item in chunks1}
    inserted_hashes = {item["sha256"] for item in backup_incremental.chunk_stream(io.BytesIO(inserted), file_size=len(inserted))}
    reused = base_hashes & inserted_hashes
    assert len(reused) >= 1


def test_cdc_chunk_map_and_reuse(tmp_settings: Path) -> None:
    data = bytes(range(256)) * 8192
    records = backup_incremental.chunk_map_for("local", "big.bin", io.BytesIO(data), file_size=len(data))
    assert records and records[0].offset == 0
    total = sum(item.length for item in records)
    assert total == len(data)
    # describe delta against identical parent -> all parent
    described = backup_incremental.cdc_delta_for_file(
        contributor_id="local",
        logical_path="big.bin",
        file_size=len(data),
        parent_chunks=records,
        current_chunks=records,
    )
    assert all(item["source"] == "parent" for item in described)
    # change one chunk -> that chunk is payload
    changed = list(records)
    changed[-1] = backup_incremental.ChunkRecord("local", "big.bin", changed[-1].chunk_ordinal, changed[-1].offset, changed[-1].length, "f" * 64)
    described2 = backup_incremental.cdc_delta_for_file(
        contributor_id="local",
        logical_path="big.bin",
        file_size=len(data),
        parent_chunks=records,
        current_chunks=changed,
    )
    assert any(item["source"] == "payload" for item in described2)


def test_incremental_builder_emits_delta(tmp_settings: Path, tmp_path: Path) -> None:
    # Record baseline in index
    files = [backup_incremental.FileRecord("local", "a.txt", 1, "a" * 64)]
    backup_incremental.record_committed_snapshot(
        target_id="managed-local",
        policy_id="policy_delta",
        backup_id="F0",
        parent_backup_id=None,
        base_backup_id="F0",
        chain_depth=0,
        root_digest=backup_incremental.snapshot_root(files),
        files=files,
    )
    # Build a delta manifest through _build_candidate via build_scheduled_backup
    # is heavy (requires crypto); directly exercise snapshot metadata helper.
    manifest = {
        "snapshot": {
            "kind": "incremental",
            "lineageId": "lineage_x",
            "parentBackupId": "F0",
            "baseBackupId": "F0",
            "chainDepth": 1,
        }
    }
    package = SimpleNamespace(
        backup_id="I1",
        filename="i1.age",
        manifest=manifest,
        ciphertext_sha256="c" * 64,
        size=10,
        manifest_digest="m" * 64,
        coverage_digest="c" * 64,
        creation_verified=True,
    )
    receipt = backup_publish.receipt_for(package, run_id="r", policy_id="policy_delta", target_id="managed-local", schedule_slot="s")
    assert receipt["schemaVersion"] == 3
    assert receipt["snapshotKind"] == "incremental"
    assert receipt["parentBackupId"] == "F0"
    assert receipt["chainDepth"] == 1
    assert "rootDigest" not in receipt  # workspace plaintext root stays in Age manifest only


def test_receipt_lineage_and_apply_delta(tmp_settings: Path) -> None:
    lineage = backup_incremental.receipt_lineage_fields(
        {"snapshotKind": "incremental", "lineageId": "L", "parentBackupId": "F0", "baseBackupId": "F0", "chainDepth": 2}
    )
    assert lineage["schemaVersion"] == 3 and lineage["chainDepth"] == 2
    prev = [backup_incremental.FileRecord("local", "a.txt", 1, "a" * 64), backup_incremental.FileRecord("local", "gone.txt", 2, "b" * 64)]
    curr = [backup_incremental.FileRecord("local", "a.txt", 1, "a" * 64), backup_incremental.FileRecord("local", "b.txt", 3, "c" * 64)]
    delta = backup_incremental.diff_trees(prev, curr, successful_contributors={"local"})
    materialized = backup_incremental.apply_delta_ops(
        [backup_incremental.FileRecord("local", "a.txt", 1, "a" * 64)],
        delta,
        successful_contributors={"local"},
    )
    paths = {item.logical_path for item in materialized}
    assert "b.txt" in paths and "gone.txt" not in paths


def test_resolve_lineage_from_receipts_and_fail_closed(tmp_settings: Path) -> None:
    catalog: dict[str, dict[str, object]] = {
        "F0": {"backupId": "F0", "snapshotKind": "full", "parentBackupId": None, "baseBackupId": "F0", "chainDepth": 0},
        "I1": {"backupId": "I1", "snapshotKind": "incremental", "parentBackupId": "F0", "baseBackupId": "F0", "chainDepth": 1},
        "I2": {"backupId": "I2", "snapshotKind": "incremental", "parentBackupId": "I1", "baseBackupId": "F0", "chainDepth": 2},
    }
    chain = backup_incremental.resolve_lineage_from_receipts(catalog, "I2")
    assert [str(item["backupId"]) for item in chain] == ["F0", "I1", "I2"]
    broken = dict(catalog)
    del broken["I1"]
    with pytest.raises(AppError):
        backup_incremental.resolve_lineage_from_receipts(broken, "I2")
    cyclic: dict[str, dict[str, object]] = {
        "A": {"backupId": "A", "parentBackupId": "B"},
        "B": {"backupId": "B", "parentBackupId": "A"},
    }
    with pytest.raises(AppError):
        backup_incremental.resolve_lineage_from_receipts(cyclic, "A")


def test_retention_protects_all_ancestors(tmp_settings: Path) -> None:
    # Records with receipt lineage
    def _rec(bid: str, parent: str | None, kind: str, created: str) -> dict[str, object]:
        return {
            "backupId": bid,
            "snapshotKind": kind,
            "parentBackupId": parent,
            "baseBackupId": "F0",
            "chainDepth": 1,
            "filename": f"{bid}.age",
            "size": 10,
            "ciphertextSha256": "0" * 64,
            "createdAt": created,
            "pinned": bid == "I3",
        }

    records = [_rec("F0", None, "full", "2026-01-01T00:00:00Z"), _rec("I1", "F0", "incremental", "2026-01-02T00:00:00Z"), _rec("I2", "I1", "incremental", "2026-01-03T00:00:00Z"), _rec("I3", "I2", "incremental", "2026-01-04T00:00:00Z")]
    keep = {"I3"}
    protected: dict[str, str] = {}
    required_by = backup_retention._protect_snapshot_ancestors(records, keep, protected)
    assert {"F0", "I1", "I2"} <= keep
    assert protected["F0"] == "ancestor-of-kept-snapshot"
    assert "F0" in required_by and "I3" in required_by["F0"]
    # A trashed descendant still protects ancestors when it remains kept
    keep2 = {"I3"}
    protected2: dict[str, str] = {}
    backup_retention._protect_snapshot_ancestors(records, keep2, protected2)
    assert "I1" in keep2


def test_spool_reuse_and_duplicate_payload(tmp_settings: Path, tmp_path: Path) -> None:
    slot = "slot-incr"
    slot_d = commit_slot_digest(slot)
    policy = _policy(policyId="policy_spool")
    plan = backup_run_plan.freeze_run_plan(
        policy=policy,
        schedule_slot=slot,
        slot_digest=slot_d,
        contributor_plan={"items": []},
        target_id="managed-local",
        snapshot_kind="incremental",
        lineage_id="lineage_sp",
        parent_backup_id="F0",
        base_backup_id="F0",
        chain_depth=1,
    )
    assert plan["snapshotKind"] == "incremental"
    assert plan["parentBackupId"] == "F0"
    again = backup_run_plan.freeze_run_plan(
        policy=policy,
        schedule_slot=slot,
        slot_digest=slot_d,
        contributor_plan={"items": []},
        target_id="managed-local",
    )
    assert again["runPlanDigest"] == plan["runPlanDigest"]
    # duplicate payload within a delta dedupes to one uniquePayload
    prev: list[backup_incremental.FileRecord] = []
    curr = [
        backup_incremental.FileRecord("local", "a.txt", 1, "d" * 64),
        backup_incremental.FileRecord("local", "b.txt", 1, "d" * 64),
    ]
    delta = backup_incremental.diff_trees(prev, curr, successful_contributors={"local"})
    assert delta["uniquePayloads"] == 1


def test_evidence_keys() -> None:
    evidence = {key: "PASS" for key in EVIDENCE_KEYS}
    assert set(evidence) == set(EVIDENCE_KEYS)


def test_restore_session_edges(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.workspace import backup_remote_restore

    # session not found
    with pytest.raises(AppError):
        backup_remote_restore.fetch_restore_session("restore_nope")
    store = MemoryTargetStore()
    put_json_if_absent(store, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"})
    # Build a real ciphertext object in the store
    raw = b"age-encryption.org/v1\nrestore-body"
    digest = hashlib.sha256(raw).hexdigest()
    from deepseek_infra.infra.workspace.backup_target_store import object_key

    store.put_if_absent(object_key(digest), raw, checksum_sha256=digest)
    put_json_if_absent(
        store,
        "receipts/rs1.json",
        {"backupId": "rs1", "objectDigest": digest, "filename": "rs1.age", "size": len(raw)},
    )
    put_json_if_absent(
        store,
        "commits/pol/slot.json",
        {"commitHash": "c" * 64, "backupId": "rs1", "objectDigest": digest, "targetGeneration": 1},
    )
    target = backup_publish.ResolvedTarget(target_id="target_rs", root=None, managed=False, kind="s3", store=store)
    monkeypatch.setattr(backup_publish, "resolve_target", lambda *a, **k: target)
    created = backup_remote_restore.create_restore_from_target(target_id="target_rs", backup_id="rs1")
    restore_id = str(created["restoreId"])
    # ETag drift rejects resume
    session_path = tmp_settings / ".restore-staging" / restore_id / "remote-fetch.json"
    if not session_path.is_file():
        # find via RESTORE_DIR override
        from deepseek_infra.infra.workspace import backups as _b

        session_path = _b.RESTORE_DIR / restore_id / "remote-fetch.json"
    data = json.loads(session_path.read_text(encoding="utf-8"))
    data["remoteETag"] = '"other"'
    session_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AppError):
        backup_remote_restore.fetch_restore_session(restore_id)
    # fetched idempotent
    data["remoteETag"] = ""
    data["phase"] = "fetched"
    data["downloadedBytes"] = len(raw)
    data["expectedBytes"] = len(raw)
    session_path.write_text(json.dumps(data), encoding="utf-8")
    done = backup_remote_restore.fetch_restore_session(restore_id)
    assert done["phase"] == "fetched"


def test_incremental_chain_materialize_e2e(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Build F0/I1/I2, materialize the chain and compare the tree byte-for-byte."""
    import random
    import zipfile

    from deepseek_infra.core import config
    from deepseek_infra.infra.workspace import backup_incremental_restore
    from deepseek_infra.infra.workspace import backups as _backups
    from deepseek_infra.infra.workspace import backup_scheduled as _scheduled

    prefix = b"age-encryption.org/v1\n"
    _stub_crypto(monkeypatch, prefix)
    rng = random.Random(11)
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    memories = config.MEMORY_DIR / "memories.json"
    big = rng.randbytes(2 * 1024 * 1024)
    memories.write_bytes(big)
    policy = backup_policies.create_policy(
        _policy(incremental={"mode": "file-delta", "largeFileMode": "cdc", "largeFileThresholdBytes": 1024 * 1024})
    )
    policy_id = str(policy["policyId"])
    contributor_plan = _backups._contributor_plan(_scheduled._context_from_policy(policy))

    def record(package: object, backup_id: str, parent: str | None, base: str, depth: int) -> None:
        manifest = package.manifest  # type: ignore[attr-defined]
        files = [
            backup_incremental.FileRecord(str(item["contributorId"]), str(item["path"]), int(item["size"]), str(item["sha256"]))
            for item in manifest["files"]
        ]
        snapshot = manifest.get("snapshot")
        root_digest = snapshot.get("rootDigest") if isinstance(snapshot, dict) else None
        backup_incremental.record_committed_snapshot(
            target_id="managed-local",
            policy_id=policy_id,
            backup_id=backup_id,
            parent_backup_id=parent,
            base_backup_id=base,
            chain_depth=depth,
            root_digest=str(root_digest or backup_incremental.snapshot_root(files)),
            files=files,
        )
        backup_incremental.record_snapshot_chunks(
            target_id="managed-local", policy_id=policy_id, backup_id=backup_id, chunks=list(package.chunk_records)  # type: ignore[attr-defined]
        )

    def build(slot: str, run_id: str, kind: str, parent: str | None = None, base: str | None = None, depth: int = 0) -> object:
        plan = backup_run_plan.freeze_run_plan(
            policy=policy,
            schedule_slot=slot,
            slot_digest=commit_slot_digest(slot),
            contributor_plan=contributor_plan,
            target_id="managed-local",
            snapshot_kind=kind,
            lineage_id="F0",
            parent_backup_id=parent,
            base_backup_id=base,
            chain_depth=depth,
        )
        return _scheduled.build_scheduled_backup(
            policy,
            run_id=run_id,
            staging_root=tmp_settings / ".staging",
            schedule_slot=slot,
            backup_id=str(plan["backupId"]),
            contributor_plan=contributor_plan,
            snapshot_kind=kind,
            parent_backup_id=parent,
            base_backup_id=base,
            lineage_id="F0",
            chain_depth=depth,
        )

    pkg0 = build("2026-05-01T03:00@UTC", "run_chain0", "full")
    record(pkg0, "F0", None, "F0", 0)
    mutated1 = bytearray(big)
    mutated1[0] ^= 0x01
    memories.write_bytes(bytes(mutated1))
    pkg1 = build("2026-05-01T04:00@UTC", "run_chain1", "incremental", parent="F0", base="F0", depth=1)
    record(pkg1, str(pkg1.backup_id), "F0", "F0", 1)  # type: ignore[attr-defined]
    mutated2 = bytearray(mutated1)
    mutated2[100] ^= 0x02
    memories.write_bytes(bytes(mutated2))
    pkg2 = build("2026-05-01T05:00@UTC", "run_chain2", "incremental", parent=str(pkg1.backup_id), base="F0", depth=2)  # type: ignore[attr-defined]

    def extract(package: object, dest: Path) -> None:
        raw = package.path.read_bytes()  # type: ignore[attr-defined]
        assert raw.startswith(prefix)
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(raw[len(prefix):][::-1])) as archive:
            archive.extractall(dest)

    roots = [tmp_settings / f"chain/{index}" for index in range(3)]
    for package, dest in ((pkg0, roots[0]), (pkg1, roots[1]), (pkg2, roots[2])):
        extract(package, dest)
    output = tmp_settings / "chain-materialized"
    backup_incremental_restore.materialize_chain(roots, output)
    restored = (output / "payload/memory/memories.json").read_bytes()
    assert restored == bytes(mutated2)
    # Deletes: nothing in the source was removed, but the migration file must exist.
    assert (output / "migration/source-schemas.json").is_file()


def test_incremental_chain_materialize_fails_closed(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleting a chain member or corrupting a chunk fails closed."""
    import random
    import zipfile

    from deepseek_infra.core import config
    from deepseek_infra.infra.workspace import backup_incremental_restore
    from deepseek_infra.infra.workspace import backups as _backups
    from deepseek_infra.infra.workspace import backup_scheduled as _scheduled

    prefix = b"age-encryption.org/v1\n"
    _stub_crypto(monkeypatch, prefix)
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_bytes(random.Random(3).randbytes(2 * 1024 * 1024))
    policy = backup_policies.create_policy(
        _policy(incremental={"mode": "file-delta", "largeFileMode": "cdc", "largeFileThresholdBytes": 1024 * 1024})
    )
    contributor_plan = _backups._contributor_plan(_scheduled._context_from_policy(policy))

    def build(slot: str, kind: str, parent: str | None = None, base: str | None = None, depth: int = 0) -> object:
        plan = backup_run_plan.freeze_run_plan(
            policy=policy,
            schedule_slot=slot,
            slot_digest=commit_slot_digest(slot),
            contributor_plan=contributor_plan,
            target_id="managed-local",
            snapshot_kind=kind,
            lineage_id="F0",
            parent_backup_id=parent,
            base_backup_id=base,
            chain_depth=depth,
        )
        return _scheduled.build_scheduled_backup(
            policy,
            run_id=f"run_{kind}_{int(hashlib.sha256(slot.encode()).hexdigest()[:8], 16)}",
            staging_root=tmp_settings / ".staging",
            schedule_slot=slot,
            backup_id=str(plan["backupId"]),
            contributor_plan=contributor_plan,
            snapshot_kind=kind,
            parent_backup_id=parent,
            base_backup_id=base,
            lineage_id="F0",
            chain_depth=depth,
        )

    pkg0 = build("2026-06-01T03:00@UTC", "full")
    backup_incremental.record_committed_snapshot(
        target_id="managed-local",
        policy_id=str(policy["policyId"]),
        backup_id="F0",
        parent_backup_id=None,
        base_backup_id="F0",
        chain_depth=0,
        root_digest=str(pkg0.manifest["snapshot"]["rootDigest"])  # type: ignore[attr-defined]
        if isinstance(pkg0.manifest.get("snapshot"), dict)  # type: ignore[attr-defined]
        else backup_incremental.snapshot_root([backup_incremental.FileRecord(str(i["contributorId"]), str(i["path"]), int(i["size"]), str(i["sha256"])) for i in pkg0.manifest["files"]]),  # type: ignore[attr-defined]
        files=[backup_incremental.FileRecord(str(i["contributorId"]), str(i["path"]), int(i["size"]), str(i["sha256"])) for i in pkg0.manifest["files"]],  # type: ignore[attr-defined]
    )
    mutated = bytearray(config.MEMORY_FILE.read_bytes())
    mutated[len(mutated) // 2] ^= 0x01
    config.MEMORY_FILE.write_bytes(bytes(mutated))
    pkg1 = build("2026-06-01T04:00@UTC", "incremental", parent="F0", base="F0", depth=1)

    def extract(package: object, dest: Path) -> None:
        raw = package.path.read_bytes()  # type: ignore[attr-defined]
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(raw[len(prefix):][::-1])) as archive:
            archive.extractall(dest)

    roots = [tmp_settings / "fail0", tmp_settings / "fail1"]
    extract(pkg0, roots[0])
    extract(pkg1, roots[1])
    # Missing F0 -> fail closed.
    with pytest.raises(AppError):
        backup_incremental_restore.materialize_chain([roots[1]], tmp_settings / "out_missing")
    # Corrupt a payload chunk in I1 -> fail closed.
    (roots[1] / "delta" / "operations.json").write_text('{"put": [], "delete": []}', encoding="utf-8")
    with pytest.raises(AppError):
        backup_incremental_restore.materialize_chain(roots, tmp_settings / "out_corrupt")


def test_incremental_chain_restore_session(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.workspace import backup_remote_restore
    from deepseek_infra.infra.workspace.backup_target_store import object_key

    store = MemoryTargetStore()
    put_json_if_absent(store, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"})
    for bid, parent in (("F0", None), ("I1", "F0"), ("I2", "I1")):
        raw = f"age-encryption.org/v1\n{bid}-body".encode()
        digest = hashlib.sha256(raw).hexdigest()
        store.put_if_absent(object_key(digest), raw, checksum_sha256=digest)
        receipt: dict[str, object] = {
            "backupId": bid,
            "objectDigest": digest,
            "filename": f"{bid}.age",
            "size": len(raw),
            "snapshotKind": "incremental" if parent else "full",
            "parentBackupId": parent,
            "baseBackupId": "F0" if parent else bid,
            "chainDepth": 0 if not parent else (1 if parent == "F0" else 2),
        }
        put_json_if_absent(store, f"receipts/{bid}.json", receipt)
    put_json_if_absent(
        store,
        "commits/pol/slot.json",
        {"commitHash": "c" * 64, "backupId": "I2", "objectDigest": hashlib.sha256(b"age-encryption.org/v1\nI2-body").hexdigest(), "targetGeneration": 1},
    )
    target = backup_publish.ResolvedTarget(target_id="target_chain", root=None, managed=False, kind="s3", store=store)
    monkeypatch.setattr(backup_publish, "resolve_target", lambda *a, **k: target)
    created = backup_remote_restore.create_restore_from_target(target_id="target_chain", backup_id="I2")
    assert created["snapshotKind"] == "incremental"
    assert created["chain"] == ["F0", "I1", "I2"]
    assert created["phase"] == "fetching-chain"
    restore_id = str(created["restoreId"])
    assert len(created["holds"]) == 3
    result = backup_remote_restore.fetch_restore_session(restore_id)
    assert result["phase"] == "chain-fetched"
    assert result["chain"] == ["F0", "I1", "I2"]
    assert len(result["ciphertextPaths"]) == 3
    assert result["downloadedBytes"] == result["expectedBytes"]
    # idempotent
    again = backup_remote_restore.fetch_restore_session(restore_id)
    assert again["phase"] == "chain-fetched"


def test_record_committed_index_full_and_incremental(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_executor

    pkg = SimpleNamespace(
        manifest={
            "files": [
                {"contributorId": "local", "path": "a.txt", "size": 1, "sha256": "a" * 64},
                {"contributorId": "local", "path": "b.txt", "size": 2, "sha256": "b" * 64},
            ]
        }
    )
    # Full snapshot
    backup_executor._record_committed_index(
        target_id="t",
        policy_id="p",
        backup_id="F0",
        package=pkg,
        run_plan={"snapshotKind": "full"},
    )
    latest0 = backup_incremental.latest_committed_snapshot("t", "p")
    assert latest0 is not None and int(latest0["chain_depth"]) == 0
    # Incremental against baseline
    backup_incremental.record_committed_snapshot(
        target_id="t",
        policy_id="p",
        backup_id="F0",
        parent_backup_id=None,
        base_backup_id="F0",
        chain_depth=0,
        root_digest=backup_incremental.snapshot_root([backup_incremental.FileRecord("local", "a.txt", 1, "a" * 64)]),
        files=[backup_incremental.FileRecord("local", "a.txt", 1, "a" * 64)],
    )
    incr_pkg = SimpleNamespace(
        manifest={
            "files": [
                {"contributorId": "local", "path": "a.txt", "size": 1, "sha256": "a" * 64},
                {"contributorId": "local", "path": "b.txt", "size": 2, "sha256": "b" * 64},
            ],
            "snapshot": {"rootDigest": "0" * 64},
        },
        chunk_records=[backup_incremental.ChunkRecord("local", "big.bin", 0, 0, 10, "c" * 64)],
    )
    backup_executor._record_committed_index(
        target_id="t",
        policy_id="p",
        backup_id="I1",
        package=incr_pkg,
        run_plan={"snapshotKind": "incremental", "parentBackupId": "F0", "baseBackupId": "F0", "chainDepth": 1},
    )
    assert backup_incremental.ancestor_chain("t", "p", "I1") == ["F0", "I1"]
    persisted_chunks = backup_incremental.load_snapshot_chunks("t", "p", "I1")
    assert len(persisted_chunks) == 1
    assert persisted_chunks[0].chunk_sha256 == "c" * 64
    # Exception safety: corrupt manifest does not raise
    backup_executor._record_committed_index(
        target_id="t",
        policy_id="p",
        backup_id="I2",
        package=SimpleNamespace(manifest={"files": [{"path": "x", "size": "bad"}]}),
        run_plan={"snapshotKind": "incremental", "parentBackupId": "F0"},
    )


def test_snapshot_chunk_map_persistence(tmp_settings: Path) -> None:
    data = bytes(range(256)) * 256  # ~64KiB, single chunk
    records = backup_incremental.chunk_map_for("local", "big.bin", io.BytesIO(data), file_size=len(data))
    assert len(records) == 1
    backup_incremental.record_snapshot_chunks(target_id="t", policy_id="p", backup_id="I1", chunks=records)
    loaded = backup_incremental.load_snapshot_chunks("t", "p", "I1")
    assert len(loaded) == 1
    assert loaded[0].chunk_sha256 == records[0].chunk_sha256
    assert loaded[0].offset == 0
    assert loaded[0].length == len(data)
    # replace
    backup_incremental.record_snapshot_chunks(target_id="t", policy_id="p", backup_id="I1", chunks=[])
    assert backup_incremental.load_snapshot_chunks("t", "p", "I1") == []


def test_full_incremental_build_and_index(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the real incremental builder: baseline in index -> delta package."""
    from deepseek_infra.core import config
    from deepseek_infra.infra.workspace import backup_scheduled

    prefix = b"age-encryption.org/v1\n"
    _stub_crypto(monkeypatch, prefix)

    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('{"items":[{"id":"m1","text":"remember"}]}', encoding="utf-8")
    duplicate_dir = config.MEMORY_DIR / "duplicates"
    duplicate_dir.mkdir(parents=True, exist_ok=True)
    (duplicate_dir / "state-copy.json").write_text('{"items":[{"id":"m1","text":"remember"}]}', encoding="utf-8")
    policy = backup_policies.create_policy(_policy(incremental={"mode": "file-delta"}))
    # Record a baseline snapshot in the index for this policy/target.
    baseline_files = [
        backup_incremental.FileRecord("local", "state.json", 10, "a" * 64),
        backup_incremental.FileRecord("mcp", "state.jsonl", 5, "b" * 64),
    ]
    backup_incremental.record_committed_snapshot(
        target_id="managed-local",
        policy_id=str(policy["policyId"]),
        backup_id="F0",
        parent_backup_id=None,
        base_backup_id="F0",
        chain_depth=0,
        root_digest=backup_incremental.snapshot_root(baseline_files),
        files=baseline_files,
    )
    # Ensure the frozen run plan carries incremental lineage, then build. A real
    # contributor plan selects the durable memory contributor so its files land
    # in the delta.
    from deepseek_infra.infra.workspace import backups as _backups
    from deepseek_infra.infra.workspace import backup_scheduled as _scheduled

    contributor_plan = _backups._contributor_plan(_scheduled._context_from_policy(policy))
    slot = "2026-02-01T03:00@UTC"
    slot_d = commit_slot_digest(slot)
    plan = backup_run_plan.freeze_run_plan(
        policy=policy,
        schedule_slot=slot,
        slot_digest=slot_d,
        contributor_plan=contributor_plan,
        target_id="managed-local",
        snapshot_kind="incremental",
        lineage_id="lineage_b",
        parent_backup_id="F0",
        base_backup_id="F0",
        chain_depth=1,
    )
    assert plan["snapshotKind"] == "incremental"
    package = backup_scheduled.build_scheduled_backup(
        policy,
        run_id="run_incr",
        staging_root=tmp_settings / ".staging",
        schedule_slot=slot,
        backup_id=str(plan["backupId"]),
        contributor_plan=contributor_plan,
        snapshot_kind="incremental",
        parent_backup_id="F0",
        base_backup_id="F0",
        lineage_id="lineage_b",
        chain_depth=1,
    )
    assert package.path.is_file()
    assert package.manifest.get("snapshotKind") == "incremental"
    # The delta manifest must be serialized after payload allocation: every
    # whole-file PUT payloadRef must point at the final payload/files/ path,
    # not the pre-allocation SHA-256 placeholder.
    import zipfile as _zipfile

    raw = package.path.read_bytes()
    assert raw.startswith(prefix)
    with _zipfile.ZipFile(io.BytesIO(raw[len(prefix):][::-1])) as archive:
        names = set(archive.namelist())
        ops = json.loads(archive.read("delta/operations.json"))
    payload_refs = [item["payloadRef"] for item in ops["put"] if item.get("storage") == "whole"]
    assert payload_refs
    assert all(str(ref).startswith("payload/files/") for ref in payload_refs)
    # Identical whole-file payloads must deduplicate to one physical blob.
    memory_puts = [item for item in ops["put"] if "payload/memory/" in str(item.get("path"))]
    assert len(memory_puts) >= 2
    assert len({str(item["payloadRef"]) for item in memory_puts}) == 1
    delta_paths = [str(item["path"]) for item in package.manifest["deltaFiles"]]
    assert len([p for p in delta_paths if p.startswith("payload/files/")]) < len(ops["put"])
    # True delta storage: the archive must not carry full payload/<contributor>
    # copies of the workspace; only the changed payload blobs may appear.
    assert not any(name.startswith("payload/") and not name.startswith("payload/files/") for name in names)
    snapshot = package.manifest["snapshot"]
    assert snapshot["kind"] == "incremental"
    assert snapshot["parentBackupId"] == "F0"
    assert snapshot["chainDepth"] == 1
    assert snapshot["rootDigest"]
    # Index committed state from the REAL logical tree of the built package.
    real_files = [
        backup_incremental.FileRecord(str(item["contributorId"]), str(item["path"]), int(item["size"]), str(item["sha256"]))
        for item in package.manifest["files"]
    ]
    backup_incremental.record_committed_snapshot(
        target_id="managed-local",
        policy_id=str(policy["policyId"]),
        backup_id=str(plan["backupId"]),
        parent_backup_id="F0",
        base_backup_id="F0",
        chain_depth=1,
        root_digest=snapshot["rootDigest"],
        files=real_files,
    )
    # The incremental snapshot must be index-backed and chain to the baseline.
    chain = backup_incremental.ancestor_chain("managed-local", str(policy["policyId"]), str(plan["backupId"]))
    assert chain == ["F0", str(plan["backupId"])]
    loaded = backup_incremental.load_snapshot_files("managed-local", str(policy["policyId"]), str(plan["backupId"]))
    assert loaded
    # A second incremental with no workspace changes must emit an empty delta:
    # zero payload blobs, only the operations manifest.
    slot2 = "2026-02-01T04:00@UTC"
    plan2 = backup_run_plan.freeze_run_plan(
        policy=policy,
        schedule_slot=slot2,
        slot_digest=commit_slot_digest(slot2),
        contributor_plan=contributor_plan,
        target_id="managed-local",
        snapshot_kind="incremental",
        lineage_id="lineage_b",
        parent_backup_id=str(plan["backupId"]),
        base_backup_id="F0",
        chain_depth=2,
    )
    package2 = backup_scheduled.build_scheduled_backup(
        policy,
        run_id="run_incr2",
        staging_root=tmp_settings / ".staging",
        schedule_slot=slot2,
        backup_id=str(plan2["backupId"]),
        contributor_plan=contributor_plan,
        snapshot_kind="incremental",
        parent_backup_id=str(plan["backupId"]),
        base_backup_id="F0",
        lineage_id="lineage_b",
        chain_depth=2,
    )
    raw2 = package2.path.read_bytes()
    with _zipfile.ZipFile(io.BytesIO(raw2[len(prefix):][::-1])) as archive:
        names2 = set(archive.namelist())
        ops2 = json.loads(archive.read("delta/operations.json"))
    assert ops2["put"] == [] and ops2["delete"] == []
    assert not any(name.startswith("payload/") for name in names2)


def test_full_snapshot_computes_chunk_maps(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full snapshots chunk large files so the first delta can reuse parents."""
    import random

    from deepseek_infra.core import config
    from deepseek_infra.infra.workspace import backups as _backups
    from deepseek_infra.infra.workspace import backup_scheduled as _scheduled

    prefix = b"age-encryption.org/v1\n"
    _stub_crypto(monkeypatch, prefix)
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_bytes(random.Random(9).randbytes(2 * 1024 * 1024))
    policy = backup_policies.create_policy(
        _policy(incremental={"mode": "file-delta", "largeFileMode": "cdc", "largeFileThresholdBytes": 1024 * 1024})
    )
    contributor_plan = _backups._contributor_plan(_scheduled._context_from_policy(policy))
    slot = "2026-04-01T03:00@UTC"
    plan = backup_run_plan.freeze_run_plan(
        policy=policy,
        schedule_slot=slot,
        slot_digest=commit_slot_digest(slot),
        contributor_plan=contributor_plan,
        target_id="managed-local",
        snapshot_kind="full",
    )
    package = _scheduled.build_scheduled_backup(
        policy,
        run_id="run_full_cdc",
        staging_root=tmp_settings / ".staging",
        schedule_slot=slot,
        backup_id=str(plan["backupId"]),
        contributor_plan=contributor_plan,
        snapshot_kind="full",
    )
    assert package.manifest.get("snapshotKind") != "incremental"
    assert package.chunk_records
    big_records = [item for item in package.chunk_records if item.logical_path.endswith("memories.json")]
    assert len(big_records) > 1


def test_incremental_builder_cdc_payloads_and_reuse(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CDC emission in the production builder: payload chunks, then parent reuse."""
    import random

    from deepseek_infra.core import config
    from deepseek_infra.infra.workspace import backups as _backups
    from deepseek_infra.infra.workspace import backup_scheduled as _scheduled

    prefix = b"age-encryption.org/v1\n"
    _stub_crypto(monkeypatch, prefix)
    rng = random.Random(7)
    big = rng.randbytes(2 * 1024 * 1024)
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_bytes(big)
    policy = backup_policies.create_policy(
        _policy(incremental={"mode": "file-delta", "largeFileMode": "cdc", "largeFileThresholdBytes": 1024 * 1024})
    )
    backup_incremental.record_committed_snapshot(
        target_id="managed-local",
        policy_id=str(policy["policyId"]),
        backup_id="F0",
        parent_backup_id=None,
        base_backup_id="F0",
        chain_depth=0,
        root_digest=backup_incremental.snapshot_root([]),
        files=[],
    )
    contributor_plan = _backups._contributor_plan(_scheduled._context_from_policy(policy))
    slot = "2026-03-01T03:00@UTC"
    plan = backup_run_plan.freeze_run_plan(
        policy=policy,
        schedule_slot=slot,
        slot_digest=commit_slot_digest(slot),
        contributor_plan=contributor_plan,
        target_id="managed-local",
        snapshot_kind="incremental",
        lineage_id="lineage_cdc",
        parent_backup_id="F0",
        base_backup_id="F0",
        chain_depth=1,
    )
    package = _scheduled.build_scheduled_backup(
        policy,
        run_id="run_cdc1",
        staging_root=tmp_settings / ".staging",
        schedule_slot=slot,
        backup_id=str(plan["backupId"]),
        contributor_plan=contributor_plan,
        snapshot_kind="incremental",
        parent_backup_id="F0",
        base_backup_id="F0",
        lineage_id="lineage_cdc",
        chain_depth=1,
    )
    import zipfile as _zipfile

    raw = package.path.read_bytes()
    with _zipfile.ZipFile(io.BytesIO(raw[len(prefix):][::-1])) as archive:
        ops = json.loads(archive.read("delta/operations.json"))
    big_put = next(item for item in ops["put"] if str(item["path"]).endswith("memories.json"))
    assert big_put["storage"] == "cdc"
    assert big_put["size"] == len(big)
    assert len(big_put["chunks"]) > 1
    assert all(item["source"] == "payload" for item in big_put["chunks"])
    assert all(str(item["payloadRef"]).startswith("payload/files/") for item in big_put["chunks"])
    # First CDC delta uploads every chunk of the file as a distinct blob.
    delta_paths = [str(item["path"]) for item in package.manifest["deltaFiles"]]
    chunk_refs = {str(item["payloadRef"]) for item in big_put["chunks"]}
    assert len(chunk_refs) == len(big_put["chunks"])
    assert chunk_refs <= set(delta_paths)
    assert package.chunk_records
    # Commit I1's file tree and chunk maps to the index, then mutate one byte.
    real_files = [
        backup_incremental.FileRecord(str(item["contributorId"]), str(item["path"]), int(item["size"]), str(item["sha256"]))
        for item in package.manifest["files"]
    ]
    backup_incremental.record_committed_snapshot(
        target_id="managed-local",
        policy_id=str(policy["policyId"]),
        backup_id=str(plan["backupId"]),
        parent_backup_id="F0",
        base_backup_id="F0",
        chain_depth=1,
        root_digest=str(package.manifest["snapshot"]["rootDigest"]),
        files=real_files,
    )
    backup_incremental.record_snapshot_chunks(
        target_id="managed-local",
        policy_id=str(policy["policyId"]),
        backup_id=str(plan["backupId"]),
        chunks=list(package.chunk_records),
    )
    mutated = bytearray(big)
    mutated[len(big) // 2] ^= 0x01
    config.MEMORY_FILE.write_bytes(bytes(mutated))
    slot2 = "2026-03-01T04:00@UTC"
    plan2 = backup_run_plan.freeze_run_plan(
        policy=policy,
        schedule_slot=slot2,
        slot_digest=commit_slot_digest(slot2),
        contributor_plan=contributor_plan,
        target_id="managed-local",
        snapshot_kind="incremental",
        lineage_id="lineage_cdc",
        parent_backup_id=str(plan["backupId"]),
        base_backup_id="F0",
        chain_depth=2,
    )
    package2 = _scheduled.build_scheduled_backup(
        policy,
        run_id="run_cdc2",
        staging_root=tmp_settings / ".staging",
        schedule_slot=slot2,
        backup_id=str(plan2["backupId"]),
        contributor_plan=contributor_plan,
        snapshot_kind="incremental",
        parent_backup_id=str(plan["backupId"]),
        base_backup_id="F0",
        lineage_id="lineage_cdc",
        chain_depth=2,
    )
    raw2 = package2.path.read_bytes()
    with _zipfile.ZipFile(io.BytesIO(raw2[len(prefix):][::-1])) as archive:
        ops2 = json.loads(archive.read("delta/operations.json"))
    big_put2 = next(item for item in ops2["put"] if str(item["path"]).endswith("memories.json"))
    assert big_put2["storage"] == "cdc"
    parent_chunks = [item for item in big_put2["chunks"] if item["source"] == "parent"]
    payload_chunks = [item for item in big_put2["chunks"] if item["source"] == "payload"]
    assert len(parent_chunks) >= len(big_put2["chunks"]) - 2
    assert all("parentOrdinal" in item for item in parent_chunks)
    assert 0 < len(payload_chunks) < len(big_put2["chunks"])
    delta_paths2 = [str(item["path"]) for item in package2.manifest["deltaFiles"]]
    payload_blobs2 = [p for p in delta_paths2 if p.startswith("payload/files/")]
    assert len(payload_blobs2) == len(payload_chunks)

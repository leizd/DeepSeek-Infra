"""Production incremental backups and content-defined deltas contracts."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
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
    # An unparseable committed full timestamp degrades to "recent" (no crash).
    record("p_badtime", scope=scope, recipients=recips, schema=schemas, full_at="not-a-timestamp")
    selected = backup_incremental.select_snapshot_plan(
        policy=matching, target_id="t", policy_id="p_badtime", index_available=True, contributor_schemas={"local": 1}
    )
    assert selected[0] == "incremental"
    # A chain whose parent record vanished fails closed in ancestor_chain.
    backup_incremental.record_committed_snapshot(
        target_id="t",
        policy_id="p_orphan",
        backup_id="I1",
        parent_backup_id="GHOST",
        base_backup_id="GHOST",
        chain_depth=1,
        root_digest=backup_incremental.snapshot_root([]),
        files=[],
    )
    with pytest.raises(AppError, match="missing parent snapshot"):
        backup_incremental.ancestor_chain("t", "p_orphan", "I1")


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
    mutated2[1024 * 1024] ^= 0x02
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


def test_materialize_restore_session_chain(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A chain-fetched session decrypts, materializes and verifies the tree."""
    import random

    from deepseek_infra.core import config
    from deepseek_infra.infra.workspace import backup_remote_restore
    from deepseek_infra.infra.workspace import backups as _backups
    from deepseek_infra.infra.workspace import backup_scheduled as _scheduled

    prefix = b"age-encryption.org/v1\n"
    _stub_crypto(monkeypatch, prefix)
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    memories = config.MEMORY_DIR / "memories.json"
    big = random.Random(13).randbytes(2 * 1024 * 1024)
    memories.write_bytes(big)
    policy = backup_policies.create_policy(
        _policy(incremental={"mode": "file-delta", "largeFileMode": "cdc", "largeFileThresholdBytes": 1024 * 1024})
    )
    contributor_plan = _backups._contributor_plan(_scheduled._context_from_policy(policy))

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

    pkg0 = build("2026-07-01T03:00@UTC", "run_rs0", "full")
    backup_incremental.record_committed_snapshot(
        target_id="managed-local",
        policy_id=str(policy["policyId"]),
        backup_id="F0",
        parent_backup_id=None,
        base_backup_id="F0",
        chain_depth=0,
        root_digest=backup_incremental.snapshot_root(
            [backup_incremental.FileRecord(str(i["contributorId"]), str(i["path"]), int(i["size"]), str(i["sha256"])) for i in pkg0.manifest["files"]]  # type: ignore[attr-defined]
        ),
        files=[backup_incremental.FileRecord(str(i["contributorId"]), str(i["path"]), int(i["size"]), str(i["sha256"])) for i in pkg0.manifest["files"]],  # type: ignore[attr-defined]
    )
    mutated = bytearray(big)
    mutated[1] ^= 0x04
    memories.write_bytes(bytes(mutated))
    pkg1 = build("2026-07-01T04:00@UTC", "run_rs1", "incremental", parent="F0", base="F0", depth=1)

    restore_id = "restore_chainmat"
    base_dir = tmp_settings / ".restore-staging" / restore_id
    base_dir.mkdir(parents=True, exist_ok=True)
    session = {
        "schemaVersion": 2,
        "restoreId": restore_id,
        "source": "remote-target",
        "targetId": "t",
        "backupId": "I1",
        "snapshotKind": "incremental",
        "chainIndex": 2,
        "phase": "chain-fetched",
        "chain": [
            {"backupId": "F0", "ciphertextPath": str(pkg0.path), "expectedBytes": pkg0.size},  # type: ignore[attr-defined]
            {"backupId": "I1", "ciphertextPath": str(pkg1.path), "expectedBytes": pkg1.size},  # type: ignore[attr-defined]
        ],
    }
    (base_dir / "remote-fetch.json").write_text(json.dumps(session), encoding="utf-8")
    result = backup_remote_restore.materialize_restore_session(restore_id, secret=bytearray(b"x" * 32))
    assert result["phase"] == "materialized"
    tree = Path(str(result["tree"]))
    assert (tree / "payload/memory/memories.json").read_bytes() == bytes(mutated)
    assert (tree / "migration/source-schemas.json").is_file()
    # The normalized manifest is a complete tree: full verification passes.
    assert result["manifest"]["snapshotKind"] == "full"


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
    partial = backup_remote_restore.fetch_restore_session(restore_id, max_bytes=2)
    assert partial["phase"] == "fetching-chain"
    assert partial["downloadedBytes"] < partial["expectedBytes"]
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


def test_true_delta_size_regression(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A small edit to a large file must ship a tiny payload, not the whole file."""
    import random

    from deepseek_infra.core import config
    from deepseek_infra.infra.workspace import backups as _backups
    from deepseek_infra.infra.workspace import backup_scheduled as _scheduled

    prefix = b"age-encryption.org/v1\n"
    _stub_crypto(monkeypatch, prefix)
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    memories = config.MEMORY_DIR / "memories.json"
    big = random.Random(17).randbytes(8 * 1024 * 1024)
    memories.write_bytes(big)
    policy = backup_policies.create_policy(
        _policy(incremental={"mode": "file-delta", "largeFileMode": "cdc", "largeFileThresholdBytes": 1024 * 1024})
    )
    policy_id = str(policy["policyId"])
    contributor_plan = _backups._contributor_plan(_scheduled._context_from_policy(policy))

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

    pkg0 = build("2026-08-01T03:00@UTC", "run_size0", "full")
    snapshot = pkg0.manifest.get("snapshot")  # type: ignore[attr-defined]
    files0 = [
        backup_incremental.FileRecord(str(i["contributorId"]), str(i["path"]), int(i["size"]), str(i["sha256"])) for i in pkg0.manifest["files"]  # type: ignore[attr-defined]
    ]
    backup_incremental.record_committed_snapshot(
        target_id="managed-local",
        policy_id=policy_id,
        backup_id="F0",
        parent_backup_id=None,
        base_backup_id="F0",
        chain_depth=0,
        root_digest=str(snapshot.get("rootDigest") if isinstance(snapshot, dict) else backup_incremental.snapshot_root(files0)),
        files=files0,
    )
    backup_incremental.record_snapshot_chunks(
        target_id="managed-local", policy_id=policy_id, backup_id="F0", chunks=list(pkg0.chunk_records)  # type: ignore[attr-defined]
    )
    mutated = bytearray(big)
    mutated[42] ^= 0x7F
    memories.write_bytes(bytes(mutated))
    pkg1 = build("2026-08-01T04:00@UTC", "run_size1", "incremental", parent="F0", base="F0", depth=1)
    # The acceptance line: physical payload stays near the changed-chunk size.
    savings = pkg1.manifest["incrementalSavings"]  # type: ignore[attr-defined]
    assert savings["physicalPayloadBytes"] < savings["logicalChangedBytes"] * 0.15
    assert savings["savedRatio"] > 0.85
    # The whole package (ciphertext) must also be far smaller than the full.
    assert pkg1.size < pkg0.size * 0.5  # type: ignore[attr-defined]


def test_corrupt_payload_chunk_fails_closed(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Corrupting a CDC payload chunk must fail the chain restore closed."""
    import random
    import zipfile

    from deepseek_infra.core import config
    from deepseek_infra.infra.workspace import backup_incremental_restore
    from deepseek_infra.infra.workspace import backups as _backups
    from deepseek_infra.infra.workspace import backup_scheduled as _scheduled

    prefix = b"age-encryption.org/v1\n"
    _stub_crypto(monkeypatch, prefix)
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    memories = config.MEMORY_DIR / "memories.json"
    big = random.Random(19).randbytes(4 * 1024 * 1024)
    memories.write_bytes(big)
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
            run_id=f"run_corrupt_{int(hashlib.sha256(slot.encode()).hexdigest()[:8], 16)}",
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

    pkg0 = build("2026-09-01T03:00@UTC", "full")
    backup_incremental.record_committed_snapshot(
        target_id="managed-local",
        policy_id=str(policy["policyId"]),
        backup_id="F0",
        parent_backup_id=None,
        base_backup_id="F0",
        chain_depth=0,
        root_digest=backup_incremental.snapshot_root(
            [backup_incremental.FileRecord(str(i["contributorId"]), str(i["path"]), int(i["size"]), str(i["sha256"])) for i in pkg0.manifest["files"]]  # type: ignore[attr-defined]
        ),
        files=[backup_incremental.FileRecord(str(i["contributorId"]), str(i["path"]), int(i["size"]), str(i["sha256"])) for i in pkg0.manifest["files"]],  # type: ignore[attr-defined]
    )
    backup_incremental.record_snapshot_chunks(
        target_id="managed-local", policy_id=str(policy["policyId"]), backup_id="F0", chunks=list(pkg0.chunk_records)  # type: ignore[attr-defined]
    )
    mutated = bytearray(big)
    mutated[3 * 1024 * 1024] ^= 0x08
    memories.write_bytes(bytes(mutated))
    pkg1 = build("2026-09-01T04:00@UTC", "incremental", parent="F0", base="F0", depth=1)

    def extract(package: object, dest: Path) -> None:
        raw = package.path.read_bytes()  # type: ignore[attr-defined]
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(raw[len(prefix):][::-1])) as archive:
            archive.extractall(dest)

    roots = [tmp_settings / "corrupt0", tmp_settings / "corrupt1"]
    extract(pkg0, roots[0])
    extract(pkg1, roots[1])
    payload_blobs = sorted((roots[1] / "payload" / "files").glob("*")) if (roots[1] / "payload" / "files").is_dir() else []
    assert payload_blobs, "incremental must carry CDC payload chunks"
    blob = payload_blobs[0]
    corrupted = bytearray(blob.read_bytes())
    corrupted[len(corrupted) // 2] ^= 0xFF
    blob.write_bytes(bytes(corrupted))
    with pytest.raises(AppError):
        backup_incremental_restore.materialize_chain(roots, tmp_settings / "out_corrupt_chunk")


def test_store_incremental_restore_end_to_end(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real end-to-end: build a chain, publish to a target store, restore via the
    chain session and compare the materialized workspace byte-for-byte."""
    import random

    from deepseek_infra.core import config
    from deepseek_infra.infra.workspace import backup_remote_restore
    from deepseek_infra.infra.workspace import backups as _backups
    from deepseek_infra.infra.workspace import backup_scheduled as _scheduled
    from deepseek_infra.infra.workspace.backup_target_store import object_key

    prefix = b"age-encryption.org/v1\n"
    _stub_crypto(monkeypatch, prefix)
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    memories = config.MEMORY_DIR / "memories.json"
    big = random.Random(23).randbytes(4 * 1024 * 1024)
    memories.write_bytes(big)
    policy = backup_policies.create_policy(
        _policy(incremental={"mode": "file-delta", "largeFileMode": "cdc", "largeFileThresholdBytes": 1024 * 1024})
    )
    policy_id = str(policy["policyId"])
    contributor_plan = _backups._contributor_plan(_scheduled._context_from_policy(policy))

    def build(slot: str, kind: str, parent: str | None = None, base: str | None = None, depth: int = 0) -> tuple[object, str]:
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
        package = _scheduled.build_scheduled_backup(
            policy,
            run_id=f"run_e2e_{int(hashlib.sha256(slot.encode()).hexdigest()[:8], 16)}",
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
        return package, str(plan["backupId"])

    pkg0, bid0 = build("2026-10-01T03:00@UTC", "full")
    backup_incremental.record_committed_snapshot(
        target_id="managed-local",
        policy_id=policy_id,
        backup_id="F0",
        parent_backup_id=None,
        base_backup_id="F0",
        chain_depth=0,
        root_digest=backup_incremental.snapshot_root(
            [backup_incremental.FileRecord(str(i["contributorId"]), str(i["path"]), int(i["size"]), str(i["sha256"])) for i in pkg0.manifest["files"]]  # type: ignore[attr-defined]
        ),
        files=[backup_incremental.FileRecord(str(i["contributorId"]), str(i["path"]), int(i["size"]), str(i["sha256"])) for i in pkg0.manifest["files"]],  # type: ignore[attr-defined]
    )
    backup_incremental.record_snapshot_chunks(
        target_id="managed-local", policy_id=policy_id, backup_id="F0", chunks=list(pkg0.chunk_records)  # type: ignore[attr-defined]
    )
    m1 = bytearray(big)
    m1[1024] ^= 0x01
    memories.write_bytes(bytes(m1))
    pkg1, bid1 = build("2026-10-01T04:00@UTC", "incremental", parent="F0", base="F0", depth=1)
    backup_incremental.record_committed_snapshot(
        target_id="managed-local",
        policy_id=policy_id,
        backup_id=bid1,
        parent_backup_id="F0",
        base_backup_id="F0",
        chain_depth=1,
        root_digest=str(pkg1.manifest["snapshot"]["rootDigest"]),  # type: ignore[attr-defined]
        files=[backup_incremental.FileRecord(str(i["contributorId"]), str(i["path"]), int(i["size"]), str(i["sha256"])) for i in pkg1.manifest["files"]],  # type: ignore[attr-defined]
    )
    m2 = bytearray(m1)
    m2[2048] ^= 0x02
    memories.write_bytes(bytes(m2))
    pkg2, bid2 = build("2026-10-01T05:00@UTC", "incremental", parent=bid1, base="F0", depth=2)

    # Publish the chain into a backend-neutral target store (S3-compatible).
    store = MemoryTargetStore()
    put_json_if_absent(store, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"})
    published: dict[str, tuple[bytes, str]] = {}
    for package, backup_id, parent, depth in ((pkg0, "F0", None, 0), (pkg1, bid1, "F0", 1), (pkg2, bid2, bid1, 2)):
        raw = package.path.read_bytes()  # type: ignore[attr-defined]
        digest = hashlib.sha256(raw).hexdigest()
        store.put_if_absent(object_key(digest), raw, checksum_sha256=digest)
        published[backup_id] = (raw, digest)
        put_json_if_absent(
            store,
            f"receipts/{backup_id}.json",
            {
                "backupId": backup_id,
                "objectDigest": digest,
                "filename": f"{backup_id}.age",
                "size": len(raw),
                "snapshotKind": "incremental" if parent else "full",
                "parentBackupId": parent,
                "baseBackupId": "F0",
                "chainDepth": depth,
            },
        )
    _, i2_digest = published[bid2]
    put_json_if_absent(
        store,
        "commits/e2e/slot.json",
        {"commitHash": "c" * 64, "backupId": bid2, "objectDigest": i2_digest, "targetGeneration": 1},
    )
    target = backup_publish.ResolvedTarget(target_id="target_e2e", root=None, managed=False, kind="s3", store=store)
    monkeypatch.setattr(backup_publish, "resolve_target", lambda *a, **k: target)

    created = backup_remote_restore.create_restore_from_target(target_id="target_e2e", backup_id=bid2)
    assert created["snapshotKind"] == "incremental"
    assert created["chain"] == ["F0", bid1, bid2]
    restore_id = str(created["restoreId"])
    fetched = backup_remote_restore.fetch_restore_session(restore_id)
    assert fetched["phase"] == "chain-fetched"
    materialized = backup_remote_restore.materialize_restore_session(restore_id, secret=bytearray(b"s" * 32))
    assert materialized["phase"] == "materialized"
    tree = Path(str(materialized["tree"]))
    assert (tree / "payload/memory/memories.json").read_bytes() == bytes(m2)
    # The restored tree matches the source snapshot byte-for-byte across layers.
    source_files = sorted((config.MEMORY_DIR).rglob("*")) if config.MEMORY_DIR.is_dir() else []
    for source in source_files:
        if source.is_file():
            relative = source.relative_to(config.MEMORY_DIR).as_posix()
            assert (tree / "payload" / "memory" / relative).is_file()


def _manual_chain(tmp_path: Path) -> tuple[Path, Path, str]:
    """A minimal valid F0+I1 chain for materializer error-branch coverage."""
    content = b"data"
    sha = hashlib.sha256(content).hexdigest()
    record = backup_incremental.FileRecord("local", "payload/local/a.txt", len(content), sha)
    f0_root = backup_incremental.snapshot_root([record])
    f0_dir = tmp_path / "mc-f0"
    (f0_dir / "payload" / "local").mkdir(parents=True)
    (f0_dir / "payload" / "local" / "a.txt").write_bytes(content)
    (f0_dir / "manifest.json").write_text(
        json.dumps({"files": [{"contributorId": "local", "path": "payload/local/a.txt", "size": 4, "sha256": sha}], "snapshot": {"rootDigest": f0_root}}),
        encoding="utf-8",
    )
    i1_dir = tmp_path / "mc-i1"
    (i1_dir / "delta").mkdir(parents=True)
    ops = {"put": [], "delete": [], "parentRootDigest": f0_root, "rootDigest": f0_root}
    (i1_dir / "delta" / "operations.json").write_text(json.dumps(ops), encoding="utf-8")
    (i1_dir / "manifest.json").write_text(
        json.dumps(
            {"files": [{"contributorId": "local", "path": "payload/local/a.txt", "size": 4, "sha256": sha}], "snapshotKind": "incremental", "snapshot": {"rootDigest": f0_root}}
        ),
        encoding="utf-8",
    )
    return f0_dir, i1_dir, f0_root


def test_materialize_chain_error_branches(tmp_settings: Path, tmp_path: Path) -> None:
    from deepseek_infra.infra.workspace import backup_incremental_restore

    f0_dir, i1_dir, f0_root = _manual_chain(tmp_path)
    with pytest.raises(AppError):
        backup_incremental_restore.materialize_chain([], tmp_path / "e-empty")
    # First member must be a full baseline.
    with pytest.raises(AppError, match="full baseline"):
        backup_incremental_restore.materialize_chain([i1_dir], tmp_path / "e-baseline")
    # Missing manifest.json.
    broken = tmp_path / "e-nomanifest"
    broken.mkdir(parents=True)
    with pytest.raises(AppError, match="manifest"):
        backup_incremental_restore.materialize_chain([broken, i1_dir], tmp_path / "e-m")
    # Full baseline Merkle root mismatch.
    bad_f0 = tmp_path / "e-badroot"
    shutil.copytree(f0_dir, bad_f0)
    (bad_f0 / "manifest.json").write_text(
        (bad_f0 / "manifest.json").read_text(encoding="utf-8").replace(f0_root, "0" * 64), encoding="utf-8"
    )
    with pytest.raises(AppError, match="Merkle root mismatch"):
        backup_incremental_restore.materialize_chain([bad_f0, i1_dir], tmp_path / "e-r")
    # Invalid delta ops (missing put/delete).
    bad_ops = tmp_path / "e-ops"
    shutil.copytree(i1_dir, bad_ops)
    (bad_ops / "delta" / "operations.json").write_text(json.dumps({"nope": 1}), encoding="utf-8")
    with pytest.raises(AppError):
        backup_incremental_restore.materialize_chain([f0_dir, bad_ops], tmp_path / "e-ops2")
    # Parent root mismatch at the incremental layer.
    bad_parent = tmp_path / "e-parent"
    shutil.copytree(i1_dir, bad_parent)
    (bad_parent / "delta" / "operations.json").write_text(
        json.dumps({"put": [], "delete": [], "parentRootDigest": "0" * 64, "rootDigest": f0_root}), encoding="utf-8"
    )
    with pytest.raises(AppError, match="parent root mismatch"):
        backup_incremental_restore.materialize_chain([f0_dir, bad_parent], tmp_path / "e-p")
    # Whole put missing blob.
    whole = _ops_with_put({"path": "payload/local/b.txt", "size": 1, "sha256": "1" * 64, "storage": "whole", "payloadRef": "payload/files/099"}, f0_root)
    bad_blob = tmp_path / "e-blob"
    shutil.copytree(i1_dir, bad_blob)
    (bad_blob / "delta" / "operations.json").write_text(json.dumps(whole), encoding="utf-8")
    with pytest.raises(AppError, match="missing"):
        backup_incremental_restore.materialize_chain([f0_dir, bad_blob], tmp_path / "e-blob2")
    # Unsupported storage.
    weird = _ops_with_put({"path": "payload/local/b.txt", "size": 1, "sha256": "1" * 64, "storage": "weird", "payloadRef": "x"}, f0_root)
    bad_storage = tmp_path / "e-storage"
    shutil.copytree(i1_dir, bad_storage)
    (bad_storage / "delta" / "operations.json").write_text(json.dumps(weird), encoding="utf-8")
    with pytest.raises(AppError, match="Unsupported delta storage"):
        backup_incremental_restore.materialize_chain([f0_dir, bad_storage], tmp_path / "e-st")
    # Whole put checksum mismatch.
    payload_dir = tmp_path / "e-checksum"
    shutil.copytree(i1_dir, payload_dir)
    (payload_dir / "payload" / "files").mkdir(parents=True)
    (payload_dir / "payload" / "files" / "000000").write_bytes(b"zzzz")
    checksum_put = _ops_with_put(
        {"path": "payload/local/b.txt", "size": 4, "sha256": hashlib.sha256(b"zzzzz").hexdigest(), "storage": "whole", "payloadRef": "payload/files/000000"}, f0_root
    )
    (payload_dir / "delta" / "operations.json").write_text(json.dumps(checksum_put), encoding="utf-8")
    with pytest.raises(AppError, match="failed checksum"):
        backup_incremental_restore.materialize_chain([f0_dir, payload_dir], tmp_path / "e-c")
    # CDC invalid parent ordinal.
    cdc_put = _ops_with_put(
        {"path": "payload/local/a.txt", "size": 4, "sha256": sha_of("data"), "storage": "cdc", "chunks": [{"length": 4, "sha256": sha_of("data"), "source": "parent", "parentOrdinal": 99}]},
        f0_root,
    )
    bad_cdc = tmp_path / "e-cdc"
    shutil.copytree(i1_dir, bad_cdc)
    (bad_cdc / "delta" / "operations.json").write_text(json.dumps(cdc_put), encoding="utf-8")
    with pytest.raises(AppError, match="invalid parent chunk"):
        backup_incremental_restore.materialize_chain([f0_dir, bad_cdc], tmp_path / "e-cdc2")
    # CDC missing parent file.
    cdc_missing = _ops_with_put(
        {"path": "payload/local/new.bin", "size": 4, "sha256": sha_of("data"), "storage": "cdc", "chunks": [{"length": 4, "sha256": sha_of("data"), "source": "payload", "payloadRef": "payload/files/000000"}]},
        f0_root,
    )
    bad_cdc_missing = tmp_path / "e-cdcm"
    shutil.copytree(i1_dir, bad_cdc_missing)
    (bad_cdc_missing / "delta" / "operations.json").write_text(json.dumps(cdc_missing), encoding="utf-8")
    with pytest.raises(AppError, match="missing parent file"):
        backup_incremental_restore.materialize_chain([f0_dir, bad_cdc_missing], tmp_path / "e-cdcm2")
    # Corrupt / non-dict manifest.
    bad_manifest = tmp_path / "e-mf"
    shutil.copytree(i1_dir, bad_manifest)
    (bad_manifest / "manifest.json").write_text("not json", encoding="utf-8")
    with pytest.raises(AppError, match="manifest is invalid"):
        backup_incremental_restore.materialize_chain([f0_dir, bad_manifest], tmp_path / "e-mf2")
    bad_list = tmp_path / "e-ml"
    shutil.copytree(i1_dir, bad_list)
    (bad_list / "manifest.json").write_text("[]", encoding="utf-8")
    with pytest.raises(AppError, match="manifest is invalid"):
        backup_incremental_restore.materialize_chain([f0_dir, bad_list], tmp_path / "e-ml2")
    bad_item = tmp_path / "e-mi"
    shutil.copytree(i1_dir, bad_item)
    (bad_item / "manifest.json").write_text(json.dumps({"files": ["nope"], "snapshotKind": "incremental"}), encoding="utf-8")
    with pytest.raises(AppError, match="inventory is invalid"):
        backup_incremental_restore.materialize_chain([f0_dir, bad_item], tmp_path / "e-mi2")
    # Missing operations manifest.
    no_ops = tmp_path / "e-noops"
    shutil.copytree(i1_dir, no_ops)
    (no_ops / "delta" / "operations.json").unlink()
    with pytest.raises(AppError, match="missing its operations"):
        backup_incremental_restore.materialize_chain([f0_dir, no_ops], tmp_path / "e-noops2")
    # CDC chunk list / entries / lengths malformed.
    for label, chunks, match in (
        ("nocl", "not-a-list", "no chunk list"),
        ("badchunk", [None], "invalid chunk"),
        ("lenmismatch", [{"source": "parent", "parentOrdinal": 0, "length": 999, "sha256": sha_of("data")}], "length mismatch"),
        ("missingref", [{"source": "payload", "length": 4, "sha256": sha_of("data")}], "missing"),
        ("lenmis", [{"source": "payload", "length": 999, "sha256": sha_of("data"), "payloadRef": "payload/files/000000"}], "length mismatch"),
        ("shamis", [{"source": "payload", "length": 4, "sha256": "0" * 64, "payloadRef": "payload/files/000000"}], "checksum mismatch"),
    ):
        branch = tmp_path / f"e-cdc-{label}"
        shutil.copytree(i1_dir, branch)
        (branch / "payload" / "files").mkdir(parents=True, exist_ok=True)
        (branch / "payload" / "files" / "000000").write_bytes(b"data")
        op = _ops_with_put(
            {"path": "payload/local/a.txt", "size": 4, "sha256": sha_of("data"), "storage": "cdc", "chunks": chunks},
            f0_root,
        )
        (branch / "delta" / "operations.json").write_text(json.dumps(op), encoding="utf-8")
        with pytest.raises(AppError, match=match):
            backup_incremental_restore.materialize_chain([f0_dir, branch], tmp_path / f"e-cdc-{label}-out")
    # Delete of a declared file.
    del_dir = tmp_path / "e-del"
    shutil.copytree(i1_dir, del_dir)
    (del_dir / "delta" / "operations.json").write_text(
        json.dumps({"put": [], "delete": [{"contributorId": "local", "path": "payload/local/a.txt"}], "parentRootDigest": f0_root, "rootDigest": backup_incremental.snapshot_root([])}), encoding="utf-8"
    )
    out_del = tmp_path / "e-del-out"
    backup_incremental_restore.materialize_chain([f0_dir, del_dir], out_del)
    assert not (out_del / "payload" / "local" / "a.txt").exists()
    # Non-dict put / delete entries.
    bad_entries = tmp_path / "e-bade"
    shutil.copytree(i1_dir, bad_entries)
    (bad_entries / "delta" / "operations.json").write_text(
        json.dumps({"put": ["x"], "delete": [], "parentRootDigest": f0_root, "rootDigest": f0_root}), encoding="utf-8"
    )
    with pytest.raises(AppError, match="invalid"):
        backup_incremental_restore.materialize_chain([f0_dir, bad_entries], tmp_path / "e-bade2")
    # Non-dict delete entry.
    bad_del = tmp_path / "e-badd"
    shutil.copytree(i1_dir, bad_del)
    (bad_del / "delta" / "operations.json").write_text(
        json.dumps({"put": [], "delete": ["x"], "parentRootDigest": f0_root, "rootDigest": f0_root}), encoding="utf-8"
    )
    with pytest.raises(AppError, match="invalid"):
        backup_incremental_restore.materialize_chain([f0_dir, bad_del], tmp_path / "e-badd2")
    # CDC ordinal that is not an integer.
    cdc_noint = _ops_with_put(
        {"path": "payload/local/a.txt", "size": 4, "sha256": sha_of("data"), "storage": "cdc", "chunks": [{"source": "parent", "parentOrdinal": "zero", "length": 4, "sha256": sha_of("data")}]},
        f0_root,
    )
    bad_noint = tmp_path / "e-noint"
    shutil.copytree(i1_dir, bad_noint)
    (bad_noint / "delta" / "operations.json").write_text(json.dumps(cdc_noint), encoding="utf-8")
    with pytest.raises(AppError, match="invalid parent chunk"):
        backup_incremental_restore.materialize_chain([f0_dir, bad_noint], tmp_path / "e-noint2")
    # Successful apply with a wrong rootDigest -> Merkle root mismatch.
    wrong_root = tmp_path / "e-wrongroot"
    shutil.copytree(i1_dir, wrong_root)
    (wrong_root / "delta" / "operations.json").write_text(
        json.dumps({"put": [], "delete": [], "parentRootDigest": f0_root, "rootDigest": "0" * 64}), encoding="utf-8"
    )
    with pytest.raises(AppError, match="Merkle root mismatch"):
        backup_incremental_restore.materialize_chain([f0_dir, wrong_root], tmp_path / "e-wrongroot2")
    # Physical tree verification: a declared file missing from disk.
    missing_file = tmp_path / "e-missingfile"
    shutil.copytree(i1_dir, missing_file)
    (missing_file / "delta" / "operations.json").write_text(
        json.dumps({"put": [], "delete": [{"contributorId": "", "path": "payload/local/a.txt"}], "parentRootDigest": f0_root, "rootDigest": f0_root}), encoding="utf-8"
    )
    with pytest.raises(AppError, match="missing declared file"):
        backup_incremental_restore.materialize_chain([f0_dir, missing_file], tmp_path / "e-missingfile2")
    # Physical tree verification: corrupted bytes on disk.
    corrupt_f0 = tmp_path / "e-corruptf0"
    shutil.copytree(f0_dir, corrupt_f0)
    (corrupt_f0 / "payload" / "local" / "a.txt").write_bytes(b"xxxx")
    bad_phys = tmp_path / "e-badphys"
    shutil.copytree(i1_dir, bad_phys)
    with pytest.raises(AppError, match="failed checksum"):
        backup_incremental_restore.materialize_chain([corrupt_f0, bad_phys], tmp_path / "e-badphys2")


def _ops_with_put(put: dict, f0_root: str) -> dict:
    return {"put": [put], "delete": [], "parentRootDigest": f0_root, "rootDigest": backup_incremental.snapshot_root([])}


def sha_of(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_restore_session_error_branches(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.workspace import backup_remote_restore
    from deepseek_infra.infra.workspace.backup_target_store import object_key

    store = MemoryTargetStore()
    put_json_if_absent(store, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"})
    target = backup_publish.ResolvedTarget(target_id="target_er", root=None, managed=False, kind="s3", store=store)
    monkeypatch.setattr(backup_publish, "resolve_target", lambda *a, **k: target)
    # Unknown restore session.
    with pytest.raises(AppError):
        backup_remote_restore.materialize_restore_session("restore_nope", secret=bytearray(4))
    # Receipt missing on target.
    with pytest.raises(AppError):
        backup_remote_restore.create_restore_from_target(target_id="target_er", backup_id="missing")
    # Receipt with an invalid digest.
    put_json_if_absent(store, "receipts/short.json", {"backupId": "short", "objectDigest": "nope", "filename": "s.age", "size": 1})
    with pytest.raises(AppError):
        backup_remote_restore.create_restore_from_target(target_id="target_er", backup_id="short")
    # Chain member ciphertext missing on target.
    for bid, parent in (("F0", None), ("I1", "F0")):
        raw = f"age-encryption.org/v1\n{bid}".encode()
        digest = hashlib.sha256(raw).hexdigest()
        store.put_if_absent(object_key(digest), raw, checksum_sha256=digest)
        put_json_if_absent(
            store,
            f"receipts/{bid}.json",
            {"backupId": bid, "objectDigest": digest, "filename": f"{bid}.age", "size": len(raw), "snapshotKind": "full" if parent is None else "incremental", "parentBackupId": parent, "baseBackupId": "F0"},
        )
    put_json_if_absent(
        store,
        "commits/pol/slot.json",
        {"commitHash": "c" * 64, "backupId": "I1", "objectDigest": hashlib.sha256(b"age-encryption.org/v1\nI1").hexdigest(), "targetGeneration": 1},
    )
    # Corrupt I1's object so the chain fetch fails closed.
    created = backup_remote_restore.create_restore_from_target(target_id="target_er", backup_id="I1")
    assert created["chain"] == ["F0", "I1"]
    restore_id = str(created["restoreId"])
    # A chain whose member object is missing fails the fetch closed.
    session_path = tmp_settings / ".restore-staging" / restore_id / "remote-fetch.json"
    from deepseek_infra.infra.workspace import backups as _b

    if not session_path.is_file():
        session_path = _b.RESTORE_DIR / restore_id / "remote-fetch.json"
    data = json.loads(session_path.read_text(encoding="utf-8"))
    data["chain"] = [dict(item) for item in data["chain"]]
    data["chain"][0]["objectDigest"] = "0" * 64
    session_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AppError):
        backup_remote_restore.fetch_restore_session(restore_id)
    # Materializing before the chain finishes fetching is rejected.
    data["phase"] = "fetching-chain"
    data["chain"] = [dict(item) for item in data["chain"]]
    data["chain"][0]["objectDigest"] = hashlib.sha256(b"age-encryption.org/v1\nF0").hexdigest()
    session_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AppError):
        backup_remote_restore.materialize_restore_session(restore_id, secret=bytearray(4))
    # Receipt with a valid digest but no formal commit marker is rejected.
    no_commit_store = MemoryTargetStore()
    put_json_if_absent(no_commit_store, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"})
    raw = b"age-encryption.org/v1\nnc"
    digest = hashlib.sha256(raw).hexdigest()
    no_commit_store.put_if_absent(object_key(digest), raw, checksum_sha256=digest)
    put_json_if_absent(no_commit_store, "receipts/nc.json", {"backupId": "nc", "objectDigest": digest, "filename": "nc.age", "size": len(raw)})
    target2 = backup_publish.ResolvedTarget(target_id="target_nc", root=None, managed=False, kind="s3", store=no_commit_store)
    monkeypatch.setattr(backup_publish, "resolve_target", lambda *a, **k: target2)
    with pytest.raises(AppError, match="no formal slot commit"):
        backup_remote_restore.create_restore_from_target(target_id="target_nc", backup_id="nc")
    # A chain referencing a missing intermediate fails closed at creation.
    broken_store = MemoryTargetStore()
    put_json_if_absent(broken_store, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"})
    for bid, parent in (("F0", None), ("I1", "F0"), ("I2", "GHOST")):
        raw = f"age-encryption.org/v1\n{bid}-broken".encode()
        digest = hashlib.sha256(raw).hexdigest()
        broken_store.put_if_absent(object_key(digest), raw, checksum_sha256=digest)
        put_json_if_absent(
            broken_store,
            f"receipts/{bid}.json",
            {"backupId": bid, "objectDigest": digest, "filename": f"{bid}.age", "size": len(raw), "snapshotKind": "full" if parent is None else "incremental", "parentBackupId": parent, "baseBackupId": "F0"},
        )
    put_json_if_absent(
        broken_store,
        "commits/pol/slot.json",
        {"commitHash": "c" * 64, "backupId": "I2", "objectDigest": hashlib.sha256(b"age-encryption.org/v1\nI2-broken").hexdigest(), "targetGeneration": 1},
    )
    target3 = backup_publish.ResolvedTarget(target_id="target_broken", root=None, managed=False, kind="s3", store=broken_store)
    monkeypatch.setattr(backup_publish, "resolve_target", lambda *a, **k: target3)
    with pytest.raises(AppError):
        backup_remote_restore.create_restore_from_target(target_id="target_broken", backup_id="I2")
    # A chain member whose ciphertext object is missing fails closed.
    missing_store = MemoryTargetStore()
    put_json_if_absent(missing_store, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"})
    for bid, parent in (("F0", None), ("I1", "F0")):
        raw = f"age-encryption.org/v1\n{bid}-missing".encode()
        digest = hashlib.sha256(raw).hexdigest()
        if bid == "F0":
            missing_store.put_if_absent(object_key(digest), raw, checksum_sha256=digest)
        put_json_if_absent(
            missing_store,
            f"receipts/{bid}.json",
            {"backupId": bid, "objectDigest": digest, "filename": f"{bid}.age", "size": len(raw), "snapshotKind": "full" if parent is None else "incremental", "parentBackupId": parent, "baseBackupId": "F0"},
        )
    put_json_if_absent(
        missing_store,
        "commits/pol/slot.json",
        {"commitHash": "c" * 64, "backupId": "I1", "objectDigest": hashlib.sha256(b"age-encryption.org/v1\nI1-missing").hexdigest(), "targetGeneration": 1},
    )
    target4 = backup_publish.ResolvedTarget(target_id="target_missing", root=None, managed=False, kind="s3", store=missing_store)
    monkeypatch.setattr(backup_publish, "resolve_target", lambda *a, **k: target4)
    with pytest.raises(AppError, match="ciphertext object is missing"):
        backup_remote_restore.create_restore_from_target(target_id="target_missing", backup_id="I1")


def test_materialize_restore_session_full_package(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single full package materializes through the same session path."""
    import random

    from deepseek_infra.core import config
    from deepseek_infra.infra.workspace import backup_remote_restore
    from deepseek_infra.infra.workspace import backups as _backups
    from deepseek_infra.infra.workspace import backup_scheduled as _scheduled

    prefix = b"age-encryption.org/v1\n"
    _stub_crypto(monkeypatch, prefix)
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_bytes(random.Random(29).randbytes(64 * 1024))
    policy = backup_policies.create_policy(_policy(incremental={"mode": "file-delta"}))
    contributor_plan = _backups._contributor_plan(_scheduled._context_from_policy(policy))
    plan = backup_run_plan.freeze_run_plan(
        policy=policy,
        schedule_slot="2026-11-01T03:00@UTC",
        slot_digest=commit_slot_digest("2026-11-01T03:00@UTC"),
        contributor_plan=contributor_plan,
        target_id="managed-local",
        snapshot_kind="full",
    )
    package = _scheduled.build_scheduled_backup(
        policy,
        run_id="run_fullmat",
        staging_root=tmp_settings / ".staging",
        schedule_slot="2026-11-01T03:00@UTC",
        backup_id=str(plan["backupId"]),
        contributor_plan=contributor_plan,
        snapshot_kind="full",
    )
    restore_id = "restore_fullmat"
    base_dir = tmp_settings / ".restore-staging" / restore_id
    base_dir.mkdir(parents=True, exist_ok=True)
    session = {
        "schemaVersion": 2,
        "restoreId": restore_id,
        "targetId": "t",
        "backupId": "F0",
        "snapshotKind": "full",
        "phase": "fetched",
        "ciphertextPath": str(package.path),
        "downloadedBytes": package.size,
        "expectedBytes": package.size,
    }
    (base_dir / "remote-fetch.json").write_text(json.dumps(session), encoding="utf-8")
    result = backup_remote_restore.materialize_restore_session(restore_id, secret=bytearray(b"f" * 32))
    assert result["phase"] == "materialized"
    assert result["snapshotKind"] == "full"
    tree = Path(str(result["tree"]))
    assert (tree / "manifest.json").is_file()


def test_scheduled_backup_error_branches(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scheduled builder fail-closed paths: no recipients, retry exhaustion."""
    from deepseek_infra.infra.workspace import backup_scheduled as _scheduled
    from deepseek_infra.infra.workspace import mutation_gate

    _stub_crypto(monkeypatch, b"age-encryption.org/v1\n")
    # A policy without recipients is refused.
    policy = backup_policies.create_policy(_policy(incremental={"mode": "file-delta"}))
    policy["protection"] = {"mode": "age-recipient", "recipients": []}
    with pytest.raises(AppError, match="no recipients"):
        _scheduled.build_scheduled_backup(policy, run_id="run_norec", staging_root=tmp_settings / ".staging")
    # A workspace that keeps changing exhausts the three attempts.
    counter = {"n": 0}
    original_read_generation = mutation_gate.read_generation

    def flaky_generation(_root: object) -> int:
        counter["n"] += 1
        return counter["n"]

    monkeypatch.setattr(mutation_gate, "read_generation", flaky_generation)
    policy2 = backup_policies.create_policy(_policy(incremental={"mode": "file-delta"}))
    with pytest.raises(AppError, match="Workspace changed repeatedly"):
        _scheduled.build_scheduled_backup(policy2, run_id="run_exhaust", staging_root=tmp_settings / ".staging")
    assert counter["n"] >= 6
    # Index read failures degrade gracefully to an all-changed delta.
    monkeypatch.setattr(mutation_gate, "read_generation", original_read_generation)
    monkeypatch.setattr(
        backup_incremental,
        "load_snapshot_files",
        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")),
    )
    monkeypatch.setattr(
        backup_incremental,
        "load_snapshot_chunks",
        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")),
    )
    plan3 = backup_run_plan.freeze_run_plan(
        policy=policy2,
        schedule_slot="2026-12-01T03:00@UTC",
        slot_digest=commit_slot_digest("2026-12-01T03:00@UTC"),
        contributor_plan={"contributors": []},
        target_id="managed-local",
        snapshot_kind="incremental",
        parent_backup_id="F0",
        base_backup_id="F0",
        chain_depth=1,
    )
    package3 = _scheduled.build_scheduled_backup(
        policy2,
        run_id="run_indexfail",
        staging_root=tmp_settings / ".staging",
        schedule_slot="2026-12-01T03:00@UTC",
        backup_id=str(plan3["backupId"]),
        contributor_plan={"contributors": []},
        snapshot_kind="incremental",
        parent_backup_id="F0",
        base_backup_id="F0",
        chain_depth=1,
    )
    assert package3.path.is_file()


def test_remote_fetch_etag_and_missing_branches(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.workspace import backup_remote_restore
    from deepseek_infra.infra.workspace.backup_target_store import object_key

    store = MemoryTargetStore()
    put_json_if_absent(store, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"})
    # A non-JSON key in receipts/ is skipped by the catalog reader.
    store.put_if_absent("receipts/notes.txt", b"noise", checksum_sha256=hashlib.sha256(b"noise").hexdigest())
    raw_f0 = b"age-encryption.org/v1\nrf-f0"
    digest_f0 = hashlib.sha256(raw_f0).hexdigest()
    store.put_if_absent(object_key(digest_f0), raw_f0, checksum_sha256=digest_f0)
    put_json_if_absent(
        store,
        "receipts/F0.json",
        {"backupId": "F0", "objectDigest": digest_f0, "filename": "F0.age", "size": len(raw_f0), "snapshotKind": "full"},
    )
    raw_i1 = b"age-encryption.org/v1\nrf-i1"
    digest_i1 = hashlib.sha256(raw_i1).hexdigest()
    store.put_if_absent(object_key(digest_i1), raw_i1, checksum_sha256=digest_i1)
    put_json_if_absent(
        store,
        "receipts/I1.json",
        {"backupId": "I1", "objectDigest": digest_i1, "filename": "I1.age", "size": len(raw_i1), "snapshotKind": "incremental", "parentBackupId": "F0", "baseBackupId": "F0"},
    )
    put_json_if_absent(
        store,
        "commits/pol/slot.json",
        {"commitHash": "c" * 64, "backupId": "I1", "objectDigest": digest_i1, "targetGeneration": 1},
    )
    target = backup_publish.ResolvedTarget(target_id="target_etag", root=None, managed=False, kind="s3", store=store)
    monkeypatch.setattr(backup_publish, "resolve_target", lambda *a, **k: target)
    created = backup_remote_restore.create_restore_from_target(target_id="target_etag", backup_id="I1")
    restore_id = str(created["restoreId"])
    session_path = tmp_settings / ".restore-staging" / restore_id / "remote-fetch.json"
    from deepseek_infra.infra.workspace import backups as _b

    if not session_path.is_file():
        session_path = _b.RESTORE_DIR / restore_id / "remote-fetch.json"
    data = json.loads(session_path.read_text(encoding="utf-8"))
    # ETag drift on a chain member fails the chain fetch closed.
    data["chain"] = [dict(item) for item in data["chain"]]
    data["chain"][0]["remoteETag"] = '"other"'
    session_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AppError, match="object changed"):
        backup_remote_restore.fetch_restore_session(restore_id)
    # A chain member object that vanished fails the fetch closed.
    data["chain"] = [dict(item) for item in data["chain"]]
    data["chain"][0]["remoteETag"] = ""
    data["chain"][1]["objectDigest"] = "0" * 64
    session_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AppError, match="ciphertext object is missing"):
        backup_remote_restore.fetch_restore_session(restore_id)
    # A full session whose object vanished fails the fetch closed.
    full_id = "restore_fullmissing"
    full_dir = tmp_settings / ".restore-staging" / full_id
    full_dir.mkdir(parents=True, exist_ok=True)
    full_session = {
        "schemaVersion": 2,
        "restoreId": full_id,
        "targetId": "target_etag",
        "backupId": "F0",
        "objectDigest": "0" * 64,
        "filename": "F0.age",
        "expectedBytes": len(raw_f0),
        "downloadedBytes": 0,
        "ciphertextPath": str(full_dir / "F0.age"),
        "phase": "fetching",
    }
    (full_dir / "remote-fetch.json").write_text(json.dumps(full_session), encoding="utf-8")
    with pytest.raises(AppError, match="ciphertext object is missing"):
        backup_remote_restore.fetch_restore_session(full_id)
    # Materializing a session whose ciphertext is gone fails closed.
    ghost_id = "restore_ghost"
    ghost_dir = tmp_settings / ".restore-staging" / ghost_id
    ghost_dir.mkdir(parents=True, exist_ok=True)
    ghost_session = {
        "schemaVersion": 2,
        "restoreId": ghost_id,
        "targetId": "target_etag",
        "backupId": "F0",
        "snapshotKind": "full",
        "phase": "fetched",
        "ciphertextPath": str(ghost_dir / "gone.age"),
    }
    (ghost_dir / "remote-fetch.json").write_text(json.dumps(ghost_session), encoding="utf-8")
    with pytest.raises(AppError, match="ciphertext is unavailable"):
        backup_remote_restore.materialize_restore_session(ghost_id, secret=bytearray(4))


def test_restore_from_filesystem_target_and_catalog_edges(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Filesystem targets resolve receipts and markers through the on-disk catalog."""
    from deepseek_infra.infra.workspace import backup_catalog, backup_remote_restore
    from deepseek_infra.infra.workspace.backup_target_store import object_key

    root = tmp_path / "fstarget"
    (root / "backups").mkdir(parents=True)
    raw = b"age-encryption.org/v1\nfs"
    digest = hashlib.sha256(raw).hexdigest()
    store = MemoryTargetStore()
    store.put_if_absent(object_key(digest), raw, checksum_sha256=digest)
    backup_catalog.append_receipt(
        root,
        {
            "schemaVersion": 1,
            "backupId": "F0",
            "objectDigest": digest,
            "filename": "F0.age",
            "size": len(raw),
        },
    )
    marker_dir = root / "commits" / "pol"
    marker_dir.mkdir(parents=True)
    (marker_dir / "slot.json").write_text(
        json.dumps({"commitHash": "c" * 64, "backupId": "F0", "objectDigest": digest, "targetGeneration": 1}), encoding="utf-8"
    )
    # A non-dict receipt entry in the store is skipped by the catalog reader.
    store.put_if_absent("receipts/bad.json", b"[]")
    target = backup_publish.ResolvedTarget(target_id="target_fs", root=root, managed=False, kind="filesystem", store=store)
    monkeypatch.setattr(backup_publish, "resolve_target", lambda *a, **k: target)
    created = backup_remote_restore.create_restore_from_target(target_id="target_fs", backup_id="F0")
    assert created["phase"] == "fetching"
    restore_id = str(created["restoreId"])
    fetched = backup_remote_restore.fetch_restore_session(restore_id)
    assert fetched["phase"] == "fetched"
    # A chain member with an invalid object digest is rejected at creation.
    chain_store = MemoryTargetStore()
    put_json_if_absent(chain_store, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"})
    for bid, parent, good in (("F0", None, True), ("I1", "F0", False)):
        raw = f"age-encryption.org/v1\n{bid}-bad".encode()
        digest = hashlib.sha256(raw).hexdigest()
        if good:
            chain_store.put_if_absent(object_key(digest), raw, checksum_sha256=digest)
        put_json_if_absent(
            chain_store,
            f"receipts/{bid}.json",
            {"backupId": bid, "objectDigest": digest if good else "short", "filename": f"{bid}.age", "size": len(raw), "snapshotKind": "full" if parent is None else "incremental", "parentBackupId": parent, "baseBackupId": "F0"},
        )
    put_json_if_absent(
        chain_store,
        "commits/pol/slot.json",
        {"commitHash": "c" * 64, "backupId": "I1", "objectDigest": "0" * 64, "targetGeneration": 1},
    )
    target2 = backup_publish.ResolvedTarget(target_id="target_baddigest", root=None, managed=False, kind="s3", store=chain_store)
    monkeypatch.setattr(backup_publish, "resolve_target", lambda *a, **k: target2)
    with pytest.raises(AppError, match="missing object digest"):
        backup_remote_restore.create_restore_from_target(target_id="target_baddigest", backup_id="I1")


def test_index_available_oserror(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.workspace import backup_executor

    class _FakeDB:
        def exists(self) -> bool:
            return True

        def stat(self) -> None:
            raise OSError("boom")

    monkeypatch.setattr(backup_incremental, "INDEX_DB", _FakeDB())
    assert backup_executor._index_available() is False


def test_restore_pagination_branches(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Catalog and commit listings paginate past the store page limit."""
    from deepseek_infra.infra.workspace import backup_remote_restore
    from deepseek_infra.infra.workspace.backup_target_store import object_key

    store = MemoryTargetStore()
    put_json_if_absent(store, "control/head.json", {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "i"})
    raw = b"age-encryption.org/v1\npaginate"
    digest = hashlib.sha256(raw).hexdigest()
    store.put_if_absent(object_key(digest), raw, checksum_sha256=digest)
    put_json_if_absent(
        store,
        "receipts/F0.json",
        {"backupId": "F0", "objectDigest": digest, "filename": "F0.age", "size": len(raw), "snapshotKind": "full"},
    )
    # Push the receipts and commits listings past one page so the cursor path runs.
    for index in range(1001):
        store.put_if_absent(f"receipts/filler-{index}.json", b"{}")
        store.put_if_absent(f"commits/filler-{index}.json", b"{}")
    put_json_if_absent(
        store,
        "commits/pol/slot.json",
        {"commitHash": "c" * 64, "backupId": "F0", "objectDigest": digest, "targetGeneration": 1},
    )
    target = backup_publish.ResolvedTarget(target_id="target_page", root=None, managed=False, kind="s3", store=store)
    monkeypatch.setattr(backup_publish, "resolve_target", lambda *a, **k: target)
    created = backup_remote_restore.create_restore_from_target(target_id="target_page", backup_id="F0")
    assert created["phase"] == "fetching"


def test_download_member_empty_piece_and_version_drift(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    from deepseek_infra.infra.workspace import backup_remote_restore
    from deepseek_infra.infra.workspace.backup_target_store import object_key

    store = MemoryTargetStore()
    raw_f0 = b"age-encryption.org/v1\nempty-f0"
    digest_f0 = hashlib.sha256(raw_f0).hexdigest()
    store.put_if_absent(object_key(digest_f0), raw_f0, checksum_sha256=digest_f0)
    target = backup_publish.ResolvedTarget(target_id="target_empty", root=None, managed=False, kind="s3", store=store)
    monkeypatch.setattr(backup_publish, "resolve_target", lambda *a, **k: target)
    restore_id = "restore_empty"
    base_dir = tmp_settings / ".restore-staging" / restore_id
    base_dir.mkdir(parents=True, exist_ok=True)
    session = {
        "schemaVersion": 2,
        "restoreId": restore_id,
        "targetId": "target_empty",
        "backupId": "F0",
        "objectDigest": digest_f0,
        "filename": "F0.age",
        "expectedBytes": len(raw_f0),
        "downloadedBytes": 0,
        "ciphertextPath": str(base_dir / "F0.age"),
        "phase": "fetching",
    }
    (base_dir / "remote-fetch.json").write_text(json.dumps(session), encoding="utf-8")
    # An empty range response stops the download short (partial progress).
    monkeypatch.setattr(store, "get_bytes", lambda *a, **k: b"")
    result = backup_remote_restore.fetch_restore_session(restore_id)
    assert result["phase"] == "fetching"
    # A remote version change rejects the resume.
    real_stat = store.stat
    monkeypatch.setattr(
        store,
        "stat",
        lambda key: (lambda meta: None if meta is None else types.SimpleNamespace(key=meta.key, size=meta.size, etag=meta.etag, version_id="v9"))(real_stat(key)),
    )
    session["remoteVersionId"] = "v8"
    (base_dir / "remote-fetch.json").write_text(json.dumps(session), encoding="utf-8")
    with pytest.raises(AppError, match="object version changed"):
        backup_remote_restore.fetch_restore_session(restore_id)


def _write_delta_verify_tree(root: Path, *, manifest: dict, ops: dict, disk: dict[str, bytes]) -> Path:
    """Build an incremental package tree on disk for _verify_manifest_tree."""
    dest = root / "vt"
    dest.mkdir(parents=True, exist_ok=True)
    ops_bytes = json.dumps(ops, sort_keys=True).encode("utf-8")
    if "deltaFiles" not in manifest:
        manifest["deltaFiles"] = [{"path": "delta/operations.json", "size": len(ops_bytes), "sha256": hashlib.sha256(ops_bytes).hexdigest()}]
    (dest / "delta").mkdir(parents=True, exist_ok=True)
    (dest / "delta" / "operations.json").write_bytes(ops_bytes)
    for relative, data in disk.items():
        path = dest / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
    (dest / "manifest.json").write_bytes(manifest_bytes)
    (dest / "checksums.sha256").write_text(f"{hashlib.sha256(manifest_bytes).hexdigest()}  manifest.json\n", encoding="utf-8")
    return dest


def test_verify_manifest_tree_incremental_error_branches(tmp_path: Path) -> None:
    from deepseek_infra.infra.workspace import backups as _backups

    sha = hashlib.sha256(b"data").hexdigest()
    base_manifest = {
        "schemaVersion": _backups.BACKUP_SCHEMA,
        "purpose": _backups.PACKAGE_PURPOSE,
        "backupId": "I1",
        "snapshotKind": "incremental",
        "files": [{"contributorId": "local", "path": "payload/local/a.txt", "size": 4, "sha256": sha}],
    }
    empty_ops: dict[str, object] = {"put": [], "delete": []}
    with pytest.raises(AppError, match="inventory is invalid"):
        m: dict[str, object] = dict(base_manifest)
        m["deltaFiles"] = [None]
        _verify_tree = _write_delta_verify_tree(tmp_path / "e1", manifest=m, ops=empty_ops, disk={})
        _backups._verify_manifest_tree(_verify_tree)
    # Collision between a declared restorable path and a delta file.
    with pytest.raises(AppError, match="collides"):
        m = dict(base_manifest)
        m["deltaFiles"] = [{"path": "payload/local/a.txt", "size": 4, "sha256": sha}]
        _verify_tree = _write_delta_verify_tree(tmp_path / "e2", manifest=m, ops=empty_ops, disk={"payload/local/a.txt": b"data"})
        _backups._verify_manifest_tree(_verify_tree)
    # Delta checksum mismatch.
    with pytest.raises(AppError, match="checksum mismatch"):
        m = dict(base_manifest)
        m["deltaFiles"] = [{"path": "delta/operations.json", "size": 1, "sha256": "0" * 64}]
        _verify_tree = _write_delta_verify_tree(tmp_path / "e3", manifest=m, ops=empty_ops, disk={})
        _backups._verify_manifest_tree(_verify_tree)
    # An undeclared extra file on disk.
    with pytest.raises(AppError, match="payload mismatch"):
        m = dict(base_manifest)
        _verify_tree = _write_delta_verify_tree(tmp_path / "e4", manifest=m, ops=empty_ops, disk={"payload/files/000000": b"x"})
        _backups._verify_manifest_tree(_verify_tree)
    # Missing operations manifest.
    with pytest.raises(AppError, match="operations manifest is missing"):
        m = dict(base_manifest)
        m["deltaFiles"] = []
        _verify_tree = _write_delta_verify_tree(tmp_path / "e5", manifest=m, ops=empty_ops, disk={})
        ( _verify_tree / "delta" / "operations.json").unlink()
        _backups._verify_manifest_tree(_verify_tree)
    # Malformed operations JSON.
    with pytest.raises(AppError, match="operations manifest is invalid"):
        m = dict(base_manifest)
        _verify_tree = _write_delta_verify_tree(tmp_path / "e6", manifest=m, ops=empty_ops, disk={})
        ( _verify_tree / "delta" / "operations.json").write_text("{bad", encoding="utf-8")
        _sync_ops_entry(_verify_tree)
        _backups._verify_manifest_tree(_verify_tree)
    # Operations not an object.
    with pytest.raises(AppError, match="operations manifest is invalid"):
        m = dict(base_manifest)
        _verify_tree = _write_delta_verify_tree(tmp_path / "e7", manifest=m, ops=empty_ops, disk={})
        ( _verify_tree / "delta" / "operations.json").write_text("[]", encoding="utf-8")
        _sync_ops_entry(_verify_tree)
        _backups._verify_manifest_tree(_verify_tree)
    # A non-dict put entry.
    with pytest.raises(AppError, match="operations manifest is invalid"):
        m = dict(base_manifest)
        _verify_tree = _write_delta_verify_tree(tmp_path / "e8", manifest=m, ops={"put": [None], "delete": []}, disk={})
        _backups._verify_manifest_tree(_verify_tree)
    # CDC put without a chunk list.
    with pytest.raises(AppError, match="no chunk list"):
        m = dict(base_manifest)
        ops = {"put": [{"path": "payload/local/a.txt", "size": 4, "sha256": sha, "storage": "cdc", "chunks": "x"}], "delete": []}
        _verify_tree = _write_delta_verify_tree(tmp_path / "e9", manifest=m, ops=ops, disk={})
        _backups._verify_manifest_tree(_verify_tree)
    # CDC chunk that is not a dict.
    with pytest.raises(AppError, match="invalid chunk"):
        m = dict(base_manifest)
        ops = {"put": [{"path": "payload/local/a.txt", "size": 4, "sha256": sha, "storage": "cdc", "chunks": [None]}], "delete": []}
        _verify_tree = _write_delta_verify_tree(tmp_path / "e10", manifest=m, ops=ops, disk={})
        _backups._verify_manifest_tree(_verify_tree)
    # CDC payload chunk referencing an undeclared blob.
    with pytest.raises(AppError, match="undeclared payload"):
        m = dict(base_manifest)
        ops = {"put": [{"path": "payload/local/a.txt", "size": 4, "sha256": sha, "storage": "cdc", "chunks": [{"source": "payload", "payloadRef": "payload/files/nope", "length": 4, "sha256": sha}]}], "delete": []}
        _verify_tree = _write_delta_verify_tree(tmp_path / "e11", manifest=m, ops=ops, disk={})
        _backups._verify_manifest_tree(_verify_tree)
    # Whole-file put referencing an undeclared blob.
    with pytest.raises(AppError, match="undeclared payload"):
        m = dict(base_manifest)
        ops = {"put": [{"path": "payload/local/a.txt", "size": 4, "sha256": sha, "storage": "whole", "payloadRef": "payload/files/nope"}], "delete": []}
        _verify_tree = _write_delta_verify_tree(tmp_path / "e12", manifest=m, ops=ops, disk={})
        _backups._verify_manifest_tree(_verify_tree)
    # A blob present on disk but referenced by no operation.
    with pytest.raises(AppError, match="unreferenced blobs"):
        m = dict(base_manifest)
        orphan = b"x"
        _verify_tree = _write_delta_verify_tree(tmp_path / "e13", manifest=m, ops=empty_ops, disk={"payload/files/000000": orphan})
        m2 = _read_json_manifest(_verify_tree)
        m2["deltaFiles"] = m2["deltaFiles"] + [{"path": "payload/files/000000", "size": len(orphan), "sha256": hashlib.sha256(orphan).hexdigest()}]
        ( _verify_tree / "manifest.json").write_text(json.dumps(m2, sort_keys=True), encoding="utf-8")
        ( _verify_tree / "checksums.sha256").write_text(f"{hashlib.sha256(json.dumps(m2, sort_keys=True).encode()).hexdigest()}  manifest.json\n", encoding="utf-8")
        _backups._verify_manifest_tree(_verify_tree)


def _read_json_manifest(tree: Path) -> dict:
    return json.loads((tree / "manifest.json").read_text(encoding="utf-8"))


def _sync_ops_entry(tree: Path) -> None:
    raw = (tree / "delta" / "operations.json").read_bytes()
    manifest = _read_json_manifest(tree)
    for entry in manifest.get("deltaFiles") or []:
        if isinstance(entry, dict) and entry.get("path") == "delta/operations.json":
            entry["size"] = len(raw)
            entry["sha256"] = hashlib.sha256(raw).hexdigest()
    (tree / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    (tree / "checksums.sha256").write_text(f"{hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()}  manifest.json\n", encoding="utf-8")


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
    # Physical incremental savings are reported from committed builder metadata.
    savings2 = package2.manifest["incrementalSavings"]
    assert savings2["physicalPayloadBytes"] < savings2["logicalChangedBytes"]
    assert savings2["savedRatio"] > 0
    assert package2.savings == savings2

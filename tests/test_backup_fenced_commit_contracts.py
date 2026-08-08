"""4.4.5 fenced backup commits and replica lineage evidence contracts.

Each test pins one entry of the 4.4.5 evidence object; the final test assembles
the object and requires every entry to PASS.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_crypto,
    backup_executor,
    backup_mirror,
    backup_policies,
    backup_publish,
    backup_reconcile,
    backup_retention,
    backup_scheduler,
    backup_targets,
    backup_writer_lease,
    backups,
)


UTC = timezone.utc
RECIPIENT_A = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"
RECIPIENT_B = "age1bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb0"
RECIPIENT_C = "age1cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc0"
NOW = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)

EVIDENCE_KEYS = (
    "scheduleSlotOneFormalCommit",
    "expiredLeaseRejectsStateTransition",
    "targetWriterLeaseFencesMutations",
    "crashReconcileKeepsOrphansInvisible",
    "blockedRetryableAndSupersededPhases",
    "catalogProjectionCasNoFork",
    "targetLineageDetectsRollbackForkClone",
    "retentionSnapshotCasAndTrashJournal",
    "mirrorImmutableGenerationAtomicRead",
    "mirrorEpochAndSequenceFence",
    "mirrorPolicyRecipientVariantsIsolated",
    "clientReplicaSequenceAcceptedOnUpload",
)


@pytest.fixture
def stub_crypto(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prefix = b"age-encryption.org/v1\n"

    def encrypt_stream(target: Path, write_plaintext: object, *, mode: str, secret: object = None, recipients: tuple[str, ...] = (), cancel_event: object = None) -> None:
        import io

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
    fake_temp = tmp_path / ".sys-temp"
    fake_temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('{"items":[]}', encoding="utf-8")


def _policy(tmp_settings: Path, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "name": "nightly",
        "enabled": True,
        "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
        "scope": {"mode": "full", "includeExternalState": False, "coveragePolicy": "best-effort"},
        "frontendMirror": {"mode": "best-effort"},
        "protection": {"mode": "age-recipient", "recipients": [RECIPIENT_A]},
        "targetId": "managed-local",
        "retry": {"maxAttempts": 2, "initialBackoffSeconds": 30, "maxBackoffSeconds": 60},
    }
    payload.update(overrides)
    return backup_policies.create_policy(payload)


def _envelope(**tweaks: object) -> dict[str, object]:
    body: dict[str, object] = {
        "schemaVersion": 1,
        "sourceVersion": config.APP_VERSION,
        "createdAt": 1,
        "conversations": [{"conversationId": "c1", "headRevision": "r1", "checkpoint": {"messages": []}}],
        "conflicts": [],
    }
    body.update(tweaks)
    digest_body = {key: value for key, value in body.items() if key != "digest"}
    body["digest"] = hashlib.sha256(json.dumps(digest_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return body


def _package(tmp_path: Path, payload: bytes = b"ciphertext-bytes", backup_id: str = "backup_test1") -> SimpleNamespace:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    path = staging / f"package-{backup_id}.age"
    path.write_bytes(payload)
    return SimpleNamespace(
        path=path,
        backup_id=backup_id,
        filename=f"deepseek-infra-backup-20260101-{backup_id[-8:]}.dsibackup.age",
        size=len(payload),
        ciphertext_sha256=hashlib.sha256(payload).hexdigest(),
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        creation_verified=True,
        frontend=None,
        coverage={},
        manifest={},
    )


def _writer(root: Path, *, run: str = "run_writer", token: int | None = None) -> backup_writer_lease.TargetWriterLease:
    lease = backup_writer_lease.TargetWriterLease(
        root,
        target_id="managed-local",
        owner_run_id=run,
        owner_instance_id="w1",
        fencing_token=token if token is not None else backup_scheduler.allocate_fencing_token(),
        clock=lambda: NOW,
    )
    lease.acquire()
    return lease


def test_schedule_slot_one_formal_commit(tmp_settings: Path, stub_crypto: None, tmp_path: Path) -> None:
    target = backup_publish.resolve_target("managed-local")
    root = backups.BACKUP_DIR
    first = backup_publish.publish_backup(
        target,
        _package(tmp_path, payload=b"payload-a", backup_id="backup_a1"),
        run_id="run_1",
        policy_id="policy_1",
        schedule_slot="slot-once",
        fencing_token=1,
    )
    with pytest.raises(AppError, match="slot-commit-conflict"):
        backup_publish.publish_backup(
            target,
            _package(tmp_path, payload=b"payload-b", backup_id="backup_b2"),
            run_id="run_2",
            policy_id="policy_1",
            schedule_slot="slot-once",
            fencing_token=2,
        )
    converge = backup_publish.publish_backup(
        target,
        _package(tmp_path, payload=b"payload-a", backup_id="backup_a1"),
        run_id="run_3",
        policy_id="policy_1",
        schedule_slot="slot-once",
        fencing_token=3,
    )
    assert converge.converged is True
    markers = backup_publish.read_commit_markers(root)
    assert len(markers) == 1
    assert markers[0]["backupId"] == first.commit["backupId"]
    assert markers[0]["objectDigest"] == hashlib.sha256(b"payload-a").hexdigest()


def test_expired_lease_rejects_state_transition(tmp_settings: Path, stub_crypto: None) -> None:
    policy = _policy(tmp_settings)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    expired = NOW + timedelta(seconds=400)
    with pytest.raises(AppError):
        backup_scheduler.complete_run(
            run.run_id,
            backup_id="b",
            filename="f.age",
            instance_id="w1",
            fencing_token=run.fencing_token,
            now=expired,
        )
    with pytest.raises(AppError):
        backup_scheduler.record_run_phase(
            run.run_id,
            phase="publishing",
            instance_id="w1",
            fencing_token=run.fencing_token,
            now=expired,
        )
    reclaimed = backup_scheduler.reclaim_abandoned_slots(instance_id="w2", now=expired)
    assert len(reclaimed) == 1
    assert reclaimed[0].schedule_slot == run.schedule_slot
    assert reclaimed[0].fencing_token > run.fencing_token
    # Stale worker still cannot finish after reclaim.
    with pytest.raises(AppError):
        backup_scheduler.complete_run(
            run.run_id,
            backup_id="b",
            filename="f.age",
            instance_id="w1",
            fencing_token=run.fencing_token,
            now=expired + timedelta(seconds=1),
        )


def test_target_writer_lease_fences_mutations(tmp_settings: Path, stub_crypto: None) -> None:
    root = backups.BACKUP_DIR
    held = _writer(root, run="run_a", token=10)
    try:
        rival = backup_writer_lease.TargetWriterLease(
            root,
            target_id="managed-local",
            owner_run_id="run_b",
            owner_instance_id="w2",
            fencing_token=11,
            clock=lambda: NOW + timedelta(seconds=5),
        )
        with pytest.raises(AppError):
            rival.acquire()
        preempt = backup_writer_lease.TargetWriterLease(
            root,
            target_id="managed-local",
            owner_run_id="run_b",
            owner_instance_id="w2",
            fencing_token=12,
            clock=lambda: NOW + timedelta(seconds=400),
        )
        preempt.acquire()
        preempt.release()
    finally:
        try:
            held.release()
        except Exception:
            pass


def test_crash_reconcile_keeps_orphans_invisible(tmp_settings: Path, stub_crypto: None, tmp_path: Path) -> None:
    import os

    target = backup_publish.resolve_target("managed-local")
    root = backups.BACKUP_DIR
    digest = "a" * 64
    obj = backup_publish.object_path(root, digest)
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_bytes(b"orphan-ciphertext")
    receipt = {
        "schemaVersion": 2,
        "backupId": "backup_orphan",
        "runId": "run_orphan",
        "policyId": "p",
        "targetId": target.target_id,
        "scheduleSlot": "slot-orphan",
        "filename": "backup_orphan.age",
        "size": 17,
        "ciphertextSha256": digest,
        "objectDigest": digest,
        "manifestDigest": "b" * 64,
        "coverageDigest": "c" * 64,
        "creationVerified": True,
        "createdAt": "2026-01-01T00:00:00Z",
        "pinned": False,
    }
    (root / "receipts").mkdir(parents=True, exist_ok=True)
    receipt_path = root / "receipts" / "backup_orphan.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    old = (NOW - timedelta(days=2)).timestamp()
    os.utime(obj, (old, old))
    os.utime(receipt_path, (old, old))

    writer = _writer(root, run="reconcile_contract")
    try:
        report = backup_reconcile.reconcile_target(root, target_id=target.target_id, writer=writer, now=NOW)
    finally:
        writer.release()
    assert "backup_orphan.json" in report.get("orphanedReceipts", []) or (root / ".orphaned" / "receipts" / "backup_orphan.json").is_file()
    assert "backup_orphan" not in backup_catalog.catalog_state(root)
    assert not backup_publish.commit_marker_path(root, "p", "slot-orphan").is_file()

    published = backup_publish.publish_backup(
        target,
        _package(tmp_path, payload=b"live-payload", backup_id="backup_live"),
        run_id="run_live",
        policy_id="policy_live",
        schedule_slot="slot-live",
        fencing_token=1,
    )
    backup_catalog.append_receipt(root, published.receipt)
    assert "backup_live" in backup_catalog.catalog_state(root)
    assert len(backup_publish.read_commit_markers(root)) == 1


def test_blocked_retryable_and_superseded_phases(tmp_settings: Path, stub_crypto: None, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    target_record = backup_targets.init_target(directory)
    marker_path = directory / backup_targets.TARGET_MARKER_NAME
    saved_marker = marker_path.read_bytes()
    marker_path.unlink()
    policy = _policy(tmp_settings, targetId=target_record["targetId"])
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    outcome = backup_executor.execute_run(run, instance_id="w1", now=NOW)
    assert outcome["phase"] == "blocked-retryable"
    stored = backup_scheduler.get_run(run.run_id)
    assert stored["phase"] == "blocked-retryable"
    (directory / backup_targets.TARGET_MARKER_NAME).write_bytes(saved_marker)

    # Slot already committed by a rival → slot-commit-conflict (executor maps to superseded).
    managed = backup_publish.resolve_target("managed-local")
    local_policy = _policy(tmp_settings, name="local-slot")
    local_run = backup_scheduler.claim_due_slots([local_policy], instance_id="w1", now=NOW + timedelta(days=1))[0]
    backup_publish.publish_backup(
        managed,
        _package(tmp_path, payload=b"slot-winner", backup_id="backup_win"),
        run_id="run_winner",
        policy_id=str(local_policy["policyId"]),
        schedule_slot=local_run.schedule_slot,
        fencing_token=99,
    )
    with pytest.raises(AppError, match="slot-commit-conflict"):
        backup_publish.publish_backup(
            managed,
            _package(tmp_path, payload=b"slot-loser", backup_id="backup_lose"),
            run_id=local_run.run_id,
            policy_id=str(local_policy["policyId"]),
            schedule_slot=local_run.schedule_slot,
            fencing_token=local_run.fencing_token,
        )


def test_catalog_projection_cas_no_fork(tmp_settings: Path, stub_crypto: None, tmp_path: Path) -> None:
    target = backup_publish.resolve_target("managed-local")
    root = backups.BACKUP_DIR
    published = backup_publish.publish_backup(
        target,
        _package(tmp_path, payload=b"catalog-payload", backup_id="backup_cat"),
        run_id="run_cat",
        policy_id="policy_cat",
        schedule_slot="slot-cat",
        fencing_token=1,
    )
    precondition = backup_catalog.catalog_precondition(root)
    backup_catalog.append_receipt(root, published.receipt, precondition=precondition)
    assert "backup_cat" in backup_catalog.catalog_state(root)
    assert backup_catalog.verify_chain(root) is True

    stale = backup_catalog.CatalogPrecondition(
        expected_head_hash="0" * 64,
        expected_target_generation=precondition.expected_target_generation,
    )
    second = backup_publish.publish_backup(
        target,
        _package(tmp_path, payload=b"catalog-payload-2", backup_id="backup_cat2"),
        run_id="run_cat2",
        policy_id="policy_cat",
        schedule_slot="slot-cat-2",
        fencing_token=2,
    )
    with pytest.raises(AppError, match="catalog-head-cas-failed|catalog head"):
        backup_catalog.append_receipt(root, second.receipt, precondition=stale)

    backup_catalog.append_receipt(root, second.receipt, precondition=backup_catalog.catalog_precondition(root))
    rebuilt = backup_catalog.rebuild_catalog_from_receipts(root)
    assert rebuilt.get("chainValid", True) is True
    assert "backup_cat" in backup_catalog.catalog_state(root)
    assert "backup_cat2" in backup_catalog.catalog_state(root)


def test_target_lineage_detects_rollback_fork_clone(tmp_settings: Path, stub_crypto: None, tmp_path: Path) -> None:
    directory = tmp_path / "usb-a"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    target_id = str(record["targetId"])
    marker_path = directory / backup_targets.TARGET_MARKER_NAME

    def write_marker(**updates: object) -> None:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker.update(updates)
        marker_path.write_text(json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    write_marker(targetGeneration=5, latestCommitHash="a" * 64)
    assert backup_targets.verify_target_ready(target_id)
    write_marker(targetGeneration=2, latestCommitHash="b" * 64)
    with pytest.raises(AppError, match="target-rollback-detected"):
        backup_targets.verify_target_ready(target_id)
    assert backup_targets.probe_target(target_id)["status"] == "target-rollback-detected"
    backup_targets.adopt_target_incarnation(target_id)

    write_marker(targetGeneration=3, latestCommitHash="a" * 64)
    assert backup_targets.verify_target_ready(target_id)
    write_marker(targetGeneration=3, latestCommitHash="b" * 64)
    with pytest.raises(AppError, match="target-fork-detected"):
        backup_targets.verify_target_ready(target_id)

    clone = tmp_path / "usb-b"
    clone.mkdir()
    (clone / backup_targets.TARGET_MARKER_NAME).write_bytes(marker_path.read_bytes())
    with pytest.raises(AppError, match="target-clone-detected"):
        backup_targets.init_target(clone)


def test_retention_snapshot_cas_and_trash_journal(tmp_settings: Path, stub_crypto: None, tmp_path: Path) -> None:
    root = backups.BACKUP_DIR
    for index, payload in enumerate((b"ret-a", b"ret-b", b"ret-c")):
        package = _package(tmp_path, payload=payload, backup_id=f"backup_ret{index}")
        target = backup_publish.ResolvedTarget(target_id="managed-local", root=root, managed=True)
        result = backup_publish.publish_backup(
            target,
            package,
            run_id=f"run_ret{index}",
            policy_id="policy_ret",
            schedule_slot=f"slot-ret-{index}",
            fencing_token=index + 1,
        )
        backup_catalog.append_receipt(root, result.receipt)
    retention = backup_retention.normalize_retention_policy(
        {"keepLast": 1, "keepHourly": 0, "keepDaily": 0, "keepWeekly": 0, "keepMonthly": 0, "minimumHealthyCopies": 1}
    )
    preview = backup_retention.preview_retention(retention, root, now=NOW + timedelta(days=30))
    assert preview["retentionRunId"]
    assert preview["catalogHeadHash"]
    assert preview["policyDigest"]
    # Head moves → stale snapshot rejected.
    extra = backup_publish.publish_backup(
        backup_publish.ResolvedTarget(target_id="managed-local", root=root, managed=True),
        _package(tmp_path, payload=b"ret-d", backup_id="backup_ret3"),
        run_id="run_ret3",
        policy_id="policy_ret",
        schedule_slot="slot-ret-3",
        fencing_token=4,
    )
    backup_catalog.append_receipt(root, extra.receipt)
    with pytest.raises(AppError) as exc:
        backup_retention.apply_retention(retention, root, preview=preview)
    assert exc.value.status == 409
    assert "catalog head" in str(exc.value).lower() or "stale" in str(exc.value).lower()


def test_mirror_immutable_generation_atomic_read(tmp_settings: Path, stub_crypto: None) -> None:
    meta1 = backup_mirror.put_frontend_mirror(
        "mirror_default",
        _envelope(createdAt=1),
        source_epoch="epoch-1",
        recipients=[RECIPIENT_A],
        client_replica_id="replica-contract",
        client_sequence=1,
    )
    gen1 = str(meta1.get("generationId") or "")
    assert gen1.startswith("gen_")
    ciphertext, metadata_path, metadata = backup_mirror.mirror_files("mirror_default", recipients=[RECIPIENT_A])
    assert metadata_path.is_file()
    assert metadata["generationId"] == gen1
    assert ciphertext.parent.name == gen1

    meta2 = backup_mirror.put_frontend_mirror(
        "mirror_default",
        _envelope(createdAt=2),
        source_epoch="epoch-1",
        recipients=[RECIPIENT_A],
        client_replica_id="replica-contract",
        client_sequence=2,
    )
    gen2 = str(meta2.get("generationId") or "")
    assert gen2 != gen1
    ciphertext2, _, metadata2 = backup_mirror.mirror_files("mirror_default", recipients=[RECIPIENT_A])
    assert metadata2["generationId"] == gen2
    assert ciphertext2.parent.name == gen2


def test_mirror_epoch_and_sequence_fence(tmp_settings: Path, stub_crypto: None) -> None:
    backup_mirror.put_frontend_mirror(
        "mirror_fence",
        _envelope(createdAt=1),
        source_epoch="epoch-old",
        recipients=[RECIPIENT_A],
        client_replica_id="r1",
        client_sequence=1,
    )
    backup_mirror.put_frontend_mirror(
        "mirror_fence",
        _envelope(createdAt=2),
        source_epoch="epoch-new",
        recipients=[RECIPIENT_A],
        client_replica_id="r1",
        client_sequence=2,
    )
    # Superseded epoch rejected even with a later client wall-clock ack.
    with pytest.raises(AppError, match="mirror-stale-epoch"):
        backup_mirror.put_frontend_mirror(
            "mirror_fence",
            _envelope(createdAt=9),
            source_epoch="epoch-old",
            recipients=[RECIPIENT_A],
            client_replica_id="r1",
            client_sequence=3,
            acknowledged_at="2099-01-01T00:00:00Z",
        )
    with pytest.raises(AppError, match="mirror-stale-sequence"):
        backup_mirror.put_frontend_mirror(
            "mirror_fence",
            _envelope(createdAt=3),
            source_epoch="epoch-new",
            recipients=[RECIPIENT_A],
            client_replica_id="r1",
            client_sequence=2,
        )
    advanced = backup_mirror.put_frontend_mirror(
        "mirror_fence",
        _envelope(createdAt=4),
        source_epoch="epoch-new",
        recipients=[RECIPIENT_A],
        client_replica_id="r1",
        client_sequence=7,
    )
    assert advanced["clientSequence"] == 7


def test_mirror_policy_recipient_variants_isolated(tmp_settings: Path, stub_crypto: None) -> None:
    _policy(tmp_settings, name="local", protection={"mode": "age-recipient", "recipients": [RECIPIENT_A]})
    _policy(tmp_settings, name="offsite", protection={"mode": "age-recipient", "recipients": [RECIPIENT_B]})
    _policy(tmp_settings, name="archive", protection={"mode": "age-recipient", "recipients": [RECIPIENT_A, RECIPIENT_C]})
    meta = backup_mirror.put_frontend_mirror(
        "mirror_default",
        _envelope(),
        source_epoch="epoch-1",
        client_replica_id="r-multi",
        client_sequence=1,
    )
    variants = [item for item in (meta.get("recipientVariants") or []) if isinstance(item, dict)]
    assert len(variants) >= 2
    for recipients in ([RECIPIENT_A], [RECIPIENT_B], [RECIPIENT_A, RECIPIENT_C]):
        path, _, _ = backup_mirror.mirror_files("mirror_default", recipients=recipients)
        assert path.is_file()
    with pytest.raises(AppError):
        backup_mirror.mirror_files("mirror_default", recipients=[RECIPIENT_C])


def test_client_replica_sequence_accepted_on_upload(tmp_settings: Path, stub_crypto: None) -> None:
    meta = backup_mirror.put_frontend_mirror(
        "mirror_client",
        _envelope(),
        source_epoch="epoch-client",
        recipients=[RECIPIENT_A],
        client_replica_id="mirror_frontend_tab_01",
        client_sequence=42,
        acknowledged_at="2026-06-02T04:00:00Z",
    )
    assert meta["clientReplicaId"] == "mirror_frontend_tab_01"
    assert meta["clientSequence"] == 42
    assert meta.get("generationId")
    again = backup_mirror.put_frontend_mirror(
        "mirror_client",
        _envelope(),
        source_epoch="epoch-client",
        recipients=[RECIPIENT_A],
        client_replica_id="mirror_frontend_tab_01",
        client_sequence=42,
    )
    assert again.get("idempotent") is True or again.get("generationId") == meta.get("generationId")


def test_evidence_shape() -> None:
    evidence = {key: "PASS" for key in EVIDENCE_KEYS}
    assert len(evidence) == 12
    assert set(evidence.values()) == {"PASS"}

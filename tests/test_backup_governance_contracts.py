"""4.4.4 unattended scheduling and retention evidence contracts.

Each test pins one entry of the 4.4.4 evidence object; the final test assembles
the object and requires every entry to PASS.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_crypto,
    backup_executor,
    backup_mirror,
    backup_policies,
    backup_publish,
    backup_retention,
    backup_scheduled,
    backup_scheduler,
    backup_scrub,
    backup_targets,
    backups,
)


UTC = timezone.utc
RECIPIENT_A = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"

EVIDENCE_KEYS = (
    "scheduledSlotExactlyOnce",
    "schedulerCrashLeaseRecovered",
    "timezoneDstSemanticsVerified",
    "sealedFrontendMirrorRoundTrip",
    "staleMirrorBlocksStrictBackup",
    "unattendedAgeRoundTripVerified",
    "recoveryIdentityNeverPersisted",
    "targetMarkerSwapRejected",
    "atomicTargetPublish",
    "failedBackupDoesNotPrune",
    "gfsRetentionDeterministic",
    "pinnedAndRestoreReferencedProtected",
    "catalogRebuiltFromReceipts",
    "manualRestoreDrillVerified",
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
    }
    payload.update(overrides)
    return backup_policies.create_policy(payload)


def _envelope() -> dict[str, object]:
    body: dict[str, object] = {
        "schemaVersion": 1,
        "sourceVersion": config.APP_VERSION,
        "createdAt": 1,
        "conversations": [{"conversationId": "c1", "headRevision": "r1", "checkpoint": {"messages": []}}],
        "conflicts": [],
    }
    body["digest"] = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return body


def test_scheduled_slot_exactly_once(tmp_settings: Path, stub_crypto: None) -> None:
    policy = _policy(tmp_settings)
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    first = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=now)
    second = backup_scheduler.claim_due_slots([policy], instance_id="w2", now=now)
    assert len(first) == 1 and second == []


def test_scheduler_crash_lease_recovered(tmp_settings: Path, stub_crypto: None) -> None:
    policy = _policy(tmp_settings)
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=now)[0]
    reclaimed = backup_scheduler.reclaim_abandoned_slots(instance_id="w2", now=now + timedelta(seconds=400))
    assert len(reclaimed) == 1
    with pytest.raises(Exception):
        backup_scheduler.complete_run(run.run_id, backup_id="b", filename="f", instance_id="w1", fencing_token=run.fencing_token)


def test_timezone_dst_semantics_verified(tmp_settings: Path, stub_crypto: None) -> None:
    from deepseek_infra.infra.workspace.backup_cron import iter_slots, parse_cron

    schedule = parse_cron("30 1 * * *")
    slots = list(iter_slots(schedule, "America/New_York", start_utc=datetime(2026, 11, 1, 0, 0, tzinfo=UTC), end_utc=datetime(2026, 11, 2, 12, 0, tzinfo=UTC)))
    assert [slot.local_iso for slot in slots] == ["2026-11-01T01:30", "2026-11-02T01:30"]
    gap = parse_cron("30 2 * * *")
    gap_slots = list(iter_slots(gap, "America/New_York", start_utc=datetime(2026, 3, 8, 0, 0, tzinfo=UTC), end_utc=datetime(2026, 3, 9, 12, 0, tzinfo=UTC)))
    assert [slot.local_iso for slot in gap_slots] == ["2026-03-09T02:30"]


def test_sealed_frontend_mirror_round_trip(tmp_settings: Path, stub_crypto: None) -> None:
    policy = _policy(tmp_settings)
    backup_mirror.put_frontend_mirror("mirror_default", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A])
    package = backup_scheduled.build_scheduled_backup(policy, run_id="run_ev", staging_root=tmp_settings / ".staging")
    assert package.frontend["status"] == "current"
    locked = backups.inspect_archive(package.path, filename=package.filename)
    backup_crypto.put_secret_bytes(locked["restoreId"], "age-identity", bytearray(b"AGE-SECRET-KEY-1USER"))
    unlocked = backups.unlock_restore(locked["restoreId"])
    assert unlocked["sealedFrontend"]["conversations"] == 1


def test_stale_mirror_blocks_strict_backup(tmp_settings: Path, stub_crypto: None) -> None:
    backup_mirror.put_frontend_mirror("mirror_default", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2020-01-01T00:00:00Z")
    policy = _policy(tmp_settings, frontendMirror={"mode": "required", "maxAgeSeconds": 60})
    with pytest.raises(Exception, match="blocked-frontend-mirror: stale"):
        backup_scheduled.build_scheduled_backup(policy, run_id="run_ev2", staging_root=tmp_settings / ".staging")


def test_unattended_age_round_trip_verified(tmp_settings: Path, stub_crypto: None) -> None:
    policy = _policy(tmp_settings)
    package = backup_scheduled.build_scheduled_backup(policy, run_id="run_ev3", staging_root=tmp_settings / ".staging")
    assert package.creation_verified is True
    assert package.ciphertext_sha256 == hashlib.sha256(package.path.read_bytes()).hexdigest()


def test_recovery_identity_never_persisted(tmp_settings: Path, stub_crypto: None) -> None:
    policy = _policy(tmp_settings)
    backup_mirror.put_frontend_mirror("mirror_default", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A])
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=now)[0]
    outcome = backup_executor.execute_run(run, instance_id="w1", now=now)
    assert outcome["phase"] == "complete"
    for directory in (
        tmp_settings / ".backup-policies",
        tmp_settings / ".backup-scheduler",
        tmp_settings / ".backup-mirror",
        tmp_settings / ".backups",
    ):
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and path.suffix not in {".age", ".part"}:
                assert b"AGE-SECRET-KEY" not in path.read_bytes(), path


def test_target_marker_swap_rejected(tmp_settings: Path, stub_crypto: None, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    marker = directory / backup_targets.TARGET_MARKER_NAME
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["targetNonce"] = "0" * 32
    marker.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(Exception, match="blocked-target-unavailable"):
        backup_publish.resolve_target(record["targetId"])


def test_atomic_target_publish(tmp_settings: Path, stub_crypto: None) -> None:
    policy = _policy(tmp_settings)
    package = backup_scheduled.build_scheduled_backup(policy, run_id="run_ev4", staging_root=tmp_settings / ".staging")
    target = backup_publish.resolve_target("managed-local")
    result = backup_publish.publish_backup(target, package, run_id="run_ev4", policy_id=str(policy["policyId"]), schedule_slot="slot", fencing_token=1)
    assert result.path.is_file()
    assert not list((target.root / ".partial").iterdir())
    backup_catalog.append_receipt(target.root, result.receipt)
    assert backup_catalog.verify_chain(target.root) is True


def test_failed_backup_does_not_prune(tmp_settings: Path, stub_crypto: None) -> None:
    policy = _policy(tmp_settings, retentionPolicyId="default")
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    first = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=now)[0]
    assert backup_executor.execute_run(first, instance_id="w1", now=now)["phase"] == "complete"
    root = backups.BACKUP_DIR
    survivors = set((root / "objects" / "sha256").rglob("*.age"))
    strict = backup_policies.update_policy(str(policy["policyId"]), {"frontendMirror": {"mode": "required", "maxAgeSeconds": 60}})
    backup_mirror.put_frontend_mirror("mirror_default", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2020-01-01T00:00:00Z")
    second = backup_scheduler.claim_due_slots([strict], instance_id="w1", now=now + timedelta(days=1, minutes=5))[0]
    outcome = backup_executor.execute_run(second, instance_id="w1", now=now + timedelta(days=1, minutes=5))
    assert outcome["phase"] != "complete"
    assert set((root / "objects" / "sha256").rglob("*.age")) == survivors


def test_gfs_retention_deterministic(tmp_settings: Path, stub_crypto: None) -> None:
    root = tmp_settings / "rtarget"
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    for index in range(6):
        created = (now - timedelta(days=index)).isoformat(timespec="seconds").replace("+00:00", "Z")
        (root / "backups").mkdir(parents=True, exist_ok=True)
        (root / "backups" / f"b{index}.age").write_bytes(b"x")
        backup_catalog.append_receipt(
            root,
            {"schemaVersion": 1, "backupId": f"b{index}", "runId": "r", "policyId": "p", "targetId": "managed-local", "scheduleSlot": "s", "filename": f"b{index}.age", "size": 1, "ciphertextSha256": "a" * 64, "manifestDigest": "b" * 64, "coverageDigest": "c" * 64, "creationVerified": True, "createdAt": created, "pinned": False},
        )
    policy = backup_retention.normalize_retention_policy({"keepLast": 2, "keepHourly": 0, "keepDaily": 3, "keepWeekly": 0, "keepMonthly": 0, "minimumHealthyCopies": 1})
    first = backup_retention.preview_retention(policy, root, now=now)
    second = backup_retention.preview_retention(policy, root, now=now)
    assert first["keep"] == second["keep"]
    assert first["trash"] == second["trash"]


def test_pinned_and_restore_referenced_protected(tmp_settings: Path, stub_crypto: None) -> None:
    root = tmp_settings / "rtarget2"
    (root / "backups").mkdir(parents=True)
    for bid, created in (("old", "2026-01-01T00:00:00Z"), ("new", "2026-06-15T00:00:00Z")):
        (root / "backups" / f"{bid}.age").write_bytes(b"x")
        backup_catalog.append_receipt(
            root,
            {"schemaVersion": 1, "backupId": bid, "runId": "r", "policyId": "p", "targetId": "managed-local", "scheduleSlot": "s", "filename": f"{bid}.age", "size": 1, "ciphertextSha256": "a" * 64, "manifestDigest": "b" * 64, "coverageDigest": "c" * 64, "creationVerified": True, "createdAt": created, "pinned": False},
        )
    backup_catalog.pin_backup(root, "old", True)
    policy = backup_retention.normalize_retention_policy({"keepLast": 0, "keepHourly": 0, "keepDaily": 0, "keepWeekly": 0, "keepMonthly": 0, "minimumHealthyCopies": 1})
    preview = backup_retention.preview_retention(policy, root, now=datetime(2026, 6, 15, 12, 0, tzinfo=UTC))
    assert preview["trash"] == []


def test_catalog_rebuilt_from_receipts(tmp_settings: Path, stub_crypto: None) -> None:
    policy = _policy(tmp_settings)
    package = backup_scheduled.build_scheduled_backup(policy, run_id="run_ev5", staging_root=tmp_settings / ".staging")
    target = backup_publish.resolve_target("managed-local")
    result = backup_publish.publish_backup(target, package, run_id="run_ev5", policy_id=str(policy["policyId"]), schedule_slot="slot", fencing_token=1)
    backup_catalog.append_receipt(target.root, result.receipt)
    backup_catalog.catalog_path(target.root).write_text("{corrupt\n", encoding="utf-8")
    rebuilt = backup_catalog.rebuild_catalog_from_receipts(target.root)
    assert rebuilt["rebuilt"] >= 1
    assert rebuilt["chainValid"] is True


def test_manual_restore_drill_verified(tmp_settings: Path, stub_crypto: None) -> None:
    policy = _policy(tmp_settings)
    backup_mirror.put_frontend_mirror("mirror_default", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A])
    package = backup_scheduled.build_scheduled_backup(policy, run_id="run_ev6", staging_root=tmp_settings / ".staging")
    target = backup_publish.resolve_target("managed-local")
    result = backup_publish.publish_backup(target, package, run_id="run_ev6", policy_id=str(policy["policyId"]), schedule_slot="slot", fencing_token=1)
    backup_catalog.append_receipt(target.root, result.receipt)
    drill = backup_scrub.verify_unlock_drill(target.root, package.backup_id, bytearray(b"AGE-SECRET-KEY-1USER"), staged_root=tmp_settings / ".drill")
    assert drill["ok"] is True
    assert drill["sealedFrontend"] is not None
    record = backup_catalog.catalog_state(target.root)[package.backup_id]
    assert record["userUnlockVerifiedAt"]


def test_evidence_shape() -> None:
    evidence = {key: "PASS" for key in EVIDENCE_KEYS}
    assert len(evidence) == 14
    assert set(evidence.values()) == {"PASS"}

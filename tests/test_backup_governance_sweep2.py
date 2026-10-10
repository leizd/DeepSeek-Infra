"""Second coverage sweep for 4.4.4 backup governance modules."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_cron,
    backup_crypto,
    backup_mirror,
    backup_policies,
    backup_retention,
    backup_scheduled,
    backup_scheduler,
    backup_targets,
    backups,
)


UTC = timezone.utc
RECIPIENT_A = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"


@pytest.fixture
def stub_temp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / ".sys-temp"
    fake_temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def _envelope() -> dict[str, object]:
    body: dict[str, object] = {"schemaVersion": 1, "sourceVersion": "4.4.4", "createdAt": 1, "conversations": [], "conflicts": []}
    body["digest"] = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return body


@pytest.fixture
def stub_crypto(monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = b"age-encryption.org/v1\n"

    def encrypt_stream(target: Path, write_plaintext: object, *, mode: str, secret: object = None, recipients: tuple[str, ...] = (), cancel_event: object = None) -> None:
        import io

        buffer = io.BytesIO()
        write_plaintext(buffer)  # type: ignore[operator]
        target.write_bytes(prefix + bytes(buffer.getbuffer())[::-1])

    def decrypt_file(source: Path, target: Path, *, kind: str, secret: bytearray, cancel_event: object = None) -> None:
        target.write_bytes(source.read_bytes()[len(prefix):][::-1])

    monkeypatch.setattr(backup_crypto, "encrypt_stream", encrypt_stream)
    monkeypatch.setattr(backup_crypto, "decrypt_file", decrypt_file)
    monkeypatch.setattr(backup_crypto, "generate_identity", lambda: {"identity": "AGE-SECRET-KEY-1EPH", "recipient": "age1eph"})


# ── cron ────────────────────────────────────────────────────────────────────


def test_cron_empty_part_and_gap_exhaustion(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AppError):
        backup_cron.parse_cron("0,,3 * * * *")
    tz = backup_cron.load_timezone("America/New_York")
    monkeypatch.setattr(backup_cron, "_resolve_local", lambda naive, zone: None)
    with pytest.raises(AppError, match="DST transition"):
        backup_cron._gap_transition(datetime(2026, 3, 8, 2, 30), tz)


def test_cron_next_slot_none_for_impossible_date(tmp_settings: Path) -> None:
    schedule = backup_cron.parse_cron("0 0 31 2 *")
    assert backup_cron.next_slot(schedule, "UTC", after_utc=datetime(2026, 1, 1, tzinfo=UTC)) is None


# ── targets ─────────────────────────────────────────────────────────────────


def test_reparse_point_detection_paths(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "p"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(Path, "is_symlink", lambda self: True)
    assert backup_targets._is_reparse_point(target) is True
    monkeypatch.undo()
    monkeypatch.setattr(os, "lstat", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    if os.name == "nt":
        assert backup_targets._is_reparse_point(target) is False


def test_containment_skips_contributor_without_path_getter(tmp_settings: Path, stub_temp: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoRoot:
        contributor_id = "external-x"

    monkeypatch.setattr(backups, "_registered_contributors", lambda: [_NoRoot()])
    directory = tmp_path / "usb"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    assert record["targetId"]


def test_list_targets_skips_corrupt(tmp_settings: Path, stub_temp: None, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    backup_targets.init_target(directory)
    (backup_targets.BACKUP_TARGET_DIR / "target_broken.json").write_text("{not json", encoding="utf-8")
    assert len(backup_targets.list_targets()) == 1


def test_verify_wraps_location_errors(tmp_settings: Path, stub_temp: None, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    (directory / backup_targets.TARGET_MARKER_NAME).unlink()
    directory.rmdir()
    with pytest.raises(AppError, match="blocked-target-unavailable"):
        backup_targets.verify_target_ready(record["targetId"])


# ── retention ───────────────────────────────────────────────────────────────


def test_get_retention_policy_unreadable_store(tmp_settings: Path) -> None:
    backup_retention.BACKUP_RETENTION_DIR.mkdir(parents=True, exist_ok=True)
    (backup_retention.BACKUP_RETENTION_DIR / "x.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(AppError, match="unreadable"):
        backup_retention.get_retention_policy("x")


def test_restore_references_scans_nested_lists(tmp_settings: Path) -> None:
    restore = backups.RESTORE_DIR / "restore_n"
    restore.mkdir(parents=True)
    (restore / "plan.json").write_text(json.dumps({"nested": [{"backupId": "b_nested"}, ["b2"]]}), encoding="utf-8")
    references = backup_retention._restore_references()
    assert "b_nested" in references


def test_retention_max_total_bytes_discards_unpinned(tmp_settings: Path, tmp_path: Path) -> None:
    root = tmp_path / "target"
    (root / "backups").mkdir(parents=True)
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    for index, size in ((0, 100), (1, 100), (2, 100)):
        created = (now - timedelta(hours=index)).isoformat(timespec="seconds").replace("+00:00", "Z")
        (root / "backups" / f"b{index}.age").write_bytes(b"x" * size)
        backup_catalog.append_receipt(
            root,
            {"schemaVersion": 1, "backupId": f"b{index}", "runId": "r", "policyId": "p", "targetId": "managed-local", "scheduleSlot": "s", "filename": f"b{index}.age", "size": size, "ciphertextSha256": "a" * 64, "manifestDigest": "b" * 64, "coverageDigest": "c" * 64, "creationVerified": True, "createdAt": created, "pinned": False},
        )
    policy = backup_retention.normalize_retention_policy({"keepLast": 3, "keepHourly": 0, "keepDaily": 0, "keepWeekly": 0, "keepMonthly": 0, "minimumHealthyCopies": 1, "maxTotalBytes": 150})
    preview = backup_retention.preview_retention(policy, root, now=now)
    assert "b1" not in preview["keep"] or "b2" not in preview["keep"]


def test_apply_skips_unsafe_filenames(tmp_settings: Path, tmp_path: Path) -> None:
    root = tmp_path / "target"
    (root / "backups").mkdir(parents=True)
    backup_catalog.append_receipt(
        root,
        {"schemaVersion": 1, "backupId": "evil", "runId": "r", "policyId": "p", "targetId": "managed-local", "scheduleSlot": "s", "filename": "../evil.age", "size": 1, "ciphertextSha256": "a" * 64, "manifestDigest": "b" * 64, "coverageDigest": "c" * 64, "creationVerified": True, "createdAt": "2026-01-01T00:00:00Z", "pinned": False},
    )
    backup_catalog.append_receipt(
        root,
        {"schemaVersion": 1, "backupId": "new", "runId": "r", "policyId": "p", "targetId": "managed-local", "scheduleSlot": "s", "filename": "new.age", "size": 1, "ciphertextSha256": "a" * 64, "manifestDigest": "b" * 64, "coverageDigest": "c" * 64, "creationVerified": True, "createdAt": "2026-06-15T00:00:00Z", "pinned": False},
    )
    policy = backup_retention.normalize_retention_policy({"keepLast": 1, "keepHourly": 0, "keepDaily": 0, "keepWeekly": 0, "keepMonthly": 0, "minimumHealthyCopies": 1})
    applied = backup_retention.apply_retention(policy, root, now=datetime(2026, 6, 15, 12, 0, tzinfo=UTC))
    assert applied["trashed"] == []
    assert not (root / ".trash" / "evil").exists()


# ── scheduler ───────────────────────────────────────────────────────────────


def test_complete_on_terminal_run_rejected(tmp_settings: Path) -> None:
    policy = backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": "nightly",
            "enabled": True,
            "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
            "protection": {"mode": "age-recipient", "recipients": [RECIPIENT_A]},
            "targetId": "managed-local",
        }
    )
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=now)[0]
    backup_scheduler.fail_run(run.run_id, error="x", instance_id="w1", fencing_token=run.fencing_token, now=now)
    with pytest.raises(AppError, match="no longer active"):
        backup_scheduler.complete_run(run.run_id, backup_id="b", filename="f", instance_id="w1", fencing_token=run.fencing_token)


def test_record_phase_and_requeue_ownership(tmp_settings: Path) -> None:
    policy = backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": "nightly",
            "enabled": True,
            "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
            "protection": {"mode": "age-recipient", "recipients": [RECIPIENT_A]},
            "targetId": "managed-local",
        }
    )
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=now)[0]
    with pytest.raises(AppError, match="lease"):
        backup_scheduler.requeue_run(run.run_id, instance_id="w2", fencing_token=run.fencing_token, retry_at=now)
    with pytest.raises(AppError, match="lease"):
        backup_scheduler.fail_run(run.run_id, error="x", instance_id="w2", fencing_token=run.fencing_token)
    with pytest.raises(AppError, match="lease"):
        backup_scheduler.record_run_phase(run.run_id, "snapshotting", instance_id="w2", fencing_token=run.fencing_token)
    backup_scheduler.record_run_phase(run.run_id, "deferred", reason="workspace-restore-active")
    assert backup_scheduler.get_run(run.run_id)["phase"] == "deferred"


def test_next_run_none_for_bad_timezone_policy(tmp_settings: Path) -> None:
    assert backup_scheduler.next_run_for_policy({"policyId": "p", "schedule": {"cron": "0 3 * * *", "timezone": "Nowhere/Zone"}}, now=datetime(2026, 6, 1, tzinfo=UTC)) is None


# ── mirror / scheduled build ────────────────────────────────────────────────


def test_mirror_blank_ack_and_oversize_envelope(tmp_settings: Path, stub_crypto: None, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AppError, match="acknowledgedAt"):
        backup_mirror.put_frontend_mirror("mirror_x", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at=" ")
    monkeypatch.setattr(backup_mirror, "_MAX_ENVELOPE_BYTES", 16)
    with pytest.raises(AppError) as too_large:
        backup_mirror.put_frontend_mirror("mirror_x", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A])
    assert too_large.value.status == 413


def test_mirror_coverage_includes_profile_id_when_missing(tmp_settings: Path, stub_crypto: None) -> None:
    policy = backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": "nightly",
            "enabled": True,
            "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
            "frontendMirror": {"mode": "best-effort", "profileId": "mirror_pinned", "maxAgeSeconds": 60},
            "protection": {"mode": "age-recipient", "recipients": [RECIPIENT_A]},
            "targetId": "managed-local",
        }
    )
    _, coverage = backup_scheduled.mirror_coverage(policy)
    assert coverage == {"mode": "sealed-mirror", "status": "missing", "profileId": "mirror_pinned"}


def test_scheduled_build_propagates_build_errors(tmp_settings: Path, stub_crypto: None, monkeypatch: pytest.MonkeyPatch) -> None:
    policy = backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": "nightly",
            "enabled": True,
            "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
            "scope": {"mode": "full", "includeExternalState": False, "coveragePolicy": "best-effort"},
            "frontendMirror": {"mode": "excluded"},
            "protection": {"mode": "age-recipient", "recipients": [RECIPIENT_A]},
            "targetId": "managed-local",
        }
    )
    monkeypatch.setattr(backup_scheduled, "_build_candidate", lambda *a, **k: (_ for _ in ()).throw(AppError("build boom", status=500)))
    with pytest.raises(AppError, match="build boom"):
        backup_scheduled.build_scheduled_backup(policy, run_id="run_b", staging_root=tmp_settings / ".staging")

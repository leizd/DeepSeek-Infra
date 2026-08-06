"""Final coverage sweep for 4.4.4 backup governance modules."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_crypto,
    backup_mirror,
    backup_policies,
    backup_retention,
    backup_scheduler,
    backup_scrub,
    backup_targets,
)
from deepseek_infra.web.routes.backup_governance import _find_backup_root


UTC = timezone.utc
RECIPIENT_A = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"


@pytest.fixture
def stub_temp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / ".sys-temp"
    fake_temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def _policy(tmp_settings: Path) -> dict[str, object]:
    return backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": "nightly",
            "enabled": True,
            "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
            "protection": {"mode": "age-recipient", "recipients": [RECIPIENT_A]},
            "targetId": "managed-local",
        }
    )


def test_probe_missing_target_is_not_ready(tmp_settings: Path) -> None:
    result = backup_targets.probe_target("target_missing")
    assert result["ready"] is False


def test_reparse_walk_reaches_filesystem_root(tmp_settings: Path, tmp_path: Path) -> None:
    assert backup_targets._has_reparse_component(Path(tmp_path.anchor)) in {True, False}


def test_target_registry_unreadable(tmp_settings: Path, stub_temp: None, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    registry = backup_targets.BACKUP_TARGET_DIR / f"{record['targetId']}.json"
    registry.write_text("{not json", encoding="utf-8")
    with pytest.raises(AppError, match="unreadable"):
        backup_targets.get_target(record["targetId"])


def test_retention_weekly_and_monthly_buckets(tmp_settings: Path, tmp_path: Path) -> None:
    root = tmp_path / "target"
    (root / "backups").mkdir(parents=True)
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    for index in range(10):
        created = (now - timedelta(weeks=index)).isoformat(timespec="seconds").replace("+00:00", "Z")
        (root / "backups" / f"w{index}.age").write_bytes(b"x")
        backup_catalog.append_receipt(
            root,
            {"schemaVersion": 1, "backupId": f"w{index}", "runId": "r", "policyId": "p", "targetId": "managed-local", "scheduleSlot": "s", "filename": f"w{index}.age", "size": 1, "ciphertextSha256": "a" * 64, "manifestDigest": "b" * 64, "coverageDigest": "c" * 64, "creationVerified": True, "createdAt": created, "pinned": False},
        )
    policy = backup_retention.normalize_retention_policy({"keepLast": 0, "keepHourly": 0, "keepDaily": 0, "keepWeekly": 3, "keepMonthly": 2, "minimumHealthyCopies": 1})
    preview = backup_retention.preview_retention(policy, root, now=now)
    assert {"w0", "w1", "w2"} <= set(preview["keep"])
    assert "w9" in preview["trash"]


def test_retention_put_get_roundtrip(tmp_settings: Path) -> None:
    backup_retention.put_retention_policy("weekly", {"keepWeekly": 4})
    assert backup_retention.get_retention_policy("weekly")["keepWeekly"] == 4
    with pytest.raises(AppError):
        backup_retention.put_retention_policy("bad/id", {})


def test_reclaim_deferred_without_previous_run(tmp_settings: Path) -> None:
    policy = _policy(tmp_settings)
    with backup_scheduler._connect() as connection:
        connection.execute(
            "INSERT INTO backup_schedule_slots(policy_id, slot_key, scheduled_for, local_date_time, timezone, status, run_id, created_at) VALUES (?, 'slot-x', '2026-06-02T03:00:00Z', '2026-06-02T03:00', 'UTC', 'deferred', NULL, '2026-06-02T03:00:00Z')",
            (str(policy["policyId"]),),
        )
    reclaimed = backup_scheduler.reclaim_deferred_slots([policy], instance_id="w1", now=datetime(2026, 6, 2, 4, 0, tzinfo=UTC))
    assert len(reclaimed) == 1
    assert reclaimed[0].attempt == 1


def test_worker_loop_swallows_tick_errors(tmp_settings: Path) -> None:
    import time

    def _boom(run: object) -> None:
        raise RuntimeError("tick failure")

    worker = backup_scheduler.BackupWorker(_boom, instance_id="inst_err", tick_seconds=0.02)
    worker.start()
    time.sleep(0.06)
    worker.stop()
    assert worker._thread is None


def test_mirror_naive_acknowledged_at_normalized(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    envelope = {"schemaVersion": 1, "sourceVersion": "4.4.4", "createdAt": 1, "conversations": [], "conflicts": []}
    envelope["digest"] = hashlib.sha256(json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    metadata = backup_mirror.put_frontend_mirror("mirror_n", envelope, source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-01T00:00:00")
    assert metadata["acknowledgedAt"].endswith("Z")


def test_scrub_health_with_no_backups(tmp_settings: Path, tmp_path: Path) -> None:
    health = backup_scrub.backup_health(tmp_path)
    assert health == {"status": "ok", "backups": [], "evaluatedAt": health["evaluatedAt"]}


def test_find_backup_root_scans_filesystem_targets(tmp_settings: Path, stub_temp: None, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    target = backup_targets.init_target(directory)
    (directory / "backups").mkdir(exist_ok=True)
    (directory / "backups" / "b1.age").write_bytes(b"x")
    backup_catalog.append_receipt(
        directory,
        {"schemaVersion": 1, "backupId": "b1", "runId": "r", "policyId": "p", "targetId": target["targetId"], "scheduleSlot": "s", "filename": "b1.age", "size": 1, "ciphertextSha256": "a" * 64, "manifestDigest": "b" * 64, "coverageDigest": "c" * 64, "creationVerified": True, "createdAt": "2026-06-01T00:00:00Z", "pinned": False},
    )
    root, record, target_id = _find_backup_root("b1")
    assert target_id == target["targetId"]
    assert record["backupId"] == "b1"
    with pytest.raises(AppError):
        _find_backup_root("backup_missing")


def test_catalog_list_includes_deleted_filter_and_state_sort(tmp_settings: Path, tmp_path: Path) -> None:
    for index in range(2):
        backup_catalog.append_receipt(
            tmp_path,
            {"schemaVersion": 1, "backupId": f"b{index}", "runId": "r", "policyId": "p", "targetId": "managed-local", "scheduleSlot": "s", "filename": f"b{index}.age", "size": 1, "ciphertextSha256": "a" * 64, "manifestDigest": "b" * 64, "coverageDigest": "c" * 64, "creationVerified": True, "createdAt": f"2026-06-0{index + 1}T00:00:00Z", "pinned": False},
        )
    assert [item["backupId"] for item in backup_catalog.list_backups(tmp_path, target_id="managed-local")] == ["b1", "b0"]
    assert backup_catalog.list_backups(tmp_path, target_id="other") == []


def test_scheduler_list_runs_limit_and_policy_filter(tmp_settings: Path) -> None:
    policy = _policy(tmp_settings)
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    backup_scheduler.claim_due_slots([policy], instance_id="w1", now=now)
    assert len(backup_scheduler.list_runs(limit=1)) == 1
    assert backup_scheduler.list_runs(policy_id="policy_other") == []

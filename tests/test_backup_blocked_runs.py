from __future__ import annotations

import logging
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_crypto,
    backup_executor,
    backup_policies,
    backup_publish,
    backup_scheduler,
    backup_targets,
    backups,
)


UTC = timezone.utc
RECIPIENT_A = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"
NOW = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _tempdir_outside_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / ".sys-temp"
    fake_temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


@pytest.fixture
def stub_crypto(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _seed_workspace() -> None:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('{"items":[]}', encoding="utf-8")


def _offline_target(tmp_path: Path) -> tuple[dict[str, object], Path, bytes]:
    directory = tmp_path / "usb"
    directory.mkdir()
    target = backup_targets.init_target(directory)
    marker_path = directory / backup_targets.TARGET_MARKER_NAME
    saved = marker_path.read_bytes()
    marker_path.unlink()
    return target, directory, saved


def _restore_marker(directory: Path, saved: bytes) -> None:
    (directory / backup_targets.TARGET_MARKER_NAME).write_bytes(saved)


def test_blocked_retryable_reclaimed_when_target_returns(tmp_settings: Path, stub_crypto: None, tmp_path: Path) -> None:
    _seed_workspace()
    target, directory, saved = _offline_target(tmp_path)
    policy = _policy(tmp_settings, targetId=target["targetId"])
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    outcome = backup_executor.execute_run(run, instance_id="w1", now=NOW)
    assert outcome["phase"] == "blocked-retryable"
    stored = backup_scheduler.get_run(run.run_id)
    assert stored["nextRetryAt"] == stored["leaseUntil"]
    assert stored["blockedReason"] == "blocked-target-unavailable"
    probe = backup_scheduler.reclaim_blocked_slots([policy], instance_id="w2", now=NOW + timedelta(seconds=60))
    assert probe == []
    still = backup_scheduler.get_run(run.run_id)
    assert still["phase"] == "blocked-retryable"
    assert still["nextRetryAt"] > stored["nextRetryAt"]
    _restore_marker(directory, saved)
    reclaimed = backup_scheduler.reclaim_blocked_slots([policy], instance_id="w2", now=NOW + timedelta(seconds=400))
    assert len(reclaimed) == 1
    takeover = reclaimed[0]
    assert takeover.attempt == 2
    assert backup_scheduler.get_run(run.run_id)["phase"] == "abandoned"
    completed = backup_executor.execute_run(takeover, instance_id="w2", now=NOW + timedelta(seconds=400))
    assert completed["phase"] == "complete"
    assert backup_publish.read_commit_markers(directory)


def test_blocked_terminal_when_attempts_exhausted(tmp_settings: Path, stub_crypto: None, tmp_path: Path) -> None:
    _seed_workspace()
    target, _, _ = _offline_target(tmp_path)
    policy = _policy(tmp_settings, targetId=target["targetId"], retry={"maxAttempts": 1, "initialBackoffSeconds": 30, "maxBackoffSeconds": 60})
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    outcome = backup_executor.execute_run(run, instance_id="w1", now=NOW)
    assert outcome["phase"] == "blocked-terminal"
    stored = backup_scheduler.get_run(run.run_id)
    assert stored["phase"] == "blocked-terminal"
    assert stored["nextRetryAt"] is None
    assert backup_scheduler.reclaim_blocked_slots([policy], instance_id="w2", now=NOW + timedelta(hours=2)) == []


def test_blocked_terminal_outside_catchup_window(tmp_settings: Path, stub_crypto: None, tmp_path: Path) -> None:
    _seed_workspace()
    target, _, _ = _offline_target(tmp_path)
    policy = backup_policies.update_policy(
        str(_policy(tmp_settings, targetId=target["targetId"])["policyId"]),
        {"schedule": {"cron": "0 3 * * *", "timezone": "UTC", "catchupWindowSeconds": 3600}},
    )
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    stale = backup_executor.execute_run(run, instance_id="w1", now=NOW + timedelta(hours=2))
    assert stale["phase"] == "abandoned"
    reclaimed = backup_scheduler.reclaim_abandoned_slots(instance_id="w2", now=NOW + timedelta(hours=2))
    assert len(reclaimed) == 1
    outcome = backup_executor.execute_run(reclaimed[0], instance_id="w2", now=NOW + timedelta(hours=2))
    assert outcome["phase"] == "blocked-terminal"
    assert backup_scheduler.get_run(reclaimed[0].run_id)["phase"] == "blocked-terminal"


def test_blocked_run_of_missing_policy_becomes_terminal(tmp_settings: Path, tmp_path: Path) -> None:
    target, _, _ = _offline_target(tmp_path)
    policy = _policy(tmp_settings, targetId=target["targetId"])
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    backup_scheduler.block_run(run.run_id, instance_id="w1", fencing_token=run.fencing_token, error="x", reason="blocked-target-unavailable", retry_at=NOW + timedelta(seconds=30), now=NOW)
    backup_policies.delete_policy(str(policy["policyId"]))
    assert backup_scheduler.reclaim_blocked_slots([], instance_id="w2", now=NOW + timedelta(seconds=60)) == []
    assert backup_scheduler.get_run(run.run_id)["phase"] == "blocked-terminal"


def test_manual_run_slot_keys_are_unique_uuids(tmp_settings: Path) -> None:
    policy = _policy(tmp_settings)
    first = backup_scheduler.claim_manual_run(policy, instance_id="w1", now=NOW)
    second = backup_scheduler.claim_manual_run(policy, instance_id="w1", now=NOW)
    assert first.schedule_slot != second.schedule_slot
    assert first.schedule_slot.startswith("manual/")
    assert len(first.schedule_slot) == len("manual/") + 32
    assert backup_scheduler.get_run(first.run_id)["phase"] == "leased"
    assert backup_scheduler.get_run(second.run_id)["phase"] == "leased"


def test_incomplete_journal_marks_run_reconciling(tmp_settings: Path, stub_crypto: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_workspace()
    policy = _policy(tmp_settings)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    root = backups.BACKUP_DIR
    (root / "transactions").mkdir(parents=True, exist_ok=True)
    (root / "transactions" / "run_stale.json").write_text(
        '{"runId": "run_stale", "policyId": "%s", "scheduleSlot": "%s", "phase": "receipt-published"}'
        % (policy["policyId"], run.schedule_slot),
        encoding="utf-8",
    )
    phases: list[str] = []
    original = backup_scheduler.record_run_phase

    def spy(run_id: str, phase: str, **kwargs: object) -> None:
        phases.append(phase)
        original(run_id, phase, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backup_executor.backup_scheduler, "record_run_phase", spy)
    outcome = backup_executor.execute_run(run, instance_id="w1", now=NOW)
    assert outcome["phase"] == "complete"
    assert "reconciling" in phases
    assert backup_scheduler.get_run(run.run_id)["reason"] == "interrupted-target-transaction"


def test_worker_loop_logs_and_counts_failures(tmp_settings: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**_kwargs: object) -> None:
        raise RuntimeError("tick exploded")

    monkeypatch.setattr(backup_scheduler, "reclaim_abandoned_slots", boom)
    worker = backup_scheduler.BackupWorker(lambda _run: None, instance_id="w1", tick_seconds=0.02, reconcile_on_start=False)
    with caplog.at_level(logging.ERROR, logger="deepseek_infra.backup_worker"):
        worker.start()
        try:
            deadline = time.monotonic() + 15.0
            while worker.tick_failures < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            worker.stop()
    assert worker.tick_failures >= 2
    assert any("backup worker tick failed" in record.getMessage() for record in caplog.records)


def test_worker_tick_reclaims_blocked_runs(tmp_settings: Path, stub_crypto: None, tmp_path: Path) -> None:
    _seed_workspace()
    target, directory, saved = _offline_target(tmp_path)
    policy = _policy(tmp_settings, targetId=target["targetId"])
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    assert backup_executor.execute_run(run, instance_id="w1", now=NOW)["phase"] == "blocked-retryable"
    _restore_marker(directory, saved)
    later = NOW + timedelta(seconds=400)
    seen: list[backup_scheduler.ClaimedRun] = []

    def _execute(claimed: backup_scheduler.ClaimedRun) -> object:
        seen.append(claimed)
        return backup_executor.execute_run(claimed, instance_id="w2", now=later)

    tick = backup_scheduler.worker_tick(instance_id="w2", executor=_execute, now=later)
    assert tick["blocked"] == 1
    assert any(candidate.schedule_slot == run.schedule_slot for candidate in seen)
    assert backup_scheduler.get_run(seen[0].run_id)["phase"] == "complete"


def test_manual_run_rejects_duplicate_slot_insert(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uuid as uuid_module
    from types import SimpleNamespace

    monkeypatch.setattr(backup_scheduler, "uuid", SimpleNamespace(uuid4=lambda: uuid_module.UUID(int=7)))
    policy = _policy(tmp_settings)
    backup_scheduler.claim_manual_run(policy, instance_id="w1", now=NOW)
    with pytest.raises(AppError) as exc:
        backup_scheduler.claim_manual_run(policy, instance_id="w1", now=NOW)
    assert exc.value.status == 409


def test_worker_start_logs_reconcile_failure(tmp_settings: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.workspace import backup_reconcile

    def boom(**_kwargs: object) -> None:
        raise RuntimeError("reconcile exploded")

    monkeypatch.setattr(backup_reconcile, "reconcile_all_targets", boom)
    worker = backup_scheduler.BackupWorker(lambda _run: None, instance_id="w1", tick_seconds=3600, reconcile_on_start=True)
    with caplog.at_level(logging.ERROR, logger="deepseek_infra.backup_worker"):
        worker.start()
        try:
            time.sleep(0.05)
        finally:
            worker.stop()
    assert any("startup reconciliation failed" in record.getMessage() for record in caplog.records)


def test_publish_blocked_oserror_becomes_blocked_retryable(tmp_settings: Path, stub_crypto: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_workspace()
    policy = _policy(tmp_settings)

    def broken_publish(*_args: object, **_kwargs: object) -> None:
        raise AppError("blocked-target-unavailable: disk full", status=503)

    monkeypatch.setattr(backup_executor.backup_publish, "publish_backup", broken_publish)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    outcome = backup_executor.execute_run(run, instance_id="w1", now=NOW)
    assert outcome["phase"] == "blocked-retryable"
    assert outcome["retryInSeconds"] > 0


def test_blocked_outcome_tolerates_unparsable_slot_time(tmp_settings: Path, stub_crypto: None, tmp_path: Path) -> None:
    import dataclasses

    _seed_workspace()
    target, _, _ = _offline_target(tmp_path)
    policy = _policy(tmp_settings, targetId=target["targetId"])
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    warped = dataclasses.replace(run, scheduled_for="not-a-date")
    outcome = backup_executor.execute_run(warped, instance_id="w1", now=NOW)
    assert outcome["phase"] == "blocked-retryable"


def test_slot_has_incomplete_journal_edges(tmp_path: Path) -> None:
    assert backup_publish.slot_has_incomplete_journal(tmp_path, policy_id="p", schedule_slot="s") is False
    marker = tmp_path / "commits" / "p" / "x.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    (tmp_path / "transactions").mkdir()
    (tmp_path / "transactions" / "run_1.json").write_text('{"runId": "run_1", "policyId": "p", "scheduleSlot": "s", "phase": "started"}', encoding="utf-8")
    from unittest.mock import patch as mock_patch

    with mock_patch.object(backup_publish, "commit_marker_path", return_value=marker):
        assert backup_publish.slot_has_incomplete_journal(tmp_path, policy_id="p", schedule_slot="s") is False
    assert backup_publish.slot_has_incomplete_journal(tmp_path, policy_id="other", schedule_slot="s") is False

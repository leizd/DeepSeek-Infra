from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_crypto,
    backup_executor,
    backup_object_set,
    backup_policies,
    backup_publish,
    backup_scheduler,
    backups,
)


UTC = timezone.utc
RECIPIENT_A = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"
NOW = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)


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
    monkeypatch.setattr(backup_crypto, "capabilities", lambda: {"encryptedBackupAvailable": True, "formats": ["age-v1"], "protectionModes": ["passphrase", "age-recipient"]})


def _policy(tmp_settings: Path) -> dict[str, object]:
    return backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": "nightly",
            "enabled": True,
            "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
            "scope": {"mode": "full", "includeExternalState": False, "coveragePolicy": "best-effort"},
            "frontendMirror": {"mode": "best-effort"},
            "protection": {"mode": "age-recipient", "recipients": [RECIPIENT_A]},
            "targetId": "managed-local",
            "retry": {"maxAttempts": 1, "initialBackoffSeconds": 30, "maxBackoffSeconds": 60},
        }
    )


def _claim(policy: dict[str, object], *, instance: str = "w1") -> backup_scheduler.ClaimedRun:
    claimed = backup_scheduler.claim_due_slots([policy], instance_id=instance, now=NOW)
    assert len(claimed) == 1
    return claimed[0]


def _anchored_clock() -> list[datetime]:
    return [NOW]


def _clock_from(box: list[datetime]) -> Callable[[], datetime]:
    return lambda: box[0]


def test_lease_guard_heartbeat_renews_lease(tmp_settings: Path) -> None:
    run = _claim(_policy(tmp_settings))
    initial_until = str(backup_scheduler.get_run(run.run_id)["leaseUntil"])
    box = _anchored_clock()
    guard = backup_scheduler.RunLeaseGuard(run.run_id, "w1", run.fencing_token, heartbeat_seconds=0.05, clock=_clock_from(box))
    guard.start_heartbeat()
    try:
        for step in range(5):
            time.sleep(0.06)
            box[0] = NOW + timedelta(seconds=10 * (step + 1))
    finally:
        guard.stop()
    renewed_until = str(backup_scheduler.get_run(run.run_id)["leaseUntil"])
    assert renewed_until > initial_until
    assert not guard.cancel_event.is_set()
    guard.checkpoint()


def test_lease_guard_heartbeat_failure_sets_cancel(tmp_settings: Path) -> None:
    run = _claim(_policy(tmp_settings))
    reclaimed = backup_scheduler.reclaim_abandoned_slots(instance_id="w2", now=NOW + timedelta(seconds=400))
    assert len(reclaimed) == 1
    guard = backup_scheduler.RunLeaseGuard(run.run_id, "w1", run.fencing_token, heartbeat_seconds=0.05)
    guard.start_heartbeat()
    try:
        deadline = time.monotonic() + 5.0
        while not guard.cancel_event.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        guard.stop()
    assert guard.cancel_event.is_set()
    with pytest.raises(AppError) as exc:
        guard.checkpoint()
    assert exc.value.status == 409
    assert "lease" in str(exc.value).casefold()


def test_expired_lease_blocks_run_transitions(tmp_settings: Path) -> None:
    run = _claim(_policy(tmp_settings))
    expired = NOW + timedelta(seconds=400)
    with pytest.raises(AppError) as phase_error:
        backup_scheduler.record_run_phase(run.run_id, "snapshotting", instance_id="w1", fencing_token=run.fencing_token, now=expired)
    assert phase_error.value.status == 409
    with pytest.raises(AppError) as complete_error:
        backup_scheduler.complete_run(run.run_id, backup_id="b", filename="f", instance_id="w1", fencing_token=run.fencing_token, now=expired)
    assert complete_error.value.status == 409
    with pytest.raises(AppError) as requeue_error:
        backup_scheduler.requeue_run(run.run_id, instance_id="w1", fencing_token=run.fencing_token, retry_at=expired + timedelta(seconds=60), error="x", now=expired)
    assert requeue_error.value.status == 409
    with pytest.raises(AppError) as fail_error:
        backup_scheduler.fail_run(run.run_id, error="x", instance_id="w1", fencing_token=run.fencing_token, now=expired)
    assert fail_error.value.status == 409
    assert backup_scheduler.get_run(run.run_id)["phase"] == "leased"


def test_run_transitions_accept_valid_lease(tmp_settings: Path) -> None:
    run = _claim(_policy(tmp_settings))
    backup_scheduler.record_run_phase(run.run_id, "snapshotting", instance_id="w1", fencing_token=run.fencing_token, now=NOW + timedelta(seconds=10))
    backup_scheduler.complete_run(run.run_id, backup_id="b", filename="f", instance_id="w1", fencing_token=run.fencing_token, now=NOW + timedelta(seconds=20))
    assert backup_scheduler.get_run(run.run_id)["phase"] == "complete"


def test_cancelled_guard_checkpoint_raises(tmp_settings: Path) -> None:
    run = _claim(_policy(tmp_settings))
    guard = backup_scheduler.RunLeaseGuard(run.run_id, "w1", run.fencing_token)
    guard.cancel_event.set()
    with pytest.raises(AppError) as exc:
        guard.checkpoint()
    assert exc.value.status == 409
    assert "lease" in str(exc.value).casefold()


def test_execute_run_abandons_when_lease_expires_during_publish(tmp_settings: Path, stub_crypto: None, monkeypatch: pytest.MonkeyPatch) -> None:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('{"items":[]}', encoding="utf-8")
    policy = _policy(tmp_settings)
    run = _claim(policy)
    box = _anchored_clock()

    original_publish = backup_publish.publish_backup

    def publish_with_expired_lease(*args: object, **kwargs: object) -> object:
        box[0] = NOW + timedelta(seconds=400)
        return original_publish(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backup_executor.backup_publish, "publish_backup", publish_with_expired_lease)
    outcome = backup_executor.execute_run(run, instance_id="w1", now=NOW, clock=_clock_from(box))
    assert outcome["phase"] == "abandoned"
    assert "lease" in str(outcome["error"]).casefold()
    objects_root = backups.BACKUP_DIR / "objects"
    assert not list(objects_root.rglob("*.age")) if objects_root.is_dir() else True
    monkeypatch.setattr(backup_executor.backup_publish, "publish_backup", original_publish)
    reclaimed = backup_scheduler.reclaim_abandoned_slots(instance_id="w2", now=NOW + timedelta(seconds=400))
    assert len(reclaimed) == 1
    takeover = backup_executor.execute_run(reclaimed[0], instance_id="w2", now=NOW + timedelta(seconds=400))
    assert takeover["phase"] == "complete"
    published = list((backups.BACKUP_DIR / "objects").rglob("*.age"))
    committed = next(iter(backup_catalog.catalog_state(backups.BACKUP_DIR).values()))
    expected_digests = backup_object_set.committed_object_digests(committed)
    assert {path.stem for path in published} == expected_digests
    assert backup_scheduler.get_run(str(takeover["runId"]))["phase"] == "complete"

from __future__ import annotations

import hashlib
import json
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
    backup_scheduler,
    backups,
    mutation_gate,
)


UTC = timezone.utc
RECIPIENT_A = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"


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


def _policy(tmp_settings: Path, **schedule: object) -> dict[str, object]:
    base: dict[str, object] = {"cron": "0 3 * * *", "timezone": "UTC"}
    base.update(schedule)
    return backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": "nightly",
            "enabled": True,
            "schedule": base,
            "scope": {"mode": "full", "includeExternalState": False, "coveragePolicy": "best-effort"},
            "frontendMirror": {"mode": "best-effort"},
            "protection": {"mode": "age-recipient", "recipients": [RECIPIENT_A]},
            "targetId": "managed-local",
            "retry": {"maxAttempts": 1, "initialBackoffSeconds": 30, "maxBackoffSeconds": 60},
        }
    )


def _envelope() -> dict[str, object]:
    body: dict[str, object] = {
        "schemaVersion": 1,
        "sourceVersion": config.APP_VERSION,
        "createdAt": 1,
        "conversations": [],
        "conflicts": [],
    }
    body["digest"] = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return body


def _claim_and_run(policy: dict[str, object], *, now: datetime, instance: str = "w1") -> dict[str, object]:
    claimed = backup_scheduler.claim_due_slots([policy], instance_id=instance, now=now)
    assert len(claimed) == 1
    return backup_executor.execute_run(claimed[0], instance_id=instance, now=now)


def test_execute_run_completes_and_publishes(tmp_settings: Path, stub_crypto: None) -> None:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('{"items":[]}', encoding="utf-8")
    backup_mirror.put_frontend_mirror("mirror_default", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A])
    policy = _policy(tmp_settings)
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    outcome = _claim_and_run(policy, now=now)
    assert outcome["phase"] == "complete"
    filename = str(outcome["filename"])
    record = backup_catalog.catalog_state(backups.BACKUP_DIR)[str(outcome["backupId"])]
    published = backup_publish.backup_file_candidates(backups.BACKUP_DIR, record)[0]
    assert published.is_file()
    assert published.read_bytes().startswith(b"age-encryption.org/v1")
    assert published.name == f"{record['objectDigest']}.age"
    receipt = backups.BACKUP_DIR / "receipts" / f"{outcome['backupId']}.json"
    assert receipt.is_file()
    marker = backup_publish.commit_marker_path(backups.BACKUP_DIR, str(policy["policyId"]), "2026-06-02T03:00@UTC")
    assert marker.is_file()
    run = backup_scheduler.get_run(str(outcome["runId"]))
    assert run["phase"] == "complete"
    assert run["filename"] == filename


def test_execute_run_defers_during_restore_fence(tmp_settings: Path, stub_crypto: None) -> None:
    policy = _policy(tmp_settings)
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    mutation_gate.write_fence({"restoreId": "r1", "phase": "commit"}, root=tmp_settings)
    try:
        outcome = _claim_and_run(policy, now=now)
        assert outcome["phase"] == "deferred"
        assert outcome["reason"] == "workspace-restore-active"
        assert backup_scheduler.get_run(str(outcome["runId"]))["phase"] == "deferred"
    finally:
        mutation_gate.clear_fence("r1", root=tmp_settings)
    reclaimed = backup_scheduler.reclaim_deferred_slots([policy], instance_id="w1", now=now + timedelta(hours=1))
    assert len(reclaimed) == 1
    retry = backup_executor.execute_run(reclaimed[0], instance_id="w1", now=now + timedelta(hours=1))
    assert retry["phase"] == "complete"


def test_execute_run_fails_when_policy_missing(tmp_settings: Path, stub_crypto: None) -> None:
    policy = _policy(tmp_settings)
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    claimed = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=now)
    backup_policies.delete_policy(str(policy["policyId"]))
    outcome = backup_executor.execute_run(claimed[0], instance_id="w1", now=now)
    assert outcome["phase"] == "failed"
    assert outcome["reason"] == "policy-missing"


def test_execute_run_blocked_mirror_retries_then_fails(tmp_settings: Path, stub_crypto: None) -> None:
    policy = _policy(tmp_settings)
    policy = backup_policies.update_policy(
        str(policy["policyId"]),
        {"frontendMirror": {"mode": "required", "maxAgeSeconds": 60}, "retry": {"maxAttempts": 2, "initialBackoffSeconds": 30, "maxBackoffSeconds": 60}},
    )
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    outcome = _claim_and_run(policy, now=now)
    assert outcome["phase"] == "queued"
    assert "blocked-frontend-mirror" in str(outcome["error"])
    reclaimed = backup_scheduler.reclaim_abandoned_slots(instance_id="w1", now=now + timedelta(seconds=120))
    assert len(reclaimed) == 1
    final = backup_executor.execute_run(reclaimed[0], instance_id="w1", now=now + timedelta(seconds=120))
    assert final["phase"] == "failed"
    assert "blocked-frontend-mirror" in str(final["error"])


def test_execute_run_with_wrong_instance_abandons(tmp_settings: Path, stub_crypto: None) -> None:
    policy = _policy(tmp_settings)
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    claimed = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=now)
    outcome = backup_executor.execute_run(claimed[0], instance_id="intruder", now=now)
    assert outcome["phase"] in {"failed", "abandoned"}
    assert not list((backups.BACKUP_DIR / "objects").rglob("*.age")) if (backups.BACKUP_DIR / "objects").is_dir() else True

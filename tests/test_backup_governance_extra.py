"""Coverage-oriented edge tests for the 4.4.4 backup governance modules."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deepseek_infra import backup_worker as worker_module
from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
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
        "frontendMirror": {"mode": "excluded"},
        "protection": {"mode": "age-recipient", "recipients": [RECIPIENT_A]},
        "targetId": "managed-local",
    }
    payload.update(overrides)
    return backup_policies.create_policy(payload)


# ── backup_worker / BackupWorker ────────────────────────────────────────────


def test_worker_module_create_and_embedded_modes(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worker = worker_module.create_worker("inst_test")
    assert worker.instance_id == "inst_test"
    monkeypatch.setenv("DEEPSEEK_BACKUP_TICK_SECONDS", "0.05")
    worker = worker_module.create_worker()
    assert worker.tick_seconds == 0.05
    monkeypatch.setenv("DEEPSEEK_BACKUP_WORKER", "disabled")
    assert worker_module.start_embedded_worker() is None
    monkeypatch.setenv("DEEPSEEK_BACKUP_WORKER", "embedded")
    started = worker_module.start_embedded_worker()
    assert started is not None
    started.stop()


def test_backup_worker_tick_loop_runs_and_stops(tmp_settings: Path, stub_crypto: None) -> None:
    calls: list[int] = []
    worker = backup_scheduler.BackupWorker(lambda run: calls.append(1), instance_id="inst_loop", tick_seconds=0.02)
    worker.start()
    worker.start()  # idempotent
    import time

    time.sleep(0.08)
    worker.stop()
    assert worker._thread is None


def test_worker_module_main_keyboard_interrupt(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module.time, "sleep", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert worker_module.main() == 0


# ── scheduler leftovers ─────────────────────────────────────────────────────


def test_instance_id_from_environment(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_BACKUP_INSTANCE_ID", "inst_env")
    assert backup_scheduler.instance_id_from_environment() == "inst_env"
    monkeypatch.delenv("DEEPSEEK_BACKUP_INSTANCE_ID")
    assert backup_scheduler.instance_id_from_environment().startswith("instance_")


def test_claim_ignores_disabled_and_invalid_cron(tmp_settings: Path) -> None:
    policy = _policy(tmp_settings)
    backup_policies.update_policy(str(policy["policyId"]), {"enabled": False})
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    assert backup_scheduler.claim_due_slots(backup_policies.list_policies(), instance_id="w1", now=now) == []
    raw = backup_policies.get_policy(str(policy["policyId"]))
    raw["enabled"] = True
    raw["schedule"] = {"cron": "not-a-cron", "timezone": "UTC"}
    assert backup_scheduler.claim_due_slots([raw], instance_id="w1", now=now) == []


def test_fail_run_phase_validation(tmp_settings: Path) -> None:
    with pytest.raises(AppError):
        backup_scheduler.fail_run("run_x", error="e", phase="complete")


def test_staging_root_and_cleanup(tmp_settings: Path) -> None:
    root = backup_scheduler.staging_root()
    (root / "run_x").mkdir()
    backup_scheduler.cleanup_run_staging("run_x")
    assert not (root / "run_x").exists()
    assert backup_scheduler.scheduler_thread_name("instance_abcdef") == "backup-worker-instance"


# ── executor edge paths ─────────────────────────────────────────────────────


def test_executor_blocked_target(tmp_settings: Path, stub_crypto: None, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    target = backup_targets.init_target(directory)
    (directory / backup_targets.TARGET_MARKER_NAME).unlink()
    policy = _policy(tmp_settings, targetId=target["targetId"])
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=now)[0]
    outcome = backup_executor.execute_run(run, instance_id="w1", now=now)
    assert outcome["phase"] == "blocked-retryable"
    assert outcome["reason"] == "blocked-target-unavailable"
    assert outcome["retryInSeconds"] > 0
    stored = backup_scheduler.get_run(run.run_id)
    assert stored["phase"] == "blocked-retryable"
    assert stored["nextRetryAt"]


def test_executor_unexpected_exception_fails_run(tmp_settings: Path, stub_crypto: None, monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _policy(tmp_settings)
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=now)[0]
    monkeypatch.setattr(backup_executor.backup_scheduled, "build_scheduled_backup", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    outcome = backup_executor.execute_run(run, instance_id="w1", now=now)
    assert outcome["phase"] == "failed"
    assert "boom" in str(outcome["error"])


def test_executor_publishes_to_filesystem_target(tmp_settings: Path, stub_crypto: None, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    target = backup_targets.init_target(directory)
    policy = _policy(tmp_settings, targetId=target["targetId"])
    now = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=now)[0]
    outcome = backup_executor.execute_run(run, instance_id="w1", now=now)
    assert outcome["phase"] == "complete"
    record = backup_catalog.catalog_state(directory)[str(outcome["backupId"])]
    assert backup_publish.backup_file_candidates(directory, record)[0].is_file()
    assert backup_publish.commit_marker_path(directory, str(policy["policyId"]), run.schedule_slot).is_file()
    assert backup_catalog.verify_chain(directory) is True


# ── retention edge paths ────────────────────────────────────────────────────


def _add(root: Path, backup_id: str, *, created: str, size: int = 10, with_receipt_file: bool = False) -> None:
    (root / "backups").mkdir(parents=True, exist_ok=True)
    (root / "backups" / f"{backup_id}.age").write_bytes(b"x" * size)
    receipt = {"schemaVersion": 1, "backupId": backup_id, "runId": "r", "policyId": "p", "targetId": "managed-local", "scheduleSlot": "s", "filename": f"{backup_id}.age", "size": size, "ciphertextSha256": "a" * 64, "manifestDigest": "b" * 64, "coverageDigest": "c" * 64, "creationVerified": True, "createdAt": created, "pinned": False}
    backup_catalog.append_receipt(root, receipt)
    if with_receipt_file:
        (root / "receipts").mkdir(exist_ok=True)
        (root / "receipts" / f"{backup_id}.age.receipt.json").write_text(json.dumps(receipt), encoding="utf-8")


def test_retention_max_age_and_total_bytes(tmp_settings: Path, tmp_path: Path) -> None:
    root = tmp_path / "target"
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    _add(root, "recent", created="2026-06-14T00:00:00Z", size=100)
    _add(root, "mid", created="2026-06-01T00:00:00Z", size=100)
    _add(root, "old", created="2026-01-01T00:00:00Z", size=100)
    policy = backup_retention.normalize_retention_policy({"keepLast": 0, "keepHourly": 0, "keepDaily": 0, "keepWeekly": 0, "keepMonthly": 0, "minimumHealthyCopies": 1, "maxAgeDays": 7, "maxTotalBytes": 150})
    preview = backup_retention.preview_retention(policy, root, now=now)
    assert "recent" in preview["keep"]
    assert "old" in preview["trash"]
    assert preview["trashRecords"]


def test_retention_restore_reference_scan_handles_corrupt(tmp_settings: Path, tmp_path: Path) -> None:
    restore = backups.RESTORE_DIR / "restore_corrupt"
    restore.mkdir(parents=True)
    (restore / "upload.json").write_text("{not json", encoding="utf-8")
    references = backup_retention._restore_references()
    assert isinstance(references, set)


def test_retention_apply_moves_receipt_and_finalize_keeps_recent(tmp_settings: Path, tmp_path: Path) -> None:
    root = tmp_path / "target"
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    _add(root, "old", created="2026-01-01T00:00:00Z", with_receipt_file=True)
    _add(root, "new", created="2026-06-15T00:00:00Z")
    policy = backup_retention.normalize_retention_policy({"keepLast": 1, "keepHourly": 0, "keepDaily": 0, "keepWeekly": 0, "keepMonthly": 0, "minimumHealthyCopies": 1})
    applied = backup_retention.apply_retention(policy, root, now=now)
    assert applied["trashed"] == ["old"]
    assert (root / ".trash" / "old" / "old.age.receipt.json").is_file()
    late = backup_retention.finalize_retention(policy, root, now=now + timedelta(days=2))
    assert late["deleted"] == ["old"]
    again = backup_retention.finalize_retention(policy, root, now=now + timedelta(days=3))
    assert again["deleted"] == []


def test_retention_finalize_ignores_non_directory_entries(tmp_settings: Path, tmp_path: Path) -> None:
    root = tmp_path / "target"
    (root / ".trash").mkdir(parents=True)
    (root / ".trash" / "stray-file").write_text("x", encoding="utf-8")
    policy = backup_retention.normalize_retention_policy({})
    result = backup_retention.finalize_retention(policy, root, now=datetime(2026, 6, 15, tzinfo=UTC))
    assert result["deleted"] == []


def test_retention_list_skips_corrupt_policy_files(tmp_settings: Path) -> None:
    backup_retention.BACKUP_RETENTION_DIR.mkdir(parents=True, exist_ok=True)
    (backup_retention.BACKUP_RETENTION_DIR / "broken.json").write_text("{not json", encoding="utf-8")
    (backup_retention.BACKUP_RETENTION_DIR / "default.json").write_text(json.dumps({"keepLast": 9}), encoding="utf-8")
    policies = backup_retention.list_retention_policies()
    defaults = [item for item in policies if item["retentionPolicyId"] == "default"]
    assert len(defaults) == 1
    assert defaults[0]["keepLast"] == 9
    with pytest.raises(AppError):
        backup_retention.put_retention_policy("", {})
    with pytest.raises(AppError):
        backup_retention.normalize_retention_policy("not-a-dict")  # type: ignore[arg-type]


def test_retention_preview_handles_missing_created_at(tmp_settings: Path, tmp_path: Path) -> None:
    root = tmp_path / "target"
    _add(root, "nodate", created="not-a-date")
    policy = backup_retention.normalize_retention_policy({"keepLast": 0, "keepHourly": 1, "keepDaily": 1, "keepWeekly": 1, "keepMonthly": 1})
    preview = backup_retention.preview_retention(policy, root, now=datetime(2026, 6, 15, tzinfo=UTC))
    assert "nodate" in preview["keep"]


# ── targets edge paths ──────────────────────────────────────────────────────


def test_installation_id_recreated_when_empty(tmp_settings: Path) -> None:
    path = backup_targets.BACKUP_TARGET_DIR / "installation.id"
    backup_targets.BACKUP_TARGET_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="ascii")
    value = backup_targets.installation_id()
    assert value.startswith("inst_")
    assert path.read_text(encoding="ascii") == value


def test_verify_target_unreadable_and_mismatched_marker(tmp_settings: Path, stub_crypto: None, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    record = backup_targets.init_target(directory)
    marker = directory / backup_targets.TARGET_MARKER_NAME
    marker.write_text("{not json", encoding="utf-8")
    with pytest.raises(AppError, match="unreadable"):
        backup_targets.verify_target_ready(record["targetId"])
    marker.write_text(json.dumps({"schemaVersion": 1, "targetId": "target_other", "targetNonce": "x"}), encoding="utf-8")
    with pytest.raises(AppError, match="replaced"):
        backup_targets.verify_target_ready(record["targetId"])


def test_init_marker_with_invalid_target_id(tmp_settings: Path, stub_crypto: None, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    (directory / backup_targets.TARGET_MARKER_NAME).write_text(json.dumps({"schemaVersion": 1, "targetId": "bad id", "targetNonce": "x"}), encoding="utf-8")
    with pytest.raises(AppError, match="invalid target id"):
        backup_targets.init_target(directory)


def test_reparse_point_on_plain_file(tmp_settings: Path, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.write_text("x", encoding="utf-8")
    assert backup_targets._is_reparse_point(plain) is False


# ── mirror edge paths ───────────────────────────────────────────────────────


def test_mirror_metadata_unreadable(tmp_settings: Path) -> None:
    profile = backup_mirror.BACKUP_MIRROR_DIR / "mirror_x"
    profile.mkdir(parents=True)
    (profile / "frontend-state.meta.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(AppError, match="unreadable"):
        backup_mirror.mirror_status("mirror_x")
    (profile / "frontend-state.meta.json").write_text("[]", encoding="utf-8")
    assert backup_mirror.mirror_status("mirror_x")["status"] == "missing"


def test_mirror_listing_skips_corrupt_profiles(tmp_settings: Path, stub_crypto: None) -> None:
    backup_mirror.put_frontend_mirror("mirror_ok", {"schemaVersion": 1, "sourceVersion": "4.4.4", "createdAt": 1, "conversations": [], "conflicts": [], "digest": hashlib.sha256(json.dumps({"schemaVersion": 1, "sourceVersion": "4.4.4", "createdAt": 1, "conversations": [], "conflicts": []}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}, source_epoch="epoch-1", recipients=[RECIPIENT_A])
    corrupt = backup_mirror.BACKUP_MIRROR_DIR / "mirror_bad"
    corrupt.mkdir()
    (corrupt / "frontend-state.meta.json").write_text("{not json", encoding="utf-8")
    assert [item["profileId"] for item in backup_mirror.list_mirrors()] == ["mirror_ok"]


def test_mirror_files_requires_ciphertext(tmp_settings: Path) -> None:
    profile = backup_mirror.BACKUP_MIRROR_DIR / "mirror_x"
    profile.mkdir(parents=True)
    (profile / "frontend-state.meta.json").write_text(json.dumps({"profileId": "mirror_x"}), encoding="utf-8")
    with pytest.raises(AppError):
        backup_mirror.mirror_files("mirror_x")


def test_mirror_status_without_max_age_and_bad_ack(tmp_settings: Path, stub_crypto: None) -> None:
    envelope = {"schemaVersion": 1, "sourceVersion": "4.4.4", "createdAt": 1, "conversations": [], "conflicts": []}
    envelope["digest"] = hashlib.sha256(json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    backup_mirror.put_frontend_mirror("mirror_x", envelope, source_epoch="epoch-1", recipients=[RECIPIENT_A])
    assert backup_mirror.mirror_status("mirror_x", max_age_seconds=None)["status"] == "current"
    head = json.loads((backup_mirror.BACKUP_MIRROR_DIR / "mirror_x" / "HEAD.json").read_text(encoding="utf-8"))
    meta = backup_mirror.BACKUP_MIRROR_DIR / "mirror_x" / "generations" / str(head["generationId"]) / "metadata.json"
    payload = json.loads(meta.read_text(encoding="utf-8"))
    payload["acknowledgedAt"] = "not-a-time"
    meta.write_text(json.dumps(payload), encoding="utf-8")
    assert backup_mirror.mirror_status("mirror_x", max_age_seconds=60)["status"] == "stale"


# ── scheduled build edge paths ──────────────────────────────────────────────


def test_age_seconds_handles_invalid_timestamp() -> None:
    assert backup_scheduled._age_seconds("not-a-time") == -1
    assert backup_scheduled._age_seconds("2026-01-01T00:00:00Z", now=datetime(2026, 1, 2, tzinfo=UTC)) == 86400


def test_scheduled_cancel_aborts_before_build(tmp_settings: Path, stub_crypto: None) -> None:
    import threading

    policy = _policy(tmp_settings)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(AppError) as cancelled:
        backup_scheduled.build_scheduled_backup(policy, run_id="run_cancel", staging_root=tmp_settings / ".staging", cancel_event=cancel)
    assert cancelled.value.status == 499


def test_scheduled_quiesce_exhaustion(tmp_settings: Path, stub_crypto: None, monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _policy(tmp_settings)
    generations = iter(range(10))
    monkeypatch.setattr(backup_scheduled.mutation_gate, "read_generation", lambda _root: next(generations) % 2)
    with pytest.raises(AppError, match="quieter|retry"):
        backup_scheduled.build_scheduled_backup(policy, run_id="run_busy", staging_root=tmp_settings / ".staging")


# ── scrub edge paths ────────────────────────────────────────────────────────


def test_scrub_invalid_age_header(tmp_settings: Path, stub_crypto: None, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_settings / "starget"
    _add(root, "b1", created="2026-06-01T00:00:00Z")
    monkeypatch.setattr(backup_crypto, "inspect_header", lambda _path: {"age": False})
    result = backup_scrub.scrub_backup(root, "b1")
    assert result["ok"] is False
    assert "FAIL" in result["checks"]["age-header"]
    monkeypatch.setattr(backup_crypto, "inspect_header", lambda _path: (_ for _ in ()).throw(AppError("bad header")))
    result = backup_scrub.scrub_backup(root, "b1")
    assert result["ok"] is False


def test_drill_missing_file_and_health_bad_date(tmp_settings: Path, stub_crypto: None) -> None:
    root = tmp_settings / "starget2"
    _add(root, "b1", created="2026-06-01T00:00:00Z")
    (root / "backups" / "b1.age").unlink()
    with pytest.raises(AppError, match="missing"):
        backup_scrub.verify_unlock_drill(root, "b1", bytearray(b"x"), staged_root=tmp_settings / ".drill")
    backup_catalog.record_unlock_verification(root, "b1")
    state_path = backup_catalog.catalog_path(root)
    lines = state_path.read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    last["payload"]["userUnlockVerifiedAt"] = "not-a-time"
    lines[-1] = json.dumps(last)
    state_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    health = backup_scrub.backup_health(root)
    assert health["backups"][0]["issues"] == ["unlock-verification-missing"]


def test_publish_oserror_becomes_blocked(tmp_settings: Path, stub_crypto: None, monkeypatch: pytest.MonkeyPatch) -> None:
    target = backup_publish.resolve_target("managed-local")

    class _Package:
        path = tmp_settings / "staging" / "p.age"
        backup_id = "b1"
        filename = "f.age"
        size = 1
        ciphertext_sha256 = "a" * 64
        manifest_digest = "b" * 64
        coverage_digest = "c" * 64
        creation_verified = True

    _Package.path.parent.mkdir(parents=True, exist_ok=True)
    _Package.path.write_bytes(b"x")
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(AppError, match="blocked-target-unavailable"):
        backup_publish.publish_backup(target, _Package(), run_id="run_e", policy_id="p", schedule_slot="s", fencing_token=1)

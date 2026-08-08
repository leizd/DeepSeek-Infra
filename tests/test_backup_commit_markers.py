from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_crypto,
    backup_executor,
    backup_policies,
    backup_publish,
    backup_retention,
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


class _Package:
    def __init__(self, path: Path, payload: bytes, backup_id: str = "backup_test1") -> None:
        self.path = path
        self.backup_id = backup_id
        self.filename = f"deepseek-infra-backup-20260101-{backup_id[-8:]}.dsibackup.age"
        self.size = len(payload)
        self.ciphertext_sha256 = hashlib.sha256(payload).hexdigest()
        self.manifest_digest = "a" * 64
        self.coverage_digest = "b" * 64
        self.creation_verified = True


def _package(tmp_path: Path, payload: bytes = b"ciphertext-bytes", backup_id: str = "backup_test1") -> _Package:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    path = staging / f"package-{backup_id}.age"
    path.write_bytes(payload)
    return _Package(path, payload, backup_id)


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


def _seed_workspace() -> None:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('{"items":[]}', encoding="utf-8")


def _clock_box(value: datetime = NOW) -> list[datetime]:
    return [value]


def test_objects_are_deduplicated_across_slots(tmp_settings: Path, tmp_path: Path) -> None:
    target = backup_publish.resolve_target("managed-local")
    root = backups.BACKUP_DIR
    first = backup_publish.publish_backup(target, _package(tmp_path, backup_id="backup_a1"), run_id="run_1", policy_id="policy_1", schedule_slot="slot-a", fencing_token=1)
    second = backup_publish.publish_backup(target, _package(tmp_path, backup_id="backup_b2"), run_id="run_2", policy_id="policy_1", schedule_slot="slot-b", fencing_token=2)
    objects = list((root / "objects" / "sha256").rglob("*.age"))
    assert len(objects) == 1
    assert first.commit["objectDigest"] == second.commit["objectDigest"]
    assert second.commit["targetGeneration"] == 2
    assert second.commit["previousCommitHash"] == first.commit["commitHash"]
    assert (root / "receipts" / "backup_a1.json").is_file()
    assert (root / "receipts" / "backup_b2.json").is_file()


def test_same_slot_same_digest_converges(tmp_settings: Path, tmp_path: Path) -> None:
    target = backup_publish.resolve_target("managed-local")
    root = backups.BACKUP_DIR
    package = _package(tmp_path)
    first = backup_publish.publish_backup(target, package, run_id="run_1", policy_id="policy_1", schedule_slot="slot", fencing_token=1)
    second = backup_publish.publish_backup(target, package, run_id="run_2", policy_id="policy_1", schedule_slot="slot", fencing_token=2)
    assert first.converged is False
    assert second.converged is True
    assert second.commit["runId"] == "run_1"
    assert second.receipt["backupId"] == "backup_test1"
    markers = backup_publish.read_commit_markers(root)
    assert len(markers) == 1
    journal = backup_publish.read_journal(root, "run_2")
    assert journal is not None
    assert journal["phase"] == "converged"
    assert journal["convergedToRunId"] == "run_1"


def test_same_slot_different_digest_conflicts(tmp_settings: Path, tmp_path: Path) -> None:
    target = backup_publish.resolve_target("managed-local")
    root = backups.BACKUP_DIR
    backup_publish.publish_backup(target, _package(tmp_path, payload=b"payload-a", backup_id="backup_a1"), run_id="run_1", policy_id="policy_1", schedule_slot="slot", fencing_token=1)
    with pytest.raises(AppError) as exc:
        backup_publish.publish_backup(target, _package(tmp_path, payload=b"payload-b", backup_id="backup_b2"), run_id="run_2", policy_id="policy_1", schedule_slot="slot", fencing_token=2)
    assert exc.value.status == 409
    assert "slot-commit-conflict" in str(exc.value)
    marker = json.loads(backup_publish.commit_marker_path(root, "policy_1", "slot").read_text(encoding="utf-8"))
    assert marker["objectDigest"] == hashlib.sha256(b"payload-a").hexdigest()
    journal = backup_publish.read_journal(root, "run_2")
    assert journal is not None
    assert journal["phase"] == "slot-commit-conflict"


def test_stale_fencing_token_is_named_in_conflict(tmp_settings: Path, tmp_path: Path) -> None:
    target = backup_publish.resolve_target("managed-local")
    backup_publish.publish_backup(target, _package(tmp_path, payload=b"payload-a", backup_id="backup_a1"), run_id="run_1", policy_id="policy_1", schedule_slot="slot", fencing_token=5)
    with pytest.raises(AppError) as exc:
        backup_publish.publish_backup(target, _package(tmp_path, payload=b"payload-b", backup_id="backup_b2"), run_id="run_2", policy_id="policy_1", schedule_slot="slot", fencing_token=3)
    assert "stale-fencing-token" in str(exc.value)


def test_executor_converges_when_marker_survives_crash(tmp_settings: Path, stub_crypto: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_workspace()
    policy = _policy(tmp_settings)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    box = _clock_box()
    original_publish = backup_publish.publish_backup
    stashed: dict[str, object] = {}

    def publish_then_expire(*args: object, **kwargs: object) -> object:
        package = args[1]
        stashed["bytes"] = package.path.read_bytes()  # type: ignore[attr-defined]
        stashed["fields"] = {
            "backup_id": package.backup_id,  # type: ignore[attr-defined]
            "filename": package.filename,  # type: ignore[attr-defined]
            "size": package.size,  # type: ignore[attr-defined]
            "ciphertext_sha256": package.ciphertext_sha256,  # type: ignore[attr-defined]
            "manifest_digest": package.manifest_digest,  # type: ignore[attr-defined]
            "coverage_digest": package.coverage_digest,  # type: ignore[attr-defined]
            "creation_verified": package.creation_verified,  # type: ignore[attr-defined]
            "frontend": package.frontend,  # type: ignore[attr-defined]
            "coverage": package.coverage,  # type: ignore[attr-defined]
            "manifest": package.manifest,  # type: ignore[attr-defined]
        }
        result = original_publish(*args, **kwargs)  # type: ignore[arg-type]
        box[0] = NOW + timedelta(seconds=400)
        return result

    monkeypatch.setattr(backup_executor.backup_publish, "publish_backup", publish_then_expire)
    crashed = backup_executor.execute_run(run, instance_id="w1", now=NOW, clock=lambda: box[0])
    assert crashed["phase"] == "abandoned"
    assert backup_catalog.catalog_state(backups.BACKUP_DIR) == {}
    markers = backup_publish.read_commit_markers(backups.BACKUP_DIR)
    assert len(markers) == 1
    monkeypatch.setattr(backup_executor.backup_publish, "publish_backup", original_publish)

    def rebuild_package(
        _policy: object,
        *,
        run_id: str,
        staging_root: Path,
        schedule_slot: str = "",
        cancel_event: object = None,
        backup_id: object = None,
        contributor_plan: object = None,
        snapshot_kind: object = None,
        parent_backup_id: object = None,
        base_backup_id: object = None,
    ) -> object:
        del backup_id, contributor_plan, snapshot_kind, parent_backup_id, base_backup_id
        run_dir = staging_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        fields = dict(cast(dict[str, Any], stashed["fields"]))
        target = run_dir / str(fields["filename"])
        target.write_bytes(cast(bytes, stashed["bytes"]))
        return SimpleNamespace(path=target, **fields)

    monkeypatch.setattr(backup_executor.backup_scheduled, "build_scheduled_backup", rebuild_package)
    reclaimed = backup_scheduler.reclaim_abandoned_slots(instance_id="w2", now=NOW + timedelta(seconds=400))
    assert len(reclaimed) == 1
    takeover = backup_executor.execute_run(reclaimed[0], instance_id="w2", now=NOW + timedelta(seconds=400))
    assert takeover["phase"] == "complete", takeover
    assert takeover["backupId"] == cast(dict[str, Any], stashed["fields"])["backup_id"]
    state = backup_catalog.catalog_state(backups.BACKUP_DIR)
    assert len(state) == 1
    assert str(next(iter(state.values())).get("runId")) == run.run_id
    assert len(backup_publish.read_commit_markers(backups.BACKUP_DIR)) == 1
    assert len(list((backups.BACKUP_DIR / "objects" / "sha256").rglob("*.age"))) == 1


def test_executor_slot_commit_conflict_fails_without_overwrite(tmp_settings: Path, stub_crypto: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_workspace()
    policy = _policy(tmp_settings)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    box = _clock_box()
    original_complete = backup_scheduler.complete_run

    def complete_then_expire(run_id: str, **kwargs: object) -> None:
        box[0] = NOW + timedelta(seconds=400)
        kwargs["now"] = box[0]
        original_complete(run_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backup_executor.backup_scheduler, "complete_run", complete_then_expire)
    first = backup_executor.execute_run(run, instance_id="w1", now=NOW, clock=lambda: box[0])
    assert first["phase"] == "abandoned"
    monkeypatch.setattr(backup_executor.backup_scheduler, "complete_run", original_complete)
    markers = backup_publish.read_commit_markers(backups.BACKUP_DIR)
    assert len(markers) == 1
    committed_digest = str(markers[0]["objectDigest"])
    config.MEMORY_FILE.write_text('{"items":[{"id":"m1","text":"changed"}]}', encoding="utf-8")
    reclaimed = backup_scheduler.reclaim_abandoned_slots(instance_id="w2", now=NOW + timedelta(seconds=400))
    assert len(reclaimed) == 1
    outcome = backup_executor.execute_run(reclaimed[0], instance_id="w2", now=NOW + timedelta(seconds=400))
    assert outcome["phase"] == "superseded"
    assert "slot-commit-conflict" in str(outcome["error"])
    assert backup_scheduler.get_run(str(outcome["runId"]))["phase"] == "superseded"
    marker_after = json.loads(backup_publish.commit_marker_path(backups.BACKUP_DIR, str(policy["policyId"]), run.schedule_slot).read_text(encoding="utf-8"))
    assert marker_after["objectDigest"] == committed_digest
    state = backup_catalog.catalog_state(backups.BACKUP_DIR)
    assert len(state) == 1


def test_expired_worker_leaves_only_invisible_orphans(tmp_settings: Path, stub_crypto: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_workspace()
    policy = _policy(tmp_settings)
    run = backup_scheduler.claim_due_slots([policy], instance_id="w1", now=NOW)[0]
    box = _clock_box()
    original_write_immutable = backup_publish._write_immutable

    def receipt_then_expire(path: Path, content: bytes) -> None:
        original_write_immutable(path, content)
        box[0] = NOW + timedelta(seconds=400)

    monkeypatch.setattr(backup_publish, "_write_immutable", receipt_then_expire)
    outcome = backup_executor.execute_run(run, instance_id="w1", now=NOW, clock=lambda: box[0])
    assert outcome["phase"] == "abandoned"
    assert "lease" in str(outcome["error"]).casefold()
    root = backups.BACKUP_DIR
    assert backup_publish.read_commit_markers(root) == []
    assert backup_catalog.catalog_state(root) == {}
    assert len(list((root / "receipts").glob("*.json"))) == 1
    assert len(list((root / "objects" / "sha256").rglob("*.age"))) == 1
    assert backup_catalog.find_orphans_and_missing(root) == {"orphans": [], "missing": []}
    journal = backup_publish.read_journal(root, run.run_id)
    assert journal is not None
    assert journal["phase"] == "receipt-published"


def test_marker_create_race_converges(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = backup_publish.resolve_target("managed-local")
    root = backups.BACKUP_DIR
    package = _package(tmp_path, backup_id="backup_w1")
    rival = _package(tmp_path, backup_id="backup_w2")
    backup_publish.publish_backup(target, package, run_id="run_1", policy_id="policy_1", schedule_slot="slot", fencing_token=1)
    marker_path = backup_publish.commit_marker_path(root, "policy_1", "slot")
    real_is_file = Path.is_file

    def precheck_misses_marker(self: Path) -> bool:
        if self == marker_path:
            return False
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", precheck_misses_marker)
    second = backup_publish.publish_backup(target, rival, run_id="run_2", policy_id="policy_1", schedule_slot="slot", fencing_token=2)
    assert second.converged is True
    assert second.commit["runId"] == "run_1"
    assert second.receipt["backupId"] == "backup_w1"


def test_existing_corrupt_object_fails_digest(tmp_settings: Path, tmp_path: Path) -> None:
    target = backup_publish.resolve_target("managed-local")
    root = backups.BACKUP_DIR
    package = _package(tmp_path)
    obj = backup_publish.object_path(root, package.ciphertext_sha256)
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_bytes(b"tampered")
    with pytest.raises(AppError) as exc:
        backup_publish.publish_backup(target, package, run_id="run_9", policy_id="policy_1", schedule_slot="slot-9", fencing_token=1)
    assert exc.value.status == 500
    assert "fails its digest" in str(exc.value)


def test_write_immutable_idempotent_and_rejects_divergence(tmp_path: Path) -> None:
    path = tmp_path / "receipts" / "b.json"
    backup_publish._write_immutable(path, b"one")
    backup_publish._write_immutable(path, b"one")
    assert path.read_bytes() == b"one"
    with pytest.raises(AppError) as exc:
        backup_publish._write_immutable(path, b"two")
    assert exc.value.status == 409


def test_create_exclusive_only_first_wins(tmp_path: Path) -> None:
    path = tmp_path / "commits" / "p" / "s.json"
    assert backup_publish._create_exclusive(path, b"a") is True
    assert backup_publish._create_exclusive(path, b"b") is False
    assert path.read_bytes() == b"a"


def test_read_journal_missing_corrupt_and_roundtrip(tmp_path: Path) -> None:
    assert backup_publish.read_journal(tmp_path, "run_x") is None
    backup_publish._write_journal(tmp_path, {"runId": "run_y", "phase": "started"})
    journal = backup_publish.read_journal(tmp_path, "run_y")
    assert journal is not None and journal["phase"] == "started"
    (tmp_path / "transactions" / "run_z.json").write_text("{nope", encoding="utf-8")
    assert backup_publish.read_journal(tmp_path, "run_z") is None


def test_read_commit_markers_skips_corrupt(tmp_path: Path) -> None:
    assert backup_publish.read_commit_markers(tmp_path) == []
    commits = tmp_path / "commits" / "policy_1"
    commits.mkdir(parents=True)
    (commits / "bad.json").write_text("{nope", encoding="utf-8")
    (commits / "no-hash.json").write_text("{}", encoding="utf-8")
    assert backup_publish.read_commit_markers(tmp_path) == []
    (commits / "ok.json").write_text(json.dumps({"commitHash": "a" * 64, "targetGeneration": 3}), encoding="utf-8")
    markers = backup_publish.read_commit_markers(tmp_path)
    assert len(markers) == 1
    latest = backup_publish.latest_commit(tmp_path)
    assert latest is not None and latest["targetGeneration"] == 3


def test_retention_trash_and_restore_roundtrip_new_layout(tmp_settings: Path, tmp_path: Path) -> None:
    target = backup_publish.resolve_target("managed-local")
    root = backups.BACKUP_DIR
    older = backup_publish.publish_backup(target, _package(tmp_path, payload=b"payload-old", backup_id="backup_old"), run_id="run_1", policy_id="policy_1", schedule_slot="slot-a", fencing_token=1)
    backup_catalog.append_receipt(root, {**older.receipt, "createdAt": "2026-01-01T00:00:00Z"})
    newer = backup_publish.publish_backup(target, _package(tmp_path, payload=b"payload-new", backup_id="backup_new"), run_id="run_2", policy_id="policy_1", schedule_slot="slot-b", fencing_token=2)
    backup_catalog.append_receipt(root, {**newer.receipt, "createdAt": "2026-06-01T00:00:00Z"})
    retention = backup_retention.normalize_retention_policy({"keepLast": 0, "keepHourly": 0, "keepDaily": 0, "keepWeekly": 0, "keepMonthly": 0, "minimumHealthyCopies": 1})
    applied = backup_retention.apply_retention(retention, root)
    assert applied["trashed"] == ["backup_old"]
    assert not backup_publish.object_path(root, older.receipt["objectDigest"]).exists()
    assert (root / ".trash" / "backup_old" / f"{older.receipt['objectDigest']}.age").is_file()
    assert (root / ".trash" / "backup_old" / "backup_old.json").is_file()
    restored = backup_retention.restore_from_trash(root, "backup_old")
    assert restored["restored"] is True
    assert backup_publish.object_path(root, older.receipt["objectDigest"]).is_file()
    assert (root / "receipts" / "backup_old.json").is_file()

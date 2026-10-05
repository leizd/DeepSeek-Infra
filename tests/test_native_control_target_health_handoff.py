"""An isolated target transfer includes the scheduler's real health history."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import backup_control, backup_control_authority, backup_scheduler
from scripts import native_control_handoff as handoff


@pytest.fixture
def health_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, Path]:
    control_dir = tmp_path / ".backup-control"
    control_db = control_dir / "control.sqlite3"
    scheduler_dir = tmp_path / ".backup-scheduler"
    monkeypatch.setattr(backup_control, "CONTROL_DIR", control_dir)
    monkeypatch.setattr(backup_control, "CONTROL_DB", control_db)
    monkeypatch.setattr(backup_scheduler, "BACKUP_SCHEDULER_DIR", scheduler_dir)
    monkeypatch.setattr(backup_scheduler, "_utc_iso", lambda: "2026-09-30T01:02:03Z")
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_authoritative")
    monkeypatch.delenv("DEEPSEEK_GO_CONTROL", raising=False)
    backup_control_authority.configure_authority_anchor_roots(None)
    backup_control_authority.configure_authority_anchor_stores(None)
    backup_control.upsert_target({"targetId": "t-1", "kind": "s3", "topologyGeneration": 1})
    backup_scheduler.record_target_health("t-1", "blocked", "provider timeout")
    # Historical rows for a removed target are still returned by Python's API.
    backup_scheduler.record_target_health("old-target", "healthy")
    checkpoint = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority.record_local_authority_head(checkpoint)
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    return control_db, scheduler_dir / "scheduler.db", checkpoint_path, tmp_path / "target-export.json"


def export_health(source: tuple[Path, Path, Path, Path], transfer: str = "target-health-transfer") -> dict[str, Any]:
    control_db, scheduler_db, checkpoint, output = source
    return handoff.export_and_fence(control_db, checkpoint, output, "target", transfer, scheduler_db=scheduler_db)


@pytest.mark.parametrize("fault", ["blob", "foreign-schema", "dot-id"])
def test_target_health_refuses_changed_source_types_without_committing_a_fence(
    health_source: tuple[Path, Path, Path, Path], fault: str,
) -> None:
    with sqlite3.connect(health_source[1]) as connection:
        if fault == "blob":
            connection.execute("UPDATE backup_target_health SET status=? WHERE target_id='t-1'", (b"blocked",))
        elif fault == "dot-id":
            connection.execute("UPDATE backup_target_health SET target_id='.' WHERE target_id='t-1'")
        else:
            connection.execute("ALTER TABLE backup_target_health RENAME TO old_health")
            connection.execute(
                "CREATE TABLE backup_target_health(target_id BLOB PRIMARY KEY,status TEXT NOT NULL,checked_at TEXT NOT NULL,detail TEXT)"
            )
            connection.execute("INSERT INTO backup_target_health SELECT * FROM old_health")
            connection.execute("DROP TABLE old_health")
    with pytest.raises(handoff.HandoffError):
        export_health(health_source)
    assert not health_source[3].exists()
    for path, table in [(health_source[0], "native_control_handoff_fences"), (health_source[1], "native_target_health_handoff_fence")]:
        with sqlite3.connect(path) as connection:
            assert not connection.execute("SELECT 1 FROM sqlite_schema WHERE name=?", (table,)).fetchone()


def test_target_export_binds_nonempty_health_and_denies_restarted_python_writer(
    health_source: tuple[Path, Path, Path, Path],
) -> None:
    manifest = export_health(health_source)
    assert manifest["schema"] == "python-control-inventory-export-v2"
    assert manifest["targetHealth"]["rows"] == [
        {"target_id": "old-target", "status": "healthy", "checked_at": "2026-09-30T01:02:03Z", "detail": None},
        {"target_id": "t-1", "status": "blocked", "checked_at": "2026-09-30T01:02:03Z", "detail": "provider timeout"},
    ]
    assert handoff._read_manifest(health_source[3]) == manifest
    assert export_health(health_source) == manifest
    with pytest.raises(sqlite3.IntegrityError, match="NATIVE_TARGET_HEALTH_HANDOFF"):
        backup_scheduler.record_target_health("t-1", "healthy")
    script = (
        "from pathlib import Path; "
        "from deepseek_infra.infra.workspace import backup_scheduler as s; "
        f"s.BACKUP_SCHEDULER_DIR=Path({health_source[1].parent.as_posix()!r}); "
        "s.record_target_health('t-1','healthy')"
    )
    child = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert child.returncode != 0 and "NATIVE_TARGET_HEALTH_HANDOFF" in child.stderr
    assert backup_scheduler.target_health()[1]["status"] == "blocked"


def test_health_export_refuses_missing_source_before_control_fence(
    health_source: tuple[Path, Path, Path, Path],
) -> None:
    database, _scheduler, checkpoint, output = health_source
    with pytest.raises(handoff.HandoffError, match="scheduler"):
        handoff.export_and_fence(database, checkpoint, output, "target", "missing-source", scheduler_db=output.parent / "missing.db")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM sqlite_schema WHERE name='native_control_handoff_fences'").fetchone()[0] == 0
    assert not output.exists()


def test_health_export_recovers_output_failure_and_refuses_different_transfer(
    health_source: tuple[Path, Path, Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish = handoff._publish
    monkeypatch.setattr(handoff, "_publish", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        export_health(health_source)
    with pytest.raises(sqlite3.IntegrityError, match="NATIVE_TARGET_HEALTH_HANDOFF"):
        backup_scheduler.record_target_health("t-1", "healthy")
    monkeypatch.setattr(handoff, "_publish", publish)
    manifest = export_health(health_source)
    assert manifest["targetHealth"]["rows"][1]["status"] == "blocked"
    with pytest.raises(handoff.HandoffError, match="fenced"):
        export_health(health_source, "different-transfer")


def test_health_retry_refuses_lost_guard_and_preserves_existing_fences(
    health_source: tuple[Path, Path, Path, Path],
) -> None:
    export_health(health_source)
    with sqlite3.connect(health_source[1]) as connection:
        connection.execute("DROP TRIGGER native_target_health_no_update")
    with pytest.raises(handoff.HandoffError, match="fence"):
        export_health(health_source)
    with sqlite3.connect(health_source[0]) as connection:
        assert connection.execute("SELECT transfer_id FROM native_control_handoff_fences WHERE domain='target'").fetchone()[0] == "target-health-transfer"


def copied_health_handback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, Path, Path]:
    fixtures = Path(__file__).resolve().parents[1] / "go/internal/store/testdata/target-health-v1"
    control_dir, scheduler_dir = tmp_path / ".backup-control", tmp_path / ".backup-scheduler"
    control_dir.mkdir()
    scheduler_dir.mkdir()
    database, scheduler = control_dir / "control.sqlite3", scheduler_dir / "scheduler.db"
    database.write_bytes((fixtures / "python_control_source_v1.sqlite3").read_bytes())
    scheduler.write_bytes((fixtures / "python_scheduler_source_v1.sqlite3").read_bytes())
    monkeypatch.setattr(backup_scheduler, "BACKUP_SCHEDULER_DIR", scheduler_dir)
    monkeypatch.setattr(backup_control, "CONTROL_DIR", control_dir)
    monkeypatch.setattr(backup_control, "CONTROL_DB", database)
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_authoritative")
    monkeypatch.delenv("DEEPSEEK_GO_CONTROL", raising=False)
    return database, scheduler, fixtures / "python_target_inventory_export_v2.json", fixtures / "go_target_inventory_handback_v1.json", tmp_path / "receipt.json"


def test_go_health_handback_releases_real_python_writer_and_can_republish_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, scheduler, manifest, proof, receipt = copied_health_handback(tmp_path, monkeypatch)
    with pytest.raises(sqlite3.IntegrityError, match="NATIVE_TARGET_HEALTH_HANDOFF"):
        backup_scheduler.record_target_health("t-1", "healthy")
    document = handoff.revoke_handoff(database, manifest, proof, receipt, "fixture-target", scheduler_db=scheduler)
    backup_scheduler.record_target_health("t-1", "healthy", "recovered by Python")
    assert backup_scheduler.target_health()[1]["status"] == "healthy"
    assert handoff.revoke_handoff(database, manifest, proof, receipt, "fixture-target", scheduler_db=scheduler) == document
    assert json.loads(receipt.read_text(encoding="utf-8")) == document


def test_health_handback_recovers_after_scheduler_commit_and_control_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, scheduler, manifest, proof, receipt = copied_health_handback(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        connection.execute(handoff._REVOCATION_TABLE)
        for statement in handoff._REVOCATION_TRIGGERS.values():
            connection.execute(statement)
        connection.execute("CREATE TRIGGER inject_control_handback_failure BEFORE INSERT ON native_control_handoff_revocations BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(handoff.HandoffError):
        handoff.revoke_handoff(database, manifest, proof, receipt, "fixture-target", scheduler_db=scheduler)
    # Go's copy was already abandoned; a scheduler-side release does not grant
    # Go another writer. The target inventory must still be fenced until retry.
    backup_scheduler.record_target_health("t-1", "healthy")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM native_control_handoff_fences WHERE domain='target'").fetchone()[0] == 1
        connection.execute("DROP TRIGGER inject_control_handback_failure")
    document = handoff.revoke_handoff(database, manifest, proof, receipt, "fixture-target", scheduler_db=scheduler)
    assert document["transferId"] == "fixture-target"


def test_health_export_never_overwrites_the_scheduler_database(
    health_source: tuple[Path, Path, Path, Path],
) -> None:
    database, scheduler, checkpoint, _output = health_source
    before = scheduler.read_bytes()
    with pytest.raises(handoff.HandoffError, match="output"):
        handoff.export_and_fence(database, checkpoint, scheduler, "target", "bad-output", scheduler_db=scheduler)
    assert scheduler.read_bytes() == before
    with sqlite3.connect(scheduler) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sqlite_schema WHERE name='native_target_health_handoff_fence'").fetchone()[0] == 0

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_writer_lease as wl

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_LEASE_SRC = REPO_ROOT / "rust/crates/deepseek-policy/src/backup_writer_lease.rs"


def _utc(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 9, 5, hour, minute, second, tzinfo=timezone.utc)


def test_filesystem_writer_lease_oracle_contract_and_rust_parity(tmp_path: Path) -> None:
    wl.probe_atomic_target(tmp_path)
    assert not wl.active_writer_lease(SimpleNamespace(root=tmp_path, store=None), now=_utc(15, 0))

    clock_box = [_utc(15, 0, 0)]
    lease = wl.TargetWriterLease(
        tmp_path,
        target_id="managed-local",
        owner_run_id="run-1",
        owner_instance_id="inst-1",
        fencing_token=5,
        lease_seconds=60,
        clock=lambda: clock_box[0],
    )
    lease.acquire()
    raw = lease.path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert raw == json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    assert parsed["schemaVersion"] == 1
    assert parsed["acquiredAt"] == "2026-09-05T15:00:00Z"
    assert parsed["expiresAt"] == "2026-09-05T15:01:00Z"
    assert wl.active_writer_lease(SimpleNamespace(root=tmp_path, store=None), now=_utc(15, 0, 30))

    # Contention while active -> 423 busy
    contender = wl.TargetWriterLease(
        tmp_path,
        target_id="managed-local",
        owner_run_id="run-2",
        owner_instance_id="inst-2",
        fencing_token=99,
        clock=lambda: clock_box[0],
    )
    with pytest.raises(AppError) as busy_exc:
        contender.acquire()
    assert busy_exc.value.status == 423
    assert "Target writer is busy with another run" in str(busy_exc.value)

    # Same-run resumption with strictly higher token preempts immediately
    resumed = wl.TargetWriterLease(
        tmp_path,
        target_id="managed-local",
        owner_run_id="run-1",
        owner_instance_id="inst-2",
        fencing_token=6,
        lease_seconds=60,
        clock=lambda: clock_box[0],
    )
    resumed.acquire()
    with pytest.raises(AppError) as lost_exc:
        lease.assert_owned()
    assert lost_exc.value.status == 409
    assert "Target writer lease lost to another writer" in str(lost_exc.value)

    # Preempted release must not remove the winner's writer.json
    lease.release()
    assert resumed.path.is_file()

    # Expiry preemption requires strictly higher fencing token
    clock_box[0] = _utc(15, 5, 0)
    assert not wl.active_writer_lease(SimpleNamespace(root=tmp_path, store=None), now=clock_box[0])
    with pytest.raises(AppError) as equal_exc:
        wl.TargetWriterLease(
            tmp_path,
            target_id="managed-local",
            owner_run_id="run-3",
            owner_instance_id="inst-3",
            fencing_token=6,
            clock=lambda: clock_box[0],
        ).acquire()
    assert equal_exc.value.status == 423
    assert "Target writer is held by a newer or equal fencing token" in str(equal_exc.value)

    winner = wl.TargetWriterLease(
        tmp_path,
        target_id="managed-local",
        owner_run_id="run-3",
        owner_instance_id="inst-3",
        fencing_token=7,
        clock=lambda: clock_box[0],
    )
    winner.acquire()
    clock_box[0] += timedelta(seconds=10)
    winner.renew()
    winner.assert_owned()
    winner.release()
    assert not winner.path.exists()


def test_corrupt_or_missing_expiry_counts_as_active_and_blocks_naive_takeover(tmp_path: Path) -> None:
    lock = tmp_path / ".target-lock"
    lock.mkdir(parents=True)
    writer = lock / "writer.json"
    writer.write_text("{corrupt", encoding="utf-8")
    assert wl.active_writer_lease(SimpleNamespace(root=tmp_path, store=None), now=_utc(15, 0))

    writer.write_text(json.dumps({"ownerRunId": "r1"}), encoding="utf-8")
    assert wl.active_writer_lease(SimpleNamespace(root=tmp_path, store=None), now=_utc(15, 0))

    with pytest.raises(AppError) as missing_exc:
        wl.TargetWriterLease(
            tmp_path,
            target_id="managed-local",
            owner_run_id="r1",
            owner_instance_id="i1",
            fencing_token=2,
            clock=lambda: _utc(15, 0),
        ).assert_owned()
    assert missing_exc.value.status == 409


def test_rust_backup_writer_lease_matches_python_contract_symbols() -> None:
    source = RUST_LEASE_SRC.read_text(encoding="utf-8")
    assert f"pub const TARGET_WRITER_LEASE_SECONDS: i64 = {wl.TARGET_WRITER_LEASE_SECONDS};" in source
    for required_message in (
        "Target writer is busy with another run",
        "Target writer is held by a newer or equal fencing token",
        "Target writer lease lost: writer.json is missing or unreadable",
        "Target writer lease lost to another writer",
        "Target writer lease expired",
        "unsupported-atomic-target",
    ):
        assert required_message in source

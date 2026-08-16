"""Unit tests for Autonomous Recovery Lease Keeper (Recovery Assurance Gate A)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import (
    backup_recovery_keeper,
    backup_recovery_lease,
)


def _create_fake_session(
    staging_dir: Path,
    restore_id: str,
    phase: str,
    target_id: str = "target_test",
    holds: list[dict[str, Any]] | None = None,
) -> Path:
    session_dir = staging_dir / restore_id
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / "remote-fetch.json"
    session_data = {
        "restoreId": restore_id,
        "phase": phase,
        "targetId": target_id,
        "backupId": "bk_001",
        "createdAt": "2026-08-15T00:00:00Z",
        "holds": holds or [
            {
                "holdKey": f"holds/restore_{restore_id}.json",
                "generation": 1,
                "etag": '"etag1"',
                "expiresAt": "2026-08-15T06:00:00Z",
                "acquiredAt": "2026-08-15T00:00:00Z",
            }
        ],
    }
    session_file.write_text(json.dumps(session_data, indent=2), encoding="utf-8")
    return session_file


def test_scan_durable_recovery_sessions(tmp_settings: Path) -> None:
    restore_root = tmp_settings / ".restore-staging"
    _create_fake_session(restore_root, "restore_active", "fetching")
    _create_fake_session(restore_root, "restore_paused", "paused")
    _create_fake_session(restore_root, "restore_req", "recovery-required")
    _create_fake_session(restore_root, "restore_done", "complete")

    sessions = backup_recovery_keeper.scan_durable_recovery_sessions()
    assert len(sessions) == 4
    assert "restore_active" in sessions
    assert "restore_paused" in sessions
    assert "restore_req" in sessions
    assert "restore_done" in sessions


def test_reconcile_durable_recovery_leases_local_target(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    restore_root = tmp_settings / ".restore-staging"
    _create_fake_session(restore_root, "restore_local_active", "fetching", target_id="managed-local")
    _create_fake_session(restore_root, "restore_local_paused", "paused", target_id="managed-local")
    _create_fake_session(restore_root, "restore_local_done", "complete", target_id="managed-local")

    summary = backup_recovery_keeper.reconcile_durable_recovery_leases()
    assert summary["scanned"] == 3
    assert summary["skipped"] == 3
    assert summary["renewed"] == 0

    health = backup_recovery_keeper.get_recovery_lease_health()
    assert health["status"] in {"ok", "degraded"}
    assert health["activeLeases"] == 0  # local targets are not remote-hold protected
    assert "keeperRunning" in health
    assert "consecutiveFailures" in health


def test_reconcile_durable_recovery_leases_remote_store(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    restore_root = tmp_settings / ".restore-staging"
    holds = [
        {
            "holdKey": "holds/restore_rem1.json",
            "generation": 1,
            "etag": '"etag1"',
            "expiresAt": "2026-08-15T06:00:00Z",
            "acquiredAt": "2026-08-15T00:00:00Z",
        }
    ]
    _create_fake_session(restore_root, "restore_rem1", "fetching", target_id="target_remote", holds=holds)
    _create_fake_session(restore_root, "restore_rem2", "complete", target_id="target_remote")

    # Mock open_target_store
    mock_store = object()
    renewed_calls: list[str] = []

    def fake_open_target_store(target_id: str, write_intent: bool = True) -> Any:
        assert target_id == "target_remote"
        return mock_store

    def fake_renew_hold(store: Any, hold_entry: dict[str, Any], *, ttl_seconds: int = 6 * 3600) -> dict[str, Any]:
        assert store is mock_store
        renewed_calls.append(hold_entry["holdKey"])
        return {
            **hold_entry,
            "generation": int(hold_entry.get("generation", 1)) + 1,
            "etag": '"new-etag"',
            "expiresAt": "2026-08-15T12:00:00Z",
        }

    from deepseek_infra.infra.workspace import backup_targets
    monkeypatch.setattr(backup_targets, "open_target_store", fake_open_target_store)
    monkeypatch.setattr(backup_recovery_lease, "renew_recovery_hold", fake_renew_hold)

    summary = backup_recovery_keeper.reconcile_durable_recovery_leases(min_renew_age_seconds=0)
    assert summary["scanned"] == 2
    assert summary["renewed"] == 1
    assert summary["skipped"] == 1
    assert len(renewed_calls) == 1

    # Verify session file updated
    s_data = json.loads((restore_root / "restore_rem1" / "remote-fetch.json").read_text(encoding="utf-8"))
    assert s_data["holds"][0]["generation"] == 2
    assert s_data["holds"][0]["etag"] == '"new-etag"'


def test_reconcile_durable_recovery_leases_failure_handling(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    restore_root = tmp_settings / ".restore-staging"
    holds = [{"holdKey": "holds/fail.json"}]
    _create_fake_session(restore_root, "restore_fail", "fetching", target_id="target_remote", holds=holds)

    from deepseek_infra.infra.workspace import backup_targets
    monkeypatch.setattr(backup_targets, "open_target_store", lambda *a, **k: (_ for _ in ()).throw(OSError("network down")))

    summary = backup_recovery_keeper.reconcile_durable_recovery_leases(min_renew_age_seconds=0)
    assert summary["failed"] == 1
    assert len(summary["details"]["failed"]) == 1


def test_recovery_lease_keeper_thread_lifecycle(tmp_settings: Path) -> None:
    keeper = backup_recovery_keeper.RecoveryLeaseKeeper(tick_interval_seconds=0.05, min_renew_interval_seconds=1.0)
    summary = keeper.step()
    assert isinstance(summary, dict)
    assert not keeper.is_running

    keeper.start()
    assert keeper.is_running
    # Second start should be a no-op
    keeper.start()
    assert keeper.is_running

    time.sleep(0.1)
    keeper.stop(timeout=1.0)
    assert not keeper.is_running

    # Global keeper singleton
    gk = backup_recovery_keeper.get_global_recovery_keeper()
    assert gk is not None

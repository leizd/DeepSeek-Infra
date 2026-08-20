"""Cover remaining audit/keeper/app lines for the coverage gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import (
    backup_dr_audit,
    backup_dr_ledger,
    backup_publish,
    backup_recovery_keeper,
    backup_recovery_lease,
    backup_targets,
)
from deepseek_infra.infra.workspace.backup_target_store import ListPage, ObjectMeta, object_key


def _receipt_bytes(receipt: dict[str, Any]) -> bytes:
    return (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def test_audit_remote_full_success_and_resume(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target_id = "target_ok_full"
    receipt = {
        "backupId": "bk_ok",
        "policyId": "pol",
        "targetId": target_id,
        "snapshotKind": "full",
        "storageProtocol": "object-set-v1",
        "objectSetDigest": "a" * 64,
        "objects": [{"digest": "b" * 64, "size": 1}],
        "size": 1,
        "logicalBytes": 2,
    }
    rbytes = _receipt_bytes(receipt)
    commit = {
        "schemaVersion": 4,
        "backupId": "bk_ok",
        "policyId": "pol",
        "receiptDigest": hashlib.sha256(rbytes).hexdigest(),
        "commitHash": "c" * 64,
        "previousCommitHash": "0" * 64,
        "targetGeneration": 2,
        "storageProtocol": "object-set-v1",
        "objectSetDigest": "a" * 64,
        "controlObjectDigest": "d" * 64,
        "fencingToken": 1,
        "runId": "r",
        "scheduleSlot": "s",
        "slotDigest": "e" * 64,
    }
    commit["commitHash"] = backup_publish._commit_hash(commit)

    class Store:
        def list_objects(self, prefix: str, cursor: str | None = None, limit: int = 100) -> ListPage:
            if cursor:
                return ListPage((), None)
            return ListPage((ObjectMeta(key="commits/p/c.json", size=10, etag="e"),), "next")

        def get_bytes(self, key: str) -> bytes | None:
            return None

        def stat(self, key: str) -> ObjectMeta | None:
            return ObjectMeta(key=key, size=1, etag="payload") if key == object_key("b" * 64) else None

    store = Store()
    monkeypatch.setattr(backup_targets, "open_target_store", lambda *a, **k: store)
    monkeypatch.setattr(
        backup_dr_audit,
        "read_json",
        lambda s, key: commit if key.startswith("commits/") else receipt if key.startswith("receipts/") else None,
    )
    monkeypatch.setattr(backup_publish, "commit_marker_valid", lambda m: True)

    first = backup_dr_audit.audit_remote_target(target_id, page_size=10)
    assert first["status"] == "in-progress"
    assert first["recoveryPointsFound"] >= 1
    second = backup_dr_audit.resume_audit(first["auditId"])
    assert second["status"] == "completed"
    pts = backup_dr_ledger.list_recovery_points(target_id=target_id)
    assert any(p.get("recoverable") for p in pts)


def test_keeper_renew_session_holdkeys_path(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_settings / ".restore-staging" / "restore_hk"
    root.mkdir(parents=True)
    session = {
        "restoreId": "restore_hk",
        "phase": "fetching-selected-components",
        "targetId": "target_remote",
        "holdKeys": ["holds/a.json"],
        "lastHoldRenewedAt": "2000-01-01T00:00:00Z",
    }
    (root / "remote-fetch.json").write_text(json.dumps(session), encoding="utf-8")
    monkeypatch.setattr(backup_targets, "open_target_store", lambda *a, **k: object())
    monkeypatch.setattr(backup_recovery_lease, "renew_session", lambda *a, **k: True)
    summary = backup_recovery_keeper.reconcile_durable_recovery_leases(min_renew_age_seconds=0)
    assert summary["renewed"] == 1


def test_keeper_local_and_no_hold_and_terminal(tmp_settings: Path) -> None:
    root = tmp_settings / ".restore-staging"
    for name, phase, tid, holds in [
        ("loc", "fetching", "managed-local", [{"holdKey": "h"}]),
        ("noh", "fetching", "target_x", []),
        ("term", "complete", "target_x", [{"holdKey": "h"}]),
        ("emptyphase", "", "target_x", [{"holdKey": "h"}]),
    ]:
        d = root / name
        d.mkdir(parents=True)
        payload: dict[str, Any] = {"restoreId": name, "phase": phase, "targetId": tid}
        if holds:
            payload["holds"] = holds
        (d / "remote-fetch.json").write_text(json.dumps(payload), encoding="utf-8")
    summary = backup_recovery_keeper.reconcile_durable_recovery_leases(min_renew_age_seconds=0)
    assert summary["scanned"] == 4
    assert summary["renewed"] == 0


def test_app_shutdown_exception_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra import app as app_mod

    class BoomServer:
        def shutdown(self) -> None:
            return None

        def server_close(self) -> None:
            raise OSError("close")

    class H:
        stop_cache_cleanup = type("E", (), {"set": lambda self: None})()
        server = BoomServer()

    monkeypatch.setattr(
        "deepseek_infra.infra.workspace.backup_recovery_keeper.stop_global_recovery_keeper",
        lambda **k: (_ for _ in ()).throw(RuntimeError("stop-fail")),
    )
    # should not raise
    app_mod.shutdown_handle(H())  # type: ignore[arg-type]


def test_app_startup_keeper_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra import app as app_mod

    monkeypatch.setattr(app_mod, "multipart_module", object())
    monkeypatch.setattr(app_mod, "supported_multipart_module", lambda m: True)
    monkeypatch.setattr("deepseek_infra.infra.workspace.backups.recover_interrupted_restores", lambda: {"recoveryRequired": ["x"]})
    monkeypatch.setattr(
        "deepseek_infra.infra.workspace.backup_recovery_keeper.start_global_recovery_keeper",
        lambda **k: (_ for _ in ()).throw(RuntimeError("start-fail")),
    )
    monkeypatch.setattr("deepseek_infra.backup_worker.start_embedded_worker", lambda: None)
    app_mod.ensure_startup_dependencies()


def test_planner_no_recoverable_logical(tmp_settings: Path) -> None:
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="t",
        policy_id="p_empty",
        backup_id="b",
        committed_at="2026-08-15T00:00:00Z",
        recoverable=False,
    )
    path = tmp_settings / ".backup-policies" / "p_empty.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "policyId": "p_empty",
                "name": "e",
                "enabled": True,
                "targetId": "t_missing",
                "replication": {"enabled": False, "targets": [], "minCommittedCopies": 1},
                "protection": {"mode": "age-recipient", "recipients": ["age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"]},
                "schedule": {"cron": "0 3 * * *", "timezone": "UTC", "misfirePolicy": "skip", "catchupWindowSeconds": 86400, "jitterSeconds": 0},
                "scope": {"mode": "full", "projectIds": []},
                "frontendMirror": {"mode": "best-effort"},
                "retentionPolicyId": "default",
                "retry": {"maxAttempts": 3, "initialBackoffSeconds": 60, "maxBackoffSeconds": 900},
                "incremental": {"mode": "off"},
                "recoveryObjectives": {},
                "recoveryDrill": {"enabled": False},
                "createdAt": "2026-08-15T00:00:00Z",
                "updatedAt": "2026-08-15T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    from deepseek_infra.core.errors import AppError
    from deepseek_infra.infra.workspace import backup_recovery_planner

    with pytest.raises(AppError):
        backup_recovery_planner.plan_recovery(policy_id="p_empty")

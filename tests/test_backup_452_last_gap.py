from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import backup_dr_audit, backup_recovery_keeper, backup_targets
from deepseek_infra.infra.workspace.backup_target_store import ListPage, ObjectMeta


def test_audit_skips_non_json_and_empty_backup(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Store:
        def list_objects(self, prefix: str, cursor: str | None = None, limit: int = 100) -> ListPage:
            return ListPage((ObjectMeta(key="commits/p/note.txt", size=1, etag="e"), ObjectMeta(key="commits/p/c.json", size=1, etag="e2")), None)

    monkeypatch.setattr(backup_targets, "open_target_store", lambda *a, **k: Store())
    monkeypatch.setattr(backup_dr_audit, "read_json", lambda s, k: {"backupId": ""} if k.endswith(".json") else None)
    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_publish.commit_marker_valid", lambda m: True)
    res = backup_dr_audit.audit_remote_target("target_skip")
    assert res["status"] == "completed"
    assert res["objectsAudited"] >= 1


def test_keeper_hold_entry_without_holdkey(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_settings / ".restore-staging" / "r_partial"
    root.mkdir(parents=True)
    (root / "remote-fetch.json").write_text(
        '{"restoreId":"r_partial","phase":"fetching","targetId":"target_r","holds":[{"x":1},{"holdKey":"holds/a.json","generation":1}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(backup_targets, "open_target_store", lambda *a, **k: object())

    def renew(store: Any, hold: dict[str, Any], *, ttl_seconds: int = 0) -> dict[str, Any]:
        return {**hold, "generation": 2}

    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_recovery_lease.renew_recovery_hold", renew)
    summary = backup_recovery_keeper.reconcile_durable_recovery_leases(min_renew_age_seconds=0)
    assert summary["renewed"] == 1


def test_keeper_retained_when_renew_false(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_settings / ".restore-staging" / "r_ret"
    root.mkdir(parents=True)
    (root / "remote-fetch.json").write_text(
        '{"restoreId":"r_ret","phase":"fetching","targetId":"target_r","holds":[{"holdKey":"holds/a.json"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(backup_targets, "open_target_store", lambda *a, **k: object())
    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_recovery_lease.renew_recovery_hold", lambda *a, **k: {"holdKey": "holds/a.json"})
    # force did_renew False by making renew return same without generation change and empty holds path
    # actually renew_recovery_hold always sets did_renew True when holdKey present
    # use holdKeys path with renew_session returning False
    (root / "remote-fetch.json").write_text(
        '{"restoreId":"r_ret","phase":"fetching","targetId":"target_r","holdKeys":["holds/a.json"]}',
        encoding="utf-8",
    )
    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_recovery_lease.renew_session", lambda *a, **k: False)
    summary = backup_recovery_keeper.reconcile_durable_recovery_leases(min_renew_age_seconds=0)
    assert summary["retained"] >= 1

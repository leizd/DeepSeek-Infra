from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import BinaryIO

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_recovery_lease, backup_target_store


def _now() -> datetime:
    return datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def test_renew_increments_generation_and_extends_expiry_with_cas() -> None:
    store = backup_target_store.MemoryTargetStore()
    key = "holds/restore/restore-test.json"
    backup_target_store.put_json_if_absent(
        store,
        key,
        {
            "schemaVersion": 3,
            "restoreId": "restore-test",
            "generation": 1,
            "expiresAt": "2026-08-13T13:00:00Z",
        },
    )

    renewed = backup_recovery_lease.renew(store, key, now=_now(), ttl_seconds=6 * 3600)

    assert renewed["generation"] == 2
    assert renewed["expiresAt"] == "2026-08-13T18:00:00Z"
    assert backup_target_store.read_json(store, key) == renewed

    renewed_again = backup_recovery_lease.renew(store, key, now=_now(), ttl_seconds=6 * 3600)
    assert renewed_again["expiresAt"] == "2026-08-13T18:00:01Z"


def test_renew_fails_closed_on_cas_conflict() -> None:
    class ConflictStore(backup_target_store.MemoryTargetStore):
        def put_if_match(
            self,
            key: str,
            source: BinaryIO | bytes,
            *,
            expected_etag: str,
            checksum_sha256: str | None = None,
            content_type: str = "application/octet-stream",
        ) -> backup_target_store.PutResult:
            current = backup_target_store.read_json(self, key)
            assert current is not None
            current["generation"] = 99
            backup_target_store.MemoryTargetStore.put_if_match(
                self,
                key,
                __import__("json").dumps(current).encode(),
                expected_etag=expected_etag,
            )
            return backup_target_store.MemoryTargetStore.put_if_match(
                self,
                key,
                source,
                expected_etag=expected_etag,
                checksum_sha256=checksum_sha256,
                content_type=content_type,
            )

    store = ConflictStore()
    key = "holds/restore/conflict.json"
    backup_target_store.put_json_if_absent(
        store,
        key,
        {"schemaVersion": 3, "restoreId": "restore-conflict", "generation": 1, "expiresAt": "2026-08-13T13:00:00Z"},
    )

    with pytest.raises(AppError, match="renewal conflict"):
        backup_recovery_lease.renew(store, key, now=_now(), ttl_seconds=6 * 3600)

    observed = backup_target_store.read_json(store, key)
    assert observed is not None and observed["generation"] == 99


def test_renew_session_protects_paused_and_recovery_required_but_not_terminal() -> None:
    store = backup_target_store.MemoryTargetStore()
    keys = ["holds/restore/a.json", "holds/restore/b.json"]
    for key in keys:
        backup_target_store.put_json_if_absent(
            store,
            key,
            {"schemaVersion": 3, "restoreId": "restore-session", "generation": 0, "expiresAt": "2026-08-13T13:00:00Z"},
        )

    for phase in ("paused", "recovery-required"):
        session = {"phase": phase, "holdKeys": keys, "lastHoldRenewedAt": "2026-08-13T11:00:00Z"}
        assert backup_recovery_lease.renew_session(store, session, now=_now(), min_interval_seconds=60) is True
    terminal = {"phase": "complete", "holdKeys": keys}
    assert backup_recovery_lease.renew_session(store, terminal, now=_now()) is False
    throttled = {"phase": "paused", "holdKeys": keys, "lastHoldRenewedAt": (_now() - timedelta(seconds=30)).isoformat()}
    assert backup_recovery_lease.renew_session(store, throttled, now=_now(), min_interval_seconds=60) is False

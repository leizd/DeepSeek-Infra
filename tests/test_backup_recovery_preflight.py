from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_component_cache,
    backup_recovery_preflight,
    backup_remote_restore,
    backup_verified_plan,
    backups,
)
from deepseek_infra.infra.workspace.backup_publish import ResolvedTarget
from deepseek_infra.infra.workspace.backup_target_store import MemoryTargetStore, object_key, receipt_key


def test_capacity_report_includes_bounded_crypto_and_exact_boundary() -> None:
    report = backup_recovery_preflight.capacity_report(
        materialized_tree_bytes=100,
        safety_backup_peak_bytes=200,
        uncached_ciphertext_bytes=30,
        plaintext_component_bytes=[5, 20, 10],
        free_disk_bytes=370,
        reserve_bytes=10,
    )

    assert report["scratch"] == {
        "materializedTreeBytes": 100,
        "uncachedCiphertextBytes": 30,
        "boundedCryptoPlaintextBytes": 30,
        "estimatedPeakBytes": 160,
    }
    assert report["disk"] == {"freeBytes": 370, "reserveBytes": 10, "requiredBytes": 370, "sufficient": True}


def test_safety_backup_peak_covers_staging_archive_and_verification() -> None:
    estimate = backup_recovery_preflight.estimate_safety_backup_peak(100)

    assert estimate == {
        "liveLogicalBytes": 100,
        "archiveBytes": 100 + 1024 * 1024,
        "estimatedPeakBytes": 2 * 100 + 2 * (100 + 1024 * 1024),
    }


def test_cache_inspect_is_read_only_for_valid_and_corrupt_entries(tmp_path: Path) -> None:
    cache = backup_component_cache.ComponentCache(tmp_path)
    raw = b"verified-ciphertext"
    digest = hashlib.sha256(raw).hexdigest()
    path = cache.path_for(digest)
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    old = 1_700_000_000
    os.utime(path, (old, old))

    assert cache.inspect(digest, len(raw)) is True
    assert int(path.stat().st_mtime) == old
    path.write_bytes(b"corrupt")
    assert cache.inspect(digest, len(raw)) is False
    assert path.read_bytes() == b"corrupt"


def test_evaluate_preflight_reports_cache_network_health_and_capacity(tmp_path: Path) -> None:
    cached_raw = b"cached"
    remote_raw = b"remote"
    cached_digest = hashlib.sha256(cached_raw).hexdigest()
    remote_digest = hashlib.sha256(remote_raw).hexdigest()
    cache = backup_component_cache.ComponentCache(tmp_path / "cache")
    cached_path = cache.path_for(cached_digest)
    cached_path.parent.mkdir(parents=True)
    cached_path.write_bytes(cached_raw)
    store = MemoryTargetStore()
    store.put_if_absent(receipt_key("backup-full"), b"{}")
    store.put_if_absent(object_key(remote_digest), remote_raw)
    session = {
        "restoreId": "restore-preflight",
        "backupId": "backup-full",
        "chain": [
            {
                "backupId": "backup-full",
                "snapshotKind": "full",
                "requiredComponents": [
                    {
                        "objectDigest": cached_digest,
                        "expectedBytes": len(cached_raw),
                        "plaintextSize": 11,
                        "ciphertextPath": str(tmp_path / "missing-cached.age"),
                    },
                    {
                        "objectDigest": remote_digest,
                        "expectedBytes": len(remote_raw),
                        "plaintextSize": 22,
                        "ciphertextPath": str(tmp_path / "missing-remote.age"),
                    },
                ],
            }
        ],
    }
    projection = {
        "bytes": {"selectedLogicalBytes": 100, "estimatedMaterializedBytes": 120},
        "requiredComponents": 2,
        "totalComponents": 4,
    }
    safety = {**backup_recovery_preflight.estimate_safety_backup_peak(50), "externalBytesKnown": True}

    report = backup_recovery_preflight.evaluate_preflight(
        session,
        projection,
        store=store,
        target_kind="memory",
        catalog={"backup-full": {"scrubOk": True, "userUnlockVerifiedAt": "now", "ciphertextScrubbedAt": "now"}},
        cache=cache,
        safety_backup=safety,
        free_disk_bytes=10_000_000,
        reserve_bytes=10,
    )

    assert report["ready"] is True
    assert report["cache"] == {"hitComponents": 1, "missComponents": 1, "hitBytes": len(cached_raw)}
    assert report["network"] == {"remoteBytes": len(remote_raw)}
    assert report["projectionRecoverability"]["status"] == "recoverable"
    assert report["lastWholeSnapshotHealth"]["status"] == "ok"


def test_evaluate_preflight_fails_closed_for_missing_component_and_disk_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    digest = hashlib.sha256(b"missing").hexdigest()
    store = MemoryTargetStore()
    store.put_if_absent(receipt_key("backup-full"), b"{}")
    session = {
        "restoreId": "restore-blocked",
        "backupId": "backup-full",
        "chain": [
            {
                "backupId": "backup-full",
                "snapshotKind": "full",
                "requiredComponents": [
                    {"objectDigest": digest, "expectedBytes": 7, "plaintextSize": 9, "ciphertextPath": str(tmp_path / "missing.age")}
                ],
            }
        ],
    }
    monkeypatch.setattr(backup_recovery_preflight, "_disk_free_bytes", lambda _paths: (_ for _ in ()).throw(OSError("offline")))

    report = backup_recovery_preflight.evaluate_preflight(
        session,
        {"bytes": {"estimatedMaterializedBytes": 1}},
        store=store,
        target_kind="memory",
        cache=backup_component_cache.ComponentCache(tmp_path / "cache"),
        safety_backup={**backup_recovery_preflight.estimate_safety_backup_peak(1), "externalBytesKnown": True},
    )

    assert report["ready"] is False
    assert {item["code"] for item in report["blockingReasons"]} == {"required-component-unavailable", "disk-probe-failed"}
    assert report["projectionRecoverability"]["missingComponents"] == 1


def test_capacity_inputs_reject_negative_values() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        backup_recovery_preflight.capacity_report(
            materialized_tree_bytes=-1,
            safety_backup_peak_bytes=0,
            uncached_ciphertext_bytes=0,
            plaintext_component_bytes=[],
            free_disk_bytes=0,
        )


def test_preflight_service_maps_capacity_block_to_stable_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    restore_id = "restore-service-preflight"
    session = {
        "restoreId": restore_id,
        "backupId": "backup-full",
        "targetId": "target-memory",
        "storageProtocol": "object-set-v1",
        "chain": [],
    }
    store = MemoryTargetStore()
    monkeypatch.setattr(backups, "RESTORE_DIR", tmp_path)
    monkeypatch.setattr(backup_remote_restore, "read_restore_session", lambda _restore_id: session)
    monkeypatch.setattr(backup_verified_plan, "load_verified_plan", lambda _base, _session: (None, {"bytes": {}}))
    monkeypatch.setattr(
        backup_remote_restore.backup_publish,
        "resolve_target",
        lambda _target_id, write_intent=False: ResolvedTarget("target-memory", None, False, "memory", store),
    )
    monkeypatch.setattr(backup_remote_restore.backup_catalog, "catalog_state_store", lambda _store: {})
    blocked = {
        "restoreId": restore_id,
        "ready": False,
        "blockingReasons": [{"code": "insufficient-disk", "message": "blocked"}],
    }
    monkeypatch.setattr(backup_recovery_preflight, "evaluate_preflight", lambda *_args, **_kwargs: blocked)

    with pytest.raises(AppError) as caught:
        backup_remote_restore.preflight_restore_session(restore_id)

    assert caught.value.status == 409
    assert caught.value.code == ErrorCode.RECOVERY_PREFLIGHT_CAPACITY
    assert caught.value.details == {"preflight": blocked}
    persisted = backup_remote_restore.read_restore_session(restore_id)
    assert persisted is not None and persisted["preflight"] == blocked


def test_preflight_service_requires_verified_plan_or_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    restore_id = "restore-no-plan"
    monkeypatch.setattr(backups, "RESTORE_DIR", tmp_path)
    monkeypatch.setattr(
        backup_remote_restore,
        "read_restore_session",
        lambda _restore_id: {"restoreId": restore_id, "storageProtocol": "object-set-v1"},
    )
    monkeypatch.setattr(backup_verified_plan, "load_verified_plan", lambda _base, _session: None)
    monkeypatch.setattr(backup_remote_restore.backup_crypto, "has_secret", lambda _restore_id: False)

    with pytest.raises(AppError, match="verified plan") as caught:
        backup_remote_restore.preflight_restore_session(restore_id)

    assert caught.value.code == ErrorCode.INVALID_REQUEST


def test_object_set_materialize_requires_preflight_before_secret_consumption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    restore_id = "restore-enforced-preflight"
    session = {
        "restoreId": restore_id,
        "phase": "components-fetched",
        "storageProtocol": "object-set-v1",
        "chain": [],
    }
    monkeypatch.setattr(backups, "RESTORE_DIR", tmp_path)
    backup_remote_restore._atomic_write_json(tmp_path / restore_id / "remote-fetch.json", session)
    calls: list[str] = []

    def record_preflight(value: str, client: object = None) -> dict[str, object]:
        del client
        calls.append(value)
        return {"restoreId": value, "ready": True}

    monkeypatch.setattr(
        backup_remote_restore,
        "preflight_restore_session",
        record_preflight,
    )
    monkeypatch.setattr(
        backup_remote_restore.backup_crypto,
        "consume_secret",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("stop after preflight")),
    )

    with pytest.raises(AssertionError, match="stop after preflight"):
        backup_remote_restore.materialize_federated_restore(restore_id)

    assert calls == [restore_id]

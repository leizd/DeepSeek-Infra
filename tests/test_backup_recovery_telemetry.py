from __future__ import annotations

import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.infra.observability import metrics as observability_metrics
from deepseek_infra.infra.workspace import backup_recovery_telemetry, backup_remote_restore, backups
from deepseek_infra.infra.workspace.backup_target_store import MemoryTargetStore, object_key


UTC = timezone.utc


def _at(seconds: int) -> datetime:
    return datetime(2026, 8, 13, tzinfo=UTC) + timedelta(seconds=seconds)


def test_recovery_telemetry_persists_bounded_stage_samples_and_counters() -> None:
    session: dict[str, Any] = {
        "restoreId": "restore_secret_correlation",
        "phase": "fetching-selected-components",
        "createdAt": "2026-08-13T00:00:00Z",
        "updatedAt": "2026-08-13T00:00:00Z",
        "expectedBytes": 1_024,
        "downloadedBytes": 0,
        "componentStates": {"a" * 64: {"state": "downloading", "downloadedBytes": 0}},
    }
    backup_recovery_telemetry.update_for_persist({}, session, now=_at(0))
    existing = json.loads(json.dumps(session))
    session["phase"] = "components-fetched"
    session["downloadedBytes"] = 1_024
    session["componentStates"] = {"a" * 64: {"state": "verified", "downloadedBytes": 1_024}}
    backup_recovery_telemetry.increment_counter(session, "transferBytes", 1_024)
    backup_recovery_telemetry.record_stage(
        session,
        stage="transfer",
        result="success",
        duration_ms=2_000,
        byte_count=1_024,
        components=1,
        observed_at=_at(2),
    )

    backup_recovery_telemetry.update_for_persist(existing, session, now=_at(2))

    telemetry = session["recoveryTelemetry"]
    assert telemetry["counters"]["transferBytes"] == 1_024
    assert telemetry["counters"]["componentsVerified"] == 1
    assert telemetry["samples"] == [
        {
            "sequence": 1,
            "stage": "transfer",
            "result": "success",
            "durationMs": 2_000,
            "bytes": 1_024,
            "components": 1,
            "observedAt": "2026-08-13T00:00:02Z",
        }
    ]

    for index in range(backup_recovery_telemetry.MAX_STAGE_SAMPLES + 8):
        backup_recovery_telemetry.record_stage(
            session,
            stage="crypto",
            result="success",
            duration_ms=index + 1,
            byte_count=index,
            components=1,
            observed_at=_at(index + 3),
        )
    assert len(session["recoveryTelemetry"]["samples"]) == backup_recovery_telemetry.MAX_STAGE_SAMPLES
    assert session["recoveryTelemetry"]["samples"][-1]["durationMs"] == backup_recovery_telemetry.MAX_STAGE_SAMPLES + 8


def test_recovery_telemetry_merge_and_redaction_fail_closed() -> None:
    existing: dict[str, Any] = {
        "phase": "paused",
        "recoveryTelemetry": {
            "schemaVersion": 1,
            "counters": {"cacheHit": 2, "evilCounter": 99},
            "samples": [
                {
                    "sequence": 3,
                    "stage": "crypto",
                    "result": "success",
                    "durationMs": 10,
                    "bytes": 20,
                    "components": 1,
                    "observedAt": "2026-08-13T00:00:01Z",
                    "digest": "a" * 64,
                }
            ],
            "currentPhase": "paused",
            "phaseStartedAt": "2026-08-13T00:00:00Z",
            "secret": "age1-private",
        },
    }
    payload: dict[str, Any] = {
        "restoreId": "restore_private",
        "phase": "paused",
        "ciphertextPath": "C:/secret/project.zip",
        "error": "provider credential leaked",
        "recoveryTelemetry": {
            "schemaVersion": 1,
            "counters": {"cacheHit": 1, "cacheMiss": 4, "componentsTransferred": 3, "transferRetry": 1},
            "samples": [
                {
                    "sequence": 3,
                    "stage": "materialization",
                    "result": "success",
                    "durationMs": 30,
                    "bytes": 40,
                    "components": 2,
                    "observedAt": "2026-08-13T00:00:02Z",
                }
            ],
        },
    }

    backup_recovery_telemetry.update_for_persist(existing, payload, now=_at(5))

    telemetry = payload["recoveryTelemetry"]
    assert telemetry["counters"] == {"cacheHit": 2, "cacheMiss": 4, "componentsTransferred": 3, "transferRetry": 1}
    assert [(item["sequence"], item["stage"]) for item in telemetry["samples"]] == [(3, "crypto"), (4, "materialization")]
    serialized = json.dumps(telemetry)
    for forbidden in ("restore_private", "project.zip", "credential leaked", "age1-private", "a" * 64, "evilCounter"):
        assert forbidden not in serialized


def test_recovery_telemetry_merges_concurrent_counter_deltas() -> None:
    durable: dict[str, Any] = {
        "phase": "fetching",
        "recoveryTelemetry": {"schemaVersion": 1, "counters": {"cacheHit": 2}, "samples": []},
    }
    first = json.loads(json.dumps(durable))
    second = json.loads(json.dumps(durable))
    backup_recovery_telemetry.increment_counter(first, "cacheHit")
    backup_recovery_telemetry.increment_counter(second, "cacheHit")

    backup_recovery_telemetry.update_for_persist(durable, first)
    backup_recovery_telemetry.update_for_persist(first, second)

    assert second["recoveryTelemetry"]["counters"]["cacheHit"] == 4
    assert "_recoveryTelemetryPendingCounters" not in second


def test_recovery_telemetry_snapshot_uses_only_bounded_dimensions(tmp_path: Path) -> None:
    sessions: list[dict[str, Any]] = [
        {
            "phase": "components-fetched",
            "updatedAt": "2026-08-13T00:00:05Z",
            "recoveryTelemetry": {
                "schemaVersion": 1,
                "counters": {"cacheHit": 1, "holdRenewalSuccess": 2},
                "samples": [
                    {
                        "sequence": 1,
                        "stage": "transfer",
                        "result": "success",
                        "durationMs": 2_000,
                        "bytes": 4_000,
                        "components": 2,
                        "observedAt": "2026-08-13T00:00:05Z",
                    }
                ],
            },
        },
        {
            "phase": "attacker-controlled-phase",
            "updatedAt": "2026-08-13T00:00:06Z",
            "recoveryTelemetry": {"schemaVersion": 1, "counters": {"cacheMiss": 1}, "samples": []},
        },
    ]
    for index, session in enumerate(sessions):
        root = tmp_path / f"restore-{index}"
        root.mkdir()
        (root / "remote-fetch.json").write_text(json.dumps(session), encoding="utf-8")

    snapshot = backup_recovery_telemetry.metrics_snapshot(tmp_path)

    assert snapshot["jobsByPhase"] == {"components-fetched": 1, "unknown": 1}
    assert snapshot["counters"] == {"cacheHit": 1, "cacheMiss": 1, "holdRenewalSuccess": 2}
    assert snapshot["stageDuration"]["transfer"] == {
        "count": 1,
        "sumMs": 2_000,
        "buckets": {"1000": 0, "5000": 1, "15000": 1, "60000": 1, "300000": 1, "+Inf": 1},
    }
    assert snapshot["stageThroughput"]["transfer"] == {"count": 1, "sumBytesPerSecond": 2_000.0}

    prometheus = observability_metrics._recovery_lines({"recovery": snapshot})
    prometheus += observability_metrics._recovery_lines(
        {
            "recovery": {
                "jobsByPhase": {'bad"} 1\nforged_metric': 99},
                "stageDuration": {'bad"} 1\nforged_metric': {"buckets": {"1000": 99}}},
                "stageThroughput": {'bad"} 1\nforged_metric': {"count": 99, "sumBytesPerSecond": 99}},
            }
        }
    )
    rendered = "\n".join(prometheus)
    assert 'deepseek_recovery_jobs{phase="components-fetched"} 1' in rendered
    assert 'deepseek_recovery_jobs{phase="unknown"} 1' in rendered
    assert 'deepseek_recovery_stage_duration_seconds_bucket{stage="transfer",le="5.0"} 1' in rendered
    assert "# TYPE deepseek_recovery_stage_throughput_bytes_per_second summary" in rendered
    for forbidden in ("restore-0", "remote-fetch.json", "a" * 64, "attacker-controlled-phase"):
        assert forbidden not in rendered


def test_remote_download_telemetry_counts_only_new_bytes_after_resume(tmp_settings: Path) -> None:
    data = b"0123456789"
    digest = hashlib.sha256(data).hexdigest()
    store = MemoryTargetStore()
    store.put_if_absent(object_key(digest), data, checksum_sha256=digest)
    restore_id = "restore-telemetry-resume"
    ciphertext = backups.RESTORE_DIR / restore_id / "payload.age"
    ciphertext.parent.mkdir(parents=True)
    ciphertext.write_bytes(data[:4])
    session: dict[str, Any] = {
        "restoreId": restore_id,
        "phase": "fetching",
        "downloadedBytes": 4,
        "expectedBytes": len(data),
    }
    member: dict[str, Any] = {
        "backupId": "backup-telemetry",
        "objectDigest": digest,
        "expectedBytes": len(data),
        "downloadedBytes": 4,
        "ciphertextPath": str(ciphertext),
    }
    backup_remote_restore._atomic_write_json(backup_remote_restore._session_path(restore_id), session)

    assert backup_remote_restore._download_member_with_telemetry(session, store, member, None) is True

    persisted = backup_remote_restore.read_restore_session(restore_id)
    assert persisted is not None
    telemetry = persisted["recoveryTelemetry"]
    assert telemetry["counters"]["transferBytes"] == 6
    assert telemetry["counters"]["componentsTransferred"] == 1
    assert telemetry["counters"]["transferRetry"] == 1
    assert telemetry["samples"][-1] == {
        "sequence": 1,
        "stage": "transfer",
        "result": "success",
        "durationMs": telemetry["samples"][-1]["durationMs"],
        "bytes": 6,
        "components": 1,
        "observedAt": telemetry["samples"][-1]["observedAt"],
    }

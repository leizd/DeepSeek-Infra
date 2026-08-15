"""Targeted test coverage boosters for backup_dr_readiness, backup_retention, and backup_scheduled."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_dr_readiness,
    backup_retention,
    backup_scheduled,
)


def test_backup_dr_readiness_helpers(tmp_settings: Path) -> None:
    # 1. Time parsing
    now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
    iso_str = backup_dr_readiness._utc_iso(now)
    assert iso_str == "2026-08-15T12:00:00Z"

    parsed = backup_dr_readiness._parse_time("2026-08-15T12:00:00Z")
    assert parsed == now
    assert backup_dr_readiness._parse_time("invalid-date") is None
    assert backup_dr_readiness._parse_time(None) is None

    # 2. Cache health
    health = backup_dr_readiness._cache_health(now)
    assert health["source"] == "local-ciphertext-cache"

    # 3. Scope readiness evaluation with no points
    res = backup_dr_readiness.evaluate_scope_readiness("target-1", "policy-1", now=now)
    assert res["recoveryPoint"]["status"] == "unavailable"
    assert "no-recoverable-points" in res["reasons"]


def test_backup_retention_policy_crud(tmp_settings: Path) -> None:
    # 1. Normalization
    default_pol = backup_retention.normalize_retention_policy({})
    assert default_pol["schemaVersion"] == 1
    assert default_pol["keepLast"] == 3

    with pytest.raises(AppError):
        backup_retention.normalize_retention_policy("not_a_dict")  # type: ignore

    with pytest.raises(AppError):
        backup_retention.normalize_retention_policy({"keepLast": -1})

    # 2. Put and Get policy
    custom = backup_retention.put_retention_policy("custom_pol", {"keepLast": 5, "keepDaily": 7})
    assert custom["retentionPolicyId"] == "custom_pol"
    assert custom["keepLast"] == 5

    retrieved = backup_retention.get_retention_policy("custom_pol")
    assert retrieved["keepLast"] == 5

    # 3. List policies
    policies = backup_retention.list_retention_policies()
    assert any(p["retentionPolicyId"] == "custom_pol" for p in policies)

    # 4. Bucketing helpers
    dt = datetime(2026, 8, 15, 14, 30, 0)
    bkeys = backup_retention._bucket_keys(dt)
    assert bkeys["hourly"] == "2026-08-15T14"
    assert bkeys["daily"] == "2026-08-15"
    assert "2026" in bkeys["weekly"]

    ordered_h = backup_retention._ordered_buckets(dt, "hourly", 3)
    assert len(ordered_h) == 3
    assert ordered_h[0] == "2026-08-15T14"


def test_backup_scheduled_threshold_writer(tmp_settings: Path) -> None:
    # 1. Threshold writer
    stream = io.BytesIO()
    writer = backup_scheduled.ThresholdWriter(stream, byte_limit=10)
    assert writer.tell() == 0

    writer.write(b"12345")
    assert writer.tell() == 5

    with pytest.raises(backup_scheduled.DeltaCostExceeded):
        writer.write(b"123456")  # Total 11 > 10

    writer.seek(0)
    assert writer.tell() == 0
    writer.flush()

"""Tiny coverage bump for readiness cache/health edges."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from deepseek_infra.infra.workspace import backup_component_cache, backup_dr_readiness


def test_cache_health_invalid_pin_and_partials(tmp_settings: Path, monkeypatch) -> None:
    root = tmp_settings / ".backup-component-cache"
    (root / "sha256" / "aa").mkdir(parents=True)
    (root / "sha256" / "aa" / "bb.age").write_bytes(b"x" * 10)
    (root / "sha256" / "aa" / "cc.partial").write_bytes(b"y")
    pins = root / "pins"
    pins.mkdir()
    (pins / "bad.json").write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(backup_component_cache, "CACHE_DIR", root)
    now = datetime(2026, 8, 16, tzinfo=timezone.utc)
    h = backup_dr_readiness._cache_health(now)
    assert h["status"] == "error"
    assert h["reason"] == "pin-metadata-invalid"

    (pins / "bad.json").unlink()
    (pins / "ok.json").write_text(
        json.dumps({"schemaVersion": 1, "digests": ["a" * 64]}),
        encoding="utf-8",
    )
    h2 = backup_dr_readiness._cache_health(now)
    assert h2["status"] in {"ok", "warning"}
    assert h2["partialFiles"] >= 1


def test_parse_time_edges() -> None:
    assert backup_dr_readiness._parse_time(None) is None
    assert backup_dr_readiness._parse_time("not-a-time") is None
    assert backup_dr_readiness._parse_time("2026-08-15T00:00:00") is None
    assert backup_dr_readiness._parse_time("2026-08-15T00:00:00Z") is not None
    assert backup_dr_readiness._parse_time("2026-99-99T00:00:00Z") is None


def test_resolve_target_kind_and_helpers(tmp_settings: Path, monkeypatch) -> None:
    from deepseek_infra.infra.workspace import backup_targets

    assert backup_dr_readiness._resolve_target_kind("managed-local") == "managed-local"
    monkeypatch.setattr(backup_targets, "get_target", lambda tid: {"kind": "s3"})
    assert backup_dr_readiness._resolve_target_kind("target_x") == "s3"
    monkeypatch.setattr(backup_targets, "get_target", lambda tid: {"kind": ""})
    assert backup_dr_readiness._resolve_target_kind("target_y") == "filesystem"
    monkeypatch.setattr(backup_targets, "get_target", lambda tid: (_ for _ in ()).throw(RuntimeError("x")))
    assert backup_dr_readiness._resolve_target_kind("target_z") == "filesystem"

    assert backup_dr_readiness._nonnegative(-1) == 0
    assert backup_dr_readiness._nonnegative(3) == 3
    assert backup_dr_readiness._stage_samples([]) == []
    assert isinstance(backup_dr_readiness._drill_records(None), list)
    empty_root = tmp_settings / "empty"
    empty_root.mkdir()
    records, points, ok = backup_dr_readiness._commit_records_for_root(empty_root, "t")
    assert records == []
    assert points == set()
    assert ok is True


def test_resolve_recoverable_chain_and_latest(tmp_settings: Path) -> None:
    records = [
        {
            "backupId": "full",
            "targetId": "t",
            "policyId": "p",
            "snapshotKind": "full",
            "createdAt": "2026-08-15T00:00:00Z",
            "size": 10,
            "logicalBytes": 20,
            "result": "success",
            "observedAt": "2026-08-15T00:00:00Z",
        },
        {
            "backupId": "inc",
            "targetId": "t",
            "policyId": "p",
            "snapshotKind": "incremental",
            "parentBackupId": "full",
            "createdAt": "2026-08-15T01:00:00Z",
            "size": 5,
            "logicalBytes": 25,
            "result": "failed",
            "observedAt": "2026-08-15T01:00:00Z",
        },
    ]
    matched = backup_dr_readiness._resolve_recoverable_chain(records, "inc")
    assert matched is not None
    assert len(matched) == 1
    assert matched[0]["backupId"] == "inc"
    assert backup_dr_readiness._resolve_recoverable_chain(records, "missing") == []
    assert backup_dr_readiness._resolve_recoverable_chain("bad") is None
    outcome = backup_dr_readiness._latest_outcome(records, time_key="observedAt")
    assert outcome["status"] in {"ok", "error"}
    empty = backup_dr_readiness._latest_outcome([])
    assert empty["status"] == "unavailable"

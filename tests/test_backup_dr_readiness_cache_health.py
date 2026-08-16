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

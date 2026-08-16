"""Unit tests for Component Cache Pin Reconciliation (Recovery Assurance Gate E)."""

from __future__ import annotations

import json
from pathlib import Path


from deepseek_infra.infra.workspace import (
    backup_component_cache,
)


def _create_fake_session(
    staging_dir: Path,
    restore_id: str,
    phase: str,
) -> Path:
    session_dir = staging_dir / restore_id
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / "remote-fetch.json"
    session_data = {
        "restoreId": restore_id,
        "phase": phase,
        "createdAt": "2026-08-15T00:00:00Z",
    }
    session_file.write_text(json.dumps(session_data, indent=2), encoding="utf-8")
    return session_file


def test_reconcile_pins_retains_active_and_prunes_orphans(tmp_settings: Path) -> None:
    restore_root = tmp_settings / ".restore-staging"

    _create_fake_session(restore_root, "sess_active", "fetching")
    _create_fake_session(restore_root, "sess_paused", "paused")
    _create_fake_session(restore_root, "sess_done", "complete")

    # Pin various digests with owner IDs
    backup_component_cache.pin("sha256:d1", owner_id="sess_active")
    backup_component_cache.pin("sha256:d2", owner_id="sess_paused")
    backup_component_cache.pin("sha256:d3", owner_id="sess_done")
    backup_component_cache.pin("sha256:d4", owner_id="sess_nonexistent")
    backup_component_cache.pin("sha256:d5")  # manual pin without owner

    assert backup_component_cache.is_pinned("sha256:d1")
    assert backup_component_cache.is_pinned("sha256:d2")
    assert backup_component_cache.is_pinned("sha256:d3")
    assert backup_component_cache.is_pinned("sha256:d4")
    assert backup_component_cache.is_pinned("sha256:d5")

    # Run reconciliation
    summary = backup_component_cache.reconcile_pins()
    assert summary["retained"] == 3  # sess_active, sess_paused, and ownerless d5
    assert summary["unpinned"] == 2  # sess_done, sess_nonexistent

    assert backup_component_cache.is_pinned("sha256:d1")
    assert backup_component_cache.is_pinned("sha256:d2")
    assert not backup_component_cache.is_pinned("sha256:d3")
    assert not backup_component_cache.is_pinned("sha256:d4")
    assert backup_component_cache.is_pinned("sha256:d5")

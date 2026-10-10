"""Targeted test coverage boosters for core workspace backup contributors and capabilities."""

from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backups


def test_backups_capabilities_and_contributors(tmp_settings: Path) -> None:
    # 1. Capabilities
    caps = backups.capabilities()
    assert caps["ok"] is True
    assert caps["schemaVersion"] == backups.BACKUP_SCHEMA
    assert "projects" in caps["includedByDefault"]

    # 2. Registered contributors
    contributors = backups._registered_contributors()
    assert len(contributors) >= 8

    # 3. DirectoryContributor methods
    p_contrib = contributors[0]
    ctx = backups.BackupContext()
    inv = p_contrib.inventory(ctx)
    assert isinstance(inv, dict)

    p_contrib.flush(ctx)

    schema_info = p_contrib.inspect_schema(1)
    assert schema_info["compatible"] is True

    migrated = p_contrib.migrate(tmp_settings, 1)
    assert migrated == tmp_settings

    with pytest.raises(AppError):
        p_contrib.migrate(tmp_settings, 999)

    # 4. External participant journal parsing
    journal_val = {
        "phase": "committed",
        "sourceDigest": "abc",
        "preparedDigest": "def",
        "imported": 5,
        "skipped": 1,
        "interrupted": 0,
        "remapped": {"k1": "v1"},
    }
    journal = backups._participant_journal(journal_val)
    assert journal["phase"] == "committed"
    assert journal["imported"] == 5
    assert journal["remapped"]["k1"] == "v1"

    with pytest.raises(AppError):
        backups._participant_journal("not_a_dict")

    with pytest.raises(AppError):
        backups._participant_journal({})  # missing phase

    # 5. External participants filter
    tx = {"contributors": [{"external": True, "id": "ext1"}, {"external": False, "id": "local1"}]}
    ext = backups._external_participants(tx)
    assert len(ext) == 1
    assert ext[0]["id"] == "ext1"

    assert backups._external_participants({}) == []

    # 6. Project artifact paths with no projects
    empty_paths = backups._project_artifact_paths(backups.BackupContext(project_ids=("nonexistent_p",)))
    assert len(empty_paths) == 0

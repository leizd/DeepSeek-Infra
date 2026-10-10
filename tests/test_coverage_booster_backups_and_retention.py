"""Targeted test coverage boosters for backups.py and backup_retention.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_retention,
    backups,
)
from deepseek_infra.infra.workspace.backup_target_store import ListPage, ObjectMeta


def test_stateless_mcp_contributor_methods(tmp_settings: Path) -> None:
    participant = backups.StatelessMcpContributor()
    assert participant.inspect_schema(participant.schema_version)["compatible"] is True
    assert participant.inspect_schema(999)["compatible"] is False

    dummy_path = tmp_settings / "dummy"
    assert participant.migrate(dummy_path, participant.schema_version) == dummy_path

    with pytest.raises(AppError):
        participant.migrate(dummy_path, 999)

    assert participant.build_identity_map(dummy_path, dummy_path) == {}


def test_retention_policy_crud(tmp_settings: Path) -> None:
    # 1. Default policy
    policy = backup_retention.get_retention_policy("default")
    assert policy["retentionPolicyId"] == "default"

    # 2. Put custom policy
    custom = backup_retention.put_retention_policy(
        "custom_pol",
        {
            "keepDaily": 7,
            "keepWeekly": 4,
            "keepMonthly": 12,
            "keepYearly": 1,
            "trashTtlSeconds": 86400,
        },
    )
    assert custom["keepDaily"] == 7

    # 3. List policies
    policies = backup_retention.list_retention_policies()
    assert any(p["retentionPolicyId"] == "custom_pol" for p in policies)


def test_restore_hold_digests() -> None:
    d1 = "a" * 64
    d2 = "b" * 64
    hold_data = {
        "holdKey": "hold_key_1",
        "objectDigest": d1,
        "objects": [{"digest": d2}],
        "expiresAt": "2099-01-01T00:00:00Z",
    }
    objects = {
        "holds/restore/h1.json": json.dumps(hold_data).encode("utf-8"),
    }

    class MockHoldStore:
        def list_objects(self, prefix: str, *, cursor: str | None = None, limit: int = 1000) -> ListPage:
            return ListPage(
                objects=(ObjectMeta(key="holds/restore/h1.json", size=len(objects["holds/restore/h1.json"]), etag="e1"),),
                cursor=None,
            )

        def get_bytes(self, key: str) -> bytes:
            return objects[key]

    digests = backup_retention._restore_hold_digests(MockHoldStore())
    assert d1 in digests
    assert d2 in digests

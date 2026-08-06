from __future__ import annotations

import json
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_policies


RECIPIENT_A = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"
RECIPIENT_B = "age1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa0"


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "name": "Nightly backup",
        "enabled": True,
        "schedule": {"cron": "0 3 * * *", "timezone": "Asia/Singapore"},
        "protection": {"mode": "age-recipient", "recipients": [RECIPIENT_A]},
        "targetId": "managed-local",
    }
    payload.update(overrides)
    return payload


def test_create_policy_persists_validated_document(tmp_settings: Path) -> None:
    policy = backup_policies.create_policy(_payload())
    assert policy["policyId"].startswith("policy_")
    assert policy["schemaVersion"] == 1
    assert policy["schedule"]["misfirePolicy"] == "skip"
    assert policy["schedule"]["catchupWindowSeconds"] == 86400
    assert policy["scope"] == {
        "mode": "full",
        "projectIds": [],
        "includeHistory": True,
        "includeExternalState": True,
        "coveragePolicy": "strict",
    }
    assert policy["frontendMirror"] == {"mode": "best-effort", "maxAgeSeconds": 3600}
    assert policy["retry"] == {"maxAttempts": 3, "initialBackoffSeconds": 60, "maxBackoffSeconds": 900}
    stored = json.loads((tmp_settings / ".backup-policies" / f"{policy['policyId']}.json").read_text(encoding="utf-8"))
    assert stored["policyId"] == policy["policyId"]
    assert stored["createdAt"] == policy["createdAt"]


def test_create_policy_rejects_passphrase_protection(tmp_settings: Path) -> None:
    with pytest.raises(AppError, match="age-recipient|passphrase"):
        backup_policies.create_policy(_payload(protection={"mode": "passphrase"}))


def test_create_policy_rejects_unknown_protection_mode(tmp_settings: Path) -> None:
    with pytest.raises(AppError):
        backup_policies.create_policy(_payload(protection={"mode": "none"}))


def test_create_policy_validates_recipients(tmp_settings: Path) -> None:
    with pytest.raises(AppError):
        backup_policies.create_policy(_payload(protection={"mode": "age-recipient", "recipients": []}))
    with pytest.raises(AppError):
        backup_policies.create_policy(_payload(protection={"mode": "age-recipient", "recipients": ["not-an-age-key"]}))
    with pytest.raises(AppError):
        backup_policies.create_policy(_payload(protection={"mode": "age-recipient", "recipients": [f"age1{i:058d}" for i in range(17)]}))
    policy = backup_policies.create_policy(_payload(protection={"mode": "age-recipient", "recipients": [RECIPIENT_A, RECIPIENT_A]}))
    assert policy["protection"]["recipients"] == [RECIPIENT_A]


@pytest.mark.parametrize(
    "field",
    (
        {"name": "contains AGE-SECRET-KEY-1PRIVATE"},
        {"name": "redis://default:secret@host:6379/0"},
        {"schedule": {"cron": "0 3 * * *", "timezone": "Asia/Singapore", "note": "bearer abc"}},
    ),
)
def test_create_policy_rejects_secret_markers(tmp_settings: Path, field: dict[str, object]) -> None:
    with pytest.raises(AppError, match="must not contain"):
        backup_policies.create_policy(_payload(**field))


def test_create_policy_validates_schedule(tmp_settings: Path) -> None:
    with pytest.raises(AppError):
        backup_policies.create_policy(_payload(schedule={"cron": "not a cron", "timezone": "UTC"}))
    with pytest.raises(AppError):
        backup_policies.create_policy(_payload(schedule={"cron": "0 3 * * *", "timezone": "Mars/Olympus"}))
    with pytest.raises(AppError):
        backup_policies.create_policy(_payload(schedule={"cron": "0 3 * * *"}))
    with pytest.raises(AppError):
        backup_policies.create_policy(_payload(schedule={"cron": "0 3 * * *", "timezone": "UTC", "misfirePolicy": "explode"}))
    with pytest.raises(AppError):
        backup_policies.create_policy(_payload(schedule={"cron": "0 3 * * *", "timezone": "UTC", "catchupWindowSeconds": 10}))


def test_create_policy_validates_scope_and_mirror(tmp_settings: Path) -> None:
    with pytest.raises(AppError):
        backup_policies.create_policy(_payload(scope={"mode": "project", "projectIds": []}))
    with pytest.raises(AppError):
        backup_policies.create_policy(_payload(scope={"mode": "weird"}))
    with pytest.raises(AppError):
        backup_policies.create_policy(_payload(scope={"mode": "project", "projectIds": ["bad id!"]}))
    with pytest.raises(AppError):
        backup_policies.create_policy(_payload(frontendMirror={"mode": "sometimes"}))
    policy = backup_policies.create_policy(
        _payload(scope={"mode": "project", "projectIds": ["proj_1"], "coveragePolicy": "best-effort"}, frontendMirror={"mode": "required", "profileId": "mirror_main", "maxAgeSeconds": 600})
    )
    assert policy["scope"]["projectIds"] == ["proj_1"]
    assert policy["frontendMirror"]["profileId"] == "mirror_main"


def test_create_policy_validates_target_and_retry(tmp_settings: Path) -> None:
    with pytest.raises(AppError):
        backup_policies.create_policy(_payload(targetId="/mnt/backup"))
    with pytest.raises(AppError):
        backup_policies.create_policy(_payload(retry={"maxAttempts": 99}))
    with pytest.raises(AppError):
        backup_policies.create_policy(_payload(retry={"initialBackoffSeconds": 100, "maxBackoffSeconds": 10}))
    policy = backup_policies.create_policy(_payload(targetId="target_usb1", retry={"maxAttempts": 5, "initialBackoffSeconds": 30, "maxBackoffSeconds": 300}))
    assert policy["targetId"] == "target_usb1"


def test_update_policy_merges_sections_and_keeps_identity(tmp_settings: Path) -> None:
    policy = backup_policies.create_policy(_payload())
    updated = backup_policies.update_policy(policy["policyId"], {"enabled": False, "schedule": {"cron": "0 4 * * *", "timezone": "UTC"}})
    assert updated["policyId"] == policy["policyId"]
    assert updated["createdAt"] == policy["createdAt"]
    assert updated["enabled"] is False
    assert updated["schedule"]["cron"] == "0 4 * * *"
    assert updated["protection"]["recipients"] == [RECIPIENT_A]
    reloaded = backup_policies.get_policy(policy["policyId"])
    assert reloaded["schedule"]["timezone"] == "UTC"


def test_update_policy_validates_merged_result(tmp_settings: Path) -> None:
    policy = backup_policies.create_policy(_payload())
    with pytest.raises(AppError):
        backup_policies.update_policy(policy["policyId"], {"protection": {"mode": "passphrase"}})
    with pytest.raises(AppError):
        backup_policies.update_policy(policy["policyId"], "not-a-dict")  # type: ignore[arg-type]


def test_get_list_and_delete_policy(tmp_settings: Path) -> None:
    assert backup_policies.list_policies() == []
    first = backup_policies.create_policy(_payload(name="one"))
    second = backup_policies.create_policy(_payload(name="two", enabled=False))
    names = sorted(item["name"] for item in backup_policies.list_policies())
    assert names == ["one", "two"]
    with pytest.raises(AppError) as missing:
        backup_policies.get_policy("policy_missing")
    assert missing.value.status == 404
    with pytest.raises(AppError):
        backup_policies.get_policy("bad id")
    result = backup_policies.delete_policy(first["policyId"])
    assert result == {"deleted": True, "policyId": first["policyId"]}
    assert [item["policyId"] for item in backup_policies.list_policies()] == [second["policyId"]]


def test_enabled_policies_and_active_recipients(tmp_settings: Path) -> None:
    backup_policies.create_policy(_payload(name="a"))
    backup_policies.create_policy(_payload(name="b", enabled=False, protection={"mode": "age-recipient", "recipients": [RECIPIENT_B]}))
    backup_policies.create_policy(_payload(name="c", protection={"mode": "age-recipient", "recipients": [RECIPIENT_B]}))
    enabled = backup_policies.enabled_policies()
    assert {item["name"] for item in enabled} == {"a", "c"}
    assert backup_policies.active_recipients() == (RECIPIENT_A, RECIPIENT_B) if RECIPIENT_A < RECIPIENT_B else (RECIPIENT_B, RECIPIENT_A)


def test_recipient_set_digest_is_order_independent() -> None:
    first = backup_policies.recipient_set_digest([RECIPIENT_A, RECIPIENT_B])
    second = backup_policies.recipient_set_digest([RECIPIENT_B, RECIPIENT_A, RECIPIENT_A])
    assert first == second
    assert len(first) == 64


def test_restore_projection_disables_and_unbinds(tmp_settings: Path) -> None:
    policy = backup_policies.create_policy(_payload(targetId="target_usb1"))
    policy["lastRunAt"] = "2026-01-01T00:00:00Z"
    policy["lease"] = {"owner": "worker-1"}
    projected = backup_policies.restore_projection(policy)
    assert projected["enabled"] is False
    assert projected["targetId"] == "unbound"
    assert "lastRunAt" not in projected and "lease" not in projected
    assert projected["protection"]["recipients"] == [RECIPIENT_A]


def test_list_policies_skips_corrupt_files(tmp_settings: Path) -> None:
    policy = backup_policies.create_policy(_payload())
    (tmp_settings / ".backup-policies" / "policy_corrupt.json").write_text("{not json", encoding="utf-8")
    (tmp_settings / ".backup-policies" / "other.json").write_text("{}", encoding="utf-8")
    assert [item["policyId"] for item in backup_policies.list_policies()] == [policy["policyId"]]

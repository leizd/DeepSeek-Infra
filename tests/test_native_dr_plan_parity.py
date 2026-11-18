from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import backup_dr_ledger
from deepseek_infra.web.server import create_server


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.settings.auth.token}",
        "X-DeepSeek-Client": "test",
    }


def test_native_dr_plan_oracle_contract_and_ranking(tmp_settings: Path) -> None:
    policies_dir = tmp_settings / ".backup-policies"
    policies_dir.mkdir(parents=True, exist_ok=True)
    (policies_dir / "pol_pl.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "policyId": "pol_pl",
                "name": "pl",
                "enabled": True,
                "targetId": "target_p",
                "replication": {
                    "enabled": True,
                    "targets": [
                        {"targetId": "target_broken", "mode": "required"},
                        {"targetId": "target_aud", "mode": "best-effort"},
                        {"targetId": "target_r2", "mode": "best-effort"},
                    ],
                    "minCommittedCopies": 2,
                },
                "protection": {
                    "mode": "age-recipient",
                    "recipients": ["age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"],
                },
                "schedule": {
                    "cron": "0 3 * * *",
                    "timezone": "UTC",
                    "misfirePolicy": "skip",
                    "catchupWindowSeconds": 86400,
                    "jitterSeconds": 0,
                },
                "scope": {"mode": "full", "projectIds": []},
                "frontendMirror": {"mode": "best-effort"},
                "retentionPolicyId": "default",
                "retry": {"maxAttempts": 3, "initialBackoffSeconds": 60, "maxBackoffSeconds": 900},
                "incremental": {"mode": "off"},
                "recoveryObjectives": {},
                "recoveryDrill": {"enabled": False},
                "createdAt": "2026-08-15T00:00:00Z",
                "updatedAt": "2026-08-15T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    targets_dir = tmp_settings / ".backup-targets"
    targets_dir.mkdir(parents=True, exist_ok=True)
    for tid, kind in (
        ("target_p", "s3"),
        ("target_r2", "filesystem"),
        ("target_broken", "filesystem"),
        ("target_aud", "s3"),
    ):
        (targets_dir / f"{tid}.json").write_text(
            json.dumps({"targetId": tid, "kind": kind}),
            encoding="utf-8",
        )
        backup_dr_ledger.record_recovery_point(
            target_id=tid,
            policy_id="pol_pl",
            backup_id="bk_1",
            committed_at="2026-08-15T00:00:00Z",
            logical_bytes=1024,
            recoverable=True,
            storage_protocol="object-set-v1",
            metadata={"objectSetDigest": "osd_1"},
        )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="target_p",
        policy_id="pol_pl",
        backup_id="bk_1",
        object_set_digest="osd_1",
        committed_at="2026-08-15T00:00:00Z",
        recoverable=True,
        role="primary",
        mode="required",
    )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="target_r2",
        policy_id="pol_pl",
        backup_id="bk_1",
        object_set_digest="osd_1",
        committed_at="2026-08-15T00:00:00Z",
        recoverable=True,
        role="replica",
        mode="best-effort",
    )
    backup_dr_ledger.record_target_evidence(
        target_id="target_p",
        observed_at="2026-08-15T00:00:00Z",
        scheduled_ready=True,
        status="ok",
    )
    backup_dr_ledger.record_target_evidence(
        target_id="target_r2",
        observed_at="2026-08-15T00:00:00Z",
        scheduled_ready=True,
        status="ok",
    )
    backup_dr_ledger.record_target_evidence(
        target_id="target_broken",
        observed_at="2026-08-15T00:00:00Z",
        scheduled_ready=False,
        status="error",
    )
    backup_dr_ledger.record_target_evidence(
        target_id="target_aud",
        observed_at="2026-08-15T00:00:00Z",
        scheduled_ready=True,
        status="ok",
    )
    backup_dr_ledger.record_audit_evidence(
        target_id="target_aud",
        status="failed",
        result="failed",
        observed_at="2026-08-15T00:00:00Z",
    )
    backup_dr_ledger.record_scrub_evidence(
        target_id="target_r2",
        backup_id="bk_1",
        policy_id="pol_pl",
        observed_at="2026-08-15T01:00:00Z",
        result="success",
    )
    backup_dr_ledger.record_drill_evidence(
        target_id="target_r2",
        backup_id="bk_1",
        policy_id="pol_pl",
        observed_at="2026-08-15T01:00:00Z",
        result="success",
    )

    server, _ = create_server(0, host="127.0.0.1")
    client = TestClient(server.app, base_url="http://127.0.0.1", raise_server_exceptions=False)

    bad = client.post("/api/workspace/disaster-recovery/plan", headers=_headers(), json={})
    assert bad.status_code == 400
    assert bad.json()["code"] == "invalid_payload"

    missing = client.post(
        "/api/workspace/disaster-recovery/plan",
        headers=_headers(),
        json={"policyId": "pol_missing"},
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"

    res = client.post(
        "/api/workspace/disaster-recovery/plan",
        headers=_headers(),
        json={"policyId": "pol_pl", "backupId": "bk_1", "preferredTargetId": "target_r2"},
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["schemaVersion"] == 1
    assert payload["policyId"] == "pol_pl"
    assert payload["selectedTargetId"] == "target_r2"
    assert payload["remoteIo"] is False
    assert payload["source"] == "dr-evidence-ledger"
    assert payload["maxFailovers"] == 3
    assert payload["logicalRecoveryPoint"]["backupId"] == "bk_1"
    assert payload["logicalRecoveryPoint"]["objectSetDigest"] == "osd_1"
    assert payload["logicalRecoveryPoint"]["logicalBytes"] == 1024
    ordered = payload["orderedCandidates"]
    assert [c["targetId"] for c in ordered] == ["target_r2", "target_p"]
    assert ordered[0]["rankKey"] == [0, 0, 1, 0, 0, 999999, "target_r2"]
    assert payload["rejectedCandidates"] == [
        {"targetId": "target_broken", "reasons": ["target-known-broken"]},
        {"targetId": "target_aud", "reasons": ["audit-known-failed"]},
    ]

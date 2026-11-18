from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import backup_dr_ledger, backup_publish, backup_targets, backups
from deepseek_infra.web.server import create_server


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.settings.auth.token}",
        "X-DeepSeek-Client": "test",
    }


def _signed_commit(fields: dict[str, object]) -> dict[str, object]:
    payload = dict(fields)
    payload["commitHash"] = backup_publish._commit_hash(payload)  # noqa: SLF001
    return payload


def test_native_dr_audit_and_resume_oracle_contract(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_temp = tmp_settings / ".sys-temp"
    fake_temp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))

    rem_root = tmp_settings / "remote-fs"
    rem_root.mkdir(parents=True, exist_ok=True)
    registered = backup_targets.init_target(rem_root, label="Remote FS")
    target_id = str(registered["targetId"])
    commits_dir = rem_root / "commits" / "pol_rem"
    receipts_dir = rem_root / "receipts"
    control_dir = rem_root / "control"
    commits_dir.mkdir(parents=True, exist_ok=True)
    receipts_dir.mkdir(parents=True, exist_ok=True)
    control_dir.mkdir(parents=True, exist_ok=True)

    r1 = (
        json.dumps(
            {
                "backupId": "bk_rem_1",
                "policyId": "pol_rem",
                "targetId": target_id,
                "snapshotKind": "full",
                "chainLength": 1,
                "size": 200,
                "logicalBytes": 500,
                "createdAt": "2026-08-15T01:00:00Z",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    (receipts_dir / "bk_rem_1.json").write_bytes(r1)
    c1 = _signed_commit(
        {
            "schemaVersion": 4,
            "backupId": "bk_rem_1",
            "policyId": "pol_rem",
            "targetGeneration": 1,
            "previousCommitHash": "0" * 64,
            "committedAt": "2026-08-15T01:00:00Z",
            "receiptDigest": hashlib.sha256(r1).hexdigest(),
        }
    )
    (commits_dir / "c1.json").write_text(json.dumps(c1), encoding="utf-8")

    r2 = (
        json.dumps(
            {
                "backupId": "bk_rem_2",
                "policyId": "pol_rem",
                "targetId": target_id,
                "snapshotKind": "incremental",
                "parentBackupId": "bk_rem_1",
                "chainLength": 2,
                "size": 300,
                "logicalBytes": 800,
                "createdAt": "2026-08-15T02:00:00Z",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    (receipts_dir / "bk_rem_2.json").write_bytes(r2)
    c2 = _signed_commit(
        {
            "schemaVersion": 4,
            "backupId": "bk_rem_2",
            "policyId": "pol_rem",
            "targetGeneration": 2,
            "previousCommitHash": c1["commitHash"],
            "committedAt": "2026-08-15T02:00:00Z",
            "receiptDigest": hashlib.sha256(r2).hexdigest(),
        }
    )
    (commits_dir / "c2.json").write_text(json.dumps(c2), encoding="utf-8")
    (control_dir / "head.json").write_text(
        json.dumps({"targetGeneration": 2, "latestCommitHash": c2["commitHash"]}),
        encoding="utf-8",
    )

    server, _ = create_server(0, host="127.0.0.1")
    client = TestClient(server.app, base_url="http://127.0.0.1", raise_server_exceptions=False)

    missing_resume = client.post(
        "/api/workspace/disaster-recovery/audit/audit_missing/resume",
        headers=_headers(),
    )
    assert missing_resume.status_code == 404
    assert missing_resume.json()["code"] == "not_found"

    page1 = client.post(
        "/api/workspace/disaster-recovery/audit",
        headers=_headers(),
        json={"targetId": target_id, "pageSize": 1},
    )
    assert page1.status_code == 200, page1.text
    p1_data = page1.json()
    assert p1_data["status"] == "in-progress"
    assert p1_data["cursor"] == "commits/pol_rem/c1.json"
    assert p1_data["recoveryPointsFound"] == 1

    resumed = client.post(
        f"/api/workspace/disaster-recovery/audit/{p1_data['auditId']}/resume",
        headers=_headers(),
    )
    assert resumed.status_code == 200, resumed.text
    r_data = resumed.json()
    assert r_data["status"] == "completed"
    assert r_data["recoveryPointsFound"] == 2
    assert r_data["anomalies"] == []
    assert r_data["targetGeneration"] == 2

    resumed_again = client.post(
        f"/api/workspace/disaster-recovery/audit/{p1_data['auditId']}/resume",
        headers=_headers(),
    )
    assert resumed_again.status_code == 200
    assert resumed_again.json()["resumed"] is False

    pt, chain = backup_dr_ledger.get_latest_recoverable_point(target_id, "pol_rem")
    assert pt is not None
    assert pt["backupId"] == "bk_rem_2"
    assert len(chain) == 2

    ml_commits = backups.BACKUP_DIR / "commits" / "pol_ml"
    ml_commits.mkdir(parents=True, exist_ok=True)
    (ml_commits / "bad.json").write_text("bad", encoding="utf-8")
    ml_res = client.post(
        "/api/workspace/disaster-recovery/audit",
        headers=_headers(),
        json={"targetId": "managed-local"},
    )
    assert ml_res.status_code == 200
    assert len(ml_res.json()["anomalies"]) == 1

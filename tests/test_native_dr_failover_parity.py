from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import backup_targets
from deepseek_infra.web.server import create_server


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.settings.auth.token}",
        "X-DeepSeek-Client": "test",
    }


def test_native_dr_failover_oracle_contract(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_temp = tmp_settings / ".sys-temp"
    fake_temp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))

    ml_hold_dir = tmp_settings / ".backups" / "holds" / "restore"
    ml_hold_dir.mkdir(parents=True, exist_ok=True)
    old_hold = ml_hold_dir / "restore_fo1.json"
    old_hold.write_text("{}", encoding="utf-8")

    t2_root = tmp_settings / "target_r2"
    t2_root.mkdir(parents=True, exist_ok=True)
    t2_rec = backup_targets.init_target(t2_root, label="Failover Replica")
    t2_id = str(t2_rec["targetId"])

    session_dir = tmp_settings / ".restore-staging" / "restore_fo1"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / "remote-fetch.json"
    session_file.write_text(
        json.dumps(
            {
                "restoreId": "restore_fo1",
                "phase": "fetching",
                "targetId": "managed-local",
                "activeSourceTargetId": "managed-local",
                "backupId": "bk_1",
                "storageProtocol": "object-set-v1",
                "holdKeys": ["holds/restore/restore_fo1.json"],
                "downloadedBytes": 128,
                "attemptedSourceTargets": ["managed-local"],
                "failoverCount": 0,
                "recoveryPlan": {
                    "maxFailovers": 3,
                    "orderedCandidates": [
                        {"targetId": "managed-local"},
                        {"targetId": t2_id},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    server, _ = create_server(0, host="127.0.0.1")
    client = TestClient(server.app, base_url="http://127.0.0.1", raise_server_exceptions=False)

    bad_reason = client.post(
        "/api/workspace/disaster-recovery/failover/restore_fo1",
        headers=_headers(),
        json={"reason": "identity-error"},
    )
    assert bad_reason.status_code == 409

    ok = client.post(
        "/api/workspace/disaster-recovery/failover/restore_fo1",
        headers=_headers(),
        json={"reason": "network-unavailable"},
    )
    assert ok.status_code == 200, ok.text
    payload = ok.json()
    assert payload["activeSourceTargetId"] == t2_id
    assert payload["previousSourceTargetId"] == "managed-local"
    assert payload["failoverCount"] == 1
    assert payload["phase"] == "fetching-controls"
    assert payload["newHoldBeforeOldRelease"] is True
    assert (t2_root / "holds" / "restore" / "restore_fo1-fo1.json").is_file()
    assert not old_hold.exists()

    exhausted = client.post(
        "/api/workspace/disaster-recovery/failover/restore_fo1",
        headers=_headers(),
        json={"reason": "network-unavailable"},
    )
    assert exhausted.status_code == 409

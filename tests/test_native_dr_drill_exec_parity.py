from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from deepseek_infra.core import config
from deepseek_infra.infra.native_runtime.authority import PythonWriterMechanicallyDeniedError
from deepseek_infra.infra.workspace import backup_policies, backup_recovery_drill
from deepseek_infra.web.server import create_server


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.settings.auth.token}",
        "X-DeepSeek-Client": "test",
    }


def test_native_dr_drill_exec_oracle_contract_and_writer_denial(
    tmp_settings: Path,
    monkeypatch,
) -> None:
    skip_pol = backup_policies.create_policy(
        {
            "policyId": "pol_skip_1",
            "name": "Skip Drill Policy",
            "targetId": "managed-local",
            "recoveryDrill": {"enabled": False},
        }
    )
    blk_pol = backup_policies.create_policy(
        {
            "policyId": "pol_blk_1",
            "name": "Block Drill Policy",
            "targetId": "managed-local",
            "recoveryDrill": {"enabled": True, "provider": "memory", "credentialRef": "cred_missing"},
        }
    )

    server, _ = create_server(0, host="127.0.0.1")
    client = TestClient(server.app, base_url="http://127.0.0.1", raise_server_exceptions=False)

    res_bad = client.post(
        "/api/workspace/disaster-recovery/drills",
        headers=_headers(),
        json={"restoreId": "restore_1", "extra": 1},
    )
    assert res_bad.status_code == 400

    res_skip = client.post(
        f"/api/workspace/disaster-recovery/drills/schedule/{skip_pol['policyId']}",
        headers=_headers(),
    )
    assert res_skip.status_code == 200
    assert res_skip.json()["status"] == "skipped"

    res_blk = client.post(
        f"/api/workspace/disaster-recovery/drills/schedule/{blk_pol['policyId']}",
        headers=_headers(),
    )
    assert res_blk.status_code == 200
    assert res_blk.json()["status"] == "blocked"
    assert res_blk.json()["reason"] == "unlock-required"

    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    for call in (
        lambda: backup_recovery_drill.run_recovery_drill("restore_missing"),
        lambda: backup_recovery_drill.execute_scheduled_drill(str(blk_pol["policyId"])),
    ):
        try:
            call()
        except PythonWriterMechanicallyDeniedError:
            pass
        else:
            raise AssertionError("Expected PythonWriterMechanicallyDeniedError under python_disabled")

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from deepseek_infra.core import config
from deepseek_infra.infra.native_runtime.authority import PythonWriterMechanicallyDeniedError
from deepseek_infra.infra.workspace import backup_dr_readiness
from deepseek_infra.web.server import create_server


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.settings.auth.token}",
        "X-DeepSeek-Client": "test",
    }


def test_native_dr_drill_run_oracle_contract_and_writer_denial(
    tmp_settings: Path,
    monkeypatch,
) -> None:
    proj = tmp_settings / ".projects" / "proj_1"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "note.txt").write_bytes(b"hello drill")

    server, _ = create_server(0, host="127.0.0.1")
    client = TestClient(server.app, base_url="http://127.0.0.1", raise_server_exceptions=False)

    res = client.post(
        "/api/workspace/disaster-recovery/drills/run",
        headers=_headers(),
        json={"backupId": "bk_drill_1", "targetId": "managed-local"},
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["status"] == "success"
    assert payload["backupId"] == "bk_drill_1"
    assert payload["testedBackupId"] == "bk_drill_1"
    assert payload["targetId"] == "managed-local"
    assert payload["bytesRestored"] == 11
    proof = payload["proof"]
    assert proof["schema"] == "dr-readiness-proof-v1"
    assert proof["workspaceDigestBefore"] == proof["workspaceDigestAfter"]
    assert proof["cleanupCompleted"] is True
    assert (tmp_settings / ".restore-staging" / payload["drillId"] / "drill-result.json").is_file()

    # Under python_disabled, Python writer refuses to run DR drill into restore_staging_store
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    try:
        backup_dr_readiness.run_dr_drill(backup_id="bk_drill_2", target_id="managed-local")
    except PythonWriterMechanicallyDeniedError:
        pass
    else:
        raise AssertionError("Expected PythonWriterMechanicallyDeniedError under python_disabled")

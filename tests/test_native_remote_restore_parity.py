from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from deepseek_infra.core import config
from deepseek_infra.infra.native_runtime.authority import PythonWriterMechanicallyDeniedError
from deepseek_infra.infra.workspace import backup_remote_restore
from deepseek_infra.web.server import create_server


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.settings.auth.token}",
        "X-DeepSeek-Client": "test",
    }


def test_native_remote_restore_oracle_contract_and_writer_denial(
    tmp_settings: Path,
    monkeypatch,
) -> None:
    server, _ = create_server(0, host="127.0.0.1")
    client = TestClient(server.app, base_url="http://127.0.0.1", raise_server_exceptions=False)

    # Validation contract on missing backup/session
    res_missing = client.post(
        "/api/workspace/restores/from-target",
        headers=_headers(),
        json={"targetId": "managed-local", "backupId": "bk_none"},
    )
    assert res_missing.status_code == 404

    res_pre_bad = client.post(
        "/api/workspace/restores/restore_missing/preflight",
        headers=_headers(),
        json={"freeDiskBytes": 123},
    )
    assert res_pre_bad.status_code == 400

    res_mat_missing = client.post(
        "/api/workspace/restores/restore_missing/materialize",
        headers=_headers(),
        json={},
    )
    assert res_mat_missing.status_code == 404

    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    for call in (
        lambda: backup_remote_restore.create_restore_from_target(target_id="managed-local", backup_id="bk_none"),
        lambda: backup_remote_restore.preview_restore_from_target(
            target_id="managed-local",
            backup_id="bk_none",
            selection={"contributors": ["projects"]},
        ),
        lambda: backup_remote_restore.fetch_restore_session("restore_missing"),
        lambda: backup_remote_restore.preflight_restore_session("restore_missing"),
        lambda: backup_remote_restore.materialize_federated_restore("restore_missing"),
    ):
        try:
            call()
        except PythonWriterMechanicallyDeniedError:
            pass
        else:
            raise AssertionError("Expected PythonWriterMechanicallyDeniedError under python_disabled")

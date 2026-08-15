"""Targeted test coverage boosters for backup sessions, identity generation, and session lifecycle."""

from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_crypto, backups


def test_backup_sessions_and_identity(tmp_settings: Path) -> None:
    # 1. Generate recovery identity (if crypto helper is available)
    if bool(backup_crypto.capabilities().get("encryptedBackupAvailable")):
        identity_res = backups.generate_recovery_identity()
        assert identity_res["ok"] is True
        assert "identity" in identity_res
        assert "recipient" in identity_res
        assert identity_res["displayedOnce"] is True
    else:
        with pytest.raises(AppError):
            backups.generate_recovery_identity()

    # 2. Create session without encryption
    session = backups.create_session({
        "mode": "full",
        "requiresFrontendState": False,
    })
    assert session["ok"] is True
    b_id = str(session["backupId"])
    assert session["phase"] == "preparing"

    # 3. Get session
    retrieved = backups.get_session(b_id)
    assert retrieved["ok"] is True
    assert retrieved["backupId"] == b_id

    # 4. Put session secret error handling
    with pytest.raises(AppError):
        backups.put_session_secret("invalid_prefix", {"kind": "passphrase", "secret": "pass"})

    # 5. Delete session
    deleted = backups.delete_backup(b_id)
    assert deleted is True

    with pytest.raises(AppError):
        backups.get_session(b_id)

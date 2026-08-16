"""Targeted test coverage boosters for server multipart helpers and recovery credentials."""

from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_recovery_credential
from deepseek_infra.web import server


def test_server_multipart_validation(tmp_settings: Path) -> None:
    # 1. Non-module candidate
    assert server.multipart_module_issue(None) is not None
    assert server.supported_multipart_module(None) is False

    # 2. Object missing required methods
    class FakeIncomplete:
        pass

    assert "missing callable" in str(server.multipart_module_issue(FakeIncomplete()))

    # 3. Object with bad signature
    class FakeBadSig:
        def parse_options_header(self) -> None:
            pass

        def MultipartParser(self, a: int) -> None:
            pass

    assert "missing parameters" in str(server.multipart_module_issue(FakeBadSig()))


def test_recovery_credentials_lifecycle(tmp_settings: Path) -> None:
    # 1. Zeroize tests
    buf = bytearray(b"secret_passphrase")
    assert any(b != 0 for b in buf)
    backup_recovery_credential.zeroize(buf)
    assert all(b == 0 for b in buf)
    backup_recovery_credential.zeroize(None)
    backup_recovery_credential.zeroize_bytearray(None)

    # 2. InMemoryCredentialProvider
    prov = backup_recovery_credential.InMemoryCredentialProvider()
    assert prov.has_credential("ref1") is False

    with pytest.raises(AppError):
        prov.acquire_secret_bytes("ref1")

    prov.set_credential("ref1", "my_secret_string")
    assert prov.has_credential("ref1") is True

    with prov.open_secret("ref1") as secret:
        assert secret == b"my_secret_string"

    prov.set_secret("ref2", b"bytes_secret")
    assert prov.has_credential("ref2") is True

    # Clear specific ref
    prov.clear("ref1")
    assert prov.has_credential("ref1") is False
    assert prov.has_credential("ref2") is True

    # Clear all
    prov.clear()
    assert prov.has_credential("ref2") is False

    # 3. Direct secret in acquire_recovery_secret context
    with backup_recovery_credential.acquire_recovery_secret(direct_secret="direct_password") as s:
        assert s == b"direct_password"

    with backup_recovery_credential.acquire_recovery_secret(direct_secret=b"direct_bytes") as s:
        assert s == b"direct_bytes"

    # Missing cred ref raises
    with pytest.raises(AppError) as exc_info:
        with backup_recovery_credential.acquire_recovery_secret(credential_ref=""):
            pass
    assert "unlock-required" in str(exc_info.value)

    # 4. Custom provider registration
    backup_recovery_credential.register_provider("custom_prov", prov)
    assert backup_recovery_credential.get_provider("custom_prov") is prov

    with pytest.raises(AppError):
        backup_recovery_credential.get_provider("nonexistent_prov")

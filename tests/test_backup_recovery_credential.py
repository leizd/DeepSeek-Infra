"""Unit tests for RecoveryCredentialProvider abstraction (Recovery Assurance Gate G)."""

from __future__ import annotations

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_recovery_credential


def test_in_memory_credential_provider() -> None:
    provider = backup_recovery_credential.InMemoryCredentialProvider()
    secret_bytes = bytearray(b"AGE-SECRET-KEY-TEST1234567890")

    provider.set_secret("prod_ref", secret_bytes)
    assert provider.has_credential("prod_ref")
    assert not provider.has_credential("nonexistent")

    with backup_recovery_credential.acquire_recovery_secret("prod_ref", provider=provider) as acquired:
        assert acquired == bytearray(b"AGE-SECRET-KEY-TEST1234567890")

    # Acquired should have been zeroized upon context exit
    assert all(b == 0 for b in acquired)

    # Overwrite secret with str
    provider.set_credential("prod_ref", "NEW-SECRET-STRING")
    with backup_recovery_credential.acquire_recovery_secret("prod_ref", provider=provider) as acquired2:
        assert acquired2 == bytearray(b"NEW-SECRET-STRING")

    # Clean up single ref
    provider.clear("prod_ref")
    assert not provider.has_credential("prod_ref")
    with pytest.raises(AppError) as exc_info:
        with backup_recovery_credential.acquire_recovery_secret("prod_ref", provider=provider):
            pass
    assert "not found" in str(exc_info.value)

    # Set multiple and clear all
    provider.set_secret("k1", b"v1")
    provider.set_secret("k2", b"v2")
    provider.clear()
    assert not provider.has_credential("k1")
    assert not provider.has_credential("k2")


def test_missing_credential_provider_blocks() -> None:
    # Clear default provider
    backup_recovery_credential.set_default_credential_provider(None)

    with pytest.raises(AppError) as exc_info:
        with backup_recovery_credential.acquire_recovery_secret("ref_123"):
            pass
    assert "No RecoveryCredentialProvider" in str(exc_info.value)

    # Restore default
    mem = backup_recovery_credential.InMemoryCredentialProvider()
    backup_recovery_credential.set_default_credential_provider(mem)
    backup_recovery_credential.register_provider("custom_prov", mem)
    assert backup_recovery_credential.get_provider("custom_prov") is mem


def test_acquire_direct_secret() -> None:
    # Direct bytes
    with backup_recovery_credential.acquire_recovery_secret(direct_secret=b"direct_secret_bytes") as acquired:
        assert acquired == bytearray(b"direct_secret_bytes")
    assert all(b == 0 for b in acquired)

    # Direct str
    with backup_recovery_credential.acquire_recovery_secret(direct_secret="direct_secret_str") as acquired:
        assert acquired == bytearray(b"direct_secret_str")
    assert all(b == 0 for b in acquired)

    # Missing both ref and direct
    with pytest.raises(AppError) as exc_info:
        with backup_recovery_credential.acquire_recovery_secret(None):
            pass
    assert "unlock-required" in str(exc_info.value)


def test_zeroize_helper() -> None:
    buf = bytearray(b"sensitive_password_bytes")
    backup_recovery_credential.zeroize_bytearray(buf)
    assert all(b == 0 for b in buf)
    assert len(buf) == 24

    # None handling
    backup_recovery_credential.zeroize(None)

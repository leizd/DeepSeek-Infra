"""Unattended recovery credential provider abstraction (4.5.1 Gate G).

Allows automated recovery drills and verification routines to securely obtain
decryption keys without persisting plaintext secrets or passphrase strings to
disk, environment files, or logs. Secrets are returned as zeroizable bytearrays.
"""

from __future__ import annotations

import abc
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from deepseek_infra.core.errors import AppError, ErrorCode


def zeroize(buffer: bytearray | None) -> None:
    """Explicitly wipe memory buffer containing secrets."""
    if buffer is None:
        return
    for i in range(len(buffer)):
        buffer[i] = 0


def zeroize_bytearray(buffer: bytearray | None) -> None:
    zeroize(buffer)


class RecoveryCredentialProvider(abc.ABC):
    """Abstract interface for unattended recovery credential retrieval."""

    @abc.abstractmethod
    def has_credential(self, credential_ref: str) -> bool:
        """Check whether provider holds credentials for given ref."""
        ...

    @abc.abstractmethod
    def acquire_secret_bytes(self, credential_ref: str) -> bytearray:
        """Return ephemeral secret as mutable bytearray to be zeroized by caller."""
        ...

    @contextmanager
    def open_secret(self, credential_ref: str) -> Iterator[bytearray]:
        """Context manager guaranteeing secret zeroization upon exit."""
        secret = self.acquire_secret_bytes(credential_ref)
        try:
            yield secret
        finally:
            zeroize(secret)


class InMemoryCredentialProvider(RecoveryCredentialProvider):
    """In-memory transient credential store for tests and interactive runs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._secrets: dict[str, bytearray] = {}

    def set_credential(self, credential_ref: str, secret_data: bytes | bytearray | str) -> None:
        if isinstance(secret_data, str):
            raw = bytearray(secret_data.encode("utf-8"))
        else:
            raw = bytearray(secret_data)
        with self._lock:
            # Wipe any existing secret under key
            if credential_ref in self._secrets:
                zeroize(self._secrets[credential_ref])
            self._secrets[credential_ref] = raw

    def set_secret(self, credential_ref: str, secret_data: bytes | bytearray | str) -> None:
        self.set_credential(credential_ref, secret_data)

    def clear(self, credential_ref: str | None = None) -> None:
        with self._lock:
            if credential_ref is not None:
                if credential_ref in self._secrets:
                    zeroize(self._secrets[credential_ref])
                    del self._secrets[credential_ref]
            else:
                for buf in self._secrets.values():
                    zeroize(buf)
                self._secrets.clear()

    def has_credential(self, credential_ref: str) -> bool:
        with self._lock:
            return credential_ref in self._secrets

    def acquire_secret_bytes(self, credential_ref: str) -> bytearray:
        with self._lock:
            if credential_ref not in self._secrets:
                raise AppError(
                    f"unlock-required: credential reference '{credential_ref}' not found in in-memory provider",
                    code=ErrorCode.INVALID_REQUEST,
                    status=428,
                )
            # Return a copy so caller can zeroize it independently
            return bytearray(self._secrets[credential_ref])


_PROVIDER_REGISTRY: dict[str, RecoveryCredentialProvider] = {
    "memory": InMemoryCredentialProvider(),
    "default": InMemoryCredentialProvider(),
}
_REGISTRY_LOCK = threading.Lock()


def register_provider(name: str, provider: RecoveryCredentialProvider) -> None:
    with _REGISTRY_LOCK:
        _PROVIDER_REGISTRY[name] = provider


def set_default_credential_provider(provider: RecoveryCredentialProvider | None) -> None:
    with _REGISTRY_LOCK:
        if provider is None:
            _PROVIDER_REGISTRY.pop("default", None)
        else:
            _PROVIDER_REGISTRY["default"] = provider


def get_provider(name: str | None = None) -> RecoveryCredentialProvider:
    provider_name = name or "default"
    with _REGISTRY_LOCK:
        if provider_name not in _PROVIDER_REGISTRY:
            raise AppError(
                f"No RecoveryCredentialProvider: provider '{provider_name}' is not registered",
                code=ErrorCode.INVALID_REQUEST,
                status=428,
            )
        return _PROVIDER_REGISTRY[provider_name]


@contextmanager
def acquire_recovery_secret(
    credential_ref: str | None = None,
    *,
    provider: RecoveryCredentialProvider | None = None,
    provider_name: str | None = None,
    direct_secret: bytes | bytearray | str | None = None,
) -> Iterator[bytearray]:
    """Acquire recovery secret with guaranteed zeroization on exit."""
    if direct_secret is not None:
        if isinstance(direct_secret, str):
            buf = bytearray(direct_secret.encode("utf-8"))
        else:
            buf = bytearray(direct_secret)
        try:
            yield buf
        finally:
            zeroize(buf)
        return

    if not credential_ref:
        raise AppError(
            "unlock-required: no credential reference or key provided for recovery drill",
            code=ErrorCode.INVALID_REQUEST,
            status=428,
        )

    active_provider = provider or get_provider(provider_name)
    with active_provider.open_secret(credential_ref) as secret:
        yield secret

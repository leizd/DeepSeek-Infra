"""Unattended age encryption verified with ephemeral recipients (4.4.4).

Scheduled runs only know the user's public ``age1...`` recipients, so they
cannot decrypt with the user's Recovery Identity to prove a round trip. Instead
each run generates a one-off ephemeral age identity, encrypts to the user
recipients plus the ephemeral recipient, decrypts immediately with the
ephemeral identity, verifies the plaintext, then zeroes and discards it. The
final ciphertext remains unlockable by the user's Recovery Identity; the dead
ephemeral recipient is not a usable backdoor.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_crypto


@dataclass(frozen=True, slots=True)
class UnattendedEncryption:
    ciphertext_sha256: str
    size: int
    creation_verified: bool
    recipients: tuple[str, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def zero_buffer(buffer: bytearray) -> None:
    for index in range(len(buffer)):
        buffer[index] = 0


def scrub_plaintext_file(path: Path) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        return
    try:
        with path.open("r+b") as handle:
            remaining = size
            while remaining > 0:
                chunk = min(remaining, 1024 * 1024)
                handle.write(b"\0" * chunk)
                remaining -= chunk
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass
    finally:
        path.unlink(missing_ok=True)


def encrypt_unattended(
    target: Path,
    write_plaintext: Callable[[BinaryIO], None],
    *,
    recipients: tuple[str, ...] | list[str],
    verify: Callable[[Path], None] | None = None,
) -> UnattendedEncryption:
    user_recipients = tuple(dict.fromkeys(str(item).strip() for item in recipients if str(item).strip()))
    if not user_recipients:
        raise AppError("Unattended encryption requires at least one user recipient", code=ErrorCode.INVALID_PAYLOAD)
    identity = backup_crypto.generate_identity()
    ephemeral_recipient = str(identity.get("recipient") or "")
    ephemeral_identity = bytearray(str(identity.get("identity") or "").encode("utf-8"))
    if not ephemeral_recipient or not ephemeral_identity:
        raise AppError("Unable to generate ephemeral verification identity", code=ErrorCode.INTERNAL, status=500)
    all_recipients = tuple(dict.fromkeys([*user_recipients, ephemeral_recipient]))
    verify_path = target.with_name(f".{target.name}.{os.getpid()}.verify")
    try:
        backup_crypto.encrypt_stream(target, write_plaintext, mode="age-recipient", recipients=all_recipients)
        backup_crypto.decrypt_file(target, verify_path, kind="age-identity", secret=ephemeral_identity)
        if verify is not None:
            verify(verify_path)
    finally:
        scrub_plaintext_file(verify_path)
        zero_buffer(ephemeral_identity)
    return UnattendedEncryption(
        ciphertext_sha256=sha256_file(target),
        size=target.stat().st_size,
        creation_verified=True,
        recipients=all_recipients,
    )

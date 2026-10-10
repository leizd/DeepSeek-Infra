from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_crypto, backup_unattended


@pytest.fixture
def stub_crypto(monkeypatch: pytest.MonkeyPatch) -> None:
    def encrypt_stream(target: Path, write_plaintext: object, *, mode: str, secret: object = None, recipients: tuple[str, ...] = (), cancel_event: object = None) -> None:
        import io

        buffer = io.BytesIO()
        write_plaintext(buffer)  # type: ignore[operator]
        target.write_bytes(b"age:" + bytes(buffer.getbuffer()))

    def decrypt_file(source: Path, target: Path, *, kind: str, secret: bytearray, cancel_event: object = None) -> None:
        assert bytes(secret) == b"AGE-SECRET-KEY-1EPHEMERAL"
        target.write_bytes(source.read_bytes()[4:])

    monkeypatch.setattr(backup_crypto, "encrypt_stream", encrypt_stream)
    monkeypatch.setattr(backup_crypto, "decrypt_file", decrypt_file)
    monkeypatch.setattr(
        backup_crypto,
        "generate_identity",
        lambda: {"identity": "AGE-SECRET-KEY-1EPHEMERAL", "recipient": "age1ephemeral"},
    )


def _write(output: BinaryIO) -> None:
    output.write(b"payload-bytes")


def test_unattended_encryption_round_trip(tmp_path: Path, stub_crypto: None) -> None:
    target = tmp_path / "out.age"
    verified: list[bytes] = []
    result = backup_unattended.encrypt_unattended(
        target,
        lambda output: _write(output),
        recipients=("age1user",),
        verify=lambda path: verified.append(path.read_bytes()),
    )
    assert result.creation_verified is True
    assert result.ciphertext_sha256 == backup_unattended.sha256_file(target)
    assert result.size == target.stat().st_size
    assert result.recipients == ("age1user", "age1ephemeral")
    assert verified == [b"payload-bytes"]
    assert target.read_bytes() == b"age:payload-bytes"
    assert list(tmp_path.glob("*.verify")) == []


def test_unattended_encryption_requires_recipients(tmp_path: Path, stub_crypto: None) -> None:
    with pytest.raises(AppError):
        backup_unattended.encrypt_unattended(tmp_path / "out.age", lambda output: None, recipients=[])


def test_unattended_encryption_verify_failure_propagates(tmp_path: Path, stub_crypto: None) -> None:
    with pytest.raises(AppError, match="boom"):
        backup_unattended.encrypt_unattended(
            tmp_path / "out.age",
            lambda output: _write(output),
            recipients=("age1user",),
            verify=lambda path: (_ for _ in ()).throw(AppError("boom")),
        )
    assert list(tmp_path.glob("*.verify")) == []


def test_unattended_encryption_rejects_bad_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_crypto: None) -> None:
    monkeypatch.setattr(backup_crypto, "generate_identity", lambda: {"identity": "", "recipient": ""})
    with pytest.raises(AppError):
        backup_unattended.encrypt_unattended(tmp_path / "out.age", lambda output: None, recipients=("age1user",))


def test_scrub_plaintext_file_zeroes_and_removes(tmp_path: Path) -> None:
    target = tmp_path / "plain.bin"
    target.write_bytes(b"sensitive" * 1000)
    backup_unattended.scrub_plaintext_file(target)
    assert not target.exists()
    backup_unattended.scrub_plaintext_file(target)


def test_zero_buffer_clears_contents() -> None:
    buffer = bytearray(b"secret")
    backup_unattended.zero_buffer(buffer)
    assert bytes(buffer) == b"\0" * 6

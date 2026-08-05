"""Streaming age helper integration and ephemeral backup secret slots.

Secrets are held only in this process and sent to the Rust helper through a
dedicated inherited anonymous pipe. Workspace bytes use stdin/stdout, so
passphrases and identities never appear in argv, environment variables, files,
logs, traces, or persisted backup metadata.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Literal, cast

from deepseek_infra.core.errors import AppError, ErrorCode

SecretKind = Literal["passphrase", "age-identity"]
MAX_SECRET_BYTES = 64 * 1024
SECRET_TTL_SECONDS = 5 * 60
SECRET_ATTEMPTS = 5


@dataclass(slots=True)
class EphemeralBackupSecret:
    session_id: str
    kind: SecretKind
    value: bytearray
    created_at: float
    expires_at: float
    attempts_remaining: int

    def clear(self) -> None:
        self.value[:] = b"\x00" * len(self.value)


_SLOTS: dict[str, EphemeralBackupSecret] = {}
_FAILURES: dict[str, tuple[int, float]] = {}
_SLOT_LOCK = threading.RLock()


def _prune(now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    for session_id, slot in list(_SLOTS.items()):
        if slot.expires_at <= current:
            slot.clear()
            del _SLOTS[session_id]
    for session_id, (_, expires_at) in list(_FAILURES.items()):
        if expires_at <= current:
            del _FAILURES[session_id]


def put_secret(session_id: str, kind: str, value: str) -> dict[str, object]:
    if kind not in {"passphrase", "age-identity"}:
        raise AppError("Unsupported backup secret kind", code=ErrorCode.INVALID_PAYLOAD)
    encoded = value.encode("utf-8")
    return put_secret_bytes(session_id, kind, bytearray(encoded))


def put_secret_bytes(session_id: str, kind: str, encoded: bytearray) -> dict[str, object]:
    """Store an owned secret buffer without ever serializing it."""

    if kind not in {"passphrase", "age-identity"}:
        raise AppError("Unsupported backup secret kind", code=ErrorCode.INVALID_PAYLOAD)
    if not encoded or len(encoded) > MAX_SECRET_BYTES or 0 in encoded:
        raise AppError("Backup secret is invalid", code=ErrorCode.INVALID_PAYLOAD)
    now = time.monotonic()
    with _SLOT_LOCK:
        _prune(now)
        failures, failure_expiry = _FAILURES.get(session_id, (0, now + SECRET_TTL_SECONDS))
        if failures >= SECRET_ATTEMPTS:
            raise AppError("Unable to unlock backup", code=ErrorCode.INVALID_REQUEST, status=429)
        existing = _SLOTS.pop(session_id, None)
        if existing is not None:
            existing.clear()
        _SLOTS[session_id] = EphemeralBackupSecret(
            session_id=session_id,
            kind=cast(SecretKind, kind),
            value=encoded,
            created_at=now,
            expires_at=now + SECRET_TTL_SECONDS,
            attempts_remaining=SECRET_ATTEMPTS - failures,
        )
    return {
        "ok": True,
        "sessionId": session_id,
        "kind": kind,
        "expiresInSeconds": SECRET_TTL_SECONDS,
        "attemptsRemaining": SECRET_ATTEMPTS - failures,
    }


def consume_secret(session_id: str, expected_kind: SecretKind | None = None) -> tuple[SecretKind, bytearray]:
    now = time.monotonic()
    with _SLOT_LOCK:
        _prune(now)
        slot = _SLOTS.pop(session_id, None)
    if slot is None or (expected_kind is not None and slot.kind != expected_kind):
        if slot is not None:
            slot.clear()
        raise AppError("Backup secret is required or expired", code=ErrorCode.INVALID_REQUEST, status=409)
    return slot.kind, slot.value


def record_unlock_failure(session_id: str) -> None:
    now = time.monotonic()
    with _SLOT_LOCK:
        _prune(now)
        count, expires_at = _FAILURES.get(session_id, (0, now + SECRET_TTL_SECONDS))
        _FAILURES[session_id] = (count + 1, expires_at)


def clear_secret(session_id: str) -> None:
    with _SLOT_LOCK:
        slot = _SLOTS.pop(session_id, None)
        if slot is not None:
            slot.clear()


def helper_path() -> Path | None:
    explicit = os.environ.get("DEEPSEEK_BACKUP_CRYPTO_HELPER", "").strip()
    executable = "backup-crypto.exe" if os.name == "nt" else "backup-crypto"
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    bundle_root = getattr(sys, "_MEIPASS", "")
    if bundle_root:
        candidates.append(Path(bundle_root) / "bin" / executable)
    repository = Path(__file__).resolve().parents[3]
    candidates.extend(
        [
            repository / "bin" / executable,
            repository / "rust" / "target" / "release" / executable,
            repository / "rust" / "target" / "debug" / executable,
        ]
    )
    discovered = shutil.which(executable)
    if discovered:
        candidates.append(Path(discovered))
    return next((path.resolve() for path in candidates if path.is_file()), None)


def capabilities() -> dict[str, object]:
    path = helper_path()
    return {
        "encryptedBackupAvailable": path is not None,
        "formats": ["age-v1"] if path is not None else [],
        "protectionModes": ["none", "passphrase", "age-recipient"] if path is not None else ["none"],
        **({} if path is not None else {"reason": "backup crypto helper unavailable"}),
    }


def _secret_pipe(secret: bytearray) -> tuple[int, int, str, Any]:
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    startupinfo: Any = None
    if sys.platform == "win32":
        import msvcrt

        raw_handle = int(msvcrt.get_osfhandle(read_fd))
        os.set_handle_inheritable(raw_handle, True)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.lpAttributeList = {"handle_list": [raw_handle]}
        identifier = str(raw_handle)
    else:
        identifier = str(read_fd)
    return read_fd, write_fd, identifier, startupinfo


class _CancellationWriter:
    def __init__(self, target: BinaryIO, cancelled: threading.Event) -> None:
        self._target = target
        self._cancelled = cancelled

    def write(self, value: bytes) -> int:
        if self._cancelled.is_set():
            raise BrokenPipeError("backup crypto operation cancelled")
        return self._target.write(value)

    def __getattr__(self, name: str) -> object:
        return getattr(self._target, name)


def _run_helper(
    command: str,
    *,
    output: BinaryIO | None = None,
    write_input: Callable[[BinaryIO], None] | None = None,
    secret: bytearray | None = None,
    recipients: tuple[str, ...] = (),
    cancel_event: threading.Event | None = None,
) -> bytes:
    helper = helper_path()
    if helper is None:
        raise AppError("Backup crypto helper unavailable", code=ErrorCode.INVALID_REQUEST, status=501)
    read_fd = write_fd = -1
    startupinfo: Any = None
    args = [str(helper), command, *recipients]
    pass_fds: tuple[int, ...] = ()
    if secret is not None:
        read_fd, write_fd, identifier, startupinfo = _secret_pipe(secret)
        args.extend(["--secret-handle", identifier])
        if os.name != "nt":
            pass_fds = (read_fd,)
    process: subprocess.Popen[bytes] | None = None
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise BrokenPipeError("backup crypto operation cancelled")
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=output if output is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            startupinfo=startupinfo,
            pass_fds=pass_fds,
            shell=False,
        )
        if read_fd >= 0:
            os.close(read_fd)
            read_fd = -1
            with os.fdopen(write_fd, "wb", closefd=True) as secret_pipe:
                write_fd = -1
                secret_pipe.write(secret or b"")
                secret_pipe.flush()
        if process.stdin is None:
            raise OSError("backup crypto stdin is unavailable")
        try:
            if write_input is not None:
                input_stream = cast(BinaryIO, process.stdin)
                if cancel_event is not None:
                    input_stream = cast(BinaryIO, _CancellationWriter(input_stream, cancel_event))
                write_input(input_stream)
        finally:
            process.stdin.close()
        stdout = process.stdout.read(MAX_SECRET_BYTES) if output is None and process.stdout is not None else b""
        stderr = process.stderr.read(MAX_SECRET_BYTES) if process.stderr is not None else b""
        status = process.wait()
        if status != 0:
            del stderr
            raise AppError("Unable to process encrypted backup", code=ErrorCode.INVALID_PAYLOAD)
        return stdout
    except (OSError, BrokenPipeError) as exc:
        if process is not None:
            process.kill()
            process.wait()
        raise AppError("Unable to process encrypted backup", code=ErrorCode.INVALID_PAYLOAD) from exc
    finally:
        if read_fd >= 0:
            os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def transform_file(
    command: str,
    source: Path,
    target: Path,
    *,
    secret: bytearray,
    recipients: tuple[str, ...] = (),
    cancel_event: threading.Event | None = None,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        with temporary.open("wb") as output:
            def write_input(pipe: BinaryIO) -> None:
                with source.open("rb") as input_file:
                    shutil.copyfileobj(input_file, pipe, length=1024 * 1024)

            _run_helper(
                command,
                output=output,
                write_input=write_input,
                secret=secret,
                recipients=recipients,
                cancel_event=cancel_event,
            )
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def encrypt_stream(
    target: Path,
    write_plaintext: Callable[[BinaryIO], None],
    *,
    mode: Literal["passphrase", "age-recipient"],
    secret: bytearray | None = None,
    recipients: tuple[str, ...] = (),
    cancel_event: threading.Event | None = None,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        with temporary.open("wb") as output:
            _run_helper(
                "encrypt-passphrase" if mode == "passphrase" else "encrypt-age",
                output=output,
                write_input=write_plaintext,
                secret=secret,
                recipients=recipients,
                cancel_event=cancel_event,
            )
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def decrypt_file(
    source: Path,
    target: Path,
    *,
    kind: SecretKind,
    secret: bytearray,
    cancel_event: threading.Event | None = None,
) -> None:
    transform_file(
        "decrypt-passphrase" if kind == "passphrase" else "decrypt-age",
        source,
        target,
        secret=secret,
        cancel_event=cancel_event,
    )


def inspect_header(source: Path) -> dict[str, object]:
    def write_input(pipe: BinaryIO) -> None:
        with source.open("rb") as input_file:
            shutil.copyfileobj(input_file, pipe, length=1024 * 1024)

    raw = _run_helper("inspect-header", write_input=write_input)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or value.get("age") is not True:
        raise AppError("Backup encryption header is invalid", code=ErrorCode.INVALID_PAYLOAD)
    return value


def generate_identity() -> dict[str, str]:
    raw = _run_helper("generate-identity")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("identity"), str) or not isinstance(value.get("recipient"), str):
        raise AppError("Unable to generate recovery identity", code=ErrorCode.INVALID_PAYLOAD)
    return {"identity": value["identity"], "recipient": value["recipient"]}


def derive_recipients(secret: bytearray) -> tuple[str, ...]:
    raw = _run_helper("derive-recipient", secret=secret)
    value = json.loads(raw.decode("utf-8"))
    recipient_values = value.get("recipients") if isinstance(value, dict) else None
    if not isinstance(recipient_values, list) or not recipient_values or any(not isinstance(item, str) for item in recipient_values):
        raise AppError("Unable to derive recovery recipient", code=ErrorCode.INVALID_PAYLOAD)
    return tuple(recipient_values)

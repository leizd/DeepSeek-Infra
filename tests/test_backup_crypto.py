from __future__ import annotations

import hashlib
import io
import json
import os
import threading
from pathlib import Path
from typing import BinaryIO, Callable, Iterator
from urllib import error as urllib_error

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_crypto, backups

_AGE_HEADER = b"age-encryption.org/v1\n"
_PASSPHRASE = b"correct-password"


@pytest.fixture(autouse=True)
def _clear_secret_slots() -> Iterator[None]:
    for slot in backup_crypto._SLOTS.values():
        slot.clear()
    backup_crypto._SLOTS.clear()
    backup_crypto._FAILURES.clear()
    yield
    for slot in backup_crypto._SLOTS.values():
        slot.clear()
    backup_crypto._SLOTS.clear()
    backup_crypto._FAILURES.clear()


def _install_fake_age(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        backup_crypto,
        "capabilities",
        lambda: {
            "encryptedBackupAvailable": True,
            "formats": ["age-v1"],
            "protectionModes": ["none", "passphrase", "age-recipient"],
        },
    )
    monkeypatch.setattr(backup_crypto, "derive_recipients", lambda secret: ("age1unitrecipient",) if secret else ())

    def encrypt_stream(
        target: Path,
        write_plaintext: Callable[[BinaryIO], None],
        *,
        mode: str,
        secret: bytearray | None = None,
        recipients: tuple[str, ...] = (),
        cancel_event: object | None = None,
    ) -> None:
        del cancel_event
        if mode == "passphrase" and bytes(secret or b"") != _PASSPHRASE:
            raise AppError("Unable to process encrypted backup")
        if mode == "age-recipient" and recipients != ("age1unitrecipient",):
            raise AppError("Unable to process encrypted backup")
        plaintext = io.BytesIO()
        write_plaintext(plaintext)
        target.write_bytes(_AGE_HEADER + bytes(value ^ 0xA5 for value in plaintext.getvalue()))

    def decrypt_file(
        source: Path,
        target: Path,
        *,
        kind: str,
        secret: bytearray,
        cancel_event: object | None = None,
    ) -> None:
        del cancel_event
        if kind == "passphrase" and bytes(secret) != _PASSPHRASE:
            raise AppError("Unable to process encrypted backup")
        if kind == "age-identity" and bytes(secret) != b"AGE-SECRET-KEY-UNIT":
            raise AppError("Unable to process encrypted backup")
        raw = source.read_bytes()
        if not raw.startswith(_AGE_HEADER):
            raise AppError("Unable to process encrypted backup")
        target.write_bytes(bytes(value ^ 0xA5 for value in raw[len(_AGE_HEADER) :]))

    monkeypatch.setattr(backup_crypto, "encrypt_stream", encrypt_stream)
    monkeypatch.setattr(backup_crypto, "decrypt_file", decrypt_file)
    monkeypatch.setattr(backup_crypto, "inspect_header", lambda _path: {"age": True, "passphrase": True})


def test_passphrase_encrypted_round_trip_keeps_secret_and_metadata_out_of_ciphertext(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_age(monkeypatch)
    project = tmp_settings / ".projects" / "secret-project"
    project.mkdir(parents=True)
    (project / "project.json").write_text(json.dumps({"id": "secret-project", "name": "Private Launch"}), encoding="utf-8")

    created = backups.create_session(
        {
            "mode": "full",
            "requiresFrontendState": False,
            "protection": {"mode": "passphrase"},
        }
    )
    backup_id = str(created["backupId"])
    backups.put_session_secret(backup_id, {"kind": "passphrase", "secret": _PASSPHRASE.decode()})
    session_raw = (backups.BACKUP_DIR / "sessions" / backup_id / "session.json").read_bytes()
    assert _PASSPHRASE not in session_raw

    ready = backups.finalize_session(backup_id)
    archive = backups.backup_path(backup_id)
    assert archive.name.endswith(".dsibackup.age")
    assert b"Private Launch" not in archive.read_bytes()
    assert b"manifest.json" not in archive.read_bytes()
    assert ready["protection"] == {"mode": "passphrase"}
    with pytest.raises(AppError, match="required or expired"):
        backup_crypto.consume_secret(backup_id)

    locked = backups.inspect_archive(archive, filename=archive.name)
    assert locked["phase"] == "locked"
    restore_id = str(locked["restoreId"])
    backups.put_session_secret(restore_id, {"kind": "passphrase", "secret": "wrong-password"})
    with pytest.raises(AppError, match="Unable to unlock backup"):
        backups.unlock_restore(restore_id)
    backups.put_session_secret(restore_id, {"kind": "passphrase", "secret": _PASSPHRASE.decode()})
    plan = backups.unlock_restore(restore_id)
    assert plan["phase"] == "inspected"
    assert plan["encrypted"] is True
    assert plan["sourceVersion"]
    assert bytes(backup_crypto._SLOTS[restore_id].value) == _PASSPHRASE
    assert _PASSPHRASE not in (backups.RESTORE_DIR / restore_id / "plan.json").read_bytes()


def test_extracted_tree_tampering_forces_secret_reentry(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_age(monkeypatch)
    project = tmp_settings / ".projects" / "secret-project"
    project.mkdir(parents=True)
    (project / "project.json").write_text(json.dumps({"id": "secret-project"}), encoding="utf-8")
    created = backups.create_session({"mode": "full", "requiresFrontendState": False, "protection": {"mode": "passphrase"}})
    backup_id = str(created["backupId"])
    backups.put_session_secret(backup_id, {"kind": "passphrase", "secret": _PASSPHRASE.decode()})
    backups.finalize_session(backup_id)
    locked = backups.inspect_archive(backups.backup_path(backup_id), filename="encrypted.dsibackup.age")
    restore_id = str(locked["restoreId"])
    root = backups.RESTORE_DIR / restore_id
    backups.put_session_secret(restore_id, {"kind": "passphrase", "secret": _PASSPHRASE.decode()})
    backups.unlock_restore(restore_id)

    tampered = root / "extracted" / "payload" / "projects" / "secret-project" / "project.json"
    tampered.write_text("tampered", encoding="utf-8")
    with pytest.raises(AppError, match="mismatch|changed after inspection"):
        backups.prepare_restore(restore_id)

    manifest = root / "extracted" / "manifest.json"
    original = manifest.read_bytes()
    manifest.write_bytes(b"{}")
    with pytest.raises(AppError, match="changed after inspection"):
        backups.prepare_restore(restore_id)
    manifest.write_bytes(original)


def test_recovery_identity_is_verified_against_public_recipient(    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_age(monkeypatch)
    monkeypatch.setattr(backup_crypto, "inspect_header", lambda _path: {"age": True, "passphrase": False})
    created = backups.create_session(
        {
            "mode": "full",
            "requiresFrontendState": False,
            "protection": {"mode": "age-recipient", "recipients": ["age1unitrecipient"]},
        }
    )
    backup_id = str(created["backupId"])
    backups.put_session_secret(backup_id, {"kind": "age-identity", "secret": "AGE-SECRET-KEY-UNIT"})
    ready = backups.finalize_session(backup_id)
    assert ready["filename"].endswith(".age")


def test_ephemeral_secret_slot_expires_and_is_zeroized(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    monkeypatch.setattr(backup_crypto.time, "monotonic", lambda: now)
    backup_crypto.put_secret("backup_unit", "passphrase", "do-not-persist")
    slot = backup_crypto._SLOTS["backup_unit"]
    now += backup_crypto.SECRET_TTL_SECONDS + 1
    with pytest.raises(AppError, match="required or expired"):
        backup_crypto.consume_secret("backup_unit")
    assert bytes(slot.value) == b"\x00" * len("do-not-persist")


def test_strict_external_coverage_fails_and_best_effort_reports_omission(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STATELESS_MCP_BACKUP_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(
        backups.StatelessMcpContributor,
        "capabilities",
        lambda _self: {"id": "stateless-mcp", "available": False, "reason": "service unavailable"},
    )
    with pytest.raises(AppError, match="Strict backup coverage"):
        backups.create_session({"mode": "full", "requiresFrontendState": False, "coveragePolicy": "strict"})
    created = backups.create_session({"mode": "full", "requiresFrontendState": False, "coveragePolicy": "best-effort"})
    assert created["coverage"]["complete"] is False
    assert created["coverage"]["unavailableDurableSources"] == [
        {"id": "stateless-mcp", "reason": "service unavailable"}
    ]


def test_secret_slot_validates_replaces_throttles_and_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AppError, match="Unsupported"):
        backup_crypto.put_secret("slot", "unknown", "secret")
    with pytest.raises(AppError, match="Unsupported"):
        backup_crypto.put_secret_bytes("slot", "unknown", bytearray(b"secret"))
    for invalid in (bytearray(), bytearray(b"bad\0secret"), bytearray(b"x" * (backup_crypto.MAX_SECRET_BYTES + 1))):
        with pytest.raises(AppError, match="invalid"):
            backup_crypto.put_secret_bytes("slot", "passphrase", invalid)

    first = bytearray(b"first-secret")
    backup_crypto.put_secret_bytes("slot", "passphrase", first)
    second = bytearray(b"second-secret")
    backup_crypto.put_secret_bytes("slot", "age-identity", second)
    assert bytes(first) == b"\0" * len(first)
    with pytest.raises(AppError, match="required or expired"):
        backup_crypto.consume_secret("slot", "passphrase")
    assert bytes(second) == b"\0" * len(second)

    third = bytearray(b"clear-me")
    backup_crypto.put_secret_bytes("slot", "passphrase", third)
    backup_crypto.clear_secret("slot")
    backup_crypto.clear_secret("missing")
    assert bytes(third) == b"\0" * len(third)

    now = 50.0
    monkeypatch.setattr(backup_crypto.time, "monotonic", lambda: now)
    for _ in range(backup_crypto.SECRET_ATTEMPTS):
        backup_crypto.record_unlock_failure("locked")
    with pytest.raises(AppError) as exc_info:
        backup_crypto.put_secret("locked", "passphrase", "secret")
    assert exc_info.value.status == 429
    now += backup_crypto.SECRET_TTL_SECONDS + 1
    backup_crypto._prune()
    assert "locked" not in backup_crypto._FAILURES
    assert backup_crypto.put_secret("locked", "passphrase", "secret")["attemptsRemaining"] == backup_crypto.SECRET_ATTEMPTS


def test_helper_discovery_capabilities_and_secret_pipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = tmp_path / "backup-crypto-test"
    helper.write_bytes(b"helper")
    monkeypatch.setenv("DEEPSEEK_BACKUP_CRYPTO_HELPER", str(helper))
    assert backup_crypto.helper_path() == helper.resolve()
    assert backup_crypto.capabilities()["encryptedBackupAvailable"] is True

    monkeypatch.delenv("DEEPSEEK_BACKUP_CRYPTO_HELPER")
    executable = "backup-crypto.exe" if os.name == "nt" else "backup-crypto"
    bundle = tmp_path / "bundle"
    (bundle / "bin").mkdir(parents=True)
    (bundle / "bin" / executable).write_bytes(b"bundled")
    monkeypatch.setattr(backup_crypto.sys, "_MEIPASS", str(bundle), raising=False)
    assert backup_crypto.helper_path() == (bundle / "bin" / executable).resolve()
    monkeypatch.delattr(backup_crypto.sys, "_MEIPASS", raising=False)

    discovered = tmp_path / executable
    discovered.write_bytes(b"path")
    monkeypatch.setattr(backup_crypto.shutil, "which", lambda _name: str(discovered))
    real_is_file = backup_crypto.Path.is_file
    monkeypatch.setattr(
        backup_crypto.Path,
        "is_file",
        lambda self: real_is_file(self) and self == discovered,
    )
    assert backup_crypto.helper_path() == discovered.resolve()

    monkeypatch.setattr(backup_crypto.shutil, "which", lambda _name: None)
    monkeypatch.setattr(backup_crypto.Path, "is_file", lambda _self: False)
    unavailable = backup_crypto.capabilities()
    assert unavailable["encryptedBackupAvailable"] is False
    assert unavailable["reason"] == "backup crypto helper unavailable"

    read_fd, write_fd, identifier, startupinfo = backup_crypto._secret_pipe(bytearray(b"unused"))
    try:
        assert identifier
        if os.name == "nt":
            assert startupinfo is not None
        else:
            assert startupinfo is None
        os.write(write_fd, b"pipe-byte")
        assert os.read(read_fd, 9) == b"pipe-byte"
    finally:
        os.close(write_fd)
        os.close(read_fd)


class _InputSink:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, value: bytes) -> int:
        self.data.extend(value)
        return len(value)

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, *, status: int = 0, stdin: _InputSink | None = None) -> None:
        self.stdin = _InputSink() if stdin is None else stdin
        self.stdout = io.BytesIO(b'{"ok":true}')
        self.stderr = io.BytesIO(b"sensitive helper detail")
        self.status = status
        self.killed = False

    def wait(self) -> int:
        return self.status

    def kill(self) -> None:
        self.killed = True


def test_run_helper_success_failure_and_process_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = tmp_path / "backup-crypto"
    helper.write_bytes(b"helper")
    monkeypatch.setattr(backup_crypto, "helper_path", lambda: helper)
    monkeypatch.setattr(backup_crypto, "_secret_pipe", lambda _secret: (-1, -1, "77", None))
    captured: list[tuple[list[str], _FakeProcess]] = []

    def popen(args: list[str], **_kwargs: object) -> _FakeProcess:
        process = _FakeProcess()
        captured.append((args, process))
        return process

    monkeypatch.setattr(backup_crypto.subprocess, "Popen", popen)

    def write_payload(stream: BinaryIO) -> None:
        stream.write(b"payload")

    result = backup_crypto._run_helper(
        "derive-recipient",
        write_input=write_payload,
        secret=bytearray(b"private"),
        recipients=("age1recipient",),
    )
    assert result == b'{"ok":true}'
    assert captured[0][0][-2:] == ["--secret-handle", "77"]
    assert captured[0][1].stdin.data == b"payload"
    assert captured[0][1].stdin.closed is True

    cancelled = threading.Event()
    cancelled_process = _FakeProcess()
    monkeypatch.setattr(backup_crypto.subprocess, "Popen", lambda *_args, **_kwargs: cancelled_process)

    def cancel_write(stream: BinaryIO) -> None:
        cancelled.set()
        stream.write(b"payload")

    with pytest.raises(AppError, match="Unable to process"):
        backup_crypto._run_helper("inspect-header", write_input=cancel_write, cancel_event=cancelled)
    assert cancelled_process.killed is True

    monkeypatch.setattr(backup_crypto.subprocess, "Popen", lambda *_args, **_kwargs: _FakeProcess(status=2))
    with pytest.raises(AppError, match="Unable to process"):
        backup_crypto._run_helper("inspect-header")

    missing_stdin = _FakeProcess()
    missing_stdin.stdin = None  # type: ignore[assignment]
    monkeypatch.setattr(backup_crypto.subprocess, "Popen", lambda *_args, **_kwargs: missing_stdin)
    with pytest.raises(AppError, match="Unable to process"):
        backup_crypto._run_helper("inspect-header", write_input=write_payload)
    assert missing_stdin.killed is True

    def broken_popen(*_args: object, **_kwargs: object) -> _FakeProcess:
        raise OSError("cannot start")

    monkeypatch.setattr(backup_crypto.subprocess, "Popen", broken_popen)
    with pytest.raises(AppError, match="Unable to process"):
        backup_crypto._run_helper("inspect-header")
    monkeypatch.setattr(backup_crypto, "helper_path", lambda: None)
    with pytest.raises(AppError) as unavailable:
        backup_crypto._run_helper("inspect-header")
    assert unavailable.value.status == 501


def test_inspect_header_uses_inherited_handle_without_stdin_producer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = tmp_path / "backup-crypto"
    helper.write_bytes(b"helper")
    monkeypatch.setattr(backup_crypto, "helper_path", lambda: helper)
    source = tmp_path / "large.dsibackup.age"
    header = b"age-encryption.org/v1\n-> scrypt abc 14\nxyz\n--- mac\n"
    source.write_bytes(header + b"\x00" * (8 * 1024 * 1024))
    captured: dict[str, object] = {}

    class _HandleProcess(_FakeProcess):
        def __init__(self) -> None:
            super().__init__()
            self.stdout = io.BytesIO(b'{"age":true,"passphrase":false}')

    def popen(args: list[str], **kwargs: object) -> _FakeProcess:
        captured["args"] = args
        captured["stdin"] = kwargs.get("stdin")
        captured["pass_fds"] = kwargs.get("pass_fds")
        captured["startupinfo"] = kwargs.get("startupinfo")
        return _HandleProcess()

    monkeypatch.setattr(backup_crypto.subprocess, "Popen", popen)
    assert backup_crypto.inspect_header(source) == {"age": True, "passphrase": False}

    args = [str(value) for value in captured["args"]]  # type: ignore[union-attr]
    assert "--input-handle" in args
    identifier = args[args.index("--input-handle") + 1]
    assert identifier and identifier != "-1"
    # No stdin producer exists for header inspection, so a helper that exits
    # after the header can never cause a BrokenPipeError in the parent.
    assert captured["stdin"] == backup_crypto.subprocess.DEVNULL
    if os.name != "nt":
        passed = tuple(int(value) for value in captured["pass_fds"])  # type: ignore[union-attr]
        assert int(identifier) in passed
        with os.fdopen(os.dup(int(identifier)), "rb") as inherited:
            assert inherited.read(len(header)) == header
    else:
        startupinfo = captured["startupinfo"]
        assert startupinfo is not None
        handles = startupinfo.lpAttributeList["handle_list"]  # type: ignore[union-attr]
        assert int(identifier) in handles

    monkeypatch.setattr(
        backup_crypto,
        "_run_helper",
        lambda *_args, **_kwargs: b'{"age":false}',
    )
    with pytest.raises(AppError, match="header"):
        backup_crypto.inspect_header(source)


def test_run_helper_writes_secret_pipe_and_closes_descriptors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = tmp_path / "backup-crypto"
    helper.write_bytes(b"helper")
    monkeypatch.setattr(backup_crypto, "helper_path", lambda: helper)
    original_secret_pipe = backup_crypto._secret_pipe
    descriptors: dict[str, int] = {}

    def secret_pipe(secret: bytearray) -> tuple[int, int, str, object | None]:
        read_fd, write_fd, identifier, startupinfo = original_secret_pipe(secret)
        descriptors["read"] = read_fd
        descriptors["write"] = write_fd
        return read_fd, write_fd, identifier, startupinfo

    process = _FakeProcess()

    def popen(*_args: object, **_kwargs: object) -> _FakeProcess:
        descriptors["duplicate"] = os.dup(descriptors["read"])
        return process

    monkeypatch.setattr(backup_crypto, "_secret_pipe", secret_pipe)
    monkeypatch.setattr(backup_crypto.subprocess, "Popen", popen)
    try:
        assert backup_crypto._run_helper("derive-recipient", secret=bytearray(b"pipe-secret")) == b'{"ok":true}'
    finally:
        os.close(descriptors["duplicate"])
    for key in ("read", "write"):
        with pytest.raises(OSError):
            os.fstat(descriptors[key])

    closed: dict[str, int] = {}

    def failing_pipe(secret: bytearray) -> tuple[int, int, str, None]:
        del secret
        read_fd, write_fd = os.pipe()
        closed.update(read=read_fd, write=write_fd)
        return read_fd, write_fd, "invalid", None

    monkeypatch.setattr(backup_crypto, "_secret_pipe", failing_pipe)
    monkeypatch.setattr(
        backup_crypto.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn failed")),
    )
    with pytest.raises(AppError):
        backup_crypto._run_helper("derive-recipient", secret=bytearray(b"pipe-secret"))
    for descriptor in closed.values():
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_crypto_file_wrappers_publish_atomically_and_validate_helper_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "plain.zip"
    source.write_bytes(b"plain-workspace")
    target = tmp_path / "nested" / "cipher.age"

    def copy_helper(
        command: str,
        *,
        output: BinaryIO | None = None,
        write_input: Callable[[BinaryIO], None] | None = None,
        **_kwargs: object,
    ) -> bytes:
        assert command in {"encrypt-passphrase", "encrypt-age", "decrypt-passphrase", "decrypt-age"}
        collected = io.BytesIO()
        assert write_input is not None
        write_input(collected)
        assert output is not None
        output.write(collected.getvalue())
        return b""

    monkeypatch.setattr(backup_crypto, "_run_helper", copy_helper)
    backup_crypto.transform_file("decrypt-passphrase", source, target, secret=bytearray(b"secret"))
    assert target.read_bytes() == source.read_bytes()
    encrypted = tmp_path / "stream.age"
    def write_streamed_zip(pipe: BinaryIO) -> None:
        pipe.write(b"streamed-zip")

    backup_crypto.encrypt_stream(
        encrypted,
        write_streamed_zip,
        mode="age-recipient",
        recipients=("age1recipient",),
    )
    assert encrypted.read_bytes() == b"streamed-zip"

    called: list[str] = []
    monkeypatch.setattr(
        backup_crypto,
        "transform_file",
        lambda command, *_args, **_kwargs: called.append(command),
    )
    backup_crypto.decrypt_file(source, target, kind="passphrase", secret=bytearray(b"secret"))
    backup_crypto.decrypt_file(source, target, kind="age-identity", secret=bytearray(b"secret"))
    assert called == ["decrypt-passphrase", "decrypt-age"]

    failed = tmp_path / "failed.age"
    monkeypatch.setattr(backup_crypto, "_run_helper", lambda *_args, **_kwargs: (_ for _ in ()).throw(AppError("failed")))
    def write_secret(pipe: BinaryIO) -> None:
        pipe.write(b"secret")

    with pytest.raises(AppError):
        backup_crypto.encrypt_stream(failed, write_secret, mode="passphrase", secret=bytearray(b"pw"))
    assert not failed.exists()
    assert not failed.with_suffix(".age.tmp").exists()

    responses = iter(
        (
            b'{"age":true,"passphrase":true}',
            b'{"age":false}',
            b'{"identity":"AGE-SECRET-KEY-UNIT","recipient":"age1unit"}',
            b'{"identity":1,"recipient":"age1unit"}',
            b'{"recipients":["age1unit","age1backup"]}',
            b'{"recipients":[]}',
        )
    )
    def response_helper(
        *_args: object,
        write_input: Callable[[BinaryIO], None] | None = None,
        **_kwargs: object,
    ) -> bytes:
        if write_input is not None:
            write_input(io.BytesIO())
        return next(responses)

    monkeypatch.setattr(backup_crypto, "_run_helper", response_helper)
    assert backup_crypto.inspect_header(source)["passphrase"] is True
    with pytest.raises(AppError, match="header"):
        backup_crypto.inspect_header(source)
    assert backup_crypto.generate_identity()["recipient"] == "age1unit"
    with pytest.raises(AppError, match="generate"):
        backup_crypto.generate_identity()
    assert backup_crypto.derive_recipients(bytearray(b"identity")) == ("age1unit", "age1backup")
    with pytest.raises(AppError, match="derive"):
        backup_crypto.derive_recipients(bytearray(b"identity"))


class _Response(io.BytesIO):
    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def test_stateless_mcp_external_contributor_snapshot_restore_and_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contributor = backups.StatelessMcpContributor()
    snapshot = (
        b'{"type":"task","schemaVersion":1,"task":{"id":"task-1"}}\n'
        b'{"type":"complete","schemaVersion":1,"stateGeneration":1}\n'
    )
    calls: list[tuple[str, str, bytes | None]] = []
    stream_attempts = 0

    def request(
        path: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> _Response:
        nonlocal stream_attempts
        del timeout
        calls.append((path, method, body))
        if path == "/internal/backups/capabilities":
            return _Response(b'{"contributorId":"stateless-mcp","schemaVersion":1}')
        if path.endswith("/stream"):
            stream_attempts += 1
            if stream_attempts == 1:
                raise AppError("generation changed")
            return _Response(snapshot)
        if "/internal/restores/" in path:
            if headers is not None:
                assert headers["X-Transaction-Digest"] == "digest-12345678"
                assert headers["X-Content-SHA256"]
                assert headers["Content-Length"] == str((source / "state.jsonl").stat().st_size)
            journal = {
                "phase": "prepared",
                "sourceDigest": "a" * 64,
                "preparedDigest": "b" * 64,
                "imported": 1,
                "skipped": 0,
                "interrupted": 1,
                "remapped": {},
            }
            return _Response(json.dumps(journal).encode("utf-8"))
        return _Response(b'{"ok":true}')

    monkeypatch.setattr(backups.StatelessMcpContributor, "_request", staticmethod(request))
    assert contributor.capabilities()["available"] is True
    assert contributor.inventory(backups.BackupContext()) == {"records": 0, "bytes": 0}
    contributor.flush(backups.BackupContext())
    contribution = contributor.snapshot(tmp_path, backups.BackupContext())
    assert contribution.records == 1
    assert stream_attempts == 2
    assert any(path.endswith("/release") for path, _method, _body in calls)

    source = tmp_path / "payload" / "stateless-mcp"
    assert contributor.validate(source, backups.BackupContext()) == []
    plan = contributor.plan_restore(source, backups.BackupContext())
    assert plan["external"] is True and plan["available"] is True
    with pytest.raises(AppError, match="participant protocol"):
        contributor.apply_restore(plan, backups.BackupContext())

    journal = contributor.prepare_restore("restore_external_unit", source, "digest-12345678")
    assert journal["phase"] == "prepared"
    prepare_call = next(path for path, _method, _body in calls if path.startswith("/internal/restores/") and path.endswith("/prepare"))
    assert prepare_call == "/internal/restores/restore_external_unit/prepare"
    for action in ("commit-intent", "commit", "complete", "abort"):
        getattr(contributor, {"commit-intent": "commit_restore_intent"}.get(action, f"{action}_restore"))(
            "restore_external_unit", *("digest-12345678",) if action in {"commit-intent", "commit"} else ()
        )
        assert any(path.endswith(f"/{action}") for path, _method, _body in calls)
    assert contributor.restore_status("restore_external_unit") is not None
    assert contributor.inspect_schema(1)["compatible"] is True
    assert contributor.inspect_schema(2)["compatible"] is False
    assert contributor.migrate(source, 1) == source
    with pytest.raises(AppError, match="incompatible"):
        contributor.migrate(source, 2)
    assert contributor.build_identity_map(source, source) == {}
    assert contributor.rewrite_references(b"user body", {"old": "new"}, "backup") == b"user body"
    with pytest.raises(AppError, match="do not merge"):
        contributor.merge_into_staging({}, backups.BackupContext())
    assert contributor.validate_staging(source, backups.BackupContext()) == []

    missing = tmp_path / "missing"
    assert "missing" in contributor.validate(missing, backups.BackupContext())[0]
    state = source / "state.jsonl"
    state.write_text('{"type":"task","schemaVersion":2}\n', encoding="utf-8")
    assert "unsupported" in contributor.validate(source, backups.BackupContext())[0]
    state.write_text('{"type":"task","schemaVersion":1,"redis":"redis://secret"}\n', encoding="utf-8")
    assert "forbidden" in contributor.validate(source, backups.BackupContext())[0]
    state.write_text('{"type":"task","schemaVersion":1}\n', encoding="utf-8")
    assert "incomplete" in contributor.validate(source, backups.BackupContext())[0]
    state.write_text("not-json\n", encoding="utf-8")
    assert "invalid" in contributor.validate(source, backups.BackupContext())[0]


def test_external_snapshot_transport_streams_with_receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    contributor = backups.StatelessMcpContributor()
    payload = b'{"type":"task","schemaVersion":1,"task":{"id":"t-1"}}\n{"type":"complete","schemaVersion":1,"stateGeneration":1}\n'
    seen: dict[str, object] = {}

    def request(
        path: str,
        *,
        method: str = "GET",
        body: object | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> _Response:
        del timeout
        seen["path"] = path
        seen["method"] = method
        seen["body"] = body
        seen["headers"] = headers
        if method == "POST":
            return _Response(b'{"phase":"prepared","sourceDigest":"' + b"a" * 64 + b'","records":1}')
        return _Response(payload)

    monkeypatch.setattr(backups.StatelessMcpContributor, "_request", staticmethod(request))
    transport = backups.ExternalSnapshotTransport(contributor)

    target = tmp_path / "state.jsonl"
    with target.open("wb") as output:
        receipt = transport.download_to("/internal/backups/backup_x/stream", output)
    assert receipt.bytes == len(payload)
    assert receipt.sha256 == hashlib.sha256(payload).hexdigest()
    assert receipt.records == 2
    assert target.read_bytes() == payload

    capped = tmp_path / "capped.jsonl"
    with pytest.raises(AppError) as too_large:
        with capped.open("wb") as output:
            transport.download_to("/internal/backups/backup_x/stream", output, max_bytes=8)
    assert too_large.value.status == 413

    upload_source = tmp_path / "upload.jsonl"
    upload_source.write_bytes(payload)
    size = upload_source.stat().st_size
    digest = hashlib.sha256(payload).hexdigest()
    with upload_source.open("rb") as stream:
        journal = transport.upload_from(
            "/internal/restores/restore_x/prepare",
            stream,
            size=size,
            sha256=digest,
            headers={"X-Transaction-Digest": "digest-12345678"},
        )
    assert journal["phase"] == "prepared"
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["Content-Length"] == str(size)
    assert headers["X-Content-SHA256"] == digest
    assert headers["X-Transaction-Digest"] == "digest-12345678"
    assert not isinstance(seen["body"], bytes), "upload must stream the file object, not materialized bytes"


def test_coverage_plan_is_frozen_and_attested(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STATELESS_MCP_BACKUP_URL", "http://backup.internal/")
    availability = {"available": True}
    monkeypatch.setattr(
        backups.StatelessMcpContributor,
        "capabilities",
        lambda _self: {
            "id": "stateless-mcp",
            "available": availability["available"],
            "schemaVersion": 1,
            **({} if availability["available"] else {"reason": "service unavailable"}),
        },
    )
    snapshots: list[str] = []

    def fake_snapshot(self: backups.RegisteredContributor, staging: Path, context: backups.BackupContext) -> backups.BackupContribution:
        snapshots.append(self.contributor_id)
        payload = staging / "payload" / self.contributor_id
        payload.mkdir(parents=True, exist_ok=True)
        data = payload / "data.json"
        data.write_text("{}", encoding="utf-8")
        entry = {"path": f"payload/{self.contributor_id}/data.json", "size": 2, "sha256": hashlib.sha256(b"{}").hexdigest()}
        return backups.BackupContribution(
            contributor_id=self.contributor_id,
            schema_version=self.schema_version,
            records=1,
            bytes=2,
            digest=hashlib.sha256(b"{}").hexdigest(),
            restore_policy=self.restore_policy,
            files=(entry,),
            snapshot_generation=42 if self.contributor_id == "stateless-mcp" else None,
        )

    monkeypatch.setattr(backups.DirectoryContributor, "snapshot", fake_snapshot)
    monkeypatch.setattr(backups.StatelessMcpContributor, "snapshot", fake_snapshot)

    created = backups.create_session({"mode": "full", "requiresFrontendState": False, "coveragePolicy": "best-effort"})
    backup_id = str(created["backupId"])
    plan = created["contributorPlan"]
    assert plan["planId"]
    external_entry = next(item for item in plan["contributors"] if item["kind"] == "external")
    assert external_entry["status"] == "selected"

    availability["available"] = False
    finalized = backups.finalize_session(backup_id)
    assert finalized["phase"] == "ready"
    import zipfile

    with zipfile.ZipFile(backups.backup_path(backup_id)) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    coverage = manifest["coverage"]
    assert coverage["planId"] == plan["planId"]
    assert coverage["complete"] is True
    assert coverage["externalContributors"][0]["id"] == "stateless-mcp"
    assert coverage["externalContributors"][0]["snapshotGeneration"] == 42
    assert coverage["externalContributors"][0]["snapshotDigest"] == hashlib.sha256(b"{}").hexdigest()
    assert "stateless-mcp" in snapshots, "frozen plan must include the external contributor even if it later drops"

    availability["available"] = True
    created_second = backups.create_session({"mode": "full", "requiresFrontendState": False, "coveragePolicy": "best-effort"})
    second_id = str(created_second["backupId"])
    availability["available"] = False
    monkeypatch.setattr(backups.StatelessMcpContributor, "capabilities", lambda _self: {"id": "stateless-mcp", "available": False, "schemaVersion": 1, "reason": "service unavailable"})
    created_third = backups.create_session({"mode": "full", "requiresFrontendState": False, "coveragePolicy": "best-effort"})
    third_id = str(created_third["backupId"])
    third_plan = created_third["contributorPlan"]
    third_external = next(item for item in third_plan["contributors"] if item["kind"] == "external")
    assert third_external["status"] == "unavailable"
    availability["available"] = True
    monkeypatch.setattr(backups.StatelessMcpContributor, "capabilities", lambda _self: {"id": "stateless-mcp", "available": True, "schemaVersion": 1})
    snapshots.clear()
    backups.finalize_session(third_id)
    with zipfile.ZipFile(backups.backup_path(third_id)) as archive:
        third_manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    assert third_manifest["coverage"]["complete"] is False
    assert third_manifest["coverage"]["unavailableDurableSources"][0]["id"] == "stateless-mcp"
    assert third_manifest["coverage"]["externalContributors"] == [], "a later recovery must not fake inclusion"
    assert "stateless-mcp" not in snapshots
    backups.finalize_session(second_id)

    plan_by_id = {item["id"]: item for item in plan["contributors"]}
    for item in manifest["contributors"]:
        assert item["id"] in plan_by_id
        assert plan_by_id[item["id"]]["status"] == "selected"
        assert item["digest"]
    payload_ids = {entry["id"] for entry in manifest["contributors"]}
    selected_ids = {item["id"] for item in plan["contributors"] if item["status"] == "selected"}
    assert payload_ids == selected_ids


def test_coverage_attestation_rejects_missing_payloads(tmp_settings: Path) -> None:
    plan = backups._contributor_plan(backups.BackupContext())
    selected = [item for item in plan["contributors"] if item["status"] == "selected"]
    assert selected
    partial = [
        backups.BackupContribution(
            contributor_id=selected[0]["id"],
            schema_version=1,
            records=0,
            bytes=0,
            digest="0" * 64,
            restore_policy="merge",
            files=(),
        )
    ]
    with pytest.raises(AppError, match="does not match the contributor plan"):
        backups._attest_coverage(plan, partial)


def test_stateless_mcp_request_auth_errors_and_coverage_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    contributor = backups.StatelessMcpContributor()
    monkeypatch.delenv("STATELESS_MCP_BACKUP_URL", raising=False)
    with pytest.raises(AppError, match="not configured"):
        contributor._url("/internal")

    monkeypatch.setenv("STATELESS_MCP_BACKUP_URL", "http://backup.internal/")
    monkeypatch.setenv("STATELESS_MCP_BACKUP_TOKEN", "internal-secret")
    seen: dict[str, object] = {}

    def urlopen(request: object, timeout: float) -> _Response:
        seen["request"] = request
        seen["timeout"] = timeout
        return _Response(b"{}")

    monkeypatch.setattr(backups.urllib_request, "urlopen", urlopen)
    with contributor._request("/internal", method="POST", body=b"{}") as response:
        assert response.read() == b"{}"
    request = seen["request"]
    assert getattr(request, "headers")["Authorization"] == "Bearer internal-secret"
    assert getattr(request, "headers")["Content-type"] == "application/json"
    with contributor._request("/internal/restores/restore_x/prepare", method="POST", body=b'{"type":"complete"}\n') as response:
        assert response.read() == b"{}"
    request = seen["request"]
    assert getattr(request, "headers")["Content-type"] == "application/x-ndjson"

    monkeypatch.setattr(backups.urllib_request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib_error.URLError("down")))
    with pytest.raises(AppError, match="unavailable"):
        contributor._request("/internal")
    assert contributor.capabilities()["available"] is False

    monkeypatch.setattr(
        backups.StatelessMcpContributor,
        "capabilities",
        lambda _self: {"id": "stateless-mcp", "available": True, "schemaVersion": 1},
    )
    available = backups._coverage_status(backups.BackupContext())
    assert available["complete"] is True
    assert available["externalContributors"][0]["id"] == "stateless-mcp"
    excluded = backups._coverage_status(backups.BackupContext(include_external_state=False))
    assert excluded["complete"] is False
    assert excluded["unavailableDurableSources"][0]["reason"] == "excluded by backup request"
    assert isinstance(backups._selected_contributors(backups.BackupContext())[-1], backups.StatelessMcpContributor)

    jsonl = tmp_path / "records.jsonl"
    jsonl.write_text("bad\n" + json.dumps({"type": "task"}) + "\n", encoding="utf-8")
    assert backups._jsonl_task_count(jsonl) == 1


def test_backup_protection_validation_and_recovery_identity_mismatch(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_crypto, "capabilities", lambda: {"encryptedBackupAvailable": False})
    with pytest.raises(AppError, match="Unsupported"):
        backups._protection_from_payload({"protection": {"mode": "custom"}})
    with pytest.raises(AppError) as unavailable:
        backups._protection_from_payload({"protection": {"mode": "passphrase"}})
    assert unavailable.value.status == 501

    monkeypatch.setattr(backup_crypto, "capabilities", lambda: {"encryptedBackupAvailable": True})
    for recipients in ([], ["invalid"], [f"age1{index}" for index in range(17)]):
        with pytest.raises(AppError, match="recipient"):
            backups._protection_from_payload({"protection": {"mode": "age-recipient", "recipients": recipients}})
    assert backups._protection_from_payload(
        {"protection": {"mode": "age-recipient", "recipients": ["age1unit", "age1unit"]}}
    )["recipients"] == ["age1unit"]

    for payload in ({"mode": "invalid"}, {"mode": "project"}, {"coveragePolicy": "invalid"}):
        with pytest.raises(AppError):
            backups._context_from_payload(payload)
    with pytest.raises(AppError, match="Invalid backup secret session"):
        backups.put_session_secret("invalid", {"kind": "passphrase", "secret": "pw"})

    _install_fake_age(monkeypatch)
    monkeypatch.setattr(backup_crypto, "derive_recipients", lambda _secret: ("age1different",))
    created = backups.create_session(
        {
            "mode": "full",
            "requiresFrontendState": False,
            "protection": {"mode": "age-recipient", "recipients": ["age1unitrecipient"]},
        }
    )
    backup_id = str(created["backupId"])
    backups.put_session_secret(backup_id, {"kind": "age-identity", "secret": "AGE-SECRET-KEY-UNIT"})
    slot = backup_crypto._SLOTS[backup_id]
    with pytest.raises(AppError, match="does not match"):
        backups.finalize_session(backup_id)
    assert bytes(slot.value) == b"\0" * len(slot.value)


def test_external_snapshot_failure_paths_release_fence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    contributor = backups.StatelessMcpContributor()
    releases = 0

    def always_changed(path: str, **_kwargs: object) -> _Response:
        nonlocal releases
        if path.endswith("/release"):
            releases += 1
            return _Response(b"{}")
        if path.endswith("/stream"):
            raise AppError("generation changed")
        return _Response(b"{}")

    monkeypatch.setattr(backups.StatelessMcpContributor, "_request", staticmethod(always_changed))
    with pytest.raises(AppError, match="generation changed"):
        contributor.snapshot(tmp_path / "changed", backups.BackupContext())
    assert releases == 1

    def oversized(path: str, **_kwargs: object) -> _Response:
        if path.endswith("/release"):
            raise AppError("release unavailable")
        if path.endswith("/stream"):
            return _Response(b"x" * 32)
        return _Response(b"{}")

    monkeypatch.setattr(backups, "MAX_EXPANDED_BYTES", 8)
    monkeypatch.setattr(backups.StatelessMcpContributor, "_request", staticmethod(oversized))
    with pytest.raises(AppError, match="too large"):
        contributor.snapshot(tmp_path / "oversized", backups.BackupContext())

    def invalid_snapshot(path: str, **_kwargs: object) -> _Response:
        if path.endswith("/stream"):
            return _Response(b'{"type":"task","schemaVersion":1}\n')
        return _Response(b"{}")

    monkeypatch.setattr(backups, "MAX_EXPANDED_BYTES", 5 * 1024 * 1024 * 1024)
    monkeypatch.setattr(backups.StatelessMcpContributor, "_request", staticmethod(invalid_snapshot))
    with pytest.raises(AppError, match="incomplete"):
        contributor.snapshot(tmp_path / "invalid", backups.BackupContext())


def test_encrypted_restore_guard_paths_and_identity_api(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        backup_crypto,
        "generate_identity",
        lambda: {"identity": "AGE-SECRET-KEY-UNIT", "recipient": "age1unit"},
    )
    assert backups.generate_recovery_identity() == {
        "ok": True,
        "identity": "AGE-SECRET-KEY-UNIT",
        "recipient": "age1unit",
        "displayedOnce": True,
    }

    plain = backups.create_session({"mode": "full", "requiresFrontendState": False})
    plain_id = str(plain["backupId"])
    backups.finalize_session(plain_id)
    inspected = backups.inspect_archive(backups.backup_path(plain_id), filename="legacy.dsibackup")
    assert backups.unlock_restore(str(inspected["restoreId"]))["phase"] == "inspected"

    restore_id = "restore_changedciphertext"
    root = backups.RESTORE_DIR / restore_id
    root.mkdir(parents=True)
    cipher = root / "changed.dsibackup.age"
    cipher.write_bytes(_AGE_HEADER + b"changed")
    backups._write_json(
        root / "upload.json",
        {
            "restoreId": restore_id,
            "phase": "locked",
            "filename": cipher.name,
            "ciphertextSha256": "0" * 64,
            "protection": "passphrase",
        },
    )
    with pytest.raises(AppError, match="upload changed"):
        backups.unlock_restore(restore_id)

    restore_id = "restore_helpermissing"
    root = backups.RESTORE_DIR / restore_id
    root.mkdir(parents=True)
    cipher = root / "locked.dsibackup.age"
    cipher.write_bytes(_AGE_HEADER + b"locked")
    backups._write_json(
        root / "upload.json",
        {
            "restoreId": restore_id,
            "phase": "locked",
            "filename": cipher.name,
            "ciphertextSha256": backups._sha256_file(cipher),
            "protection": "passphrase",
        },
    )
    backup_crypto.put_secret(restore_id, "passphrase", "secret")
    monkeypatch.setattr(
        backup_crypto,
        "decrypt_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AppError("Backup crypto helper unavailable")),
    )
    with pytest.raises(AppError, match="helper unavailable"):
        backups.unlock_restore(restore_id)

    monkeypatch.setattr(backup_crypto, "capabilities", lambda: {"encryptedBackupAvailable": True})
    with pytest.raises(AppError, match="secret is required"):
        backups._build_archive(
            "backup_missingsecret",
            backups.BackupContext(),
            None,
            {"mode": "passphrase"},
            None,
        )
    session_dir = backups.BACKUP_DIR / "sessions" / "backup_missingsecret"
    assert not (session_dir / "staging").exists()
    assert not (session_dir / "verification").exists()
    assert not list(backups.BACKUP_DIR.glob("*missingsecret*"))


def test_backup_secret_is_not_consumed_before_frontend_state_is_ready(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_age(monkeypatch)
    created = backups.create_session(
        {
            "mode": "full",
            "requiresFrontendState": True,
            "protection": {"mode": "passphrase"},
            "coveragePolicy": "best-effort",
        }
    )
    backup_id = str(created["backupId"])
    backups.put_session_secret(backup_id, {"kind": "passphrase", "secret": "still-needed"})

    with pytest.raises(AppError, match="frontend state is required"):
        backups.finalize_session(backup_id)

    assert bytes(backup_crypto._SLOTS[backup_id].value) == b"still-needed"


def test_failed_archive_and_unlock_remove_partial_plaintext(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_age(monkeypatch)
    created = backups.create_session(
        {
            "mode": "full",
            "requiresFrontendState": False,
            "protection": {"mode": "passphrase"},
            "coveragePolicy": "best-effort",
        }
    )
    backup_id = str(created["backupId"])

    def fail_encrypt(target: Path, *_args: object, **_kwargs: object) -> None:
        target.write_bytes(b"partial ciphertext")
        raise AppError("encryption failed")

    monkeypatch.setattr(backup_crypto, "encrypt_stream", fail_encrypt)
    with pytest.raises(AppError, match="encryption failed"):
        backups._build_archive(
            backup_id,
            backups.BackupContext(coverage_policy="best-effort"),
            None,
            {"mode": "passphrase"},
            bytearray(_PASSPHRASE),
        )
    session_dir = backups.BACKUP_DIR / "sessions" / backup_id
    assert not (session_dir / "staging").exists()
    assert not (session_dir / "verification.dsibackup").exists()
    assert not list(backups.BACKUP_DIR.glob(f"*{backup_id[-8:]}*"))

    restore_id = "restore_partialplaintext"
    root = backups.RESTORE_DIR / restore_id
    root.mkdir(parents=True)
    cipher = root / "locked.dsibackup.age"
    cipher.write_bytes(_AGE_HEADER + b"locked")
    backups._write_json(
        root / "upload.json",
        {
            "restoreId": restore_id,
            "phase": "locked",
            "filename": cipher.name,
            "ciphertextSha256": backups._sha256_file(cipher),
            "protection": "passphrase",
        },
    )
    backup_crypto.put_secret(restore_id, "passphrase", _PASSPHRASE.decode())

    def partial_decrypt(_source: Path, target: Path, **_kwargs: object) -> None:
        target.write_bytes(b"not a zip")

    monkeypatch.setattr(backup_crypto, "decrypt_file", partial_decrypt)
    with pytest.raises(AppError, match="Unable to unlock backup"):
        backups.unlock_restore(restore_id)
    assert not (root / "unlocked.dsibackup").exists()
    assert not (root / "extracted").exists()


def test_external_restore_prepare_commit_complete(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    restore_id = "restore_externaltransaction"
    root = backups.RESTORE_DIR / restore_id
    root.mkdir(parents=True)
    archive = root / "unlocked.dsibackup"
    archive.write_bytes(b"verified archive")
    manifest: dict[str, object] = {
        "backupId": "backup_external_source",
        "scope": {"mode": "full"},
        "contributors": [{"id": "stateless-mcp", "schemaVersion": 1}],
    }
    backups._write_json(
        root / "plan.json",
        {
            "restoreId": restore_id,
            "compatible": True,
            "archiveSha256": backups._sha256_file(archive),
            "requiresFrontendApply": False,
            "operations": [{"contributorId": "stateless-mcp", "external": True}],
            "manifest": manifest,
            "phase": "inspected",
        },
    )
    snapshot = (
        '{"type":"task","schemaVersion":1,"task":{"id":"task-1"}}\n'
        '{"type":"complete","schemaVersion":1,"stateGeneration":1}\n'
    )

    def extract(_archive: Path, destination: Path) -> dict[str, object]:
        payload = destination / "payload" / "stateless-mcp"
        payload.mkdir(parents=True)
        (payload / "state.jsonl").write_text(snapshot, encoding="utf-8")
        return manifest

    monkeypatch.setattr(backups, "_safe_extract_and_verify", extract)
    monkeypatch.setattr(backups, "_create_safety_backup", lambda _plan, _restore_id: {"backupId": "backup_safety"})
    monkeypatch.setattr(backups, "_restore_identity_map", lambda _plan, _mode: {})
    monkeypatch.setattr(
        backups.StatelessMcpContributor,
        "capabilities",
        lambda _self: {"id": "stateless-mcp", "available": True, "schemaVersion": 1},
    )
    participant_calls: list[tuple[str, ...]] = []

    def fake_journal(phase: str) -> dict[str, object]:
        return {
            "sourceDigest": "a" * 64,
            "preparedDigest": "b" * 64,
            "phase": phase,
            "imported": 1,
            "skipped": 0,
            "interrupted": 1,
            "remapped": {},
        }

    monkeypatch.setattr(
        backups.StatelessMcpContributor,
        "prepare_restore",
        lambda _self, restore_id, _source, digest: participant_calls.append(("prepare", restore_id, digest)) or fake_journal("prepared"),
    )
    prepared = backups.prepare_restore(restore_id)
    assert prepared["phase"] == "backend-staged"
    transaction = backups._read_json(root / "transaction.json")
    assert transaction["contributors"][0]["external"] is True
    assert participant_calls[0][0] == "prepare"
    assert transaction["contributors"][0]["participant"]["imported"] == 1

    monkeypatch.setattr(
        backups.StatelessMcpContributor,
        "commit_restore_intent",
        lambda _self, restore_id, digest: participant_calls.append(("commit-intent", restore_id, digest)) or fake_journal("commit-intent"),
    )
    monkeypatch.setattr(
        backups.StatelessMcpContributor,
        "commit_restore",
        lambda _self, restore_id, digest: participant_calls.append(("commit", restore_id, digest)) or fake_journal("committed-pending-complete"),
    )
    committed = backups.commit_restore(restore_id)
    assert committed["phase"] == "backend-committed"
    assert [call[0] for call in participant_calls] == ["prepare", "commit-intent", "commit"]
    assert participant_calls[0][2] == participant_calls[1][2] == participant_calls[2][2]

    monkeypatch.setattr(
        backups.StatelessMcpContributor,
        "complete_restore",
        lambda _self, restore_id: participant_calls.append(("complete", restore_id)) or fake_journal("complete"),
    )
    completed = backups.complete_restore(restore_id)
    assert completed["phase"] == "complete"
    assert [call[0] for call in participant_calls] == ["prepare", "commit-intent", "commit", "complete"]


@pytest.mark.parametrize(
    ("kind", "protection", "expected"),
    [
        ("passphrase", "passphrase", {"mode": "passphrase"}),
        ("age-identity", "age-recipient", {"mode": "age-recipient", "recipients": ["age1derived"]}),
    ],
)
def test_encrypted_safety_backup_inherits_source_protection_and_zeroizes(
    kind: backup_crypto.SecretKind,
    protection: str,
    expected: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = bytearray(b"safety-secret")
    captured: dict[str, object] = {}
    monkeypatch.setattr(backup_crypto, "consume_secret", lambda _restore_id, _expected: (kind, original))
    monkeypatch.setattr(backup_crypto, "derive_recipients", lambda _secret: ("age1derived",))

    def create(payload: dict[str, object]) -> dict[str, object]:
        captured["request"] = payload
        return {"backupId": "backup_safetyunit"}

    def finalize(backup_id: str, *, owner_restore_id: str | None = None) -> dict[str, object]:
        captured["backupId"] = backup_id
        captured["ownerRestoreId"] = owner_restore_id
        captured["stored"] = bytes(backup_crypto._SLOTS[backup_id].value)
        return {"backupId": backup_id, "phase": "ready"}

    monkeypatch.setattr(backups, "create_session", create)
    monkeypatch.setattr(backups, "finalize_session", finalize)
    result = backups._create_safety_backup({"encrypted": True, "protection": protection}, "restore_safetyunit")
    assert result["phase"] == "ready"
    assert captured["request"] == {
        "mode": "full",
        "includeHistory": True,
        "requiresFrontendState": False,
        "coveragePolicy": "best-effort",
        "protection": expected,
    }
    assert captured["stored"] == b"safety-secret"
    assert bytes(original) == b"\0" * len(original)


def test_safety_backup_zeroizes_secret_when_session_creation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    original = bytearray(b"safety-secret")
    monkeypatch.setattr(backup_crypto, "consume_secret", lambda _restore_id, _expected: ("passphrase", original))
    monkeypatch.setattr(
        backups,
        "create_session",
        lambda _payload: (_ for _ in ()).throw(AppError("session creation failed")),
    )

    with pytest.raises(AppError, match="session creation failed"):
        backups._create_safety_backup({"encrypted": True, "protection": "passphrase"}, "restore_safetyfailure")

    assert bytes(original) == b"\0" * len(original)


def test_terminal_restore_cleanup_removes_plaintext_and_upload(tmp_settings: Path) -> None:
    restore_id = "restore_terminalcleanup"
    root = backups.RESTORE_DIR / restore_id
    root.mkdir(parents=True)
    for directory in ("extracted", "verified", "staged", "rollback"):
        (root / directory).mkdir()
        (root / directory / "secret.txt").write_text("sensitive", encoding="utf-8")
    (root / "uploaded.dsibackup.age").write_bytes(b"ciphertext")
    (root / "unlocked.dsibackup").write_bytes(b"plaintext")
    backups._write_json(root / "upload.json", {"restoreId": restore_id, "phase": "locked"})
    backups._write_json(
        root / "transaction.json",
        {"restoreId": restore_id, "phase": "rolled-back", "contributors": [], "updatedAt": backups._utc_iso()},
    )
    backup_crypto.put_secret(restore_id, "passphrase", "terminal-secret")

    result = backups.abort_restore(restore_id)

    assert result["phase"] == "rolled-back"
    assert not any((root / name).exists() for name in ("extracted", "verified", "staged", "rollback"))
    assert not list(root.glob("*.dsibackup*"))
    assert not (root / "upload.json").exists()
    assert restore_id not in backup_crypto._SLOTS


def test_external_and_file_rollback_paths_are_deterministic(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = backups.RESTORE_DIR / "restore_rollbackunit"
    root.mkdir(parents=True)
    destination = root / "installed-file"
    destination.write_text("installed", encoding="utf-8")
    transaction: dict[str, object] = {
        "restoreId": "restore_rollbackunit",
        "phase": "backend-committed",
        "contributors": [
            {"id": "stateless-mcp", "external": True, "swapped": True},
            {
                "id": "local-file",
                "destination": str(destination),
                "rollbackPath": str(root / "rollback" / "local-file"),
                "hadDestination": False,
                "swapped": True,
                "swapState": "swapped",
            },
        ],
    }
    backups._rollback_transaction(transaction, root)
    assert not destination.exists()
    assert transaction["phase"] == "recovery-required"
    contributors = transaction["contributors"]
    assert isinstance(contributors, list)
    assert contributors[0]["swapState"] == "recovery-required"

    reachable: dict[str, object] = {
        "restoreId": "restore_rollbackunit",
        "phase": "commit-intent",
        "contributors": [{"id": "stateless-mcp", "external": True, "swapped": False}],
    }
    monkeypatch.setattr(
        backups.StatelessMcpContributor,
        "abort_restore",
        lambda _self, restore_id: {
            "sourceDigest": "a" * 64,
            "preparedDigest": "b" * 64,
            "phase": "rolled-back",
            "imported": 0,
            "skipped": 0,
            "interrupted": 0,
            "remapped": {},
        },
    )
    backups._rollback_transaction(reachable, root)
    assert reachable["phase"] == "rolled-back"
    external = reachable["contributors"]
    assert isinstance(external, list)
    assert external[0]["swapState"] == "rolled-back"
    assert external[0]["participant"]["phase"] == "rolled-back"

    with pytest.raises(AppError, match="journal is invalid"):
        backups._rollback_transaction({"restoreId": "restore_invalid", "contributors": {}}, root)


def test_durability_helpers_tolerate_platform_limits_and_reject_invalid_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    regular = tmp_path / "regular.txt"
    regular.write_text("copy", encoding="utf-8")
    copied = tmp_path / "copied.txt"
    backups._copy_consistent(regular, copied)
    assert copied.read_text(encoding="utf-8") == "copy"

    invalid_db = tmp_path / "invalid.db"
    invalid_db.write_text("not sqlite", encoding="utf-8")
    with pytest.raises(AppError, match="consistent SQLite snapshot"):
        backups._copy_consistent(invalid_db, tmp_path / "snapshot.db")
    assert not (tmp_path / "snapshot.db").exists()

    monkeypatch.setattr(backups.os, "fsync", lambda _descriptor: (_ for _ in ()).throw(OSError("unsupported")))
    backups._fsync_file(regular)
    backups._fsync_directory(tmp_path)
    monkeypatch.setattr(backups.os, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unsupported")))
    backups._fsync_directory(tmp_path)

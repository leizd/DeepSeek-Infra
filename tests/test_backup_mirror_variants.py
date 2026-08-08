from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_crypto,
    backup_mirror,
    backup_policies,
    backup_scheduled,
    backup_unattended,
)


RECIPIENT_A = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"
RECIPIENT_B = "age1qyqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqp"
RECIPIENT_C = "age1qzqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqs"


@pytest.fixture
def stub_crypto(monkeypatch: pytest.MonkeyPatch) -> None:
    def encrypt_stream(target: Path, write_plaintext: object, *, mode: str, secret: object = None, recipients: tuple[str, ...] = (), cancel_event: object = None) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        buffer = __import__("io").BytesIO()
        write_plaintext(buffer)  # type: ignore[operator]
        marker = ("|".join(sorted(recipients))).encode()
        target.write_bytes(b"age:" + marker + b"\n" + bytes(buffer.getbuffer())[::-1])

    def decrypt_file(source: Path, target: Path, *, kind: str, secret: bytearray, cancel_event: object = None) -> None:
        raw = source.read_bytes()
        assert raw.startswith(b"age:")
        target.write_bytes(raw[4:].split(b"\n", 1)[1][::-1])

    monkeypatch.setattr(backup_crypto, "encrypt_stream", encrypt_stream)
    monkeypatch.setattr(backup_crypto, "decrypt_file", decrypt_file)
    monkeypatch.setattr(backup_crypto, "generate_identity", lambda: {"identity": "AGE-SECRET-KEY-1EPHEMERAL", "recipient": "age1ephemeral"})
    monkeypatch.setattr(backup_crypto, "inspect_header", lambda _path: {"age": True})


def _envelope() -> dict[str, object]:
    body: dict[str, object] = {
        "schemaVersion": 1,
        "sourceVersion": "4.4.6",
        "createdAt": 1,
        "conversations": [{"conversationId": "c1", "headRevision": "r1", "checkpoint": {"messages": []}}],
        "conflicts": [],
    }
    body["digest"] = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return body


def _policy(name: str, recipients: list[str]) -> dict[str, object]:
    return backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": name,
            "enabled": True,
            "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
            "scope": {"mode": "full", "includeExternalState": False, "coveragePolicy": "best-effort"},
            "frontendMirror": {"mode": "best-effort"},
            "protection": {"mode": "age-recipient", "recipients": recipients},
            "targetId": "managed-local",
        }
    )


def _digest(recipients: list[str]) -> str:
    return backup_policies.recipient_set_digest(recipients)


def _generation_dir(tmp_settings: Path, generation_id: str) -> Path:
    return tmp_settings / ".backup-mirror" / "mirror_main" / "generations" / generation_id


def test_multi_policy_recipient_variants(tmp_settings: Path, stub_crypto: None) -> None:
    _policy("daily-local", [RECIPIENT_A])
    _policy("weekly-offsite", [RECIPIENT_B])
    metadata = backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1")
    digests = {str(variant["recipientSetDigest"]) for variant in metadata["recipientVariants"]}
    assert digests == {_digest([RECIPIENT_A]), _digest([RECIPIENT_B])}
    generation_dir = _generation_dir(tmp_settings, str(metadata["generationId"]))
    files = sorted(path.name for path in generation_dir.glob("state.*.age"))
    assert files == sorted([f"state.{_digest([RECIPIENT_A])[:16]}.age", f"state.{_digest([RECIPIENT_B])[:16]}.age"])
    assert backup_mirror.mirror_status("mirror_main", recipients=[RECIPIENT_A])["status"] == "current"
    assert backup_mirror.mirror_status("mirror_main", recipients=[RECIPIENT_B])["status"] == "current"
    assert backup_mirror.mirror_status("mirror_main", recipients=[RECIPIENT_A, RECIPIENT_C])["status"] == "recipient-mismatch"


def test_archive_group_gets_third_variant(tmp_settings: Path, stub_crypto: None) -> None:
    _policy("daily-local", [RECIPIENT_A])
    _policy("weekly-offsite", [RECIPIENT_B])
    _policy("archive", [RECIPIENT_A, RECIPIENT_C])
    metadata = backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1")
    digests = {str(variant["recipientSetDigest"]) for variant in metadata["recipientVariants"]}
    assert digests == {_digest([RECIPIENT_A]), _digest([RECIPIENT_B]), _digest([RECIPIENT_A, RECIPIENT_C])}
    assert backup_mirror.mirror_status("mirror_main", recipients=[RECIPIENT_A, RECIPIENT_C])["status"] == "current"


def test_policy_reads_only_its_own_variant(tmp_settings: Path, stub_crypto: None) -> None:
    _policy("daily-local", [RECIPIENT_A])
    _policy("weekly-offsite", [RECIPIENT_B])
    backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1")
    ciphertext_a, _, _ = backup_mirror.mirror_files("mirror_main", recipients=[RECIPIENT_A])
    ciphertext_b, _, _ = backup_mirror.mirror_files("mirror_main", recipients=[RECIPIENT_B])
    assert ciphertext_a != ciphertext_b
    assert ciphertext_a.is_file() and ciphertext_b.is_file()
    assert f"{_digest([RECIPIENT_A])[:16]}" in ciphertext_a.name
    assert f"{_digest([RECIPIENT_B])[:16]}" in ciphertext_b.name
    assert backup_unattended.sha256_file(ciphertext_a) != backup_unattended.sha256_file(ciphertext_b)
    with pytest.raises(AppError) as exc:
        backup_mirror.mirror_files("mirror_main", recipients=[RECIPIENT_A, RECIPIENT_C])
    assert exc.value.status == 404
    assert "no variant sealed to this recipient set" in str(exc.value)


def test_explicit_recipients_produce_single_variant(tmp_settings: Path, stub_crypto: None) -> None:
    _policy("daily-local", [RECIPIENT_A])
    _policy("weekly-offsite", [RECIPIENT_B])
    metadata = backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_B])
    assert [str(variant["recipientSetDigest"]) for variant in metadata["recipientVariants"]] == [_digest([RECIPIENT_B])]


def test_idempotent_with_variants(tmp_settings: Path, stub_crypto: None) -> None:
    _policy("daily-local", [RECIPIENT_A])
    _policy("weekly-offsite", [RECIPIENT_B])
    first = backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1", client_sequence=1)
    second = backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1", client_sequence=2)
    assert second.get("idempotent") is True
    assert second["generationId"] == first["generationId"]


def test_scheduled_backups_share_mirror_without_mismatch(tmp_settings: Path, stub_crypto: None) -> None:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('{"items":[]}', encoding="utf-8")
    policy_a = _policy("daily-local", [RECIPIENT_A])
    policy_b = _policy("weekly-offsite", [RECIPIENT_B])
    backup_mirror.put_frontend_mirror("mirror_default", _envelope(), source_epoch="epoch-1")
    for policy in (policy_a, policy_b):
        metadata, coverage = backup_scheduled.mirror_coverage(policy)
        assert metadata is not None
        assert coverage["status"] == "current"
        package = backup_scheduled.build_scheduled_backup(policy, run_id=f"run_{policy['policyId']}", staging_root=tmp_settings / ".staging", schedule_slot="2026-06-02T03:00@UTC")
        assert package.frontend["status"] == "current"
        assert package.creation_verified is True

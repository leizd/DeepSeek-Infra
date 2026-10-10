from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_crypto, backup_mirror, backup_policies, mutation_gate


RECIPIENT_A = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"
RECIPIENT_B = "age1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa0"


@pytest.fixture
def stub_crypto(monkeypatch: pytest.MonkeyPatch) -> None:
    def encrypt_stream(target: Path, write_plaintext: object, *, mode: str, secret: object = None, recipients: tuple[str, ...] = (), cancel_event: object = None) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        buffer = __import__("io").BytesIO()
        write_plaintext(buffer)  # type: ignore[operator]
        target.write_bytes(b"age:" + bytes(buffer.getbuffer())[::-1])

    def decrypt_file(source: Path, target: Path, *, kind: str, secret: bytearray, cancel_event: object = None) -> None:
        raw = source.read_bytes()
        assert raw.startswith(b"age:")
        target.write_bytes(raw[4:][::-1])

    monkeypatch.setattr(backup_crypto, "encrypt_stream", encrypt_stream)
    monkeypatch.setattr(backup_crypto, "decrypt_file", decrypt_file)
    monkeypatch.setattr(
        backup_crypto,
        "generate_identity",
        lambda: {"identity": "AGE-SECRET-KEY-1EPHEMERAL", "recipient": "age1ephemeral"},
    )


def _envelope(conversation_id: str = "c1") -> dict[str, object]:
    body: dict[str, object] = {
        "schemaVersion": 1,
        "sourceVersion": "4.4.4",
        "createdAt": 1,
        "conversations": [{"conversationId": conversation_id, "headRevision": "r1", "checkpoint": {"messages": []}}],
        "conflicts": [],
    }
    body["digest"] = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return body


def test_mirror_round_trip_and_metadata_shape(tmp_settings: Path, stub_crypto: None) -> None:
    metadata = backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-01T00:00:00Z")
    assert metadata["schemaVersion"] == 2
    assert metadata["profileId"] == "mirror_main"
    assert metadata["sourceEpoch"] == "epoch-1"
    assert metadata["conversations"] == 1
    assert metadata["conflicts"] == 0
    assert metadata["creationVerified"] is True
    assert metadata["generationId"].startswith("gen_")
    assert metadata["parentGenerationId"] is None
    assert len(metadata["ciphertextSha256"]) == 64
    ciphertext, meta_path, loaded = backup_mirror.mirror_files("mirror_main")
    raw = ciphertext.read_bytes()
    assert raw.startswith(b"age:")
    assert b'"conversationId"' not in raw
    assert meta_path.is_file()
    assert loaded["envelopeDigest"] == metadata["envelopeDigest"]
    meta_keys = set(loaded)
    assert meta_keys == {
        "schemaVersion",
        "profileId",
        "generationId",
        "parentGenerationId",
        "sourceEpoch",
        "clientReplicaId",
        "clientSequence",
        "envelopeDigest",
        "recipientVariants",
        "recipientSetDigest",
        "conversations",
        "conflicts",
        "createdAt",
        "acknowledgedAt",
        "ciphertextSha256",
        "creationVerified",
    }


def test_mirror_rejects_invalid_digest_and_forbidden_keys(tmp_settings: Path, stub_crypto: None) -> None:
    envelope = _envelope()
    envelope["digest"] = "0" * 64
    with pytest.raises(AppError, match="digest"):
        backup_mirror.put_frontend_mirror("mirror_main", envelope, source_epoch="epoch-1", recipients=[RECIPIENT_A])
    forbidden = _envelope()
    forbidden["writerSessionId"] = "ws-1"
    forbidden["digest"] = hashlib.sha256(
        json.dumps({key: value for key, value in forbidden.items() if key != "digest"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(AppError):
        backup_mirror.put_frontend_mirror("mirror_main", forbidden, source_epoch="epoch-1", recipients=[RECIPIENT_A])


def test_mirror_idempotent_reupload_skips(tmp_settings: Path, stub_crypto: None) -> None:
    first = backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-01T00:00:00Z")
    second = backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-02T00:00:00Z")
    assert second.get("idempotent") is True
    assert second["createdAt"] == first["createdAt"]


def test_mirror_rejects_stale_and_previous_epoch_uploads(tmp_settings: Path, stub_crypto: None) -> None:
    backup_mirror.put_frontend_mirror("mirror_main", _envelope("one"), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-01T00:00:00Z", client_sequence=1)
    backup_mirror.put_frontend_mirror("mirror_main", _envelope("two"), source_epoch="epoch-2", recipients=[RECIPIENT_A], acknowledged_at="2026-01-02T00:00:00Z", client_sequence=2)
    with pytest.raises(AppError) as stale:
        backup_mirror.put_frontend_mirror("mirror_main", _envelope("one-b"), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-04T00:00:00Z", client_sequence=3)
    assert stale.value.status == 409
    assert "mirror-stale-epoch" in str(stale.value)
    updated = backup_mirror.put_frontend_mirror("mirror_main", _envelope("newer"), source_epoch="epoch-3", recipients=[RECIPIENT_A], acknowledged_at="2026-01-03T00:00:00Z", client_sequence=4)
    assert updated["sourceEpoch"] == "epoch-3"
    parent = tmp_settings / ".backup-mirror" / "mirror_main" / "generations" / str(updated["parentGenerationId"]) / "metadata.json"
    assert json.loads(parent.read_text(encoding="utf-8"))["sourceEpoch"] == "epoch-2"


def test_mirror_updates_rejected_during_restore_fence(tmp_settings: Path, stub_crypto: None) -> None:
    mutation_gate.write_fence({"restoreId": "r1", "phase": "commit"}, root=tmp_settings)
    try:
        with pytest.raises(AppError) as fenced:
            backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A])
        assert fenced.value.status == 423
    finally:
        mutation_gate.clear_fence("r1", root=tmp_settings)


def test_mirror_recipient_change_rotates_and_marks_mismatch(tmp_settings: Path, stub_crypto: None) -> None:
    backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-01T00:00:00Z", client_sequence=1)
    status = backup_mirror.mirror_status("mirror_main", recipients=[RECIPIENT_B])
    assert status["status"] == "recipient-mismatch"
    backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_B], acknowledged_at="2026-01-02T00:00:00Z", client_sequence=2)
    assert backup_mirror.mirror_status("mirror_main", recipients=[RECIPIENT_B])["status"] == "current"
    head = json.loads((tmp_settings / ".backup-mirror" / "mirror_main" / "HEAD.json").read_text(encoding="utf-8"))
    parent_dir = tmp_settings / ".backup-mirror" / "mirror_main" / "generations" / str(head["generationId"])
    assert list(parent_dir.glob("state.*.age"))
    metadata = json.loads((parent_dir / "metadata.json").read_text(encoding="utf-8"))
    previous = tmp_settings / ".backup-mirror" / "mirror_main" / "generations" / str(metadata["parentGenerationId"])
    assert list(previous.glob("state.*.age"))


def test_mirror_status_matrix(tmp_settings: Path, stub_crypto: None) -> None:
    assert backup_mirror.mirror_status("mirror_main")["status"] == "missing"
    assert backup_mirror.mirror_status(None, excluded=True)["status"] == "excluded"
    backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-01T00:00:00Z")
    assert backup_mirror.mirror_status("mirror_main")["status"] == "current"
    assert backup_mirror.mirror_status("mirror_main", expected_epoch="epoch-2")["status"] == "epoch-mismatch"
    from datetime import datetime, timezone

    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert backup_mirror.mirror_status("mirror_main", max_age_seconds=60, now=now)["status"] == "stale"
    assert backup_mirror.mirror_status("mirror_main", max_age_seconds=90000, now=now)["status"] == "current"


def test_mirror_uses_policy_recipients_when_omitted(tmp_settings: Path, stub_crypto: None) -> None:
    backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": "nightly",
            "enabled": True,
            "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
            "protection": {"mode": "age-recipient", "recipients": [RECIPIENT_A]},
            "targetId": "managed-local",
        }
    )
    metadata = backup_mirror.put_frontend_mirror("mirror_default", _envelope(), source_epoch="epoch-1")
    assert metadata["recipientSetDigest"] == backup_policies.recipient_set_digest([RECIPIENT_A])


def test_mirror_listing_and_latest(tmp_settings: Path, stub_crypto: None) -> None:
    assert backup_mirror.list_mirrors() == []
    assert backup_mirror.latest_mirror() is None
    backup_mirror.put_frontend_mirror("mirror_a", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-01T00:00:00Z")
    backup_mirror.put_frontend_mirror("mirror_b", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-03T00:00:00Z")
    assert {item["profileId"] for item in backup_mirror.list_mirrors()} == {"mirror_a", "mirror_b"}
    assert backup_mirror.latest_mirror()["profileId"] == "mirror_b"  # type: ignore[index]


def test_mirror_validation_errors(tmp_settings: Path, stub_crypto: None) -> None:
    with pytest.raises(AppError):
        backup_mirror.put_frontend_mirror("bad/profile", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A])
    with pytest.raises(AppError):
        backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="", recipients=[RECIPIENT_A])
    with pytest.raises(AppError):
        backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="not-a-time")
    with pytest.raises(AppError):
        backup_mirror.mirror_files("mirror_missing")
    with pytest.raises(AppError):
        backup_mirror.put_frontend_mirror("mirror_main", "not-a-dict", source_epoch="epoch-1", recipients=[RECIPIENT_A])  # type: ignore[arg-type]


def test_mirror_round_trip_failure_raises(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def encrypt_stream(target: Path, write_plaintext: object, *, mode: str, secret: object = None, recipients: tuple[str, ...] = (), cancel_event: object = None) -> None:
        target.write_bytes(b"age:tampered")

    def decrypt_file(source: Path, target: Path, *, kind: str, secret: bytearray, cancel_event: object = None) -> None:
        target.write_bytes(source.read_bytes()[4:])

    monkeypatch.setattr(backup_crypto, "encrypt_stream", encrypt_stream)
    monkeypatch.setattr(backup_crypto, "decrypt_file", decrypt_file)
    monkeypatch.setattr(backup_crypto, "generate_identity", lambda: {"identity": "AGE-SECRET-KEY-1X", "recipient": "age1x"})
    with pytest.raises(AppError, match="round-trip"):
        backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A])
    assert backup_mirror.mirror_status("mirror_main")["status"] == "missing"

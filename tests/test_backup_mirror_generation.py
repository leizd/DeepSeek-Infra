from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_crypto, backup_mirror, backup_unattended


RECIPIENT_A = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"
RECIPIENT_B = "age1qyqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqp"


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
    monkeypatch.setattr(backup_crypto, "generate_identity", lambda: {"identity": "AGE-SECRET-KEY-1EPHEMERAL", "recipient": "age1ephemeral"})
    monkeypatch.setattr(backup_crypto, "inspect_header", lambda _path: {"age": True})


def _envelope(marker: str = "v1") -> dict[str, object]:
    body: dict[str, object] = {
        "schemaVersion": 1,
        "sourceVersion": "4.4.6",
        "createdAt": 1,
        "conversations": [{"conversationId": marker, "headRevision": "r1", "checkpoint": {"messages": []}}],
        "conflicts": [],
    }
    body["digest"] = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return body


def _mirror_root(tmp_settings: Path) -> Path:
    return tmp_settings / ".backup-mirror" / "mirror_main"


def _head_generation(tmp_settings: Path) -> str:
    return str(json.loads((_mirror_root(tmp_settings) / "HEAD.json").read_text(encoding="utf-8"))["generationId"])


def test_put_creates_immutable_generation_layout(tmp_settings: Path, stub_crypto: None) -> None:
    metadata = backup_mirror.put_frontend_mirror(
        "mirror_main",
        _envelope(),
        source_epoch="epoch-1",
        recipients=[RECIPIENT_A],
        acknowledged_at="2026-01-01T00:00:00Z",
        client_replica_id="replica-1",
        client_sequence=7,
    )
    root = _mirror_root(tmp_settings)
    generation_dir = root / "generations" / metadata["generationId"]
    variant = metadata["recipientVariants"][0]
    ciphertext = generation_dir / variant["filename"]
    assert ciphertext.is_file()
    assert (generation_dir / "metadata.json").is_file()
    assert metadata["schemaVersion"] == 2
    assert metadata["parentGenerationId"] is None
    assert metadata["clientReplicaId"] == "replica-1"
    assert metadata["clientSequence"] == 7
    assert variant["ciphertextSha256"] == metadata["ciphertextSha256"]
    assert backup_unattended.sha256_file(ciphertext) == variant["ciphertextSha256"]
    head = json.loads((root / "HEAD.json").read_text(encoding="utf-8"))
    assert head["generationId"] == metadata["generationId"]


def test_read_uses_single_immutable_generation(tmp_settings: Path, stub_crypto: None) -> None:
    first = backup_mirror.put_frontend_mirror("mirror_main", _envelope("one"), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-01T00:00:00Z", client_sequence=1)
    first_files = backup_mirror.mirror_files("mirror_main")
    second = backup_mirror.put_frontend_mirror("mirror_main", _envelope("two"), source_epoch="epoch-2", recipients=[RECIPIENT_A], acknowledged_at="2026-01-02T00:00:00Z", client_sequence=2)
    second_files = backup_mirror.mirror_files("mirror_main")
    assert first_files[0] != second_files[0]
    assert first_files[0].is_file()
    assert first_files[1].is_file()
    assert str(first_files[2]["generationId"]) == first["generationId"]
    assert str(second_files[2]["generationId"]) == second["generationId"]
    assert str(second_files[2]["parentGenerationId"]) == first["generationId"]


def test_prune_keeps_head_and_parent_only(tmp_settings: Path, stub_crypto: None) -> None:
    ids = []
    for index in range(4):
        metadata = backup_mirror.put_frontend_mirror("mirror_main", _envelope(f"v{index}"), source_epoch=f"epoch-{index}", recipients=[RECIPIENT_A], acknowledged_at=f"2026-01-0{index + 1}T00:00:00Z", client_sequence=index + 1)
        ids.append(str(metadata["generationId"]))
    generations = sorted(path.name for path in (_mirror_root(tmp_settings) / "generations").iterdir())
    assert generations == sorted([ids[-1], ids[-2]])
    assert _head_generation(tmp_settings) == ids[-1]


def test_crash_before_head_update_keeps_old_head(tmp_settings: Path, stub_crypto: None, monkeypatch: pytest.MonkeyPatch) -> None:
    first = backup_mirror.put_frontend_mirror("mirror_main", _envelope("one"), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-01T00:00:00Z")
    original_write = backup_mirror._atomic_write

    def fail_on_head(path: Path, data: bytes) -> None:
        if path.name == backup_mirror.HEAD_NAME:
            raise RuntimeError("crash before HEAD")
        original_write(path, data)

    monkeypatch.setattr(backup_mirror, "_atomic_write", fail_on_head)
    with pytest.raises(RuntimeError):
        backup_mirror.put_frontend_mirror("mirror_main", _envelope("two"), source_epoch="epoch-2", recipients=[RECIPIENT_A], acknowledged_at="2026-01-02T00:00:00Z", client_sequence=2)
    monkeypatch.setattr(backup_mirror, "_atomic_write", original_write)
    ciphertext, meta_path, metadata = backup_mirror.mirror_files("mirror_main")
    assert metadata["generationId"] == first["generationId"]
    recovered = backup_mirror.put_frontend_mirror("mirror_main", _envelope("three"), source_epoch="epoch-3", recipients=[RECIPIENT_A], acknowledged_at="2026-01-03T00:00:00Z", client_sequence=3)
    assert recovered["parentGenerationId"] == first["generationId"]
    generations = sorted(path.name for path in (_mirror_root(tmp_settings) / "generations").iterdir())
    assert generations == sorted([str(first["generationId"]), str(recovered["generationId"])])


def test_read_rejects_tampered_ciphertext(tmp_settings: Path, stub_crypto: None) -> None:
    backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-01T00:00:00Z")
    ciphertext, _, _ = backup_mirror.mirror_files("mirror_main")
    ciphertext.write_bytes(b"tampered")
    with pytest.raises(AppError) as exc:
        backup_mirror.mirror_files("mirror_main")
    assert exc.value.status == 409
    assert "mirror-generation-corrupt" in str(exc.value)


def test_same_digest_different_epoch_is_not_idempotent(tmp_settings: Path, stub_crypto: None) -> None:
    first = backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-01T00:00:00Z", client_sequence=1)
    second = backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-2", recipients=[RECIPIENT_A], acknowledged_at="2026-01-02T00:00:00Z", client_sequence=2)
    assert not second.get("idempotent")
    assert second["generationId"] != first["generationId"]
    third = backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-2", recipients=[RECIPIENT_A], acknowledged_at="2026-01-03T00:00:00Z", client_sequence=3)
    assert third.get("idempotent") is True
    assert third["generationId"] == second["generationId"]


def test_expected_head_conflict_rejected(tmp_settings: Path, stub_crypto: None) -> None:
    first = backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-01T00:00:00Z", client_sequence=1)
    with pytest.raises(AppError) as exc:
        backup_mirror.put_frontend_mirror(
            "mirror_main",
            _envelope("two"),
            source_epoch="epoch-2",
            recipients=[RECIPIENT_A],
            acknowledged_at="2026-01-02T00:00:00Z",
            client_sequence=2,
            expected_head_generation_id="gen_stale00000000",
        )
    assert exc.value.status == 409
    assert "mirror-head-conflict" in str(exc.value)
    matched = backup_mirror.put_frontend_mirror(
        "mirror_main",
        _envelope("two"),
        source_epoch="epoch-2",
        recipients=[RECIPIENT_A],
        acknowledged_at="2026-01-02T00:00:00Z",
        client_sequence=3,
        expected_head_generation_id=str(first["generationId"]),
    )
    assert matched["parentGenerationId"] == first["generationId"]


def test_legacy_mirror_read_fallback(tmp_settings: Path, stub_crypto: None) -> None:
    root = _mirror_root(tmp_settings)
    root.mkdir(parents=True)
    payload = b"age:legacy"
    (root / "frontend-state.age").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (root / "frontend-state.meta.json").write_text(
        json.dumps({"schemaVersion": 1, "profileId": "mirror_main", "sourceEpoch": "epoch-legacy", "envelopeDigest": "x", "recipientSetDigest": "y", "conversations": 1, "conflicts": 0, "createdAt": "2025-01-01T00:00:00Z", "acknowledgedAt": "2025-01-01T00:00:00Z", "ciphertextSha256": digest, "creationVerified": True}),
        encoding="utf-8",
    )
    ciphertext, meta_path, metadata = backup_mirror.mirror_files("mirror_main")
    assert ciphertext.read_bytes() == payload
    assert metadata["sourceEpoch"] == "epoch-legacy"
    assert backup_mirror.mirror_status("mirror_main")["status"] == "current"
    (root / "frontend-state.age").write_bytes(b"tampered")
    with pytest.raises(AppError, match="mirror-generation-corrupt"):
        backup_mirror.mirror_files("mirror_main")


def test_first_generation_removes_legacy_files(tmp_settings: Path, stub_crypto: None) -> None:
    root = _mirror_root(tmp_settings)
    root.mkdir(parents=True)
    (root / "frontend-state.age").write_bytes(b"age:legacy")
    digest = hashlib.sha256(b"age:legacy").hexdigest()
    (root / "frontend-state.meta.json").write_text(json.dumps({"sourceEpoch": "epoch-legacy", "acknowledgedAt": "2025-01-01T00:00:00Z", "ciphertextSha256": digest}), encoding="utf-8")
    backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-01T00:00:00Z")
    assert not (root / "frontend-state.age").exists()
    assert not (root / "frontend-state.meta.json").exists()
    assert (root / "HEAD.json").is_file()


def test_head_and_generation_metadata_edge_cases(tmp_settings: Path, stub_crypto: None) -> None:
    root = _mirror_root(tmp_settings)
    root.mkdir(parents=True)
    (root / "HEAD.json").write_text("{nope", encoding="utf-8")
    assert backup_mirror._read_head("mirror_main") is None
    assert backup_mirror._read_generation_metadata("mirror_main", "../evil") is None
    (root / "HEAD.json").write_text('{"generationId": "gen_deadbeef00"}', encoding="utf-8")
    assert backup_mirror.mirror_status("mirror_main")["status"] == "missing"
    generation_dir = root / "generations" / "gen_deadbeef00"
    generation_dir.mkdir(parents=True)
    (generation_dir / "metadata.json").write_text("{nope", encoding="utf-8")
    assert backup_mirror.mirror_status("mirror_main")["status"] == "missing"
    with pytest.raises(AppError, match="clientSequence"):
        backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], client_sequence="not-a-number")  # type: ignore[arg-type]
    metadata = backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-01T00:00:00Z")
    assert metadata["generationId"].startswith("gen_")
    assert backup_mirror.mirror_status("mirror_main")["status"] == "current"


def test_mirror_files_default_reads_first_variant(tmp_settings: Path, stub_crypto: None) -> None:
    from deepseek_infra.infra.workspace import backup_policies

    for name, recipients in (("daily", [RECIPIENT_A]), ("weekly", [RECIPIENT_B])):
        backup_policies.create_policy(
            {
                "schemaVersion": 1,
                "name": name,
                "enabled": True,
                "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
                "protection": {"mode": "age-recipient", "recipients": recipients},
                "targetId": "managed-local",
            }
        )
    metadata = backup_mirror.put_frontend_mirror("mirror_main", _envelope(), source_epoch="epoch-1")
    ciphertext, _, _ = backup_mirror.mirror_files("mirror_main")
    first = sorted(metadata["recipientVariants"], key=lambda item: str(item["recipientSetDigest"]))[0]
    assert ciphertext.name == str(first["filename"])
    head = json.loads((_mirror_root(tmp_settings) / "HEAD.json").read_text(encoding="utf-8"))
    head["epochIndexes"] = {"epoch-1": "not-an-int"}
    (_mirror_root(tmp_settings) / "HEAD.json").write_text(json.dumps(head), encoding="utf-8")
    second = backup_mirror.put_frontend_mirror("mirror_main", _envelope("two"), source_epoch="epoch-2", client_sequence=1)
    assert second["generationId"]


def test_mirror_files_rejects_broken_generation_descriptor(tmp_settings: Path, stub_crypto: None) -> None:
    root = _mirror_root(tmp_settings)
    generation_dir = root / "generations" / "gen_beefface00"
    generation_dir.mkdir(parents=True)
    (generation_dir / "metadata.json").write_text(json.dumps({"schemaVersion": 2, "profileId": "mirror_main", "recipientVariants": []}), encoding="utf-8")
    (root / "HEAD.json").write_text('{"generationId": "gen_beefface00"}', encoding="utf-8")
    with pytest.raises(AppError, match="no recipient variants"):
        backup_mirror.mirror_files("mirror_main")
    (generation_dir / "metadata.json").write_text(json.dumps({"schemaVersion": 2, "profileId": "mirror_main", "recipientVariants": [{"recipientSetDigest": "x", "ciphertextSha256": "y", "filename": "evil/../evil.age"}]}), encoding="utf-8")
    with pytest.raises(AppError, match="invalid variant filename"):
        backup_mirror.mirror_files("mirror_main")
    (generation_dir / "metadata.json").write_text(json.dumps({"schemaVersion": 2, "profileId": "mirror_main", "recipientVariants": [{"recipientSetDigest": "x", "ciphertextSha256": "y", "filename": "state.deadbeef.age"}]}), encoding="utf-8")
    with pytest.raises(AppError, match="ciphertext is missing"):
        backup_mirror.mirror_files("mirror_main")


def test_sequence_must_increase_within_epoch(tmp_settings: Path, stub_crypto: None) -> None:
    backup_mirror.put_frontend_mirror("mirror_main", _envelope("one"), source_epoch="epoch-1", recipients=[RECIPIENT_A], client_sequence=5)
    with pytest.raises(AppError) as equal:
        backup_mirror.put_frontend_mirror("mirror_main", _envelope("two"), source_epoch="epoch-1", recipients=[RECIPIENT_A], client_sequence=5)
    assert "mirror-stale-sequence" in str(equal.value)
    with pytest.raises(AppError) as lower:
        backup_mirror.put_frontend_mirror("mirror_main", _envelope("three"), source_epoch="epoch-1", recipients=[RECIPIENT_A], client_sequence=4)
    assert "mirror-stale-sequence" in str(lower.value)
    accepted = backup_mirror.put_frontend_mirror("mirror_main", _envelope("two"), source_epoch="epoch-1", recipients=[RECIPIENT_A], client_sequence=6)
    assert accepted["clientSequence"] == 6


def test_unknown_epoch_takeover_requires_sequence_increase(tmp_settings: Path, stub_crypto: None) -> None:
    backup_mirror.put_frontend_mirror("mirror_main", _envelope("one"), source_epoch="epoch-1", recipients=[RECIPIENT_A], client_sequence=5)
    with pytest.raises(AppError) as exc:
        backup_mirror.put_frontend_mirror("mirror_main", _envelope("two"), source_epoch="epoch-restore", recipients=[RECIPIENT_A], client_sequence=2)
    assert "mirror-stale-sequence" in str(exc.value)
    taken_over = backup_mirror.put_frontend_mirror("mirror_main", _envelope("two"), source_epoch="epoch-restore", recipients=[RECIPIENT_A], client_sequence=6)
    assert taken_over["sourceEpoch"] == "epoch-restore"


def test_acknowledged_at_not_used_for_ordering(tmp_settings: Path, stub_crypto: None) -> None:
    backup_mirror.put_frontend_mirror("mirror_main", _envelope("one"), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-05T00:00:00Z", client_sequence=1)
    accepted = backup_mirror.put_frontend_mirror("mirror_main", _envelope("two"), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-01T00:00:00Z", client_sequence=2)
    assert accepted["acknowledgedAt"] == "2026-01-01T00:00:00Z"


def test_head_state_persists_epoch_registry(tmp_settings: Path, stub_crypto: None) -> None:
    backup_mirror.put_frontend_mirror("mirror_main", _envelope("one"), source_epoch="epoch-1", recipients=[RECIPIENT_A], client_sequence=1)
    backup_mirror.put_frontend_mirror("mirror_main", _envelope("two"), source_epoch="epoch-2", recipients=[RECIPIENT_A], client_sequence=2)
    head = json.loads((_mirror_root(tmp_settings) / "HEAD.json").read_text(encoding="utf-8"))
    assert head["schemaVersion"] == 2
    assert head["acceptedEpoch"] == "epoch-2"
    assert head["acceptedSequence"] == 2
    assert head["epochIndexes"] == {"epoch-1": 1, "epoch-2": 2}
    with pytest.raises(AppError) as exc:
        backup_mirror.put_frontend_mirror("mirror_main", _envelope("three"), source_epoch="epoch-1", recipients=[RECIPIENT_A], acknowledged_at="2026-01-06T00:00:00Z", client_sequence=3)
    assert "mirror-stale-epoch" in str(exc.value)


def test_legacy_mirror_accepts_any_first_v2_sequence(tmp_settings: Path, stub_crypto: None) -> None:
    root = _mirror_root(tmp_settings)
    root.mkdir(parents=True)
    (root / "frontend-state.age").write_bytes(b"age:legacy")
    digest = hashlib.sha256(b"age:legacy").hexdigest()
    (root / "frontend-state.meta.json").write_text(json.dumps({"sourceEpoch": "epoch-legacy", "acknowledgedAt": "2025-01-01T00:00:00Z", "ciphertextSha256": digest}), encoding="utf-8")
    first = backup_mirror.put_frontend_mirror("mirror_main", _envelope("one"), source_epoch="epoch-legacy", recipients=[RECIPIENT_A], client_sequence=0)
    assert first["generationId"]
    with pytest.raises(AppError) as exc:
        backup_mirror.put_frontend_mirror("mirror_main", _envelope("two"), source_epoch="epoch-legacy", recipients=[RECIPIENT_A], client_sequence=0)
    assert "mirror-stale-sequence" in str(exc.value)

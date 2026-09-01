from __future__ import annotations

import base64
import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from deepseek_infra.infra.workspace import federation_identity


UTC = timezone.utc


def _raw_public_key(encoded: str) -> bytes:
    padded = encoded + ("=" * (-len(encoded) % 4))
    return base64.b64decode(padded, altchars=b"-_", validate=True)


def _identity_fixture(tmp_path: Path) -> tuple[dict[str, object], dict[str, object], federation_identity.OnlineFleetSigner]:
    root_bundle = tmp_path / "offline" / "fleet-root.bundle.json"
    signer_bundle = tmp_path / "online" / "signer-1.bundle.json"
    issued_at = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)
    identity = federation_identity.create_fleet_root(
        "fleet-a",
        bundle_path=root_bundle,
        passphrase=b"root-passphrase-for-tests",
        now=issued_at,
    )
    certificate = federation_identity.issue_online_signer(
        root_bundle_path=root_bundle,
        root_passphrase=b"root-passphrase-for-tests",
        signer_bundle_path=signer_bundle,
        signer_passphrase=b"signer-passphrase-for-tests",
        sequence=1,
        not_before=issued_at,
        expires_at=issued_at + timedelta(days=30),
    )
    public_identity_path = tmp_path / "online" / "fleet-identity-v1.json"
    assert federation_identity.export_public_fleet_identity(root_bundle, public_identity_path) == identity
    assert federation_identity.read_public_fleet_identity(public_identity_path) == identity
    public_identity_bytes = public_identity_path.read_bytes()
    assert b"ENCRYPTED PRIVATE KEY" not in public_identity_bytes
    assert b"encryptedPrivateKeyPem" not in public_identity_bytes
    signer = federation_identity.load_online_signer(
        signer_bundle,
        b"signer-passphrase-for-tests",
        root_identity=identity,
        now=issued_at + timedelta(seconds=1),
    )
    return identity, certificate, signer


def _cross_certify_signer(
    *,
    root_bundle: Path,
    root_passphrase: bytes,
    source_certificate: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    identity, root_key = federation_identity._load_root_key(root_bundle, root_passphrase)
    certificate_payload: dict[str, object] = {
        "schema": federation_identity.ONLINE_SIGNER_CERTIFICATE_SCHEMA,
        "fleetId": identity["fleetId"],
        "rootKeyId": identity["rootKeyId"],
        "rootFingerprint": identity["rootFingerprint"],
        "signerKeyId": source_certificate["signerKeyId"],
        "signerPublicKey": source_certificate["signerPublicKey"],
        "signatureAlgorithm": federation_identity.SIGNATURE_ALGORITHM,
        "sequence": 1,
        "issuedAt": "2026-09-01T01:00:00Z",
        "notBefore": "2026-09-01T01:00:00Z",
        "expiresAt": "2026-10-01T01:00:00Z",
    }
    certificate = {
        **certificate_payload,
        "rootSignature": federation_identity._b64url_encode(
            root_key.sign(federation_identity._certificate_message(certificate_payload))
        ),
    }
    return identity, certificate


def test_dedicated_fleet_root_and_online_signer_are_encrypted_and_distinct(tmp_path: Path) -> None:
    root_bundle = tmp_path / "offline" / "fleet-root.bundle.json"
    signer_bundle = tmp_path / "online" / "signer-1.bundle.json"
    issued_at = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)

    identity = federation_identity.create_fleet_root(
        "fleet-a",
        bundle_path=root_bundle,
        passphrase=b"root-passphrase-for-tests",
        now=issued_at,
    )
    certificate = federation_identity.issue_online_signer(
        root_bundle_path=root_bundle,
        root_passphrase=b"root-passphrase-for-tests",
        signer_bundle_path=signer_bundle,
        signer_passphrase=b"signer-passphrase-for-tests",
        sequence=1,
        not_before=issued_at,
        expires_at=issued_at + timedelta(days=30),
    )

    assert identity == federation_identity.read_fleet_identity(root_bundle)
    assert identity["schema"] == "fleet-identity-v1"
    assert identity["fleetId"] == "fleet-a"
    assert identity["signatureAlgorithm"] == "Ed25519"
    assert str(identity["rootKeyId"]).startswith("fed-root-")
    assert str(identity["rootFingerprint"]).startswith("sha256:")
    assert len(_raw_public_key(str(identity["rootPublicKey"]))) == 32

    assert certificate["schema"] == "federation-online-signer-certificate-v1"
    assert certificate["fleetId"] == identity["fleetId"]
    assert certificate["rootKeyId"] == identity["rootKeyId"]
    assert certificate["rootFingerprint"] == identity["rootFingerprint"]
    assert str(certificate["signerKeyId"]).startswith("fed-signer-")
    assert certificate["signerKeyId"] != identity["rootKeyId"]
    assert _raw_public_key(str(certificate["signerPublicKey"])) != _raw_public_key(str(identity["rootPublicKey"]))
    assert federation_identity.validate_online_signer_certificate(
        certificate,
        identity,
        now=issued_at + timedelta(seconds=1),
    ) == []

    for path, passphrase in (
        (root_bundle, "root-passphrase-for-tests"),
        (signer_bundle, "signer-passphrase-for-tests"),
    ):
        raw = path.read_text(encoding="utf-8")
        bundle = json.loads(raw)
        envelope = bundle["privateKeyEnvelope"]
        assert envelope["schema"] == "federation-private-key-envelope-v1"
        assert envelope["kdf"] == {
            "algorithm": "Argon2id",
            "iterations": 3,
            "lanes": 4,
            "length": 32,
            "memoryKiB": 64 * 1024,
            "salt": envelope["kdf"]["salt"],
        }
        assert len(_raw_public_key(envelope["kdf"]["salt"])) == 16
        assert envelope["aead"]["algorithm"] == "AES-256-GCM"
        assert len(_raw_public_key(envelope["aead"]["nonce"])) == 12
        assert len(_raw_public_key(envelope["ciphertext"])) > 32
        assert "BEGIN PRIVATE KEY" not in raw  # pragma: allowlist secret
        assert "BEGIN ENCRYPTED PRIVATE KEY" not in raw  # pragma: allowlist secret
        assert "encryptedPrivateKeyPem" not in raw
        assert passphrase not in raw
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600

    public_blob = json.dumps({"identity": identity, "certificate": certificate}, sort_keys=True).casefold()
    assert "privatekey" not in public_blob.replace("_", "").replace("-", "")
    assert "age-secret-key" not in public_blob
    assert "control-authority" not in public_blob


def test_private_key_envelope_rejects_kdf_downgrade_and_ciphertext_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_bundle = tmp_path / "root.bundle.json"
    now = datetime(2026, 9, 1, tzinfo=UTC)
    federation_identity.create_fleet_root(
        "fleet-a",
        bundle_path=root_bundle,
        passphrase=b"root-passphrase-for-tests",
        now=now,
    )
    original = json.loads(root_bundle.read_text(encoding="utf-8"))

    downgraded = copy.deepcopy(original)
    downgraded["privateKeyEnvelope"]["kdf"]["memoryKiB"] = 8
    root_bundle.write_text(json.dumps(downgraded), encoding="utf-8")
    with pytest.raises(federation_identity.FederationIdentityError) as downgrade_error:
        federation_identity.issue_online_signer(
            root_bundle_path=root_bundle,
            root_passphrase=b"root-passphrase-for-tests",
            signer_bundle_path=tmp_path / "downgraded-signer.json",
            signer_passphrase=b"signer-passphrase-for-tests",
            sequence=1,
            not_before=now,
            expires_at=now + timedelta(days=1),
        )
    assert downgrade_error.value.code == "FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID"

    tampered = copy.deepcopy(original)
    ciphertext = str(tampered["privateKeyEnvelope"]["ciphertext"])
    tampered["privateKeyEnvelope"]["ciphertext"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
    root_bundle.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(federation_identity.FederationIdentityError) as tamper_error:
        federation_identity.issue_online_signer(
            root_bundle_path=root_bundle,
            root_passphrase=b"root-passphrase-for-tests",
            signer_bundle_path=tmp_path / "tampered-signer.json",
            signer_passphrase=b"signer-passphrase-for-tests",
            sequence=1,
            not_before=now,
            expires_at=now + timedelta(days=1),
        )
    assert tamper_error.value.code == "FEDERATION_PRIVATE_KEY_UNAVAILABLE"

    invalid_envelopes: list[object] = [[], {**original["privateKeyEnvelope"], "ciphertext": "AA"}]
    for invalid_envelope in invalid_envelopes:
        invalid = copy.deepcopy(original)
        invalid["privateKeyEnvelope"] = invalid_envelope
        root_bundle.write_text(json.dumps(invalid), encoding="utf-8")
        with pytest.raises(federation_identity.FederationIdentityError) as invalid_error:
            federation_identity.issue_online_signer(
                root_bundle_path=root_bundle,
                root_passphrase=b"root-passphrase-for-tests",
                signer_bundle_path=tmp_path / f"invalid-{type(invalid_envelope).__name__}.json",
                signer_passphrase=b"signer-passphrase-for-tests",
                sequence=1,
                not_before=now,
                expires_at=now + timedelta(days=1),
            )
        assert invalid_error.value.code == "FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID"

    root_bundle.write_text(json.dumps(original), encoding="utf-8")

    def unavailable_argon2(*_args: object, **_kwargs: object) -> None:
        raise federation_identity.UnsupportedAlgorithm("argon2 unavailable")

    monkeypatch.setattr(federation_identity, "Argon2id", unavailable_argon2)
    with pytest.raises(federation_identity.FederationIdentityError) as unavailable:
        federation_identity.issue_online_signer(
            root_bundle_path=root_bundle,
            root_passphrase=b"root-passphrase-for-tests",
            signer_bundle_path=tmp_path / "unavailable-signer.json",
            signer_passphrase=b"signer-passphrase-for-tests",
            sequence=1,
            not_before=now,
            expires_at=now + timedelta(days=1),
        )
    assert unavailable.value.code == "FEDERATION_PRIVATE_KEY_ENCRYPTION_UNAVAILABLE"


def test_root_and_signer_bundle_creation_is_create_only(tmp_path: Path) -> None:
    root_bundle = tmp_path / "fleet-root.bundle.json"
    initial = federation_identity.create_fleet_root(
        "fleet-a",
        bundle_path=root_bundle,
        passphrase=b"root-passphrase-for-tests",
        now=datetime(2026, 9, 1, tzinfo=UTC),
    )
    original_bytes = root_bundle.read_bytes()

    with pytest.raises(federation_identity.FederationIdentityError) as duplicate_root:
        federation_identity.create_fleet_root(
            "fleet-b",
            bundle_path=root_bundle,
            passphrase=b"different-root-passphrase",
            now=datetime(2026, 9, 2, tzinfo=UTC),
        )
    assert duplicate_root.value.code == "FEDERATION_IDENTITY_ALREADY_EXISTS"
    assert root_bundle.read_bytes() == original_bytes
    assert federation_identity.read_fleet_identity(root_bundle) == initial

    signer_bundle = tmp_path / "signer.bundle.json"
    federation_identity.issue_online_signer(
        root_bundle_path=root_bundle,
        root_passphrase=b"root-passphrase-for-tests",
        signer_bundle_path=signer_bundle,
        signer_passphrase=b"signer-passphrase-for-tests",
        sequence=1,
        not_before=datetime(2026, 9, 1, tzinfo=UTC),
        expires_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    signer_bytes = signer_bundle.read_bytes()
    with pytest.raises(federation_identity.FederationIdentityError) as duplicate_signer:
        federation_identity.issue_online_signer(
            root_bundle_path=root_bundle,
            root_passphrase=b"root-passphrase-for-tests",
            signer_bundle_path=signer_bundle,
            signer_passphrase=b"another-signer-passphrase",
            sequence=2,
            not_before=datetime(2026, 9, 2, tzinfo=UTC),
            expires_at=datetime(2026, 10, 2, tzinfo=UTC),
        )
    assert duplicate_signer.value.code == "FEDERATION_IDENTITY_ALREADY_EXISTS"
    assert signer_bundle.read_bytes() == signer_bytes


@pytest.mark.parametrize(
    ("field", "replacement", "expected_error"),
    [
        ("fleetId", "fleet-b", "FEDERATION_SIGNER_CERTIFICATE_FLEET_MISMATCH"),
        ("rootKeyId", "fed-root-wrong", "FEDERATION_SIGNER_CERTIFICATE_ROOT_MISMATCH"),
        ("rootFingerprint", "sha256:" + ("0" * 64), "FEDERATION_SIGNER_CERTIFICATE_ROOT_MISMATCH"),
        ("signerKeyId", "fed-signer-wrong", "FEDERATION_SIGNER_CERTIFICATE_SIGNER_KEY_ID_INVALID"),
        ("sequence", 2, "FEDERATION_SIGNER_CERTIFICATE_SIGNATURE_INVALID"),
        ("expiresAt", "2027-01-01T00:00:00Z", "FEDERATION_SIGNER_CERTIFICATE_SIGNATURE_INVALID"),
        ("rootSignature", "AA", "FEDERATION_SIGNER_CERTIFICATE_SIGNATURE_INVALID"),
    ],
)
def test_online_signer_certificate_binds_every_field(
    tmp_path: Path,
    field: str,
    replacement: object,
    expected_error: str,
) -> None:
    identity, certificate, _ = _identity_fixture(tmp_path)
    tampered = {**certificate, field: replacement}

    errors = federation_identity.validate_online_signer_certificate(
        tampered,
        identity,
        now=datetime(2026, 9, 1, 1, 0, 1, tzinfo=UTC),
    )

    assert expected_error in errors


def test_online_signer_certificate_rejects_wrong_root_expiry_and_future_validity(tmp_path: Path) -> None:
    identity, certificate, _ = _identity_fixture(tmp_path / "fleet-a")
    other_identity = federation_identity.create_fleet_root(
        "fleet-a",
        bundle_path=tmp_path / "fleet-b" / "root.bundle.json",
        passphrase=b"other-root-passphrase",
        now=datetime(2026, 9, 1, tzinfo=UTC),
    )
    now = datetime(2026, 9, 1, 1, 0, 1, tzinfo=UTC)

    assert "FEDERATION_SIGNER_CERTIFICATE_ROOT_MISMATCH" in federation_identity.validate_online_signer_certificate(
        certificate,
        other_identity,
        now=now,
    )
    assert "FEDERATION_SIGNER_CERTIFICATE_EXPIRED" in federation_identity.validate_online_signer_certificate(
        certificate,
        identity,
        now=datetime(2026, 10, 2, tzinfo=UTC),
    )
    assert "FEDERATION_SIGNER_CERTIFICATE_NOT_YET_VALID" in federation_identity.validate_online_signer_certificate(
        certificate,
        identity,
        now=datetime(2026, 8, 31, 23, 0, tzinfo=UTC),
        max_future_skew_seconds=30,
    )
    near_future = {**certificate, "issuedAt": "2026-09-01T01:00:20Z", "notBefore": "2026-09-01T01:00:20Z"}
    root_bundle = tmp_path / "fleet-a" / "offline" / "fleet-root.bundle.json"
    _, root_key = federation_identity._load_root_key(root_bundle, b"root-passphrase-for-tests")
    near_future_payload = {key: value for key, value in near_future.items() if key != "rootSignature"}
    near_future["rootSignature"] = federation_identity._b64url_encode(
        root_key.sign(federation_identity._certificate_message(near_future_payload))
    )
    assert "FEDERATION_SIGNER_CERTIFICATE_ISSUED_IN_FUTURE" in federation_identity.validate_online_signer_certificate(
        near_future,
        identity,
        now=datetime(2026, 9, 1, 1, 0, tzinfo=UTC),
    )
    assert federation_identity.validate_online_signer_certificate(
        near_future,
        identity,
        now=datetime(2026, 9, 1, 1, 0, tzinfo=UTC),
        max_future_skew_seconds=30,
    ) == []
    assert federation_identity.validate_online_signer_certificate(
        certificate,
        identity,
        now=datetime(2026, 9, 1, 1, 0, 1),
    ) == ["FEDERATION_CERTIFICATE_VALIDATION_TIME_INVALID"]
    assert "FEDERATION_CERTIFICATE_VALIDATION_SKEW_INVALID" in federation_identity.validate_online_signer_certificate(
        certificate,
        identity,
        now=now,
        max_future_skew_seconds=True,
    )


def test_online_signer_load_fails_closed_on_wrong_passphrase_or_certificate_binding(tmp_path: Path) -> None:
    identity, certificate, _ = _identity_fixture(tmp_path)
    signer_bundle = tmp_path / "online" / "signer-1.bundle.json"

    with pytest.raises(federation_identity.FederationIdentityError) as wrong_passphrase:
        federation_identity.load_online_signer(
            signer_bundle,
            b"wrong-signer-passphrase",
            root_identity=identity,
            now=datetime(2026, 9, 1, 1, 0, 1, tzinfo=UTC),
        )
    assert wrong_passphrase.value.code == "FEDERATION_PRIVATE_KEY_UNAVAILABLE"

    bundle = json.loads(signer_bundle.read_text(encoding="utf-8"))
    bundle["certificate"] = {**certificate, "signerPublicKey": str(identity["rootPublicKey"])}
    signer_bundle.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(federation_identity.FederationIdentityError) as wrong_binding:
        federation_identity.load_online_signer(
            signer_bundle,
            b"signer-passphrase-for-tests",
            root_identity=identity,
            now=datetime(2026, 9, 1, 1, 0, 1, tzinfo=UTC),
        )
    assert wrong_binding.value.code in {
        "FEDERATION_SIGNER_CERTIFICATE_SIGNER_KEY_ID_INVALID",
        "FEDERATION_SIGNER_CERTIFICATE_SIGNATURE_INVALID",
        "FEDERATION_SIGNER_PRIVATE_KEY_MISMATCH",
    }


def test_signed_federation_document_binds_full_canonical_payload_and_schema(tmp_path: Path) -> None:
    identity, certificate, signer = _identity_fixture(tmp_path)
    now = datetime(2026, 9, 1, 1, 0, 1, tzinfo=UTC)
    payload = {
        "schema": "federation-test-document-v1",
        "fleetId": "fleet-a",
        "sequence": 7,
        "nested": {"coverage": ["risk", "capacity"], "ready": True},
    }

    signed = federation_identity.sign_federation_document(signer, payload)
    verified = federation_identity.verify_federation_document(
        signed,
        certificate=certificate,
        root_identity=identity,
        expected_schema="federation-test-document-v1",
        now=now,
    )

    assert verified == signed
    assert signed["signerKeyId"] == certificate["signerKeyId"]
    assert signed["signatureAlgorithm"] == "Ed25519"
    assert "signature" in signed
    assert payload == {
        "schema": "federation-test-document-v1",
        "fleetId": "fleet-a",
        "sequence": 7,
        "nested": {"coverage": ["risk", "capacity"], "ready": True},
    }

    for tampered in (
        {**signed, "sequence": 8},
        {**signed, "schema": "different-schema-v1"},
        {**signed, "fleetId": "fleet-b"},
        {**signed, "signerKeyId": "fed-signer-wrong"},
    ):
        with pytest.raises(federation_identity.FederationIdentityError):
            federation_identity.verify_federation_document(
                tampered,
                certificate=certificate,
                root_identity=identity,
                expected_schema="federation-test-document-v1",
                now=now,
            )

    pre_signed = copy.deepcopy(payload)
    pre_signed["signature"] = "attacker-controlled"
    with pytest.raises(federation_identity.FederationIdentityError) as confused_deputy:
        federation_identity.sign_federation_document(signer, pre_signed)
    assert confused_deputy.value.code == "FEDERATION_DOCUMENT_ALREADY_SIGNED"


def test_signed_document_binds_exact_fleet_and_root_certificate_chain(tmp_path: Path) -> None:
    identity_a, certificate_a, signer_a = _identity_fixture(tmp_path / "fleet-a")
    root_b = tmp_path / "fleet-b" / "root.bundle.json"
    federation_identity.create_fleet_root(
        "fleet-a",
        bundle_path=root_b,
        passphrase=b"other-root-passphrase",
        now=datetime(2026, 9, 1, tzinfo=UTC),
    )
    identity_b, certificate_b = _cross_certify_signer(
        root_bundle=root_b,
        root_passphrase=b"other-root-passphrase",
        source_certificate=certificate_a,
    )
    signed = federation_identity.sign_federation_document(
        signer_a,
        {"schema": "federation-test-document-v1", "fleetId": "fleet-a", "sequence": 1},
    )

    assert federation_identity.verify_federation_document(
        signed,
        certificate=certificate_a,
        root_identity=identity_a,
        expected_schema="federation-test-document-v1",
        now=datetime(2026, 9, 2, tzinfo=UTC),
    ) == signed
    with pytest.raises(federation_identity.FederationIdentityError) as wrong_chain:
        federation_identity.verify_federation_document(
            signed,
            certificate=certificate_b,
            root_identity=identity_b,
            expected_schema="federation-test-document-v1",
            now=datetime(2026, 9, 2, tzinfo=UTC),
        )
    assert wrong_chain.value.code == "FEDERATION_DOCUMENT_SIGNATURE_INVALID"


@pytest.mark.parametrize("fleet_id", ["", " fleet-a", "fleet/A", "FLEET-A", "a" * 129, 1])
def test_fleet_identity_rejects_noncanonical_fleet_ids(tmp_path: Path, fleet_id: Any) -> None:
    with pytest.raises(federation_identity.FederationIdentityError) as invalid:
        federation_identity.create_fleet_root(
            fleet_id,
            bundle_path=tmp_path / "root.bundle.json",
            passphrase=b"root-passphrase-for-tests",
            now=datetime(2026, 9, 1, tzinfo=UTC),
        )
    assert invalid.value.code == "FEDERATION_FLEET_ID_INVALID"


@pytest.mark.parametrize("passphrase", [b"", b"short", b"123456789012345"])
def test_private_key_bundles_require_nontrivial_passphrases(tmp_path: Path, passphrase: bytes) -> None:
    with pytest.raises(federation_identity.FederationIdentityError) as weak:
        federation_identity.create_fleet_root(
            "fleet-a",
            bundle_path=tmp_path / "root.bundle.json",
            passphrase=passphrase,
            now=datetime(2026, 9, 1, tzinfo=UTC),
        )
    assert weak.value.code == "FEDERATION_PRIVATE_KEY_PASSPHRASE_INVALID"


def test_private_key_bundles_reject_invalid_passphrase_types_and_timestamps(tmp_path: Path) -> None:
    for invalid_passphrase in ("not-bytes", None, b"valid-length-but\x00nul"):
        with pytest.raises(federation_identity.FederationIdentityError) as invalid:
            federation_identity.create_fleet_root(
                "fleet-a",
                bundle_path=tmp_path / f"root-{type(invalid_passphrase).__name__}.json",
                passphrase=invalid_passphrase,  # type: ignore[arg-type]
                now=datetime(2026, 9, 1, tzinfo=UTC),
            )
        assert invalid.value.code == "FEDERATION_PRIVATE_KEY_PASSPHRASE_INVALID"

    with pytest.raises(federation_identity.FederationIdentityError) as naive:
        federation_identity.create_fleet_root(
            "fleet-a",
            bundle_path=tmp_path / "naive-root.json",
            passphrase=b"root-passphrase-for-tests",
            now=datetime(2026, 9, 1),
        )
    assert naive.value.code == "FEDERATION_TIMESTAMP_INVALID"


@pytest.mark.parametrize(
    ("field", "replacement", "expected_error"),
    [
        ("schema", "other", "FEDERATION_ROOT_IDENTITY_SCHEMA_INVALID"),
        ("fleetId", "FLEET-A", "FEDERATION_FLEET_ID_INVALID"),
        ("signatureAlgorithm", "RSA", "FEDERATION_ROOT_IDENTITY_ALGORITHM_INVALID"),
        ("rootPublicKey", None, "FEDERATION_ROOT_PUBLIC_KEY_INVALID"),
        ("rootPublicKey", "not-base64!", "FEDERATION_ROOT_PUBLIC_KEY_INVALID"),
        ("rootPublicKey", "AA", "FEDERATION_ROOT_PUBLIC_KEY_INVALID"),
        ("rootKeyId", "fed-root-wrong", "FEDERATION_ROOT_KEY_ID_INVALID"),
        ("rootFingerprint", "sha256:" + ("0" * 64), "FEDERATION_ROOT_FINGERPRINT_INVALID"),
        ("createdAt", "not-a-time", "FEDERATION_ROOT_IDENTITY_TIMESTAMP_INVALID"),
        ("createdAt", "2026-09-01T00:00:00", "FEDERATION_ROOT_IDENTITY_TIMESTAMP_INVALID"),
    ],
)
def test_root_identity_fields_fail_closed(
    tmp_path: Path,
    field: str,
    replacement: object,
    expected_error: str,
) -> None:
    identity, certificate, _ = _identity_fixture(tmp_path)
    tampered = {**identity, field: replacement}

    errors = federation_identity.validate_online_signer_certificate(certificate, tampered)

    assert expected_error in errors


def test_root_and_certificate_reject_non_json_runtime_shapes(tmp_path: Path) -> None:
    identity, certificate, _ = _identity_fixture(tmp_path)

    assert federation_identity.validate_online_signer_certificate(
        certificate,
        {**identity, "runtimeOnly": ("not-json",)},
    ) == ["FEDERATION_ROOT_IDENTITY_INVALID"]
    assert federation_identity.validate_online_signer_certificate(
        {**certificate, "runtimeOnly": ("not-json",)},
        identity,
    ) == ["FEDERATION_SIGNER_CERTIFICATE_INVALID"]


def test_root_and_signer_bundle_readers_reject_malformed_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(federation_identity.FederationIdentityError) as missing_error:
        federation_identity.read_fleet_identity(missing)
    assert missing_error.value.code == "FEDERATION_IDENTITY_BUNDLE_INVALID"

    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{", encoding="utf-8")
    with pytest.raises(federation_identity.FederationIdentityError) as invalid_json_error:
        federation_identity.read_fleet_identity(invalid_json)
    assert invalid_json_error.value.code == "FEDERATION_IDENTITY_BUNDLE_INVALID"

    list_bundle = tmp_path / "list.json"
    list_bundle.write_text("[]", encoding="utf-8")
    with pytest.raises(federation_identity.FederationIdentityError) as list_error:
        federation_identity.read_fleet_identity(list_bundle)
    assert list_error.value.code == "FEDERATION_IDENTITY_BUNDLE_INVALID"

    wrong_schema = tmp_path / "wrong-schema.json"
    wrong_schema.write_text(json.dumps({"schema": "wrong"}), encoding="utf-8")
    with pytest.raises(federation_identity.FederationIdentityError) as root_schema_error:
        federation_identity.read_fleet_identity(wrong_schema)
    assert root_schema_error.value.code == "FEDERATION_ROOT_BUNDLE_SCHEMA_INVALID"

    monkeypatch.setattr(federation_identity, "_MAX_BUNDLE_BYTES", 1)
    with pytest.raises(federation_identity.FederationIdentityError) as oversized:
        federation_identity.read_fleet_identity(wrong_schema)
    assert oversized.value.code == "FEDERATION_IDENTITY_BUNDLE_TOO_LARGE"


def test_root_bundle_rejects_wrong_password_key_type_and_public_binding(tmp_path: Path) -> None:
    root_a = tmp_path / "root-a.json"
    root_b = tmp_path / "root-b.json"
    now = datetime(2026, 9, 1, tzinfo=UTC)
    federation_identity.create_fleet_root(
        "fleet-a",
        bundle_path=root_a,
        passphrase=b"shared-root-passphrase",
        now=now,
    )
    federation_identity.create_fleet_root(
        "fleet-a",
        bundle_path=root_b,
        passphrase=b"shared-root-passphrase",
        now=now,
    )

    with pytest.raises(federation_identity.FederationIdentityError) as wrong_password:
        federation_identity.issue_online_signer(
            root_bundle_path=root_a,
            root_passphrase=b"incorrect-root-password",
            signer_bundle_path=tmp_path / "wrong-password-signer.json",
            signer_passphrase=b"signer-passphrase-for-tests",
            sequence=1,
            not_before=now,
            expires_at=now + timedelta(days=1),
        )
    assert wrong_password.value.code == "FEDERATION_PRIVATE_KEY_UNAVAILABLE"

    bundle_a = json.loads(root_a.read_text(encoding="utf-8"))
    _, root_b_key = federation_identity._load_root_key(root_b, b"shared-root-passphrase")
    bundle_a["privateKeyEnvelope"] = federation_identity._encrypt_private_key(
        root_b_key,
        b"shared-root-passphrase",
        binding=bundle_a["fleetIdentity"],
    )
    root_a.write_text(json.dumps(bundle_a), encoding="utf-8")
    with pytest.raises(federation_identity.FederationIdentityError) as mismatched:
        federation_identity.issue_online_signer(
            root_bundle_path=root_a,
            root_passphrase=b"shared-root-passphrase",
            signer_bundle_path=tmp_path / "mismatched-signer.json",
            signer_passphrase=b"signer-passphrase-for-tests",
            sequence=1,
            not_before=now,
            expires_at=now + timedelta(days=1),
        )
    assert mismatched.value.code == "FEDERATION_ROOT_PRIVATE_KEY_MISMATCH"

    bundle_a["privateKeyEnvelope"] = federation_identity._encrypt_private_key(
        X25519PrivateKey.generate(),  # type: ignore[arg-type]
        b"shared-root-passphrase",
        binding=bundle_a["fleetIdentity"],
    )
    root_a.write_text(json.dumps(bundle_a), encoding="utf-8")
    with pytest.raises(federation_identity.FederationIdentityError) as wrong_type:
        federation_identity.issue_online_signer(
            root_bundle_path=root_a,
            root_passphrase=b"shared-root-passphrase",
            signer_bundle_path=tmp_path / "wrong-type-signer.json",
            signer_passphrase=b"signer-passphrase-for-tests",
            sequence=1,
            not_before=now,
            expires_at=now + timedelta(days=1),
        )
    assert wrong_type.value.code == "FEDERATION_PRIVATE_KEY_TYPE_INVALID"


@pytest.mark.parametrize("sequence", [0, -1, True, "1"])
def test_online_signer_issue_rejects_invalid_sequence(tmp_path: Path, sequence: Any) -> None:
    root_bundle = tmp_path / "root.json"
    now = datetime(2026, 9, 1, tzinfo=UTC)
    federation_identity.create_fleet_root(
        "fleet-a",
        bundle_path=root_bundle,
        passphrase=b"root-passphrase-for-tests",
        now=now,
    )
    with pytest.raises(federation_identity.FederationIdentityError) as invalid:
        federation_identity.issue_online_signer(
            root_bundle_path=root_bundle,
            root_passphrase=b"root-passphrase-for-tests",
            signer_bundle_path=tmp_path / f"signer-{sequence}.json",
            signer_passphrase=b"signer-passphrase-for-tests",
            sequence=sequence,
            not_before=now,
            expires_at=now + timedelta(days=1),
        )
    assert invalid.value.code == "FEDERATION_SIGNER_CERTIFICATE_SEQUENCE_INVALID"


def test_online_signer_issue_rejects_invalid_validity_window(tmp_path: Path) -> None:
    root_bundle = tmp_path / "root.json"
    now = datetime(2026, 9, 1, tzinfo=UTC)
    federation_identity.create_fleet_root(
        "fleet-a",
        bundle_path=root_bundle,
        passphrase=b"root-passphrase-for-tests",
        now=now,
    )
    with pytest.raises(federation_identity.FederationIdentityError) as inverted:
        federation_identity.issue_online_signer(
            root_bundle_path=root_bundle,
            root_passphrase=b"root-passphrase-for-tests",
            signer_bundle_path=tmp_path / "signer.json",
            signer_passphrase=b"signer-passphrase-for-tests",
            sequence=1,
            not_before=now,
            expires_at=now,
        )
    assert inverted.value.code == "FEDERATION_SIGNER_CERTIFICATE_WINDOW_INVALID"


@pytest.mark.parametrize(
    ("mutations", "expected_error"),
    [
        ({"schema": "wrong"}, "FEDERATION_SIGNER_CERTIFICATE_SCHEMA_INVALID"),
        ({"signatureAlgorithm": "RSA"}, "FEDERATION_SIGNER_CERTIFICATE_ALGORITHM_INVALID"),
        ({"signerPublicKey": "not-base64!"}, "FEDERATION_SIGNER_CERTIFICATE_PUBLIC_KEY_INVALID"),
        ({"sequence": 0}, "FEDERATION_SIGNER_CERTIFICATE_SEQUENCE_INVALID"),
        ({"issuedAt": None}, "FEDERATION_SIGNER_CERTIFICATE_TIMESTAMP_INVALID"),
        ({"issuedAt": "invalid"}, "FEDERATION_SIGNER_CERTIFICATE_TIMESTAMP_INVALID"),
        ({"issuedAt": "2026-09-02T00:00:00Z"}, "FEDERATION_SIGNER_CERTIFICATE_WINDOW_INVALID"),
    ],
)
def test_online_signer_certificate_rejects_malformed_fields(
    tmp_path: Path,
    mutations: dict[str, object],
    expected_error: str,
) -> None:
    identity, certificate, _ = _identity_fixture(tmp_path)

    errors = federation_identity.validate_online_signer_certificate(
        {**certificate, **mutations},
        identity,
        now=datetime(2026, 9, 1, 1, 0, 1, tzinfo=UTC),
    )

    assert expected_error in errors
    assert federation_identity.validate_online_signer_certificate(None, identity) == [
        "FEDERATION_SIGNER_CERTIFICATE_INVALID"
    ]


def test_signer_bundle_schema_and_private_key_binding_fail_closed(tmp_path: Path) -> None:
    identity, _, signer = _identity_fixture(tmp_path / "one")
    _, certificate_two, _ = _identity_fixture(tmp_path / "two")
    signer_one_path = tmp_path / "one" / "online" / "signer-1.bundle.json"
    signer_two_path = tmp_path / "two" / "online" / "signer-1.bundle.json"

    assert signer.certificate["signerKeyId"] == signer.signer_key_id
    assert signer.signer_key_id in repr(signer)

    wrong_schema = json.loads(signer_one_path.read_text(encoding="utf-8"))
    wrong_schema["schema"] = "wrong"
    signer_one_path.write_text(json.dumps(wrong_schema), encoding="utf-8")
    with pytest.raises(federation_identity.FederationIdentityError) as schema_error:
        federation_identity.load_online_signer(
            signer_one_path,
            b"signer-passphrase-for-tests",
            root_identity=identity,
            now=datetime(2026, 9, 1, 1, 0, 1, tzinfo=UTC),
        )
    assert schema_error.value.code == "FEDERATION_SIGNER_BUNDLE_SCHEMA_INVALID"

    # Keep signer two's valid certificate but bind signer one's private key to it.
    signer_one_bundle = wrong_schema
    signer_two_bundle = json.loads(signer_two_path.read_text(encoding="utf-8"))
    signer_one_key = federation_identity._load_private_key(
        signer_one_bundle["privateKeyEnvelope"],
        b"signer-passphrase-for-tests",
        binding=signer_one_bundle["certificate"],
    )
    signer_two_bundle["privateKeyEnvelope"] = federation_identity._encrypt_private_key(
        signer_one_key,
        b"signer-passphrase-for-tests",
        binding=certificate_two,
    )
    signer_two_bundle["certificate"] = certificate_two
    signer_two_path.write_text(json.dumps(signer_two_bundle), encoding="utf-8")
    identity_two = federation_identity.read_fleet_identity(tmp_path / "two" / "offline" / "fleet-root.bundle.json")
    with pytest.raises(federation_identity.FederationIdentityError) as key_mismatch:
        federation_identity.load_online_signer(
            signer_two_path,
            b"signer-passphrase-for-tests",
            root_identity=identity_two,
            now=datetime(2026, 9, 1, 1, 0, 1, tzinfo=UTC),
        )
    assert key_mismatch.value.code == "FEDERATION_SIGNER_PRIVATE_KEY_MISMATCH"


def test_document_boundary_rejects_malformed_and_noncanonical_payloads(tmp_path: Path) -> None:
    identity, certificate, signer = _identity_fixture(tmp_path)
    now = datetime(2026, 9, 1, 1, 0, 1, tzinfo=UTC)
    with pytest.raises(federation_identity.FederationIdentityError) as not_object:
        federation_identity.sign_federation_document(signer, None)  # type: ignore[arg-type]
    assert not_object.value.code == "FEDERATION_DOCUMENT_INVALID"
    with pytest.raises(federation_identity.FederationIdentityError) as no_schema:
        federation_identity.sign_federation_document(signer, {"fleetId": "fleet-a"})
    assert no_schema.value.code == "FEDERATION_DOCUMENT_SCHEMA_INVALID"
    with pytest.raises(federation_identity.FederationIdentityError) as no_fleet:
        federation_identity.sign_federation_document(signer, {"schema": "x-v1"})
    assert no_fleet.value.code == "FEDERATION_DOCUMENT_FLEET_ID_REQUIRED"
    with pytest.raises(federation_identity.FederationIdentityError) as wrong_fleet:
        federation_identity.sign_federation_document(signer, {"schema": "x-v1", "fleetId": "fleet-b"})
    assert wrong_fleet.value.code == "FEDERATION_DOCUMENT_FLEET_MISMATCH"
    with pytest.raises(federation_identity.FederationIdentityError) as noncanonical:
        federation_identity.sign_federation_document(
            signer,
            {"schema": "x-v1", "fleetId": "fleet-a", "value": float("nan")},
        )
    assert noncanonical.value.code == "FEDERATION_CANONICAL_PAYLOAD_INVALID"
    for ambiguous in (
        {"schema": "x-v1", "fleetId": "fleet-a", "rules": {1: "deny"}},
        {"schema": "x-v1", "fleetId": "fleet-a", "rules": ("deny",)},
        {"schema": "x-v1", "fleetId": "fleet-a", "value": "\ud800"},
        {"schema": "x-v1", "fleetId": "fleet-a", "rules": {"\ud800": "deny"}},
    ):
        with pytest.raises(federation_identity.FederationIdentityError) as ambiguous_error:
            federation_identity.sign_federation_document(signer, ambiguous)
        assert ambiguous_error.value.code == "FEDERATION_CANONICAL_PAYLOAD_INVALID"

    signed = federation_identity.sign_federation_document(signer, {"schema": "x-v1", "fleetId": "fleet-a"})
    invalid_documents = [
        (None, "FEDERATION_DOCUMENT_INVALID"),
        ({**signed, "signatureAlgorithm": "RSA"}, "FEDERATION_DOCUMENT_ALGORITHM_INVALID"),
        ({**signed, "signature": "AA"}, "FEDERATION_DOCUMENT_SIGNATURE_INVALID"),
    ]
    for document, code in invalid_documents:
        with pytest.raises(federation_identity.FederationIdentityError) as invalid:
            federation_identity.verify_federation_document(
                document,
                certificate=certificate,
                root_identity=identity,
                expected_schema="x-v1",
                now=now,
            )
        assert invalid.value.code == code

    signed_with_string_key = federation_identity.sign_federation_document(
        signer,
        {"schema": "x-v1", "fleetId": "fleet-a", "rules": {"1": "deny"}},
    )
    key_type_tamper = {**signed_with_string_key, "rules": {1: "deny"}}
    with pytest.raises(federation_identity.FederationIdentityError) as key_type_error:
        federation_identity.verify_federation_document(
            key_type_tamper,
            certificate=certificate,
            root_identity=identity,
            expected_schema="x-v1",
            now=now,
        )
    assert key_type_error.value.code == "FEDERATION_CANONICAL_PAYLOAD_INVALID"

    signed_float = federation_identity.sign_federation_document(
        signer,
        {"schema": "x-v1", "fleetId": "fleet-a", "ratio": 1.25},
    )
    assert federation_identity.verify_federation_document(
        signed_float,
        certificate=certificate,
        root_identity=identity,
        expected_schema="x-v1",
        now=now,
    ) == signed_float

    missing_fleet = {key: value for key, value in signed.items() if key != "fleetId"}
    with pytest.raises(federation_identity.FederationIdentityError) as missing_fleet_error:
        federation_identity.verify_federation_document(
            missing_fleet,
            certificate=certificate,
            root_identity=identity,
            expected_schema="x-v1",
            now=now,
        )
    assert missing_fleet_error.value.code == "FEDERATION_DOCUMENT_FLEET_ID_REQUIRED"

    malformed_signer = federation_identity.OnlineFleetSigner(
        signer._private_key,
        {"fleetId": "fleet-a", "signerKeyId": signer.signer_key_id},
    )
    with pytest.raises(federation_identity.FederationIdentityError) as malformed_context:
        federation_identity.sign_federation_document(
            malformed_signer,
            {"schema": "x-v1", "fleetId": "fleet-a"},
        )
    assert malformed_context.value.code == "FEDERATION_SIGNER_CERTIFICATE_INVALID"


def test_identity_storage_failure_removes_partial_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "partial.json"

    def fail_fsync(_fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(federation_identity.os, "fsync", fail_fsync)
    with pytest.raises(federation_identity.FederationIdentityError) as failed:
        federation_identity.create_fleet_root(
            "fleet-a",
            bundle_path=path,
            passphrase=b"root-passphrase-for-tests",
            now=datetime(2026, 9, 1, tzinfo=UTC),
        )
    assert failed.value.code == "FEDERATION_IDENTITY_STORAGE_FAILED"
    assert not path.exists()


def test_root_bundle_internal_shape_errors_are_generic_and_fail_closed(tmp_path: Path) -> None:
    root_bundle = tmp_path / "root.json"
    now = datetime(2026, 9, 1, tzinfo=UTC)
    federation_identity.create_fleet_root(
        "fleet-a",
        bundle_path=root_bundle,
        passphrase=b"root-passphrase-for-tests",
        now=now,
    )
    original = json.loads(root_bundle.read_text(encoding="utf-8"))

    assert federation_identity.validate_online_signer_certificate({}, None) == ["FEDERATION_ROOT_IDENTITY_INVALID"]

    missing_identity = {**original, "fleetIdentity": None}
    root_bundle.write_text(json.dumps(missing_identity), encoding="utf-8")
    with pytest.raises(federation_identity.FederationIdentityError) as invalid_identity:
        federation_identity.read_fleet_identity(root_bundle)
    assert invalid_identity.value.code == "FEDERATION_ROOT_IDENTITY_INVALID"

    wrong_schema = {**original, "schema": "wrong"}
    root_bundle.write_text(json.dumps(wrong_schema), encoding="utf-8")
    with pytest.raises(federation_identity.FederationIdentityError) as invalid_schema:
        federation_identity.issue_online_signer(
            root_bundle_path=root_bundle,
            root_passphrase=b"root-passphrase-for-tests",
            signer_bundle_path=tmp_path / "signer-schema.json",
            signer_passphrase=b"signer-passphrase-for-tests",
            sequence=1,
            not_before=now,
            expires_at=now + timedelta(days=1),
        )
    assert invalid_schema.value.code == "FEDERATION_ROOT_BUNDLE_SCHEMA_INVALID"

    missing_private_key = {**original, "privateKeyEnvelope": None}
    root_bundle.write_text(json.dumps(missing_private_key), encoding="utf-8")
    with pytest.raises(federation_identity.FederationIdentityError) as no_private_key:
        federation_identity.issue_online_signer(
            root_bundle_path=root_bundle,
            root_passphrase=b"root-passphrase-for-tests",
            signer_bundle_path=tmp_path / "signer-private.json",
            signer_passphrase=b"signer-passphrase-for-tests",
            sequence=1,
            not_before=now,
            expires_at=now + timedelta(days=1),
        )
    assert no_private_key.value.code == "FEDERATION_PRIVATE_KEY_UNAVAILABLE"

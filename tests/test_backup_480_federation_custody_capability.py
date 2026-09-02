from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import (
    backup_crypto,
    backup_recovery_credential,
    federation_custody_capability,
    federation_identity,
    federation_peer_trust,
)
from deepseek_infra.core.errors import AppError, ErrorCode
from tests.test_backup_480_federated_replica_receiver import NOW, _fixture


IDENTITY_TEXT = "AGE-SECRET-KEY-1TEST-RECOVERY-IDENTITY"
RECIPIENT = "age1preprovisionedreceiveridentity"


def _registry(tmp_settings: Path, fixture: dict[str, Any]) -> federation_custody_capability.FederationCustodyCapabilityRegistry:
    return federation_custody_capability.FederationCustodyCapabilityRegistry(
        tmp_settings / "fleet-b" / "custody-capabilities.sqlite3",
        fixture["identityB"],
    )


def _provider(name: str) -> backup_recovery_credential.InMemoryCredentialProvider:
    provider = backup_recovery_credential.InMemoryCredentialProvider()
    backup_recovery_credential.register_provider(name, provider)
    return provider


def _assert_capability_error(call: Any, code: str) -> None:
    with pytest.raises(federation_custody_capability.FederationCustodyCapabilityError) as rejected:
        call()
    assert rejected.value.code == code


def _consume_recovery_identity(
    registry: federation_custody_capability.FederationCustodyCapabilityRegistry,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
) -> None:
    with registry.open_recovery_identity(peer_registry, "fleet-a"):
        pass


def test_cold_custody_stores_ciphertext_capability_but_cannot_claim_recovery(
    tmp_settings: Path,
) -> None:
    fixture = _fixture(tmp_settings)
    registry = _registry(tmp_settings, fixture)

    record = registry.configure_peer(
        fixture["registry"],
        "fleet-a",
        mode=federation_custody_capability.COLD_CUSTODY,
        actor="operator-b",
        now=NOW + timedelta(seconds=3),
    )

    assert set(record) == federation_custody_capability.CUSTODY_CAPABILITY_PUBLIC_FIELDS
    assert record["localFleetId"] == "fleet-b"
    assert record["peerFleetId"] == "fleet-a"
    assert record["mode"] == federation_custody_capability.COLD_CUSTODY
    assert record["recoveryIdentityPreprovisioned"] is False
    assert record["ageRecipient"] is None
    assert registry.get_peer("fleet-a") == record
    _assert_capability_error(
        lambda: _consume_recovery_identity(registry, fixture["registry"]),
        "FEDERATION_PEER_COLD_CUSTODY_ONLY",
    )

    _assert_capability_error(
        lambda: registry.configure_peer(
            fixture["registry"],
            "fleet-a",
            mode=federation_custody_capability.COLD_CUSTODY,
            credential_provider="vault",
            credential_ref="slot-a",
            age_recipient=RECIPIENT,
            actor="operator-b",
            expected_revision=1,
            now=NOW + timedelta(seconds=4),
        ),
        "FEDERATION_COLD_CUSTODY_RECOVERY_IDENTITY_FORBIDDEN",
    )


def test_recovery_capable_requires_preprovisioned_identity_and_never_persists_secret(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_settings)
    registry = _registry(tmp_settings, fixture)
    provider_name = "fleet-b-recovery-capable"
    provider = _provider(provider_name)
    provider.set_credential("age-slot-a", IDENTITY_TEXT)
    derived_inputs: list[bytes] = []

    def derive(secret: bytearray) -> tuple[str, ...]:
        derived_inputs.append(bytes(secret))
        return (RECIPIENT,)

    monkeypatch.setattr(backup_crypto, "derive_recipients", derive)
    record = registry.configure_peer(
        fixture["registry"],
        "fleet-a",
        mode=federation_custody_capability.RECOVERY_CAPABLE,
        credential_provider=provider_name,
        credential_ref="age-slot-a",
        age_recipient=RECIPIENT,
        actor="operator-b",
        now=NOW + timedelta(seconds=3),
    )

    assert record["mode"] == federation_custody_capability.RECOVERY_CAPABLE
    assert record["recoveryIdentityPreprovisioned"] is True
    assert record["ageRecipient"] == RECIPIENT
    assert "credential" not in "".join(record).lower()
    assert IDENTITY_TEXT.encode("utf-8") not in registry.db_path.read_bytes()
    assert derived_inputs == [IDENTITY_TEXT.encode("utf-8")]

    with registry.open_recovery_identity(fixture["registry"], "fleet-a") as secret:
        opened = secret
        assert bytes(secret) == IDENTITY_TEXT.encode("utf-8")
    assert opened == bytearray(len(IDENTITY_TEXT))
    assert derived_inputs == [IDENTITY_TEXT.encode("utf-8"), IDENTITY_TEXT.encode("utf-8")]


def test_recovery_capable_rejects_missing_or_mismatched_identity(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_settings)
    provider_name = "fleet-b-missing-recovery"
    provider = _provider(provider_name)
    monkeypatch.setattr(backup_crypto, "derive_recipients", lambda _secret: ("age1different",))

    cases: tuple[tuple[dict[str, str], str], ...] = (
        ({}, "FEDERATION_RECOVERY_IDENTITY_BINDING_REQUIRED"),
        (
            {
                "credential_provider": provider_name,
                "credential_ref": "missing",
                "age_recipient": RECIPIENT,
            },
            "FEDERATION_RECOVERY_IDENTITY_NOT_PREPROVISIONED",
        ),
    )
    for index, (kwargs, code) in enumerate(cases):
        registry = federation_custody_capability.FederationCustodyCapabilityRegistry(
            tmp_settings / f"missing-{index}.sqlite3",
            fixture["identityB"],
        )
        _assert_capability_error(
            lambda registry=registry, kwargs=kwargs: registry.configure_peer(
                fixture["registry"],
                "fleet-a",
                mode=federation_custody_capability.RECOVERY_CAPABLE,
                actor="operator-b",
                now=NOW + timedelta(seconds=3),
                **kwargs,
            ),
            code,
        )
        assert registry.list_peers() == []

    provider.set_credential("age-slot-a", IDENTITY_TEXT)
    mismatch = federation_custody_capability.FederationCustodyCapabilityRegistry(
        tmp_settings / "mismatch.sqlite3",
        fixture["identityB"],
    )
    _assert_capability_error(
        lambda: mismatch.configure_peer(
            fixture["registry"],
            "fleet-a",
            mode=federation_custody_capability.RECOVERY_CAPABLE,
            credential_provider=provider_name,
            credential_ref="age-slot-a",
            age_recipient=RECIPIENT,
            actor="operator-b",
            now=NOW + timedelta(seconds=3),
        ),
        "FEDERATION_RECOVERY_IDENTITY_RECIPIENT_MISMATCH",
    )
    assert mismatch.list_peers() == []


def test_capability_identity_root_and_revision_are_immutable(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_settings)
    registry = _registry(tmp_settings, fixture)
    cold = registry.configure_peer(
        fixture["registry"],
        "fleet-a",
        mode=federation_custody_capability.COLD_CUSTODY,
        actor="operator-b",
        now=NOW + timedelta(seconds=3),
    )
    assert registry.configure_peer(
        fixture["registry"],
        "fleet-a",
        mode=federation_custody_capability.COLD_CUSTODY,
        actor="operator-b",
        now=NOW + timedelta(seconds=4),
    ) == cold

    provider_name = "fleet-b-capability-upgrade"
    provider = _provider(provider_name)
    provider.set_credential("age-slot-a", IDENTITY_TEXT)
    monkeypatch.setattr(backup_crypto, "derive_recipients", lambda _secret: (RECIPIENT,))
    _assert_capability_error(
        lambda: registry.configure_peer(
            fixture["registry"],
            "fleet-a",
            mode=federation_custody_capability.RECOVERY_CAPABLE,
            credential_provider=provider_name,
            credential_ref="age-slot-a",
            age_recipient=RECIPIENT,
            actor="operator-b",
            now=NOW + timedelta(seconds=4),
        ),
        "FEDERATION_CUSTODY_CAPABILITY_REVISION_CONFLICT",
    )
    upgraded = registry.configure_peer(
        fixture["registry"],
        "fleet-a",
        mode=federation_custody_capability.RECOVERY_CAPABLE,
        credential_provider=provider_name,
        credential_ref="age-slot-a",
        age_recipient=RECIPIENT,
        actor="operator-b",
        expected_revision=1,
        now=NOW + timedelta(seconds=4),
    )
    assert upgraded["revision"] == 2
    assert [event["eventType"] for event in registry.list_events("fleet-a")] == [
        "CUSTODY_CAPABILITY_CONFIGURED",
        "CUSTODY_CAPABILITY_UPDATED",
    ]
    assert registry.list_peers() == [upgraded]
    assert federation_custody_capability.FederationCustodyCapabilityRegistry(
        registry.db_path,
        fixture["identityB"],
    ).get_peer("fleet-a") == upgraded

    _assert_capability_error(
        lambda: federation_custody_capability.FederationCustodyCapabilityRegistry(
            registry.db_path,
            fixture["identityA"],
        ),
        "FEDERATION_CUSTODY_LOCAL_IDENTITY_CONFLICT",
    )

    fixture["registry"].revoke_peer(
        "fleet-a",
        actor="operator-b",
        reason="incident",
        now=NOW + timedelta(seconds=5),
    )
    _assert_capability_error(
        lambda: _consume_recovery_identity(registry, fixture["registry"]),
        "FEDERATION_PEER_REVOKED",
    )


def test_all_federation_documents_reject_age_private_identity_material(
    tmp_settings: Path,
) -> None:
    fixture = _fixture(tmp_settings)
    signer = fixture["signerB"]
    payload = {
        "schema": "test-federation-secret-boundary-v1",
        "fleetId": "fleet-b",
        "ageIdentity": IDENTITY_TEXT,
    }
    with pytest.raises(federation_identity.FederationIdentityError) as rejected:
        federation_identity.sign_federation_document(signer, payload)
    assert rejected.value.code == "FEDERATION_DOCUMENT_CONTAINS_SECRET"

    for candidate in (
        {"schema": "x", "fleetId": "fleet-b", "nested": {"credentialRef": "vault:age"}},
        {"schema": "x", "fleetId": "fleet-b", "note": "BEGIN PRIVATE KEY"},
    ):
        with pytest.raises(federation_identity.FederationIdentityError) as blocked:
            federation_identity.assert_federation_document_secret_free(candidate)
        assert blocked.value.code == "FEDERATION_DOCUMENT_CONTAINS_SECRET"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    (
        ({"peer_fleet_id": "Fleet A"}, "FEDERATION_CUSTODY_PEER_FLEET_ID_INVALID"),
        ({"mode": "PRIMARY"}, "FEDERATION_CUSTODY_MODE_INVALID"),
        ({"actor": " operator-b"}, "FEDERATION_CUSTODY_ACTOR_INVALID"),
        ({"actor": "operator\nb"}, "FEDERATION_CUSTODY_ACTOR_INVALID"),
        ({"expected_revision": True}, "FEDERATION_CUSTODY_CAPABILITY_REVISION_CONFLICT"),
        ({"expected_revision": 0}, "FEDERATION_CUSTODY_CAPABILITY_REVISION_CONFLICT"),
    ),
)
def test_capability_rejects_invalid_public_inputs(
    tmp_settings: Path,
    kwargs: dict[str, Any],
    code: str,
) -> None:
    fixture = _fixture(tmp_settings)
    registry = _registry(tmp_settings, fixture)
    arguments: dict[str, Any] = {
        "peer_fleet_id": "fleet-a",
        "mode": federation_custody_capability.COLD_CUSTODY,
        "actor": "operator-b",
        "now": NOW + timedelta(seconds=3),
    }
    arguments.update(kwargs)

    _assert_capability_error(
        lambda: registry.configure_peer(fixture["registry"], **arguments),
        code,
    )
    assert registry.list_peers() == []


def test_capability_rejects_invalid_time_identity_and_peer_registry(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_settings)
    registry = _registry(tmp_settings, fixture)
    _assert_capability_error(
        lambda: registry.configure_peer(
            fixture["registry"],
            "fleet-a",
            mode=federation_custody_capability.COLD_CUSTODY,
            actor="operator-b",
            now=(NOW + timedelta(seconds=3)).replace(tzinfo=None),
        ),
        "FEDERATION_CUSTODY_TIMESTAMP_INVALID",
    )
    _assert_capability_error(
        lambda: federation_custody_capability.FederationCustodyCapabilityRegistry(
            tmp_settings / "invalid-identity.sqlite3",
            {},
        ),
        "FEDERATION_ROOT_IDENTITY_SCHEMA_INVALID",
    )

    other_registry = federation_peer_trust.PeerTrustRegistry(
        tmp_settings / "other-peer-registry.sqlite3",
        fixture["identityA"],
    )
    _assert_capability_error(
        lambda: registry.configure_peer(
            other_registry,
            "fleet-a",
            mode=federation_custody_capability.COLD_CUSTODY,
            actor="operator-b",
        ),
        "FEDERATION_CUSTODY_LOCAL_IDENTITY_MISMATCH",
    )

    monkeypatch.setattr(
        fixture["registry"],
        "require_active_peer",
        lambda _peer: {"peerFleetId": "fleet-a", "rootFingerprint": "invalid"},
    )
    _assert_capability_error(
        lambda: registry.configure_peer(
            fixture["registry"],
            "fleet-a",
            mode=federation_custody_capability.COLD_CUSTODY,
            actor="operator-b",
        ),
        "FEDERATION_CUSTODY_PEER_ROOT_INVALID",
    )


def test_recovery_binding_rejects_invalid_references_and_age_recipient(
    tmp_settings: Path,
) -> None:
    fixture = _fixture(tmp_settings)
    registry = _registry(tmp_settings, fixture)
    cases: tuple[tuple[dict[str, str], str], ...] = (
        ({"credential_provider": "bad provider", "credential_ref": "slot", "age_recipient": RECIPIENT},
         "FEDERATION_RECOVERY_CREDENTIAL_PROVIDER_INVALID"),
        ({"credential_provider": "provider", "credential_ref": "bad ref", "age_recipient": RECIPIENT},
         "FEDERATION_RECOVERY_CREDENTIAL_REFERENCE_INVALID"),
        ({"credential_provider": "provider", "credential_ref": "slot", "age_recipient": "not-age"},
         "FEDERATION_RECOVERY_AGE_RECIPIENT_INVALID"),
        ({"credential_provider": "unregistered-provider", "credential_ref": "slot", "age_recipient": RECIPIENT},
         "FEDERATION_RECOVERY_IDENTITY_NOT_PREPROVISIONED"),
    )
    for kwargs, code in cases:
        _assert_capability_error(
            lambda kwargs=kwargs: registry.configure_peer(
                fixture["registry"],
                "fleet-a",
                mode=federation_custody_capability.RECOVERY_CAPABLE,
                actor="operator-b",
                **kwargs,
            ),
            code,
        )


class _EmptySecretProvider(backup_recovery_credential.RecoveryCredentialProvider):
    def has_credential(self, credential_ref: str) -> bool:
        return True

    def acquire_secret_bytes(self, credential_ref: str) -> bytearray:
        return bytearray()


def test_recovery_binding_rejects_empty_or_invalid_age_identity(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_settings)
    registry = _registry(tmp_settings, fixture)
    empty_provider_name = "fleet-b-empty-recovery"
    backup_recovery_credential.register_provider(empty_provider_name, _EmptySecretProvider())
    _assert_capability_error(
        lambda: registry.configure_peer(
            fixture["registry"],
            "fleet-a",
            mode=federation_custody_capability.RECOVERY_CAPABLE,
            credential_provider=empty_provider_name,
            credential_ref="slot",
            age_recipient=RECIPIENT,
            actor="operator-b",
        ),
        "FEDERATION_RECOVERY_IDENTITY_NOT_PREPROVISIONED",
    )

    provider_name = "fleet-b-invalid-recovery"
    provider = _provider(provider_name)
    provider.set_credential("slot", IDENTITY_TEXT)

    def invalid_identity(_secret: bytearray) -> tuple[str, ...]:
        raise AppError("invalid age identity", code=ErrorCode.INVALID_REQUEST)

    monkeypatch.setattr(backup_crypto, "derive_recipients", invalid_identity)
    _assert_capability_error(
        lambda: registry.configure_peer(
            fixture["registry"],
            "fleet-a",
            mode=federation_custody_capability.RECOVERY_CAPABLE,
            credential_provider=provider_name,
            credential_ref="slot",
            age_recipient=RECIPIENT,
            actor="operator-b",
        ),
        "FEDERATION_RECOVERY_IDENTITY_INVALID",
    )


def test_open_recovery_identity_revalidates_local_secret_and_binding(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_settings)
    registry = _registry(tmp_settings, fixture)
    provider_name = "fleet-b-runtime-recovery"
    provider = _provider(provider_name)
    provider.set_credential("slot", IDENTITY_TEXT)
    monkeypatch.setattr(backup_crypto, "derive_recipients", lambda _secret: (RECIPIENT,))
    registry.configure_peer(
        fixture["registry"],
        "fleet-a",
        mode=federation_custody_capability.RECOVERY_CAPABLE,
        credential_provider=provider_name,
        credential_ref="slot",
        age_recipient=RECIPIENT,
        actor="operator-b",
    )

    provider.clear("slot")
    _assert_capability_error(
        lambda: _consume_recovery_identity(registry, fixture["registry"]),
        "FEDERATION_RECOVERY_IDENTITY_NOT_PREPROVISIONED",
    )
    provider.set_credential("slot", IDENTITY_TEXT)
    monkeypatch.setattr(backup_crypto, "derive_recipients", lambda _secret: ("age1rotatedidentity",))
    _assert_capability_error(
        lambda: _consume_recovery_identity(registry, fixture["registry"]),
        "FEDERATION_RECOVERY_IDENTITY_RECIPIENT_MISMATCH",
    )

    def invalid_identity(_secret: bytearray) -> tuple[str, ...]:
        raise AppError("invalid age identity", code=ErrorCode.INVALID_REQUEST)

    monkeypatch.setattr(backup_crypto, "derive_recipients", invalid_identity)
    _assert_capability_error(
        lambda: _consume_recovery_identity(registry, fixture["registry"]),
        "FEDERATION_RECOVERY_IDENTITY_INVALID",
    )


def test_capability_fails_closed_for_missing_or_tampered_durable_binding(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_settings)
    registry = _registry(tmp_settings, fixture)
    assert registry.get_peer("fleet-a") is None
    _assert_capability_error(
        lambda: _consume_recovery_identity(registry, fixture["registry"]),
        "FEDERATION_CUSTODY_CAPABILITY_NOT_CONFIGURED",
    )

    provider_name = "fleet-b-tampered-binding"
    provider = _provider(provider_name)
    provider.set_credential("slot", IDENTITY_TEXT)
    monkeypatch.setattr(backup_crypto, "derive_recipients", lambda _secret: (RECIPIENT,))
    registry.configure_peer(
        fixture["registry"],
        "fleet-a",
        mode=federation_custody_capability.RECOVERY_CAPABLE,
        credential_provider=provider_name,
        credential_ref="slot",
        age_recipient=RECIPIENT,
        actor="operator-b",
    )

    with closing(sqlite3.connect(registry.db_path)) as connection:
        connection.execute(
            "UPDATE federation_custody_capabilities SET peer_root_fingerprint = ? WHERE peer_fleet_id = ?",
            ("sha256:" + "0" * 64, "fleet-a"),
        )
        connection.commit()
    _assert_capability_error(
        lambda: _consume_recovery_identity(registry, fixture["registry"]),
        "FEDERATION_CUSTODY_PEER_ROOT_CONFLICT",
    )

    with closing(sqlite3.connect(registry.db_path)) as connection:
        connection.execute(
            "UPDATE federation_custody_capabilities SET peer_root_fingerprint = ?, age_recipient_digest = ? WHERE peer_fleet_id = ?",
            (fixture["identityA"]["rootFingerprint"], "sha256:" + "0" * 64, "fleet-a"),
        )
        connection.commit()
    _assert_capability_error(
        lambda: registry.get_peer("fleet-a"),
        "FEDERATION_CUSTODY_CAPABILITY_RECORD_INVALID",
    )

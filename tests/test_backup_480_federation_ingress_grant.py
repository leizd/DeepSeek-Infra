from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import (
    federation_challenge,
    federation_identity,
    federation_ingress_grant,
    federation_peer_trust,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)
TRANSFER_ID = "sha256:" + ("1" * 64)
OBJECT_SET_DIGEST = "sha256:" + ("2" * 64)
POLICY_ID = "policy-federated-custody"
BACKUP_ID = "backup-20260901-001"
PREFIX = f"federation/fleet-a/{TRANSFER_ID}/"


def _metadata(*, region: str) -> dict[str, str]:
    return {
        "provider": "operator-known-provider",
        "region": region,
        "jurisdiction": "CN",
        "siteClass": "independent-datacenter",
    }


def _fixture(
    tmp_path: Path,
) -> tuple[
    federation_peer_trust.PeerTrustRegistry,
    federation_peer_trust.PeerTrustRegistry,
    federation_identity.OnlineFleetSigner,
    federation_identity.OnlineFleetSigner,
    dict[str, object],
    dict[str, object],
]:
    root_a = tmp_path / "fleet-a" / "root.bundle.json"
    root_b = tmp_path / "fleet-b" / "root.bundle.json"
    root_passphrase_a = b"fleet-a-root-passphrase-ingress"
    root_passphrase_b = b"fleet-b-root-passphrase-ingress"
    identity_a = federation_identity.create_fleet_root(
        "fleet-a",
        bundle_path=root_a,
        passphrase=root_passphrase_a,
        now=NOW - timedelta(hours=2),
    )
    identity_b = federation_identity.create_fleet_root(
        "fleet-b",
        bundle_path=root_b,
        passphrase=root_passphrase_b,
        now=NOW - timedelta(hours=2),
    )
    signer_bundle_a = tmp_path / "fleet-a" / "signer.bundle.json"
    signer_bundle_b = tmp_path / "fleet-b" / "signer.bundle.json"
    certificate_a = federation_identity.issue_online_signer(
        root_bundle_path=root_a,
        root_passphrase=root_passphrase_a,
        signer_bundle_path=signer_bundle_a,
        signer_passphrase=b"fleet-a-signer-passphrase-ingress",
        sequence=1,
        not_before=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=2),
        purposes=(federation_identity.PURPOSE_SESSION_AUTHENTICATION,),
    )
    certificate_b = federation_identity.issue_online_signer(
        root_bundle_path=root_b,
        root_passphrase=root_passphrase_b,
        signer_bundle_path=signer_bundle_b,
        signer_passphrase=b"fleet-b-signer-passphrase-ingress",
        sequence=1,
        not_before=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=2),
        purposes=(
            federation_identity.PURPOSE_SESSION_AUTHENTICATION,
            federation_identity.PURPOSE_INGRESS_GRANT,
        ),
    )
    signer_a = federation_identity.load_online_signer(
        signer_bundle_a,
        b"fleet-a-signer-passphrase-ingress",
        root_identity=identity_a,
        now=NOW,
    )
    signer_b = federation_identity.load_online_signer(
        signer_bundle_b,
        b"fleet-b-signer-passphrase-ingress",
        root_identity=identity_b,
        now=NOW,
    )
    registry_a = federation_peer_trust.PeerTrustRegistry(tmp_path / "fleet-a" / "trust.sqlite3", identity_a)
    registry_b = federation_peer_trust.PeerTrustRegistry(tmp_path / "fleet-b" / "trust.sqlite3", identity_b)
    registry_a.pin_peer(
        identity_b,
        expected_root_fingerprint=str(identity_b["rootFingerprint"]),
        metadata=_metadata(region="cn-south-1"),
        operator_id="operator-a",
        now=NOW - timedelta(minutes=30),
    )
    registry_a.verify_peer("fleet-b", identity_b, actor="verifier-a", now=NOW - timedelta(minutes=29))
    registry_a.activate_peer("fleet-b", actor="operator-a", now=NOW - timedelta(minutes=28))
    registry_a.accept_online_signer("fleet-b", certificate_b, actor="operator-a", now=NOW)
    registry_b.pin_peer(
        identity_a,
        expected_root_fingerprint=str(identity_a["rootFingerprint"]),
        metadata=_metadata(region="cn-north-1"),
        operator_id="operator-b",
        now=NOW - timedelta(minutes=30),
    )
    registry_b.verify_peer("fleet-a", identity_a, actor="verifier-b", now=NOW - timedelta(minutes=29))
    registry_b.activate_peer("fleet-a", actor="operator-b", now=NOW - timedelta(minutes=28))
    registry_b.accept_online_signer("fleet-a", certificate_a, actor="operator-b", now=NOW)
    challenge = federation_challenge.issue_federation_challenge(
        peer_registry=registry_a,
        challenger_signer=signer_a,
        destination_fleet_id="fleet-b",
        session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
        now=NOW,
    )
    federation_challenge.respond_to_federation_challenge(
        challenge,
        peer_registry=registry_b,
        responder_signer=signer_b,
        now=NOW + timedelta(seconds=1),
    )
    return registry_a, registry_b, signer_a, signer_b, challenge, identity_b


def _issue(
    registry_b: federation_peer_trust.PeerTrustRegistry,
    signer_b: federation_identity.OnlineFleetSigner,
    challenge: dict[str, object],
    *,
    now: datetime = NOW + timedelta(seconds=2),
    expires_at: datetime = NOW + timedelta(seconds=90),
) -> dict[str, object]:
    return federation_ingress_grant.issue_ingress_grant(
        peer_registry=registry_b,
        receiver_signer=signer_b,
        source_fleet_id="fleet-a",
        session_nonce=str(challenge["nonce"]),
        transfer_id=TRANSFER_ID,
        policy_id=POLICY_ID,
        backup_id=BACKUP_ID,
        object_set_digest=OBJECT_SET_DIGEST,
        allowed_object_prefix=PREFIX,
        max_bytes=1_000,
        now=now,
        expires_at=expires_at,
    )


def _resign(
    signer: federation_identity.OnlineFleetSigner,
    grant: dict[str, object],
    *,
    remove: tuple[str, ...] = (),
    **changes: object,
) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in grant.items()
        if key not in {"signerKeyId", "signatureAlgorithm", "signature"} and key not in remove
    }
    payload.update(changes)
    return federation_identity.sign_federation_document(
        signer,
        payload,
        purpose=federation_identity.PURPOSE_INGRESS_GRANT,
    )


def _verify(
    registry_a: federation_peer_trust.PeerTrustRegistry,
    grant: dict[str, Any],
    *,
    source: str = "fleet-a",
    destination: str = "fleet-b",
    transfer_id: str = TRANSFER_ID,
    policy_id: str = POLICY_ID,
    backup_id: str = BACKUP_ID,
    object_set_digest: str = OBJECT_SET_DIGEST,
    now: datetime = NOW + timedelta(seconds=3),
    max_future_skew_seconds: int = 30,
) -> dict[str, Any]:
    return federation_ingress_grant.verify_ingress_grant(
        grant,
        peer_registry=registry_a,
        expected_source_fleet_id=source,
        expected_destination_fleet_id=destination,
        expected_transfer_id=transfer_id,
        expected_policy_id=policy_id,
        expected_backup_id=backup_id,
        expected_object_set_digest=object_set_digest,
        now=now,
        max_future_skew_seconds=max_future_skew_seconds,
    )


def _new_remote_custody_session(
    registry_a: federation_peer_trust.PeerTrustRegistry,
    registry_b: federation_peer_trust.PeerTrustRegistry,
    signer_a: federation_identity.OnlineFleetSigner,
    signer_b: federation_identity.OnlineFleetSigner,
    *,
    now: datetime,
) -> dict[str, Any]:
    challenge = federation_challenge.issue_federation_challenge(
        peer_registry=registry_a,
        challenger_signer=signer_a,
        destination_fleet_id="fleet-b",
        session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
        now=now,
    )
    federation_challenge.respond_to_federation_challenge(
        challenge,
        peer_registry=registry_b,
        responder_signer=signer_b,
        now=now + timedelta(seconds=1),
    )
    return challenge


def test_receiver_signed_ingress_grant_binds_scope_session_and_has_no_credentials(tmp_path: Path) -> None:
    registry_a, registry_b, _, signer_b, challenge, _ = _fixture(tmp_path)
    grant = _issue(registry_b, signer_b, challenge)

    assert grant["schema"] == "federation-ingress-grant-v1"
    assert grant["fleetId"] == grant["destinationFleetId"] == "fleet-b"
    assert grant["sourceFleetId"] == "fleet-a"
    assert grant["transferId"] == TRANSFER_ID
    assert grant["policyId"] == POLICY_ID
    assert grant["backupId"] == BACKUP_ID
    assert grant["objectSetDigest"] == OBJECT_SET_DIGEST
    assert grant["allowedObjectPrefix"] == PREFIX
    assert grant["maxBytes"] == 1_000
    assert grant["sessionNonceDigest"] == federation_challenge.nonce_digest(str(challenge["nonce"]))
    assert str(grant["grantId"]).startswith("grant-")
    assert len(str(grant["nonce"])) == 43
    public_json = json.dumps(grant, sort_keys=True).casefold()
    for forbidden in ("credential", "password", "privatekey", "age-secret-key", "accesskey", "secretkey"):
        assert forbidden not in public_json.replace("_", "").replace("-", "")

    verified = federation_ingress_grant.verify_ingress_grant(
        grant,
        peer_registry=registry_a,
        expected_source_fleet_id="fleet-a",
        expected_destination_fleet_id="fleet-b",
        expected_transfer_id=TRANSFER_ID,
        expected_policy_id=POLICY_ID,
        expected_backup_id=BACKUP_ID,
        expected_object_set_digest=OBJECT_SET_DIGEST,
        now=NOW + timedelta(seconds=3),
    )
    assert verified == grant
    stored = registry_b.get_ingress_grant(str(grant["grantId"]))
    assert stored is not None
    assert stored["grant"] == grant
    assert stored["bytesReserved"] == 0
    assert stored["state"] == "ACTIVE"


def test_ingress_write_is_receiver_mediated_prefix_scoped_and_byte_bounded(tmp_path: Path) -> None:
    _, registry_b, _, signer_b, challenge, _ = _fixture(tmp_path)
    grant = _issue(registry_b, signer_b, challenge)
    first = federation_ingress_grant.authorize_ingress_write(
        grant,
        peer_registry=registry_b,
        source_fleet_id="fleet-a",
        write_id="write-component-1",
        object_key=PREFIX + "objects/component-1.age",
        byte_count=600,
        now=NOW + timedelta(seconds=3),
    )
    assert first["schema"] == "federation-ingress-write-reservation-v1"
    assert first["byteCount"] == 600
    assert first["bytesReservedAfter"] == 600
    assert federation_ingress_grant.authorize_ingress_write(
        grant,
        peer_registry=registry_b,
        source_fleet_id="fleet-a",
        write_id="write-component-1",
        object_key=PREFIX + "objects/component-1.age",
        byte_count=600,
        now=NOW + timedelta(seconds=4),
    ) == first
    second = federation_ingress_grant.authorize_ingress_write(
        grant,
        peer_registry=registry_b,
        source_fleet_id="fleet-a",
        write_id="write-component-2",
        object_key=PREFIX + "objects/component-2.age",
        byte_count=400,
        now=NOW + timedelta(seconds=4),
    )
    assert second["bytesReservedAfter"] == 1_000
    assert len(registry_b.list_ingress_writes(str(grant["grantId"]))) == 2

    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as overrun:
        federation_ingress_grant.authorize_ingress_write(
            grant,
            peer_registry=registry_b,
            source_fleet_id="fleet-a",
            write_id="write-component-3",
            object_key=PREFIX + "objects/component-3.age",
            byte_count=1,
            now=NOW + timedelta(seconds=5),
        )
    assert overrun.value.code == "FEDERATION_INGRESS_MAX_BYTES_EXCEEDED"
    for escaped_key in (
        "other-prefix/component.age",
        PREFIX + "../escape.age",
        PREFIX + "objects\\escape.age",
        PREFIX + "objects//ambiguous.age",
    ):
        with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as escaped:
            federation_ingress_grant.authorize_ingress_write(
                grant,
                peer_registry=registry_b,
                source_fleet_id="fleet-a",
                write_id="write-escape-" + str(abs(hash(escaped_key))),
                object_key=escaped_key,
                byte_count=1,
                now=NOW + timedelta(seconds=5),
            )
        assert escaped.value.code == "FEDERATION_INGRESS_OBJECT_PREFIX_VIOLATION"


def test_ingress_grant_replay_expiry_tamper_wrong_fleet_and_write_identity_fail_closed(tmp_path: Path) -> None:
    registry_a, registry_b, _, signer_b, challenge, _ = _fixture(tmp_path)
    grant = _issue(registry_b, signer_b, challenge)
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as session_replay:
        _issue(registry_b, signer_b, challenge)
    assert session_replay.value.code == "FEDERATION_INGRESS_SESSION_REPLAY"
    assert registry_b.get_ingress_grant_by_session_nonce(
        federation_challenge.nonce_digest(str(challenge["nonce"]))
    )["grant"] == grant  # type: ignore[index]

    tampered = {**grant, "maxBytes": 10_000}
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as tamper:
        federation_ingress_grant.verify_ingress_grant(
            tampered,
            peer_registry=registry_a,
            expected_source_fleet_id="fleet-a",
            expected_destination_fleet_id="fleet-b",
            expected_transfer_id=TRANSFER_ID,
            expected_policy_id=POLICY_ID,
            expected_backup_id=BACKUP_ID,
            expected_object_set_digest=OBJECT_SET_DIGEST,
            now=NOW + timedelta(seconds=3),
        )
    assert tamper.value.code == "FEDERATION_DOCUMENT_SIGNATURE_INVALID"
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as wrong_fleet:
        federation_ingress_grant.verify_ingress_grant(
            grant,
            peer_registry=registry_a,
            expected_source_fleet_id="fleet-c",
            expected_destination_fleet_id="fleet-b",
            expected_transfer_id=TRANSFER_ID,
            expected_policy_id=POLICY_ID,
            expected_backup_id=BACKUP_ID,
            expected_object_set_digest=OBJECT_SET_DIGEST,
            now=NOW + timedelta(seconds=3),
        )
    assert wrong_fleet.value.code == "FEDERATION_INGRESS_SOURCE_FLEET_MISMATCH"
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as expired:
        federation_ingress_grant.authorize_ingress_write(
            grant,
            peer_registry=registry_b,
            source_fleet_id="fleet-a",
            write_id="write-expired",
            object_key=PREFIX + "objects/expired.age",
            byte_count=1,
            now=NOW + timedelta(seconds=90),
        )
    assert expired.value.code == "FEDERATION_INGRESS_GRANT_EXPIRED"

    federation_ingress_grant.authorize_ingress_write(
        grant,
        peer_registry=registry_b,
        source_fleet_id="fleet-a",
        write_id="write-immutable",
        object_key=PREFIX + "objects/one.age",
        byte_count=1,
        now=NOW + timedelta(seconds=3),
    )
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as write_conflict:
        federation_ingress_grant.authorize_ingress_write(
            grant,
            peer_registry=registry_b,
            source_fleet_id="fleet-a",
            write_id="write-immutable",
            object_key=PREFIX + "objects/two.age",
            byte_count=1,
            now=NOW + timedelta(seconds=4),
        )
    assert write_conflict.value.code == "FEDERATION_INGRESS_WRITE_IDENTITY_CONFLICT"


def test_ingress_grant_requires_authenticated_remote_custody_session_and_current_peer(tmp_path: Path) -> None:
    _, registry_b, _, signer_b, challenge, _ = _fixture(tmp_path)
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as unknown_session:
        federation_ingress_grant.issue_ingress_grant(
            peer_registry=registry_b,
            receiver_signer=signer_b,
            source_fleet_id="fleet-a",
            session_nonce=federation_challenge.generate_nonce(),
            transfer_id=TRANSFER_ID,
            policy_id=POLICY_ID,
            backup_id=BACKUP_ID,
            object_set_digest=OBJECT_SET_DIGEST,
            allowed_object_prefix=PREFIX,
            max_bytes=1_000,
            now=NOW + timedelta(seconds=2),
            expires_at=NOW + timedelta(seconds=90),
        )
    assert unknown_session.value.code == "FEDERATION_INGRESS_SESSION_NOT_AUTHENTICATED"

    registry_b.revoke_peer("fleet-a", actor="operator-b", reason="peer-incident", now=NOW + timedelta(seconds=2))
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as revoked:
        _issue(registry_b, signer_b, challenge)
    assert revoked.value.code == "FEDERATION_PEER_REVOKED"

    _, fresh_registry_b, fresh_signer_a, fresh_signer_b, fresh_challenge, _ = _fixture(tmp_path / "signer-revoked")
    fresh_registry_b.revoke_online_signer(
        "fleet-a",
        fresh_signer_a.signer_key_id,
        actor="operator-b",
        reason="signer-incident",
        revoked_at=NOW + timedelta(seconds=2),
    )
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as signer_revoked:
        _issue(fresh_registry_b, fresh_signer_b, fresh_challenge)
    assert signer_revoked.value.code == "FEDERATION_SIGNER_REVOKED"


def test_ingress_grant_issue_input_and_default_window_fail_closed(tmp_path: Path) -> None:
    _, registry_b, signer_a, signer_b, challenge, _ = _fixture(tmp_path)
    common = {
        "peer_registry": registry_b,
        "receiver_signer": signer_b,
        "source_fleet_id": "fleet-a",
        "session_nonce": str(challenge["nonce"]),
        "transfer_id": TRANSFER_ID,
        "policy_id": POLICY_ID,
        "backup_id": BACKUP_ID,
        "object_set_digest": OBJECT_SET_DIGEST,
        "allowed_object_prefix": PREFIX,
        "max_bytes": 1_000,
        "now": NOW + timedelta(seconds=2),
        "expires_at": NOW + timedelta(seconds=90),
    }
    invalid_cases = (
        ({"now": datetime(2026, 9, 1, 6, 0)}, "FEDERATION_INGRESS_TIMESTAMP_INVALID"),
        ({"expires_at": NOW + timedelta(seconds=2)}, "FEDERATION_INGRESS_GRANT_LIFETIME_INVALID"),
        ({"expires_at": NOW + timedelta(seconds=303)}, "FEDERATION_INGRESS_GRANT_LIFETIME_INVALID"),
        ({"source_fleet_id": "Fleet A"}, "FEDERATION_INGRESS_FLEET_ID_INVALID"),
        ({"session_nonce": "not-a-32-byte-nonce"}, "FEDERATION_CHALLENGE_NONCE_INVALID"),
        ({"transfer_id": "bad"}, "FEDERATION_INGRESS_TRANSFER_ID_INVALID"),
        ({"policy_id": "bad/policy"}, "FEDERATION_INGRESS_POLICY_ID_INVALID"),
        ({"backup_id": "bad/backup"}, "FEDERATION_INGRESS_BACKUP_ID_INVALID"),
        ({"object_set_digest": "bad"}, "FEDERATION_INGRESS_OBJECT_SET_DIGEST_INVALID"),
        ({"allowed_object_prefix": "outside/"}, "FEDERATION_INGRESS_OBJECT_PREFIX_INVALID"),
        ({"allowed_object_prefix": "federation//escape/"}, "FEDERATION_INGRESS_OBJECT_PREFIX_INVALID"),
        ({"allowed_object_prefix": "federation/%2e%2e/escape/"}, "FEDERATION_INGRESS_OBJECT_PREFIX_INVALID"),
        ({"allowed_object_prefix": "federation/../escape/"}, "FEDERATION_INGRESS_OBJECT_PREFIX_INVALID"),
        ({"max_bytes": 0}, "FEDERATION_INGRESS_MAX_BYTES_INVALID"),
        ({"max_bytes": True}, "FEDERATION_INGRESS_MAX_BYTES_INVALID"),
        (
            {"max_bytes": federation_ingress_grant.MAX_INGRESS_GRANT_BYTES + 1},
            "FEDERATION_INGRESS_MAX_BYTES_INVALID",
        ),
        ({"receiver_signer": signer_a}, "FEDERATION_INGRESS_LOCAL_SIGNER_MISMATCH"),
    )
    for changes, expected_code in invalid_cases:
        with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as rejected:
            federation_ingress_grant.issue_ingress_grant(**{**common, **changes})  # type: ignore[arg-type]
        assert rejected.value.code == expected_code

    default_grant = federation_ingress_grant.issue_ingress_grant(
        **{key: value for key, value in common.items() if key != "expires_at"}  # type: ignore[arg-type]
    )
    assert datetime.fromisoformat(str(default_grant["expiresAt"]).replace("Z", "+00:00")) - datetime.fromisoformat(
        str(default_grant["issuedAt"]).replace("Z", "+00:00")
    ) == timedelta(seconds=federation_ingress_grant.DEFAULT_INGRESS_GRANT_LIFETIME_SECONDS)
    assert federation_ingress_grant.grant_digest(default_grant).startswith("sha256:")


def test_signed_ingress_grant_semantic_variants_fail_closed(tmp_path: Path) -> None:
    registry_a, registry_b, _, signer_b, challenge, _ = _fixture(tmp_path)
    grant = _issue(registry_b, signer_b, challenge)
    other_transfer = "sha256:" + ("3" * 64)
    other_object_set = "sha256:" + ("4" * 64)
    cases: list[tuple[dict[str, Any], dict[str, Any], str]] = [
        (_resign(signer_b, grant, remove=("maxBytes",)), {}, "FEDERATION_INGRESS_GRANT_FIELDS_INVALID"),
        (_resign(signer_b, grant, grantId="invalid"), {}, "FEDERATION_INGRESS_GRANT_ID_INVALID"),
        (
            _resign(signer_b, grant, destinationFleetId="fleet-c"),
            {},
            "FEDERATION_INGRESS_DESTINATION_FLEET_MISMATCH",
        ),
        (
            _resign(signer_b, grant, sourceFleetId="fleet-b"),
            {"source": "fleet-b"},
            "FEDERATION_INGRESS_REFLECTION_REJECTED",
        ),
        (_resign(signer_b, grant, transferId=other_transfer), {}, "FEDERATION_INGRESS_TRANSFER_ID_MISMATCH"),
        (_resign(signer_b, grant, policyId="policy-other"), {}, "FEDERATION_INGRESS_POLICY_ID_MISMATCH"),
        (_resign(signer_b, grant, backupId="backup-other"), {}, "FEDERATION_INGRESS_BACKUP_ID_MISMATCH"),
        (
            _resign(signer_b, grant, objectSetDigest=other_object_set),
            {},
            "FEDERATION_INGRESS_OBJECT_SET_DIGEST_MISMATCH",
        ),
        (
            _resign(signer_b, grant, allowedObjectPrefix="federation/../escape/"),
            {},
            "FEDERATION_INGRESS_OBJECT_PREFIX_INVALID",
        ),
        (_resign(signer_b, grant, maxBytes=0), {}, "FEDERATION_INGRESS_MAX_BYTES_INVALID"),
        (_resign(signer_b, grant, nonce="bad"), {}, "FEDERATION_INGRESS_GRANT_NONCE_INVALID"),
        (
            _resign(signer_b, grant, sessionNonceDigest="bad"),
            {},
            "FEDERATION_INGRESS_SESSION_NONCE_DIGEST_INVALID",
        ),
        (
            _resign(
                signer_b,
                grant,
                issuedAt=(NOW + timedelta(seconds=40)).isoformat(timespec="seconds").replace("+00:00", "Z"),
                expiresAt=(NOW + timedelta(seconds=80)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            ),
            {"now": NOW + timedelta(seconds=3), "max_future_skew_seconds": 0},
            "FEDERATION_INGRESS_GRANT_FROM_FUTURE",
        ),
        (
            _resign(
                signer_b,
                grant,
                expiresAt=(NOW + timedelta(seconds=303)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            ),
            {},
            "FEDERATION_INGRESS_GRANT_LIFETIME_INVALID",
        ),
        (
            _resign(
                signer_b,
                grant,
                issuedAt=(NOW + timedelta(hours=1, minutes=59, seconds=30))
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                expiresAt=(NOW + timedelta(hours=2, seconds=30))
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            ),
            {"now": NOW + timedelta(hours=1, minutes=59, seconds=30)},
            "FEDERATION_INGRESS_SIGNER_WINDOW_INVALID",
        ),
        (
            _resign(signer_b, grant, sessionNonceDigest="sha256:" + ("5" * 64)),
            {},
            "FEDERATION_INGRESS_SESSION_NOT_AUTHENTICATED",
        ),
    ]
    for variant, options, expected_code in cases:
        with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as rejected:
            _verify(registry_a, variant, **options)
        assert rejected.value.code == expected_code

    expired = _resign(
        signer_b,
        grant,
        expiresAt=(NOW + timedelta(seconds=3)).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as expired_error:
        _verify(registry_a, expired, now=NOW + timedelta(seconds=3))
    assert expired_error.value.code == "FEDERATION_INGRESS_GRANT_EXPIRED"

    unsigned_certificate = _resign(signer_b, grant, signerCertificate="invalid")
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as certificate_error:
        _verify(registry_a, unsigned_certificate)
    assert certificate_error.value.code == "FEDERATION_INGRESS_SIGNER_CERTIFICATE_INVALID"

    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as unknown_peer:
        _verify(registry_a, grant, destination="fleet-c")
    assert unknown_peer.value.code == "FEDERATION_PEER_NOT_PINNED"


def test_ingress_write_reservations_are_atomic_across_threads_and_reopen(tmp_path: Path) -> None:
    _, registry_b, _, signer_b, challenge, _ = _fixture(tmp_path)
    grant = _issue(registry_b, signer_b, challenge)

    def reserve(write_id: str, byte_count: int, barrier: threading.Barrier) -> dict[str, Any] | str:
        barrier.wait(timeout=10)
        try:
            return federation_ingress_grant.authorize_ingress_write(
                grant,
                peer_registry=registry_b,
                source_fleet_id="fleet-a",
                write_id=write_id,
                object_key=PREFIX + f"objects/{write_id}.age",
                byte_count=byte_count,
                now=NOW + timedelta(seconds=3),
            )
        except federation_ingress_grant.FederationIngressGrantError as exc:
            return exc.code

    same_barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        same_results = list(executor.map(lambda _: reserve("write-same", 600, same_barrier), range(2)))
    assert all(isinstance(result, dict) for result in same_results)
    assert same_results[0] == same_results[1]
    assert registry_b.get_ingress_grant(str(grant["grantId"]))["bytesReserved"] == 600  # type: ignore[index]

    competing_barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        competing_results = list(
            executor.map(lambda index: reserve(f"write-competing-{index}", 400, competing_barrier), range(2))
        )
    assert sum(isinstance(result, dict) for result in competing_results) == 1
    assert competing_results.count("FEDERATION_INGRESS_MAX_BYTES_EXCEEDED") == 1
    assert registry_b.get_ingress_grant(str(grant["grantId"]))["bytesReserved"] == 1_000  # type: ignore[index]

    reopened = federation_peer_trust.PeerTrustRegistry(registry_b.db_path, registry_b.local_identity)
    assert reopened.get_ingress_grant(str(grant["grantId"])) == registry_b.get_ingress_grant(str(grant["grantId"]))
    assert len(reopened.list_ingress_writes(str(grant["grantId"]))) == 2


def test_ingress_write_invalid_identity_scope_and_peer_state_fail_closed(tmp_path: Path) -> None:
    _, registry_b, _, signer_b, challenge, _ = _fixture(tmp_path)
    grant = _issue(registry_b, signer_b, challenge)
    invalid_cases = (
        ({"write_id": "bad/write"}, "FEDERATION_INGRESS_WRITE_ID_INVALID"),
        ({"byte_count": 0}, "FEDERATION_INGRESS_BYTE_COUNT_INVALID"),
        ({"byte_count": True}, "FEDERATION_INGRESS_BYTE_COUNT_INVALID"),
        ({"source_fleet_id": "fleet-c"}, "FEDERATION_PEER_NOT_PINNED"),
        ({"object_key": PREFIX + "objects/%2e%2e/escape.age"}, "FEDERATION_INGRESS_OBJECT_PREFIX_VIOLATION"),
        ({"object_key": PREFIX}, "FEDERATION_INGRESS_OBJECT_PREFIX_VIOLATION"),
    )
    common = {
        "grant": grant,
        "peer_registry": registry_b,
        "source_fleet_id": "fleet-a",
        "write_id": "write-valid",
        "object_key": PREFIX + "objects/valid.age",
        "byte_count": 1,
        "now": NOW + timedelta(seconds=3),
    }
    for changes, expected_code in invalid_cases:
        with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as rejected:
            federation_ingress_grant.authorize_ingress_write(**{**common, **changes})  # type: ignore[arg-type]
        assert rejected.value.code == expected_code

    tampered = {**grant, "maxBytes": 999}
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as identity_conflict:
        federation_ingress_grant.authorize_ingress_write(
            tampered,
            peer_registry=registry_b,
            source_fleet_id="fleet-a",
            write_id="write-tampered",
            object_key=PREFIX + "objects/tampered.age",
            byte_count=1,
            now=NOW + timedelta(seconds=3),
        )
    assert identity_conflict.value.code == "FEDERATION_INGRESS_GRANT_IDENTITY_CONFLICT"

    registry_b.suspend_peer("fleet-a", actor="operator-b", reason="maintenance", now=NOW + timedelta(seconds=4))
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as suspended:
        federation_ingress_grant.authorize_ingress_write(
            grant,
            peer_registry=registry_b,
            source_fleet_id="fleet-a",
            write_id="write-suspended",
            object_key=PREFIX + "objects/suspended.age",
            byte_count=1,
            now=NOW + timedelta(seconds=5),
        )
    assert suspended.value.code == "FEDERATION_PEER_NOT_ACTIVE"


def test_ingress_document_parsers_and_session_limits_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_a, registry_b, _, signer_b, challenge, _ = _fixture(tmp_path)
    grant = _issue(registry_b, signer_b, challenge)

    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as non_json:
        _verify(registry_a, {"notJson": object()})
    assert non_json.value.code == "FEDERATION_INGRESS_CANONICAL_PAYLOAD_INVALID"
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as digest_non_json:
        federation_ingress_grant.grant_digest({"notJson": object()})
    assert digest_non_json.value.code == "FEDERATION_INGRESS_CANONICAL_PAYLOAD_INVALID"
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as non_document:
        _verify(registry_a, [])  # type: ignore[arg-type]
    assert non_document.value.code == "FEDERATION_INGRESS_GRANT_DOCUMENT_INVALID"

    invalid_timestamps = (None, "not-a-time", "2026-09-01T06:00:02", "2026-09-01T06:00:02+00:00")
    for issued_at in invalid_timestamps:
        variant = _resign(signer_b, grant, issuedAt=issued_at)
        with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as invalid_timestamp:
            _verify(registry_a, variant)
        assert invalid_timestamp.value.code == "FEDERATION_INGRESS_TIMESTAMP_INVALID"

    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as schema_error:
        federation_ingress_grant._grant_semantics(  # noqa: SLF001
            {**grant, "schema": "federation-ingress-grant-v2"},
            expected_source_fleet_id="fleet-a",
            expected_destination_fleet_id="fleet-b",
            expected_transfer_id=TRANSFER_ID,
            expected_policy_id=POLICY_ID,
            expected_backup_id=BACKUP_ID,
            expected_object_set_digest=OBJECT_SET_DIGEST,
            now=NOW + timedelta(seconds=3),
            max_future_skew_seconds=30,
        )
    assert schema_error.value.code == "FEDERATION_INGRESS_GRANT_SCHEMA_INVALID"
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as semantics_certificate:
        federation_ingress_grant._grant_semantics(  # noqa: SLF001
            {**grant, "signerCertificate": "invalid"},
            expected_source_fleet_id="fleet-a",
            expected_destination_fleet_id="fleet-b",
            expected_transfer_id=TRANSFER_ID,
            expected_policy_id=POLICY_ID,
            expected_backup_id=BACKUP_ID,
            expected_object_set_digest=OBJECT_SET_DIGEST,
            now=NOW + timedelta(seconds=3),
            max_future_skew_seconds=30,
        )
    assert semantics_certificate.value.code == "FEDERATION_INGRESS_SIGNER_CERTIFICATE_INVALID"

    certificate_expiry = datetime.fromisoformat(
        str(signer_b.certificate["expiresAt"]).replace("Z", "+00:00")
    )
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as signer_window:
        federation_ingress_grant._require_local_signer(  # noqa: SLF001
            registry_b,
            signer_b,
            at=NOW + timedelta(seconds=2),
            document_expires_at=certificate_expiry + timedelta(seconds=1),
        )
    assert signer_window.value.code == "FEDERATION_INGRESS_SIGNER_WINDOW_INVALID"

    monkeypatch.setattr(registry_a, "get_peer", lambda _fleet_id: {"fleetIdentity": "invalid"})
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as invalid_root:
        _verify(registry_a, grant)
    assert invalid_root.value.code == "FEDERATION_PEER_IDENTITY_INVALID"


def test_ingress_issue_enforces_session_expiry_and_wraps_journal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, registry_b, _, signer_b, challenge, _ = _fixture(tmp_path)
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as beyond_session:
        _issue(
            registry_b,
            signer_b,
            challenge,
            expires_at=NOW + timedelta(seconds=121),
        )
    assert beyond_session.value.code == "FEDERATION_INGRESS_SESSION_NOT_AUTHENTICATED"

    def reject_record(_grant: dict[str, Any], *, recorded_at: datetime) -> dict[str, Any]:
        del recorded_at
        raise federation_peer_trust.FederationTrustError("FEDERATION_INGRESS_JOURNAL_UNAVAILABLE")

    monkeypatch.setattr(registry_b, "record_ingress_grant", reject_record)
    with pytest.raises(federation_ingress_grant.FederationIngressGrantError) as journal_failure:
        _issue(registry_b, signer_b, challenge)
    assert journal_failure.value.code == "FEDERATION_INGRESS_JOURNAL_UNAVAILABLE"


def test_ingress_grant_journal_independently_revalidates_every_binding(tmp_path: Path) -> None:
    registry_a, registry_b, signer_a, signer_b, challenge, _ = _fixture(tmp_path)
    grant = _issue(registry_b, signer_b, challenge)

    invalid_documents: tuple[tuple[object, datetime, str], ...] = (
        ([], NOW + timedelta(seconds=2), "FEDERATION_INGRESS_GRANT_DOCUMENT_INVALID"),
        ({"notJson": object()}, NOW + timedelta(seconds=2), "FEDERATION_INGRESS_GRANT_DOCUMENT_INVALID"),
        (
            {key: value for key, value in grant.items() if key != "maxBytes"},
            NOW + timedelta(seconds=2),
            "FEDERATION_INGRESS_GRANT_FIELDS_INVALID",
        ),
        ({**grant, "schema": "federation-ingress-grant-v2"}, NOW + timedelta(seconds=2), "FEDERATION_INGRESS_GRANT_SCHEMA_INVALID"),
        ({**grant, "fleetId": "fleet-c"}, NOW + timedelta(seconds=2), "FEDERATION_INGRESS_GRANT_FLEET_BINDING_INVALID"),
        ({**grant, "grantId": "bad"}, NOW + timedelta(seconds=2), "FEDERATION_INGRESS_GRANT_ID_INVALID"),
        ({**grant, "transferId": "bad"}, NOW + timedelta(seconds=2), "FEDERATION_INGRESS_TRANSFER_ID_INVALID"),
        ({**grant, "policyId": "bad/policy"}, NOW + timedelta(seconds=2), "FEDERATION_INGRESS_POLICY_ID_INVALID"),
        ({**grant, "backupId": "bad/backup"}, NOW + timedelta(seconds=2), "FEDERATION_INGRESS_BACKUP_ID_INVALID"),
        (
            {**grant, "objectSetDigest": "bad"},
            NOW + timedelta(seconds=2),
            "FEDERATION_INGRESS_OBJECT_SET_DIGEST_INVALID",
        ),
        (
            {**grant, "allowedObjectPrefix": "outside/"},
            NOW + timedelta(seconds=2),
            "FEDERATION_INGRESS_OBJECT_PREFIX_INVALID",
        ),
        ({**grant, "maxBytes": 0}, NOW + timedelta(seconds=2), "FEDERATION_INGRESS_MAX_BYTES_INVALID"),
        (
            {**grant, "maxBytes": federation_ingress_grant.MAX_INGRESS_GRANT_BYTES + 1},
            NOW + timedelta(seconds=2),
            "FEDERATION_INGRESS_MAX_BYTES_INVALID",
        ),
        ({**grant, "nonce": None}, NOW + timedelta(seconds=2), "FEDERATION_CHALLENGE_NONCE_INVALID"),
        ({**grant, "nonce": "\N{SNOWMAN}"}, NOW + timedelta(seconds=2), "FEDERATION_CHALLENGE_NONCE_INVALID"),
        ({**grant, "signerKeyId": "bad"}, NOW + timedelta(seconds=2), "FEDERATION_SIGNER_KEY_ID_INVALID"),
        (
            {**grant, "issuedAt": "2026-09-01T06:00:02+00:00"},
            NOW + timedelta(seconds=2),
            "FEDERATION_INGRESS_GRANT_WINDOW_INVALID",
        ),
        (grant, NOW + timedelta(seconds=1), "FEDERATION_INGRESS_GRANT_WINDOW_INVALID"),
        (
            {**grant, "signerCertificate": "invalid"},
            NOW + timedelta(seconds=2),
            "FEDERATION_INGRESS_SIGNER_CERTIFICATE_INVALID",
        ),
        (
            {
                **grant,
                "signerCertificate": {
                    **signer_b.certificate,
                    "expiresAt": (NOW + timedelta(seconds=30)).isoformat(timespec="seconds").replace("+00:00", "Z"),
                },
            },
            NOW + timedelta(seconds=2),
            "FEDERATION_INGRESS_SIGNER_WINDOW_INVALID",
        ),
    )
    for document, recorded_at, expected_code in invalid_documents:
        with pytest.raises(federation_peer_trust.FederationTrustError) as rejected:
            registry_b.record_ingress_grant(document, recorded_at=recorded_at)  # type: ignore[arg-type]
        assert rejected.value.code == expected_code

    unknown_session = _resign(
        signer_b,
        grant,
        grantId="grant-" + ("1" * 32),
        nonce=federation_challenge.generate_nonce(),
        sessionNonceDigest="sha256:" + ("6" * 64),
    )
    with pytest.raises(federation_peer_trust.FederationTrustError) as unknown_session_error:
        registry_b.record_ingress_grant(unknown_session, recorded_at=NOW + timedelta(seconds=2))
    assert unknown_session_error.value.code == "FEDERATION_INGRESS_SESSION_NOT_AUTHENTICATED"

    second_session_at = NOW + timedelta(seconds=10)
    second_challenge = _new_remote_custody_session(
        registry_a,
        registry_b,
        signer_a,
        signer_b,
        now=second_session_at,
    )
    second_session_digest = federation_challenge.nonce_digest(str(second_challenge["nonce"]))
    beyond_session = _resign(
        signer_b,
        grant,
        grantId="grant-" + ("2" * 32),
        nonce=federation_challenge.generate_nonce(),
        sessionNonceDigest=second_session_digest,
        issuedAt=(second_session_at + timedelta(seconds=2)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        expiresAt=(second_session_at + timedelta(seconds=121)).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    with pytest.raises(federation_peer_trust.FederationTrustError) as session_scope_error:
        registry_b.record_ingress_grant(beyond_session, recorded_at=second_session_at + timedelta(seconds=2))
    assert session_scope_error.value.code == "FEDERATION_INGRESS_SESSION_NOT_AUTHENTICATED"

    identity_conflict = _resign(
        signer_b,
        grant,
        nonce=federation_challenge.generate_nonce(),
        sessionNonceDigest=second_session_digest,
        issuedAt=(second_session_at + timedelta(seconds=2)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        expiresAt=(second_session_at + timedelta(seconds=90)).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    with pytest.raises(federation_peer_trust.FederationTrustError) as identity_error:
        registry_b.record_ingress_grant(identity_conflict, recorded_at=second_session_at + timedelta(seconds=2))
    assert identity_error.value.code == "FEDERATION_INGRESS_GRANT_IDENTITY_CONFLICT"

    nonce_replay = _resign(
        signer_b,
        grant,
        grantId="grant-" + ("3" * 32),
        sessionNonceDigest=second_session_digest,
        issuedAt=(second_session_at + timedelta(seconds=2)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        expiresAt=(second_session_at + timedelta(seconds=90)).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    with pytest.raises(federation_peer_trust.FederationTrustError) as nonce_error:
        registry_b.record_ingress_grant(nonce_replay, recorded_at=second_session_at + timedelta(seconds=2))
    assert nonce_error.value.code == "FEDERATION_INGRESS_GRANT_NONCE_REPLAY"

    with pytest.raises(federation_peer_trust.FederationTrustError) as session_replay:
        registry_b.record_ingress_grant(grant, recorded_at=NOW + timedelta(seconds=2))
    assert session_replay.value.code == "FEDERATION_INGRESS_SESSION_REPLAY"

    unknown_peer = _resign(signer_b, grant, sourceFleetId="fleet-c")
    with pytest.raises(federation_peer_trust.FederationTrustError) as unknown_peer_error:
        registry_b.record_ingress_grant(unknown_peer, recorded_at=NOW + timedelta(seconds=2))
    assert unknown_peer_error.value.code == "FEDERATION_PEER_NOT_PINNED"

    registry_b.suspend_peer("fleet-a", actor="operator-b", reason="journal-test", now=NOW + timedelta(seconds=3))
    with pytest.raises(federation_peer_trust.FederationTrustError) as suspended:
        registry_b.record_ingress_grant(grant, recorded_at=NOW + timedelta(seconds=3))
    assert suspended.value.code == "FEDERATION_PEER_NOT_ACTIVE"
    registry_b.revoke_peer("fleet-a", actor="operator-b", reason="journal-test", now=NOW + timedelta(seconds=4))
    with pytest.raises(federation_peer_trust.FederationTrustError) as revoked:
        registry_b.record_ingress_grant(grant, recorded_at=NOW + timedelta(seconds=4))
    assert revoked.value.code == "FEDERATION_PEER_REVOKED"


def test_ingress_write_journal_rejects_direct_bypass_attempts(tmp_path: Path) -> None:
    _, registry_b, _, signer_b, challenge, _ = _fixture(tmp_path)
    grant = _issue(registry_b, signer_b, challenge)
    assert registry_b.get_ingress_grant("grant-" + ("f" * 32)) is None
    assert registry_b.list_ingress_writes(str(grant["grantId"])) == []
    for invalid_id in ("bad", "grant-" + ("g" * 32)):
        with pytest.raises(federation_peer_trust.FederationTrustError) as invalid_grant_id:
            registry_b.get_ingress_grant(invalid_id)
        assert invalid_grant_id.value.code == "FEDERATION_INGRESS_GRANT_ID_INVALID"

    invalid_documents: tuple[tuple[object, str], ...] = (
        ([], "FEDERATION_INGRESS_GRANT_DOCUMENT_INVALID"),
        ({"notJson": object()}, "FEDERATION_INGRESS_GRANT_DOCUMENT_INVALID"),
        ({key: value for key, value in grant.items() if key != "maxBytes"}, "FEDERATION_INGRESS_GRANT_FIELDS_INVALID"),
    )
    for document, expected_code in invalid_documents:
        with pytest.raises(federation_peer_trust.FederationTrustError) as rejected:
            registry_b.reserve_ingress_write(
                document,  # type: ignore[arg-type]
                source_fleet_id="fleet-a",
                write_id="write-bypass",
                object_key=PREFIX + "objects/bypass.age",
                byte_count=1,
                reserved_at=NOW + timedelta(seconds=3),
            )
        assert rejected.value.code == expected_code

    unknown_grant = {**grant, "grantId": "grant-" + ("e" * 32)}
    with pytest.raises(federation_peer_trust.FederationTrustError) as unknown:
        registry_b.reserve_ingress_write(
            unknown_grant,
            source_fleet_id="fleet-a",
            write_id="write-unknown",
            object_key=PREFIX + "objects/unknown.age",
            byte_count=1,
            reserved_at=NOW + timedelta(seconds=3),
        )
    assert unknown.value.code == "FEDERATION_INGRESS_GRANT_NOT_FOUND"

    source_mismatch = {**grant, "sourceFleetId": "fleet-b"}
    with pytest.raises(federation_peer_trust.FederationTrustError) as source_error:
        registry_b.reserve_ingress_write(
            source_mismatch,
            source_fleet_id="fleet-a",
            write_id="write-source-mismatch",
            object_key=PREFIX + "objects/source-mismatch.age",
            byte_count=1,
            reserved_at=NOW + timedelta(seconds=3),
        )
    assert source_error.value.code == "FEDERATION_INGRESS_SOURCE_FLEET_MISMATCH"

    with pytest.raises(federation_peer_trust.FederationTrustError) as from_future:
        registry_b.reserve_ingress_write(
            grant,
            source_fleet_id="fleet-a",
            write_id="write-before-issue",
            object_key=PREFIX + "objects/before-issue.age",
            byte_count=1,
            reserved_at=NOW + timedelta(seconds=1),
        )
    assert from_future.value.code == "FEDERATION_INGRESS_GRANT_FROM_FUTURE"

    registry_b.revoke_peer("fleet-a", actor="operator-b", reason="write-test", now=NOW + timedelta(seconds=4))
    with pytest.raises(federation_peer_trust.FederationTrustError) as revoked:
        registry_b.reserve_ingress_write(
            grant,
            source_fleet_id="fleet-a",
            write_id="write-revoked",
            object_key=PREFIX + "objects/revoked.age",
            byte_count=1,
            reserved_at=NOW + timedelta(seconds=5),
        )
    assert revoked.value.code == "FEDERATION_PEER_REVOKED"

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from deepseek_infra.infra.native_runtime import authority_request as authority
from deepseek_infra.infra.native_runtime import mutation_request as mutation

_RFC8032_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")


def _private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_RFC8032_SEED)


def _public_key() -> str:
    raw = _private_key().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return authority._b64url_encode(raw)


def _auth_context(**overrides: object) -> authority.AuthorityRequestContext:
    public = _public_key()
    values = {
        "now": datetime(2026, 9, 4, 0, 0, 40, tzinfo=timezone.utc),
        "signer_public_key": public,
        "signer_key_id": authority.signer_key_id_for_public_key(public),
        "expected_domain": "action",
        "expected_operation": "install-epoch",
        "expected_runtime": "go",
        "expected_mode": "shadow",
        "expected_fleet_id": "fleet-a",
        "expected_environment": "test",
        "expected_role": "control-plane",
        "current_fencing_token": 4,
        "live_epoch": 3,
        "seen_request_ids": frozenset(),
        "seen_nonces": frozenset(),
        "max_future_skew_seconds": 30,
    }
    values.update(overrides)
    return authority.AuthorityRequestContext(**values)  # type: ignore[arg-type]


def _auth_unsigned() -> dict[str, object]:
    return {
        "schema": authority.AUTHORITY_REQUEST_SCHEMA,
        "schemaVersion": 1,
        "domain": "action",
        "operation": "install-epoch",
        "actionId": "act-1",
        "executionEpoch": 4,
        "fencingToken": 4,
        "revision": 1,
        "requestId": "aa" * 32,
        "nonce": "bb" * 32,
        "issuedAt": "2026-09-04T00:00:30Z",
        "expiresAt": "2026-09-04T00:05:30Z",
        "runtime": "go",
        "mode": "shadow",
        "fleetId": "fleet-a",
        "environment": "test",
        "role": "control-plane",
        "payload": {},
    }


def _mutation_context(**overrides: object) -> mutation.MutationRequestContext:
    public = _public_key()
    values = {
        "now": datetime(2026, 9, 4, 0, 0, 40, tzinfo=timezone.utc),
        "signer_public_key": public,
        "signer_key_id": mutation.signer_key_id_for_public_key(public),
        "expected_domain": "policy",
        "expected_operation": "propose-mutation",
        "expected_runtime": "go",
        "expected_mode": "shadow",
        "expected_fleet_id": "fleet-a",
        "expected_environment": "test",
        "expected_role": "control-plane",
        "current_fencing_token": 4,
        "live_epoch": 3,
        "seen_request_ids": frozenset(),
        "seen_nonces": frozenset(),
        "seen_operation_digests": {},
        "max_future_skew_seconds": 30,
    }
    values.update(overrides)
    return mutation.MutationRequestContext(**values)  # type: ignore[arg-type]


def _mutation_unsigned() -> dict[str, object]:
    return {
        "schema": mutation.MUTATION_REQUEST_SCHEMA,
        "schemaVersion": 1,
        "domain": "policy",
        "operation": "propose-mutation",
        "actionId": "act-1",
        "executionEpoch": 4,
        "fencingToken": 4,
        "revision": 1,
        "requestId": "11" * 32,
        "nonce": "22" * 32,
        "operationId": "33" * 32,
        "issuedAt": "2026-09-04T00:00:30Z",
        "expiresAt": "2026-09-04T00:05:30Z",
        "runtime": "go",
        "mode": "shadow",
        "fleetId": "fleet-a",
        "environment": "test",
        "role": "control-plane",
        "payload": {
            "intent": "shadow-compare",
            "recordId": "rec-1",
            "revision": 1,
            "state": "applied",
        },
    }


# =========================================================================
# Authority Request Extra Coverage
# =========================================================================

def test_authority_request_serialization_and_key_errors() -> None:
    # Invalid signer key id for malformed public key
    with pytest.raises(authority.AuthorityRequestError) as exc:
        authority.signer_key_id_for_public_key("invalid-key-short")
    assert exc.value.code == "AUTHORITY_REQUEST_SIGNER_MISMATCH"

    # Cannot sign if already signed
    with pytest.raises(authority.AuthorityRequestError) as exc:
        authority.sign_authority_request({"signature": "already_signed"}, private_key=_private_key(), public_key=_public_key())
    assert exc.value.code == "AUTHORITY_REQUEST_INVALID"

    # NaN is rejected in canonical encoding
    with pytest.raises(authority.AuthorityRequestError) as exc:
        authority.canonical_authority_request_bytes({"val": float("nan")})
    assert exc.value.code == "AUTHORITY_REQUEST_INVALID"


def test_authority_request_verification_edge_branches() -> None:
    ctx = _auth_context()
    base_signed = authority.sign_authority_request(_auth_unsigned(), private_key=_private_key(), public_key=_public_key())

    def _verify_tampered(
        mutator,
        expected_code: str,
        *,
        recompute_digest: bool = False,
        custom_ctx: authority.AuthorityRequestContext | None = None,
    ) -> None:
        doc = copy.deepcopy(base_signed)
        mutator(doc)
        if recompute_digest:
            doc["digest"] = authority.authority_request_digest(doc)
        raw = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with pytest.raises(authority.AuthorityRequestError) as exc:
            authority.verify_authority_request_document(raw, custom_ctx or ctx)
        assert exc.value.code == expected_code

    # Non-dict JSON
    with pytest.raises(authority.AuthorityRequestError) as exc:
        authority.verify_authority_request_document(b"123", ctx)
    assert exc.value.code == "AUTHORITY_REQUEST_INVALID"

    # Empty raw
    with pytest.raises(authority.AuthorityRequestError) as exc:
        authority.verify_authority_request_document(b"", ctx)
    assert exc.value.code == "AUTHORITY_REQUEST_INVALID"

    # Request too large
    with pytest.raises(authority.AuthorityRequestError) as exc:
        authority.verify_authority_request_document(b" " * (authority.MAX_AUTHORITY_REQUEST_BYTES + 1), ctx)
    assert exc.value.code == "AUTHORITY_REQUEST_TOO_LARGE"

    # Revision invalid
    _verify_tampered(lambda d: d.update({"revision": 0}), "AUTHORITY_REQUEST_INVALID")
    _verify_tampered(lambda d: d.update({"revision": True}), "AUTHORITY_REQUEST_INVALID")

    # Fencing token invalid
    _verify_tampered(lambda d: d.update({"fencingToken": 0}), "AUTHORITY_REQUEST_STALE_FENCING_TOKEN")

    # RequestId / Nonce format invalid
    _verify_tampered(lambda d: d.update({"requestId": "not_hex"}), "AUTHORITY_REQUEST_INVALID")
    _verify_tampered(lambda d: d.update({"nonce": "not_hex"}), "AUTHORITY_REQUEST_INVALID")

    # Payload not empty dict
    _verify_tampered(lambda d: d.update({"payload": {"extra": 1}}), "AUTHORITY_REQUEST_INVALID")

    # PayloadDigest mismatch
    _verify_tampered(lambda d: d.update({"payloadDigest": "sha256:0000"}), "AUTHORITY_REQUEST_PAYLOAD_DIGEST_MISMATCH")

    # Lifetime exceeded or inverted (with recompute_digest so it reaches lifetime checks)
    _verify_tampered(lambda d: d.update({"expiresAt": d["issuedAt"]}), "AUTHORITY_REQUEST_INVALID", recompute_digest=True)
    _verify_tampered(lambda d: d.update({"expiresAt": "2026-09-04T01:00:30Z"}), "AUTHORITY_REQUEST_INVALID", recompute_digest=True)

    # Operation invalid
    _verify_tampered(lambda d: d.update({"operation": "unknown_op"}), "AUTHORITY_REQUEST_OPERATION_INVALID", recompute_digest=True)

    # Signature algorithm invalid
    _verify_tampered(lambda d: d.update({"signatureAlgorithm": "RSA"}), "AUTHORITY_REQUEST_SIGNATURE_INVALID", recompute_digest=True)

    # Signer key id mismatch
    _verify_tampered(lambda d: d.update({"signerKeyId": "ctrl-signer-0000000000000000"}), "AUTHORITY_REQUEST_SIGNER_MISMATCH", recompute_digest=True)

    # Invalid signature base64 length
    _verify_tampered(lambda d: d.update({"signature": "aW52YWxpZA"}), "AUTHORITY_REQUEST_SIGNATURE_INVALID")


def test_authority_request_internal_helpers() -> None:
    # _normalize branches
    assert authority._normalize(["item1", 2, True]) == ["item1", 2, True]
    assert authority._normalize({"key": ["nested"]}) == {"key": ["nested"]}
    with pytest.raises(authority.AuthorityRequestError) as exc:
        authority._normalize({123: "non-str-key"})
    assert exc.value.code == "AUTHORITY_REQUEST_INVALID"
    with pytest.raises(authority.AuthorityRequestError) as exc:
        authority._normalize(object())
    assert exc.value.code == "AUTHORITY_REQUEST_INVALID"

    # _parse_timestamp branches
    with pytest.raises(authority.AuthorityRequestError) as exc:
        authority._parse_timestamp("2026-09-04T00:00:30")  # missing Z
    assert exc.value.code == "AUTHORITY_REQUEST_INVALID"
    with pytest.raises(authority.AuthorityRequestError) as exc:
        authority._parse_timestamp("bad-dateZ")
    assert exc.value.code == "AUTHORITY_REQUEST_INVALID"
    with pytest.raises(authority.AuthorityRequestError) as exc:
        authority._parse_timestamp("2026-09-04T00:00:30.123456Z")  # microseconds rejected
    assert exc.value.code == "AUTHORITY_REQUEST_INVALID"

    # _reject_secrets branches
    authority._reject_secrets(["safe", {"safe_key": "safe_val"}])
    with pytest.raises(authority.AuthorityRequestError) as exc:
        authority._reject_secrets(["safe", {"token": "secret"}])
    assert exc.value.code == "AUTHORITY_REQUEST_SECRET_DETECTED"
    with pytest.raises(authority.AuthorityRequestError) as exc:
        authority._reject_secrets({"nested": "age-secret-key-12345"})
    assert exc.value.code == "AUTHORITY_REQUEST_SECRET_DETECTED"
    with pytest.raises(authority.AuthorityRequestError) as exc:
        authority._reject_secrets({"nested": "-----begin private key-----"})
    assert exc.value.code == "AUTHORITY_REQUEST_SECRET_DETECTED"
    with pytest.raises(authority.AuthorityRequestError) as exc:
        authority._reject_secrets({"a": 1}, depth=129)
    assert exc.value.code == "AUTHORITY_REQUEST_INVALID"

    # _b64url_decode branches
    assert authority._b64url_decode(None, expected_length=32) is None
    assert authority._b64url_decode(123, expected_length=32) is None
    assert authority._b64url_decode("", expected_length=32) is None
    assert authority._b64url_decode("invalid_base64_???", expected_length=32) is None
    assert authority._b64url_decode("AAAA", expected_length=32) is None  # length mismatch


# =========================================================================
# Mutation Request Extra Coverage
# =========================================================================

def test_mutation_request_sign_and_verify_coverage() -> None:
    ctx = _mutation_context()
    base_unsigned = _mutation_unsigned()
    signed = mutation.sign_mutation_request(base_unsigned, private_key=_private_key(), public_key=_public_key())
    raw = json.dumps(signed, sort_keys=True, separators=(",", ":")).encode("utf-8")

    # Normal verify passes
    verified = mutation.verify_mutation_request_document(raw, ctx)
    assert verified["digest"] == signed["digest"]

    # Cannot sign if already signed
    with pytest.raises(mutation.MutationRequestError) as exc:
        mutation.sign_mutation_request({"signature": "already_signed"}, private_key=_private_key(), public_key=_public_key())
    assert exc.value.code == "MUTATION_REQUEST_INVALID"

    def _verify_tampered(
        mutator,
        expected_code: str,
        *,
        recompute_digest: bool = False,
        custom_ctx: mutation.MutationRequestContext | None = None,
    ) -> None:
        doc = copy.deepcopy(signed)
        mutator(doc)
        if recompute_digest:
            doc["digest"] = mutation.mutation_request_digest(doc)
        raw_tampered = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with pytest.raises(mutation.MutationRequestError) as exc:
            mutation.verify_mutation_request_document(raw_tampered, custom_ctx or ctx)
        assert exc.value.code == expected_code

    # Non-dict JSON
    with pytest.raises(mutation.MutationRequestError) as exc:
        mutation.verify_mutation_request_document(b"123", ctx)
    assert exc.value.code == "MUTATION_REQUEST_INVALID"

    # Empty raw
    with pytest.raises(mutation.MutationRequestError) as exc:
        mutation.verify_mutation_request_document(b"", ctx)
    assert exc.value.code == "MUTATION_REQUEST_INVALID"

    # Request too large
    with pytest.raises(mutation.MutationRequestError) as exc:
        mutation.verify_mutation_request_document(b" " * (mutation.MAX_MUTATION_REQUEST_BYTES + 1), ctx)
    assert exc.value.code == "MUTATION_REQUEST_TOO_LARGE"

    # Secret detected in mutation
    _verify_tampered(lambda d: d.update({"secret_field": "pass123"}), "MUTATION_REQUEST_FIELDS_INVALID")
    _verify_tampered(lambda d: d["payload"].update({"token": "secret_tok"}), "MUTATION_REQUEST_SECRET_DETECTED")

    # Revision invalid
    _verify_tampered(lambda d: d.update({"revision": 0}), "MUTATION_REQUEST_INVALID")
    _verify_tampered(lambda d: d.update({"revision": True}), "MUTATION_REQUEST_INVALID")

    # Fencing token invalid
    _verify_tampered(lambda d: d.update({"fencingToken": 0}), "MUTATION_REQUEST_STALE_FENCING_TOKEN")

    # RequestId / Nonce / OperationId invalid format
    _verify_tampered(lambda d: d.update({"requestId": "not_hex"}), "MUTATION_REQUEST_INVALID")
    _verify_tampered(lambda d: d.update({"nonce": "not_hex"}), "MUTATION_REQUEST_INVALID")
    _verify_tampered(lambda d: d.update({"operationId": "not_hex"}), "MUTATION_REQUEST_INVALID")

    # Payload validation: invalid intent, recordId, revision, state
    _verify_tampered(lambda d: d["payload"].update({"intent": "invalid_intent"}), "MUTATION_REQUEST_INVALID")
    _verify_tampered(lambda d: d["payload"].update({"recordId": "bad/id"}), "MUTATION_REQUEST_INVALID")
    _verify_tampered(lambda d: d["payload"].update({"revision": 0}), "MUTATION_REQUEST_INVALID")
    _verify_tampered(lambda d: d["payload"].update({"state": ""}), "MUTATION_REQUEST_INVALID")

    # PayloadDigest mismatch
    _verify_tampered(lambda d: d.update({"payloadDigest": "sha256:0000"}), "MUTATION_REQUEST_PAYLOAD_DIGEST_MISMATCH")

    # Replay conflict with seen operation digest
    conflict_ctx = _mutation_context(seen_operation_digests={signed["operationId"]: "sha256:different_digest_value"})
    _verify_tampered(lambda d: None, "MUTATION_REQUEST_REPLAY_CONFLICT", custom_ctx=conflict_ctx)

    # Lifetime exceeded or inverted (with recompute_digest)
    _verify_tampered(lambda d: d.update({"expiresAt": d["issuedAt"]}), "MUTATION_REQUEST_INVALID", recompute_digest=True)
    _verify_tampered(lambda d: d.update({"expiresAt": "2026-09-04T01:00:30Z"}), "MUTATION_REQUEST_INVALID", recompute_digest=True)

    # Signature algorithm invalid
    _verify_tampered(lambda d: d.update({"signatureAlgorithm": "RSA"}), "MUTATION_REQUEST_SIGNATURE_INVALID", recompute_digest=True)

    # Signer key id mismatch
    _verify_tampered(lambda d: d.update({"signerKeyId": "ctrl-signer-0000000000000000"}), "MUTATION_REQUEST_SIGNER_MISMATCH", recompute_digest=True)

    # Invalid signature base64 length
    _verify_tampered(lambda d: d.update({"signature": "aW52YWxpZA"}), "MUTATION_REQUEST_SIGNATURE_INVALID")

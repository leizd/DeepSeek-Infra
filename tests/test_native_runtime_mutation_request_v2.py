"""Replay the frozen control-mutation-request-v2 (compat v32) corpus.

v1 semantics are asserted to be unchanged in the same file: the v1 vector must
still verify through the v1 entry point, and a v2 document must be refused by the
v1 verifier and vice versa.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from deepseek_infra.infra.native_runtime import mutation_request as mutation
from deepseek_infra.infra.native_runtime.authority_request import (
    _b64url_encode,
    canonical_authority_request_bytes,
    signer_key_id_for_public_key,
)
from scripts.native_runtime_contract import sha256_file, validate_corpus


ROOT = Path(__file__).resolve().parents[1]
V2_CORPUS = ROOT / "compat/native-runtime/v32/control/mutation_request_v2_vector.json"
V1_CORPUS = ROOT / "compat/native-runtime/v17/control/mutation_request_vector.json"
_RFC8032_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")  # pragma: allowlist secret


def _private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_RFC8032_SEED)


def _v2_fixture() -> dict:
    return json.loads(V2_CORPUS.read_text(encoding="utf-8"))


def _v2_context(**overrides: object) -> mutation.MutationRequestContext:
    fixture = _v2_fixture()
    frozen = fixture["context"]
    values: dict[str, object] = {
        "now": datetime.fromisoformat(str(fixture["now"]).replace("Z", "+00:00")),
        "signer_public_key": fixture["signer_public_key"],
        "signer_key_id": fixture["signer_key_id"],
        "expected_domain": frozen["expected_domain"],
        "expected_operation": frozen["expected_operation"],
        "expected_runtime": frozen["expected_runtime"],
        "expected_mode": frozen["expected_mode"],
        "expected_fleet_id": frozen["expected_fleet_id"],
        "expected_environment": frozen["expected_environment"],
        "expected_role": frozen["expected_role"],
        "current_fencing_token": frozen["current_fencing_token"],
        "live_epoch": frozen["live_epoch"],
        "seen_request_ids": frozenset(),
        "seen_nonces": frozenset(),
        "seen_operation_digests": {},
        "max_future_skew_seconds": fixture["max_future_skew_seconds"],
    }
    values.update(overrides)
    return mutation.MutationRequestContext(**values)  # type: ignore[arg-type]


def _v1_context(**overrides: object) -> mutation.MutationRequestContext:
    values: dict[str, object] = {
        "now": datetime(2026, 9, 5, 0, 0, 40, tzinfo=timezone.utc),
        "signer_public_key": _b64url_encode(
            _private_key().public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        ),
        "signer_key_id": signer_key_id_for_public_key(
            _b64url_encode(
                _private_key().public_key().public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                )
            )
        ),
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


def _case_context(case: dict) -> mutation.MutationRequestContext:
    overrides = dict(case.get("context") or {})
    if "seen_request_ids" in overrides:
        overrides["seen_request_ids"] = frozenset(overrides["seen_request_ids"])
    if "seen_nonces" in overrides:
        overrides["seen_nonces"] = frozenset(overrides["seen_nonces"])
    if "now" in overrides:
        overrides["now"] = datetime.fromisoformat(str(overrides["now"]).replace("Z", "+00:00"))
    return _v2_context(**overrides)


def test_v2_apply_request_verifies_and_matches_frozen_digest() -> None:
    fixture = _v2_fixture()
    raw = fixture["canonical_request"].encode("utf-8")
    verified = mutation.verify_mutation_request_v2_document(raw, _v2_context())
    assert verified["digest"] == fixture["request_digest"]
    assert verified["schema"] == "control-mutation-request-v2"
    assert verified["schemaVersion"] == 2
    assert verified["operation"] == "apply-mutation"
    assert verified["domain"] == "policy"
    assert verified["payload"]["intent"] == "apply-mutation"
    assert verified["payload"]["recordPayload"] == {
        "enabled": True,
        "name": "approved policy",
        "priority": 2,
        "tags": ["a", "b"],
    }


def test_v2_fail_closed_vectors_match_frozen_corpus() -> None:
    fixture = _v2_fixture()
    signed = json.loads(fixture["canonical_request"])
    assert len(fixture["cases"]) == 34
    for case in fixture["cases"]:
        document = copy.deepcopy(signed)
        raw: bytes
        if "raw" in case:
            raw = str(case["raw"]).encode("utf-8")
        else:
            for key, value in (case.get("replace") or {}).items():
                document[key] = value
            if case.get("recompute_digest"):
                document["digest"] = mutation.mutation_request_digest(document)
            if case.get("drop"):
                document.pop(case["drop"], None)
            if case.get("add"):
                document.update(case["add"])
            raw = canonical_authority_request_bytes(document)
            if case.get("noncanonical"):
                raw = json.dumps(document, indent=2).encode("utf-8")
        with pytest.raises(mutation.MutationRequestError) as raised:
            mutation.verify_mutation_request_v2_document(raw, _case_context(case))
        assert raised.value.code == case["error"], case["name"]


def test_v2_signature_domain_is_separate_from_v1() -> None:
    """A v2-shaped document signed under the v1 domain must never verify."""
    fixture = _v2_fixture()
    signed = json.loads(fixture["canonical_request"])
    unsigned = {key: value for key, value in signed.items() if key != "signature"}
    resigned = dict(signed)
    resigned["signature"] = _b64url_encode(
        _private_key().sign(mutation.SIGNATURE_DOMAIN + canonical_authority_request_bytes(unsigned))
    )
    with pytest.raises(mutation.MutationRequestError) as raised:
        mutation.verify_mutation_request_v2_document(
            canonical_authority_request_bytes(resigned), _v2_context()
        )
    assert raised.value.code == "MUTATION_REQUEST_SIGNATURE_INVALID"
    # And the v2 signature domain is not the v1 one.
    assert mutation.SIGNATURE_DOMAIN != mutation.SIGNATURE_DOMAIN_V2
    assert mutation.MUTATION_REQUEST_SCHEMA != mutation.MUTATION_REQUEST_V2_SCHEMA


def test_v1_vector_still_verifies_and_the_two_channels_are_disjoint() -> None:
    fixture = json.loads(V1_CORPUS.read_text(encoding="utf-8"))
    v1_raw = fixture["canonical_request"].encode("utf-8")
    verified = mutation.verify_mutation_request_document(v1_raw, _v1_context())
    assert verified["digest"] == fixture["request_digest"]
    assert verified["operation"] == "propose-mutation"

    # v1 refuses a v2 document, and v2 refuses a v1 document.
    v2_raw = _v2_fixture()["canonical_request"].encode("utf-8")
    with pytest.raises(mutation.MutationRequestError) as raised:
        mutation.verify_mutation_request_document(v2_raw, _v1_context())
    assert raised.value.code == "MUTATION_REQUEST_SCHEMA_INVALID"
    with pytest.raises(mutation.MutationRequestError) as raised:
        mutation.verify_mutation_request_v2_document(v1_raw, _v2_context())
    assert raised.value.code == "MUTATION_REQUEST_SCHEMA_INVALID"


def test_v2_same_operation_and_payload_is_idempotent() -> None:
    fixture = _v2_fixture()
    signed = json.loads(fixture["canonical_request"])
    verified = mutation.verify_mutation_request_v2_document(
        fixture["canonical_request"].encode("utf-8"),
        _v2_context(seen_operation_digests={signed["operationId"]: signed["payloadDigest"]}),
    )
    assert verified["digest"] == signed["digest"]


def test_v2_secret_key_rule_is_stricter_than_the_control_record_rule() -> None:
    """Pin the one rule where the two implementations could have diverged.

    The truncation-safe control-record scan exempts keys ending in
    `digest`/`reference`/`ref`/`id`/`type`/`provider`; the mutation-channel rule
    does not. A record body carrying `myTokenDigest` must therefore be refused, or
    an implementation using the looser rule would apply what the oracle rejects.
    """
    fixture = _v2_fixture()
    case = next(c for c in fixture["cases"] if c["name"] == "secret-suffixed-key-in-record-body")
    signed = json.loads(fixture["canonical_request"])
    document = copy.deepcopy(signed)
    for key, value in case["replace"].items():
        document[key] = value
    with pytest.raises(mutation.MutationRequestError) as raised:
        mutation.verify_mutation_request_v2_document(
            canonical_authority_request_bytes(document), _v2_context()
        )
    assert raised.value.code == "MUTATION_REQUEST_SECRET_DETECTED"


def test_v2_signing_round_trips_through_the_oracle() -> None:
    """The oracle can produce what it verifies, so the vector is not hand-written."""
    fixture = _v2_fixture()
    signed = json.loads(fixture["canonical_request"])
    unsigned = {key: value for key, value in signed.items() if key not in {"signature", "digest", "payloadDigest", "signerKeyId", "signatureAlgorithm"}}
    produced = mutation.sign_mutation_request_v2(
        unsigned,
        private_key=_private_key(),
        public_key=fixture["signer_public_key"],
    )
    assert canonical_authority_request_bytes(produced) == canonical_authority_request_bytes(signed)


def test_v32_corpus_digest_is_pinned() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v32/manifest.json")
    item = next(entry for entry in manifest["corpora"] if entry["id"] == "control-mutation-request-v2-semantics-v32")
    assert sha256_file(ROOT / item["path"]) == item["sha256"]
    assert item["sensitivity"] == "public"
    text = V2_CORPUS.read_text(encoding="utf-8")
    assert _RFC8032_SEED.hex() not in text.casefold()
    assert "age-secret-key-" not in text.casefold()
    assert "-----begin" not in text.casefold()
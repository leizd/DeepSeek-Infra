from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from deepseek_infra.infra.native_runtime import authority_request as authority
from scripts.native_runtime_contract import sha256_file, validate_corpus


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "compat/native-runtime/v7/control/authority_request_vector.json"
# RFC 8032 Ed25519 test vector 1 seed. Public test key only; not a production secret.
_RFC8032_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")  # pragma: allowlist secret


def _private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_RFC8032_SEED)


def _public_key() -> str:
    raw = _private_key().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return authority._b64url_encode(raw)


def _context(**overrides: object) -> authority.AuthorityRequestContext:
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


def _unsigned() -> dict[str, object]:
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


def _signed() -> dict[str, object]:
    return authority.sign_authority_request(_unsigned(), private_key=_private_key(), public_key=_public_key())


def test_valid_authority_request_verifies_and_matches_frozen_canonical_bytes() -> None:
    signed = _signed()
    raw = authority.canonical_authority_request_bytes(signed)
    verified = authority.verify_authority_request_document(raw, _context())
    assert verified["digest"] == signed["digest"]
    assert verified["mode"] == "shadow"
    fixture = json.loads(CORPUS.read_text(encoding="utf-8"))
    assert fixture["canonical_request"] == raw.decode("utf-8")
    assert fixture["request_digest"] == signed["digest"]
    assert fixture["signer_public_key"] == _public_key()


def test_authority_request_fail_closed_vectors_match_frozen_corpus() -> None:
    fixture = json.loads(CORPUS.read_text(encoding="utf-8"))
    signed = json.loads(fixture["canonical_request"])
    for case in fixture["cases"]:
        document = copy.deepcopy(signed)
        context_overrides = dict(case.get("context") or {})
        if "seen_request_ids" in context_overrides:
            context_overrides["seen_request_ids"] = frozenset(context_overrides["seen_request_ids"])
        if "seen_nonces" in context_overrides:
            context_overrides["seen_nonces"] = frozenset(context_overrides["seen_nonces"])
        if "now" in context_overrides:
            context_overrides["now"] = datetime.fromisoformat(str(context_overrides["now"]).replace("Z", "+00:00"))
        raw: bytes
        if "raw" in case:
            raw = str(case["raw"]).encode("utf-8")
        else:
            for key, value in (case.get("replace") or {}).items():
                document[key] = value
            if case.get("recompute_digest"):
                document["digest"] = authority.authority_request_digest(document)
            if case.get("drop"):
                document.pop(case["drop"], None)
            if case.get("add"):
                document.update(case["add"])
            raw = authority.canonical_authority_request_bytes(document)
            if case.get("noncanonical"):
                raw = json.dumps(document, indent=2).encode("utf-8")
        with pytest.raises(authority.AuthorityRequestError) as raised:
            authority.verify_authority_request_document(raw, _context(**context_overrides))
        assert raised.value.code == case["error"], case["name"]


def test_v7_corpus_digest_is_pinned() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v7/manifest.json")
    item = next(entry for entry in manifest["corpora"] if entry["id"] == "control-authority-request-semantics-v7")
    assert sha256_file(ROOT / item["path"]) == item["sha256"]
    assert item["sensitivity"] == "public"
    text = CORPUS.read_text(encoding="utf-8")
    assert _RFC8032_SEED.hex() not in text.casefold()
    assert "age-secret-key-" not in text.casefold()

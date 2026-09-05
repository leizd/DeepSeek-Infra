from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from deepseek_infra.infra.native_runtime import mutation_request as mutation
from deepseek_infra.infra.native_runtime.authority_request import canonical_authority_request_bytes
from scripts.native_runtime_contract import sha256_file, validate_corpus


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "compat/native-runtime/v17/control/mutation_request_vector.json"
_RFC8032_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")  # pragma: allowlist secret


def _private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_RFC8032_SEED)


def _encode(raw: bytes) -> str:
    from deepseek_infra.infra.native_runtime.authority_request import _b64url_encode

    return _b64url_encode(raw)


def _context(**overrides: object) -> mutation.MutationRequestContext:
    from deepseek_infra.infra.native_runtime.authority_request import signer_key_id_for_public_key

    public = _encode(
        _private_key().public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )
    values: dict[str, object] = {
        "now": datetime(2026, 9, 5, 0, 0, 40, tzinfo=timezone.utc),
        "signer_public_key": public,
        "signer_key_id": signer_key_id_for_public_key(public),
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


def test_valid_mutation_request_verifies_and_matches_frozen_canonical_bytes() -> None:
    fixture = json.loads(CORPUS.read_text(encoding="utf-8"))
    raw = fixture["canonical_request"].encode("utf-8")
    verified = mutation.verify_mutation_request_document(raw, _context())
    assert verified["digest"] == fixture["request_digest"]
    assert verified["domain"] == "policy"
    assert verified["operation"] == "propose-mutation"
    assert verified["mode"] == "shadow"
    assert verified["payload"]["intent"] == "shadow-compare"


def test_mutation_request_fail_closed_vectors_match_frozen_corpus() -> None:
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
                document["digest"] = mutation.mutation_request_digest(document)
            if case.get("drop"):
                document.pop(case["drop"], None)
            if case.get("add"):
                document.update(case["add"])
            raw = canonical_authority_request_bytes(document)
            if case.get("noncanonical"):
                raw = json.dumps(document, indent=2).encode("utf-8")
        with pytest.raises(mutation.MutationRequestError) as raised:
            mutation.verify_mutation_request_document(raw, _context(**context_overrides))
        assert raised.value.code == case["error"], case["name"]


def test_same_operation_id_same_payload_is_idempotent_and_does_not_mutate() -> None:
    fixture = json.loads(CORPUS.read_text(encoding="utf-8"))
    raw = fixture["canonical_request"].encode("utf-8")
    signed = json.loads(fixture["canonical_request"])
    verified = mutation.verify_mutation_request_document(
        raw,
        _context(seen_operation_digests={signed["operationId"]: signed["payloadDigest"]}),
    )
    assert verified["digest"] == signed["digest"]


def test_v17_corpus_digest_is_pinned() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v17/manifest.json")
    item = next(entry for entry in manifest["corpora"] if entry["id"] == "control-mutation-request-semantics-v17")
    assert sha256_file(ROOT / item["path"]) == item["sha256"]
    assert item["sensitivity"] == "public"
    text = CORPUS.read_text(encoding="utf-8")
    assert _RFC8032_SEED.hex() not in text.casefold()
    assert "age-secret-key-" not in text.casefold()

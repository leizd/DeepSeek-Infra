"""Fail-closed control-mutation-request-v1 verifier.

This module is the Python oracle for the frozen native-runtime v17 corpus.
It verifies canonical JSON mutation proposals. It does not apply production
mutations, install a live epoch, or hold Federation root private keys.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

from deepseek_infra.infra.native_runtime.authority_request import (
    ALLOWED_DOMAINS,
    SIGNATURE_ALGORITHM,
    AuthorityRequestError,
    _CONTROL_ID_PATTERN,
    _FLEET_ID_PATTERN,
    _HEX64_PATTERN,
    _SIGNER_KEY_ID_PATTERN,
    _b64url_decode,
    _b64url_encode,
    _decode_single_json,
    _parse_timestamp,
    _reject_secrets,
    canonical_authority_request_bytes,
    signer_key_id_for_public_key,
)

MUTATION_REQUEST_SCHEMA = "control-mutation-request-v1"
MUTATION_REQUEST_SCHEMA_VERSION = 1
SIGNATURE_DOMAIN = b"deepseek-infra:control-mutation-request-v1\x00"
MAX_MUTATION_REQUEST_BYTES = 16 * 1024
MAX_LIFETIME_SECONDS = 300
ALLOWED_OPERATIONS = frozenset({"propose-mutation"})
ALLOWED_INTENTS = frozenset({"shadow-compare"})
PAYLOAD_FIELDS = ("intent", "recordId", "revision", "state")
MUTATION_REQUEST_FIELDS = (
    "actionId",
    "digest",
    "domain",
    "environment",
    "executionEpoch",
    "expiresAt",
    "fencingToken",
    "fleetId",
    "issuedAt",
    "mode",
    "nonce",
    "operation",
    "operationId",
    "payload",
    "payloadDigest",
    "requestId",
    "revision",
    "role",
    "runtime",
    "schema",
    "schemaVersion",
    "signature",
    "signatureAlgorithm",
    "signerKeyId",
)


class MutationRequestError(AuthorityRequestError):
    pass


@dataclass(frozen=True)
class MutationRequestContext:
    now: datetime
    signer_public_key: str
    signer_key_id: str
    expected_domain: str
    expected_operation: str
    expected_runtime: str
    expected_mode: str
    expected_fleet_id: str
    expected_environment: str
    expected_role: str
    current_fencing_token: int
    live_epoch: int
    seen_request_ids: frozenset[str]
    seen_nonces: frozenset[str]
    seen_operation_digests: Mapping[str, str]
    max_future_skew_seconds: int = 30


def mutation_request_digest(value: Mapping[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key not in {"signature", "digest"}}
    return "sha256:" + hashlib.sha256(canonical_authority_request_bytes(unsigned)).hexdigest()


def sign_mutation_request(
    unsigned: Mapping[str, Any],
    *,
    private_key: Ed25519PrivateKey,
    public_key: str,
) -> dict[str, Any]:
    payload = dict(unsigned)
    if "signature" in payload:
        raise MutationRequestError("MUTATION_REQUEST_INVALID")
    payload["signerKeyId"] = signer_key_id_for_public_key(public_key)
    payload["signatureAlgorithm"] = SIGNATURE_ALGORITHM
    payload["payloadDigest"] = _payload_digest(payload.get("payload"))
    payload["digest"] = mutation_request_digest(payload)
    message = SIGNATURE_DOMAIN + canonical_authority_request_bytes(payload)
    signed = dict(payload)
    signed["signature"] = _b64url_encode(private_key.sign(message))
    return signed


def verify_mutation_request_document(raw: bytes, context: MutationRequestContext) -> dict[str, Any]:
    if not raw:
        raise MutationRequestError("MUTATION_REQUEST_INVALID")
    if len(raw) > MAX_MUTATION_REQUEST_BYTES:
        raise MutationRequestError("MUTATION_REQUEST_TOO_LARGE")
    try:
        document = _decode_single_json(raw)
        if type(document) is not dict:
            raise MutationRequestError("MUTATION_REQUEST_INVALID")
        canonical = canonical_authority_request_bytes(document)
    except AuthorityRequestError as exc:
        raise MutationRequestError("MUTATION_REQUEST_INVALID") from exc
    if canonical != raw:
        raise MutationRequestError("MUTATION_REQUEST_CANONICAL_MISMATCH")
    if tuple(sorted(document)) != MUTATION_REQUEST_FIELDS:
        raise MutationRequestError("MUTATION_REQUEST_FIELDS_INVALID")
    try:
        _reject_secrets(document)
    except AuthorityRequestError as exc:
        if exc.code == "AUTHORITY_REQUEST_SECRET_DETECTED":
            raise MutationRequestError("MUTATION_REQUEST_SECRET_DETECTED") from exc
        raise MutationRequestError("MUTATION_REQUEST_INVALID") from exc
    _verify_envelope(document, context)
    _verify_signature(document, context)
    return document


def _payload_digest(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_authority_request_bytes(payload)).hexdigest()


def _verify_envelope(document: Mapping[str, Any], context: MutationRequestContext) -> None:
    if document["schema"] != MUTATION_REQUEST_SCHEMA or document["schemaVersion"] != MUTATION_REQUEST_SCHEMA_VERSION:
        raise MutationRequestError("MUTATION_REQUEST_SCHEMA_INVALID")
    if document["operation"] not in ALLOWED_OPERATIONS or document["operation"] != context.expected_operation:
        raise MutationRequestError("MUTATION_REQUEST_OPERATION_INVALID")
    if document["domain"] not in ALLOWED_DOMAINS or document["domain"] != context.expected_domain:
        raise MutationRequestError("MUTATION_REQUEST_DOMAIN_MISMATCH")
    if document["runtime"] != "go" or document["runtime"] != context.expected_runtime:
        raise MutationRequestError("MUTATION_REQUEST_RUNTIME_MISMATCH")
    if document["mode"] != "shadow" or document["mode"] != context.expected_mode:
        raise MutationRequestError("MUTATION_REQUEST_MODE_MISMATCH")
    if not _FLEET_ID_PATTERN.fullmatch(str(document["fleetId"])) or document["fleetId"] != context.expected_fleet_id:
        raise MutationRequestError("MUTATION_REQUEST_FLEET_MISMATCH")
    if type(document["environment"]) is not str or not document["environment"] or document["environment"] != context.expected_environment:
        raise MutationRequestError("MUTATION_REQUEST_ENVIRONMENT_MISMATCH")
    if document["role"] != "control-plane" or document["role"] != context.expected_role:
        raise MutationRequestError("MUTATION_REQUEST_ROLE_MISMATCH")
    if type(document["actionId"]) is not str or not _CONTROL_ID_PATTERN.fullmatch(document["actionId"]):
        raise MutationRequestError("EMPTY_ACTION_ID")
    if type(document["executionEpoch"]) is not int or isinstance(document["executionEpoch"], bool) or document["executionEpoch"] < 1:
        raise MutationRequestError("ZERO_EXECUTION_EPOCH")
    if type(document["revision"]) is not int or isinstance(document["revision"], bool) or document["revision"] < 1:
        raise MutationRequestError("MUTATION_REQUEST_INVALID")
    if type(document["fencingToken"]) is not int or isinstance(document["fencingToken"], bool) or document["fencingToken"] < 1:
        raise MutationRequestError("MUTATION_REQUEST_STALE_FENCING_TOKEN")
    if document["fencingToken"] != context.current_fencing_token:
        raise MutationRequestError("MUTATION_REQUEST_STALE_FENCING_TOKEN")
    if document["executionEpoch"] <= context.live_epoch:
        raise MutationRequestError("STALE_EXECUTION_EPOCH")
    if type(document["requestId"]) is not str or not _HEX64_PATTERN.fullmatch(document["requestId"]):
        raise MutationRequestError("MUTATION_REQUEST_INVALID")
    if type(document["nonce"]) is not str or not _HEX64_PATTERN.fullmatch(document["nonce"]):
        raise MutationRequestError("MUTATION_REQUEST_INVALID")
    if type(document["operationId"]) is not str or not _HEX64_PATTERN.fullmatch(document["operationId"]):
        raise MutationRequestError("MUTATION_REQUEST_INVALID")
    if document["requestId"] in context.seen_request_ids:
        raise MutationRequestError("MUTATION_REQUEST_REPLAY")
    if document["nonce"] in context.seen_nonces:
        raise MutationRequestError("MUTATION_REQUEST_NONCE_REUSE")
    payload = document["payload"]
    if type(payload) is not dict or tuple(sorted(payload)) != PAYLOAD_FIELDS:
        raise MutationRequestError("MUTATION_REQUEST_INVALID")
    if payload["intent"] not in ALLOWED_INTENTS:
        raise MutationRequestError("MUTATION_REQUEST_INVALID")
    if type(payload["recordId"]) is not str or not _CONTROL_ID_PATTERN.fullmatch(payload["recordId"]):
        raise MutationRequestError("MUTATION_REQUEST_INVALID")
    if type(payload["revision"]) is not int or isinstance(payload["revision"], bool) or payload["revision"] < 1:
        raise MutationRequestError("MUTATION_REQUEST_INVALID")
    if type(payload["state"]) is not str or not payload["state"]:
        raise MutationRequestError("MUTATION_REQUEST_INVALID")
    expected_payload_digest = _payload_digest(payload)
    if document["payloadDigest"] != expected_payload_digest:
        raise MutationRequestError("MUTATION_REQUEST_PAYLOAD_DIGEST_MISMATCH")
    seen_digest = context.seen_operation_digests.get(document["operationId"])
    if seen_digest is not None and seen_digest != expected_payload_digest:
        raise MutationRequestError("MUTATION_REQUEST_REPLAY_CONFLICT")
    if document["digest"] != mutation_request_digest(document):
        raise MutationRequestError("MUTATION_REQUEST_DIGEST_MISMATCH")
    try:
        issued_at = _parse_timestamp(document["issuedAt"])
        expires_at = _parse_timestamp(document["expiresAt"])
    except AuthorityRequestError as exc:
        raise MutationRequestError("MUTATION_REQUEST_INVALID") from exc
    now = context.now.astimezone(timezone.utc).replace(microsecond=0)
    if expires_at <= issued_at or (expires_at - issued_at).total_seconds() > MAX_LIFETIME_SECONDS:
        raise MutationRequestError("MUTATION_REQUEST_INVALID")
    if expires_at <= now:
        raise MutationRequestError("MUTATION_REQUEST_EXPIRED")
    skew = max(0, int(context.max_future_skew_seconds))
    if (issued_at - now).total_seconds() > skew:
        raise MutationRequestError("MUTATION_REQUEST_FUTURE_SKEW")


def _verify_signature(document: Mapping[str, Any], context: MutationRequestContext) -> None:
    if document["signatureAlgorithm"] != SIGNATURE_ALGORITHM:
        raise MutationRequestError("MUTATION_REQUEST_SIGNATURE_INVALID")
    if document["signerKeyId"] != context.signer_key_id or not _SIGNER_KEY_ID_PATTERN.fullmatch(document["signerKeyId"]):
        raise MutationRequestError("MUTATION_REQUEST_SIGNER_MISMATCH")
    signature = _b64url_decode(document["signature"], expected_length=64)
    public = _b64url_decode(context.signer_public_key, expected_length=32)
    if signature is None or public is None:
        raise MutationRequestError("MUTATION_REQUEST_SIGNATURE_INVALID")
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    message = SIGNATURE_DOMAIN + canonical_authority_request_bytes(unsigned)
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, message)
    except InvalidSignature as exc:
        raise MutationRequestError("MUTATION_REQUEST_SIGNATURE_INVALID") from exc

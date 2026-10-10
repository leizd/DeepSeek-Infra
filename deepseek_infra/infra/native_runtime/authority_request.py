"""Fail-closed Go-to-Rust control-authority-request-v1 verifier.

This module is the Python oracle for the frozen native-runtime v7 corpus.
It verifies canonical JSON authority requests. It does not install a live
epoch, authorize production mutation, or hold Federation root private keys.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

AUTHORITY_REQUEST_SCHEMA = "control-authority-request-v1"
AUTHORITY_REQUEST_SCHEMA_VERSION = 1
SIGNATURE_ALGORITHM = "Ed25519"
SIGNATURE_DOMAIN = b"deepseek-infra:control-authority-request-v1\x00"
MAX_AUTHORITY_REQUEST_BYTES = 16 * 1024
MAX_LIFETIME_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30
ALLOWED_OPERATIONS = frozenset({"install-epoch"})
ALLOWED_DOMAINS = frozenset(
    {
        "policy",
        "target",
        "scheduler_run",
        "action",
        "risk",
        "wave",
        "peer",
        "grant",
        "session",
        "transfer",
        "forecast",
        "agent_run",
    }
)
AUTHORITY_REQUEST_FIELDS = (
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
_FLEET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TYPED_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTROL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SIGNER_KEY_ID_PATTERN = re.compile(r"^ctrl-signer-[0-9a-f]{16}$")
_SECRET_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "privatekey",
    "ageidentity",
    "apikey",
    "accesskey",
    "secretkey",
    "token",
    "credential",
    "oauth",
    "bearer",
    "secret",
)
_SECRET_VALUE_MARKERS = ("age-secret-key-", "-----begin")


class AuthorityRequestError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AuthorityRequestContext:
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
    max_future_skew_seconds: int = MAX_FUTURE_SKEW_SECONDS


def canonical_authority_request_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            _normalize(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise AuthorityRequestError("AUTHORITY_REQUEST_INVALID") from exc
    return rendered.encode("utf-8")


def authority_request_digest(value: Mapping[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key not in {"signature", "digest"}}
    return "sha256:" + hashlib.sha256(canonical_authority_request_bytes(unsigned)).hexdigest()


def signer_key_id_for_public_key(public_key: str) -> str:
    raw = _b64url_decode(public_key, expected_length=32)
    if raw is None:
        raise AuthorityRequestError("AUTHORITY_REQUEST_SIGNER_MISMATCH")
    return "ctrl-signer-" + hashlib.sha256(raw).hexdigest()[:16]


def sign_authority_request(
    unsigned: Mapping[str, Any],
    *,
    private_key: Ed25519PrivateKey,
    public_key: str,
) -> dict[str, Any]:
    payload = dict(unsigned)
    if "signature" in payload:
        raise AuthorityRequestError("AUTHORITY_REQUEST_INVALID")
    payload["signerKeyId"] = signer_key_id_for_public_key(public_key)
    payload["signatureAlgorithm"] = SIGNATURE_ALGORITHM
    payload["payloadDigest"] = _payload_digest(payload.get("payload"))
    payload["digest"] = authority_request_digest(payload)
    message = SIGNATURE_DOMAIN + canonical_authority_request_bytes(payload)
    signed = dict(payload)
    signed["signature"] = _b64url_encode(private_key.sign(message))
    return signed


def verify_authority_request_document(raw: bytes, context: AuthorityRequestContext) -> dict[str, Any]:
    if not raw:
        raise AuthorityRequestError("AUTHORITY_REQUEST_INVALID")
    if len(raw) > MAX_AUTHORITY_REQUEST_BYTES:
        raise AuthorityRequestError("AUTHORITY_REQUEST_TOO_LARGE")
    document = _decode_single_json(raw)
    if type(document) is not dict:
        raise AuthorityRequestError("AUTHORITY_REQUEST_INVALID")
    canonical = canonical_authority_request_bytes(document)
    if canonical != raw:
        raise AuthorityRequestError("AUTHORITY_REQUEST_CANONICAL_MISMATCH")
    if tuple(sorted(document)) != AUTHORITY_REQUEST_FIELDS:
        raise AuthorityRequestError("AUTHORITY_REQUEST_FIELDS_INVALID")
    _reject_secrets(document)
    _verify_envelope(document, context)
    _verify_signature(document, context)
    return document


def _verify_envelope(document: Mapping[str, Any], context: AuthorityRequestContext) -> None:
    if document["schema"] != AUTHORITY_REQUEST_SCHEMA or document["schemaVersion"] != AUTHORITY_REQUEST_SCHEMA_VERSION:
        raise AuthorityRequestError("AUTHORITY_REQUEST_SCHEMA_INVALID")
    if document["operation"] not in ALLOWED_OPERATIONS or document["operation"] != context.expected_operation:
        raise AuthorityRequestError("AUTHORITY_REQUEST_OPERATION_INVALID")
    if document["domain"] not in ALLOWED_DOMAINS or document["domain"] != context.expected_domain:
        raise AuthorityRequestError("AUTHORITY_REQUEST_DOMAIN_MISMATCH")
    if document["runtime"] != "go" or document["runtime"] != context.expected_runtime:
        raise AuthorityRequestError("AUTHORITY_REQUEST_RUNTIME_MISMATCH")
    if document["mode"] != "shadow" or document["mode"] != context.expected_mode:
        raise AuthorityRequestError("AUTHORITY_REQUEST_MODE_MISMATCH")
    if not _FLEET_ID_PATTERN.fullmatch(str(document["fleetId"])) or document["fleetId"] != context.expected_fleet_id:
        raise AuthorityRequestError("AUTHORITY_REQUEST_FLEET_MISMATCH")
    if type(document["environment"]) is not str or not document["environment"] or document["environment"] != context.expected_environment:
        raise AuthorityRequestError("AUTHORITY_REQUEST_ENVIRONMENT_MISMATCH")
    if document["role"] != "control-plane" or document["role"] != context.expected_role:
        raise AuthorityRequestError("AUTHORITY_REQUEST_ROLE_MISMATCH")
    if type(document["actionId"]) is not str or not _CONTROL_ID_PATTERN.fullmatch(document["actionId"]):
        raise AuthorityRequestError("EMPTY_ACTION_ID")
    if type(document["executionEpoch"]) is not int or isinstance(document["executionEpoch"], bool) or document["executionEpoch"] < 1:
        raise AuthorityRequestError("ZERO_EXECUTION_EPOCH")
    if type(document["revision"]) is not int or isinstance(document["revision"], bool) or document["revision"] < 1:
        raise AuthorityRequestError("AUTHORITY_REQUEST_INVALID")
    if type(document["fencingToken"]) is not int or isinstance(document["fencingToken"], bool) or document["fencingToken"] < 1:
        raise AuthorityRequestError("AUTHORITY_REQUEST_STALE_FENCING_TOKEN")
    if document["fencingToken"] != context.current_fencing_token:
        raise AuthorityRequestError("AUTHORITY_REQUEST_STALE_FENCING_TOKEN")
    if document["executionEpoch"] <= context.live_epoch:
        raise AuthorityRequestError("STALE_EXECUTION_EPOCH")
    if type(document["requestId"]) is not str or not _HEX64_PATTERN.fullmatch(document["requestId"]):
        raise AuthorityRequestError("AUTHORITY_REQUEST_INVALID")
    if type(document["nonce"]) is not str or not _HEX64_PATTERN.fullmatch(document["nonce"]):
        raise AuthorityRequestError("AUTHORITY_REQUEST_INVALID")
    if document["requestId"] in context.seen_request_ids:
        raise AuthorityRequestError("AUTHORITY_REQUEST_REPLAY")
    if document["nonce"] in context.seen_nonces:
        raise AuthorityRequestError("AUTHORITY_REQUEST_NONCE_REUSE")
    if document["payload"] != {}:
        raise AuthorityRequestError("AUTHORITY_REQUEST_INVALID")
    if document["payloadDigest"] != _payload_digest(document["payload"]):
        raise AuthorityRequestError("AUTHORITY_REQUEST_PAYLOAD_DIGEST_MISMATCH")
    if document["digest"] != authority_request_digest(document):
        raise AuthorityRequestError("AUTHORITY_REQUEST_DIGEST_MISMATCH")
    issued_at = _parse_timestamp(document["issuedAt"])
    expires_at = _parse_timestamp(document["expiresAt"])
    now = context.now.astimezone(timezone.utc).replace(microsecond=0)
    if expires_at <= issued_at:
        raise AuthorityRequestError("AUTHORITY_REQUEST_INVALID")
    if (expires_at - issued_at).total_seconds() > MAX_LIFETIME_SECONDS:
        raise AuthorityRequestError("AUTHORITY_REQUEST_INVALID")
    if expires_at <= now:
        raise AuthorityRequestError("AUTHORITY_REQUEST_EXPIRED")
    skew = max(0, int(context.max_future_skew_seconds))
    if (issued_at - now).total_seconds() > skew:
        raise AuthorityRequestError("AUTHORITY_REQUEST_FUTURE_SKEW")


def _verify_signature(document: Mapping[str, Any], context: AuthorityRequestContext) -> None:
    if document["signatureAlgorithm"] != SIGNATURE_ALGORITHM:
        raise AuthorityRequestError("AUTHORITY_REQUEST_SIGNATURE_INVALID")
    if document["signerKeyId"] != context.signer_key_id or not _SIGNER_KEY_ID_PATTERN.fullmatch(document["signerKeyId"]):
        raise AuthorityRequestError("AUTHORITY_REQUEST_SIGNER_MISMATCH")
    signature = _b64url_decode(document["signature"], expected_length=64)
    public = _b64url_decode(context.signer_public_key, expected_length=32)
    if signature is None or public is None:
        raise AuthorityRequestError("AUTHORITY_REQUEST_SIGNATURE_INVALID")
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    message = SIGNATURE_DOMAIN + canonical_authority_request_bytes(unsigned)
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, message)
    except InvalidSignature as exc:
        raise AuthorityRequestError("AUTHORITY_REQUEST_SIGNATURE_INVALID") from exc


def _payload_digest(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_authority_request_bytes(payload)).hexdigest()


def _decode_single_json(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
        value, index = json.JSONDecoder().raw_decode(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthorityRequestError("AUTHORITY_REQUEST_INVALID") from exc
    if index != len(text):
        raise AuthorityRequestError("AUTHORITY_REQUEST_INVALID")
    return value


def _normalize(value: Any) -> Any:
    if value is None or type(value) in {str, bool, int}:
        if type(value) is str:
            value.encode("utf-8")
        return value
    if type(value) is list:
        return [_normalize(item) for item in value]
    if type(value) is dict:
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise AuthorityRequestError("AUTHORITY_REQUEST_INVALID")
            normalized[key] = _normalize(item)
        return normalized
    raise AuthorityRequestError("AUTHORITY_REQUEST_INVALID")


def _parse_timestamp(value: Any) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise AuthorityRequestError("AUTHORITY_REQUEST_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorityRequestError("AUTHORITY_REQUEST_INVALID") from exc
    if parsed.tzinfo is None or parsed.microsecond != 0:
        raise AuthorityRequestError("AUTHORITY_REQUEST_INVALID")
    return parsed.astimezone(timezone.utc)


def _reject_secrets(value: Any, depth: int = 0) -> None:
    if depth > 128:
        raise AuthorityRequestError("AUTHORITY_REQUEST_INVALID")
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if normalized not in {"fencingtoken", "signature", "signaturealgorithm", "signerkeyid"} and any(
                fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS
            ):
                raise AuthorityRequestError("AUTHORITY_REQUEST_SECRET_DETECTED")
            _reject_secrets(nested, depth + 1)
        return
    if isinstance(value, list):
        for nested in value:
            _reject_secrets(nested, depth + 1)
        return
    if isinstance(value, str):
        lower = value.casefold()
        if "age-secret-key-" in lower or ("-----begin" in lower and "private key" in lower):
            raise AuthorityRequestError("AUTHORITY_REQUEST_SECRET_DETECTED")


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: Any, *, expected_length: int) -> bytes | None:
    if type(value) is not str or not value:
        return None
    try:
        raw = base64.b64decode(value + ("=" * (-len(value) % 4)), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(raw) != expected_length:
        return None
    return raw

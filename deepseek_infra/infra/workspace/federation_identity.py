"""Dedicated Ed25519 Fleet identity and root-certified online signers.

Federation signing keys are intentionally separate from randomized-Age
recipient identities and the secretless Control Authority. Root private keys
are expected to live in operator-controlled offline custody; online signer
bundles are independently encrypted and short-lived.

Ed25519, Argon2id, and AES-GCM follow the PyCA cryptography APIs:
https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ed25519/
https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/#argon2id
https://cryptography.io/en/latest/hazmat/primitives/aead/#cryptography.hazmat.primitives.ciphers.aead.AESGCM
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

FLEET_IDENTITY_SCHEMA = "fleet-identity-v1"
ROOT_PRIVATE_BUNDLE_SCHEMA = "fleet-federation-root-private-v1"
ONLINE_SIGNER_CERTIFICATE_SCHEMA = "federation-online-signer-certificate-v1"
ONLINE_SIGNER_PRIVATE_BUNDLE_SCHEMA = "fleet-federation-online-signer-private-v1"
PRIVATE_KEY_ENVELOPE_SCHEMA = "federation-private-key-envelope-v1"  # pragma: allowlist secret
SIGNATURE_ALGORITHM = "Ed25519"

PURPOSE_READINESS_ATTESTATION = "READINESS_ATTESTATION"
PURPOSE_SESSION_AUTHENTICATION = "SESSION_AUTHENTICATION"
PURPOSE_INGRESS_GRANT = "INGRESS_GRANT"
PURPOSE_REPLICA_ATTESTATION = "REPLICA_ATTESTATION"
PURPOSE_DR_ATTESTATION = "DR_ATTESTATION"
PURPOSE_EVIDENCE = "EVIDENCE"
ONLINE_SIGNER_PURPOSES = frozenset(
    {
        PURPOSE_READINESS_ATTESTATION,
        PURPOSE_SESSION_AUTHENTICATION,
        PURPOSE_INGRESS_GRANT,
        PURPOSE_REPLICA_ATTESTATION,
        PURPOSE_DR_ATTESTATION,
        PURPOSE_EVIDENCE,
    }
)
DEFAULT_ONLINE_SIGNER_PURPOSES = tuple(sorted(ONLINE_SIGNER_PURPOSES))

_FLEET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_ROOT_KEY_PREFIX = "fed-root-"
_SIGNER_KEY_PREFIX = "fed-signer-"
_MIN_PASSPHRASE_BYTES = 16
_MAX_PASSPHRASE_BYTES = 1024
_MAX_BUNDLE_BYTES = 256 * 1024
_ARGON2_MEMORY_KIB = 64 * 1024
_ARGON2_ITERATIONS = 3
_ARGON2_LANES = 4
_ARGON2_SALT_BYTES = 16
_DERIVED_KEY_BYTES = 32
_AES_GCM_NONCE_BYTES = 12
_PRIVATE_KEY_KDF_ALGORITHM = "Argon2id"  # pragma: allowlist secret
_PRIVATE_KEY_AEAD_ALGORITHM = "AES-256-GCM"  # pragma: allowlist secret
_CERTIFICATE_DOMAIN = b"deepseek-infra:federation-online-signer-certificate-v1\x00"
_DOCUMENT_DOMAIN_PREFIX = b"deepseek-infra:federation-document\x00"
_PRIVATE_KEY_ENVELOPE_DOMAIN = b"deepseek-infra:federation-private-key-envelope-v1\x00"


class FederationIdentityError(RuntimeError):
    """Fail-closed Fleet identity error with a stable machine-readable code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = str(code)
        super().__init__(message or self.code)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise FederationIdentityError("FEDERATION_TIMESTAMP_INVALID")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _normalize_issued_purposes(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)) or not value:
        raise FederationIdentityError("FEDERATION_SIGNER_PURPOSE_INVALID")
    if any(type(purpose) is not str or purpose not in ONLINE_SIGNER_PURPOSES for purpose in value):
        raise FederationIdentityError("FEDERATION_SIGNER_PURPOSE_INVALID")
    return sorted(set(value))


def _certificate_purpose_errors(certificate: dict[str, Any], required_purpose: str | None) -> list[str]:
    raw_purposes = certificate.get("purposes")
    normalized_purposes = raw_purposes if isinstance(raw_purposes, list) else []
    purposes_valid = (
        type(raw_purposes) is list
        and bool(raw_purposes)
        and all(type(purpose) is str and purpose in ONLINE_SIGNER_PURPOSES for purpose in raw_purposes)
        and raw_purposes == sorted(set(raw_purposes))
    )
    errors: list[str] = []
    if not purposes_valid:
        errors.append("FEDERATION_SIGNER_CERTIFICATE_PURPOSES_INVALID")
    if required_purpose is not None:
        if type(required_purpose) is not str or required_purpose not in ONLINE_SIGNER_PURPOSES:
            errors.append("FEDERATION_SIGNER_PURPOSE_INVALID")
        elif purposes_valid and required_purpose not in normalized_purposes:
            errors.append("FEDERATION_SIGNER_PURPOSE_NOT_ALLOWED")
    return errors


def _normalize_json_value(value: Any) -> Any:
    if value is None or type(value) in {str, bool, int}:
        if type(value) is str:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise FederationIdentityError("FEDERATION_CANONICAL_PAYLOAD_INVALID") from exc
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise FederationIdentityError("FEDERATION_CANONICAL_PAYLOAD_INVALID")
        return value
    if type(value) is list:
        return [_normalize_json_value(item) for item in value]
    if type(value) is dict:
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise FederationIdentityError("FEDERATION_CANONICAL_PAYLOAD_INVALID")
            try:
                key.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise FederationIdentityError("FEDERATION_CANONICAL_PAYLOAD_INVALID") from exc
            normalized[key] = _normalize_json_value(item)
        return normalized
    raise FederationIdentityError("FEDERATION_CANONICAL_PAYLOAD_INVALID")


def _canonical_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            _normalize_json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return rendered.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise FederationIdentityError("FEDERATION_CANONICAL_PAYLOAD_INVALID") from exc


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: Any, *, expected_length: int | None = None) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = base64.b64decode(
            value + ("=" * (-len(value) % 4)),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError):
        return None
    if expected_length is not None and len(raw) != expected_length:
        return None
    return raw


def _public_bytes(key: Ed25519PrivateKey | Ed25519PublicKey) -> bytes:
    public_key = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _fingerprint(public_bytes: bytes) -> str:
    return "sha256:" + hashlib.sha256(public_bytes).hexdigest()


def _key_id(prefix: str, public_bytes: bytes) -> str:
    return prefix + hashlib.sha256(public_bytes).hexdigest()[:24]


def _validate_fleet_id(fleet_id: Any) -> str:
    if type(fleet_id) is not str:
        raise FederationIdentityError("FEDERATION_FLEET_ID_INVALID")
    fleet = fleet_id
    if _FLEET_ID_PATTERN.fullmatch(fleet) is None:
        raise FederationIdentityError("FEDERATION_FLEET_ID_INVALID")
    return fleet


def _passphrase_bytes(passphrase: bytes | bytearray) -> bytes:
    if not isinstance(passphrase, (bytes, bytearray)):
        raise FederationIdentityError("FEDERATION_PRIVATE_KEY_PASSPHRASE_INVALID")
    value = bytes(passphrase)
    if not (_MIN_PASSPHRASE_BYTES <= len(value) <= _MAX_PASSPHRASE_BYTES) or b"\x00" in value:
        raise FederationIdentityError("FEDERATION_PRIVATE_KEY_PASSPHRASE_INVALID")
    return value


def _binding_digest(binding: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(binding)).hexdigest()


def _derive_private_key_encryption_key(password: bytes, salt: bytes) -> bytes:
    try:
        return Argon2id(
            salt=salt,
            length=_DERIVED_KEY_BYTES,
            iterations=_ARGON2_ITERATIONS,
            lanes=_ARGON2_LANES,
            memory_cost=_ARGON2_MEMORY_KIB,
        ).derive(password)
    except (TypeError, ValueError, UnsupportedAlgorithm) as exc:
        raise FederationIdentityError("FEDERATION_PRIVATE_KEY_ENCRYPTION_UNAVAILABLE") from exc


def _private_key_envelope_metadata(*, salt: bytes, nonce: bytes, binding: Any) -> dict[str, Any]:
    return {
        "schema": PRIVATE_KEY_ENVELOPE_SCHEMA,
        "bindingDigest": _binding_digest(binding),
        "kdf": {
            "algorithm": _PRIVATE_KEY_KDF_ALGORITHM,
            "salt": _b64url_encode(salt),
            "length": _DERIVED_KEY_BYTES,
            "iterations": _ARGON2_ITERATIONS,
            "lanes": _ARGON2_LANES,
            "memoryKiB": _ARGON2_MEMORY_KIB,
        },
        "aead": {
            "algorithm": _PRIVATE_KEY_AEAD_ALGORITHM,
            "nonce": _b64url_encode(nonce),
        },
    }


def _private_key_envelope_aad(metadata: dict[str, Any]) -> bytes:
    return _PRIVATE_KEY_ENVELOPE_DOMAIN + _canonical_bytes(metadata)


def _encrypt_private_key(
    key: Ed25519PrivateKey,
    passphrase: bytes | bytearray,
    *,
    binding: Any,
) -> dict[str, Any]:
    password = _passphrase_bytes(passphrase)
    salt = os.urandom(_ARGON2_SALT_BYTES)
    nonce = os.urandom(_AES_GCM_NONCE_BYTES)
    metadata = _private_key_envelope_metadata(salt=salt, nonce=nonce, binding=binding)
    encryption_key = _derive_private_key_encryption_key(password, salt)
    private_der = key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    ciphertext = AESGCM(encryption_key).encrypt(nonce, private_der, _private_key_envelope_aad(metadata))
    return {**metadata, "ciphertext": _b64url_encode(ciphertext)}


def _private_key_envelope_parts(envelope: Any, *, binding: Any) -> tuple[dict[str, Any], bytes, bytes, bytes]:
    if type(envelope) is not dict:
        raise FederationIdentityError("FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID")
    try:
        normalized = _normalize_json_value(envelope)
    except FederationIdentityError as exc:
        raise FederationIdentityError("FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID") from exc
    assert isinstance(normalized, dict)
    kdf = normalized.get("kdf")
    aead = normalized.get("aead")
    if (
        set(normalized) != {"schema", "bindingDigest", "kdf", "aead", "ciphertext"}
        or normalized.get("schema") != PRIVATE_KEY_ENVELOPE_SCHEMA
        or normalized.get("bindingDigest") != _binding_digest(binding)
        or type(kdf) is not dict
        or set(kdf) != {"algorithm", "salt", "length", "iterations", "lanes", "memoryKiB"}
        or kdf.get("algorithm") != _PRIVATE_KEY_KDF_ALGORITHM
        or type(kdf.get("length")) is not int
        or kdf.get("length") != _DERIVED_KEY_BYTES
        or type(kdf.get("iterations")) is not int
        or kdf.get("iterations") != _ARGON2_ITERATIONS
        or type(kdf.get("lanes")) is not int
        or kdf.get("lanes") != _ARGON2_LANES
        or type(kdf.get("memoryKiB")) is not int
        or kdf.get("memoryKiB") != _ARGON2_MEMORY_KIB
        or type(aead) is not dict
        or set(aead) != {"algorithm", "nonce"}
        or aead.get("algorithm") != _PRIVATE_KEY_AEAD_ALGORITHM
    ):
        raise FederationIdentityError("FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID")
    salt = _b64url_decode(kdf.get("salt"), expected_length=_ARGON2_SALT_BYTES)
    nonce = _b64url_decode(aead.get("nonce"), expected_length=_AES_GCM_NONCE_BYTES)
    ciphertext = _b64url_decode(normalized.get("ciphertext"))
    if salt is None or nonce is None or ciphertext is None or len(ciphertext) < 17:
        raise FederationIdentityError("FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID")
    metadata = {key: value for key, value in normalized.items() if key != "ciphertext"}
    return metadata, salt, nonce, ciphertext


def _load_private_key(envelope: Any, passphrase: bytes | bytearray, *, binding: Any) -> Ed25519PrivateKey:
    password = _passphrase_bytes(passphrase)
    if envelope is None:
        raise FederationIdentityError("FEDERATION_PRIVATE_KEY_UNAVAILABLE")
    metadata, salt, nonce, ciphertext = _private_key_envelope_parts(envelope, binding=binding)
    try:
        encryption_key = _derive_private_key_encryption_key(password, salt)
        private_der = AESGCM(encryption_key).decrypt(nonce, ciphertext, _private_key_envelope_aad(metadata))
        loaded = serialization.load_der_private_key(private_der, password=None)
    except FederationIdentityError:
        raise
    except (InvalidTag, TypeError, ValueError, UnsupportedAlgorithm) as exc:
        raise FederationIdentityError("FEDERATION_PRIVATE_KEY_UNAVAILABLE") from exc
    if not isinstance(loaded, Ed25519PrivateKey):
        raise FederationIdentityError("FEDERATION_PRIVATE_KEY_TYPE_INVALID")
    return loaded


def _exclusive_write(path: Path, payload: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    encoded = _canonical_bytes(payload) + b"\n"
    fd = -1
    created = False
    try:
        fd = os.open(destination, flags, 0o600)
        created = True
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            destination.chmod(0o600)
    except FileExistsError as exc:
        raise FederationIdentityError("FEDERATION_IDENTITY_ALREADY_EXISTS") from exc
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        if created:
            try:
                destination.unlink()
            except OSError:
                pass
        raise FederationIdentityError("FEDERATION_IDENTITY_STORAGE_FAILED") from exc


def _read_bundle(path: Path) -> dict[str, Any]:
    source = Path(path)
    try:
        if source.stat().st_size > _MAX_BUNDLE_BYTES:
            raise FederationIdentityError("FEDERATION_IDENTITY_BUNDLE_TOO_LARGE")
        decoded = json.loads(source.read_text(encoding="utf-8"))
    except FederationIdentityError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FederationIdentityError("FEDERATION_IDENTITY_BUNDLE_INVALID") from exc
    if not isinstance(decoded, dict):
        raise FederationIdentityError("FEDERATION_IDENTITY_BUNDLE_INVALID")
    return decoded


def _root_identity_errors(identity: Any) -> list[str]:
    if type(identity) is not dict:
        return ["FEDERATION_ROOT_IDENTITY_INVALID"]
    try:
        normalized = _normalize_json_value(identity)
    except FederationIdentityError:
        return ["FEDERATION_ROOT_IDENTITY_INVALID"]
    assert isinstance(normalized, dict)
    identity = normalized
    errors: list[str] = []
    if identity.get("schema") != FLEET_IDENTITY_SCHEMA:
        errors.append("FEDERATION_ROOT_IDENTITY_SCHEMA_INVALID")
    try:
        fleet_id = _validate_fleet_id(identity.get("fleetId"))
    except FederationIdentityError:
        fleet_id = ""
        errors.append("FEDERATION_FLEET_ID_INVALID")
    if identity.get("signatureAlgorithm") != SIGNATURE_ALGORITHM:
        errors.append("FEDERATION_ROOT_IDENTITY_ALGORITHM_INVALID")
    public_bytes = _b64url_decode(identity.get("rootPublicKey"), expected_length=32)
    if public_bytes is None:
        errors.append("FEDERATION_ROOT_PUBLIC_KEY_INVALID")
    else:
        if identity.get("rootKeyId") != _key_id(_ROOT_KEY_PREFIX, public_bytes):
            errors.append("FEDERATION_ROOT_KEY_ID_INVALID")
        if identity.get("rootFingerprint") != _fingerprint(public_bytes):
            errors.append("FEDERATION_ROOT_FINGERPRINT_INVALID")
    if _parse_timestamp(identity.get("createdAt")) is None:
        errors.append("FEDERATION_ROOT_IDENTITY_TIMESTAMP_INVALID")
    if not fleet_id:
        errors.append("FEDERATION_ROOT_IDENTITY_INVALID")
    return list(dict.fromkeys(errors))


def _require_valid_root_identity(identity: Any) -> dict[str, Any]:
    errors = _root_identity_errors(identity)
    if errors:
        raise FederationIdentityError(errors[0])
    normalized = _normalize_json_value(identity)
    assert isinstance(normalized, dict)
    return normalized


def validate_fleet_identity(identity: Any) -> dict[str, Any]:
    """Validate and normalize an untrusted public ``fleet-identity-v1`` document."""

    return _require_valid_root_identity(identity)


def normalize_federation_json(value: Any) -> Any:
    """Return a strict JSON-model copy using the same rules as federation signatures."""

    return _normalize_json_value(value)


def canonical_federation_json(value: Any) -> str:
    """Canonical UTF-8 JSON text used by federation signature domains."""

    return _canonical_bytes(value).decode("utf-8")


def create_fleet_root(
    fleet_id: str,
    *,
    bundle_path: Path,
    passphrase: bytes | bytearray,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create one offline encrypted root bundle without overwriting any file."""

    fleet = _validate_fleet_id(fleet_id)
    _passphrase_bytes(passphrase)
    created_at = now or datetime.now(tz=timezone.utc)
    created_at_iso = _utc_iso(created_at)
    private_key = Ed25519PrivateKey.generate()
    public_bytes = _public_bytes(private_key)
    identity = {
        "schema": FLEET_IDENTITY_SCHEMA,
        "fleetId": fleet,
        "rootKeyId": _key_id(_ROOT_KEY_PREFIX, public_bytes),
        "rootPublicKey": _b64url_encode(public_bytes),
        "rootFingerprint": _fingerprint(public_bytes),
        "signatureAlgorithm": SIGNATURE_ALGORITHM,
        "createdAt": created_at_iso,
    }
    bundle = {
        "schema": ROOT_PRIVATE_BUNDLE_SCHEMA,
        "fleetIdentity": identity,
        "privateKeyEnvelope": _encrypt_private_key(private_key, passphrase, binding=identity),
        "createdAt": created_at_iso,
    }
    _exclusive_write(Path(bundle_path), bundle)
    return copy.deepcopy(identity)


def read_fleet_identity(bundle_path: Path) -> dict[str, Any]:
    bundle = _read_bundle(Path(bundle_path))
    if str(bundle.get("schema") or "") != ROOT_PRIVATE_BUNDLE_SCHEMA:
        raise FederationIdentityError("FEDERATION_ROOT_BUNDLE_SCHEMA_INVALID")
    return _require_valid_root_identity(bundle.get("fleetIdentity"))


def export_public_fleet_identity(root_bundle_path: Path, public_identity_path: Path) -> dict[str, Any]:
    """Create a shareable public identity document without private bundle fields."""

    identity = read_fleet_identity(Path(root_bundle_path))
    _exclusive_write(Path(public_identity_path), identity)
    return copy.deepcopy(identity)


def read_public_fleet_identity(public_identity_path: Path) -> dict[str, Any]:
    return _require_valid_root_identity(_read_bundle(Path(public_identity_path)))


def _load_root_key(bundle_path: Path, passphrase: bytes | bytearray) -> tuple[dict[str, Any], Ed25519PrivateKey]:
    bundle = _read_bundle(Path(bundle_path))
    if str(bundle.get("schema") or "") != ROOT_PRIVATE_BUNDLE_SCHEMA:
        raise FederationIdentityError("FEDERATION_ROOT_BUNDLE_SCHEMA_INVALID")
    identity = _require_valid_root_identity(bundle.get("fleetIdentity"))
    key = _load_private_key(bundle.get("privateKeyEnvelope"), passphrase, binding=identity)
    if _b64url_encode(_public_bytes(key)) != str(identity.get("rootPublicKey") or ""):
        raise FederationIdentityError("FEDERATION_ROOT_PRIVATE_KEY_MISMATCH")
    return identity, key


def _certificate_message(certificate_payload: dict[str, Any]) -> bytes:
    return _CERTIFICATE_DOMAIN + _canonical_bytes(certificate_payload)


def _certificate_document_context(certificate: dict[str, Any]) -> dict[str, str]:
    normalized = _normalize_json_value(certificate)
    assert isinstance(normalized, dict)
    context: dict[str, str] = {}
    for field in ("fleetId", "rootKeyId", "rootFingerprint", "signerKeyId"):
        value = normalized.get(field)
        if type(value) is not str or not value:
            raise FederationIdentityError("FEDERATION_SIGNER_CERTIFICATE_INVALID")
        context[field] = value
    context["certificateDigest"] = "sha256:" + hashlib.sha256(_canonical_bytes(normalized)).hexdigest()
    return context


def _document_message(schema: str, document_payload: dict[str, Any], certificate: dict[str, Any]) -> bytes:
    return _DOCUMENT_DOMAIN_PREFIX + _canonical_bytes(
        {
            "schema": schema,
            "certificateContext": _certificate_document_context(certificate),
            "document": document_payload,
        }
    )


def issue_online_signer(
    *,
    root_bundle_path: Path,
    root_passphrase: bytes | bytearray,
    signer_bundle_path: Path,
    signer_passphrase: bytes | bytearray,
    sequence: int,
    not_before: datetime,
    expires_at: datetime,
    purposes: tuple[str, ...] | list[str] = DEFAULT_ONLINE_SIGNER_PURPOSES,
) -> dict[str, Any]:
    """Generate an online signer and root-sign its bounded certificate."""

    _passphrase_bytes(signer_passphrase)
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise FederationIdentityError("FEDERATION_SIGNER_CERTIFICATE_SEQUENCE_INVALID")
    not_before_iso = _utc_iso(not_before)
    expires_at_iso = _utc_iso(expires_at)
    normalized_purposes = _normalize_issued_purposes(purposes)
    if expires_at.astimezone(timezone.utc) <= not_before.astimezone(timezone.utc):
        raise FederationIdentityError("FEDERATION_SIGNER_CERTIFICATE_WINDOW_INVALID")
    identity, root_key = _load_root_key(Path(root_bundle_path), root_passphrase)
    signer_key = Ed25519PrivateKey.generate()
    signer_public = _public_bytes(signer_key)
    certificate_payload: dict[str, Any] = {
        "schema": ONLINE_SIGNER_CERTIFICATE_SCHEMA,
        "fleetId": str(identity["fleetId"]),
        "rootKeyId": str(identity["rootKeyId"]),
        "rootFingerprint": str(identity["rootFingerprint"]),
        "signerKeyId": _key_id(_SIGNER_KEY_PREFIX, signer_public),
        "signerPublicKey": _b64url_encode(signer_public),
        "signatureAlgorithm": SIGNATURE_ALGORITHM,
        "purposes": normalized_purposes,
        "sequence": sequence,
        "issuedAt": not_before_iso,
        "notBefore": not_before_iso,
        "expiresAt": expires_at_iso,
    }
    certificate = {
        **certificate_payload,
        "rootSignature": _b64url_encode(root_key.sign(_certificate_message(certificate_payload))),
    }
    bundle = {
        "schema": ONLINE_SIGNER_PRIVATE_BUNDLE_SCHEMA,
        "certificate": certificate,
        "privateKeyEnvelope": _encrypt_private_key(signer_key, signer_passphrase, binding=certificate),
        "createdAt": not_before_iso,
    }
    _exclusive_write(Path(signer_bundle_path), bundle)
    return copy.deepcopy(certificate)


def validate_online_signer_certificate(
    certificate: Any,
    root_identity: Any,
    *,
    now: datetime | None = None,
    max_future_skew_seconds: int = 0,
    required_purpose: str | None = None,
) -> list[str]:
    """Return semantic/cryptographic errors for an untrusted signer certificate."""

    root_errors = _root_identity_errors(root_identity)
    if root_errors:
        return root_errors
    normalized_root = _normalize_json_value(root_identity)
    assert isinstance(normalized_root, dict)
    root_identity = normalized_root
    if type(certificate) is not dict:
        return ["FEDERATION_SIGNER_CERTIFICATE_INVALID"]
    try:
        normalized_certificate = _normalize_json_value(certificate)
    except FederationIdentityError:
        return ["FEDERATION_SIGNER_CERTIFICATE_INVALID"]
    assert isinstance(normalized_certificate, dict)
    certificate = normalized_certificate
    errors: list[str] = []
    if certificate.get("schema") != ONLINE_SIGNER_CERTIFICATE_SCHEMA:
        errors.append("FEDERATION_SIGNER_CERTIFICATE_SCHEMA_INVALID")
    if type(certificate.get("fleetId")) is not str or certificate.get("fleetId") != root_identity.get("fleetId"):
        errors.append("FEDERATION_SIGNER_CERTIFICATE_FLEET_MISMATCH")
    if (
        type(certificate.get("rootKeyId")) is not str
        or certificate.get("rootKeyId") != root_identity.get("rootKeyId")
        or type(certificate.get("rootFingerprint")) is not str
        or certificate.get("rootFingerprint") != root_identity.get("rootFingerprint")
    ):
        errors.append("FEDERATION_SIGNER_CERTIFICATE_ROOT_MISMATCH")
    if certificate.get("signatureAlgorithm") != SIGNATURE_ALGORITHM:
        errors.append("FEDERATION_SIGNER_CERTIFICATE_ALGORITHM_INVALID")
    errors.extend(_certificate_purpose_errors(certificate, required_purpose))
    signer_public = _b64url_decode(certificate.get("signerPublicKey"), expected_length=32)
    if signer_public is None:
        errors.append("FEDERATION_SIGNER_CERTIFICATE_PUBLIC_KEY_INVALID")
    elif type(certificate.get("signerKeyId")) is not str or certificate.get("signerKeyId") != _key_id(
        _SIGNER_KEY_PREFIX, signer_public
    ):
        errors.append("FEDERATION_SIGNER_CERTIFICATE_SIGNER_KEY_ID_INVALID")
    raw_sequence = certificate.get("sequence")
    if isinstance(raw_sequence, bool) or not isinstance(raw_sequence, int) or raw_sequence < 1:
        errors.append("FEDERATION_SIGNER_CERTIFICATE_SEQUENCE_INVALID")

    issued_at = _parse_timestamp(certificate.get("issuedAt"))
    not_before = _parse_timestamp(certificate.get("notBefore"))
    expires_at = _parse_timestamp(certificate.get("expiresAt"))
    if issued_at is None or not_before is None or expires_at is None:
        errors.append("FEDERATION_SIGNER_CERTIFICATE_TIMESTAMP_INVALID")
    elif not (issued_at <= not_before < expires_at):
        errors.append("FEDERATION_SIGNER_CERTIFICATE_WINDOW_INVALID")
    else:
        candidate_now = now if now is not None else datetime.now(tz=timezone.utc)
        if not isinstance(candidate_now, datetime) or candidate_now.tzinfo is None or candidate_now.utcoffset() is None:
            errors.append("FEDERATION_CERTIFICATE_VALIDATION_TIME_INVALID")
        else:
            current = candidate_now.astimezone(timezone.utc)
            if (
                isinstance(max_future_skew_seconds, bool)
                or not isinstance(max_future_skew_seconds, int)
                or max_future_skew_seconds < 0
            ):
                errors.append("FEDERATION_CERTIFICATE_VALIDATION_SKEW_INVALID")
                skew = 0
            else:
                skew = max_future_skew_seconds
            if (issued_at - current).total_seconds() > skew:
                errors.append("FEDERATION_SIGNER_CERTIFICATE_ISSUED_IN_FUTURE")
            if (not_before - current).total_seconds() > skew:
                errors.append("FEDERATION_SIGNER_CERTIFICATE_NOT_YET_VALID")
            if current >= expires_at:
                errors.append("FEDERATION_SIGNER_CERTIFICATE_EXPIRED")

    root_public = _b64url_decode(root_identity.get("rootPublicKey"), expected_length=32)
    root_signature = _b64url_decode(certificate.get("rootSignature"), expected_length=64)
    if root_public is None or root_signature is None:
        errors.append("FEDERATION_SIGNER_CERTIFICATE_SIGNATURE_INVALID")
    else:
        payload = {key: value for key, value in certificate.items() if key != "rootSignature"}
        try:
            Ed25519PublicKey.from_public_bytes(root_public).verify(root_signature, _certificate_message(payload))
        except (InvalidSignature, ValueError, FederationIdentityError):
            errors.append("FEDERATION_SIGNER_CERTIFICATE_SIGNATURE_INVALID")
    return list(dict.fromkeys(errors))


def _require_valid_certificate(
    certificate: Any,
    root_identity: Any,
    *,
    now: datetime | None,
    required_purpose: str | None = None,
) -> dict[str, Any]:
    errors = validate_online_signer_certificate(
        certificate,
        root_identity,
        now=now,
        required_purpose=required_purpose,
    )
    if errors:
        raise FederationIdentityError(errors[0])
    normalized = _normalize_json_value(certificate)
    assert isinstance(normalized, dict)
    return normalized


@dataclass(slots=True, repr=False)
class OnlineFleetSigner:
    """Loaded online signing capability; repr never exposes private material."""

    _private_key: Ed25519PrivateKey
    _certificate: dict[str, Any]

    @property
    def signer_key_id(self) -> str:
        return str(self._certificate["signerKeyId"])

    @property
    def fleet_id(self) -> str:
        return str(self._certificate["fleetId"])

    @property
    def certificate(self) -> dict[str, Any]:
        return copy.deepcopy(self._certificate)

    def _sign(self, message: bytes) -> bytes:
        return self._private_key.sign(message)

    def __repr__(self) -> str:
        return f"OnlineFleetSigner(signer_key_id={self.signer_key_id!r})"


def load_online_signer(
    bundle_path: Path,
    passphrase: bytes | bytearray,
    *,
    root_identity: dict[str, Any],
    now: datetime | None = None,
) -> OnlineFleetSigner:
    bundle = _read_bundle(Path(bundle_path))
    if str(bundle.get("schema") or "") != ONLINE_SIGNER_PRIVATE_BUNDLE_SCHEMA:
        raise FederationIdentityError("FEDERATION_SIGNER_BUNDLE_SCHEMA_INVALID")
    certificate = _require_valid_certificate(bundle.get("certificate"), root_identity, now=now)
    key = _load_private_key(bundle.get("privateKeyEnvelope"), passphrase, binding=certificate)
    if _b64url_encode(_public_bytes(key)) != str(certificate.get("signerPublicKey") or ""):
        raise FederationIdentityError("FEDERATION_SIGNER_PRIVATE_KEY_MISMATCH")
    return OnlineFleetSigner(key, certificate)


def sign_federation_document(
    signer: OnlineFleetSigner,
    document: dict[str, Any],
    *,
    purpose: str | None = None,
) -> dict[str, Any]:
    """Sign the entire canonical document with schema domain separation."""

    if type(document) is not dict:
        raise FederationIdentityError("FEDERATION_DOCUMENT_INVALID")
    if purpose is not None:
        purpose_errors = _certificate_purpose_errors(signer.certificate, purpose)
        if purpose_errors:
            raise FederationIdentityError(purpose_errors[0])
    normalized = _normalize_json_value(document)
    assert isinstance(normalized, dict)
    if any(field in normalized for field in ("signerKeyId", "signatureAlgorithm", "signature")):
        raise FederationIdentityError("FEDERATION_DOCUMENT_ALREADY_SIGNED")
    schema = normalized.get("schema")
    if type(schema) is not str or not schema:
        raise FederationIdentityError("FEDERATION_DOCUMENT_SCHEMA_INVALID")
    fleet_id = normalized.get("fleetId")
    if type(fleet_id) is not str or not fleet_id:
        raise FederationIdentityError("FEDERATION_DOCUMENT_FLEET_ID_REQUIRED")
    if fleet_id != signer.fleet_id:
        raise FederationIdentityError("FEDERATION_DOCUMENT_FLEET_MISMATCH")
    payload = normalized
    payload["signerKeyId"] = signer.signer_key_id
    payload["signatureAlgorithm"] = SIGNATURE_ALGORITHM
    signature = signer._sign(_document_message(schema, payload, signer.certificate))
    return {**payload, "signature": _b64url_encode(signature)}


def verify_federation_document(
    document: Any,
    *,
    certificate: dict[str, Any],
    root_identity: dict[str, Any],
    expected_schema: str,
    now: datetime | None = None,
    required_purpose: str | None = None,
) -> dict[str, Any]:
    """Verify root chain and every canonical field of an untrusted document."""

    verified_certificate = _require_valid_certificate(
        certificate,
        root_identity,
        now=now,
        required_purpose=required_purpose,
    )
    if type(document) is not dict:
        raise FederationIdentityError("FEDERATION_DOCUMENT_INVALID")
    normalized = _normalize_json_value(document)
    assert isinstance(normalized, dict)
    schema = normalized.get("schema")
    if type(expected_schema) is not str or not expected_schema or type(schema) is not str or schema != expected_schema:
        raise FederationIdentityError("FEDERATION_DOCUMENT_SCHEMA_INVALID")
    if type(normalized.get("signerKeyId")) is not str or normalized.get("signerKeyId") != verified_certificate.get("signerKeyId"):
        raise FederationIdentityError("FEDERATION_DOCUMENT_SIGNER_MISMATCH")
    if normalized.get("signatureAlgorithm") != SIGNATURE_ALGORITHM:
        raise FederationIdentityError("FEDERATION_DOCUMENT_ALGORITHM_INVALID")
    fleet_id = normalized.get("fleetId")
    if type(fleet_id) is not str or not fleet_id:
        raise FederationIdentityError("FEDERATION_DOCUMENT_FLEET_ID_REQUIRED")
    if fleet_id != verified_certificate.get("fleetId"):
        raise FederationIdentityError("FEDERATION_DOCUMENT_FLEET_MISMATCH")
    signature = _b64url_decode(normalized.get("signature"), expected_length=64)
    signer_public = _b64url_decode(verified_certificate.get("signerPublicKey"), expected_length=32)
    if signature is None or signer_public is None:
        raise FederationIdentityError("FEDERATION_DOCUMENT_SIGNATURE_INVALID")
    payload = {key: value for key, value in normalized.items() if key != "signature"}
    try:
        Ed25519PublicKey.from_public_bytes(signer_public).verify(
            signature,
            _document_message(schema, payload, verified_certificate),
        )
    except (InvalidSignature, ValueError, FederationIdentityError) as exc:
        raise FederationIdentityError("FEDERATION_DOCUMENT_SIGNATURE_INVALID") from exc
    return normalized

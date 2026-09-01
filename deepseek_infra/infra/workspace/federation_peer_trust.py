"""Durable operator-pinned Fleet trust registry with no TOFU activation path."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import federation_identity, federation_transfer_journal

FEDERATION_DIR = config.ROOT / ".federation"
PEER_TRUST_DB = FEDERATION_DIR / "peer-trust.sqlite3"

PEER_TRUST_RECORD_SCHEMA = "federation-peer-trust-record-v1"
ONLINE_SIGNER_TRUST_RECORD_SCHEMA = "federation-online-signer-trust-record-v1"
READINESS_SEQUENCE_RECORD_SCHEMA = "federation-readiness-sequence-v1"
CHALLENGE_JOURNAL_RECORD_SCHEMA = "federation-challenge-journal-v1"
INGRESS_GRANT_RECORD_SCHEMA = "federation-ingress-grant-record-v1"
INGRESS_WRITE_RESERVATION_SCHEMA = "federation-ingress-write-reservation-v1"
STATE_PENDING = "PENDING"
STATE_VERIFIED = "VERIFIED"
STATE_ACTIVE = "ACTIVE"
STATE_SUSPENDED = "SUSPENDED"
STATE_REVOKED = "REVOKED"
PEER_STATES = frozenset({STATE_PENDING, STATE_VERIFIED, STATE_ACTIVE, STATE_SUSPENDED, STATE_REVOKED})
AUTHORIZATION_CURRENT = "CURRENT"
AUTHORIZATION_HISTORICAL_PROOF = "HISTORICAL_PROOF"
AUTHORIZATION_MODES = frozenset({AUTHORIZATION_CURRENT, AUTHORIZATION_HISTORICAL_PROOF})
CHALLENGE_DIRECTION_OUTBOUND = "OUTBOUND"
CHALLENGE_DIRECTION_INBOUND = "INBOUND"
CHALLENGE_DIRECTIONS = frozenset({CHALLENGE_DIRECTION_OUTBOUND, CHALLENGE_DIRECTION_INBOUND})
CHALLENGE_STATE_PENDING = "PENDING"
CHALLENGE_STATE_RESPONDED = "RESPONDED"
CHALLENGE_STATE_CONSUMED = "CONSUMED"
INGRESS_GRANT_STATE_ACTIVE = "ACTIVE"
INGRESS_GRANT_STATE_REVOKED = "REVOKED"

_FLEET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SIGNER_KEY_ID_PATTERN = re.compile(r"^fed-signer-[0-9a-f]{24}$")
_SHA256_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_GRANT_ID_PATTERN = re.compile(r"^grant-[0-9a-f]{32}$")
_WRITE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CONTROL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REQUIRED_METADATA_FIELDS = frozenset({"provider", "region", "jurisdiction", "siteClass"})
_INGRESS_GRANT_FIELDS = frozenset(
    {
        "schema",
        "fleetId",
        "grantId",
        "sourceFleetId",
        "destinationFleetId",
        "transferId",
        "policyId",
        "backupId",
        "objectSetDigest",
        "allowedObjectPrefix",
        "maxBytes",
        "issuedAt",
        "expiresAt",
        "nonce",
        "sessionNonceDigest",
        "signerCertificate",
        "signerKeyId",
        "signatureAlgorithm",
        "signature",
    }
)
_MAX_AUDIT_TEXT_LENGTH = 256

_CREATE_LOCAL_IDENTITY_SQL = """
CREATE TABLE IF NOT EXISTS federation_local_identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    fleet_id TEXT NOT NULL,
    root_key_id TEXT NOT NULL,
    root_fingerprint TEXT NOT NULL,
    identity_digest TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    bound_at TEXT NOT NULL
)
"""

_CREATE_PEERS_SQL = """
CREATE TABLE IF NOT EXISTS federation_peer_trust (
    peer_fleet_id TEXT PRIMARY KEY,
    root_key_id TEXT NOT NULL,
    root_fingerprint TEXT NOT NULL UNIQUE,
    identity_digest TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    metadata_digest TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('PENDING', 'VERIFIED', 'ACTIVE', 'SUSPENDED', 'REVOKED')),
    pinned_by TEXT NOT NULL,
    state_reason TEXT NOT NULL,
    pinned_at TEXT NOT NULL,
    verified_at TEXT,
    activated_at TEXT,
    suspended_at TEXT,
    revoked_at TEXT,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    updated_at TEXT NOT NULL
)
"""

_CREATE_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS federation_peer_trust_events (
    event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_fleet_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    previous_state TEXT,
    next_state TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY(peer_fleet_id) REFERENCES federation_peer_trust(peer_fleet_id)
)
"""

_CREATE_EVENTS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_federation_peer_trust_events_peer
ON federation_peer_trust_events(peer_fleet_id, event_sequence)
"""

_CREATE_SIGNERS_SQL = """
CREATE TABLE IF NOT EXISTS federation_online_signers (
    peer_fleet_id TEXT NOT NULL,
    signer_key_id TEXT NOT NULL UNIQUE,
    certificate_sequence INTEGER NOT NULL CHECK(certificate_sequence >= 1),
    certificate_digest TEXT NOT NULL,
    certificate_json TEXT NOT NULL,
    accepted_by TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_by TEXT,
    revocation_reason TEXT,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    PRIMARY KEY(peer_fleet_id, signer_key_id),
    UNIQUE(peer_fleet_id, certificate_sequence),
    FOREIGN KEY(peer_fleet_id) REFERENCES federation_peer_trust(peer_fleet_id)
)
"""

_CREATE_SIGNER_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS federation_online_signer_events (
    event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_fleet_id TEXT NOT NULL,
    signer_key_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY(peer_fleet_id, signer_key_id)
        REFERENCES federation_online_signers(peer_fleet_id, signer_key_id)
)
"""

_CREATE_SIGNER_EVENTS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_federation_online_signer_events_peer
ON federation_online_signer_events(peer_fleet_id, event_sequence)
"""

_CREATE_READINESS_SEQUENCES_SQL = """
CREATE TABLE IF NOT EXISTS federation_readiness_sequences (
    peer_fleet_id TEXT PRIMARY KEY,
    high_sequence INTEGER NOT NULL CHECK(high_sequence >= 1),
    signer_key_id TEXT NOT NULL,
    attestation_digest TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    FOREIGN KEY(peer_fleet_id) REFERENCES federation_peer_trust(peer_fleet_id),
    FOREIGN KEY(peer_fleet_id, signer_key_id)
        REFERENCES federation_online_signers(peer_fleet_id, signer_key_id)
)
"""

_CREATE_CHALLENGE_JOURNAL_SQL = """
CREATE TABLE IF NOT EXISTS federation_challenge_journal (
    direction TEXT NOT NULL CHECK(direction IN ('OUTBOUND', 'INBOUND')),
    nonce_digest TEXT NOT NULL,
    peer_fleet_id TEXT NOT NULL,
    source_fleet_id TEXT NOT NULL,
    destination_fleet_id TEXT NOT NULL,
    session_purpose TEXT NOT NULL,
    challenge_digest TEXT NOT NULL,
    challenge_json TEXT NOT NULL,
    response_digest TEXT,
    response_json TEXT,
    state TEXT NOT NULL CHECK(state IN ('PENDING', 'RESPONDED', 'CONSUMED')),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    completed_at TEXT,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    PRIMARY KEY(direction, nonce_digest),
    FOREIGN KEY(peer_fleet_id) REFERENCES federation_peer_trust(peer_fleet_id)
)
"""

_CREATE_CHALLENGE_NONCE_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_federation_challenge_nonce
ON federation_challenge_journal(nonce_digest)
"""

_CREATE_INGRESS_GRANTS_SQL = """
CREATE TABLE IF NOT EXISTS federation_ingress_grants (
    grant_id TEXT PRIMARY KEY,
    session_nonce_digest TEXT NOT NULL UNIQUE,
    grant_nonce_digest TEXT NOT NULL UNIQUE,
    source_fleet_id TEXT NOT NULL,
    destination_fleet_id TEXT NOT NULL,
    transfer_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    backup_id TEXT NOT NULL,
    object_set_digest TEXT NOT NULL,
    allowed_object_prefix TEXT NOT NULL,
    max_bytes INTEGER NOT NULL CHECK(max_bytes > 0),
    signer_key_id TEXT NOT NULL,
    grant_digest TEXT NOT NULL,
    grant_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('ACTIVE', 'REVOKED')),
    bytes_reserved INTEGER NOT NULL CHECK(bytes_reserved >= 0),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    FOREIGN KEY(source_fleet_id) REFERENCES federation_peer_trust(peer_fleet_id)
)
"""

_CREATE_INGRESS_WRITES_SQL = """
CREATE TABLE IF NOT EXISTS federation_ingress_writes (
    grant_id TEXT NOT NULL,
    write_id TEXT NOT NULL,
    object_key TEXT NOT NULL,
    byte_count INTEGER NOT NULL CHECK(byte_count > 0),
    bytes_reserved_after INTEGER NOT NULL CHECK(bytes_reserved_after > 0),
    reserved_at TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    PRIMARY KEY(grant_id, write_id),
    FOREIGN KEY(grant_id) REFERENCES federation_ingress_grants(grant_id)
)
"""

_CREATE_INGRESS_OBJECT_KEY_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_federation_ingress_writes_object_key
ON federation_ingress_writes(grant_id, object_key)
"""


class FederationTrustError(RuntimeError):
    """Fail-closed trust-registry error with a stable machine-readable code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = str(code)
        super().__init__(message or self.code)


def _utc_iso(value: datetime | None) -> str:
    current = value if value is not None else datetime.now(tz=timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise FederationTrustError("FEDERATION_TRUST_TIMESTAMP_INVALID")
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise FederationTrustError("FEDERATION_TRUST_CANONICAL_PAYLOAD_INVALID") from exc


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _fleet_id(value: Any) -> str:
    if type(value) is not str or _FLEET_ID_PATTERN.fullmatch(value) is None:
        raise FederationTrustError("FEDERATION_PEER_FLEET_ID_INVALID")
    return value


def _signer_key_id(value: Any) -> str:
    if type(value) is not str or _SIGNER_KEY_ID_PATTERN.fullmatch(value) is None:
        raise FederationTrustError("FEDERATION_SIGNER_KEY_ID_INVALID")
    return value


def _sha256_digest(value: Any, *, code: str) -> str:
    if type(value) is not str or _SHA256_DIGEST_PATTERN.fullmatch(value) is None:
        raise FederationTrustError(code)
    return value


def _challenge_direction(value: Any) -> str:
    if type(value) is not str or value not in CHALLENGE_DIRECTIONS:
        raise FederationTrustError("FEDERATION_CHALLENGE_DIRECTION_INVALID")
    return value


def _challenge_nonce_digest(value: Any) -> str:
    if type(value) is not str or not value:
        raise FederationTrustError("FEDERATION_CHALLENGE_NONCE_INVALID")
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise FederationTrustError("FEDERATION_CHALLENGE_NONCE_INVALID") from exc
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _grant_id(value: Any) -> str:
    if type(value) is not str or _GRANT_ID_PATTERN.fullmatch(value) is None:
        raise FederationTrustError("FEDERATION_INGRESS_GRANT_ID_INVALID")
    return value


def _write_id(value: Any) -> str:
    if type(value) is not str or _WRITE_ID_PATTERN.fullmatch(value) is None:
        raise FederationTrustError("FEDERATION_INGRESS_WRITE_ID_INVALID")
    return value


def _control_id(value: Any, *, code: str) -> str:
    if type(value) is not str or _CONTROL_ID_PATTERN.fullmatch(value) is None:
        raise FederationTrustError(code)
    return value


def _object_prefix(value: Any) -> str:
    if type(value) is not str or not value.startswith("federation/") or not value.endswith("/"):
        raise FederationTrustError("FEDERATION_INGRESS_OBJECT_PREFIX_INVALID")
    lowered = value.casefold()
    if (
        "\\" in value
        or "//" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or any(encoded in lowered for encoded in ("%2e", "%2f", "%5c"))
        or any(segment in {".", ".."} for segment in value.split("/") if segment)
    ):
        raise FederationTrustError("FEDERATION_INGRESS_OBJECT_PREFIX_INVALID")
    return value


def _object_key(value: Any, *, prefix: str) -> str:
    if type(value) is not str or not value.startswith(prefix) or value == prefix:
        raise FederationTrustError("FEDERATION_INGRESS_OBJECT_PREFIX_VIOLATION")
    lowered = value.casefold()
    if (
        "\\" in value
        or "//" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or any(encoded in lowered for encoded in ("%2e", "%2f", "%5c"))
        or any(segment in {"", ".", ".."} for segment in value[len(prefix) :].split("/"))
    ):
        raise FederationTrustError("FEDERATION_INGRESS_OBJECT_PREFIX_VIOLATION")
    return value


def _stored_timestamp(value: Any, *, code: str) -> datetime:
    if type(value) is not str:
        raise FederationTrustError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FederationTrustError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FederationTrustError(code)
    return parsed.astimezone(timezone.utc)


def _audit_text(value: Any, *, code: str) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > _MAX_AUDIT_TEXT_LENGTH:
        raise FederationTrustError(code)
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise FederationTrustError(code)
    return value


def _metadata(value: Any) -> dict[str, str]:
    if type(value) is not dict or set(value) != _REQUIRED_METADATA_FIELDS:
        raise FederationTrustError("FEDERATION_PEER_METADATA_INVALID")
    normalized: dict[str, str] = {}
    for field in sorted(_REQUIRED_METADATA_FIELDS):
        normalized[field] = _audit_text(value.get(field), code="FEDERATION_PEER_METADATA_INVALID")
    return normalized


def _identity(value: Any) -> dict[str, Any]:
    try:
        return federation_identity.validate_fleet_identity(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederationTrustError("FEDERATION_PEER_IDENTITY_INVALID") from exc


class PeerTrustRegistry:
    """One Fleet's durable, operator-controlled registry of pinned peer roots."""

    def __init__(self, db_path: Path, local_identity: dict[str, Any]) -> None:
        self._db_path = Path(db_path)
        self._local_identity = _identity(local_identity)
        self._ensure_schema()
        self._bind_local_identity()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def local_identity(self) -> dict[str, Any]:
        return json.loads(_canonical_json(self._local_identity))

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(_CREATE_LOCAL_IDENTITY_SQL)
            connection.execute(_CREATE_PEERS_SQL)
            connection.execute(_CREATE_EVENTS_SQL)
            connection.execute(_CREATE_EVENTS_INDEX_SQL)
            connection.execute(_CREATE_SIGNERS_SQL)
            connection.execute(_CREATE_SIGNER_EVENTS_SQL)
            connection.execute(_CREATE_SIGNER_EVENTS_INDEX_SQL)
            connection.execute(_CREATE_READINESS_SEQUENCES_SQL)
            connection.execute(_CREATE_CHALLENGE_JOURNAL_SQL)
            connection.execute(_CREATE_CHALLENGE_NONCE_INDEX_SQL)
            connection.execute(_CREATE_INGRESS_GRANTS_SQL)
            connection.execute(_CREATE_INGRESS_WRITES_SQL)
            connection.execute(_CREATE_INGRESS_OBJECT_KEY_INDEX_SQL)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _bind_local_identity(self) -> None:
        identity_json = _canonical_json(self._local_identity)
        identity_digest = _digest(self._local_identity)
        bound_at = _utc_iso(None)
        with self._write() as connection:
            existing = connection.execute("SELECT * FROM federation_local_identity WHERE singleton = 1").fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO federation_local_identity (
                        singleton, fleet_id, root_key_id, root_fingerprint,
                        identity_digest, identity_json, bound_at
                    ) VALUES (1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._local_identity["fleetId"],
                        self._local_identity["rootKeyId"],
                        self._local_identity["rootFingerprint"],
                        identity_digest,
                        identity_json,
                        bound_at,
                    ),
                )
                return
            if (
                existing["fleet_id"] != self._local_identity["fleetId"]
                or existing["root_key_id"] != self._local_identity["rootKeyId"]
                or existing["root_fingerprint"] != self._local_identity["rootFingerprint"]
                or existing["identity_digest"] != identity_digest
                or existing["identity_json"] != identity_json
            ):
                raise FederationTrustError("FEDERATION_LOCAL_IDENTITY_CONFLICT")

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": PEER_TRUST_RECORD_SCHEMA,
            "peerFleetId": str(row["peer_fleet_id"]),
            "rootKeyId": str(row["root_key_id"]),
            "rootFingerprint": str(row["root_fingerprint"]),
            "fleetIdentity": json.loads(str(row["identity_json"])),
            "pinnedMetadata": json.loads(str(row["metadata_json"])),
            "metadataDigest": str(row["metadata_digest"]),
            "state": str(row["state"]),
            "pinnedBy": str(row["pinned_by"]),
            "stateReason": str(row["state_reason"]),
            "pinnedAt": str(row["pinned_at"]),
            "verifiedAt": row["verified_at"],
            "activatedAt": row["activated_at"],
            "suspendedAt": row["suspended_at"],
            "revokedAt": row["revoked_at"],
            "revision": int(row["revision"]),
            "updatedAt": str(row["updated_at"]),
        }

    @staticmethod
    def _signer_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": ONLINE_SIGNER_TRUST_RECORD_SCHEMA,
            "peerFleetId": str(row["peer_fleet_id"]),
            "signerKeyId": str(row["signer_key_id"]),
            "sequence": int(row["certificate_sequence"]),
            "certificateDigest": str(row["certificate_digest"]),
            "certificate": json.loads(str(row["certificate_json"])),
            "acceptedBy": str(row["accepted_by"]),
            "acceptedAt": str(row["accepted_at"]),
            "revokedAt": row["revoked_at"],
            "revokedBy": row["revoked_by"],
            "revocationReason": row["revocation_reason"],
            "revision": int(row["revision"]),
        }

    @staticmethod
    def _readiness_sequence_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": READINESS_SEQUENCE_RECORD_SCHEMA,
            "peerFleetId": str(row["peer_fleet_id"]),
            "highSequence": int(row["high_sequence"]),
            "signerKeyId": str(row["signer_key_id"]),
            "attestationDigest": str(row["attestation_digest"]),
            "acceptedAt": str(row["accepted_at"]),
            "revision": int(row["revision"]),
        }

    @staticmethod
    def _challenge_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": CHALLENGE_JOURNAL_RECORD_SCHEMA,
            "direction": str(row["direction"]),
            "nonceDigest": str(row["nonce_digest"]),
            "peerFleetId": str(row["peer_fleet_id"]),
            "sourceFleetId": str(row["source_fleet_id"]),
            "destinationFleetId": str(row["destination_fleet_id"]),
            "sessionPurpose": str(row["session_purpose"]),
            "challengeDigest": str(row["challenge_digest"]),
            "challenge": json.loads(str(row["challenge_json"])),
            "responseDigest": row["response_digest"],
            "response": None if row["response_json"] is None else json.loads(str(row["response_json"])),
            "state": str(row["state"]),
            "issuedAt": str(row["issued_at"]),
            "expiresAt": str(row["expires_at"]),
            "recordedAt": str(row["recorded_at"]),
            "completedAt": row["completed_at"],
            "revision": int(row["revision"]),
        }

    @staticmethod
    def _ingress_grant_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": INGRESS_GRANT_RECORD_SCHEMA,
            "grantId": str(row["grant_id"]),
            "sessionNonceDigest": str(row["session_nonce_digest"]),
            "grantNonceDigest": str(row["grant_nonce_digest"]),
            "sourceFleetId": str(row["source_fleet_id"]),
            "destinationFleetId": str(row["destination_fleet_id"]),
            "transferId": str(row["transfer_id"]),
            "policyId": str(row["policy_id"]),
            "backupId": str(row["backup_id"]),
            "objectSetDigest": str(row["object_set_digest"]),
            "allowedObjectPrefix": str(row["allowed_object_prefix"]),
            "maxBytes": int(row["max_bytes"]),
            "signerKeyId": str(row["signer_key_id"]),
            "grantDigest": str(row["grant_digest"]),
            "grant": json.loads(str(row["grant_json"])),
            "state": str(row["state"]),
            "bytesReserved": int(row["bytes_reserved"]),
            "issuedAt": str(row["issued_at"]),
            "expiresAt": str(row["expires_at"]),
            "createdAt": str(row["created_at"]),
            "revision": int(row["revision"]),
        }

    @staticmethod
    def _ingress_write_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": INGRESS_WRITE_RESERVATION_SCHEMA,
            "grantId": str(row["grant_id"]),
            "writeId": str(row["write_id"]),
            "objectKey": str(row["object_key"]),
            "byteCount": int(row["byte_count"]),
            "bytesReservedAfter": int(row["bytes_reserved_after"]),
            "reservedAt": str(row["reserved_at"]),
            "revision": int(row["revision"]),
        }

    @staticmethod
    def _peer_row(connection: sqlite3.Connection, peer_fleet_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM federation_peer_trust WHERE peer_fleet_id = ?",
            (peer_fleet_id,),
        ).fetchone()

    @staticmethod
    def _signer_row(
        connection: sqlite3.Connection,
        peer_fleet_id: str,
        signer_key_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM federation_online_signers
            WHERE peer_fleet_id = ? AND signer_key_id = ?
            """,
            (peer_fleet_id, signer_key_id),
        ).fetchone()

    def _require_active_signer_in_tx(
        self,
        connection: sqlite3.Connection,
        *,
        peer_fleet_id: str,
        signer_key_id: str,
        validation_time: datetime,
        required_purpose: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        peer_row = self._peer_row(connection, peer_fleet_id)
        if peer_row is None:
            raise FederationTrustError("FEDERATION_PEER_NOT_PINNED")
        peer_state = str(peer_row["state"])
        if peer_state == STATE_REVOKED:
            raise FederationTrustError("FEDERATION_PEER_REVOKED")
        if peer_state != STATE_ACTIVE:
            raise FederationTrustError("FEDERATION_PEER_NOT_ACTIVE")
        signer_row = self._signer_row(connection, peer_fleet_id, signer_key_id)
        if signer_row is None:
            raise FederationTrustError("FEDERATION_SIGNER_NOT_ACCEPTED")
        revoked_at = (
            None
            if signer_row["revoked_at"] is None
            else _stored_timestamp(
                signer_row["revoked_at"],
                code="FEDERATION_SIGNER_REVOCATION_TIMESTAMP_INVALID",
            )
        )
        if revoked_at is not None and validation_time >= revoked_at:
            raise FederationTrustError("FEDERATION_SIGNER_REVOKED")
        certificate = json.loads(str(signer_row["certificate_json"]))
        root_identity = json.loads(str(peer_row["identity_json"]))
        certificate_errors = federation_identity.validate_online_signer_certificate(
            certificate,
            root_identity,
            now=validation_time,
            required_purpose=required_purpose,
        )
        if certificate_errors:
            raise FederationTrustError(certificate_errors[0])
        return peer_row, signer_row

    @staticmethod
    def _verify_signed_document(
        document: dict[str, Any],
        *,
        certificate: Any,
        root_identity: dict[str, Any],
        expected_schema: str,
        validation_time: datetime,
        required_purpose: str = federation_identity.PURPOSE_SESSION_AUTHENTICATION,
    ) -> dict[str, Any]:
        if type(certificate) is not dict:
            raise FederationTrustError("FEDERATION_CHALLENGE_SIGNER_CERTIFICATE_INVALID")
        try:
            return federation_identity.verify_federation_document(
                document,
                certificate=certificate,
                root_identity=root_identity,
                expected_schema=expected_schema,
                now=validation_time,
                required_purpose=required_purpose,
            )
        except federation_identity.FederationIdentityError as exc:
            raise FederationTrustError(exc.code) from exc

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        peer_fleet_id: str,
        event_type: str,
        previous_state: str | None,
        next_state: str,
        actor: str,
        reason: str,
        occurred_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO federation_peer_trust_events (
                peer_fleet_id, event_type, previous_state, next_state,
                actor, reason, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (peer_fleet_id, event_type, previous_state, next_state, actor, reason, occurred_at),
        )

    @staticmethod
    def _append_signer_event(
        connection: sqlite3.Connection,
        *,
        peer_fleet_id: str,
        signer_key_id: str,
        event_type: str,
        actor: str,
        reason: str,
        occurred_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO federation_online_signer_events (
                peer_fleet_id, signer_key_id, event_type,
                actor, reason, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (peer_fleet_id, signer_key_id, event_type, actor, reason, occurred_at),
        )

    def pin_peer(
        self,
        peer_identity: dict[str, Any],
        *,
        expected_root_fingerprint: str | None,
        metadata: dict[str, str],
        operator_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        identity = _identity(peer_identity)
        peer_fleet_id = _fleet_id(identity["fleetId"])
        if peer_fleet_id == self._local_identity["fleetId"] or identity["rootFingerprint"] == self._local_identity["rootFingerprint"]:
            raise FederationTrustError("FEDERATION_PEER_SELF_TRUST_FORBIDDEN")
        if expected_root_fingerprint is None:
            raise FederationTrustError("FEDERATION_PEER_ROOT_PIN_REQUIRED")
        if type(expected_root_fingerprint) is not str or expected_root_fingerprint != identity["rootFingerprint"]:
            raise FederationTrustError("FEDERATION_PEER_ROOT_PIN_MISMATCH")
        pinned_metadata = _metadata(metadata)
        actor = _audit_text(operator_id, code="FEDERATION_TRUST_ACTOR_INVALID")
        timestamp = _utc_iso(now)
        identity_json = _canonical_json(identity)
        identity_digest = _digest(identity)
        metadata_json = _canonical_json(pinned_metadata)
        metadata_digest = _digest(pinned_metadata)

        with self._write() as connection:
            existing = self._peer_row(connection, peer_fleet_id)
            if existing is not None:
                if existing["root_fingerprint"] != identity["rootFingerprint"]:
                    raise FederationTrustError("FEDERATION_FLEET_IDENTITY_COLLISION")
                if existing["identity_digest"] != identity_digest or existing["identity_json"] != identity_json:
                    raise FederationTrustError("FEDERATION_PEER_IDENTITY_CONFLICT")
                if existing["metadata_digest"] != metadata_digest or existing["metadata_json"] != metadata_json:
                    raise FederationTrustError("FEDERATION_PEER_METADATA_CONFLICT")
                return self._record(existing)

            root_owner = connection.execute(
                "SELECT peer_fleet_id FROM federation_peer_trust WHERE root_fingerprint = ?",
                (identity["rootFingerprint"],),
            ).fetchone()
            if root_owner is not None:
                raise FederationTrustError("FEDERATION_FLEET_IDENTITY_COLLISION")

            connection.execute(
                """
                INSERT INTO federation_peer_trust (
                    peer_fleet_id, root_key_id, root_fingerprint,
                    identity_digest, identity_json, metadata_digest, metadata_json,
                    state, pinned_by, state_reason, pinned_at, revision, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    peer_fleet_id,
                    identity["rootKeyId"],
                    identity["rootFingerprint"],
                    identity_digest,
                    identity_json,
                    metadata_digest,
                    metadata_json,
                    STATE_PENDING,
                    actor,
                    "operator-pinned-root",
                    timestamp,
                    timestamp,
                ),
            )
            self._append_event(
                connection,
                peer_fleet_id=peer_fleet_id,
                event_type="ROOT_PINNED",
                previous_state=None,
                next_state=STATE_PENDING,
                actor=actor,
                reason="operator-pinned-root",
                occurred_at=timestamp,
            )
            created = self._peer_row(connection, peer_fleet_id)
            assert created is not None
            return self._record(created)

    def _transition(
        self,
        peer_fleet_id: str,
        *,
        allowed_from: frozenset[str],
        next_state: str,
        event_type: str,
        actor: str,
        reason: str,
        now: datetime | None,
    ) -> dict[str, Any]:
        peer = _fleet_id(peer_fleet_id)
        normalized_actor = _audit_text(actor, code="FEDERATION_TRUST_ACTOR_INVALID")
        normalized_reason = _audit_text(reason, code="FEDERATION_TRUST_REASON_INVALID")
        timestamp = _utc_iso(now)
        timestamp_column = {
            STATE_VERIFIED: "verified_at",
            STATE_ACTIVE: "activated_at",
            STATE_SUSPENDED: "suspended_at",
            STATE_REVOKED: "revoked_at",
        }[next_state]
        with self._write() as connection:
            row = self._peer_row(connection, peer)
            if row is None:
                raise FederationTrustError("FEDERATION_PEER_NOT_PINNED")
            current_state = str(row["state"])
            if current_state == STATE_REVOKED:
                if next_state == STATE_REVOKED:
                    return self._record(row)
                raise FederationTrustError("FEDERATION_PEER_REVOKED")
            if current_state == next_state:
                return self._record(row)
            if current_state not in allowed_from:
                if next_state == STATE_ACTIVE and current_state == STATE_PENDING:
                    raise FederationTrustError("FEDERATION_PEER_NOT_VERIFIED")
                raise FederationTrustError("FEDERATION_PEER_STATE_TRANSITION_INVALID")
            connection.execute(
                f"""
                UPDATE federation_peer_trust
                SET state = ?, state_reason = ?, {timestamp_column} = COALESCE({timestamp_column}, ?),
                    revision = revision + 1, updated_at = ?
                WHERE peer_fleet_id = ? AND state = ?
                """,
                (next_state, normalized_reason, timestamp, timestamp, peer, current_state),
            )
            self._append_event(
                connection,
                peer_fleet_id=peer,
                event_type=event_type,
                previous_state=current_state,
                next_state=next_state,
                actor=normalized_actor,
                reason=normalized_reason,
                occurred_at=timestamp,
            )
            updated = self._peer_row(connection, peer)
            assert updated is not None
            return self._record(updated)

    def verify_peer(
        self,
        peer_fleet_id: str,
        presented_identity: dict[str, Any],
        *,
        actor: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        peer = _fleet_id(peer_fleet_id)
        identity = _identity(presented_identity)
        if identity["fleetId"] != peer:
            raise FederationTrustError("FEDERATION_PEER_IDENTITY_MISMATCH")
        with closing(self._connect()) as connection:
            row = self._peer_row(connection, peer)
        if row is None:
            raise FederationTrustError("FEDERATION_PEER_NOT_PINNED")
        if row["identity_digest"] != _digest(identity) or row["identity_json"] != _canonical_json(identity):
            raise FederationTrustError("FEDERATION_PEER_IDENTITY_MISMATCH")
        return self._transition(
            peer,
            allowed_from=frozenset({STATE_PENDING}),
            next_state=STATE_VERIFIED,
            event_type="IDENTITY_VERIFIED",
            actor=actor,
            reason="pinned-root-identity-verified",
            now=now,
        )

    def activate_peer(self, peer_fleet_id: str, *, actor: str, now: datetime | None = None) -> dict[str, Any]:
        return self._transition(
            peer_fleet_id,
            allowed_from=frozenset({STATE_VERIFIED}),
            next_state=STATE_ACTIVE,
            event_type="PEER_ACTIVATED",
            actor=actor,
            reason="operator-activated",
            now=now,
        )

    def suspend_peer(
        self,
        peer_fleet_id: str,
        *,
        actor: str,
        reason: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            peer_fleet_id,
            allowed_from=frozenset({STATE_ACTIVE}),
            next_state=STATE_SUSPENDED,
            event_type="PEER_SUSPENDED",
            actor=actor,
            reason=reason,
            now=now,
        )

    def revoke_peer(
        self,
        peer_fleet_id: str,
        *,
        actor: str,
        reason: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            peer_fleet_id,
            allowed_from=frozenset({STATE_PENDING, STATE_VERIFIED, STATE_ACTIVE, STATE_SUSPENDED}),
            next_state=STATE_REVOKED,
            event_type="PEER_REVOKED",
            actor=actor,
            reason=reason,
            now=now,
        )

    def accept_online_signer(
        self,
        peer_fleet_id: str,
        certificate: dict[str, Any],
        *,
        actor: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Accept one currently valid root-certified signer with monotonic rotation."""

        peer = _fleet_id(peer_fleet_id)
        normalized_actor = _audit_text(actor, code="FEDERATION_TRUST_ACTOR_INVALID")
        timestamp = _utc_iso(now)
        validation_time = _stored_timestamp(timestamp, code="FEDERATION_TRUST_TIMESTAMP_INVALID")
        with self._write() as connection:
            peer_row = self._peer_row(connection, peer)
            if peer_row is None:
                raise FederationTrustError("FEDERATION_PEER_NOT_PINNED")
            peer_state = str(peer_row["state"])
            if peer_state == STATE_REVOKED:
                raise FederationTrustError("FEDERATION_PEER_REVOKED")
            if peer_state != STATE_ACTIVE:
                raise FederationTrustError("FEDERATION_PEER_NOT_ACTIVE")
            root_identity = json.loads(str(peer_row["identity_json"]))
            certificate_errors = federation_identity.validate_online_signer_certificate(
                certificate,
                root_identity,
                now=validation_time,
            )
            if certificate_errors:
                raise FederationTrustError(certificate_errors[0])
            normalized_certificate = json.loads(_canonical_json(certificate))
            signer_key_id = _signer_key_id(normalized_certificate.get("signerKeyId"))
            sequence = normalized_certificate.get("sequence")
            assert isinstance(sequence, int) and not isinstance(sequence, bool)
            certificate_json = _canonical_json(normalized_certificate)
            certificate_digest = _digest(normalized_certificate)

            existing = self._signer_row(connection, peer, signer_key_id)
            if existing is not None:
                if existing["certificate_digest"] != certificate_digest or existing["certificate_json"] != certificate_json:
                    raise FederationTrustError("FEDERATION_SIGNER_IDENTITY_CONFLICT")
                return self._signer_record(existing)

            key_owner = connection.execute(
                "SELECT peer_fleet_id FROM federation_online_signers WHERE signer_key_id = ?",
                (signer_key_id,),
            ).fetchone()
            if key_owner is not None:
                raise FederationTrustError("FEDERATION_SIGNER_FLEET_COLLISION")
            sequence_owner = connection.execute(
                """
                SELECT signer_key_id FROM federation_online_signers
                WHERE peer_fleet_id = ? AND certificate_sequence = ?
                """,
                (peer, sequence),
            ).fetchone()
            if sequence_owner is not None:
                raise FederationTrustError("FEDERATION_SIGNER_SEQUENCE_CONFLICT")
            high_water = connection.execute(
                """
                SELECT MAX(certificate_sequence) AS maximum_sequence
                FROM federation_online_signers WHERE peer_fleet_id = ?
                """,
                (peer,),
            ).fetchone()
            maximum_sequence = None if high_water is None else high_water["maximum_sequence"]
            if maximum_sequence is not None and sequence < int(maximum_sequence):
                raise FederationTrustError("FEDERATION_SIGNER_SEQUENCE_REPLAY")

            connection.execute(
                """
                INSERT INTO federation_online_signers (
                    peer_fleet_id, signer_key_id, certificate_sequence,
                    certificate_digest, certificate_json, accepted_by,
                    accepted_at, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    peer,
                    signer_key_id,
                    sequence,
                    certificate_digest,
                    certificate_json,
                    normalized_actor,
                    timestamp,
                ),
            )
            self._append_signer_event(
                connection,
                peer_fleet_id=peer,
                signer_key_id=signer_key_id,
                event_type="SIGNER_CERTIFICATE_ACCEPTED",
                actor=normalized_actor,
                reason="pinned-root-certificate-accepted",
                occurred_at=timestamp,
            )
            created = self._signer_row(connection, peer, signer_key_id)
            assert created is not None
            return self._signer_record(created)

    def authorize_online_signer(
        self,
        peer_fleet_id: str,
        certificate: dict[str, Any],
        *,
        purpose: str,
        mode: str,
        validation_time: datetime | None = None,
        signed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Authorize a signer for a current action or an explicitly historical proof."""

        peer = _fleet_id(peer_fleet_id)
        if type(mode) is not str or mode not in AUTHORIZATION_MODES:
            raise FederationTrustError("FEDERATION_SIGNER_AUTHORIZATION_MODE_INVALID")
        validated_at_iso = _utc_iso(validation_time)
        validated_at = _stored_timestamp(validated_at_iso, code="FEDERATION_TRUST_TIMESTAMP_INVALID")
        if mode == AUTHORIZATION_CURRENT:
            if signed_at is not None:
                raise FederationTrustError("FEDERATION_SIGNER_HISTORICAL_TIME_UNEXPECTED")
            authorization_at = validated_at
            authorization_at_iso = validated_at_iso
        else:
            if signed_at is None:
                raise FederationTrustError("FEDERATION_SIGNER_HISTORICAL_TIME_REQUIRED")
            authorization_at_iso = _utc_iso(signed_at)
            authorization_at = _stored_timestamp(
                authorization_at_iso,
                code="FEDERATION_SIGNER_HISTORICAL_TIME_INVALID",
            )

        with closing(self._connect()) as connection:
            peer_row = self._peer_row(connection, peer)
            if peer_row is None:
                raise FederationTrustError("FEDERATION_PEER_NOT_PINNED")
            peer_state = str(peer_row["state"])
            if mode == AUTHORIZATION_CURRENT:
                if peer_state == STATE_REVOKED:
                    raise FederationTrustError("FEDERATION_PEER_REVOKED")
                if peer_state != STATE_ACTIVE:
                    raise FederationTrustError("FEDERATION_PEER_NOT_ACTIVE")
            root_identity = json.loads(str(peer_row["identity_json"]))
            certificate_errors = federation_identity.validate_online_signer_certificate(
                certificate,
                root_identity,
                now=authorization_at,
                required_purpose=purpose,
            )
            if certificate_errors:
                raise FederationTrustError(certificate_errors[0])
            normalized_certificate = json.loads(_canonical_json(certificate))
            signer_key_id = _signer_key_id(normalized_certificate.get("signerKeyId"))
            signer_row = self._signer_row(connection, peer, signer_key_id)
            if signer_row is None:
                raise FederationTrustError("FEDERATION_SIGNER_NOT_ACCEPTED")
            if (
                signer_row["certificate_digest"] != _digest(normalized_certificate)
                or signer_row["certificate_json"] != _canonical_json(normalized_certificate)
            ):
                raise FederationTrustError("FEDERATION_SIGNER_CERTIFICATE_CONFLICT")

            revoked_at = (
                None
                if signer_row["revoked_at"] is None
                else _stored_timestamp(
                    signer_row["revoked_at"],
                    code="FEDERATION_SIGNER_REVOCATION_TIMESTAMP_INVALID",
                )
            )
            if mode == AUTHORIZATION_CURRENT:
                if revoked_at is not None and validated_at >= revoked_at:
                    raise FederationTrustError("FEDERATION_SIGNER_REVOKED")
            else:
                activated_at = (
                    None
                    if peer_row["activated_at"] is None
                    else _stored_timestamp(
                        peer_row["activated_at"],
                        code="FEDERATION_PEER_ACTIVATION_TIMESTAMP_INVALID",
                    )
                )
                if activated_at is None or authorization_at < activated_at:
                    raise FederationTrustError("FEDERATION_ROOT_NOT_ACTIVE_AT_SIGNING_TIME")
                suspended_at = (
                    None
                    if peer_row["suspended_at"] is None
                    else _stored_timestamp(
                        peer_row["suspended_at"],
                        code="FEDERATION_PEER_SUSPENSION_TIMESTAMP_INVALID",
                    )
                )
                root_revoked_at = (
                    None
                    if peer_row["revoked_at"] is None
                    else _stored_timestamp(
                        peer_row["revoked_at"],
                        code="FEDERATION_PEER_REVOCATION_TIMESTAMP_INVALID",
                    )
                )
                if root_revoked_at is not None and authorization_at >= root_revoked_at:
                    raise FederationTrustError("FEDERATION_ROOT_REVOKED_AT_SIGNING_TIME")
                if suspended_at is not None and authorization_at >= suspended_at:
                    raise FederationTrustError("FEDERATION_ROOT_SUSPENDED_AT_SIGNING_TIME")
                if revoked_at is not None and authorization_at >= revoked_at:
                    raise FederationTrustError("FEDERATION_SIGNER_REVOKED_AT_SIGNING_TIME")

            authorized = self._signer_record(signer_row)
            authorized["authorizationMode"] = mode
            authorized["validatedAt"] = validated_at_iso
            if mode == AUTHORIZATION_HISTORICAL_PROOF:
                authorized["historicalAuthorizationAt"] = authorization_at_iso
            return authorized

    def revoke_online_signer(
        self,
        peer_fleet_id: str,
        signer_key_id: str,
        *,
        actor: str,
        reason: str,
        revoked_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Durably revoke an accepted signer at an explicit effective time."""

        peer = _fleet_id(peer_fleet_id)
        signer = _signer_key_id(signer_key_id)
        normalized_actor = _audit_text(actor, code="FEDERATION_TRUST_ACTOR_INVALID")
        normalized_reason = _audit_text(reason, code="FEDERATION_TRUST_REASON_INVALID")
        timestamp = _utc_iso(revoked_at)
        with self._write() as connection:
            peer_row = self._peer_row(connection, peer)
            if peer_row is None:
                raise FederationTrustError("FEDERATION_PEER_NOT_PINNED")
            row = self._signer_row(connection, peer, signer)
            if row is None:
                raise FederationTrustError("FEDERATION_SIGNER_NOT_ACCEPTED")
            if row["revoked_at"] is not None:
                return self._signer_record(row)
            connection.execute(
                """
                UPDATE federation_online_signers
                SET revoked_at = ?, revoked_by = ?, revocation_reason = ?,
                    revision = revision + 1
                WHERE peer_fleet_id = ? AND signer_key_id = ? AND revoked_at IS NULL
                """,
                (timestamp, normalized_actor, normalized_reason, peer, signer),
            )
            self._append_signer_event(
                connection,
                peer_fleet_id=peer,
                signer_key_id=signer,
                event_type="SIGNER_REVOKED",
                actor=normalized_actor,
                reason=normalized_reason,
                occurred_at=timestamp,
            )
            updated = self._signer_row(connection, peer, signer)
            assert updated is not None
            return self._signer_record(updated)

    def get_online_signer(self, peer_fleet_id: str, signer_key_id: str) -> dict[str, Any] | None:
        peer = _fleet_id(peer_fleet_id)
        signer = _signer_key_id(signer_key_id)
        with closing(self._connect()) as connection:
            row = self._signer_row(connection, peer, signer)
        return None if row is None else self._signer_record(row)

    def list_online_signers(self, peer_fleet_id: str) -> list[dict[str, Any]]:
        peer = _fleet_id(peer_fleet_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM federation_online_signers
                WHERE peer_fleet_id = ? ORDER BY certificate_sequence
                """,
                (peer,),
            ).fetchall()
        return [self._signer_record(row) for row in rows]

    def list_online_signer_events(self, peer_fleet_id: str) -> list[dict[str, Any]]:
        peer = _fleet_id(peer_fleet_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT event_sequence, peer_fleet_id, signer_key_id,
                       event_type, actor, reason, occurred_at
                FROM federation_online_signer_events
                WHERE peer_fleet_id = ? ORDER BY event_sequence
                """,
                (peer,),
            ).fetchall()
        return [
            {
                "sequence": int(row["event_sequence"]),
                "peerFleetId": str(row["peer_fleet_id"]),
                "signerKeyId": str(row["signer_key_id"]),
                "eventType": str(row["event_type"]),
                "actor": str(row["actor"]),
                "reason": str(row["reason"]),
                "occurredAt": str(row["occurred_at"]),
            }
            for row in rows
        ]

    def record_readiness_sequence(
        self,
        peer_fleet_id: str,
        *,
        signer_key_id: str,
        sequence: int,
        attestation_digest: str,
        accepted_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically fence signed-readiness replay while rechecking current trust."""

        peer = _fleet_id(peer_fleet_id)
        signer = _signer_key_id(signer_key_id)
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise FederationTrustError("FEDERATION_READINESS_SEQUENCE_INVALID")
        digest = _sha256_digest(
            attestation_digest,
            code="FEDERATION_READINESS_ATTESTATION_DIGEST_INVALID",
        )
        timestamp = _utc_iso(accepted_at)
        validation_time = _stored_timestamp(timestamp, code="FEDERATION_TRUST_TIMESTAMP_INVALID")
        with self._write() as connection:
            self._require_active_signer_in_tx(
                connection,
                peer_fleet_id=peer,
                signer_key_id=signer,
                validation_time=validation_time,
                required_purpose=federation_identity.PURPOSE_READINESS_ATTESTATION,
            )
            existing = connection.execute(
                "SELECT * FROM federation_readiness_sequences WHERE peer_fleet_id = ?",
                (peer,),
            ).fetchone()
            if existing is not None and sequence <= int(existing["high_sequence"]):
                if sequence == int(existing["high_sequence"]) and digest != existing["attestation_digest"]:
                    raise FederationTrustError("FEDERATION_READINESS_SEQUENCE_CONFLICT")
                raise FederationTrustError("FEDERATION_READINESS_SEQUENCE_REPLAY")
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO federation_readiness_sequences (
                        peer_fleet_id, high_sequence, signer_key_id,
                        attestation_digest, accepted_at, revision
                    ) VALUES (?, ?, ?, ?, ?, 1)
                    """,
                    (peer, sequence, signer, digest, timestamp),
                )
            else:
                connection.execute(
                    """
                    UPDATE federation_readiness_sequences
                    SET high_sequence = ?, signer_key_id = ?, attestation_digest = ?,
                        accepted_at = ?, revision = revision + 1
                    WHERE peer_fleet_id = ? AND high_sequence < ?
                    """,
                    (sequence, signer, digest, timestamp, peer, sequence),
                )
            recorded = connection.execute(
                "SELECT * FROM federation_readiness_sequences WHERE peer_fleet_id = ?",
                (peer,),
            ).fetchone()
            assert recorded is not None
            return self._readiness_sequence_record(recorded)

    def get_readiness_high_water(self, peer_fleet_id: str) -> dict[str, Any] | None:
        peer = _fleet_id(peer_fleet_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM federation_readiness_sequences WHERE peer_fleet_id = ?",
                (peer,),
            ).fetchone()
        return None if row is None else self._readiness_sequence_record(row)

    def record_outbound_federation_challenge(
        self,
        peer_fleet_id: str,
        *,
        nonce_digest: str,
        challenge_digest: str,
        challenge: dict[str, Any],
        session_purpose: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> dict[str, Any]:
        peer = _fleet_id(peer_fleet_id)
        nonce = _sha256_digest(nonce_digest, code="FEDERATION_CHALLENGE_NONCE_DIGEST_INVALID")
        digest = _sha256_digest(challenge_digest, code="FEDERATION_CHALLENGE_DIGEST_INVALID")
        purpose = _audit_text(session_purpose, code="FEDERATION_CHALLENGE_PURPOSE_INVALID")
        if type(challenge) is not dict:
            raise FederationTrustError("FEDERATION_CHALLENGE_DOCUMENT_INVALID")
        source = _fleet_id(challenge.get("sourceFleetId"))
        destination = _fleet_id(challenge.get("destinationFleetId"))
        if source != self._local_identity["fleetId"] or destination != peer:
            raise FederationTrustError("FEDERATION_CHALLENGE_FLEET_BINDING_INVALID")
        issued_at_iso = _utc_iso(issued_at)
        expires_at_iso = _utc_iso(expires_at)
        if _stored_timestamp(expires_at_iso, code="FEDERATION_CHALLENGE_TIMESTAMP_INVALID") <= _stored_timestamp(
            issued_at_iso,
            code="FEDERATION_CHALLENGE_TIMESTAMP_INVALID",
        ):
            raise FederationTrustError("FEDERATION_CHALLENGE_WINDOW_INVALID")
        if (
            challenge.get("fleetId") != source
            or challenge.get("sessionPurpose") != purpose
            or challenge.get("issuedAt") != issued_at_iso
            or challenge.get("expiresAt") != expires_at_iso
            or _digest(challenge) != digest
            or _challenge_nonce_digest(challenge.get("nonce")) != nonce
        ):
            raise FederationTrustError("FEDERATION_CHALLENGE_IDENTITY_CONFLICT")
        challenge_json = _canonical_json(challenge)
        with self._write() as connection:
            peer_row = self._peer_row(connection, peer)
            if peer_row is None:
                raise FederationTrustError("FEDERATION_PEER_NOT_PINNED")
            if peer_row["state"] == STATE_REVOKED:
                raise FederationTrustError("FEDERATION_PEER_REVOKED")
            if peer_row["state"] != STATE_ACTIVE:
                raise FederationTrustError("FEDERATION_PEER_NOT_ACTIVE")
            self._verify_signed_document(
                challenge,
                certificate=challenge.get("signerCertificate"),
                root_identity=self._local_identity,
                expected_schema="federation-challenge-v1",
                validation_time=_stored_timestamp(issued_at_iso, code="FEDERATION_CHALLENGE_TIMESTAMP_INVALID"),
            )
            duplicate = connection.execute(
                "SELECT state FROM federation_challenge_journal WHERE nonce_digest = ?",
                (nonce,),
            ).fetchone()
            if duplicate is not None:
                raise FederationTrustError("FEDERATION_CHALLENGE_NONCE_REPLAY")
            connection.execute(
                """
                INSERT INTO federation_challenge_journal (
                    direction, nonce_digest, peer_fleet_id, source_fleet_id,
                    destination_fleet_id, session_purpose, challenge_digest,
                    challenge_json, state, issued_at, expires_at, recorded_at, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    CHALLENGE_DIRECTION_OUTBOUND,
                    nonce,
                    peer,
                    source,
                    destination,
                    purpose,
                    digest,
                    challenge_json,
                    CHALLENGE_STATE_PENDING,
                    issued_at_iso,
                    expires_at_iso,
                    issued_at_iso,
                ),
            )
            row = connection.execute(
                "SELECT * FROM federation_challenge_journal WHERE direction = ? AND nonce_digest = ?",
                (CHALLENGE_DIRECTION_OUTBOUND, nonce),
            ).fetchone()
            assert row is not None
            return self._challenge_record(row)

    def record_inbound_federation_response(
        self,
        peer_fleet_id: str,
        *,
        peer_signer_key_id: str,
        nonce_digest: str,
        challenge_digest: str,
        challenge: dict[str, Any],
        response_digest: str,
        response: dict[str, Any],
        session_purpose: str,
        issued_at: datetime,
        expires_at: datetime,
        responded_at: datetime,
    ) -> dict[str, Any]:
        peer = _fleet_id(peer_fleet_id)
        signer = _signer_key_id(peer_signer_key_id)
        nonce = _sha256_digest(nonce_digest, code="FEDERATION_CHALLENGE_NONCE_DIGEST_INVALID")
        challenge_hash = _sha256_digest(challenge_digest, code="FEDERATION_CHALLENGE_DIGEST_INVALID")
        response_hash = _sha256_digest(response_digest, code="FEDERATION_CHALLENGE_RESPONSE_DIGEST_INVALID")
        purpose = _audit_text(session_purpose, code="FEDERATION_CHALLENGE_PURPOSE_INVALID")
        if type(challenge) is not dict or type(response) is not dict:
            raise FederationTrustError("FEDERATION_CHALLENGE_DOCUMENT_INVALID")
        source = _fleet_id(challenge.get("sourceFleetId"))
        destination = _fleet_id(challenge.get("destinationFleetId"))
        if source != peer or destination != self._local_identity["fleetId"]:
            raise FederationTrustError("FEDERATION_CHALLENGE_FLEET_BINDING_INVALID")
        issued_at_iso = _utc_iso(issued_at)
        expires_at_iso = _utc_iso(expires_at)
        responded_at_iso = _utc_iso(responded_at)
        validation_time = _stored_timestamp(responded_at_iso, code="FEDERATION_CHALLENGE_TIMESTAMP_INVALID")
        issued_time = _stored_timestamp(issued_at_iso, code="FEDERATION_CHALLENGE_TIMESTAMP_INVALID")
        expires_time = _stored_timestamp(expires_at_iso, code="FEDERATION_CHALLENGE_TIMESTAMP_INVALID")
        if not (issued_time <= validation_time < expires_time):
            raise FederationTrustError("FEDERATION_CHALLENGE_RESPONSE_WINDOW_INVALID")
        if (
            challenge.get("fleetId") != source
            or challenge.get("sessionPurpose") != purpose
            or challenge.get("issuedAt") != issued_at_iso
            or challenge.get("expiresAt") != expires_at_iso
            or _digest(challenge) != challenge_hash
            or _challenge_nonce_digest(challenge.get("nonce")) != nonce
            or response.get("fleetId") != destination
            or response.get("sourceFleetId") != source
            or response.get("destinationFleetId") != destination
            or response.get("sessionPurpose") != purpose
            or response.get("challengeDigest") != challenge_hash
            or response.get("respondedAt") != responded_at_iso
            or response.get("expiresAt") != expires_at_iso
            or _digest(response) != response_hash
            or _challenge_nonce_digest(response.get("nonce")) != nonce
        ):
            raise FederationTrustError("FEDERATION_CHALLENGE_IDENTITY_CONFLICT")
        with self._write() as connection:
            peer_row, _ = self._require_active_signer_in_tx(
                connection,
                peer_fleet_id=peer,
                signer_key_id=signer,
                validation_time=validation_time,
                required_purpose=federation_identity.PURPOSE_SESSION_AUTHENTICATION,
            )
            peer_identity = json.loads(str(peer_row["identity_json"]))
            self._verify_signed_document(
                challenge,
                certificate=challenge.get("signerCertificate"),
                root_identity=peer_identity,
                expected_schema="federation-challenge-v1",
                validation_time=validation_time,
            )
            self._verify_signed_document(
                response,
                certificate=response.get("signerCertificate"),
                root_identity=self._local_identity,
                expected_schema="federation-challenge-response-v1",
                validation_time=validation_time,
            )
            duplicate = connection.execute(
                "SELECT state FROM federation_challenge_journal WHERE nonce_digest = ?",
                (nonce,),
            ).fetchone()
            if duplicate is not None:
                raise FederationTrustError("FEDERATION_CHALLENGE_NONCE_REPLAY")
            connection.execute(
                """
                INSERT INTO federation_challenge_journal (
                    direction, nonce_digest, peer_fleet_id, source_fleet_id,
                    destination_fleet_id, session_purpose, challenge_digest,
                    challenge_json, response_digest, response_json, state,
                    issued_at, expires_at, recorded_at, completed_at, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    CHALLENGE_DIRECTION_INBOUND,
                    nonce,
                    peer,
                    source,
                    destination,
                    purpose,
                    challenge_hash,
                    _canonical_json(challenge),
                    response_hash,
                    _canonical_json(response),
                    CHALLENGE_STATE_RESPONDED,
                    issued_at_iso,
                    expires_at_iso,
                    responded_at_iso,
                    responded_at_iso,
                ),
            )
            row = connection.execute(
                "SELECT * FROM federation_challenge_journal WHERE direction = ? AND nonce_digest = ?",
                (CHALLENGE_DIRECTION_INBOUND, nonce),
            ).fetchone()
            assert row is not None
            return self._challenge_record(row)

    def consume_outbound_federation_response(
        self,
        peer_fleet_id: str,
        *,
        peer_signer_key_id: str,
        nonce_digest: str,
        challenge_digest: str,
        response_digest: str,
        response: dict[str, Any],
        consumed_at: datetime,
    ) -> dict[str, Any]:
        peer = _fleet_id(peer_fleet_id)
        signer = _signer_key_id(peer_signer_key_id)
        nonce = _sha256_digest(nonce_digest, code="FEDERATION_CHALLENGE_NONCE_DIGEST_INVALID")
        challenge_hash = _sha256_digest(challenge_digest, code="FEDERATION_CHALLENGE_DIGEST_INVALID")
        response_hash = _sha256_digest(response_digest, code="FEDERATION_CHALLENGE_RESPONSE_DIGEST_INVALID")
        if type(response) is not dict:
            raise FederationTrustError("FEDERATION_CHALLENGE_DOCUMENT_INVALID")
        consumed_at_iso = _utc_iso(consumed_at)
        validation_time = _stored_timestamp(consumed_at_iso, code="FEDERATION_CHALLENGE_TIMESTAMP_INVALID")
        with self._write() as connection:
            peer_row, _ = self._require_active_signer_in_tx(
                connection,
                peer_fleet_id=peer,
                signer_key_id=signer,
                validation_time=validation_time,
                required_purpose=federation_identity.PURPOSE_SESSION_AUTHENTICATION,
            )
            peer_identity = json.loads(str(peer_row["identity_json"]))
            self._verify_signed_document(
                response,
                certificate=response.get("signerCertificate"),
                root_identity=peer_identity,
                expected_schema="federation-challenge-response-v1",
                validation_time=validation_time,
            )
            row = connection.execute(
                "SELECT * FROM federation_challenge_journal WHERE direction = ? AND nonce_digest = ?",
                (CHALLENGE_DIRECTION_OUTBOUND, nonce),
            ).fetchone()
            if row is None:
                raise FederationTrustError("FEDERATION_CHALLENGE_NONCE_UNKNOWN")
            if row["state"] != CHALLENGE_STATE_PENDING:
                raise FederationTrustError("FEDERATION_CHALLENGE_NONCE_REPLAY")
            if (
                row["peer_fleet_id"] != peer
                or row["challenge_digest"] != challenge_hash
                or response.get("fleetId") != row["destination_fleet_id"]
                or response.get("sourceFleetId") != row["source_fleet_id"]
                or response.get("destinationFleetId") != row["destination_fleet_id"]
                or response.get("sessionPurpose") != row["session_purpose"]
                or response.get("challengeDigest") != challenge_hash
                or response.get("expiresAt") != row["expires_at"]
                or _digest(response) != response_hash
                or _challenge_nonce_digest(response.get("nonce")) != nonce
            ):
                raise FederationTrustError("FEDERATION_CHALLENGE_IDENTITY_CONFLICT")
            connection.execute(
                """
                UPDATE federation_challenge_journal
                SET response_digest = ?, response_json = ?, state = ?,
                    completed_at = ?, revision = revision + 1
                WHERE direction = ? AND nonce_digest = ? AND state = ?
                """,
                (
                    response_hash,
                    _canonical_json(response),
                    CHALLENGE_STATE_CONSUMED,
                    consumed_at_iso,
                    CHALLENGE_DIRECTION_OUTBOUND,
                    nonce,
                    CHALLENGE_STATE_PENDING,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM federation_challenge_journal WHERE direction = ? AND nonce_digest = ?",
                (CHALLENGE_DIRECTION_OUTBOUND, nonce),
            ).fetchone()
            assert updated is not None
            return self._challenge_record(updated)

    def record_ingress_grant(
        self,
        grant: dict[str, Any],
        *,
        recorded_at: datetime,
    ) -> dict[str, Any]:
        """Atomically bind one receiver-issued grant to one authenticated session."""

        if type(grant) is not dict:
            raise FederationTrustError("FEDERATION_INGRESS_GRANT_DOCUMENT_INVALID")
        try:
            normalized_value = federation_identity.normalize_federation_json(grant)
        except federation_identity.FederationIdentityError as exc:
            raise FederationTrustError("FEDERATION_INGRESS_GRANT_DOCUMENT_INVALID") from exc
        if type(normalized_value) is not dict or set(normalized_value) != _INGRESS_GRANT_FIELDS:
            raise FederationTrustError("FEDERATION_INGRESS_GRANT_FIELDS_INVALID")
        normalized = normalized_value
        if normalized.get("schema") != "federation-ingress-grant-v1":
            raise FederationTrustError("FEDERATION_INGRESS_GRANT_SCHEMA_INVALID")

        local_fleet_id = str(self._local_identity["fleetId"])
        grant_id = _grant_id(normalized.get("grantId"))
        source = _fleet_id(normalized.get("sourceFleetId"))
        destination = _fleet_id(normalized.get("destinationFleetId"))
        if normalized.get("fleetId") != destination or destination != local_fleet_id or source == destination:
            raise FederationTrustError("FEDERATION_INGRESS_GRANT_FLEET_BINDING_INVALID")
        transfer = _sha256_digest(
            normalized.get("transferId"),
            code="FEDERATION_INGRESS_TRANSFER_ID_INVALID",
        )
        policy = _control_id(normalized.get("policyId"), code="FEDERATION_INGRESS_POLICY_ID_INVALID")
        backup = _control_id(normalized.get("backupId"), code="FEDERATION_INGRESS_BACKUP_ID_INVALID")
        object_set = _sha256_digest(
            normalized.get("objectSetDigest"),
            code="FEDERATION_INGRESS_OBJECT_SET_DIGEST_INVALID",
        )
        try:
            derived_transfer = federation_transfer_journal.derive_transfer_id(
                source_fleet_id=source,
                destination_fleet_id=destination,
                backup_id=backup,
                object_set_digest=object_set,
            )
        except federation_transfer_journal.FederatedTransferJournalError as exc:
            raise FederationTrustError(exc.code) from exc
        if transfer != derived_transfer:
            raise FederationTrustError("FEDERATION_TRANSFER_ID_INVALID")
        prefix = _object_prefix(normalized.get("allowedObjectPrefix"))
        max_bytes = normalized.get("maxBytes")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not (0 < max_bytes <= (1 << 63) - 1)
        ):
            raise FederationTrustError("FEDERATION_INGRESS_MAX_BYTES_INVALID")
        session_nonce = _sha256_digest(
            normalized.get("sessionNonceDigest"),
            code="FEDERATION_INGRESS_SESSION_NONCE_DIGEST_INVALID",
        )
        grant_nonce = _challenge_nonce_digest(normalized.get("nonce"))
        signer = _signer_key_id(normalized.get("signerKeyId"))
        issued_at = _stored_timestamp(
            normalized.get("issuedAt"),
            code="FEDERATION_INGRESS_TIMESTAMP_INVALID",
        )
        expires_at = _stored_timestamp(
            normalized.get("expiresAt"),
            code="FEDERATION_INGRESS_TIMESTAMP_INVALID",
        )
        recorded_at_iso = _utc_iso(recorded_at)
        recorded_time = _stored_timestamp(recorded_at_iso, code="FEDERATION_INGRESS_TIMESTAMP_INVALID")
        if (
            normalized.get("issuedAt") != _utc_iso(issued_at)
            or normalized.get("expiresAt") != _utc_iso(expires_at)
            or not (issued_at <= recorded_time < expires_at)
        ):
            raise FederationTrustError("FEDERATION_INGRESS_GRANT_WINDOW_INVALID")
        certificate = normalized.get("signerCertificate")
        if type(certificate) is not dict:
            raise FederationTrustError("FEDERATION_INGRESS_SIGNER_CERTIFICATE_INVALID")
        certificate_expires_at = _stored_timestamp(
            certificate.get("expiresAt"),
            code="FEDERATION_INGRESS_SIGNER_CERTIFICATE_INVALID",
        )
        if expires_at > certificate_expires_at:
            raise FederationTrustError("FEDERATION_INGRESS_SIGNER_WINDOW_INVALID")
        self._verify_signed_document(
            normalized,
            certificate=certificate,
            root_identity=self._local_identity,
            expected_schema="federation-ingress-grant-v1",
            validation_time=issued_at,
            required_purpose=federation_identity.PURPOSE_INGRESS_GRANT,
        )
        grant_json = _canonical_json(normalized)
        grant_digest = _digest(normalized)

        with self._write() as connection:
            peer_row = self._peer_row(connection, source)
            if peer_row is None:
                raise FederationTrustError("FEDERATION_PEER_NOT_PINNED")
            if peer_row["state"] == STATE_REVOKED:
                raise FederationTrustError("FEDERATION_PEER_REVOKED")
            if peer_row["state"] != STATE_ACTIVE:
                raise FederationTrustError("FEDERATION_PEER_NOT_ACTIVE")
            challenge_row = connection.execute(
                """
                SELECT * FROM federation_challenge_journal
                WHERE direction = ? AND nonce_digest = ?
                """,
                (CHALLENGE_DIRECTION_INBOUND, session_nonce),
            ).fetchone()
            if challenge_row is None:
                raise FederationTrustError("FEDERATION_INGRESS_SESSION_NOT_AUTHENTICATED")
            challenge_document = json.loads(str(challenge_row["challenge_json"]))
            challenge_certificate = challenge_document.get("signerCertificate")
            if type(challenge_certificate) is not dict:
                raise FederationTrustError("FEDERATION_CHALLENGE_SIGNER_CERTIFICATE_INVALID")
            challenge_signer = _signer_key_id(challenge_document.get("signerKeyId"))
            self._require_active_signer_in_tx(
                connection,
                peer_fleet_id=source,
                signer_key_id=challenge_signer,
                validation_time=recorded_time,
                required_purpose=federation_identity.PURPOSE_SESSION_AUTHENTICATION,
            )
            peer_identity = json.loads(str(peer_row["identity_json"]))
            self._verify_signed_document(
                challenge_document,
                certificate=challenge_certificate,
                root_identity=peer_identity,
                expected_schema="federation-challenge-v1",
                validation_time=recorded_time,
                required_purpose=federation_identity.PURPOSE_SESSION_AUTHENTICATION,
            )
            challenge_expires_at = _stored_timestamp(
                challenge_row["expires_at"],
                code="FEDERATION_CHALLENGE_TIMESTAMP_INVALID",
            )
            if (
                challenge_row["state"] != CHALLENGE_STATE_RESPONDED
                or challenge_row["peer_fleet_id"] != source
                or challenge_row["source_fleet_id"] != source
                or challenge_row["destination_fleet_id"] != destination
                or challenge_row["session_purpose"] != "REMOTE_CUSTODY"
                or recorded_time >= challenge_expires_at
                or expires_at > challenge_expires_at
            ):
                raise FederationTrustError("FEDERATION_INGRESS_SESSION_NOT_AUTHENTICATED")
            existing_session = connection.execute(
                "SELECT grant_id FROM federation_ingress_grants WHERE session_nonce_digest = ?",
                (session_nonce,),
            ).fetchone()
            if existing_session is not None:
                raise FederationTrustError("FEDERATION_INGRESS_SESSION_REPLAY")
            existing_id = connection.execute(
                "SELECT grant_digest FROM federation_ingress_grants WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            if existing_id is not None:
                raise FederationTrustError("FEDERATION_INGRESS_GRANT_IDENTITY_CONFLICT")
            existing_nonce = connection.execute(
                "SELECT grant_id FROM federation_ingress_grants WHERE grant_nonce_digest = ?",
                (grant_nonce,),
            ).fetchone()
            if existing_nonce is not None:
                raise FederationTrustError("FEDERATION_INGRESS_GRANT_NONCE_REPLAY")
            connection.execute(
                """
                INSERT INTO federation_ingress_grants (
                    grant_id, session_nonce_digest, grant_nonce_digest,
                    source_fleet_id, destination_fleet_id, transfer_id,
                    policy_id, backup_id, object_set_digest,
                    allowed_object_prefix, max_bytes, signer_key_id,
                    grant_digest, grant_json, state, bytes_reserved,
                    issued_at, expires_at, created_at, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 1)
                """,
                (
                    grant_id,
                    session_nonce,
                    grant_nonce,
                    source,
                    destination,
                    transfer,
                    policy,
                    backup,
                    object_set,
                    prefix,
                    max_bytes,
                    signer,
                    grant_digest,
                    grant_json,
                    INGRESS_GRANT_STATE_ACTIVE,
                    _utc_iso(issued_at),
                    _utc_iso(expires_at),
                    recorded_at_iso,
                ),
            )
            row = connection.execute(
                "SELECT * FROM federation_ingress_grants WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            assert row is not None
            return self._ingress_grant_record(row)

    def get_ingress_grant(self, grant_id: str) -> dict[str, Any] | None:
        normalized_id = _grant_id(grant_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM federation_ingress_grants WHERE grant_id = ?",
                (normalized_id,),
            ).fetchone()
        return None if row is None else self._ingress_grant_record(row)

    def get_ingress_grant_by_session_nonce(self, session_nonce_digest: str) -> dict[str, Any] | None:
        nonce = _sha256_digest(
            session_nonce_digest,
            code="FEDERATION_INGRESS_SESSION_NONCE_DIGEST_INVALID",
        )
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM federation_ingress_grants WHERE session_nonce_digest = ?",
                (nonce,),
            ).fetchone()
        return None if row is None else self._ingress_grant_record(row)

    def reserve_ingress_write(
        self,
        grant: dict[str, Any],
        *,
        source_fleet_id: str,
        write_id: str,
        object_key: str,
        byte_count: int,
        reserved_at: datetime,
    ) -> dict[str, Any]:
        """Atomically fence a scoped write identity and cumulative byte budget."""

        if type(grant) is not dict:
            raise FederationTrustError("FEDERATION_INGRESS_GRANT_DOCUMENT_INVALID")
        try:
            normalized_value = federation_identity.normalize_federation_json(grant)
        except federation_identity.FederationIdentityError as exc:
            raise FederationTrustError("FEDERATION_INGRESS_GRANT_DOCUMENT_INVALID") from exc
        if type(normalized_value) is not dict or set(normalized_value) != _INGRESS_GRANT_FIELDS:
            raise FederationTrustError("FEDERATION_INGRESS_GRANT_FIELDS_INVALID")
        normalized = normalized_value
        grant_id = _grant_id(normalized.get("grantId"))
        source = _fleet_id(source_fleet_id)
        normalized_write_id = _write_id(write_id)
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count <= 0:
            raise FederationTrustError("FEDERATION_INGRESS_BYTE_COUNT_INVALID")
        reserved_at_iso = _utc_iso(reserved_at)
        validation_time = _stored_timestamp(reserved_at_iso, code="FEDERATION_INGRESS_TIMESTAMP_INVALID")

        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM federation_ingress_grants WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            if row is None:
                raise FederationTrustError("FEDERATION_INGRESS_GRANT_NOT_FOUND")
            peer_row = self._peer_row(connection, source)
            if peer_row is None:
                raise FederationTrustError("FEDERATION_PEER_NOT_PINNED")
            if peer_row["state"] == STATE_REVOKED:
                raise FederationTrustError("FEDERATION_PEER_REVOKED")
            if peer_row["state"] != STATE_ACTIVE:
                raise FederationTrustError("FEDERATION_PEER_NOT_ACTIVE")
            if row["state"] != INGRESS_GRANT_STATE_ACTIVE:
                raise FederationTrustError("FEDERATION_INGRESS_GRANT_NOT_ACTIVE")
            if row["source_fleet_id"] != source or normalized.get("sourceFleetId") != source:
                raise FederationTrustError("FEDERATION_INGRESS_SOURCE_FLEET_MISMATCH")
            if (
                row["grant_digest"] != _digest(normalized)
                or row["grant_json"] != _canonical_json(normalized)
            ):
                raise FederationTrustError("FEDERATION_INGRESS_GRANT_IDENTITY_CONFLICT")
            expires_at = _stored_timestamp(
                row["expires_at"],
                code="FEDERATION_INGRESS_TIMESTAMP_INVALID",
            )
            issued_at = _stored_timestamp(
                row["issued_at"],
                code="FEDERATION_INGRESS_TIMESTAMP_INVALID",
            )
            if validation_time < issued_at:
                raise FederationTrustError("FEDERATION_INGRESS_GRANT_FROM_FUTURE")
            if validation_time >= expires_at:
                raise FederationTrustError("FEDERATION_INGRESS_GRANT_EXPIRED")
            certificate = normalized.get("signerCertificate")
            if type(certificate) is not dict:
                raise FederationTrustError("FEDERATION_INGRESS_SIGNER_CERTIFICATE_INVALID")
            self._verify_signed_document(
                normalized,
                certificate=certificate,
                root_identity=self._local_identity,
                expected_schema="federation-ingress-grant-v1",
                validation_time=validation_time,
                required_purpose=federation_identity.PURPOSE_INGRESS_GRANT,
            )
            prefix = _object_prefix(row["allowed_object_prefix"])
            normalized_key = _object_key(object_key, prefix=prefix)
            existing = connection.execute(
                """
                SELECT * FROM federation_ingress_writes
                WHERE grant_id = ? AND write_id = ?
                """,
                (grant_id, normalized_write_id),
            ).fetchone()
            if existing is not None:
                if existing["object_key"] != normalized_key or int(existing["byte_count"]) != byte_count:
                    raise FederationTrustError("FEDERATION_INGRESS_WRITE_IDENTITY_CONFLICT")
                return self._ingress_write_record(existing)
            existing_object = connection.execute(
                """
                SELECT * FROM federation_ingress_writes
                WHERE grant_id = ? AND object_key = ?
                """,
                (grant_id, normalized_key),
            ).fetchone()
            if existing_object is not None:
                raise FederationTrustError("FEDERATION_INGRESS_OBJECT_WRITE_IDENTITY_CONFLICT")
            bytes_reserved_after = int(row["bytes_reserved"]) + byte_count
            if bytes_reserved_after > int(row["max_bytes"]):
                raise FederationTrustError("FEDERATION_INGRESS_MAX_BYTES_EXCEEDED")
            connection.execute(
                """
                INSERT INTO federation_ingress_writes (
                    grant_id, write_id, object_key, byte_count,
                    bytes_reserved_after, reserved_at, revision
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    grant_id,
                    normalized_write_id,
                    normalized_key,
                    byte_count,
                    bytes_reserved_after,
                    reserved_at_iso,
                ),
            )
            connection.execute(
                """
                UPDATE federation_ingress_grants
                SET bytes_reserved = ?, revision = revision + 1
                WHERE grant_id = ? AND bytes_reserved = ?
                """,
                (bytes_reserved_after, grant_id, int(row["bytes_reserved"])),
            )
            created = connection.execute(
                """
                SELECT * FROM federation_ingress_writes
                WHERE grant_id = ? AND write_id = ?
                """,
                (grant_id, normalized_write_id),
            ).fetchone()
            assert created is not None
            return self._ingress_write_record(created)

    def list_ingress_writes(self, grant_id: str) -> list[dict[str, Any]]:
        normalized_id = _grant_id(grant_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM federation_ingress_writes
                WHERE grant_id = ? ORDER BY reserved_at, write_id
                """,
                (normalized_id,),
            ).fetchall()
        return [self._ingress_write_record(row) for row in rows]

    def get_federation_challenge(self, direction: str, nonce_digest: str) -> dict[str, Any] | None:
        normalized_direction = _challenge_direction(direction)
        nonce = _sha256_digest(nonce_digest, code="FEDERATION_CHALLENGE_NONCE_DIGEST_INVALID")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM federation_challenge_journal WHERE direction = ? AND nonce_digest = ?",
                (normalized_direction, nonce),
            ).fetchone()
        return None if row is None else self._challenge_record(row)

    def get_peer(self, peer_fleet_id: str) -> dict[str, Any] | None:
        peer = _fleet_id(peer_fleet_id)
        with closing(self._connect()) as connection:
            row = self._peer_row(connection, peer)
        return None if row is None else self._record(row)

    def list_peers(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM federation_peer_trust ORDER BY peer_fleet_id").fetchall()
        return [self._record(row) for row in rows]

    def assert_pinned_metadata(self, peer_fleet_id: str, claimed_metadata: dict[str, str]) -> dict[str, str]:
        record = self.get_peer(peer_fleet_id)
        if record is None:
            raise FederationTrustError("FEDERATION_PEER_NOT_PINNED")
        claimed = _metadata(claimed_metadata)
        pinned = record["pinnedMetadata"]
        if claimed != pinned or _digest(claimed) != record["metadataDigest"]:
            raise FederationTrustError("FEDERATION_PEER_METADATA_MISMATCH")
        assert isinstance(pinned, dict)
        return dict(pinned)

    def require_active_peer(
        self,
        peer_fleet_id: str,
        *,
        presented_identity: dict[str, Any] | None = None,
        claimed_metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        record = self.get_peer(peer_fleet_id)
        if record is None:
            raise FederationTrustError("FEDERATION_PEER_NOT_PINNED")
        if record["state"] == STATE_REVOKED:
            raise FederationTrustError("FEDERATION_PEER_REVOKED")
        if record["state"] != STATE_ACTIVE:
            raise FederationTrustError("FEDERATION_PEER_NOT_ACTIVE")
        if presented_identity is not None:
            identity = _identity(presented_identity)
            if identity != record["fleetIdentity"] or _digest(identity) != _digest(record["fleetIdentity"]):
                raise FederationTrustError("FEDERATION_PEER_IDENTITY_MISMATCH")
        if claimed_metadata is not None:
            self.assert_pinned_metadata(peer_fleet_id, claimed_metadata)
        return record

    def list_events(self, peer_fleet_id: str) -> list[dict[str, Any]]:
        peer = _fleet_id(peer_fleet_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT event_sequence, peer_fleet_id, event_type, previous_state,
                       next_state, actor, reason, occurred_at
                FROM federation_peer_trust_events
                WHERE peer_fleet_id = ?
                ORDER BY event_sequence
                """,
                (peer,),
            ).fetchall()
        return [
            {
                "sequence": int(row["event_sequence"]),
                "peerFleetId": str(row["peer_fleet_id"]),
                "eventType": str(row["event_type"]),
                "previousState": row["previous_state"],
                "nextState": str(row["next_state"]),
                "actor": str(row["actor"]),
                "reason": str(row["reason"]),
                "occurredAt": str(row["occurred_at"]),
            }
            for row in rows
        ]


def open_peer_trust_registry(
    local_identity: dict[str, Any],
    *,
    db_path: Path | None = None,
) -> PeerTrustRegistry:
    return PeerTrustRegistry(db_path or PEER_TRUST_DB, local_identity)

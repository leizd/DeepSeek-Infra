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
from deepseek_infra.infra.workspace import federation_identity

FEDERATION_DIR = config.ROOT / ".federation"
PEER_TRUST_DB = FEDERATION_DIR / "peer-trust.sqlite3"

PEER_TRUST_RECORD_SCHEMA = "federation-peer-trust-record-v1"
STATE_PENDING = "PENDING"
STATE_VERIFIED = "VERIFIED"
STATE_ACTIVE = "ACTIVE"
STATE_SUSPENDED = "SUSPENDED"
STATE_REVOKED = "REVOKED"
PEER_STATES = frozenset({STATE_PENDING, STATE_VERIFIED, STATE_ACTIVE, STATE_SUSPENDED, STATE_REVOKED})

_FLEET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_REQUIRED_METADATA_FIELDS = frozenset({"provider", "region", "jurisdiction", "siteClass"})
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
    def _peer_row(connection: sqlite3.Connection, peer_fleet_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM federation_peer_trust WHERE peer_fleet_id = ?",
            (peer_fleet_id,),
        ).fetchone()

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

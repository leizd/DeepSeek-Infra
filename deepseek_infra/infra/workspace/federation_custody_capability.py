"""Receiver-local custody modes and preprovisioned recovery identity binding."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_crypto,
    backup_recovery_credential,
    federation_identity,
    federation_peer_trust,
)

CUSTODY_CAPABILITY_DB = config.ROOT / ".federation" / "custody-capabilities.sqlite3"
CUSTODY_CAPABILITY_SCHEMA = "federation-custody-capability-v1"
CUSTODY_CAPABILITY_EVENT_SCHEMA = "federation-custody-capability-event-v1"

COLD_CUSTODY = "COLD_CUSTODY"
RECOVERY_CAPABLE = "RECOVERY_CAPABLE"
CUSTODY_MODES = frozenset({COLD_CUSTODY, RECOVERY_CAPABLE})

CUSTODY_CAPABILITY_PUBLIC_FIELDS = frozenset(
    {
        "schema",
        "localFleetId",
        "peerFleetId",
        "peerRootFingerprint",
        "mode",
        "recoveryIdentityPreprovisioned",
        "ageRecipient",
        "ageRecipientDigest",
        "configuredBy",
        "configuredAt",
        "updatedAt",
        "revision",
    }
)

_FLEET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_ROOT_FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_AGE_RECIPIENT_PATTERN = re.compile(r"^age1[0-9a-z]{6,250}$")
_MAX_AUDIT_TEXT = 256

_CREATE_IDENTITY_SQL = """
CREATE TABLE IF NOT EXISTS federation_custody_local_identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    local_fleet_id TEXT NOT NULL,
    root_key_id TEXT NOT NULL,
    root_fingerprint TEXT NOT NULL,
    identity_digest TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    bound_at TEXT NOT NULL
)
"""

_CREATE_CAPABILITIES_SQL = """
CREATE TABLE IF NOT EXISTS federation_custody_capabilities (
    peer_fleet_id TEXT PRIMARY KEY,
    peer_root_fingerprint TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('COLD_CUSTODY', 'RECOVERY_CAPABLE')),
    credential_provider TEXT,
    credential_ref TEXT,
    age_recipient TEXT,
    age_recipient_digest TEXT,
    configured_by TEXT NOT NULL,
    configured_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    CHECK(
        (mode = 'COLD_CUSTODY' AND credential_provider IS NULL AND credential_ref IS NULL
         AND age_recipient IS NULL AND age_recipient_digest IS NULL)
        OR
        (mode = 'RECOVERY_CAPABLE' AND credential_provider IS NOT NULL AND credential_ref IS NOT NULL
         AND age_recipient IS NOT NULL AND age_recipient_digest IS NOT NULL)
    )
)
"""

_CREATE_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS federation_custody_capability_events (
    event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_fleet_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    previous_mode TEXT,
    next_mode TEXT NOT NULL,
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    FOREIGN KEY(peer_fleet_id) REFERENCES federation_custody_capabilities(peer_fleet_id)
)
"""

_CREATE_EVENT_REVISION_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_federation_custody_event_revision
ON federation_custody_capability_events(peer_fleet_id, revision)
"""


class FederationCustodyCapabilityError(RuntimeError):
    """Fail-closed custody capability error with a stable code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = str(code)
        super().__init__(message or self.code)


def _canonical_json(value: Any) -> str:
    try:
        return federation_identity.canonical_federation_json(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederationCustodyCapabilityError("FEDERATION_CUSTODY_CANONICAL_PAYLOAD_INVALID") from exc


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise FederationCustodyCapabilityError("FEDERATION_CUSTODY_TIMESTAMP_INVALID")
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fleet_id(value: Any) -> str:
    if type(value) is not str or _FLEET_ID_PATTERN.fullmatch(value) is None:
        raise FederationCustodyCapabilityError("FEDERATION_CUSTODY_PEER_FLEET_ID_INVALID")
    return value


def _mode(value: Any) -> str:
    if type(value) is not str or value not in CUSTODY_MODES:
        raise FederationCustodyCapabilityError("FEDERATION_CUSTODY_MODE_INVALID")
    return value


def _reference(value: Any, *, code: str) -> str:
    if type(value) is not str or _REFERENCE_PATTERN.fullmatch(value) is None:
        raise FederationCustodyCapabilityError(code)
    return value


def _recipient(value: Any) -> str:
    if type(value) is not str or _AGE_RECIPIENT_PATTERN.fullmatch(value) is None:
        raise FederationCustodyCapabilityError("FEDERATION_RECOVERY_AGE_RECIPIENT_INVALID")
    return value


def _audit_text(value: Any) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > _MAX_AUDIT_TEXT:
        raise FederationCustodyCapabilityError("FEDERATION_CUSTODY_ACTOR_INVALID")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise FederationCustodyCapabilityError("FEDERATION_CUSTODY_ACTOR_INVALID")
    return value


def _expected_revision(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise FederationCustodyCapabilityError("FEDERATION_CUSTODY_CAPABILITY_REVISION_CONFLICT")
    return value


def _recipient_digest(recipient: str) -> str:
    return "sha256:" + hashlib.sha256(recipient.encode("utf-8")).hexdigest()


def _verify_preprovisioned_identity(
    provider_name: str,
    credential_ref: str,
    age_recipient: str,
) -> None:
    try:
        provider = backup_recovery_credential.get_provider(provider_name)
        if not provider.has_credential(credential_ref):
            raise FederationCustodyCapabilityError("FEDERATION_RECOVERY_IDENTITY_NOT_PREPROVISIONED")
        secret = provider.acquire_secret_bytes(credential_ref)
    except FederationCustodyCapabilityError:
        raise
    except AppError as exc:
        raise FederationCustodyCapabilityError("FEDERATION_RECOVERY_IDENTITY_NOT_PREPROVISIONED") from exc
    try:
        if not isinstance(secret, bytearray) or not secret:
            raise FederationCustodyCapabilityError("FEDERATION_RECOVERY_IDENTITY_NOT_PREPROVISIONED")
        try:
            recipients = backup_crypto.derive_recipients(secret)
        except AppError as exc:
            raise FederationCustodyCapabilityError("FEDERATION_RECOVERY_IDENTITY_INVALID") from exc
        if age_recipient not in recipients:
            raise FederationCustodyCapabilityError("FEDERATION_RECOVERY_IDENTITY_RECIPIENT_MISMATCH")
    finally:
        backup_recovery_credential.zeroize(secret)


class FederationCustodyCapabilityRegistry:
    """Receiver-local relationship modes; private identity bytes never persist."""

    def __init__(self, db_path: Path, local_identity: dict[str, Any]) -> None:
        self.db_path = Path(db_path)
        try:
            self._local_identity = federation_identity.validate_fleet_identity(local_identity)
        except federation_identity.FederationIdentityError as exc:
            raise FederationCustodyCapabilityError(exc.code) from exc
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()
        self._bind_local_identity()

    @property
    def local_identity(self) -> dict[str, Any]:
        normalized = federation_identity.normalize_federation_json(self._local_identity)
        assert isinstance(normalized, dict)
        return normalized

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _ensure_schema(self) -> None:
        with self._write() as connection:
            connection.execute(_CREATE_IDENTITY_SQL)
            connection.execute(_CREATE_CAPABILITIES_SQL)
            connection.execute(_CREATE_EVENTS_SQL)
            connection.execute(_CREATE_EVENT_REVISION_INDEX_SQL)

    def _bind_local_identity(self) -> None:
        identity_json = _canonical_json(self._local_identity)
        identity_digest = _digest(self._local_identity)
        timestamp = _utc_iso()
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM federation_custody_local_identity WHERE singleton = 1"
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO federation_custody_local_identity (
                        singleton, local_fleet_id, root_key_id, root_fingerprint,
                        identity_digest, identity_json, bound_at
                    ) VALUES (1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._local_identity["fleetId"],
                        self._local_identity["rootKeyId"],
                        self._local_identity["rootFingerprint"],
                        identity_digest,
                        identity_json,
                        timestamp,
                    ),
                )
                return
            if (
                existing["local_fleet_id"] != self._local_identity["fleetId"]
                or existing["root_key_id"] != self._local_identity["rootKeyId"]
                or existing["root_fingerprint"] != self._local_identity["rootFingerprint"]
                or existing["identity_digest"] != identity_digest
                or existing["identity_json"] != identity_json
            ):
                raise FederationCustodyCapabilityError("FEDERATION_CUSTODY_LOCAL_IDENTITY_CONFLICT")

    def _require_registry_identity(self, peer_registry: federation_peer_trust.PeerTrustRegistry) -> None:
        if peer_registry.local_identity != self.local_identity:
            raise FederationCustodyCapabilityError("FEDERATION_CUSTODY_LOCAL_IDENTITY_MISMATCH")

    @staticmethod
    def _public_record(row: sqlite3.Row) -> dict[str, Any]:
        mode = str(row["mode"])
        recipient = row["age_recipient"]
        record = {
            "schema": CUSTODY_CAPABILITY_SCHEMA,
            "localFleetId": None,
            "peerFleetId": str(row["peer_fleet_id"]),
            "peerRootFingerprint": str(row["peer_root_fingerprint"]),
            "mode": mode,
            "recoveryIdentityPreprovisioned": mode == RECOVERY_CAPABLE,
            "ageRecipient": None if recipient is None else str(recipient),
            "ageRecipientDigest": None if row["age_recipient_digest"] is None else str(row["age_recipient_digest"]),
            "configuredBy": str(row["configured_by"]),
            "configuredAt": str(row["configured_at"]),
            "updatedAt": str(row["updated_at"]),
            "revision": int(row["revision"]),
        }
        return record

    def _record(self, row: sqlite3.Row) -> dict[str, Any]:
        record = self._public_record(row)
        record["localFleetId"] = str(self._local_identity["fleetId"])
        if set(record) != CUSTODY_CAPABILITY_PUBLIC_FIELDS:
            raise FederationCustodyCapabilityError("FEDERATION_CUSTODY_CAPABILITY_RECORD_INVALID")
        mode = str(record["mode"])
        if mode == COLD_CUSTODY and any(record[field] is not None for field in ("ageRecipient", "ageRecipientDigest")):
            raise FederationCustodyCapabilityError("FEDERATION_CUSTODY_CAPABILITY_RECORD_INVALID")
        if mode == RECOVERY_CAPABLE:
            recipient = _recipient(record["ageRecipient"])
            if record["ageRecipientDigest"] != _recipient_digest(recipient):
                raise FederationCustodyCapabilityError("FEDERATION_CUSTODY_CAPABILITY_RECORD_INVALID")
        return record

    def _require_active_peer(
        self,
        peer_registry: federation_peer_trust.PeerTrustRegistry,
        peer_fleet_id: str,
    ) -> dict[str, Any]:
        self._require_registry_identity(peer_registry)
        peer = _fleet_id(peer_fleet_id)
        try:
            record = peer_registry.require_active_peer(peer)
        except federation_peer_trust.FederationTrustError as exc:
            raise FederationCustodyCapabilityError(exc.code) from exc
        fingerprint = record.get("rootFingerprint")
        if type(fingerprint) is not str or _ROOT_FINGERPRINT_PATTERN.fullmatch(fingerprint) is None:
            raise FederationCustodyCapabilityError("FEDERATION_CUSTODY_PEER_ROOT_INVALID")
        return record

    def configure_peer(
        self,
        peer_registry: federation_peer_trust.PeerTrustRegistry,
        peer_fleet_id: str,
        *,
        mode: str,
        actor: str,
        credential_provider: str | None = None,
        credential_ref: str | None = None,
        age_recipient: str | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        peer = self._require_active_peer(peer_registry, peer_fleet_id)
        peer_id = str(peer["peerFleetId"])
        peer_root = str(peer["rootFingerprint"])
        normalized_mode = _mode(mode)
        normalized_actor = _audit_text(actor)
        normalized_expected_revision = _expected_revision(expected_revision)
        timestamp = _utc_iso(now)
        provider: str | None = None
        reference: str | None = None
        recipient: str | None = None
        recipient_digest: str | None = None
        if normalized_mode == COLD_CUSTODY:
            if any(value is not None for value in (credential_provider, credential_ref, age_recipient)):
                raise FederationCustodyCapabilityError("FEDERATION_COLD_CUSTODY_RECOVERY_IDENTITY_FORBIDDEN")
        else:
            if credential_provider is None or credential_ref is None or age_recipient is None:
                raise FederationCustodyCapabilityError("FEDERATION_RECOVERY_IDENTITY_BINDING_REQUIRED")
            provider = _reference(credential_provider, code="FEDERATION_RECOVERY_CREDENTIAL_PROVIDER_INVALID")
            reference = _reference(credential_ref, code="FEDERATION_RECOVERY_CREDENTIAL_REFERENCE_INVALID")
            recipient = _recipient(age_recipient)
            recipient_digest = _recipient_digest(recipient)
            _verify_preprovisioned_identity(provider, reference, recipient)
        private_binding = (provider, reference, recipient, recipient_digest)
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM federation_custody_capabilities WHERE peer_fleet_id = ?",
                (peer_id,),
            ).fetchone()
            if existing is not None:
                existing_binding = (
                    existing["credential_provider"],
                    existing["credential_ref"],
                    existing["age_recipient"],
                    existing["age_recipient_digest"],
                )
                if (
                    existing["peer_root_fingerprint"] == peer_root
                    and existing["mode"] == normalized_mode
                    and existing_binding == private_binding
                ):
                    return self._record(existing)
                if normalized_expected_revision is None or normalized_expected_revision != int(existing["revision"]):
                    raise FederationCustodyCapabilityError("FEDERATION_CUSTODY_CAPABILITY_REVISION_CONFLICT")
                next_revision = int(existing["revision"]) + 1
                connection.execute(
                    """
                    UPDATE federation_custody_capabilities
                    SET peer_root_fingerprint = ?, mode = ?, credential_provider = ?,
                        credential_ref = ?, age_recipient = ?, age_recipient_digest = ?,
                        configured_by = ?, updated_at = ?, revision = ?
                    WHERE peer_fleet_id = ? AND revision = ?
                    """,
                    (
                        peer_root,
                        normalized_mode,
                        provider,
                        reference,
                        recipient,
                        recipient_digest,
                        normalized_actor,
                        timestamp,
                        next_revision,
                        peer_id,
                        int(existing["revision"]),
                    ),
                )
                event_type = "CUSTODY_CAPABILITY_UPDATED"
                previous_mode = str(existing["mode"])
            else:
                if normalized_expected_revision is not None:
                    raise FederationCustodyCapabilityError("FEDERATION_CUSTODY_CAPABILITY_REVISION_CONFLICT")
                next_revision = 1
                connection.execute(
                    """
                    INSERT INTO federation_custody_capabilities (
                        peer_fleet_id, peer_root_fingerprint, mode,
                        credential_provider, credential_ref, age_recipient,
                        age_recipient_digest, configured_by, configured_at,
                        updated_at, revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        peer_id,
                        peer_root,
                        normalized_mode,
                        provider,
                        reference,
                        recipient,
                        recipient_digest,
                        normalized_actor,
                        timestamp,
                        timestamp,
                    ),
                )
                event_type = "CUSTODY_CAPABILITY_CONFIGURED"
                previous_mode = None
            connection.execute(
                """
                INSERT INTO federation_custody_capability_events (
                    peer_fleet_id, event_type, previous_mode, next_mode,
                    actor, occurred_at, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    peer_id,
                    event_type,
                    previous_mode,
                    normalized_mode,
                    normalized_actor,
                    timestamp,
                    next_revision,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM federation_custody_capabilities WHERE peer_fleet_id = ?",
                (peer_id,),
            ).fetchone()
            assert updated is not None
            return self._record(updated)

    def get_peer(self, peer_fleet_id: str) -> dict[str, Any] | None:
        peer = _fleet_id(peer_fleet_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM federation_custody_capabilities WHERE peer_fleet_id = ?",
                (peer,),
            ).fetchone()
        return None if row is None else self._record(row)

    def list_peers(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM federation_custody_capabilities ORDER BY peer_fleet_id"
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_events(self, peer_fleet_id: str) -> list[dict[str, Any]]:
        peer = _fleet_id(peer_fleet_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM federation_custody_capability_events
                WHERE peer_fleet_id = ? ORDER BY event_sequence
                """,
                (peer,),
            ).fetchall()
        return [
            {
                "schema": CUSTODY_CAPABILITY_EVENT_SCHEMA,
                "eventSequence": int(row["event_sequence"]),
                "peerFleetId": str(row["peer_fleet_id"]),
                "eventType": str(row["event_type"]),
                "previousMode": row["previous_mode"],
                "nextMode": str(row["next_mode"]),
                "actor": str(row["actor"]),
                "occurredAt": str(row["occurred_at"]),
                "revision": int(row["revision"]),
            }
            for row in rows
        ]

    def _private_binding(
        self,
        peer_registry: federation_peer_trust.PeerTrustRegistry,
        peer_fleet_id: str,
    ) -> tuple[dict[str, Any], str, str, str]:
        peer = self._require_active_peer(peer_registry, peer_fleet_id)
        peer_id = str(peer["peerFleetId"])
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM federation_custody_capabilities WHERE peer_fleet_id = ?",
                (peer_id,),
            ).fetchone()
        if row is None:
            raise FederationCustodyCapabilityError("FEDERATION_CUSTODY_CAPABILITY_NOT_CONFIGURED")
        record = self._record(row)
        if record["peerRootFingerprint"] != peer["rootFingerprint"]:
            raise FederationCustodyCapabilityError("FEDERATION_CUSTODY_PEER_ROOT_CONFLICT")
        if record["mode"] != RECOVERY_CAPABLE:
            raise FederationCustodyCapabilityError("FEDERATION_PEER_COLD_CUSTODY_ONLY")
        provider = row["credential_provider"]
        reference = row["credential_ref"]
        recipient = row["age_recipient"]
        if type(provider) is not str or type(reference) is not str or type(recipient) is not str:
            raise FederationCustodyCapabilityError("FEDERATION_RECOVERY_IDENTITY_BINDING_INVALID")
        return record, provider, reference, recipient

    @contextmanager
    def open_recovery_identity(
        self,
        peer_registry: federation_peer_trust.PeerTrustRegistry,
        peer_fleet_id: str,
    ) -> Iterator[bytearray]:
        """Yield only receiver-local identity bytes and always zeroize them."""

        _record, provider_name, credential_ref, recipient = self._private_binding(
            peer_registry,
            peer_fleet_id,
        )
        try:
            provider = backup_recovery_credential.get_provider(provider_name)
            if not provider.has_credential(credential_ref):
                raise FederationCustodyCapabilityError("FEDERATION_RECOVERY_IDENTITY_NOT_PREPROVISIONED")
            secret = provider.acquire_secret_bytes(credential_ref)
        except FederationCustodyCapabilityError:
            raise
        except AppError as exc:
            raise FederationCustodyCapabilityError("FEDERATION_RECOVERY_IDENTITY_NOT_PREPROVISIONED") from exc
        try:
            if not isinstance(secret, bytearray) or not secret:
                raise FederationCustodyCapabilityError("FEDERATION_RECOVERY_IDENTITY_NOT_PREPROVISIONED")
            try:
                recipients = backup_crypto.derive_recipients(secret)
            except AppError as exc:
                raise FederationCustodyCapabilityError("FEDERATION_RECOVERY_IDENTITY_INVALID") from exc
            if recipient not in recipients:
                raise FederationCustodyCapabilityError("FEDERATION_RECOVERY_IDENTITY_RECIPIENT_MISMATCH")
            yield secret
        finally:
            backup_recovery_credential.zeroize(secret)


def get_registry(
    local_identity: dict[str, Any],
    db_path: Path | None = None,
) -> FederationCustodyCapabilityRegistry:
    return FederationCustodyCapabilityRegistry(db_path or CUSTODY_CAPABILITY_DB, local_identity)

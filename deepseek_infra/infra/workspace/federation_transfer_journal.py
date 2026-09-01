"""Sovereign durable journal for one Fleet's side of federated transfers."""

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

FEDERATION_TRANSFER_DB = config.ROOT / ".federation" / "transfers.sqlite3"

TRANSFER_JOURNAL_RECORD_SCHEMA = "federated-transfer-journal-record-v1"
TRANSFER_JOURNAL_EVENT_SCHEMA = "federated-transfer-journal-event-v1"
TRANSFER_STATE_PAYLOAD_SCHEMA = "federated-transfer-state-v1"
TRANSFER_IDENTITY_BINDING_SCHEMA = "federated-transfer-binding-v1"

ROLE_SENDER = "SENDER"
ROLE_RECEIVER = "RECEIVER"
TRANSFER_ROLES = frozenset({ROLE_SENDER, ROLE_RECEIVER})

STATE_PROPOSED = "PROPOSED"
STATE_GRANT_REQUESTED = "GRANT_REQUESTED"
STATE_GRANT_VERIFIED = "GRANT_VERIFIED"
STATE_TRANSFERRING = "TRANSFERRING"
STATE_REMOTE_VERIFYING = "REMOTE_VERIFYING"
STATE_REMOTE_COMMITTED = "REMOTE_COMMITTED"
STATE_LOCAL_RECORDED = "LOCAL_RECORDED"
STATE_SUCCEEDED = "SUCCEEDED"
TRANSFER_STATES = (
    STATE_PROPOSED,
    STATE_GRANT_REQUESTED,
    STATE_GRANT_VERIFIED,
    STATE_TRANSFERRING,
    STATE_REMOTE_VERIFYING,
    STATE_REMOTE_COMMITTED,
    STATE_LOCAL_RECORDED,
    STATE_SUCCEEDED,
)

_FLEET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_TRANSFER_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTROL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_STATE_DETAILS_BYTES = 64 * 1024
_SENSITIVE_KEY_MARKERS = (
    "credential",
    "password",
    "privatekey",
    "secretkey",
    "accesskey",
    "agesecretkey",
)

_CREATE_JOURNAL_IDENTITY_SQL = """
CREATE TABLE IF NOT EXISTS federation_transfer_journal_identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    local_fleet_id TEXT NOT NULL,
    root_fingerprint TEXT NOT NULL,
    identity_digest TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    bound_at TEXT NOT NULL
)
"""

_CREATE_TRANSFERS_SQL = """
CREATE TABLE IF NOT EXISTS federation_transfers (
    transfer_id TEXT PRIMARY KEY,
    identity_digest TEXT NOT NULL,
    local_fleet_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('SENDER', 'RECEIVER')),
    source_fleet_id TEXT NOT NULL,
    destination_fleet_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    backup_id TEXT NOT NULL,
    object_set_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'PROPOSED', 'GRANT_REQUESTED', 'GRANT_VERIFIED', 'TRANSFERRING',
        'REMOTE_VERIFYING', 'REMOTE_COMMITTED', 'LOCAL_RECORDED', 'SUCCEEDED'
    )),
    state_payload_digest TEXT NOT NULL,
    state_payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1)
)
"""

_CREATE_TRANSFER_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS federation_transfer_events (
    event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id TEXT NOT NULL,
    previous_state TEXT,
    next_state TEXT NOT NULL,
    state_payload_digest TEXT NOT NULL,
    state_payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    FOREIGN KEY(transfer_id) REFERENCES federation_transfers(transfer_id)
)
"""

_CREATE_TRANSFER_EVENTS_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_federation_transfer_events_revision
ON federation_transfer_events(transfer_id, revision)
"""


class FederatedTransferJournalError(RuntimeError):
    """Fail-closed transfer-journal error with a stable code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = str(code)
        super().__init__(message or self.code)


def _normalize(value: Any) -> Any:
    try:
        return federation_identity.normalize_federation_json(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederatedTransferJournalError("FEDERATION_TRANSFER_CANONICAL_PAYLOAD_INVALID") from exc


def _canonical_json(value: Any) -> str:
    try:
        return federation_identity.canonical_federation_json(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederatedTransferJournalError("FEDERATION_TRANSFER_CANONICAL_PAYLOAD_INVALID") from exc


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_iso(value: datetime | None) -> str:
    current = value if value is not None else datetime.now(tz=timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise FederatedTransferJournalError("FEDERATION_TRANSFER_TIMESTAMP_INVALID")
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fleet_id(value: Any) -> str:
    if type(value) is not str or _FLEET_ID_PATTERN.fullmatch(value) is None:
        raise FederatedTransferJournalError("FEDERATION_TRANSFER_FLEET_ID_INVALID")
    return value


def _transfer_id(value: Any) -> str:
    if type(value) is not str or _TRANSFER_ID_PATTERN.fullmatch(value) is None:
        raise FederatedTransferJournalError("FEDERATION_TRANSFER_ID_INVALID")
    return value


def _control_id(value: Any, *, code: str) -> str:
    if type(value) is not str or _CONTROL_ID_PATTERN.fullmatch(value) is None:
        raise FederatedTransferJournalError(code)
    return value


def _object_set_digest(value: Any) -> str:
    if type(value) is not str or _SHA256_DIGEST_PATTERN.fullmatch(value) is None:
        raise FederatedTransferJournalError("FEDERATION_TRANSFER_OBJECT_SET_DIGEST_INVALID")
    return value


def _state(value: Any) -> str:
    if type(value) is not str or value not in TRANSFER_STATES:
        raise FederatedTransferJournalError("FEDERATION_TRANSFER_STATE_INVALID")
    return value


def _revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FederatedTransferJournalError("FEDERATION_TRANSFER_REVISION_INVALID")
    return value


def _contains_sensitive_key(value: Any) -> bool:
    if type(value) is dict:
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if any(marker in normalized_key for marker in _SENSITIVE_KEY_MARKERS):
                return True
            if _contains_sensitive_key(item):
                return True
    elif type(value) is list:
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _state_payload(transfer_id: str, state: str, details: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    normalized_details = _normalize(details)
    if type(normalized_details) is not dict:
        raise FederatedTransferJournalError("FEDERATION_TRANSFER_STATE_DETAILS_INVALID")
    if _contains_sensitive_key(normalized_details):
        raise FederatedTransferJournalError("FEDERATION_TRANSFER_SENSITIVE_STATE_REJECTED")
    details_json = _canonical_json(normalized_details)
    if len(details_json.encode("utf-8")) > _MAX_STATE_DETAILS_BYTES:
        raise FederatedTransferJournalError("FEDERATION_TRANSFER_STATE_DETAILS_TOO_LARGE")
    payload = {
        "schema": TRANSFER_STATE_PAYLOAD_SCHEMA,
        "transferId": transfer_id,
        "state": state,
        "details": normalized_details,
    }
    payload_json = _canonical_json(payload)
    return payload, _digest(payload), payload_json


def _identity_binding(
    *,
    transfer_id: str,
    source_fleet_id: str,
    destination_fleet_id: str,
    policy_id: str,
    backup_id: str,
    object_set_digest: str,
) -> tuple[dict[str, Any], str]:
    binding = {
        "schema": TRANSFER_IDENTITY_BINDING_SCHEMA,
        "transferId": transfer_id,
        "sourceFleetId": source_fleet_id,
        "destinationFleetId": destination_fleet_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": object_set_digest,
    }
    return binding, _digest(binding)


class FederatedTransferJournal:
    """One Fleet's SQLite journal; never shared across Fleet sovereignty boundaries."""

    def __init__(self, db_path: Path, local_identity: dict[str, Any]) -> None:
        self._db_path = Path(db_path)
        try:
            self._local_identity = federation_identity.validate_fleet_identity(local_identity)
        except federation_identity.FederationIdentityError as exc:
            raise FederatedTransferJournalError(exc.code) from exc
        self._ensure_schema()
        self._bind_local_identity()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def local_identity(self) -> dict[str, Any]:
        normalized = _normalize(self._local_identity)
        assert isinstance(normalized, dict)
        return normalized

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
            connection.execute(_CREATE_JOURNAL_IDENTITY_SQL)
            connection.execute(_CREATE_TRANSFERS_SQL)
            connection.execute(_CREATE_TRANSFER_EVENTS_SQL)
            connection.execute(_CREATE_TRANSFER_EVENTS_INDEX_SQL)

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
        local_fleet_id = str(self._local_identity["fleetId"])
        identity_json = _canonical_json(self._local_identity)
        identity_digest = _digest(self._local_identity)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM federation_transfer_journal_identity WHERE singleton = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO federation_transfer_journal_identity (
                        singleton, local_fleet_id, root_fingerprint,
                        identity_digest, identity_json, bound_at
                    ) VALUES (1, ?, ?, ?, ?, ?)
                    """,
                    (
                        local_fleet_id,
                        self._local_identity["rootFingerprint"],
                        identity_digest,
                        identity_json,
                        _utc_iso(None),
                    ),
                )
                return
            if (
                row["local_fleet_id"] != local_fleet_id
                or row["root_fingerprint"] != self._local_identity["rootFingerprint"]
                or row["identity_digest"] != identity_digest
                or row["identity_json"] != identity_json
            ):
                raise FederatedTransferJournalError("FEDERATION_TRANSFER_JOURNAL_IDENTITY_CONFLICT")

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(str(row["state_payload_json"]))
        return {
            "schema": TRANSFER_JOURNAL_RECORD_SCHEMA,
            "transferId": str(row["transfer_id"]),
            "identityDigest": str(row["identity_digest"]),
            "localFleetId": str(row["local_fleet_id"]),
            "role": str(row["role"]),
            "sourceFleetId": str(row["source_fleet_id"]),
            "destinationFleetId": str(row["destination_fleet_id"]),
            "policyId": str(row["policy_id"]),
            "backupId": str(row["backup_id"]),
            "objectSetDigest": str(row["object_set_digest"]),
            "state": str(row["state"]),
            "statePayloadDigest": str(row["state_payload_digest"]),
            "stateDetails": payload["details"],
            "createdAt": str(row["created_at"]),
            "updatedAt": str(row["updated_at"]),
            "revision": int(row["revision"]),
        }

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(str(row["state_payload_json"]))
        return {
            "schema": TRANSFER_JOURNAL_EVENT_SCHEMA,
            "sequence": int(row["event_sequence"]),
            "transferId": str(row["transfer_id"]),
            "previousState": row["previous_state"],
            "nextState": str(row["next_state"]),
            "statePayloadDigest": str(row["state_payload_digest"]),
            "stateDetails": payload["details"],
            "occurredAt": str(row["occurred_at"]),
            "revision": int(row["revision"]),
        }

    def persist_proposed_transfer(
        self,
        *,
        transfer_id: str,
        source_fleet_id: str,
        destination_fleet_id: str,
        policy_id: str,
        backup_id: str,
        object_set_digest: str,
        now: datetime,
    ) -> dict[str, Any]:
        transfer = _transfer_id(transfer_id)
        source = _fleet_id(source_fleet_id)
        destination = _fleet_id(destination_fleet_id)
        if source == destination:
            raise FederatedTransferJournalError("FEDERATION_TRANSFER_REFLECTION_REJECTED")
        policy = _control_id(policy_id, code="FEDERATION_TRANSFER_POLICY_ID_INVALID")
        backup = _control_id(backup_id, code="FEDERATION_TRANSFER_BACKUP_ID_INVALID")
        object_set = _object_set_digest(object_set_digest)
        timestamp = _utc_iso(now)
        _, identity_digest = _identity_binding(
            transfer_id=transfer,
            source_fleet_id=source,
            destination_fleet_id=destination,
            policy_id=policy,
            backup_id=backup,
            object_set_digest=object_set,
        )
        local_fleet_id = str(self._local_identity["fleetId"])
        initial_details = {"identityDigest": identity_digest}
        _, state_payload_digest, state_payload_json = _state_payload(
            transfer,
            STATE_PROPOSED,
            initial_details,
        )
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM federation_transfers WHERE transfer_id = ?",
                (transfer,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["identity_digest"] != identity_digest
                    or existing["source_fleet_id"] != source
                    or existing["destination_fleet_id"] != destination
                    or existing["policy_id"] != policy
                    or existing["backup_id"] != backup
                    or existing["object_set_digest"] != object_set
                ):
                    raise FederatedTransferJournalError("FEDERATION_TRANSFER_IDENTITY_CONFLICT")
                return self._record(existing)
            if local_fleet_id == source:
                role = ROLE_SENDER
            elif local_fleet_id == destination:
                role = ROLE_RECEIVER
            else:
                raise FederatedTransferJournalError("FEDERATION_TRANSFER_LOCAL_FLEET_NOT_PARTY")
            connection.execute(
                """
                INSERT INTO federation_transfers (
                    transfer_id, identity_digest, local_fleet_id, role,
                    source_fleet_id, destination_fleet_id, policy_id,
                    backup_id, object_set_digest, state,
                    state_payload_digest, state_payload_json,
                    created_at, updated_at, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    transfer,
                    identity_digest,
                    local_fleet_id,
                    role,
                    source,
                    destination,
                    policy,
                    backup,
                    object_set,
                    STATE_PROPOSED,
                    state_payload_digest,
                    state_payload_json,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO federation_transfer_events (
                    transfer_id, previous_state, next_state,
                    state_payload_digest, state_payload_json,
                    occurred_at, revision
                ) VALUES (?, NULL, ?, ?, ?, ?, 1)
                """,
                (transfer, STATE_PROPOSED, state_payload_digest, state_payload_json, timestamp),
            )
            created = connection.execute(
                "SELECT * FROM federation_transfers WHERE transfer_id = ?",
                (transfer,),
            ).fetchone()
            assert created is not None
            return self._record(created)

    def advance_transfer(
        self,
        transfer_id: str,
        *,
        expected_revision: int,
        next_state: str,
        details: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        transfer = _transfer_id(transfer_id)
        target_state = _state(next_state)
        expected = _revision(expected_revision)
        timestamp = _utc_iso(now)
        _, payload_digest, payload_json = _state_payload(transfer, target_state, details)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM federation_transfers WHERE transfer_id = ?",
                (transfer,),
            ).fetchone()
            if row is None:
                raise FederatedTransferJournalError("FEDERATION_TRANSFER_NOT_FOUND")
            current_state = str(row["state"])
            if current_state == target_state:
                if row["state_payload_digest"] != payload_digest or row["state_payload_json"] != payload_json:
                    raise FederatedTransferJournalError("FEDERATION_TRANSFER_STATE_CONFLICT")
                return self._record(row)
            current_revision = int(row["revision"])
            if expected != current_revision:
                raise FederatedTransferJournalError("FEDERATION_TRANSFER_REVISION_CONFLICT")
            if timestamp < str(row["updated_at"]):
                raise FederatedTransferJournalError("FEDERATION_TRANSFER_TIMESTAMP_REGRESSION")
            current_index = TRANSFER_STATES.index(current_state)
            if current_index + 1 >= len(TRANSFER_STATES) or TRANSFER_STATES[current_index + 1] != target_state:
                raise FederatedTransferJournalError("FEDERATION_TRANSFER_STATE_TRANSITION_INVALID")
            next_revision = current_revision + 1
            cursor = connection.execute(
                """
                UPDATE federation_transfers
                SET state = ?, state_payload_digest = ?, state_payload_json = ?,
                    updated_at = ?, revision = ?
                WHERE transfer_id = ? AND revision = ? AND state = ?
                """,
                (
                    target_state,
                    payload_digest,
                    payload_json,
                    timestamp,
                    next_revision,
                    transfer,
                    current_revision,
                    current_state,
                ),
            )
            if cursor.rowcount != 1:  # pragma: no cover - BEGIN IMMEDIATE is the primary fence
                raise FederatedTransferJournalError("FEDERATION_TRANSFER_REVISION_CONFLICT")
            connection.execute(
                """
                INSERT INTO federation_transfer_events (
                    transfer_id, previous_state, next_state,
                    state_payload_digest, state_payload_json,
                    occurred_at, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transfer,
                    current_state,
                    target_state,
                    payload_digest,
                    payload_json,
                    timestamp,
                    next_revision,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM federation_transfers WHERE transfer_id = ?",
                (transfer,),
            ).fetchone()
            assert updated is not None
            return self._record(updated)

    def get_transfer(self, transfer_id: str) -> dict[str, Any] | None:
        transfer = _transfer_id(transfer_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM federation_transfers WHERE transfer_id = ?",
                (transfer,),
            ).fetchone()
        return None if row is None else self._record(row)

    def list_transfers(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM federation_transfers ORDER BY created_at, transfer_id"
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_transfer_events(self, transfer_id: str) -> list[dict[str, Any]]:
        transfer = _transfer_id(transfer_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM federation_transfer_events
                WHERE transfer_id = ? ORDER BY event_sequence
                """,
                (transfer,),
            ).fetchall()
        return [self._event(row) for row in rows]


def open_federated_transfer_journal(
    local_identity: dict[str, Any],
    *,
    db_path: Path | None = None,
) -> FederatedTransferJournal:
    return FederatedTransferJournal(db_path or FEDERATION_TRANSFER_DB, local_identity)

"""Independent offsite durability ledger for verified federated replicas.

This module intentionally has no write path into the local DR ledger, backup
policy mutation, retention, or primary-promotion systems. A remote custody copy
can satisfy only ``federatedDurability`` objectives.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_policies,
    federated_replica_attestation,
    federation_identity,
    federation_peer_trust,
    federation_transfer_journal,
)

FEDERATED_DURABILITY_DB = config.ROOT / ".federation" / "federated-durability.sqlite3"
FEDERATED_COPY_RECORD_SCHEMA = "federated-copy-record-v1"
FEDERATED_DURABILITY_STATUS_SCHEMA = "federated-durability-status-v1"
FEDERATED_COMMITTED = "FEDERATED_COMMITTED"

FEDERATED_COPY_RECORD_FIELDS = frozenset(
    {
        "schema",
        "status",
        "transferId",
        "sourceFleetId",
        "destinationFleetId",
        "policyId",
        "backupId",
        "objectSetDigest",
        "remoteTargetId",
        "remoteReceiptDigest",
        "remoteCommitDigest",
        "attestationDigest",
        "attestationSequence",
        "signerKeyId",
        "failureDomain",
        "peerMetadata",
        "committedAt",
        "attestationAcceptedAt",
        "recordedAt",
        "localDurabilityCredit",
        "recordDigest",
        "revision",
    }
)

_FLEET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_CONTROL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TYPED_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_FAILURE_DOMAIN_PATTERN = re.compile(r"^federation-peer-domain:sha256:[0-9a-f]{64}$")
_REQUIRED_METADATA_FIELDS = frozenset({"provider", "region", "jurisdiction", "siteClass"})

_CREATE_IDENTITY_SQL = """
CREATE TABLE IF NOT EXISTS federated_durability_identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    local_fleet_id TEXT NOT NULL,
    root_key_id TEXT NOT NULL,
    root_fingerprint TEXT NOT NULL,
    identity_digest TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    bound_at TEXT NOT NULL
)
"""

_CREATE_COPIES_SQL = """
CREATE TABLE IF NOT EXISTS federated_copies (
    transfer_id TEXT PRIMARY KEY,
    source_fleet_id TEXT NOT NULL,
    destination_fleet_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    backup_id TEXT NOT NULL,
    object_set_digest TEXT NOT NULL,
    remote_target_id TEXT NOT NULL,
    remote_receipt_digest TEXT NOT NULL,
    remote_commit_digest TEXT NOT NULL,
    attestation_digest TEXT NOT NULL,
    attestation_sequence INTEGER NOT NULL CHECK(attestation_sequence >= 1),
    signer_key_id TEXT NOT NULL,
    failure_domain TEXT NOT NULL,
    peer_metadata_json TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    attestation_accepted_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    record_digest TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    status TEXT NOT NULL CHECK(status = 'FEDERATED_COMMITTED'),
    local_durability_credit INTEGER NOT NULL CHECK(local_durability_credit = 0),
    UNIQUE(destination_fleet_id, attestation_sequence)
)
"""

_CREATE_COPY_LOOKUP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_federated_copies_object
ON federated_copies(policy_id, backup_id, object_set_digest, destination_fleet_id)
"""


class FederatedDurabilityError(RuntimeError):
    """Fail-closed federated durability error with a stable code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = str(code)
        super().__init__(message or self.code)


def _normalize(value: Any) -> Any:
    try:
        return federation_identity.normalize_federation_json(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_CANONICAL_PAYLOAD_INVALID") from exc


def _canonical_json(value: Any) -> str:
    try:
        return federation_identity.canonical_federation_json(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_CANONICAL_PAYLOAD_INVALID") from exc


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_TIMESTAMP_INVALID")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if type(value) is not str:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_TIMESTAMP_INVALID")
    normalized = parsed.astimezone(timezone.utc)
    if _utc_iso(normalized) != value:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_TIMESTAMP_INVALID")
    return normalized


def _fleet_id(value: Any) -> str:
    if type(value) is not str or _FLEET_ID_PATTERN.fullmatch(value) is None:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_FLEET_ID_INVALID")
    return value


def _control_id(value: Any, *, code: str) -> str:
    if type(value) is not str or _CONTROL_ID_PATTERN.fullmatch(value) is None:
        raise FederatedDurabilityError(code)
    return value


def _typed_digest(value: Any, *, code: str) -> str:
    if type(value) is not str or _TYPED_DIGEST_PATTERN.fullmatch(value) is None:
        raise FederatedDurabilityError(code)
    return value


def _metadata(value: Any) -> dict[str, str]:
    if type(value) is not dict or set(value) != _REQUIRED_METADATA_FIELDS:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_PEER_METADATA_INVALID")
    normalized: dict[str, str] = {}
    for field in sorted(_REQUIRED_METADATA_FIELDS):
        item = value.get(field)
        if type(item) is not str or not item or item != item.strip() or len(item) > 256:
            raise FederatedDurabilityError("FEDERATED_DURABILITY_PEER_METADATA_INVALID")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in item):
            raise FederatedDurabilityError("FEDERATED_DURABILITY_PEER_METADATA_INVALID")
        normalized[field] = item
    return normalized


def _record_identity(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in record.items()
        if key not in {"recordDigest", "recordedAt", "revision"}
    }


def _record_digest(record: dict[str, Any]) -> str:
    return _digest(_record_identity(record))


class FederatedDurabilityLedger:
    """Sovereign local record of independently verified offsite copies."""

    def __init__(self, db_path: Path, local_identity: dict[str, Any]) -> None:
        self.db_path = Path(db_path)
        try:
            self._local_identity = federation_identity.validate_fleet_identity(local_identity)
        except federation_identity.FederationIdentityError as exc:
            raise FederatedDurabilityError(exc.code) from exc
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()
        self._bind_local_identity()

    @property
    def local_identity(self) -> dict[str, Any]:
        normalized = _normalize(self._local_identity)
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
            connection.execute(_CREATE_COPIES_SQL)
            connection.execute(_CREATE_COPY_LOOKUP_INDEX_SQL)

    def _bind_local_identity(self) -> None:
        identity_json = _canonical_json(self._local_identity)
        identity_digest = _digest(self._local_identity)
        bound_at = _utc_iso(datetime.now(tz=timezone.utc))
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM federated_durability_identity WHERE singleton = 1"
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO federated_durability_identity (
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
                        bound_at,
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
                raise FederatedDurabilityError("FEDERATED_DURABILITY_LOCAL_IDENTITY_CONFLICT")

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        record = {
            "schema": FEDERATED_COPY_RECORD_SCHEMA,
            "status": str(row["status"]),
            "transferId": str(row["transfer_id"]),
            "sourceFleetId": str(row["source_fleet_id"]),
            "destinationFleetId": str(row["destination_fleet_id"]),
            "policyId": str(row["policy_id"]),
            "backupId": str(row["backup_id"]),
            "objectSetDigest": str(row["object_set_digest"]),
            "remoteTargetId": str(row["remote_target_id"]),
            "remoteReceiptDigest": str(row["remote_receipt_digest"]),
            "remoteCommitDigest": str(row["remote_commit_digest"]),
            "attestationDigest": str(row["attestation_digest"]),
            "attestationSequence": int(row["attestation_sequence"]),
            "signerKeyId": str(row["signer_key_id"]),
            "failureDomain": str(row["failure_domain"]),
            "peerMetadata": json.loads(str(row["peer_metadata_json"])),
            "committedAt": str(row["committed_at"]),
            "attestationAcceptedAt": str(row["attestation_accepted_at"]),
            "recordedAt": str(row["recorded_at"]),
            "localDurabilityCredit": bool(row["local_durability_credit"]),
            "recordDigest": str(row["record_digest"]),
            "revision": int(row["revision"]),
        }
        if set(record) != FEDERATED_COPY_RECORD_FIELDS or record["recordDigest"] != _record_digest(record):
            raise FederatedDurabilityError("FEDERATED_DURABILITY_RECORD_CORRUPT")
        if record["status"] != FEDERATED_COMMITTED or record["localDurabilityCredit"] is not False:
            raise FederatedDurabilityError("FEDERATED_DURABILITY_RECORD_CORRUPT")
        return record

    def _record_verified_copy(self, evidence: dict[str, Any], *, recorded_at: datetime) -> dict[str, Any]:
        """Persist evidence produced by ``record_verified_federated_copy`` only."""

        normalized = _normalize(evidence)
        if type(normalized) is not dict:
            raise FederatedDurabilityError("FEDERATED_DURABILITY_RECORD_INVALID")
        expected_fields = FEDERATED_COPY_RECORD_FIELDS - {"recordDigest", "recordedAt", "revision"}
        if set(normalized) != expected_fields:
            raise FederatedDurabilityError("FEDERATED_DURABILITY_RECORD_FIELDS_INVALID")
        timestamp = _utc_iso(recorded_at)
        candidate = {**normalized, "recordedAt": timestamp, "revision": 1}
        candidate["recordDigest"] = _record_digest(candidate)
        transfer_id = _typed_digest(
            candidate.get("transferId"),
            code="FEDERATED_DURABILITY_TRANSFER_ID_INVALID",
        )
        metadata = _metadata(candidate.get("peerMetadata"))
        if (
            candidate.get("schema") != FEDERATED_COPY_RECORD_SCHEMA
            or candidate.get("status") != FEDERATED_COMMITTED
            or candidate.get("localDurabilityCredit") is not False
            or candidate.get("sourceFleetId") != self._local_identity["fleetId"]
            or candidate.get("failureDomain") != federated_replica_attestation.failure_domain_from_metadata(metadata)
        ):
            raise FederatedDurabilityError("FEDERATED_DURABILITY_RECORD_BINDING_INVALID")
        if isinstance(candidate.get("attestationSequence"), bool) or not isinstance(candidate.get("attestationSequence"), int):
            raise FederatedDurabilityError("FEDERATED_DURABILITY_ATTESTATION_SEQUENCE_INVALID")
        if int(candidate["attestationSequence"]) < 1:
            raise FederatedDurabilityError("FEDERATED_DURABILITY_ATTESTATION_SEQUENCE_INVALID")
        for field, code in (
            ("destinationFleetId", "FEDERATED_DURABILITY_FLEET_ID_INVALID"),
            ("policyId", "FEDERATED_DURABILITY_POLICY_ID_INVALID"),
            ("backupId", "FEDERATED_DURABILITY_BACKUP_ID_INVALID"),
            ("remoteTargetId", "FEDERATED_DURABILITY_REMOTE_TARGET_ID_INVALID"),
            ("signerKeyId", "FEDERATED_DURABILITY_SIGNER_KEY_ID_INVALID"),
        ):
            if field == "destinationFleetId":
                _fleet_id(candidate.get(field))
            else:
                _control_id(candidate.get(field), code=code)
        for field, code in (
            ("objectSetDigest", "FEDERATED_DURABILITY_OBJECT_SET_DIGEST_INVALID"),
            ("remoteReceiptDigest", "FEDERATED_DURABILITY_REMOTE_RECEIPT_DIGEST_INVALID"),
            ("remoteCommitDigest", "FEDERATED_DURABILITY_REMOTE_COMMIT_DIGEST_INVALID"),
            ("attestationDigest", "FEDERATED_DURABILITY_ATTESTATION_DIGEST_INVALID"),
        ):
            _typed_digest(candidate.get(field), code=code)
        if type(candidate.get("failureDomain")) is not str or _FAILURE_DOMAIN_PATTERN.fullmatch(candidate["failureDomain"]) is None:
            raise FederatedDurabilityError("FEDERATED_DURABILITY_FAILURE_DOMAIN_INVALID")
        committed_at = _parse_timestamp(candidate.get("committedAt"))
        accepted_at = _parse_timestamp(candidate.get("attestationAcceptedAt"))
        if committed_at > accepted_at or accepted_at > _parse_timestamp(timestamp):
            raise FederatedDurabilityError("FEDERATED_DURABILITY_TIMESTAMP_ORDER_INVALID")
        metadata_json = _canonical_json(metadata)
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM federated_copies WHERE transfer_id = ?",
                (transfer_id,),
            ).fetchone()
            if existing is not None:
                record = self._record(existing)
                if _record_identity(record) != _record_identity(candidate):
                    raise FederatedDurabilityError("FEDERATED_DURABILITY_RECORD_IDENTITY_CONFLICT")
                return record
            try:
                connection.execute(
                    """
                    INSERT INTO federated_copies (
                        transfer_id, source_fleet_id, destination_fleet_id,
                        policy_id, backup_id, object_set_digest, remote_target_id,
                        remote_receipt_digest, remote_commit_digest,
                        attestation_digest, attestation_sequence, signer_key_id,
                        failure_domain, peer_metadata_json, committed_at,
                        attestation_accepted_at, recorded_at, record_digest,
                        revision, status, local_durability_credit
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0)
                    """,
                    (
                        transfer_id,
                        candidate["sourceFleetId"],
                        candidate["destinationFleetId"],
                        candidate["policyId"],
                        candidate["backupId"],
                        candidate["objectSetDigest"],
                        candidate["remoteTargetId"],
                        candidate["remoteReceiptDigest"],
                        candidate["remoteCommitDigest"],
                        candidate["attestationDigest"],
                        candidate["attestationSequence"],
                        candidate["signerKeyId"],
                        candidate["failureDomain"],
                        metadata_json,
                        candidate["committedAt"],
                        candidate["attestationAcceptedAt"],
                        timestamp,
                        candidate["recordDigest"],
                        FEDERATED_COMMITTED,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise FederatedDurabilityError("FEDERATED_DURABILITY_ATTESTATION_SEQUENCE_CONFLICT") from exc
            created = connection.execute(
                "SELECT * FROM federated_copies WHERE transfer_id = ?",
                (transfer_id,),
            ).fetchone()
            assert created is not None
            return self._record(created)

    def get_copy(self, transfer_id: str) -> dict[str, Any] | None:
        transfer = _typed_digest(transfer_id, code="FEDERATED_DURABILITY_TRANSFER_ID_INVALID")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM federated_copies WHERE transfer_id = ?",
                (transfer,),
            ).fetchone()
        return None if row is None else self._record(row)

    def list_copies(
        self,
        *,
        policy_id: str | None = None,
        backup_id: str | None = None,
        object_set_digest: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[str] = []
        if policy_id is not None:
            clauses.append("policy_id = ?")
            parameters.append(_control_id(policy_id, code="FEDERATED_DURABILITY_POLICY_ID_INVALID"))
        if backup_id is not None:
            clauses.append("backup_id = ?")
            parameters.append(_control_id(backup_id, code="FEDERATED_DURABILITY_BACKUP_ID_INVALID"))
        if object_set_digest is not None:
            clauses.append("object_set_digest = ?")
            parameters.append(
                _typed_digest(object_set_digest, code="FEDERATED_DURABILITY_OBJECT_SET_DIGEST_INVALID")
            )
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM federated_copies" + where + " ORDER BY committed_at, transfer_id",
                tuple(parameters),
            ).fetchall()
        return [self._record(row) for row in rows]


def _require_sender_transfer(
    ledger: FederatedDurabilityLedger,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    sender_journal: federation_transfer_journal.FederatedTransferJournal,
    transfer_id: str,
) -> dict[str, Any]:
    transfer = sender_journal.get_transfer(transfer_id)
    if transfer is None:
        raise FederatedDurabilityError("FEDERATION_TRANSFER_NOT_FOUND")
    local_identity = ledger.local_identity
    if (
        sender_journal.local_identity != local_identity
        or peer_registry.local_identity != local_identity
        or transfer.get("localFleetId") != local_identity.get("fleetId")
        or transfer.get("sourceFleetId") != local_identity.get("fleetId")
        or transfer.get("role") != federation_transfer_journal.ROLE_SENDER
    ):
        raise FederatedDurabilityError("FEDERATED_DURABILITY_LOCAL_IDENTITY_MISMATCH")
    state_index = federation_transfer_journal.TRANSFER_STATES.index(str(transfer["state"]))
    verified_index = federation_transfer_journal.TRANSFER_STATES.index(
        federation_transfer_journal.STATE_REMOTE_COMMITTED
    )
    if state_index < verified_index:
        raise FederatedDurabilityError("FEDERATION_TRANSFER_NOT_VERIFIED")
    return transfer


def _remote_commit_event(
    sender_journal: federation_transfer_journal.FederatedTransferJournal,
    transfer_id: str,
) -> dict[str, Any]:
    events = [
        event
        for event in sender_journal.list_transfer_events(transfer_id)
        if event["nextState"] == federation_transfer_journal.STATE_REMOTE_COMMITTED
    ]
    if len(events) != 1:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_REMOTE_COMMIT_EVENT_INVALID")
    return events[0]


def _accepted_attestation(
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    transfer: dict[str, Any],
) -> dict[str, Any]:
    destination = str(transfer["destinationFleetId"])
    accepted = peer_registry.get_replica_attestation(destination, str(transfer["transferId"]))
    if accepted is None:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_ATTESTATION_NOT_ACCEPTED")
    attestation = accepted.get("attestation")
    if type(attestation) is not dict:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_ATTESTATION_RECORD_INVALID")
    digest = federated_replica_attestation.attestation_digest(attestation)
    if (
        accepted.get("peerFleetId") != destination
        or accepted.get("transferId") != transfer["transferId"]
        or accepted.get("attestationDigest") != digest
        or accepted.get("sequence") != attestation.get("sequence")
        or accepted.get("signerKeyId") != attestation.get("signerKeyId")
    ):
        raise FederatedDurabilityError("FEDERATED_DURABILITY_ATTESTATION_RECORD_INVALID")
    return accepted


def _expected_remote_details(attestation: dict[str, Any]) -> dict[str, Any]:
    return {
        "targetId": attestation["remoteTargetId"],
        "objectSetDigest": attestation["objectSetDigest"],
        "remoteReceiptDigest": attestation["remoteReceiptDigest"],
        "remoteCommitDigest": attestation["remoteCommitDigest"],
        "attestationDigest": federated_replica_attestation.attestation_digest(attestation),
        "attestationSequence": attestation["sequence"],
        "signerKeyId": attestation["signerKeyId"],
        "failureDomain": attestation["failureDomain"],
        "committedAt": attestation["committedAt"],
    }


def _verify_event_binding(
    sender_journal: federation_transfer_journal.FederatedTransferJournal,
    transfer: dict[str, Any],
    attestation: dict[str, Any],
) -> None:
    expected = _expected_remote_details(attestation)
    event = _remote_commit_event(sender_journal, str(transfer["transferId"]))
    if event.get("stateDetails") != expected:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_REMOTE_COMMIT_EVENT_CONFLICT")


def _copy_evidence(
    transfer: dict[str, Any],
    accepted: dict[str, Any],
    attestation: dict[str, Any],
    metadata: dict[str, str],
) -> dict[str, Any]:
    return {
        "schema": FEDERATED_COPY_RECORD_SCHEMA,
        "status": FEDERATED_COMMITTED,
        "transferId": transfer["transferId"],
        "sourceFleetId": transfer["sourceFleetId"],
        "destinationFleetId": transfer["destinationFleetId"],
        "policyId": transfer["policyId"],
        "backupId": transfer["backupId"],
        "objectSetDigest": transfer["objectSetDigest"],
        "remoteTargetId": attestation["remoteTargetId"],
        "remoteReceiptDigest": attestation["remoteReceiptDigest"],
        "remoteCommitDigest": attestation["remoteCommitDigest"],
        "attestationDigest": accepted["attestationDigest"],
        "attestationSequence": accepted["sequence"],
        "signerKeyId": accepted["signerKeyId"],
        "failureDomain": attestation["failureDomain"],
        "peerMetadata": metadata,
        "committedAt": attestation["committedAt"],
        "attestationAcceptedAt": accepted["acceptedAt"],
        "localDurabilityCredit": False,
    }


def _local_record_details(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": FEDERATED_COMMITTED,
        "federatedCopyRecordDigest": record["recordDigest"],
        "attestationDigest": record["attestationDigest"],
    }


def _success_details(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "outcome": FEDERATED_COMMITTED,
        "federatedCopyRecordDigest": record["recordDigest"],
    }


def _event_for_state(
    sender_journal: federation_transfer_journal.FederatedTransferJournal,
    transfer_id: str,
    state: str,
) -> dict[str, Any] | None:
    matches = [event for event in sender_journal.list_transfer_events(transfer_id) if event["nextState"] == state]
    if len(matches) > 1:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_SENDER_COMPLETION_CONFLICT")
    return matches[0] if matches else None


def _complete_sender_journal(
    sender_journal: federation_transfer_journal.FederatedTransferJournal,
    transfer_id: str,
    record: dict[str, Any],
    *,
    now: datetime,
) -> None:
    transfer = sender_journal.get_transfer(transfer_id)
    if transfer is None:
        raise FederatedDurabilityError("FEDERATION_TRANSFER_NOT_FOUND")
    local_details = _local_record_details(record)
    success_details = _success_details(record)
    state = str(transfer["state"])
    if state == federation_transfer_journal.STATE_REMOTE_COMMITTED:
        try:
            transfer = sender_journal.advance_transfer(
                transfer_id,
                expected_revision=int(transfer["revision"]),
                next_state=federation_transfer_journal.STATE_LOCAL_RECORDED,
                details=local_details,
                now=now,
            )
        except federation_transfer_journal.FederatedTransferJournalError as exc:
            raise FederatedDurabilityError(exc.code) from exc
        state = str(transfer["state"])
    local_event = _event_for_state(sender_journal, transfer_id, federation_transfer_journal.STATE_LOCAL_RECORDED)
    if state in {federation_transfer_journal.STATE_LOCAL_RECORDED, federation_transfer_journal.STATE_SUCCEEDED}:
        if local_event is None or local_event.get("stateDetails") != local_details:
            raise FederatedDurabilityError("FEDERATED_DURABILITY_SENDER_COMPLETION_CONFLICT")
    if state == federation_transfer_journal.STATE_LOCAL_RECORDED:
        try:
            transfer = sender_journal.advance_transfer(
                transfer_id,
                expected_revision=int(transfer["revision"]),
                next_state=federation_transfer_journal.STATE_SUCCEEDED,
                details=success_details,
                now=now,
            )
        except federation_transfer_journal.FederatedTransferJournalError as exc:
            raise FederatedDurabilityError(exc.code) from exc
        state = str(transfer["state"])
    success_event = _event_for_state(sender_journal, transfer_id, federation_transfer_journal.STATE_SUCCEEDED)
    if state != federation_transfer_journal.STATE_SUCCEEDED or success_event is None or success_event.get("stateDetails") != success_details:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_SENDER_COMPLETION_CONFLICT")


def _validate_existing_record(
    record: dict[str, Any],
    transfer: dict[str, Any],
    accepted: dict[str, Any],
    sender_journal: federation_transfer_journal.FederatedTransferJournal,
) -> None:
    attestation = accepted["attestation"]
    metadata = _metadata(record.get("peerMetadata"))
    expected = _copy_evidence(transfer, accepted, attestation, metadata)
    if _record_identity(record) != expected:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_RECORD_IDENTITY_CONFLICT")
    if attestation.get("failureDomain") != federated_replica_attestation.failure_domain_from_metadata(metadata):
        raise FederatedDurabilityError("FEDERATED_DURABILITY_RECORD_IDENTITY_CONFLICT")
    _verify_event_binding(sender_journal, transfer, attestation)


def record_verified_federated_copy(
    *,
    ledger: FederatedDurabilityLedger,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    sender_journal: federation_transfer_journal.FederatedTransferJournal,
    transfer_id: str,
    now: datetime,
) -> dict[str, Any]:
    """Record custody only after two durable, independently bound proof facts."""

    current = _parse_timestamp(_utc_iso(now))
    transfer = _require_sender_transfer(ledger, peer_registry, sender_journal, transfer_id)
    accepted = _accepted_attestation(peer_registry, transfer)
    existing = ledger.get_copy(str(transfer["transferId"]))
    if existing is not None:
        _validate_existing_record(existing, transfer, accepted, sender_journal)
        _complete_sender_journal(sender_journal, str(transfer["transferId"]), existing, now=current)
        return existing
    if transfer["state"] != federation_transfer_journal.STATE_REMOTE_COMMITTED:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_LEDGER_RECORD_MISSING")

    destination = str(transfer["destinationFleetId"])
    try:
        peer = peer_registry.require_active_peer(destination)
    except federation_peer_trust.FederationTrustError as exc:
        raise FederatedDurabilityError(exc.code) from exc
    metadata = _metadata(peer.get("pinnedMetadata"))
    attestation = accepted["attestation"]
    certificate = attestation.get("signerCertificate")
    root_identity = peer.get("fleetIdentity")
    if type(certificate) is not dict or type(root_identity) is not dict:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_ATTESTATION_TRUST_INVALID")
    try:
        peer_registry.authorize_online_signer(
            destination,
            certificate,
            purpose=federation_identity.PURPOSE_REPLICA_ATTESTATION,
            mode=federation_peer_trust.AUTHORIZATION_CURRENT,
            validation_time=current,
        )
        verified = federation_identity.verify_federation_document(
            attestation,
            certificate=certificate,
            root_identity=root_identity,
            expected_schema=federated_replica_attestation.REPLICA_ATTESTATION_SCHEMA,
            now=current,
            required_purpose=federation_identity.PURPOSE_REPLICA_ATTESTATION,
        )
    except (federation_peer_trust.FederationTrustError, federation_identity.FederationIdentityError) as exc:
        raise FederatedDurabilityError(exc.code) from exc
    try:
        federated_replica_attestation._attestation_semantics(
            verified,
            transfer=transfer,
            pinned_metadata=metadata,
            now=current,
            max_future_skew_seconds=30,
        )
    except federated_replica_attestation.FederatedReplicaAttestationError as exc:
        raise FederatedDurabilityError(exc.code) from exc
    _verify_event_binding(sender_journal, transfer, verified)
    record = ledger._record_verified_copy(
        _copy_evidence(transfer, accepted, verified, metadata),
        recorded_at=current,
    )
    _complete_sender_journal(sender_journal, str(transfer["transferId"]), record, now=current)
    return record


def evaluate_federated_durability(
    *,
    policy: dict[str, Any],
    backup_id: str,
    object_set_digest: str,
    ledger: FederatedDurabilityLedger,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    now: datetime,
) -> dict[str, Any]:
    """Evaluate only the offsite objective; no local copy receives credit."""

    current = _parse_timestamp(_utc_iso(now))
    if type(policy) is not dict:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_POLICY_INVALID")
    policy_id = _control_id(policy.get("policyId"), code="FEDERATED_DURABILITY_POLICY_ID_INVALID")
    backup = _control_id(backup_id, code="FEDERATED_DURABILITY_BACKUP_ID_INVALID")
    object_digest = _typed_digest(
        object_set_digest,
        code="FEDERATED_DURABILITY_OBJECT_SET_DIGEST_INVALID",
    )
    try:
        objective = backup_policies._normalize_federated_durability(policy.get("federatedDurability"))
    except AppError as exc:
        raise FederatedDurabilityError("FEDERATED_DURABILITY_OBJECTIVE_INVALID") from exc
    enabled = bool(objective["enabled"])
    records = ledger.list_copies(
        policy_id=policy_id,
        backup_id=backup,
        object_set_digest=object_digest,
    )
    credited: list[dict[str, Any]] = []
    issues: list[str] = []
    if enabled:
        for record in records:
            destination = str(record["destinationFleetId"])
            issue: str | None = None
            if destination not in objective["allowedPeerFleets"]:
                issue = "FEDERATED_PEER_NOT_ALLOWED"
            elif record["peerMetadata"]["jurisdiction"] not in objective["allowedJurisdictions"]:
                issue = "FEDERATED_JURISDICTION_NOT_ALLOWED"
            else:
                committed_at = _parse_timestamp(record["committedAt"])
                age = (current - committed_at).total_seconds()
                if age < 0:
                    issue = "FEDERATED_COPY_FROM_FUTURE"
                elif age > int(objective["maxFederatedCopyAge"]):
                    issue = "FEDERATED_COPY_TOO_OLD"
            if issue is None:
                try:
                    peer = peer_registry.require_active_peer(destination)
                    pinned = _metadata(peer.get("pinnedMetadata"))
                    accepted = peer_registry.get_replica_attestation(destination, str(record["transferId"]))
                except (federation_peer_trust.FederationTrustError, FederatedDurabilityError) as exc:
                    issue = exc.code
                else:
                    if pinned != record["peerMetadata"]:
                        issue = "FEDERATED_PEER_METADATA_MISMATCH"
                    elif accepted is None or accepted.get("attestationDigest") != record["attestationDigest"]:
                        issue = "FEDERATED_ATTESTATION_RECORD_MISSING"
            if issue is None:
                credited.append(record)
            else:
                issues.append(issue)
    fleets = sorted({str(record["destinationFleetId"]) for record in credited})
    domains = sorted({str(record["failureDomain"]) for record in credited})
    satisfied = not enabled or (
        len(credited) >= int(objective["minFederatedCopies"])
        and len(fleets) >= int(objective["minDistinctFleets"])
    )
    if enabled and len(credited) < int(objective["minFederatedCopies"]):
        issues.append("FEDERATED_COPY_OBJECTIVE_UNSATISFIED")
    if enabled and len(fleets) < int(objective["minDistinctFleets"]):
        issues.append("FEDERATED_FLEET_DIVERSITY_UNSATISFIED")
    return {
        "schema": FEDERATED_DURABILITY_STATUS_SCHEMA,
        "policyId": policy_id,
        "backupId": backup,
        "objectSetDigest": object_digest,
        "evaluatedAt": _utc_iso(current),
        "objectiveEnabled": enabled,
        "requiredFederatedCopies": int(objective["minFederatedCopies"]),
        "requiredDistinctFleets": int(objective["minDistinctFleets"]),
        "maxFederatedCopyAge": int(objective["maxFederatedCopyAge"]),
        "allowedPeerFleets": list(objective["allowedPeerFleets"]),
        "allowedJurisdictions": list(objective["allowedJurisdictions"]),
        "federatedCopies": len(credited),
        "distinctFleets": len(fleets),
        "distinctFailureDomains": len(domains),
        "creditedTransferIds": sorted(str(record["transferId"]) for record in credited),
        "issues": sorted(set(issues)),
        "satisfied": satisfied,
        "localDurabilityCredit": 0,
    }


def get_ledger(
    local_identity: dict[str, Any],
    db_path: Path | None = None,
) -> FederatedDurabilityLedger:
    return FederatedDurabilityLedger(db_path or FEDERATED_DURABILITY_DB, local_identity)

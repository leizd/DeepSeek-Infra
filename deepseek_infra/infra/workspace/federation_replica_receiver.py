"""Durable receiver-side staging for existing encrypted object-set-v1 replicas.

The receiver never decrypts or re-encrypts component bytes.  It authorizes
each write through a receiver-issued ingress grant, verifies the exact
ciphertext size and SHA-256 commitment, and only then exposes a verified
``ObjectSetPackage`` to the existing production publish path.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_object_set,
    backup_publish,
    backup_unattended,
    federation_identity,
    federation_ingress_grant,
    federation_peer_trust,
    federation_transfer_journal,
)


FEDERATED_REPLICA_RECEIVER_DB = config.ROOT / ".federation" / "replica-receiver.sqlite3"
FEDERATED_REPLICA_STAGING_DIR = config.ROOT / ".federation" / "replica-staging"

REPLICA_DECLARATION_SCHEMA = "federated-replica-object-set-declaration-v1"
REPLICA_COMPONENT_SCHEMA = "federated-replica-component-v1"

STATE_RECEIVING = "RECEIVING"
STATE_VERIFIED = "VERIFIED"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TYPED_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHUNK_BYTES = 1024 * 1024
MAX_SOURCE_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_REPLICA_COMPONENTS = 10_000

_CREATE_IDENTITY_SQL = """
CREATE TABLE IF NOT EXISTS federated_replica_receiver_identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    fleet_id TEXT NOT NULL,
    root_fingerprint TEXT NOT NULL,
    identity_digest TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    bound_at TEXT NOT NULL
)
"""

_CREATE_DECLARATIONS_SQL = """
CREATE TABLE IF NOT EXISTS federated_replica_declarations (
    transfer_id TEXT PRIMARY KEY,
    declaration_digest TEXT NOT NULL,
    declaration_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('RECEIVING', 'VERIFIED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    verified_at TEXT,
    revision INTEGER NOT NULL CHECK(revision >= 1)
)
"""

_CREATE_COMPONENTS_SQL = """
CREATE TABLE IF NOT EXISTS federated_replica_components (
    transfer_id TEXT NOT NULL,
    ciphertext_digest TEXT NOT NULL,
    ciphertext_size INTEGER NOT NULL CHECK(ciphertext_size > 0),
    is_control INTEGER NOT NULL CHECK(is_control IN (0, 1)),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    grant_id TEXT,
    write_id TEXT,
    object_key TEXT,
    received_at TEXT,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    PRIMARY KEY(transfer_id, ciphertext_digest),
    UNIQUE(transfer_id, ordinal),
    FOREIGN KEY(transfer_id) REFERENCES federated_replica_declarations(transfer_id)
)
"""


class FederatedReplicaReceiverError(RuntimeError):
    """Fail-closed receiver error with a stable machine-readable code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = str(code)
        super().__init__(message or self.code)


def _canonical_json(value: Any) -> str:
    try:
        return federation_identity.canonical_federation_json(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederatedReplicaReceiverError("FEDERATION_REPLICA_CANONICAL_PAYLOAD_INVALID") from exc


def _normalize(value: Any) -> Any:
    try:
        return federation_identity.normalize_federation_json(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederatedReplicaReceiverError("FEDERATION_REPLICA_CANONICAL_PAYLOAD_INVALID") from exc


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise FederatedReplicaReceiverError("FEDERATION_REPLICA_TIMESTAMP_INVALID")
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if type(value) is not str:
        raise FederatedReplicaReceiverError("FEDERATION_REPLICA_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FederatedReplicaReceiverError("FEDERATION_REPLICA_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or _utc_iso(parsed) != value:
        raise FederatedReplicaReceiverError("FEDERATION_REPLICA_TIMESTAMP_INVALID")
    return parsed.astimezone(timezone.utc)


def _plain_digest(value: Any, *, code: str) -> str:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise FederatedReplicaReceiverError(code)
    return value


def _typed_digest(value: Any, *, code: str) -> str:
    if type(value) is not str or _TYPED_SHA256_PATTERN.fullmatch(value) is None:
        raise FederatedReplicaReceiverError(code)
    return value


def storage_object_set_digest(value: Any) -> str:
    """Convert a typed Federation digest to the frozen storage-wire form."""

    return _typed_digest(value, code="FEDERATION_REPLICA_OBJECT_SET_DIGEST_INVALID")[len("sha256:") :]


def federation_object_set_digest(value: Any) -> str:
    """Convert a frozen storage-wire objectSetDigest to typed control form."""

    return "sha256:" + _plain_digest(value, code="FEDERATION_REPLICA_STORAGE_OBJECT_SET_DIGEST_INVALID")


def _receipt_manifest(receipt: dict[str, Any]) -> dict[str, Any]:
    snapshot_kind = receipt.get("snapshotKind")
    if type(snapshot_kind) is not str or not snapshot_kind:
        raise FederatedReplicaReceiverError("FEDERATION_REPLICA_SNAPSHOT_KIND_INVALID")
    chain_depth = receipt.get("chainDepth")
    if isinstance(chain_depth, bool) or not isinstance(chain_depth, int) or chain_depth < 0:
        raise FederatedReplicaReceiverError("FEDERATION_REPLICA_LINEAGE_INVALID")
    string_fields: dict[str, str | None] = {}
    for field in ("lineageId", "parentBackupId", "baseBackupId", "chunkProtocol"):
        value = receipt.get(field)
        if value is not None and (type(value) is not str or not value):
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_LINEAGE_INVALID")
        string_fields[field] = value
    manifest: dict[str, Any] = {"snapshotKind": snapshot_kind}
    if string_fields["chunkProtocol"] is not None:
        manifest["chunkProtocol"] = string_fields["chunkProtocol"]
    manifest["snapshot"] = {
        "kind": snapshot_kind,
        "lineageId": string_fields["lineageId"],
        "parentBackupId": string_fields["parentBackupId"],
        "baseBackupId": string_fields["baseBackupId"],
        "chainDepth": chain_depth,
        "chunkProtocol": string_fields["chunkProtocol"],
    }
    return manifest


class FederatedReplicaReceiver:
    """Receiver-owned component journal and ciphertext staging area."""

    def __init__(
        self,
        *,
        transfer_journal: federation_transfer_journal.FederatedTransferJournal,
        peer_registry: federation_peer_trust.PeerTrustRegistry,
        db_path: Path | None = None,
        staging_dir: Path | None = None,
    ) -> None:
        journal_identity = transfer_journal.local_identity
        registry_identity = peer_registry.local_identity
        if _canonical_json(journal_identity) != _canonical_json(registry_identity):
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_LOCAL_IDENTITY_CONFLICT")
        self._local_identity = journal_identity
        self._transfer_journal = transfer_journal
        self._peer_registry = peer_registry
        self._db_path = Path(db_path or FEDERATED_REPLICA_RECEIVER_DB)
        self._staging_dir = Path(staging_dir or FEDERATED_REPLICA_STAGING_DIR)
        self._ensure_schema()
        self._bind_identity()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def staging_dir(self) -> Path:
        return self._staging_dir

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
            connection.execute(_CREATE_IDENTITY_SQL)
            connection.execute(_CREATE_DECLARATIONS_SQL)
            connection.execute(_CREATE_COMPONENTS_SQL)

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

    def _bind_identity(self) -> None:
        identity_json = _canonical_json(self._local_identity)
        identity_digest = _digest(self._local_identity)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM federated_replica_receiver_identity WHERE singleton = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO federated_replica_receiver_identity (
                        singleton, fleet_id, root_fingerprint,
                        identity_digest, identity_json, bound_at
                    ) VALUES (1, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._local_identity["fleetId"],
                        self._local_identity["rootFingerprint"],
                        identity_digest,
                        identity_json,
                        _utc_iso(),
                    ),
                )
                return
            if (
                row["fleet_id"] != self._local_identity["fleetId"]
                or row["root_fingerprint"] != self._local_identity["rootFingerprint"]
                or row["identity_digest"] != identity_digest
                or row["identity_json"] != identity_json
            ):
                raise FederatedReplicaReceiverError("FEDERATION_REPLICA_RECEIVER_IDENTITY_CONFLICT")

    @staticmethod
    def _declaration_record(row: sqlite3.Row) -> dict[str, Any]:
        declaration = json.loads(str(row["declaration_json"]))
        if _digest(declaration) != str(row["declaration_digest"]):
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_DECLARATION_COMMITMENT_INVALID")
        return {
            **declaration,
            "declarationDigest": str(row["declaration_digest"]),
            "state": str(row["state"]),
            "createdAt": str(row["created_at"]),
            "updatedAt": str(row["updated_at"]),
            "verifiedAt": row["verified_at"],
            "revision": int(row["revision"]),
        }

    @staticmethod
    def _component_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": REPLICA_COMPONENT_SCHEMA,
            "transferId": str(row["transfer_id"]),
            "ciphertextDigest": str(row["ciphertext_digest"]),
            "ciphertextSize": int(row["ciphertext_size"]),
            "control": bool(row["is_control"]),
            "ordinal": int(row["ordinal"]),
            "grantId": row["grant_id"],
            "writeId": row["write_id"],
            "objectKey": row["object_key"],
            "receivedAt": row["received_at"],
            "revision": int(row["revision"]),
        }

    def _transfer_record(self, transfer_id: str) -> dict[str, Any]:
        try:
            record = self._transfer_journal.get_transfer(transfer_id)
        except federation_transfer_journal.FederatedTransferJournalError as exc:
            raise FederatedReplicaReceiverError(exc.code) from exc
        if record is None:
            raise FederatedReplicaReceiverError("FEDERATION_TRANSFER_NOT_FOUND")
        if (
            record.get("role") != federation_transfer_journal.ROLE_RECEIVER
            or record.get("destinationFleetId") != self._local_identity["fleetId"]
            or record.get("localFleetId") != self._local_identity["fleetId"]
        ):
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_RECEIVER_FLEET_MISMATCH")
        return record

    def _grant_record(self, grant: dict[str, Any], transfer: dict[str, Any], *, now: datetime) -> dict[str, Any]:
        if type(grant) is not dict or type(grant.get("grantId")) is not str:
            raise FederatedReplicaReceiverError("FEDERATION_INGRESS_GRANT_DOCUMENT_INVALID")
        try:
            record = self._peer_registry.get_ingress_grant(str(grant["grantId"]))
            self._peer_registry.require_active_peer(str(transfer["sourceFleetId"]))
        except federation_peer_trust.FederationTrustError as exc:
            raise FederatedReplicaReceiverError(exc.code) from exc
        if record is None:
            raise FederatedReplicaReceiverError("FEDERATION_INGRESS_GRANT_NOT_FOUND")
        try:
            supplied_digest = federation_ingress_grant.grant_digest(grant)
        except federation_ingress_grant.FederationIngressGrantError as exc:
            raise FederatedReplicaReceiverError(exc.code) from exc
        if record.get("grantDigest") != supplied_digest or _canonical_json(record.get("grant")) != _canonical_json(grant):
            raise FederatedReplicaReceiverError("FEDERATION_INGRESS_GRANT_IDENTITY_CONFLICT")
        expected = {
            "sourceFleetId": transfer["sourceFleetId"],
            "destinationFleetId": transfer["destinationFleetId"],
            "transferId": transfer["transferId"],
            "policyId": transfer["policyId"],
            "backupId": transfer["backupId"],
            "objectSetDigest": transfer["objectSetDigest"],
        }
        if any(record.get(field) != value or grant.get(field) != value for field, value in expected.items()):
            raise FederatedReplicaReceiverError("FEDERATION_INGRESS_GRANT_BINDING_MISMATCH")
        current = _parse_timestamp(_utc_iso(now))
        if current < _parse_timestamp(record.get("issuedAt")):
            raise FederatedReplicaReceiverError("FEDERATION_INGRESS_GRANT_FROM_FUTURE")
        if current >= _parse_timestamp(record.get("expiresAt")):
            raise FederatedReplicaReceiverError("FEDERATION_INGRESS_GRANT_EXPIRED")
        if record.get("state") != federation_peer_trust.INGRESS_GRANT_STATE_ACTIVE:
            raise FederatedReplicaReceiverError("FEDERATION_INGRESS_GRANT_NOT_ACTIVE")
        return record

    def declare_object_set(
        self,
        *,
        grant: dict[str, Any],
        transfer_id: str,
        source_receipt: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        """Persist the exact role-aware descriptor for one frozen object-set-v1."""

        transfer = self._transfer_record(transfer_id)
        grant_record = self._grant_record(grant, transfer, now=now)
        normalized = _normalize(source_receipt)
        if type(normalized) is not dict:
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_SOURCE_RECEIPT_INVALID")
        receipt = normalized
        if len(_canonical_json(receipt).encode("utf-8")) > MAX_SOURCE_RECEIPT_BYTES:
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_SOURCE_RECEIPT_TOO_LARGE")
        if type(receipt.get("schemaVersion")) is not int or receipt.get("schemaVersion") != backup_publish.RECEIPT_SCHEMA_VERSION:
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_RECEIPT_VERSION_INVALID")
        if receipt.get("storageProtocol") != backup_object_set.OBJECT_SET_V1:
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_STORAGE_PROTOCOL_INVALID")
        if receipt.get("backupId") != transfer["backupId"] or receipt.get("policyId") != transfer["policyId"]:
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_RECEIPT_IDENTITY_MISMATCH")
        if receipt.get("creationVerified") is not True:
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_SOURCE_CREATION_UNVERIFIED")
        storage_digest = _plain_digest(
            receipt.get("objectSetDigest"),
            code="FEDERATION_REPLICA_STORAGE_OBJECT_SET_DIGEST_INVALID",
        )
        if federation_object_set_digest(storage_digest) != transfer["objectSetDigest"]:
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_OBJECT_SET_DIGEST_MISMATCH")
        control_digest = _plain_digest(
            receipt.get("controlObjectDigest"),
            code="FEDERATION_REPLICA_CONTROL_OBJECT_INVALID",
        )
        raw_objects = receipt.get("objects")
        if not isinstance(raw_objects, list) or not (1 <= len(raw_objects) <= MAX_REPLICA_COMPONENTS):
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_COMPONENT_COUNT_INVALID")
        if control_digest not in {
            str(item.get("digest") or "") for item in raw_objects if isinstance(item, dict)
        }:
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_CONTROL_OBJECT_INVALID")
        try:
            objects = backup_object_set.committed_object_inventory(receipt)
        except AppError as exc:
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_OBJECT_INVENTORY_INVALID") from exc
        total_bytes = sum(int(item["size"]) for item in objects)
        if any(int(item["size"]) <= 0 for item in objects):
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_COMPONENT_SIZE_INVALID")
        if type(receipt.get("size")) is not int or receipt.get("size") != total_bytes:
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_RECEIPT_SIZE_MISMATCH")
        if total_bytes > int(grant_record["maxBytes"]):
            raise FederatedReplicaReceiverError("FEDERATION_INGRESS_MAX_BYTES_EXCEEDED")
        manifest = _receipt_manifest(receipt)
        declaration = {
            "schema": REPLICA_DECLARATION_SCHEMA,
            "transferId": transfer["transferId"],
            "sourceFleetId": transfer["sourceFleetId"],
            "destinationFleetId": transfer["destinationFleetId"],
            "policyId": transfer["policyId"],
            "backupId": transfer["backupId"],
            "storageProtocol": backup_object_set.OBJECT_SET_V1,
            "objectSetDigest": transfer["objectSetDigest"],
            "storageObjectSetDigest": storage_digest,
            "controlObjectDigest": control_digest,
            "objects": objects,
            "receiptManifest": manifest,
            "creationVerified": True,
            "totalBytes": total_bytes,
        }
        declaration_json = _canonical_json(declaration)
        declaration_digest = _digest(declaration)
        timestamp = _utc_iso(now)
        ordered_objects = sorted(
            objects,
            key=lambda item: (str(item["digest"]) != control_digest, str(item["digest"])),
        )
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM federated_replica_declarations WHERE transfer_id = ?",
                (transfer_id,),
            ).fetchone()
            if existing is not None:
                if existing["declaration_digest"] != declaration_digest or existing["declaration_json"] != declaration_json:
                    raise FederatedReplicaReceiverError("FEDERATION_REPLICA_DECLARATION_IDENTITY_CONFLICT")
                return self._declaration_record(existing)
            connection.execute(
                """
                INSERT INTO federated_replica_declarations (
                    transfer_id, declaration_digest, declaration_json, state,
                    created_at, updated_at, revision
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (transfer_id, declaration_digest, declaration_json, STATE_RECEIVING, timestamp, timestamp),
            )
            for ordinal, item in enumerate(ordered_objects):
                connection.execute(
                    """
                    INSERT INTO federated_replica_components (
                        transfer_id, ciphertext_digest, ciphertext_size,
                        is_control, ordinal, revision
                    ) VALUES (?, ?, ?, ?, ?, 1)
                    """,
                    (
                        transfer_id,
                        item["digest"],
                        item["size"],
                        int(item["digest"] == control_digest),
                        ordinal,
                    ),
                )
            created = connection.execute(
                "SELECT * FROM federated_replica_declarations WHERE transfer_id = ?",
                (transfer_id,),
            ).fetchone()
            assert created is not None
            return self._declaration_record(created)

    def get_declaration(self, transfer_id: str) -> dict[str, Any] | None:
        self._transfer_record(transfer_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM federated_replica_declarations WHERE transfer_id = ?",
                (transfer_id,),
            ).fetchone()
        return None if row is None else self._declaration_record(row)

    def list_components(self, transfer_id: str) -> list[dict[str, Any]]:
        self._transfer_record(transfer_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM federated_replica_components
                WHERE transfer_id = ? ORDER BY ordinal
                """,
                (transfer_id,),
            ).fetchall()
        return [self._component_record(row) for row in rows]

    def _component_path(self, transfer_id: str, ciphertext_digest: str) -> Path:
        transfer_hex = _typed_digest(transfer_id, code="FEDERATION_TRANSFER_ID_INVALID")[len("sha256:") :]
        digest = _plain_digest(ciphertext_digest, code="FEDERATION_REPLICA_COMPONENT_DIGEST_INVALID")
        return self._staging_dir / transfer_hex / "objects" / "sha256" / digest[:2] / f"{digest}.age"

    @staticmethod
    def _verify_path(path: Path, *, expected_size: int, expected_digest: str) -> bool:
        return (
            path.is_file()
            and path.stat().st_size == expected_size
            and backup_unattended.sha256_file(path) == expected_digest
        )

    def receive_component(
        self,
        *,
        grant: dict[str, Any],
        transfer_id: str,
        component_digest: str,
        write_id: str,
        content: BinaryIO | bytes | bytearray,
        now: datetime,
    ) -> dict[str, Any]:
        """Authorize, stream, verify, and durably record one ciphertext member."""

        transfer = self._transfer_record(transfer_id)
        grant_record = self._grant_record(grant, transfer, now=now)
        digest = _plain_digest(component_digest, code="FEDERATION_REPLICA_COMPONENT_DIGEST_INVALID")
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM federated_replica_components
                WHERE transfer_id = ? AND ciphertext_digest = ?
                """,
                (transfer_id, digest),
            ).fetchone()
        if row is None:
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_COMPONENT_NOT_DECLARED")
        expected_size = int(row["ciphertext_size"])
        prefix = str(grant_record["allowedObjectPrefix"])
        object_key = f"{prefix}objects/sha256/{digest[:2]}/{digest}.age"
        if row["received_at"] is not None and (
            row["grant_id"] != grant_record["grantId"] or row["write_id"] != write_id
        ):
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_COMPONENT_WRITE_REPLAY")
        try:
            reservation = federation_ingress_grant.authorize_ingress_write(
                grant,
                peer_registry=self._peer_registry,
                source_fleet_id=str(transfer["sourceFleetId"]),
                write_id=write_id,
                object_key=object_key,
                byte_count=expected_size,
                now=now,
            )
        except federation_ingress_grant.FederationIngressGrantError as exc:
            if exc.code == "FEDERATION_INGRESS_OBJECT_WRITE_IDENTITY_CONFLICT":
                raise FederatedReplicaReceiverError("FEDERATION_REPLICA_COMPONENT_WRITE_REPLAY") from exc
            raise FederatedReplicaReceiverError(exc.code) from exc
        if reservation.get("objectKey") != object_key or reservation.get("byteCount") != expected_size:
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_WRITE_RESERVATION_INVALID")

        source: BinaryIO
        if isinstance(content, (bytes, bytearray)):
            source = io.BytesIO(bytes(content))
        elif hasattr(content, "read"):
            source = content
        else:
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_COMPONENT_STREAM_INVALID")
        destination = self._component_path(transfer_id, digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.part")
        observed_size = 0
        observed_digest = hashlib.sha256()
        try:
            with temporary.open("xb") as output:
                while True:
                    chunk = source.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise FederatedReplicaReceiverError("FEDERATION_REPLICA_COMPONENT_STREAM_INVALID")
                    observed_size += len(chunk)
                    if observed_size > expected_size:
                        raise FederatedReplicaReceiverError("FEDERATION_REPLICA_COMPONENT_SIZE_MISMATCH")
                    observed_digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if observed_size != expected_size:
                raise FederatedReplicaReceiverError("FEDERATION_REPLICA_COMPONENT_SIZE_MISMATCH")
            if observed_digest.hexdigest() != digest:
                raise FederatedReplicaReceiverError("FEDERATION_REPLICA_COMPONENT_DIGEST_MISMATCH")
            if destination.exists():
                if not self._verify_path(destination, expected_size=expected_size, expected_digest=digest):
                    raise FederatedReplicaReceiverError("FEDERATION_REPLICA_STAGED_COMPONENT_CONFLICT")
            else:
                os.replace(temporary, destination)
            if not self._verify_path(destination, expected_size=expected_size, expected_digest=digest):
                raise FederatedReplicaReceiverError("FEDERATION_REPLICA_STAGED_COMPONENT_INVALID")
            timestamp = _utc_iso(now)
            with self._write() as connection:
                current = connection.execute(
                    """
                    SELECT * FROM federated_replica_components
                    WHERE transfer_id = ? AND ciphertext_digest = ?
                    """,
                    (transfer_id, digest),
                ).fetchone()
                assert current is not None
                if current["received_at"] is not None:
                    if current["grant_id"] != grant_record["grantId"] or current["write_id"] != write_id:
                        raise FederatedReplicaReceiverError("FEDERATION_REPLICA_COMPONENT_WRITE_REPLAY")
                    return self._component_record(current)
                connection.execute(
                    """
                    UPDATE federated_replica_components
                    SET grant_id = ?, write_id = ?, object_key = ?,
                        received_at = ?, revision = revision + 1
                    WHERE transfer_id = ? AND ciphertext_digest = ? AND received_at IS NULL
                    """,
                    (grant_record["grantId"], write_id, object_key, timestamp, transfer_id, digest),
                )
                connection.execute(
                    """
                    UPDATE federated_replica_declarations
                    SET updated_at = ?, revision = revision + 1
                    WHERE transfer_id = ?
                    """,
                    (timestamp, transfer_id),
                )
                updated = connection.execute(
                    """
                    SELECT * FROM federated_replica_components
                    WHERE transfer_id = ? AND ciphertext_digest = ?
                    """,
                    (transfer_id, digest),
                ).fetchone()
                assert updated is not None
                return self._component_record(updated)
        finally:
            temporary.unlink(missing_ok=True)

    def assemble_verified_package(self, transfer_id: str) -> backup_object_set.ObjectSetPackage:
        """Re-verify every staged byte and expose the unchanged object-set package."""

        transfer = self._transfer_record(transfer_id)
        with closing(self._connect()) as connection:
            declaration_row = connection.execute(
                "SELECT * FROM federated_replica_declarations WHERE transfer_id = ?",
                (transfer_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT * FROM federated_replica_components WHERE transfer_id = ? ORDER BY ordinal",
                (transfer_id,),
            ).fetchall()
        if declaration_row is None:
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_DECLARATION_NOT_FOUND")
        if not rows or any(row["received_at"] is None for row in rows):
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_OBJECT_SET_INCOMPLETE")
        declaration = self._declaration_record(declaration_row)
        components: list[backup_object_set.EncryptedComponent] = []
        payload_ordinal = 0
        for row in rows:
            digest = str(row["ciphertext_digest"])
            size = int(row["ciphertext_size"])
            path = self._component_path(transfer_id, digest)
            if not self._verify_path(path, expected_size=size, expected_digest=digest):
                raise FederatedReplicaReceiverError("FEDERATION_REPLICA_STAGED_COMPONENT_INVALID")
            control = bool(row["is_control"])
            components.append(
                backup_object_set.EncryptedComponent(
                    component_id="control" if control else f"p{payload_ordinal:04d}",
                    path=path,
                    ciphertext_digest=digest,
                    ciphertext_size=size,
                    control=control,
                )
            )
            if not control:
                payload_ordinal += 1
        package = backup_object_set.ObjectSetPackage(
            backup_id=str(transfer["backupId"]),
            components=tuple(components),
            manifest_digest="",
            coverage_digest="",
            manifest=dict(declaration["receiptManifest"]),
            creation_verified=True,
        )
        if package.object_set_digest != declaration["storageObjectSetDigest"]:
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_OBJECT_SET_DIGEST_MISMATCH")
        if package.control.ciphertext_digest != declaration["controlObjectDigest"]:
            raise FederatedReplicaReceiverError("FEDERATION_REPLICA_CONTROL_OBJECT_INVALID")
        verified_at = _utc_iso()
        with self._write() as connection:
            connection.execute(
                """
                UPDATE federated_replica_declarations
                SET state = ?, verified_at = ?, updated_at = ?, revision = revision + 1
                WHERE transfer_id = ? AND state = ?
                """,
                (STATE_VERIFIED, verified_at, verified_at, transfer_id, STATE_RECEIVING),
            )
        return package


def open_federated_replica_receiver(
    *,
    transfer_journal: federation_transfer_journal.FederatedTransferJournal,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    db_path: Path | None = None,
    staging_dir: Path | None = None,
) -> FederatedReplicaReceiver:
    return FederatedReplicaReceiver(
        transfer_journal=transfer_journal,
        peer_registry=peer_registry,
        db_path=db_path,
        staging_dir=staging_dir,
    )

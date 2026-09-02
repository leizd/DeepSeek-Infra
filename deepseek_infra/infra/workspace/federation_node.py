"""Production Federation node orchestration over sovereign local journals."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, NoReturn

from deepseek_infra.infra.workspace import (
    backup_object_set,
    backup_publish,
    backup_recovery_credential,
    backup_targets,
    federated_dr_drill,
    federated_durability,
    federated_replica_attestation,
    federated_replica_commit,
    federation_challenge,
    federation_custody_capability,
    federation_identity,
    federation_ingress_grant,
    federation_peer_trust,
    federation_readiness_attestation,
    federation_replica_receiver,
    federation_transfer,
    federation_transfer_journal,
    resilience_federation_readiness,
)

NODE_HEALTH_SCHEMA = "federation-node-health-v1"
NODE_EFFECT_SCHEMA = "federation-node-effect-v1"
NODE_STATE_IDENTITY_SCHEMA = "federation-node-state-identity-v1"
NODE_CONFIG_SCHEMA = "federation-node-config-v1"

_NODE_CONFIG_FIELDS = {
    "schema",
    "fleetId",
    "publicIdentityPath",
    "signerBundlePath",
    "peerRegistryPath",
    "transferJournalPath",
    "receiverDbPath",
    "stagingDir",
    "durabilityDbPath",
    "custodyDbPath",
    "nodeStateDbPath",
    "remoteTargetId",
    "failureDomainMetadata",
    "readiness",
    "maxIngressBytes",
    "ownerInstanceId",
    "custody",
}

_TRANSFER_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_PLAIN_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_CREATE_IDENTITY_SQL = """
CREATE TABLE IF NOT EXISTS federation_node_identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    fleet_id TEXT NOT NULL,
    root_fingerprint TEXT NOT NULL,
    identity_digest TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    bound_at TEXT NOT NULL
)
"""
_CREATE_SEQUENCES_SQL = """
CREATE TABLE IF NOT EXISTS federation_node_sequences (
    namespace TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    updated_at TEXT NOT NULL
)
"""
_CREATE_EFFECTS_SQL = """
CREATE TABLE IF NOT EXISTS federation_node_effects (
    effect_key TEXT PRIMARY KEY,
    effect_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


class FederationNodeError(RuntimeError):
    """Fail-closed node orchestration error with a stable public code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FederationNodeError("FEDERATION_NODE_TIME_INVALID")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    try:
        return federation_identity.canonical_federation_json(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederationNodeError(exc.code) from exc


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _storage_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _document(value: Any, *, code: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise FederationNodeError(code)
    try:
        normalized = federation_identity.normalize_federation_json(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederationNodeError(code) from exc
    if type(normalized) is not dict:
        raise FederationNodeError(code)
    return normalized


def _exact_payload(value: Any, fields: set[str], *, code: str) -> dict[str, Any]:
    payload = _document(value, code=code)
    if set(payload) != fields:
        raise FederationNodeError(code)
    return payload


def _transfer_id(value: Any) -> str:
    if type(value) is not str or _TRANSFER_ID.fullmatch(value) is None:
        raise FederationNodeError("FEDERATION_TRANSFER_ID_INVALID")
    return value


def _control_id(value: Any, *, code: str) -> str:
    if type(value) is not str or _CONTROL_ID.fullmatch(value) is None:
        raise FederationNodeError(code)
    return value


def _positive_int(value: Any, *, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FederationNodeError(code)
    return value


def _decode_document(value: Any, *, code: str) -> bytes:
    if type(value) is not str or not value:
        raise FederationNodeError(code)
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise FederationNodeError(code) from exc


def _source_receipt(value: Any, *, expected_source_fleet_id: str | None = None) -> tuple[dict[str, Any], str]:
    receipt = _document(value, code="FEDERATION_SOURCE_RECEIPT_INVALID")
    if (
        receipt.get("schemaVersion") != backup_publish.RECEIPT_SCHEMA_VERSION
        or receipt.get("storageProtocol") != backup_object_set.OBJECT_SET_V1
        or receipt.get("creationVerified") is not True
    ):
        raise FederationNodeError("FEDERATION_SOURCE_RECEIPT_INVALID")
    _control_id(receipt.get("policyId"), code="FEDERATION_TRANSFER_POLICY_ID_INVALID")
    _control_id(receipt.get("backupId"), code="FEDERATION_TRANSFER_BACKUP_ID_INVALID")
    digest = receipt.get("objectSetDigest")
    if type(digest) is not str or _PLAIN_DIGEST.fullmatch(digest) is None:
        raise FederationNodeError("FEDERATION_SOURCE_RECEIPT_INVALID")
    if expected_source_fleet_id is not None and receipt.get("sourceFleetId") not in {None, expected_source_fleet_id}:
        raise FederationNodeError("FEDERATION_SOURCE_RECEIPT_FLEET_MISMATCH")
    try:
        backup_object_set.committed_object_inventory(receipt)
    except Exception as exc:
        raise FederationNodeError("FEDERATION_SOURCE_RECEIPT_INVALID") from exc
    return receipt, federation_replica_receiver.federation_object_set_digest(digest)


class FederationNodeState:
    """Small immutable-effect store and monotonic sequence allocator."""

    def __init__(self, db_path: Path, identity: dict[str, Any]) -> None:
        self._db_path = Path(db_path)
        self._identity = copy.deepcopy(identity)
        self._ensure_schema()
        self._bind_identity()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(_CREATE_IDENTITY_SQL)
            connection.execute(_CREATE_SEQUENCES_SQL)
            connection.execute(_CREATE_EFFECTS_SQL)

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
        identity_json = _canonical_json(self._identity)
        identity_digest = _digest(self._identity)
        now = _utc_iso(datetime.now(tz=timezone.utc))
        with self._write() as connection:
            row = connection.execute("SELECT * FROM federation_node_identity WHERE singleton = 1").fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO federation_node_identity (
                        singleton, fleet_id, root_fingerprint,
                        identity_digest, identity_json, bound_at
                    ) VALUES (1, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._identity["fleetId"],
                        self._identity["rootFingerprint"],
                        identity_digest,
                        identity_json,
                        now,
                    ),
                )
                return
            if (
                row["fleet_id"] != self._identity["fleetId"]
                or row["root_fingerprint"] != self._identity["rootFingerprint"]
                or row["identity_digest"] != identity_digest
                or row["identity_json"] != identity_json
            ):
                raise FederationNodeError("FEDERATION_NODE_STATE_IDENTITY_CONFLICT")

    def next_sequence(self, namespace: str, *, now: datetime) -> int:
        if not namespace or len(namespace) > 255:
            raise FederationNodeError("FEDERATION_NODE_SEQUENCE_NAMESPACE_INVALID")
        timestamp = _utc_iso(now)
        with self._write() as connection:
            row = connection.execute(
                "SELECT sequence FROM federation_node_sequences WHERE namespace = ?",
                (namespace,),
            ).fetchone()
            sequence = 1 if row is None else int(row["sequence"]) + 1
            if row is None:
                connection.execute(
                    "INSERT INTO federation_node_sequences (namespace, sequence, updated_at) VALUES (?, ?, ?)",
                    (namespace, sequence, timestamp),
                )
            else:
                connection.execute(
                    "UPDATE federation_node_sequences SET sequence = ?, updated_at = ? WHERE namespace = ?",
                    (sequence, timestamp, namespace),
                )
            return sequence

    def get_effect(self, effect_key: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT effect_digest, payload_json FROM federation_node_effects WHERE effect_key = ?",
                (effect_key,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        if type(payload) is not dict or _digest(payload) != row["effect_digest"]:
            raise FederationNodeError("FEDERATION_NODE_EFFECT_COMMITMENT_INVALID")
        return payload

    def put_effect(self, effect_key: str, payload: dict[str, Any], *, now: datetime) -> dict[str, Any]:
        if not effect_key or len(effect_key) > 512:
            raise FederationNodeError("FEDERATION_NODE_EFFECT_KEY_INVALID")
        normalized = _document(payload, code="FEDERATION_NODE_EFFECT_INVALID")
        federation_identity.assert_federation_document_secret_free(normalized)
        payload_json = _canonical_json(normalized)
        effect_digest = _digest(normalized)
        with self._write() as connection:
            row = connection.execute(
                "SELECT effect_digest, payload_json FROM federation_node_effects WHERE effect_key = ?",
                (effect_key,),
            ).fetchone()
            if row is not None:
                if row["effect_digest"] != effect_digest or row["payload_json"] != payload_json:
                    raise FederationNodeError("FEDERATION_NODE_EFFECT_IDENTITY_CONFLICT")
                return copy.deepcopy(normalized)
            connection.execute(
                """
                INSERT INTO federation_node_effects (
                    effect_key, effect_digest, payload_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (effect_key, effect_digest, payload_json, _utc_iso(now)),
            )
        return copy.deepcopy(normalized)


class FederationNode:
    """Sovereign Fleet endpoint; every remote mutation remains receiver-owned."""

    def __init__(
        self,
        *,
        identity: dict[str, Any],
        signer: federation_identity.OnlineFleetSigner,
        peer_registry: federation_peer_trust.PeerTrustRegistry,
        transfer_journal: federation_transfer_journal.FederatedTransferJournal,
        receiver: federation_replica_receiver.FederatedReplicaReceiver,
        durability_ledger: federated_durability.FederatedDurabilityLedger,
        custody_registry: federation_custody_capability.FederationCustodyCapabilityRegistry,
        state_db_path: Path,
        remote_target_id: str,
        failure_domain_metadata: dict[str, str],
        readiness: dict[str, Any],
        max_ingress_bytes: int,
        owner_instance_id: str,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=timezone.utc),
    ) -> None:
        normalized_identity = federation_identity.validate_fleet_identity(identity)
        if (
            peer_registry.local_identity != normalized_identity
            or transfer_journal.local_identity != normalized_identity
            or receiver.transfer_journal.local_identity != normalized_identity
            or durability_ledger.local_identity != normalized_identity
            or signer.fleet_id != normalized_identity["fleetId"]
        ):
            raise FederationNodeError("FEDERATION_NODE_LOCAL_IDENTITY_CONFLICT")
        certificate = signer.certificate
        if (
            certificate.get("rootFingerprint") != normalized_identity["rootFingerprint"]
            or certificate.get("rootKeyId") != normalized_identity["rootKeyId"]
        ):
            raise FederationNodeError("FEDERATION_NODE_SIGNER_IDENTITY_CONFLICT")
        self._identity = normalized_identity
        self._signer = signer
        self._peer_registry = peer_registry
        self._transfer_journal = transfer_journal
        self._receiver = receiver
        self._durability_ledger = durability_ledger
        self._custody_registry = custody_registry
        self._state = FederationNodeState(state_db_path, normalized_identity)
        self._remote_target_id = _control_id(
            remote_target_id,
            code="FEDERATION_NODE_REMOTE_TARGET_INVALID",
        )
        self._failure_domain_metadata = federated_replica_attestation._metadata(failure_domain_metadata)
        self._readiness = copy.deepcopy(readiness)
        self._max_ingress_bytes = _positive_int(
            max_ingress_bytes,
            code="FEDERATION_NODE_MAX_INGRESS_BYTES_INVALID",
        )
        self._owner_instance_id = _control_id(
            owner_instance_id,
            code="FEDERATION_NODE_OWNER_INSTANCE_INVALID",
        )
        self._clock = clock

    @property
    def identity(self) -> dict[str, Any]:
        return copy.deepcopy(self._identity)

    @property
    def durability_ledger(self) -> federated_durability.FederatedDurabilityLedger:
        return self._durability_ledger

    @property
    def state(self) -> FederationNodeState:
        return self._state

    def _now(self) -> datetime:
        current = self._clock()
        _utc_iso(current)
        return current.astimezone(timezone.utc)

    def health(self) -> dict[str, Any]:
        return {
            "schema": NODE_HEALTH_SCHEMA,
            "fleetId": self._identity["fleetId"],
            "rootFingerprint": self._identity["rootFingerprint"],
            "signerKeyId": self._signer.signer_key_id,
            "pid": os.getpid(),
            "remoteTargetId": self._remote_target_id,
            "ready": True,
        }

    def issue_readiness(self) -> dict[str, Any]:
        now = self._now()
        required = {
            "wireCompatibility",
            "availableFailureDomains",
            "forecastHeadroom",
            "costClass",
            "readiness",
        }
        if set(self._readiness) != required:
            raise FederationNodeError("FEDERATION_NODE_READINESS_CONFIG_INVALID")
        snapshot = resilience_federation_readiness.build_federation_snapshot(
            fleet_id=str(self._identity["fleetId"]),
            wire_compatibility=list(self._readiness["wireCompatibility"]),
            available_failure_domains=list(self._readiness["availableFailureDomains"]),
            forecast_headroom=self._readiness["forecastHeadroom"],
            cost_class=str(self._readiness["costClass"]),
            readiness=str(self._readiness["readiness"]),
            now=now,
        )
        sequence = self._state.next_sequence("readiness", now=now)
        return federation_readiness_attestation.issue_readiness_attestation(
            self._signer,
            snapshot,
            sequence=sequence,
            signed_at=now,
            expires_at=now + timedelta(seconds=120),
        )

    def issue_challenge(self, value: dict[str, Any]) -> dict[str, Any]:
        payload = _document(value, code="FEDERATION_NODE_CHALLENGE_REQUEST_INVALID")
        if set(payload) not in (
            {"destinationFleetId"},
            {"destinationFleetId", "sessionPurpose"},
        ):
            raise FederationNodeError("FEDERATION_NODE_CHALLENGE_REQUEST_INVALID")
        purpose = str(payload.get("sessionPurpose") or federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY)
        try:
            return federation_challenge.issue_federation_challenge(
                peer_registry=self._peer_registry,
                challenger_signer=self._signer,
                destination_fleet_id=str(payload["destinationFleetId"]),
                session_purpose=purpose,
                now=self._now(),
            )
        except Exception as exc:
            self._raise_domain(exc)

    def respond_challenge(self, value: dict[str, Any]) -> dict[str, Any]:
        payload = _exact_payload(value, {"challenge"}, code="FEDERATION_NODE_CHALLENGE_REQUEST_INVALID")
        try:
            return federation_challenge.respond_to_federation_challenge(
                _document(payload["challenge"], code="FEDERATION_CHALLENGE_INVALID"),
                peer_registry=self._peer_registry,
                responder_signer=self._signer,
                now=self._now(),
            )
        except Exception as exc:
            self._raise_domain(exc)

    def verify_challenge(self, value: dict[str, Any]) -> dict[str, Any]:
        payload = _exact_payload(
            value,
            {"challenge", "response"},
            code="FEDERATION_NODE_CHALLENGE_VERIFY_INVALID",
        )
        try:
            return federation_challenge.verify_federation_challenge_response(
                _document(payload["challenge"], code="FEDERATION_CHALLENGE_INVALID"),
                _document(payload["response"], code="FEDERATION_CHALLENGE_RESPONSE_INVALID"),
                peer_registry=self._peer_registry,
                now=self._now(),
            )
        except Exception as exc:
            self._raise_domain(exc)

    def verify_readiness(self, value: dict[str, Any]) -> dict[str, Any]:
        payload = _exact_payload(
            value,
            {"expectedPeerFleetId", "attestation"},
            code="FEDERATION_NODE_READINESS_VERIFY_INVALID",
        )
        try:
            return federation_readiness_attestation.verify_and_record_readiness_attestation(
                _document(payload["attestation"], code="FEDERATION_READINESS_ATTESTATION_INVALID"),
                peer_registry=self._peer_registry,
                expected_peer_fleet_id=str(payload["expectedPeerFleetId"]),
                now=self._now(),
            )
        except Exception as exc:
            self._raise_domain(exc)

    def propose_transfer(self, value: dict[str, Any]) -> dict[str, Any]:
        payload = _exact_payload(
            value,
            {"destinationFleetId", "sourceReceipt"},
            code="FEDERATION_NODE_TRANSFER_PROPOSAL_INVALID",
        )
        destination = str(payload["destinationFleetId"])
        try:
            self._peer_registry.require_active_peer(destination)
        except Exception as exc:
            self._raise_domain(exc)
        receipt, object_set_digest = _source_receipt(
            payload["sourceReceipt"],
            expected_source_fleet_id=str(self._identity["fleetId"]),
        )
        now = self._now()
        try:
            transfer = federation_transfer.propose_transfer(
                journal=self._transfer_journal,
                source_fleet_id=str(self._identity["fleetId"]),
                destination_fleet_id=destination,
                policy_id=str(receipt["policyId"]),
                backup_id=str(receipt["backupId"]),
                object_set_digest=object_set_digest,
                now=now,
            )
        except Exception as exc:
            self._raise_domain(exc)
        self._state.put_effect(f"source-receipt:{transfer['transferId']}", receipt, now=now)
        transfer = self._advance(
            transfer,
            federation_transfer_journal.STATE_GRANT_REQUESTED,
            {"requestKind": federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY, "transferId": transfer["transferId"]},
            now=now,
        )
        return {
            "transfer": transfer,
            "sourceReceiptDigest": _digest(receipt),
            "objectSetDigest": object_set_digest,
        }

    def verify_ingress_grant(self, value: dict[str, Any]) -> dict[str, Any]:
        payload = _exact_payload(value, {"grant"}, code="FEDERATION_NODE_GRANT_VERIFY_INVALID")
        grant = _document(payload["grant"], code="FEDERATION_INGRESS_GRANT_DOCUMENT_INVALID")
        transfer_id = _transfer_id(grant.get("transferId"))
        transfer = self._require_transfer(transfer_id, role=federation_transfer_journal.ROLE_SENDER)
        try:
            verified = federation_ingress_grant.verify_ingress_grant(
                grant,
                peer_registry=self._peer_registry,
                expected_source_fleet_id=str(transfer["sourceFleetId"]),
                expected_destination_fleet_id=str(transfer["destinationFleetId"]),
                expected_transfer_id=transfer_id,
                expected_policy_id=str(transfer["policyId"]),
                expected_backup_id=str(transfer["backupId"]),
                expected_object_set_digest=str(transfer["objectSetDigest"]),
                now=self._now(),
            )
        except Exception as exc:
            self._raise_domain(exc)
        now = self._now()
        self._state.put_effect(f"sender-grant:{transfer_id}", verified, now=now)
        self._advance(
            transfer,
            federation_transfer_journal.STATE_GRANT_VERIFIED,
            {"grantIds": [verified["grantId"]], "objectSetDigest": transfer["objectSetDigest"]},
            now=now,
        )
        return verified

    def mark_remote_verifying(self, transfer_id: str, value: dict[str, Any]) -> dict[str, Any]:
        transfer = self._require_transfer(transfer_id, role=federation_transfer_journal.ROLE_SENDER)
        payload = _exact_payload(
            value,
            {"grantId", "remoteTargetId"},
            code="FEDERATION_NODE_REMOTE_VERIFYING_INVALID",
        )
        grant = self._state.get_effect(f"sender-grant:{transfer_id}")
        if grant is None or grant.get("grantId") != payload["grantId"]:
            raise FederationNodeError("FEDERATION_INGRESS_GRANT_NOT_FOUND")
        receipt = self._state.get_effect(f"source-receipt:{transfer_id}")
        if receipt is None:
            raise FederationNodeError("FEDERATION_SOURCE_RECEIPT_NOT_FOUND")
        objects = backup_object_set.committed_object_inventory(receipt)
        now = self._now()
        transfer = self._advance(
            transfer,
            federation_transfer_journal.STATE_TRANSFERRING,
            {
                "componentCount": len(objects),
                "ciphertextBytes": sum(int(item["size"]) for item in objects),
            },
            now=now,
        )
        return self._advance(
            transfer,
            federation_transfer_journal.STATE_REMOTE_VERIFYING,
            {
                "targetId": _control_id(
                    payload["remoteTargetId"],
                    code="FEDERATION_REPLICA_ATTESTATION_REMOTE_TARGET_INVALID",
                ),
                "storageProtocol": backup_object_set.OBJECT_SET_V1,
            },
            now=now,
        )

    def issue_ingress_grant(self, value: dict[str, Any]) -> dict[str, Any]:
        payload = _exact_payload(
            value,
            {
                "sourceFleetId",
                "sessionNonce",
                "transferId",
                "policyId",
                "backupId",
                "objectSetDigest",
                "totalBytes",
            },
            code="FEDERATION_NODE_GRANT_REQUEST_INVALID",
        )
        total_bytes = _positive_int(payload["totalBytes"], code="FEDERATION_INGRESS_MAX_BYTES_INVALID")
        if total_bytes > self._max_ingress_bytes:
            raise FederationNodeError("FEDERATION_INGRESS_MAX_BYTES_EXCEEDED")
        now = self._now()
        expected_prefix = f"federation/{payload['sourceFleetId']}/{payload['transferId']}/"
        try:
            self._peer_registry.require_active_peer(str(payload["sourceFleetId"]))
            federation_transfer.accept_or_resume_transfer(
                journal=self._transfer_journal,
                transfer_id=str(payload["transferId"]),
                source_fleet_id=str(payload["sourceFleetId"]),
                destination_fleet_id=str(self._identity["fleetId"]),
                policy_id=str(payload["policyId"]),
                backup_id=str(payload["backupId"]),
                object_set_digest=str(payload["objectSetDigest"]),
                now=now,
            )
            session_digest = federation_challenge.nonce_digest(str(payload["sessionNonce"]))
            existing = self._peer_registry.get_ingress_grant_by_session_nonce(session_digest)
            if existing is not None:
                expected = {
                    "sourceFleetId": str(payload["sourceFleetId"]),
                    "destinationFleetId": str(self._identity["fleetId"]),
                    "transferId": str(payload["transferId"]),
                    "policyId": str(payload["policyId"]),
                    "backupId": str(payload["backupId"]),
                    "objectSetDigest": str(payload["objectSetDigest"]),
                    "allowedObjectPrefix": expected_prefix,
                    "maxBytes": total_bytes,
                }
                grant = existing.get("grant")
                if type(grant) is not dict or any(existing.get(field) != item for field, item in expected.items()):
                    raise FederationNodeError("FEDERATION_INGRESS_SESSION_REPLAY")
                if any(grant.get(field) != item for field, item in expected.items()):
                    raise FederationNodeError("FEDERATION_INGRESS_SESSION_REPLAY")
                return grant
            return federation_ingress_grant.issue_ingress_grant(
                peer_registry=self._peer_registry,
                receiver_signer=self._signer,
                source_fleet_id=str(payload["sourceFleetId"]),
                session_nonce=str(payload["sessionNonce"]),
                transfer_id=str(payload["transferId"]),
                policy_id=str(payload["policyId"]),
                backup_id=str(payload["backupId"]),
                object_set_digest=str(payload["objectSetDigest"]),
                allowed_object_prefix=expected_prefix,
                max_bytes=total_bytes,
                now=now,
            )
        except Exception as exc:
            self._raise_domain(exc)

    def declare_replica(self, transfer_id: str, value: dict[str, Any]) -> dict[str, Any]:
        payload = _exact_payload(
            value,
            {"grantId", "sourceReceipt"},
            code="FEDERATION_NODE_REPLICA_DECLARATION_INVALID",
        )
        grant = self._grant_for_transfer(transfer_id, str(payload["grantId"]), require_active=True)
        try:
            return self._receiver.declare_object_set(
                grant=grant,
                transfer_id=transfer_id,
                source_receipt=_document(payload["sourceReceipt"], code="FEDERATION_SOURCE_RECEIPT_INVALID"),
                now=self._now(),
            )
        except Exception as exc:
            self._raise_domain(exc)

    def expected_component_size(self, transfer_id: str, component_digest: str, grant_id: str) -> int:
        self._grant_for_transfer(transfer_id, grant_id, require_active=True)
        if type(component_digest) is not str or _PLAIN_DIGEST.fullmatch(component_digest) is None:
            raise FederationNodeError("FEDERATION_REPLICA_COMPONENT_DIGEST_INVALID")
        components = self._receiver.list_components(transfer_id)
        matched = [item for item in components if item["ciphertextDigest"] == component_digest]
        if len(matched) != 1:
            raise FederationNodeError("FEDERATION_REPLICA_COMPONENT_NOT_DECLARED")
        return int(matched[0]["ciphertextSize"])

    def receive_component(
        self,
        transfer_id: str,
        component_digest: str,
        *,
        grant_id: str,
        write_id: str,
        content: BinaryIO,
    ) -> dict[str, Any]:
        grant = self._grant_for_transfer(transfer_id, grant_id, require_active=True)
        try:
            return self._receiver.receive_component(
                grant=grant,
                transfer_id=transfer_id,
                component_digest=component_digest,
                write_id=write_id,
                content=content,
                now=self._now(),
            )
        except Exception as exc:
            self._raise_domain(exc)

    def reconcile_transfer(self, transfer_id: str, grant_id: str) -> dict[str, Any]:
        grant = self._grant_for_transfer(transfer_id, grant_id, require_active=False)
        try:
            result = federation_transfer.reconcile_transfer(
                journal=self._transfer_journal,
                transfer_id=transfer_id,
                source_fleet_id=str(grant["sourceFleetId"]),
                destination_fleet_id=str(grant["destinationFleetId"]),
                policy_id=str(grant["policyId"]),
                backup_id=str(grant["backupId"]),
                object_set_digest=str(grant["objectSetDigest"]),
                now=self._now(),
            )
        except Exception as exc:
            self._raise_domain(exc)
        committed = self._state.get_effect(f"replica-commit:{transfer_id}")
        return {**result, "committedEffect": committed}

    def commit_replica(self, transfer_id: str, value: dict[str, Any]) -> dict[str, Any]:
        payload = _exact_payload(value, {"grantId"}, code="FEDERATION_NODE_COMMIT_REQUEST_INVALID")
        self._grant_for_transfer(transfer_id, str(payload["grantId"]), require_active=True)
        effect_key = f"replica-commit:{transfer_id}"
        existing = self._state.get_effect(effect_key)
        if existing is not None:
            return existing
        now = self._now()
        try:
            committed = federated_replica_commit.commit_federated_replica(
                receiver=self._receiver,
                transfer_id=transfer_id,
                target_id=self._remote_target_id,
                owner_instance_id=self._owner_instance_id,
                now=now,
            )
            sequence = self._state.next_sequence(f"replica:{transfer_id}", now=now)
            attestation = federated_replica_attestation.issue_replica_attestation(
                signer=self._signer,
                receiver=self._receiver,
                transfer_id=transfer_id,
                remote_target_id=self._remote_target_id,
                failure_domain_metadata=self._failure_domain_metadata,
                sequence=sequence,
                signed_at=now,
                expires_at=now + timedelta(seconds=300),
            )
        except Exception as exc:
            self._raise_domain(exc)
        receipt_bytes = _storage_bytes(committed.receipt)
        commit_bytes = _storage_bytes(committed.commit)
        effect = {
            "schema": NODE_EFFECT_SCHEMA,
            "effectType": "FEDERATED_REPLICA_COMMIT",
            "transferId": transfer_id,
            "targetId": committed.target_id,
            "receipt": committed.receipt,
            "commit": committed.commit,
            "remoteReceiptBase64": base64.b64encode(receipt_bytes).decode("ascii"),
            "remoteCommitBase64": base64.b64encode(commit_bytes).decode("ascii"),
            "attestation": attestation,
            "fencingToken": committed.fencing_token,
            "converged": committed.converged,
            "reconciled": committed.reconciled,
        }
        return self._state.put_effect(effect_key, effect, now=now)

    def verify_replica_attestation(self, transfer_id: str, value: dict[str, Any]) -> dict[str, Any]:
        payload = _exact_payload(
            value,
            {"attestation", "remoteReceiptBase64", "remoteCommitBase64"},
            code="FEDERATION_NODE_REPLICA_VERIFY_INVALID",
        )
        self._require_transfer(transfer_id, role=federation_transfer_journal.ROLE_SENDER)
        receipt = self._state.get_effect(f"source-receipt:{transfer_id}")
        if receipt is None:
            raise FederationNodeError("FEDERATION_SOURCE_RECEIPT_NOT_FOUND")
        try:
            accepted = federated_replica_attestation.verify_and_record_replica_attestation(
                _document(payload["attestation"], code="FEDERATION_REPLICA_ATTESTATION_INVALID"),
                peer_registry=self._peer_registry,
                sender_journal=self._transfer_journal,
                source_receipt=receipt,
                remote_receipt_bytes=_decode_document(
                    payload["remoteReceiptBase64"],
                    code="FEDERATION_REPLICA_REMOTE_RECEIPT_ENCODING_INVALID",
                ),
                remote_commit_bytes=_decode_document(
                    payload["remoteCommitBase64"],
                    code="FEDERATION_REPLICA_REMOTE_COMMIT_ENCODING_INVALID",
                ),
                now=self._now(),
            )
            copy_record = federated_durability.record_verified_federated_copy(
                ledger=self._durability_ledger,
                peer_registry=self._peer_registry,
                sender_journal=self._transfer_journal,
                transfer_id=transfer_id,
                now=self._now(),
            )
        except Exception as exc:
            self._raise_domain(exc)
        completed = self._require_transfer(transfer_id, role=federation_transfer_journal.ROLE_SENDER)
        return {"attestation": accepted, "federatedCopy": copy_record, "transfer": completed}

    def run_dr_drill(self, transfer_id: str, value: dict[str, Any]) -> dict[str, Any]:
        payload = _exact_payload(value, {"requestId"}, code="FEDERATION_NODE_DR_REQUEST_INVALID")
        request_id = str(payload["requestId"])
        if _REQUEST_ID.fullmatch(request_id) is None:
            raise FederationNodeError("FEDERATION_NODE_DR_REQUEST_INVALID")
        effect_key = f"dr-drill:{request_id}"
        existing = self._state.get_effect(effect_key)
        if existing is not None:
            if existing.get("transferId") != transfer_id:
                raise FederationNodeError("FEDERATION_NODE_EFFECT_IDENTITY_CONFLICT")
            return existing
        replica = self._state.get_effect(f"replica-commit:{transfer_id}")
        if replica is None:
            raise FederationNodeError("FEDERATION_REPLICA_REMOTE_COMMIT_MISSING")
        now = self._now()
        try:
            sequence = self._state.next_sequence(f"dr:{transfer_id}", now=now)
            production_result: dict[str, Any] = {}
            attestation = federated_dr_drill.run_federated_dr_drill(
                signer=self._signer,
                receiver=self._receiver,
                peer_registry=self._peer_registry,
                custody_registry=self._custody_registry,
                replica_attestation=_document(
                    replica["attestation"],
                    code="FEDERATION_REPLICA_ATTESTATION_INVALID",
                ),
                transfer_id=transfer_id,
                remote_target_id=self._remote_target_id,
                sequence=sequence,
                signed_at=None,
                expires_at=None,
                evidence_sink=production_result,
            )
        except Exception as exc:
            self._raise_domain(exc)
        effect = {
            "schema": NODE_EFFECT_SCHEMA,
            "effectType": "FEDERATED_DR_DRILL",
            "requestId": request_id,
            "transferId": transfer_id,
            "attestation": attestation,
            "productionRestoreResult": production_result,
        }
        return self._state.put_effect(effect_key, effect, now=self._now())

    def verify_dr_attestation(self, transfer_id: str, value: dict[str, Any]) -> dict[str, Any]:
        payload = _exact_payload(value, {"attestation"}, code="FEDERATION_NODE_DR_VERIFY_INVALID")
        attestation = _document(payload["attestation"], code="FEDERATED_DR_ATTESTATION_INVALID")
        if attestation.get("transferId") != transfer_id:
            raise FederationNodeError("FEDERATED_DR_TRANSFER_ID_MISMATCH")
        try:
            accepted = federated_dr_drill.verify_and_record_dr_drill_attestation(
                attestation,
                peer_registry=self._peer_registry,
                sender_journal=self._transfer_journal,
                now=self._now(),
            )
        except Exception as exc:
            self._raise_domain(exc)
        return {"attestation": accepted, "transfer": self._require_transfer(transfer_id)}

    def _require_transfer(self, transfer_id: str, *, role: str | None = None) -> dict[str, Any]:
        normalized = _transfer_id(transfer_id)
        try:
            transfer = self._transfer_journal.get_transfer(normalized)
        except Exception as exc:
            self._raise_domain(exc)
        if transfer is None:
            raise FederationNodeError("FEDERATION_TRANSFER_NOT_FOUND")
        if role is not None and transfer.get("role") != role:
            raise FederationNodeError("FEDERATION_TRANSFER_ROLE_MISMATCH")
        return transfer

    def _grant_for_transfer(self, transfer_id: str, grant_id: str, *, require_active: bool) -> dict[str, Any]:
        transfer = self._require_transfer(transfer_id, role=federation_transfer_journal.ROLE_RECEIVER)
        try:
            record = self._peer_registry.get_ingress_grant(grant_id)
            if require_active:
                self._peer_registry.require_active_peer(str(transfer["sourceFleetId"]))
        except Exception as exc:
            self._raise_domain(exc)
        if record is None:
            raise FederationNodeError("FEDERATION_INGRESS_GRANT_NOT_FOUND")
        fields = (
            "transferId",
            "sourceFleetId",
            "destinationFleetId",
            "policyId",
            "backupId",
            "objectSetDigest",
        )
        if any(record.get(field) != transfer.get(field) for field in fields):
            raise FederationNodeError("FEDERATION_INGRESS_GRANT_BINDING_MISMATCH")
        grant = record.get("grant")
        if type(grant) is not dict or any(grant.get(field) != transfer.get(field) for field in fields):
            raise FederationNodeError("FEDERATION_INGRESS_GRANT_IDENTITY_CONFLICT")
        return grant

    def _advance(
        self,
        transfer: dict[str, Any],
        next_state: str,
        details: dict[str, Any],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        current_state = str(transfer["state"])
        current_index = federation_transfer_journal.TRANSFER_STATES.index(current_state)
        next_index = federation_transfer_journal.TRANSFER_STATES.index(next_state)
        if current_index > next_index:
            return transfer
        try:
            return self._transfer_journal.advance_transfer(
                str(transfer["transferId"]),
                expected_revision=int(transfer["revision"]),
                next_state=next_state,
                details=details,
                now=now,
            )
        except Exception as exc:
            self._raise_domain(exc)

    @staticmethod
    def _raise_domain(exc: Exception) -> NoReturn:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code:
            raise FederationNodeError(code) from exc
        raise exc


def _config_path(base: Path, value: Any, *, field: str) -> Path:
    if type(value) is not str or not value.strip():
        raise FederationNodeError("FEDERATION_NODE_CONFIG_PATH_INVALID", field)
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _read_node_config(config_path: Path) -> dict[str, Any]:
    path = Path(config_path).resolve()
    try:
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise FederationNodeError("FEDERATION_NODE_CONFIG_INVALID")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FederationNodeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FederationNodeError("FEDERATION_NODE_CONFIG_INVALID") from exc
    if type(value) is not dict or set(value) != _NODE_CONFIG_FIELDS:
        raise FederationNodeError("FEDERATION_NODE_CONFIG_FIELDS_INVALID")
    if value.get("schema") != NODE_CONFIG_SCHEMA:
        raise FederationNodeError("FEDERATION_NODE_CONFIG_SCHEMA_INVALID")
    return value


def _configure_custody(
    config: dict[str, Any],
    *,
    registry: federation_peer_trust.PeerTrustRegistry,
    custody_registry: federation_custody_capability.FederationCustodyCapabilityRegistry,
    recovery_age_identity: bytes | bytearray | None,
    now: datetime,
) -> None:
    custody = _document(config.get("custody"), code="FEDERATION_NODE_CUSTODY_CONFIG_INVALID")
    common = {"peerFleetId", "mode", "actor"}
    mode = str(custody.get("mode") or "")
    if mode == federation_custody_capability.COLD_CUSTODY:
        if set(custody) != common or recovery_age_identity is not None:
            raise FederationNodeError("FEDERATION_NODE_CUSTODY_CONFIG_INVALID")
        kwargs: dict[str, Any] = {}
    elif mode == federation_custody_capability.RECOVERY_CAPABLE:
        if set(custody) != common | {"ageRecipient"} or not recovery_age_identity:
            raise FederationNodeError("FEDERATION_RECOVERY_IDENTITY_BINDING_REQUIRED")
        recipient = str(custody.get("ageRecipient") or "")
        provider_name = "federation-node-" + hashlib.sha256(
            f"{config['fleetId']}:{custody['peerFleetId']}:{recipient}".encode("utf-8")
        ).hexdigest()[:20]
        credential_ref = "preprovisioned-age-identity"
        provider = backup_recovery_credential.InMemoryCredentialProvider()
        secret_copy = bytearray(recovery_age_identity)
        try:
            provider.set_credential(credential_ref, secret_copy)
        finally:
            backup_recovery_credential.zeroize(secret_copy)
        backup_recovery_credential.register_provider(provider_name, provider)
        kwargs = {
            "credential_provider": provider_name,
            "credential_ref": credential_ref,
            "age_recipient": recipient,
        }
    else:
        raise FederationNodeError("FEDERATION_NODE_CUSTODY_CONFIG_INVALID")
    try:
        custody_registry.configure_peer(
            registry,
            str(custody["peerFleetId"]),
            mode=mode,
            actor=str(custody["actor"]),
            now=now,
            **kwargs,
        )
    except Exception as exc:
        FederationNode._raise_domain(exc)


def load_federation_node(
    config_path: Path,
    *,
    signer_passphrase: bytes | bytearray,
    recovery_age_identity: bytes | bytearray | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(tz=timezone.utc),
) -> FederationNode:
    """Load one process-local node without exposing the offline Federation root."""

    config_path = Path(config_path).resolve()
    node_config = _read_node_config(config_path)
    base = config_path.parent
    path_fields = (
        "publicIdentityPath",
        "signerBundlePath",
        "peerRegistryPath",
        "transferJournalPath",
        "receiverDbPath",
        "stagingDir",
        "durabilityDbPath",
        "custodyDbPath",
        "nodeStateDbPath",
    )
    paths = {field: _config_path(base, node_config[field], field=field) for field in path_fields}
    mutable = [paths[field] for field in path_fields if field not in {"publicIdentityPath", "signerBundlePath"}]
    if len(set(mutable)) != len(mutable):
        raise FederationNodeError("FEDERATION_NODE_CONFIG_PATH_CONFLICT")
    try:
        identity = federation_identity.read_public_fleet_identity(paths["publicIdentityPath"])
    except federation_identity.FederationIdentityError as exc:
        raise FederationNodeError(exc.code) from exc
    if identity.get("fleetId") != node_config.get("fleetId"):
        raise FederationNodeError("FEDERATION_NODE_CONFIG_FLEET_MISMATCH")
    now = clock()
    _utc_iso(now)
    try:
        signer = federation_identity.load_online_signer(
            paths["signerBundlePath"],
            signer_passphrase,
            root_identity=identity,
            now=now,
        )
        target = backup_targets.get_target(str(node_config["remoteTargetId"]))
    except Exception as exc:
        FederationNode._raise_domain(exc)
    if target.get("kind") != "s3" or target.get("targetId") != node_config["remoteTargetId"]:
        raise FederationNodeError("FEDERATION_REPLICA_PROVIDER_TARGET_REQUIRED")
    peer_registry = federation_peer_trust.PeerTrustRegistry(paths["peerRegistryPath"], identity)
    transfer_journal = federation_transfer_journal.FederatedTransferJournal(paths["transferJournalPath"], identity)
    receiver = federation_replica_receiver.FederatedReplicaReceiver(
        transfer_journal=transfer_journal,
        peer_registry=peer_registry,
        db_path=paths["receiverDbPath"],
        staging_dir=paths["stagingDir"],
    )
    durability = federated_durability.FederatedDurabilityLedger(paths["durabilityDbPath"], identity)
    custody = federation_custody_capability.FederationCustodyCapabilityRegistry(paths["custodyDbPath"], identity)
    _configure_custody(
        node_config,
        registry=peer_registry,
        custody_registry=custody,
        recovery_age_identity=recovery_age_identity,
        now=now,
    )
    return FederationNode(
        identity=identity,
        signer=signer,
        peer_registry=peer_registry,
        transfer_journal=transfer_journal,
        receiver=receiver,
        durability_ledger=durability,
        custody_registry=custody,
        state_db_path=paths["nodeStateDbPath"],
        remote_target_id=str(node_config["remoteTargetId"]),
        failure_domain_metadata=_document(
            node_config["failureDomainMetadata"],
            code="FEDERATION_NODE_FAILURE_DOMAIN_INVALID",
        ),
        readiness=_document(node_config["readiness"], code="FEDERATION_NODE_READINESS_CONFIG_INVALID"),
        max_ingress_bytes=_positive_int(
            node_config["maxIngressBytes"],
            code="FEDERATION_NODE_MAX_INGRESS_BYTES_INVALID",
        ),
        owner_instance_id=str(node_config["ownerInstanceId"]),
        clock=clock,
    )

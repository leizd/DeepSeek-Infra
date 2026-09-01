"""Production storage commit orchestration for a received federated replica."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_object_set,
    backup_publish,
    backup_replication,
    backup_scheduler,
    backup_target_store,
    backup_transfer_budget,
    backup_writer_lease,
    federation_replica_receiver,
    federation_transfer_journal,
)


RECEIPT_V4_FIELDS = frozenset(
    {
        "schemaVersion",
        "snapshotKind",
        "lineageId",
        "parentBackupId",
        "baseBackupId",
        "chainDepth",
        "chunkProtocol",
        "backupId",
        "runId",
        "policyId",
        "targetId",
        "scheduleSlot",
        "size",
        "creationVerified",
        "createdAt",
        "pinned",
        "storageProtocol",
        "controlObjectDigest",
        "objectSetDigest",
        "objects",
    }
)

COMMIT_V4_FIELDS = frozenset(
    {
        "schemaVersion",
        "policyId",
        "scheduleSlot",
        "slotDigest",
        "runId",
        "fencingToken",
        "backupId",
        "storageProtocol",
        "objectSetDigest",
        "controlObjectDigest",
        "receiptDigest",
        "targetGeneration",
        "previousCommitHash",
        "commitHash",
    }
)

_INSTANCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class FederatedReplicaCommitError(RuntimeError):
    """Fail-closed production commit error with a stable code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = str(code)
        super().__init__(message or self.code)


@dataclass(frozen=True, slots=True)
class FederatedReplicaCommitResult:
    transfer_id: str
    target_id: str
    receipt: dict[str, Any]
    commit: dict[str, Any]
    fencing_token: int
    converged: bool
    reconciled: bool


def federated_run_id(transfer_id: str) -> str:
    if (
        not isinstance(transfer_id, str)
        or not transfer_id.startswith("sha256:")
        or len(transfer_id) != 71
        or any(character not in "0123456789abcdef" for character in transfer_id[len("sha256:") :])
    ):
        raise FederatedReplicaCommitError("FEDERATION_TRANSFER_ID_INVALID")
    return "fed-replica-" + transfer_id[len("sha256:") :]


def federated_schedule_slot(transfer_id: str) -> str:
    federated_run_id(transfer_id)
    return "federation/replica/" + transfer_id


def _document_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _plain_digest_valid(value: Any) -> bool:
    return type(value) is str and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _canonical_utc_timestamp_valid(value: Any) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (
        parsed.tzinfo is not None
        and parsed.utcoffset() is not None
        and parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") == value
    )


def _parse_json(raw: bytes, *, code: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FederatedReplicaCommitError(code) from exc
    if type(parsed) is not dict:
        raise FederatedReplicaCommitError(code)
    return parsed


def _validate_receipt(
    receipt: dict[str, Any],
    *,
    package: backup_object_set.ObjectSetPackage,
    transfer: dict[str, Any],
    target_id: str,
) -> None:
    if set(receipt) != RECEIPT_V4_FIELDS or type(receipt.get("schemaVersion")) is not int:
        raise FederatedReplicaCommitError("FEDERATION_REPLICA_RECEIPT_V4_INVALID")
    expected = backup_publish.receipt_for(
        package,
        run_id=federated_run_id(str(transfer["transferId"])),
        policy_id=str(transfer["policyId"]),
        target_id=target_id,
        schedule_slot=federated_schedule_slot(str(transfer["transferId"])),
    )
    expected["createdAt"] = receipt.get("createdAt")
    if not _canonical_utc_timestamp_valid(receipt.get("createdAt")) or receipt != expected:
        raise FederatedReplicaCommitError("FEDERATION_REPLICA_RECEIPT_BINDING_INVALID")


def _validate_commit(
    commit: dict[str, Any],
    *,
    receipt: dict[str, Any],
    receipt_bytes: bytes,
    package: backup_object_set.ObjectSetPackage,
    transfer: dict[str, Any],
) -> None:
    transfer_id = str(transfer["transferId"])
    schedule_slot = federated_schedule_slot(transfer_id)
    if set(commit) != COMMIT_V4_FIELDS or type(commit.get("schemaVersion")) is not int:
        raise FederatedReplicaCommitError("FEDERATION_REPLICA_COMMIT_V4_INVALID")
    if (
        commit.get("schemaVersion") != backup_publish.COMMIT_SCHEMA_VERSION
        or commit.get("policyId") != transfer["policyId"]
        or commit.get("scheduleSlot") != schedule_slot
        or commit.get("slotDigest") != backup_target_store.commit_slot_digest(schedule_slot)
        or commit.get("runId") != federated_run_id(transfer_id)
        or commit.get("backupId") != transfer["backupId"]
        or commit.get("storageProtocol") != backup_object_set.OBJECT_SET_V1
        or commit.get("objectSetDigest") != package.object_set_digest
        or commit.get("controlObjectDigest") != package.control.ciphertext_digest
        or commit.get("receiptDigest") != hashlib.sha256(receipt_bytes).hexdigest()
        or not isinstance(commit.get("fencingToken"), int)
        or isinstance(commit.get("fencingToken"), bool)
        or int(commit["fencingToken"]) < 1
        or not isinstance(commit.get("targetGeneration"), int)
        or isinstance(commit.get("targetGeneration"), bool)
        or int(commit["targetGeneration"]) < 1
        or not _plain_digest_valid(commit.get("slotDigest"))
        or not _plain_digest_valid(commit.get("receiptDigest"))
        or not _plain_digest_valid(commit.get("previousCommitHash"))
        or not _plain_digest_valid(commit.get("commitHash"))
        or not backup_publish.commit_marker_valid(commit)
    ):
        raise FederatedReplicaCommitError("FEDERATION_REPLICA_COMMIT_BINDING_INVALID")


def _verify_remote_components(
    store: backup_target_store.BackupTargetStore,
    package: backup_object_set.ObjectSetPackage,
    checkpoint: Callable[[], None],
) -> None:
    for component in package.components:
        checkpoint()
        key = backup_target_store.object_key(component.ciphertext_digest)
        metadata = store.stat(key)
        if metadata is None or metadata.size != component.ciphertext_size:
            raise FederatedReplicaCommitError("FEDERATION_REPLICA_REMOTE_COMPONENT_INVALID")
        observed_size = 0
        observed_digest = hashlib.sha256()
        for chunk in store.get_stream(key):
            checkpoint()
            observed_size += len(chunk)
            observed_digest.update(chunk)
        if observed_size != component.ciphertext_size or observed_digest.hexdigest() != component.ciphertext_digest:
            raise FederatedReplicaCommitError("FEDERATION_REPLICA_REMOTE_COMPONENT_INVALID")


def _read_existing_commit(
    target: backup_publish.ResolvedTarget,
    *,
    package: backup_object_set.ObjectSetPackage,
    transfer: dict[str, Any],
    checkpoint: Callable[[], None],
) -> backup_publish.PublishResult | None:
    store = target.require_store()
    schedule_slot = federated_schedule_slot(str(transfer["transferId"]))
    marker_raw: bytes | None = None
    for key in backup_target_store.commit_marker_keys(str(transfer["policyId"]), schedule_slot):
        checkpoint()
        marker_raw = store.get_bytes(key)
        if marker_raw is not None:
            break
    if marker_raw is None:
        return None
    commit = _parse_json(marker_raw, code="FEDERATION_REPLICA_REMOTE_COMMIT_INVALID")
    if marker_raw != _document_bytes(commit):
        raise FederatedReplicaCommitError("FEDERATION_REPLICA_REMOTE_COMMIT_ENCODING_INVALID")
    receipt_key = backup_target_store.receipt_key(str(transfer["backupId"]))
    checkpoint()
    raw_receipt = store.get_bytes(receipt_key)
    if raw_receipt is None:
        raise FederatedReplicaCommitError("FEDERATION_REPLICA_REMOTE_RECEIPT_MISSING")
    receipt = _parse_json(raw_receipt, code="FEDERATION_REPLICA_REMOTE_RECEIPT_INVALID")
    _validate_receipt(receipt, package=package, transfer=transfer, target_id=target.target_id)
    _validate_commit(
        commit,
        receipt=receipt,
        receipt_bytes=raw_receipt,
        package=package,
        transfer=transfer,
    )
    if raw_receipt != _document_bytes(receipt):
        raise FederatedReplicaCommitError("FEDERATION_REPLICA_REMOTE_RECEIPT_ENCODING_INVALID")
    _verify_remote_components(store, package, checkpoint)
    return backup_publish.PublishResult(
        receipt=receipt,
        path=None,
        receipt_path=None,
        commit=commit,
        converged=True,
        object_key=backup_target_store.object_key(package.control.ciphertext_digest),
        receipt_key=receipt_key,
    )


def _precommit_state_details(
    transfer: dict[str, Any],
    components: list[dict[str, Any]],
    *,
    target_id: str,
) -> dict[str, dict[str, Any]]:
    grant_ids = sorted({str(component["grantId"]) for component in components if component.get("grantId")})
    return {
        federation_transfer_journal.STATE_GRANT_REQUESTED: {
            "requestKind": "REMOTE_CUSTODY",
            "transferId": transfer["transferId"],
        },
        federation_transfer_journal.STATE_GRANT_VERIFIED: {
            "grantIds": grant_ids,
            "objectSetDigest": transfer["objectSetDigest"],
        },
        federation_transfer_journal.STATE_TRANSFERRING: {
            "componentCount": len(components),
            "ciphertextBytes": sum(int(component["ciphertextSize"]) for component in components),
        },
        federation_transfer_journal.STATE_REMOTE_VERIFYING: {
            "targetId": target_id,
            "storageProtocol": backup_object_set.OBJECT_SET_V1,
        },
    }


def _advance_to_remote_verifying(
    journal: federation_transfer_journal.FederatedTransferJournal,
    transfer: dict[str, Any],
    components: list[dict[str, Any]],
    *,
    target_id: str,
    now: datetime,
) -> dict[str, Any]:
    expected_details = _precommit_state_details(transfer, components, target_id=target_id)
    current = transfer
    current_state = str(current["state"])
    if current_state in expected_details and current.get("stateDetails") != expected_details[current_state]:
        raise FederatedReplicaCommitError("FEDERATION_REPLICA_TRANSFER_STATE_CONFLICT")
    target_index = federation_transfer_journal.TRANSFER_STATES.index(
        federation_transfer_journal.STATE_REMOTE_VERIFYING
    )
    while federation_transfer_journal.TRANSFER_STATES.index(str(current["state"])) < target_index:
        current_index = federation_transfer_journal.TRANSFER_STATES.index(str(current["state"]))
        next_state = federation_transfer_journal.TRANSFER_STATES[current_index + 1]
        details = expected_details[next_state]
        try:
            current = journal.advance_transfer(
                str(current["transferId"]),
                expected_revision=int(current["revision"]),
                next_state=next_state,
                details=details,
                now=now,
            )
        except federation_transfer_journal.FederatedTransferJournalError as exc:
            raise FederatedReplicaCommitError(exc.code) from exc
    return current


def _remote_commit_details(
    transfer: dict[str, Any],
    target_id: str,
    published: backup_publish.PublishResult,
) -> dict[str, Any]:
    return {
        "targetId": target_id,
        "objectSetDigest": transfer["objectSetDigest"],
        "remoteReceiptDigest": "sha256:" + hashlib.sha256(_document_bytes(published.receipt)).hexdigest(),
        "remoteCommitDigest": "sha256:" + hashlib.sha256(_document_bytes(published.commit)).hexdigest(),
    }


def _record_remote_committed(
    journal: federation_transfer_journal.FederatedTransferJournal,
    transfer: dict[str, Any],
    *,
    target_id: str,
    published: backup_publish.PublishResult,
    now: datetime,
) -> dict[str, Any]:
    expected = _remote_commit_details(transfer, target_id, published)
    current_state = str(transfer["state"])
    if current_state == federation_transfer_journal.STATE_REMOTE_VERIFYING:
        try:
            return journal.advance_transfer(
                str(transfer["transferId"]),
                expected_revision=int(transfer["revision"]),
                next_state=federation_transfer_journal.STATE_REMOTE_COMMITTED,
                details=expected,
                now=now,
            )
        except federation_transfer_journal.FederatedTransferJournalError as exc:
            raise FederatedReplicaCommitError(exc.code) from exc
    events = journal.list_transfer_events(str(transfer["transferId"]))
    committed_events = [
        event for event in events if event["nextState"] == federation_transfer_journal.STATE_REMOTE_COMMITTED
    ]
    if len(committed_events) != 1 or committed_events[0]["stateDetails"] != expected:
        raise FederatedReplicaCommitError("FEDERATION_REPLICA_REMOTE_COMMIT_STATE_CONFLICT")
    return transfer


class _RenewingWriterCheckpoint:
    def __init__(
        self,
        writer: backup_writer_lease.TargetWriterLease,
        *,
        lease_seconds: int,
        monotonic_clock: Callable[[], float],
    ) -> None:
        self._writer = writer
        self._clock = monotonic_clock
        self._interval = max(1.0, float(lease_seconds) / 3.0)
        self._next_renewal = self._read_clock() + self._interval
        self._lock = threading.Lock()

    def _read_clock(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise FederatedReplicaCommitError("FEDERATION_REPLICA_MONOTONIC_CLOCK_INVALID")
        return value

    def __call__(self) -> None:
        with self._lock:
            observed = self._read_clock()
            if observed >= self._next_renewal:
                self._writer.renew()
                self._next_renewal = observed + self._interval
            else:
                self._writer.assert_owned()


def commit_federated_replica(
    *,
    receiver: federation_replica_receiver.FederatedReplicaReceiver,
    transfer_id: str,
    target_id: str,
    owner_instance_id: str,
    now: datetime,
    lease_seconds: int = backup_writer_lease.TARGET_WRITER_LEASE_SECONDS,
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> FederatedReplicaCommitResult:
    """Commit staged ciphertext through Receipt v4/Commit v4 production semantics."""

    if type(owner_instance_id) is not str or _INSTANCE_ID_PATTERN.fullmatch(owner_instance_id) is None:
        raise FederatedReplicaCommitError("FEDERATION_REPLICA_OWNER_INSTANCE_INVALID")
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 3:
        raise FederatedReplicaCommitError("FEDERATION_REPLICA_WRITER_LEASE_INVALID")
    try:
        package = receiver.assemble_verified_package(transfer_id)
        transfer = receiver.transfer_journal.get_transfer(transfer_id)
    except federation_replica_receiver.FederatedReplicaReceiverError as exc:
        raise FederatedReplicaCommitError(exc.code) from exc
    except federation_transfer_journal.FederatedTransferJournalError as exc:
        raise FederatedReplicaCommitError(exc.code) from exc
    if transfer is None:
        raise FederatedReplicaCommitError("FEDERATION_TRANSFER_NOT_FOUND")
    if transfer.get("objectSetDigest") != "sha256:" + package.object_set_digest:
        raise FederatedReplicaCommitError("FEDERATION_REPLICA_OBJECT_SET_DIGEST_MISMATCH")
    try:
        target = backup_publish.resolve_target(target_id)
    except Exception as exc:
        raise FederatedReplicaCommitError("FEDERATION_REPLICA_TARGET_UNAVAILABLE", str(exc)) from exc
    if not isinstance(target, backup_publish.ResolvedTarget) or target.target_id != target_id:
        raise FederatedReplicaCommitError("FEDERATION_REPLICA_TARGET_IDENTITY_INVALID")
    if target.kind != "s3" or target.root is not None or target.store is None:
        raise FederatedReplicaCommitError("FEDERATION_REPLICA_PROVIDER_TARGET_REQUIRED")

    components = receiver.list_components(transfer_id)
    transfer = _advance_to_remote_verifying(
        receiver.transfer_journal,
        transfer,
        components,
        target_id=target_id,
        now=now,
    )
    fencing_token = backup_scheduler.allocate_fencing_token()
    writer = backup_writer_lease.TargetWriterLease(
        None,
        store=target.store,
        target_id=target_id,
        owner_run_id=federated_run_id(transfer_id),
        owner_instance_id=owner_instance_id,
        fencing_token=fencing_token,
        lease_seconds=lease_seconds,
    )
    try:
        try:
            writer.acquire()
        except AppError as exc:
            raise FederatedReplicaCommitError("FEDERATION_REPLICA_REMOTE_COMMIT_FAILED", str(exc)) from exc
        checkpoint = _RenewingWriterCheckpoint(
            writer,
            lease_seconds=lease_seconds,
            monotonic_clock=monotonic_clock,
        )
        checkpoint()
        existing = _read_existing_commit(
            target,
            package=package,
            transfer=transfer,
            checkpoint=checkpoint,
        )
        reconciled = existing is not None
        converged = reconciled
        if existing is None:
            try:
                attempted = backup_publish.publish_backup(
                    target,
                    package,
                    run_id=federated_run_id(transfer_id),
                    policy_id=str(transfer["policyId"]),
                    schedule_slot=federated_schedule_slot(transfer_id),
                    fencing_token=fencing_token,
                    checkpoint=checkpoint,
                    traffic_class=backup_transfer_budget.TrafficClass.P3_REQUIRED_REPLICATION,
                    local_durability_credit=False,
                )
                converged = bool(attempted.converged)
            except AppError as exc:
                raise FederatedReplicaCommitError("FEDERATION_REPLICA_REMOTE_COMMIT_FAILED", str(exc)) from exc
            except Exception as exc:
                raise FederatedReplicaCommitError("FEDERATION_REPLICA_REMOTE_RESULT_UNKNOWN", str(exc)) from exc
            checkpoint()
            existing = _read_existing_commit(
                target,
                package=package,
                transfer=transfer,
                checkpoint=checkpoint,
            )
            if existing is None:
                raise FederatedReplicaCommitError("FEDERATION_REPLICA_REMOTE_RESULT_UNKNOWN")
        published = existing
        checkpoint()
        try:
            backup_replication.append_target_local_catalog(target, published.receipt)
        except Exception as exc:
            raise FederatedReplicaCommitError("FEDERATION_REPLICA_REMOTE_RESULT_UNKNOWN", str(exc)) from exc
        checkpoint()
        _record_remote_committed(
            receiver.transfer_journal,
            transfer,
            target_id=target_id,
            published=published,
            now=now,
        )
        return FederatedReplicaCommitResult(
            transfer_id=transfer_id,
            target_id=target_id,
            receipt=dict(published.receipt),
            commit=dict(published.commit),
            fencing_token=fencing_token,
            converged=converged,
            reconciled=reconciled,
        )
    except FederatedReplicaCommitError:
        raise
    except AppError as exc:
        raise FederatedReplicaCommitError("FEDERATION_REPLICA_REMOTE_RESULT_UNKNOWN", str(exc)) from exc
    finally:
        try:
            writer.release()
        except Exception:
            pass

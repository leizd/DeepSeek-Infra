"""Immutable federated transfer identity, admission, and query-first reconciliation."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import federation_identity, federation_transfer_journal

TRANSFER_RECONCILIATION_SCHEMA = "federated-transfer-reconciliation-v1"

_TRANSFER_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTROL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class FederatedTransferError(RuntimeError):
    """Fail-closed federated-transfer error with a stable code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = str(code)
        super().__init__(message or self.code)


def _utc_iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FederatedTransferError("FEDERATION_TRANSFER_TIMESTAMP_INVALID")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _transfer_id(value: Any) -> str:
    if type(value) is not str or _TRANSFER_ID_PATTERN.fullmatch(value) is None:
        raise FederatedTransferError("FEDERATION_TRANSFER_ID_INVALID")
    return value


def _policy_id(value: Any) -> str:
    if type(value) is not str or _CONTROL_ID_PATTERN.fullmatch(value) is None:
        raise FederatedTransferError("FEDERATION_TRANSFER_POLICY_ID_INVALID")
    return value


def _canonical_json(value: Any) -> str:
    try:
        return federation_identity.canonical_federation_json(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederatedTransferError("FEDERATION_TRANSFER_CANONICAL_PAYLOAD_INVALID") from exc


def _record_digest(record: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(record).encode("utf-8")).hexdigest()


def _wrap_journal_error(exc: federation_transfer_journal.FederatedTransferJournalError) -> FederatedTransferError:
    return FederatedTransferError(exc.code)


def transfer_identity_document(
    *,
    source_fleet_id: str,
    destination_fleet_id: str,
    backup_id: str,
    object_set_digest: str,
) -> dict[str, str]:
    try:
        return federation_transfer_journal.transfer_identity_document(
            source_fleet_id=source_fleet_id,
            destination_fleet_id=destination_fleet_id,
            backup_id=backup_id,
            object_set_digest=object_set_digest,
        )
    except federation_transfer_journal.FederatedTransferJournalError as exc:
        raise _wrap_journal_error(exc) from exc


def derive_transfer_id(
    *,
    source_fleet_id: str,
    destination_fleet_id: str,
    backup_id: str,
    object_set_digest: str,
) -> str:
    try:
        return federation_transfer_journal.derive_transfer_id(
            source_fleet_id=source_fleet_id,
            destination_fleet_id=destination_fleet_id,
            backup_id=backup_id,
            object_set_digest=object_set_digest,
        )
    except federation_transfer_journal.FederatedTransferJournalError as exc:
        raise _wrap_journal_error(exc) from exc


def _assert_record_binding(
    record: dict[str, Any],
    *,
    transfer_id: str,
    source_fleet_id: str,
    destination_fleet_id: str,
    policy_id: str,
    backup_id: str,
    object_set_digest: str,
) -> None:
    if (
        record.get("transferId") != transfer_id
        or record.get("sourceFleetId") != source_fleet_id
        or record.get("destinationFleetId") != destination_fleet_id
        or record.get("policyId") != policy_id
        or record.get("backupId") != backup_id
        or record.get("objectSetDigest") != object_set_digest
    ):
        raise FederatedTransferError("FEDERATION_TRANSFER_IDENTITY_CONFLICT")


def propose_transfer(
    *,
    journal: federation_transfer_journal.FederatedTransferJournal,
    source_fleet_id: str,
    destination_fleet_id: str,
    policy_id: str,
    backup_id: str,
    object_set_digest: str,
    now: datetime,
) -> dict[str, Any]:
    transfer = derive_transfer_id(
        source_fleet_id=source_fleet_id,
        destination_fleet_id=destination_fleet_id,
        backup_id=backup_id,
        object_set_digest=object_set_digest,
    )
    try:
        return journal.persist_proposed_transfer(
            transfer_id=transfer,
            source_fleet_id=source_fleet_id,
            destination_fleet_id=destination_fleet_id,
            policy_id=_policy_id(policy_id),
            backup_id=backup_id,
            object_set_digest=object_set_digest,
            now=now,
        )
    except federation_transfer_journal.FederatedTransferJournalError as exc:
        raise _wrap_journal_error(exc) from exc


def accept_or_resume_transfer(
    *,
    journal: federation_transfer_journal.FederatedTransferJournal,
    transfer_id: str,
    source_fleet_id: str,
    destination_fleet_id: str,
    policy_id: str,
    backup_id: str,
    object_set_digest: str,
    now: datetime,
) -> dict[str, Any]:
    transfer = _transfer_id(transfer_id)
    policy = _policy_id(policy_id)
    expected_transfer = derive_transfer_id(
        source_fleet_id=source_fleet_id,
        destination_fleet_id=destination_fleet_id,
        backup_id=backup_id,
        object_set_digest=object_set_digest,
    )
    try:
        existing = journal.get_transfer(transfer)
    except federation_transfer_journal.FederatedTransferJournalError as exc:
        raise _wrap_journal_error(exc) from exc
    if existing is not None:
        _assert_record_binding(
            existing,
            transfer_id=transfer,
            source_fleet_id=source_fleet_id,
            destination_fleet_id=destination_fleet_id,
            policy_id=policy,
            backup_id=backup_id,
            object_set_digest=object_set_digest,
        )
        if transfer != expected_transfer:
            raise FederatedTransferError("FEDERATION_TRANSFER_IDENTITY_CONFLICT")
        return existing
    if transfer != expected_transfer:
        raise FederatedTransferError("FEDERATION_TRANSFER_ID_INVALID")
    try:
        return journal.persist_proposed_transfer(
            transfer_id=transfer,
            source_fleet_id=source_fleet_id,
            destination_fleet_id=destination_fleet_id,
            policy_id=policy,
            backup_id=backup_id,
            object_set_digest=object_set_digest,
            now=now,
        )
    except federation_transfer_journal.FederatedTransferJournalError as exc:
        raise _wrap_journal_error(exc) from exc


def reconcile_transfer(
    *,
    journal: federation_transfer_journal.FederatedTransferJournal,
    transfer_id: str,
    source_fleet_id: str,
    destination_fleet_id: str,
    policy_id: str,
    backup_id: str,
    object_set_digest: str,
    now: datetime,
) -> dict[str, Any]:
    reconciled_at = _utc_iso(now)
    transfer = _transfer_id(transfer_id)
    policy = _policy_id(policy_id)
    expected_transfer = derive_transfer_id(
        source_fleet_id=source_fleet_id,
        destination_fleet_id=destination_fleet_id,
        backup_id=backup_id,
        object_set_digest=object_set_digest,
    )
    try:
        existing = journal.get_transfer(transfer)
    except federation_transfer_journal.FederatedTransferJournalError as exc:
        raise _wrap_journal_error(exc) from exc
    if existing is None:
        if transfer != expected_transfer:
            raise FederatedTransferError("FEDERATION_TRANSFER_ID_INVALID")
        return {
            "schema": TRANSFER_RECONCILIATION_SCHEMA,
            "transferId": transfer,
            "status": "NOT_FOUND",
            "state": None,
            "revision": None,
            "role": None,
            "identityDigest": None,
            "recordDigest": None,
            "reconciledAt": reconciled_at,
        }
    _assert_record_binding(
        existing,
        transfer_id=transfer,
        source_fleet_id=source_fleet_id,
        destination_fleet_id=destination_fleet_id,
        policy_id=policy,
        backup_id=backup_id,
        object_set_digest=object_set_digest,
    )
    if transfer != expected_transfer:
        raise FederatedTransferError("FEDERATION_TRANSFER_IDENTITY_CONFLICT")
    state = str(existing["state"])
    return {
        "schema": TRANSFER_RECONCILIATION_SCHEMA,
        "transferId": transfer,
        "status": "SUCCEEDED" if state == federation_transfer_journal.STATE_SUCCEEDED else "RESUME",
        "state": state,
        "revision": int(existing["revision"]),
        "role": str(existing["role"]),
        "identityDigest": str(existing["identityDigest"]),
        "recordDigest": _record_digest(existing),
        "reconciledAt": reconciled_at,
    }

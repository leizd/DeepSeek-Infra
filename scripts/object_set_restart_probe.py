"""Process-boundary probe for durable object-set restore state.

The command is read from stdin so Recovery Identities never appear in argv or
the environment. Each invocation performs one bounded phase and exits.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

from deepseek_infra.infra.workspace import backup_crypto, backup_remote_restore, backups


def _read_command() -> dict[str, Any]:
    value = json.load(sys.stdin)
    if not isinstance(value, dict):
        raise ValueError("probe command must be an object")
    return value


def _emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True) + "\n")


def _install_s3_object_get_audit() -> tuple[list[str], Callable[[], None]]:
    from deepseek_infra.infra.workspace.backup_target_s3 import S3TargetStore

    object_gets: list[str] = []
    original_get_bytes = S3TargetStore.get_bytes

    def counted_get_bytes(
        store: S3TargetStore,
        key: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> bytes | None:
        if key.startswith("objects/sha256/"):
            object_gets.append(key)
        return original_get_bytes(store, key, offset=offset, length=length)

    setattr(S3TargetStore, "get_bytes", counted_get_bytes)

    def restore() -> None:
        setattr(S3TargetStore, "get_bytes", original_get_bytes)

    return object_gets, restore


def main() -> int:
    command = _read_command()
    action = str(command.get("action") or "")
    if action in {"pause-job", "resume-job", "abort-job"}:
        restore_id = str(command["restoreId"])
        operation = {
            "pause-job": backup_remote_restore.request_restore_pause,
            "resume-job": backup_remote_restore.resume_restore_session,
            "abort-job": backup_remote_restore.request_restore_abort,
        }[action]
        _emit(operation(restore_id))
        return 0
    if action == "create-partial-fetch":
        created = backup_remote_restore.create_restore_from_target(
            target_id=str(command["targetId"]),
            backup_id=str(command["backupId"]),
            selection=command.get("selection"),
        )
        fetched = backup_remote_restore.fetch_restore_session(str(created["restoreId"]), max_bytes=1)
        _emit({"restoreId": created["restoreId"], "phase": fetched["phase"]})
        return 0
    restore_id = str(command["restoreId"])
    if action == "resume-and-prepare":
        object_gets: list[str] = []
        if bool(command.get("auditObjectGets")):
            object_gets, _restore_object_get_audit = _install_s3_object_get_audit()
        while True:
            fetched = backup_remote_restore.fetch_restore_session(restore_id)
            if str(fetched.get("phase") or "") == "controls-fetched":
                break
        secret_kind = str(command.get("secretKind") or "age-identity")
        backup_crypto.put_secret(restore_id, secret_kind, str(command["secret"]))
        session = backup_remote_restore.read_restore_session(restore_id)
        if session is None:
            raise ValueError("restore session disappeared")
        selection = session.get("selection")
        preview = backup_remote_restore.preview_restore_from_target(
            target_id=str(session["targetId"]),
            backup_id=str(session["backupId"]),
            selection=selection,
            restore_id=restore_id,
        )
        while True:
            fetched = backup_remote_restore.fetch_restore_session(restore_id)
            if str(fetched.get("phase") or "") == "components-fetched":
                break
        prepared = backup_remote_restore.materialize_federated_restore(
            restore_id,
            mode="merge",
            owner_document_id="restart-probe",
        )
        _emit(
            {
                "restoreId": restore_id,
                "phase": prepared["phase"],
                "projection": preview.get("projection"),
                "objectGets": object_gets,
            }
        )
        return 0
    if action == "resume-commit-complete":
        committed = backups.commit_restore(restore_id)
        completed = backups.complete_restore(restore_id)
        backup_remote_restore.advance_federated_phase(restore_id, "complete")
        _emit(
            {
                "restoreId": restore_id,
                "commitPhase": committed["phase"],
                "phase": completed["phase"],
            }
        )
        return 0
    raise ValueError(f"unsupported probe action: {action}")


if __name__ == "__main__":
    raise SystemExit(main())

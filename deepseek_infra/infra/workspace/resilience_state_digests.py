"""Independent pre/post digests for every What-If mutation domain."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Sequence

from deepseek_infra.infra.workspace import (
    backup_control,
    backup_control_recovery,
    backup_targets,
    resilience_action_journal,
)


class StateDigestUnavailable(RuntimeError):
    """A mutation-domain snapshot could not be read completely."""


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _target_inventory(target_id: str) -> dict[str, Any]:
    store = backup_targets.open_target_store(target_id, write_intent=False)
    cursor: str | None = None
    seen_cursors: set[str] = set()
    objects: list[dict[str, Any]] = []
    while True:
        page = store.list_objects("", cursor=cursor, limit=1000)
        objects.extend(
            {
                "key": item.key,
                "size": int(item.size),
                "etag": item.etag,
                "sha256": item.sha256,
                "versionId": item.version_id,
                "lastModified": item.last_modified,
            }
            for item in page.objects
        )
        next_cursor = page.cursor
        if next_cursor is None:
            break
        if next_cursor in seen_cursors:
            raise StateDigestUnavailable(f"storage inventory cursor repeated for {target_id}")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        if len(objects) > 1_000_000:
            raise StateDigestUnavailable(f"storage inventory exceeds bounded proof size for {target_id}")
    normalized = sorted(objects, key=lambda item: (str(item["key"]), str(item["versionId"] or ""), str(item["etag"])))
    return {"targetId": target_id, "objectCount": len(normalized), "inventoryDigest": _digest(normalized)}


def capture_mutation_state(
    target_ids: Sequence[str] | None = None,
    *,
    require_complete: bool = True,
) -> dict[str, Any]:
    """Read Storage, Authority, Action Journal, Policy, and Target state without mutation."""
    try:
        target_records = backup_control.list_targets()
        selected_ids = sorted(
            {str(value) for value in (target_ids or []) if str(value)}
            or {str(item.get("targetId") or "") for item in target_records if str(item.get("targetId") or "")}
        )
        inventory = [_target_inventory(target_id) for target_id in selected_ids]
        authority = backup_control_recovery.authority_health_snapshot()
        actions = resilience_action_journal.list_actions(limit=1_000_000)
        policies = backup_control.list_policies()
    except Exception as exc:
        if require_complete:
            raise StateDigestUnavailable(f"mutation state unavailable: {type(exc).__name__}: {exc}") from exc
        inventory = []
        authority = {"status": "unavailable", "detail": f"{type(exc).__name__}: {exc}"}
        actions = []
        policies = []
        target_records = []
    digests = {
        "storage": _digest(inventory),
        "authority": _digest(authority),
        "actionJournal": _digest(actions),
        "policy": _digest(policies),
        "target": _digest(target_records),
    }
    return {
        "stateDigest": _digest(digests),
        "digests": digests,
        "storageInventory": inventory,
        "targetIds": selected_ids if "selected_ids" in locals() else [],
        "capturedAt": _utc_iso(),
    }

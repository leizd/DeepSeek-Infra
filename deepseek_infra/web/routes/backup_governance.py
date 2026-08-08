"""Scheduled backup governance routes (4.4.6)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_executor,
    backup_policies,
    backup_publish,
    backup_remote_restore,
    backup_retention,
    backup_scheduler,
    backup_scrub,
    backup_targets,
    backup_writer_lease,
    backups,
)
from deepseek_infra.web.http_utils import json_response, read_json_body, require_api_auth


def _target_root(target_id: str, *, write_intent: bool = False) -> Path:
    target = backup_publish.resolve_target(str(target_id or "managed-local"), write_intent=write_intent)
    return target.require_root()


@contextmanager
def _target_writer(root: Path | None, target_id: str, *, store: Any | None = None) -> Iterator[backup_writer_lease.TargetWriterLease]:
    lease = backup_writer_lease.TargetWriterLease(
        root,
        store=store,
        target_id=target_id,
        owner_run_id=f"api_{uuid.uuid4().hex[:12]}",
        owner_instance_id=backup_scheduler.instance_id_from_environment(),
        fencing_token=backup_scheduler.allocate_fencing_token(),
    )
    lease.acquire()
    try:
        yield lease
    finally:
        lease.release()


def _find_backup_root(backup_id: str) -> tuple[Path, dict[str, Any], str]:
    roots: list[tuple[str, Path]] = [("managed-local", backups.BACKUP_DIR)]
    for target in backup_targets.list_targets():
        try:
            roots.append((str(target["targetId"]), Path(str(target["path"]))))
        except KeyError:
            continue
    for target_id, root in roots:
        record = backup_catalog.catalog_state(root).get(backup_id)
        if record is not None:
            return root, record, target_id
    raise AppError("Backup not found in any catalog", code=ErrorCode.NOT_FOUND, status=404)


def create_backup_governance_router() -> APIRouter:
    router = APIRouter()

    # ── Backup policies ────────────────────────────────────────────────────

    @router.get("/api/workspace/backup-policies")
    async def api_backup_policies_list(request: Request) -> JSONResponse:
        require_api_auth(request)
        policies = backup_policies.list_policies()
        return json_response(
            {
                "policies": policies,
                "nextRuns": {str(policy.get("policyId")): backup_scheduler.next_run_for_policy(policy) for policy in policies},
            }
        )

    @router.post("/api/workspace/backup-policies")
    async def api_backup_policies_create(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response(backup_policies.create_policy(await read_json_body(request, max_bytes=64_000)))

    @router.patch("/api/workspace/backup-policies/{policy_id}")
    async def api_backup_policies_patch(request: Request, policy_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response(backup_policies.update_policy(policy_id, await read_json_body(request, max_bytes=64_000)))

    @router.delete("/api/workspace/backup-policies/{policy_id}")
    async def api_backup_policies_delete(request: Request, policy_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response(backup_policies.delete_policy(policy_id))

    @router.post("/api/workspace/backup-policies/{policy_id}/run")
    async def api_backup_policies_run(request: Request, policy_id: str) -> JSONResponse:
        require_api_auth(request)
        policy = backup_policies.get_policy(policy_id)
        instance_id = backup_scheduler.instance_id_from_environment()
        run = backup_scheduler.claim_manual_run(policy, instance_id=instance_id)
        outcome = backup_executor.execute_run(run, instance_id=instance_id)
        return json_response(outcome)

    # ── Backup targets ─────────────────────────────────────────────────────

    @router.get("/api/workspace/backup-targets")
    async def api_backup_targets_list(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response({"targets": backup_targets.list_targets(), "health": backup_scheduler.target_health()})

    @router.post("/api/workspace/backup-targets")
    async def api_backup_targets_create(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=64_000)
        kind = str(payload.get("kind") or "filesystem").strip().lower()
        if kind in {"s3", "s3-compatible"}:
            provider = payload.get("credentialProvider") if isinstance(payload.get("credentialProvider"), dict) else None
            return json_response(
                backup_targets.init_s3_target(
                    bucket=str(payload.get("bucket") or ""),
                    prefix=str(payload.get("prefix") or ""),
                    region=str(payload.get("region") or "") or None,
                    endpoint_url=str(payload.get("endpointUrl") or "") or None,
                    expected_bucket_owner=str(payload.get("expectedBucketOwner") or "") or None,
                    label=str(payload.get("label") or ""),
                    credential_provider=provider,
                    probe=bool(payload.get("probe", True)),
                )
            )
        if kind == "webdav":
            raise AppError("WebDAV targets are reserved but not GA in 4.4.6", code=ErrorCode.INVALID_REQUEST, status=501)
        path = Path(str(payload.get("path") or ""))
        return json_response(backup_targets.init_target(path, label=str(payload.get("label") or "")))

    @router.post("/api/workspace/backup-targets/{target_id}/probe")
    async def api_backup_targets_probe(request: Request, target_id: str) -> JSONResponse:
        require_api_auth(request)
        result = backup_targets.probe_target(target_id)
        backup_scheduler.record_target_health(target_id, "ok" if result.get("ready") else "blocked", str(result.get("detail") or "")[:200] or None)
        return json_response(result)

    @router.delete("/api/workspace/backup-targets/{target_id}")
    async def api_backup_targets_delete(request: Request, target_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response(backup_targets.delete_target(target_id))

    @router.post("/api/workspace/backup-targets/{target_id}/adopt")
    async def api_backup_targets_adopt(request: Request, target_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response(backup_targets.adopt_target_incarnation(target_id))

    @router.post("/api/workspace/backup-targets/register-new")
    async def api_backup_targets_register_new(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=64_000)
        path = Path(str(payload.get("path") or ""))
        return json_response(backup_targets.reinitialize_target(path, label=str(payload.get("label") or "")))

    # ── Runs and catalog ───────────────────────────────────────────────────

    @router.get("/api/workspace/backup-runs")
    async def api_backup_runs(request: Request) -> JSONResponse:
        require_api_auth(request)
        policy_id = request.query_params.get("policyId") or None
        return json_response({"runs": backup_scheduler.list_runs(policy_id=policy_id)})

    @router.get("/api/workspace/backup-catalog")
    async def api_backup_catalog(request: Request) -> JSONResponse:
        require_api_auth(request)
        target_id = request.query_params.get("targetId") or "managed-local"
        root = _target_root(target_id)
        policy_id = request.query_params.get("policyId") or None
        entries = backup_catalog.list_backups(root, policy_id=policy_id)
        return json_response(
            {
                "backups": entries,
                "chainValid": backup_catalog.verify_chain(root),
                "integrity": backup_catalog.find_orphans_and_missing(root),
                "health": backup_scrub.backup_health(root),
            }
        )

    @router.post("/api/workspace/backup-catalog/{backup_id}/pin")
    async def api_backup_pin(request: Request, backup_id: str) -> JSONResponse:
        require_api_auth(request)
        root, _, target_id = _find_backup_root(backup_id)
        with _target_writer(root, target_id) as writer:
            backup_catalog.pin_backup(root, backup_id, True, writer=writer)
        return json_response({"backupId": backup_id, "pinned": True})

    @router.delete("/api/workspace/backup-catalog/{backup_id}/pin")
    async def api_backup_unpin(request: Request, backup_id: str) -> JSONResponse:
        require_api_auth(request)
        root, _, target_id = _find_backup_root(backup_id)
        with _target_writer(root, target_id) as writer:
            backup_catalog.pin_backup(root, backup_id, False, writer=writer)
        return json_response({"backupId": backup_id, "pinned": False})

    # ── Retention ──────────────────────────────────────────────────────────

    @router.post("/api/workspace/retention/preview")
    async def api_retention_preview(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=64_000)
        policy = backup_policies.get_policy(str(payload.get("policyId") or ""))
        retention = backup_retention.get_retention_policy(str(policy.get("retentionPolicyId") or "default"))
        root = _target_root(str(policy.get("targetId") or "managed-local"))
        timezone_name = str((policy.get("schedule") or {}).get("timezone") or "UTC")
        preview = backup_retention.preview_retention(retention, root, policy_timezone=timezone_name)
        preview.pop("trashRecords", None)
        return json_response(preview)

    @router.post("/api/workspace/retention/apply")
    async def api_retention_apply(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=64_000)
        policy = backup_policies.get_policy(str(payload.get("policyId") or ""))
        retention = backup_retention.get_retention_policy(str(policy.get("retentionPolicyId") or "default"))
        timezone_name = str((policy.get("schedule") or {}).get("timezone") or "UTC")
        root = _target_root(str(policy.get("targetId") or "managed-local"), write_intent=True)
        preview = payload.get("preview")
        with _target_writer(root, str(policy.get("targetId") or "managed-local")) as writer:
            applied = backup_retention.apply_retention(
                retention,
                root,
                policy_timezone=timezone_name,
                preview=preview if isinstance(preview, dict) else None,
                writer=writer,
            )
            finalized = backup_retention.finalize_retention(retention, root, policy_timezone=timezone_name, writer=writer)
        return json_response({"applied": applied, "finalized": finalized})

    # ── Scrub and restore drills ───────────────────────────────────────────

    @router.post("/api/workspace/backups/{backup_id}/scrub")
    async def api_backup_scrub(request: Request, backup_id: str) -> JSONResponse:
        require_api_auth(request)
        root, _, target_id = _find_backup_root(backup_id)
        with _target_writer(root, target_id):
            return json_response(backup_scrub.scrub_backup(root, backup_id, target_id=target_id))

    @router.post("/api/workspace/backups/{backup_id}/verify-unlock")
    async def api_backup_verify_unlock(request: Request, backup_id: str) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=128_000)
        identity_text = str(payload.get("identity") or "")
        if not identity_text.startswith("AGE-SECRET-KEY-"):
            raise AppError("A valid Recovery Identity is required", code=ErrorCode.INVALID_PAYLOAD)
        identity = bytearray(identity_text.encode("utf-8"))
        try:
            root, _, target_id = _find_backup_root(backup_id)
            staged = backups.RESTORE_DIR / "drills"
            with _target_writer(root, target_id):
                return json_response(backup_scrub.verify_unlock_drill(root, backup_id, identity, staged_root=staged))
        finally:
            for index in range(len(identity)):
                identity[index] = 0

    @router.post("/api/workspace/restores/from-target")
    async def api_restore_from_target(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=64_000)
        return json_response(
            backup_remote_restore.restore_from_target(
                target_id=str(payload.get("targetId") or ""),
                backup_id=str(payload.get("backupId") or ""),
            )
        )

    @router.get("/api/workspace/backup-target-capabilities")
    async def api_backup_target_capabilities(request: Request) -> JSONResponse:
        require_api_auth(request)
        from deepseek_infra.infra.workspace import backup_target_s3

        return json_response(
            {
                "s3TargetAvailable": backup_target_s3.s3_sdk_available(),
                "webdavTargetAvailable": False,
                "supportedKinds": ["filesystem", "s3"] if backup_target_s3.s3_sdk_available() else ["filesystem"],
                "reservedKinds": ["webdav"],
            }
        )

    return router

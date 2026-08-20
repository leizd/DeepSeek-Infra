"""Scheduled backup governance routes (4.5.1)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_dr_audit,
    backup_dr_readiness,
    backup_executor,
    backup_policies,
    backup_publish,
    backup_recovery_drill,
    backup_recovery_planner,
    backup_remote_restore,
    backup_replication,
    backup_retention,
    backup_scheduler,
    backup_scrub,
    backup_targets,
    backup_writer_lease,
    backups,
)
from deepseek_infra.web.http_utils import json_response, read_json_body, require_api_auth


@dataclass(frozen=True, slots=True)
class BackupTargetSession:
    target_id: str
    kind: str
    store: Any
    root: Path | None


def open_target_session(target_id: str, *, write_intent: bool = False) -> BackupTargetSession:
    target = backup_publish.resolve_target(str(target_id or "managed-local"), write_intent=write_intent)
    store = target.require_store()
    return BackupTargetSession(target_id=target.target_id, kind=target.kind, store=store, root=target.root)


def _target_root(target_id: str, *, write_intent: bool = False) -> Path:
    return open_target_session(target_id, write_intent=write_intent).root or backup_publish.resolve_target(target_id, write_intent=write_intent).require_root()


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
        path_value = str(target.get("path") or "")
        if path_value:
            roots.append((str(target["targetId"]), Path(path_value)))
    for target_id, root in roots:
        record = backup_catalog.catalog_state(root).get(backup_id)
        if record is not None:
            return root, record, target_id
    raise AppError("Backup not found in any catalog", code=ErrorCode.NOT_FOUND, status=404)


def _find_backup_session(backup_id: str) -> tuple[BackupTargetSession, dict[str, Any]]:
    candidates = ["managed-local"] + [str(item.get("targetId") or "") for item in backup_targets.list_targets()]
    for target_id in candidates:
        if not target_id:
            continue
        try:
            session = open_target_session(target_id, write_intent=False)
        except AppError:
            continue
        if session.root is not None:
            record = backup_catalog.catalog_state(session.root).get(backup_id)
        else:
            record = backup_catalog.catalog_state_store(session.store).get(backup_id)
        if record is not None:
            return session, record
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

    @router.post("/api/workspace/backup-policies/{policy_id}/promote-primary")
    async def api_backup_policies_promote_primary(request: Request, policy_id: str) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=16_000)
        target_id = str(payload.get("targetId") or "").strip()
        if not target_id:
            raise AppError("targetId is required for primary promotion", code=ErrorCode.INVALID_PAYLOAD, status=400)
        exp_rev = payload.get("expectedPolicyRevision")
        exp_epoch = payload.get("expectedFailoverEpoch")
        exp_bid = payload.get("expectedLatestBackupId")
        exp_osd = payload.get("expectedLatestObjectSetDigest")
        from deepseek_infra.infra.workspace import backup_write_continuity

        res = backup_write_continuity.promote_primary_target(
            policy_id,
            target_id,
            expected_policy_revision=int(exp_rev) if exp_rev is not None else None,
            expected_failover_epoch=int(exp_epoch) if exp_epoch is not None else None,
            expected_latest_backup_id=str(exp_bid) if exp_bid is not None else None,
            expected_latest_object_set_digest=str(exp_osd) if exp_osd is not None else None,
        )
        return json_response(res)

    @router.get("/api/workspace/backup-policies/{policy_id}/continuity")
    async def api_backup_policies_continuity(request: Request, policy_id: str) -> JSONResponse:
        require_api_auth(request)
        from deepseek_infra.infra.workspace import backup_write_continuity

        state = backup_write_continuity.get_write_continuity_state(policy_id)
        return json_response(state)

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
        failure_domain = str(payload.get("failureDomain") or "").strip() or None
        provider_name = str(payload.get("provider") or "").strip() or None
        jurisdiction = str(payload.get("jurisdiction") or "").strip() or None
        priority = int(payload.get("priority") or 0)
        cost_class = str(payload.get("costClass") or "").strip() or None
        storage_cost = payload.get("storageCostPerGiBMonth")
        egress_cost = payload.get("egressCostPerGiB")
        max_read = payload.get("maxReadBytesPerSecond")
        max_write = payload.get("maxWriteBytesPerSecond")
        max_concurrent = payload.get("maxConcurrentTransfers")

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
                    failure_domain=failure_domain,
                    provider=provider_name,
                    jurisdiction=jurisdiction,
                    priority=priority,
                    cost_class=cost_class,
                    storage_cost_per_gib_month=storage_cost,
                    egress_cost_per_gib=egress_cost,
                    max_read_bytes_per_second=max_read,
                    max_write_bytes_per_second=max_write,
                    max_concurrent_transfers=max_concurrent,
                    credential_provider=provider,
                    probe=bool(payload.get("probe", True)),
                )
            )
        if kind == "webdav":
            raise AppError("WebDAV targets are reserved but not GA in 4.4.6", code=ErrorCode.INVALID_REQUEST, status=501)
        path = Path(str(payload.get("path") or ""))
        return json_response(
            backup_targets.init_target(
                path,
                label=str(payload.get("label") or ""),
                region=str(payload.get("region") or "") or None,
                failure_domain=failure_domain,
                provider=provider_name,
                jurisdiction=jurisdiction,
                priority=priority,
                cost_class=cost_class,
                storage_cost_per_gib_month=storage_cost,
                egress_cost_per_gib=egress_cost,
                max_read_bytes_per_second=max_read,
                max_write_bytes_per_second=max_write,
                max_concurrent_transfers=max_concurrent,
            )
        )

    @router.post("/api/workspace/backup-targets/register-new")
    async def api_backup_targets_register_new(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=64_000)
        path = Path(str(payload.get("path") or ""))
        label = str(payload.get("label") or "")
        return json_response(backup_targets.reinitialize_target(path, label=label))

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

    @router.post("/api/workspace/backup-targets/{target_id}/drain")
    async def api_backup_targets_drain(request: Request, target_id: str) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=16_000)
        reason = str(payload.get("reason") or "administrative-drain")
        force = bool(payload.get("force", False))
        from deepseek_infra.infra.workspace import backup_drain
        return json_response(backup_drain.initiate_target_drain(target_id, reason=reason, force=force))

    @router.post("/api/workspace/backup-targets/{target_id}/activate")
    async def api_backup_targets_activate(request: Request, target_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response(backup_targets.activate_target(target_id))

    @router.get("/api/workspace/backup-rebalances")
    async def api_backup_rebalances_list(request: Request) -> JSONResponse:
        require_api_auth(request)
        policy_id = request.query_params.get("policyId") or None
        from deepseek_infra.infra.workspace import backup_replication
        return json_response({"rebalances": backup_replication.list_rebalance_jobs(policy_id=policy_id)})

    @router.post("/api/workspace/backup-rebalances")
    async def api_backup_rebalances_trigger(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=16_000)
        policy_id = str(payload.get("policyId") or "").strip()
        if not policy_id:
            raise AppError("policyId is required to trigger rebalance", code=ErrorCode.INVALID_PAYLOAD, status=400)
        from deepseek_infra.infra.workspace import backup_replication
        instance_id = backup_scheduler.instance_id_from_environment()
        return json_response(backup_replication.rebalance_policy_replicas(policy_id, instance_id=instance_id))

    # ── Runs and catalog ───────────────────────────────────────────────────

    @router.get("/api/workspace/backup-runs")
    async def api_backup_runs(request: Request) -> JSONResponse:
        require_api_auth(request)
        policy_id = request.query_params.get("policyId") or None
        return json_response({"runs": backup_scheduler.list_runs(policy_id=policy_id)})

    @router.get("/api/workspace/disaster-recovery/status")
    async def api_disaster_recovery_status(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response(backup_dr_readiness.readiness_status())

    @router.post("/api/workspace/disaster-recovery/audit")
    async def api_disaster_recovery_audit(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=16_000)
        target_id = str(payload.get("targetId") or "managed-local")
        page_size = int(payload.get("pageSize") or 100)
        cursor = payload.get("cursor")
        audit_id = payload.get("auditId")
        return json_response(
            backup_dr_audit.audit_remote_target(
                target_id,
                page_size=page_size,
                cursor=str(cursor) if cursor else None,
                audit_id=str(audit_id) if audit_id else None,
            )
        )

    @router.post("/api/workspace/disaster-recovery/audit/{audit_id}/resume")
    async def api_disaster_recovery_audit_resume(request: Request, audit_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response(backup_dr_audit.resume_audit(audit_id))

    @router.post("/api/workspace/disaster-recovery/plan")
    async def api_disaster_recovery_plan(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=32_000)
        policy_id = str(payload.get("policyId") or "")
        if not policy_id:
            raise AppError("policyId is required", code=ErrorCode.INVALID_PAYLOAD)
        return json_response(
            backup_recovery_planner.plan_recovery(
                policy_id=policy_id,
                backup_id=str(payload["backupId"]) if payload.get("backupId") else None,
                restore_selection=payload.get("selection") if isinstance(payload.get("selection"), dict) else None,
                preferred_target_id=str(payload["preferredTargetId"]) if payload.get("preferredTargetId") else None,
            )
        )

    @router.get("/api/workspace/disaster-recovery/replication")
    async def api_disaster_recovery_replication(request: Request) -> JSONResponse:
        require_api_auth(request)
        policy_id = request.query_params.get("policyId") or ""
        backup_id = request.query_params.get("backupId") or ""
        jobs = backup_replication.list_jobs(
            policy_id=policy_id or None,
            backup_id=backup_id or None,
            limit=100,
        )
        return json_response({"jobs": jobs})

    @router.post("/api/workspace/disaster-recovery/failover/{restore_id}")
    async def api_disaster_recovery_failover(request: Request, restore_id: str) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=16_000)
        reason = str(payload.get("reason") or "network-unavailable")
        return json_response(backup_remote_restore.attempt_target_failover(restore_id, failure_reason=reason))

    @router.post("/api/workspace/disaster-recovery/drills")
    async def api_disaster_recovery_drill_create(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=16_000)
        if set(payload) != {"restoreId"}:
            raise AppError("Recovery Drill accepts only restoreId", code=ErrorCode.INVALID_PAYLOAD)
        return json_response(backup_recovery_drill.run_recovery_drill(str(payload.get("restoreId") or "")))

    @router.post("/api/workspace/disaster-recovery/drills/schedule/{policy_id}")
    async def api_disaster_recovery_drill_schedule(request: Request, policy_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response(backup_recovery_drill.execute_scheduled_drill(policy_id))

    @router.get("/api/workspace/disaster-recovery/drills/{restore_id}")
    async def api_disaster_recovery_drill_get(request: Request, restore_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response(backup_recovery_drill.get_recovery_drill(restore_id))

    @router.get("/api/workspace/backup-catalog")
    async def api_backup_catalog(request: Request) -> JSONResponse:
        require_api_auth(request)
        target_id = request.query_params.get("targetId") or "managed-local"
        session = open_target_session(target_id)
        policy_id = request.query_params.get("policyId") or None
        if session.root is not None:
            entries = backup_catalog.list_backups(session.root, policy_id=policy_id)
            return json_response(
                {
                    "backups": entries,
                    "chainValid": backup_catalog.verify_chain(session.root),
                    "integrity": backup_catalog.find_orphans_and_missing(session.root),
                    "health": backup_scrub.backup_health(session.root),
                    "targetKind": session.kind,
                }
            )
        state = backup_catalog.catalog_state_store(session.store)
        entries = backup_catalog.state_sorted(state.values())
        if policy_id:
            entries = [item for item in entries if item.get("policyId") == policy_id]
        return json_response(
            {
                "backups": entries,
                "chainValid": True,
                "integrity": {"orphans": [], "missing": []},
                "health": {"status": "ok", "backups": []},
                "targetKind": session.kind,
            }
        )

    @router.post("/api/workspace/backup-catalog/{backup_id}/pin")
    async def api_backup_pin(request: Request, backup_id: str) -> JSONResponse:
        require_api_auth(request)
        session, _record = _find_backup_session(backup_id)
        with _target_writer(session.root, session.target_id, store=session.store if session.root is None else None) as writer:
            if session.root is not None:
                backup_catalog.pin_backup(session.root, backup_id, True, writer=writer)
            else:
                backup_catalog._append_entry_store(session.store, "pin", {"backupId": backup_id, "pinned": True}, writer=writer)
        return json_response({"backupId": backup_id, "pinned": True})

    @router.delete("/api/workspace/backup-catalog/{backup_id}/pin")
    async def api_backup_unpin(request: Request, backup_id: str) -> JSONResponse:
        require_api_auth(request)
        session, _record = _find_backup_session(backup_id)
        with _target_writer(session.root, session.target_id, store=session.store if session.root is None else None) as writer:
            if session.root is not None:
                backup_catalog.pin_backup(session.root, backup_id, False, writer=writer)
            else:
                backup_catalog._append_entry_store(session.store, "pin", {"backupId": backup_id, "pinned": False}, writer=writer)
        return json_response({"backupId": backup_id, "pinned": False})

    # ── Retention ──────────────────────────────────────────────────────────

    @router.post("/api/workspace/retention/preview")
    async def api_retention_preview(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=64_000)
        policy = backup_policies.get_policy(str(payload.get("policyId") or ""))
        retention = backup_retention.get_retention_policy(str(policy.get("retentionPolicyId") or "default"))
        session = open_target_session(str(policy.get("targetId") or "managed-local"))
        timezone_name = str((policy.get("schedule") or {}).get("timezone") or "UTC")
        if session.root is not None:
            preview = backup_retention.preview_retention(retention, session.root, policy_timezone=timezone_name)
            preview.pop("trashRecords", None)
            return json_response(preview)
        state = backup_catalog.catalog_state_store(session.store)
        live = [item for item in state.values() if not item.get("deleted") and not item.get("trashed")]
        return json_response({"keep": [str(item.get("backupId")) for item in live[: int(retention.get("keepLast") or 0)]], "trash": [], "protected": [], "targetKind": session.kind})

    @router.post("/api/workspace/retention/apply")
    async def api_retention_apply(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=64_000)
        policy = backup_policies.get_policy(str(payload.get("policyId") or ""))
        retention = backup_retention.get_retention_policy(str(policy.get("retentionPolicyId") or "default"))
        timezone_name = str((policy.get("schedule") or {}).get("timezone") or "UTC")
        target_id = str(policy.get("targetId") or "managed-local")
        session = open_target_session(target_id, write_intent=True)
        preview = payload.get("preview")
        with _target_writer(session.root, target_id, store=session.store if session.root is None else None) as writer:
            if session.root is not None:
                applied = backup_retention.apply_retention(
                    retention,
                    session.root,
                    policy_timezone=timezone_name,
                    preview=preview if isinstance(preview, dict) else None,
                    writer=writer,
                )
                finalized = backup_retention.finalize_retention(retention, session.root, policy_timezone=timezone_name, writer=writer)
            else:
                applied = backup_retention.apply_retention_store(retention, session.store, policy_timezone=timezone_name, writer=writer)
                finalized = backup_retention.finalize_retention_store(retention, session.store, policy_timezone=timezone_name, writer=writer)
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

    @router.post("/api/workspace/restores/from-target/preview")
    async def api_restore_from_target_preview(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=64_000)
        return json_response(
            backup_remote_restore.preview_restore_from_target(
                target_id=str(payload.get("targetId") or ""),
                backup_id=str(payload.get("backupId") or ""),
                selection=payload.get("selection"),
                restore_id=str(payload.get("restoreId") or "") or None,
            )
        )

    @router.post("/api/workspace/restores/from-target")
    async def api_restore_from_target(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=64_000)
        complete = bool(payload.get("complete", False))
        if complete:
            return json_response(
                backup_remote_restore.restore_from_target(
                    target_id=str(payload.get("targetId") or ""),
                    backup_id=str(payload.get("backupId") or ""),
                )
            )
        return json_response(
            backup_remote_restore.create_restore_from_target(
                target_id=str(payload.get("targetId") or ""),
                backup_id=str(payload.get("backupId") or ""),
                selection=payload.get("selection"),
                restore_id=str(payload.get("restoreId") or "") or None,
            )
        )

    @router.post("/api/workspace/restores/{restore_id}/fetch")
    async def api_restore_fetch(request: Request, restore_id: str) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=64_000)
        max_bytes = payload.get("maxBytes")
        return json_response(
            backup_remote_restore.fetch_restore_session(
                restore_id,
                max_bytes=int(max_bytes) if max_bytes is not None else None,
            )
        )

    @router.post("/api/workspace/restores/{restore_id}/preflight")
    async def api_restore_preflight(request: Request, restore_id: str) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=64_000)
        if payload:
            raise AppError("Recovery preflight does not accept client capacity overrides", code=ErrorCode.INVALID_PAYLOAD)
        return json_response(backup_remote_restore.preflight_restore_session(restore_id))

    @router.post("/api/workspace/restores/{restore_id}/materialize")
    async def api_restore_materialize(request: Request, restore_id: str) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=64_000)
        return json_response(
            backup_remote_restore.materialize_federated_restore(
                restore_id,
                mode=str(payload.get("mode") or "merge"),
                previous_epoch=str(payload.get("previousEpoch") or "legacy"),
                target_epoch=str(payload.get("targetEpoch") or "") or None,
                owner_document_id=str(payload.get("ownerDocumentId") or "browser"),
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

    # ── 4.5.7 Target Drain Routes ───────────────────────────────────────────

    @router.post("/api/workspace/backup-targets/{target_id}/drain/cancel")
    async def api_target_drain_cancel(request: Request, target_id: str) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=16_000)
        from deepseek_infra.infra.workspace import backup_drain

        job = backup_drain.cancel_target_drain(
            target_id,
            reason=str(payload.get("reason") or "operator-cancelled"),
        )
        return json_response(job)

    @router.get("/api/workspace/backup-targets/{target_id}/drain")
    async def api_target_drain_get(request: Request, target_id: str) -> JSONResponse:
        require_api_auth(request)
        from deepseek_infra.infra.workspace import backup_drain

        job = backup_drain.get_target_drain_job(target_id)
        if job is None:
            raise AppError("No drain job for target", code=ErrorCode.NOT_FOUND, status=404)
        return json_response(job)

    # ── 4.5.7 Copy Retirement Routes ────────────────────────────────────────

    @router.post("/api/workspace/backup-retirements")
    async def api_backup_retirement_create(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=16_000)
        policy_id = str(payload.get("policyId") or "").strip()
        backup_id = str(payload.get("backupId") or "").strip()
        target_id = str(payload.get("targetId") or "").strip()
        if not policy_id or not backup_id or not target_id:
            raise AppError("policyId, backupId, and targetId are required", code=ErrorCode.INVALID_PAYLOAD, status=400)
        from deepseek_infra.infra.workspace import backup_retirement

        job = backup_retirement.create_copy_retirement_job(
            policy_id=policy_id,
            backup_id=backup_id,
            target_id=target_id,
            reason=str(payload.get("reason") or "api-retirement-request"),
        )
        return json_response(job)

    @router.get("/api/workspace/backup-retirements/{job_id}")
    async def api_backup_retirement_get(request: Request, job_id: str) -> JSONResponse:
        require_api_auth(request)
        from deepseek_infra.infra.workspace import backup_retirement

        job = backup_retirement.get_copy_retirement_job(job_id)
        if job is None:
            raise AppError("Retirement job not found", code=ErrorCode.NOT_FOUND, status=404)
        return json_response(job)

    @router.get("/api/workspace/backup-retirements")
    async def api_backup_retirements_list(request: Request) -> JSONResponse:
        require_api_auth(request)
        policy_id = request.query_params.get("policyId")
        target_id = request.query_params.get("targetId")
        phase = request.query_params.get("phase")
        from deepseek_infra.infra.workspace import backup_retirement

        jobs = backup_retirement.list_copy_retirement_jobs(policy_id=policy_id, target_id=target_id, phase=phase)
        return json_response({"jobs": jobs})

    # ── 4.5.7 Capacity & QoS Routes ─────────────────────────────────────────

    @router.get("/api/workspace/backup-capacity/summary")
    async def api_backup_capacity_summary(request: Request) -> JSONResponse:
        require_api_auth(request)
        from deepseek_infra.infra.workspace import backup_capacity

        return json_response(backup_capacity.capacity_summary())

    @router.get("/api/workspace/backup-transfer-budget")
    async def api_backup_transfer_budget_get(request: Request) -> JSONResponse:
        require_api_auth(request)
        from deepseek_infra.infra.workspace import backup_transfer_budget

        summary = backup_transfer_budget.get_global_transfer_budget_manager().transfer_control_summary()
        return json_response(summary)

    return router

"""Global Recovery Intelligence - Action Journal & Evidence Binding (4.7.2 Gates A, B, C, D, E, F, I, J, K, L).

Durable, transactional lifecycle journal for all autonomous and operator-guided
resilience actions. Guarantees:
1. Immutable create-once Plan and Action Identity (Gate A)
2. Crash-recoverable CAS claims with executionEpoch and renewable leases (Gate B)
3. Effect handle persistence & crash reconciliation (Gate C)
4. Subsystem action idempotency propagation (Gate D)
5. Precondition simulation before mutation (simulate_action)
6. Real post-condition outcome verification (Gate E)
7. Scoped closed-loop risk reduction verification (Gate F)
8. Transactional resource locks (Gate I)
9. Atomic global and per-target safety budgets (Gate J)
10. Blast-radius invariant enforcement (Gate K)
11. Effect-aware compensation and EFFECT_UNKNOWN handling (Gate L)
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import sys
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    resilience_outcome_verifier,
    resilience_resource_locks,
)

JOURNAL_DIR = config.ROOT / ".resilience-journal"
JOURNAL_DB = JOURNAL_DIR / "journal.sqlite3"

SCHEMA_INIT = """
CREATE TABLE IF NOT EXISTS resilience_plans (
    plan_id TEXT PRIMARY KEY,
    plan_version INTEGER NOT NULL,
    input_risk_digest TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    overall_risk TEXT NOT NULL,
    status TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resilience_actions (
    action_id TEXT PRIMARY KEY,
    plan_id TEXT,
    action_type TEXT NOT NULL,
    created_by TEXT NOT NULL,
    input_risk_digest TEXT,
    plan_digest TEXT,
    state TEXT NOT NULL,
    parameters_json TEXT,
    owner_instance_id TEXT,
    lease_until TEXT,
    claim_token TEXT,
    execution_epoch INTEGER NOT NULL DEFAULT 0,
    effect_class TEXT,
    compensation_state TEXT,
    effect_handle_json TEXT,
    risk_subject_json TEXT,
    expected_effect TEXT,
    severity_before TEXT,
    coordination_plan_id TEXT,
    execution_result_json TEXT,
    verification_result_json TEXT,
    decision_proof_json TEXT,
    risk_before_digest TEXT,
    risk_after_digest TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_resilience_actions_state ON resilience_actions(state);
CREATE INDEX IF NOT EXISTS idx_resilience_actions_type ON resilience_actions(action_type);
CREATE INDEX IF NOT EXISTS idx_resilience_actions_plan ON resilience_actions(plan_id);

CREATE TABLE IF NOT EXISTS resilience_action_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    state TEXT NOT NULL,
    owner_instance_id TEXT,
    execution_epoch INTEGER NOT NULL,
    claim_token_sha256 TEXT,
    effect_handle_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_resilience_action_events_action
ON resilience_action_events(action_id, event_id);
"""


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _migrate_schema(conn: sqlite3.Connection) -> None:
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(resilience_actions)").fetchall()}
    needed_cols = [
        ("owner_instance_id", "TEXT"),
        ("lease_until", "TEXT"),
        ("claim_token", "TEXT"),
        ("execution_epoch", "INTEGER NOT NULL DEFAULT 0"),
        ("effect_class", "TEXT"),
        ("compensation_state", "TEXT"),
        ("effect_handle_json", "TEXT"),
        ("risk_subject_json", "TEXT"),
        ("expected_effect", "TEXT"),
        ("severity_before", "TEXT"),
        ("coordination_plan_id", "TEXT"),
        ("risk_before_digest", "TEXT"),
        ("risk_after_digest", "TEXT"),
    ]
    for col_name, col_type in needed_cols:
        if col_name not in existing_cols:
            try:
                conn.execute(f"ALTER TABLE resilience_actions ADD COLUMN {col_name} {col_type};")
            except sqlite3.OperationalError:
                pass
    resilience_resource_locks.ensure_locks_schema(conn)


_JOURNAL_LOCK = threading.RLock()


@contextlib.contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    with _JOURNAL_LOCK:
        conn = sqlite3.connect(JOURNAL_DB, timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.executescript(SCHEMA_INIT)
        _migrate_schema(conn)
        try:
            yield conn
        finally:
            conn.close()


def _commit(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("COMMIT")
    except sqlite3.OperationalError:
        pass


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


def _append_action_event(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    event_type: str,
    state: str,
    owner_instance_id: str | None,
    execution_epoch: int,
    claim_token: str | None,
    effect_handle: dict[str, Any] | None,
    created_at: str,
) -> None:
    token_digest = hashlib.sha256(claim_token.encode("utf-8")).hexdigest() if claim_token else None
    conn.execute(
        """
        INSERT INTO resilience_action_events (
            action_id, event_type, state, owner_instance_id, execution_epoch,
            claim_token_sha256, effect_handle_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action_id,
            event_type,
            state,
            owner_instance_id,
            int(execution_epoch),
            token_digest,
            json.dumps(effect_handle, sort_keys=True) if effect_handle is not None else None,
            created_at,
        ),
    )


def materialize_resilience_plan(
    plan: dict[str, Any],
    *,
    created_by: str = "resilience-planner",
) -> dict[str, Any]:
    """Atomically validate and persist a ResiliencePlan and its Action Intents (Gate A & B)."""
    from deepseek_infra.infra.workspace import (
        autonomous_action_policy,
        resilience_planner,
    )

    plan_id = str(plan.get("planId") or f"plan_{uuid.uuid4().hex[:16]}")
    plan_version = int(plan.get("planVersion") or resilience_planner.RESILIENCE_PLAN_VERSION)
    input_risk_digest = str(plan.get("inputRiskDigest") or "")
    declared_plan_digest = str(plan.get("planDigest") or "")
    overall_risk = str(plan.get("overallRisk") or "healthy")
    actions = plan.get("actions", [])

    computed_digest = resilience_planner.compute_plan_digest(plan)
    if declared_plan_digest and declared_plan_digest != computed_digest:
        raise AppError(
            f"Plan digest mismatch: declared={declared_plan_digest}, computed={computed_digest}",
            code=ErrorCode.INVALID_REQUEST,
            status=400,
        )
    effective_plan_digest = computed_digest

    # Validate every action intent
    validated_actions: list[dict[str, Any]] = []
    now_iso = _utc_iso()

    for act in actions:
        is_valid, issues = resilience_planner.validate_action_intent(act)
        if not is_valid:
            raise AppError(
                f"Action '{act.get('actionId')}' has invalid parameters: {', '.join(issues)}",
                code=ErrorCode.INVALID_REQUEST,
                status=400,
            )
        act_id = str(act.get("actionId") or f"act_{uuid.uuid4().hex[:12]}")
        act_type = str(act.get("type", "")).upper()
        req_approval = bool(act.get("requiresApproval"))
        is_auto = autonomous_action_policy.is_action_autonomous(act_type)

        initial_state = "APPROVAL_REQUIRED" if (req_approval or not is_auto) else "PENDING"
        validated_actions.append(
            {
                "actionId": act_id,
                "planId": plan_id,
                "type": act_type,
                "createdBy": created_by,
                "inputRiskDigest": input_risk_digest,
                "planDigest": effective_plan_digest,
                "state": initial_state,
                "parameters": act.get("parameters", {}),
                "ownerInstanceId": None,
                "leaseUntil": None,
                "claimToken": None,
                "executionEpoch": 0,
                "effectClass": "NO_EFFECT",
                "compensationState": None,
                "effectHandle": None,
                "riskSubject": act.get("riskSubject"),
                "expectedEffect": act.get("expectedEffect", "severity-decrease"),
                "severityBefore": act.get("severityBefore"),
                "coordinationPlanId": act.get("coordinationPlanId"),
                "executionResult": None,
                "verificationResult": None,
                "decisionProof": None,
                "riskBeforeDigest": None,
                "riskAfterDigest": None,
                "error": None,
                "createdAt": now_iso,
                "updatedAt": now_iso,
            }
        )

    with _connect() as conn:
        # Gate A: Create-once immutable plan
        existing_plan_row = conn.execute(
            "SELECT plan_digest, plan_json, status FROM resilience_plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()

        if existing_plan_row:
            existing_digest = existing_plan_row[0]
            if existing_digest != effective_plan_digest:
                raise AppError(
                    f"Plan identity conflict: plan '{plan_id}' exists with different digest ({existing_digest} != {effective_plan_digest})",
                    code=ErrorCode.INVALID_REQUEST,
                    status=409,
                )
        else:
            conn.execute(
                """
                INSERT INTO resilience_plans (
                    plan_id, plan_version, input_risk_digest, plan_digest,
                    overall_risk, status, plan_json, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    plan_version,
                    input_risk_digest,
                    effective_plan_digest,
                    overall_risk,
                    str(plan.get("status") or "PROPOSED"),
                    json.dumps(plan),
                    created_by,
                    now_iso,
                    now_iso,
                ),
            )

        # Gate A: Create-once immutable actions
        persisted_actions: list[dict[str, Any]] = []
        for va in validated_actions:
            act_id = va["actionId"]
            existing_act_row = conn.execute(
                "SELECT plan_digest, state, parameters_json, execution_result_json, verification_result_json, decision_proof_json FROM resilience_actions WHERE action_id = ?",
                (act_id,),
            ).fetchone()

            if existing_act_row:
                existing_params = json.loads(existing_act_row[2]) if existing_act_row[2] else {}
                # Check for conflict
                if existing_params != va["parameters"]:
                    raise AppError(
                        f"Action identity conflict: action '{act_id}' exists with different parameters",
                        code=ErrorCode.INVALID_REQUEST,
                        status=409,
                    )
                # Idempotent replay: return existing action without resetting state
                existing_act = get_action(act_id)
                if existing_act:
                    persisted_actions.append(existing_act)
            else:
                conn.execute(
                    """
                    INSERT INTO resilience_actions (
                        action_id, plan_id, action_type, created_by, input_risk_digest, plan_digest,
                        state, parameters_json, owner_instance_id, lease_until, claim_token, execution_epoch,
                        effect_class, compensation_state, effect_handle_json, risk_subject_json, expected_effect,
                        severity_before, coordination_plan_id, execution_result_json, verification_result_json,
                        decision_proof_json, risk_before_digest, risk_after_digest, error_message,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        act_id,
                        va["planId"],
                        va["type"],
                        va["createdBy"],
                        va["inputRiskDigest"],
                        va["planDigest"],
                        va["state"],
                        json.dumps(va["parameters"]),
                        None,
                        None,
                        None,
                        0,
                        "NO_EFFECT",
                        None,
                        None,
                        json.dumps(va["riskSubject"]) if va["riskSubject"] else None,
                        va["expectedEffect"],
                        va["severityBefore"],
                        va["coordinationPlanId"],
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        now_iso,
                        now_iso,
                    ),
                )
                persisted_actions.append(va)
        _commit(conn)

    return {
        "planId": plan_id,
        "planVersion": plan_version,
        "inputRiskDigest": input_risk_digest,
        "planDigest": effective_plan_digest,
        "overallRisk": overall_risk,
        "status": "MATERIALIZED",
        "actions": persisted_actions,
        "createdAt": now_iso,
    }


def record_action_intent(
    action: dict[str, Any],
    *,
    created_by: str = "resilience-engine",
    plan_id: str | None = None,
    input_risk_digest: str = "",
    plan_digest: str = "",
) -> dict[str, Any]:
    """Record an individual action intent into the durable journal with create-once semantics (Gate A)."""
    action_id = str(action.get("actionId") or f"act_{uuid.uuid4().hex[:12]}")
    action_type = str(action.get("type", "")).upper()
    req_approval = bool(action.get("requiresApproval"))

    from deepseek_infra.infra.workspace import autonomous_action_policy

    is_auto = autonomous_action_policy.is_action_autonomous(action_type)
    initial_state = "APPROVAL_REQUIRED" if (req_approval or not is_auto) else "PENDING"
    now_iso = _utc_iso()

    params = action.get("parameters", {})
    record = {
        "actionId": action_id,
        "planId": plan_id or action.get("planId"),
        "type": action_type,
        "createdBy": created_by,
        "inputRiskDigest": input_risk_digest,
        "planDigest": plan_digest,
        "state": initial_state,
        "parameters": params,
        "ownerInstanceId": None,
        "leaseUntil": None,
        "claimToken": None,
        "executionEpoch": 0,
        "effectClass": "NO_EFFECT",
        "compensationState": None,
        "effectHandle": None,
        "riskSubject": action.get("riskSubject"),
        "expectedEffect": action.get("expectedEffect", "severity-decrease"),
        "severityBefore": action.get("severityBefore"),
        "coordinationPlanId": action.get("coordinationPlanId"),
        "executionResult": None,
        "verificationResult": None,
        "decisionProof": None,
        "riskBeforeDigest": None,
        "riskAfterDigest": None,
        "error": None,
        "createdAt": now_iso,
        "updatedAt": now_iso,
    }

    with _connect() as conn:
        existing = conn.execute(
            "SELECT action_id, parameters_json FROM resilience_actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()

        if existing:
            existing_params = json.loads(existing[1]) if existing[1] else {}
            if existing_params != params:
                raise AppError(
                    f"Action identity conflict: action '{action_id}' exists with different parameters",
                    code=ErrorCode.INVALID_REQUEST,
                    status=409,
                )
            return get_action(action_id) or record

        conn.execute(
            """
            INSERT INTO resilience_actions (
                action_id, plan_id, action_type, created_by, input_risk_digest, plan_digest,
                state, parameters_json, owner_instance_id, lease_until, claim_token, execution_epoch,
                effect_class, compensation_state, effect_handle_json, risk_subject_json, expected_effect,
                severity_before, coordination_plan_id, execution_result_json, verification_result_json,
                decision_proof_json, risk_before_digest, risk_after_digest, error_message,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                record["planId"],
                action_type,
                created_by,
                input_risk_digest,
                plan_digest,
                initial_state,
                json.dumps(params),
                None,
                None,
                None,
                0,
                "NO_EFFECT",
                None,
                None,
                json.dumps(record["riskSubject"]) if record["riskSubject"] else None,
                record["expectedEffect"],
                record["severityBefore"],
                record["coordinationPlanId"],
                None,
                None,
                None,
                None,
                None,
                None,
                now_iso,
                now_iso,
            ),
        )
        _commit(conn)

    return record


def update_action_state(
    action_id: str,
    state: str,
    *,
    execution_epoch: int | None = None,
    claim_token: str | None = None,
    owner_instance_id: str | None = None,
    lease_until: str | None = None,
    effect_class: str | None = None,
    compensation_state: str | None = None,
    effect_handle: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    proof: dict[str, Any] | None = None,
    risk_before_digest: str | None = None,
    risk_after_digest: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Update execution state with CAS fencing on execution_epoch and claim_token (Gate B)."""
    now_iso = _utc_iso()
    terminal_states = {
        "SUCCEEDED",
        "COMPENSATED",
        "COMPENSATION_REQUIRED",
        "FAILED_BEFORE_EFFECT",
        "NEEDS_OPERATOR",
        "EFFECT_UNKNOWN",
        "BLOCKED",
        "SKIPPED_NO_LONGER_NEEDED",
        "PREEMPTED",
    }

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT action_id, plan_id, action_type, created_by, input_risk_digest, plan_digest,
                   state, parameters_json, owner_instance_id, lease_until, claim_token, execution_epoch,
                   effect_class, compensation_state, effect_handle_json, risk_subject_json, expected_effect,
                   severity_before, coordination_plan_id, execution_result_json, verification_result_json,
                   decision_proof_json, risk_before_digest, risk_after_digest, error_message,
                   created_at, updated_at
            FROM resilience_actions WHERE action_id = ?
            """,
            (action_id,),
        ).fetchone()

        if not row:
            raise AppError(f"Action '{action_id}' not found in journal", code=ErrorCode.NOT_FOUND, status=404)

        cur_result = json.loads(row[19]) if row[19] else None
        cur_verif = json.loads(row[20]) if row[20] else None
        cur_proof = json.loads(row[21]) if row[21] else None
        cur_handle = json.loads(row[14]) if row[14] else None

        new_result = result if result is not None else cur_result
        new_verif = verification if verification is not None else cur_verif
        new_proof = proof if proof is not None else cur_proof
        new_handle = effect_handle if effect_handle is not None else cur_handle

        # If execution_epoch and claim_token are provided, perform strict CAS update
        if execution_epoch is not None and claim_token is not None:
            cursor = conn.execute(
                """
                UPDATE resilience_actions SET
                    state = ?,
                    owner_instance_id = COALESCE(?, owner_instance_id),
                    lease_until = COALESCE(?, lease_until),
                    effect_class = COALESCE(?, effect_class),
                    compensation_state = COALESCE(?, compensation_state),
                    effect_handle_json = ?,
                    execution_result_json = ?,
                    verification_result_json = ?,
                    decision_proof_json = ?,
                    risk_before_digest = COALESCE(?, risk_before_digest),
                    risk_after_digest = COALESCE(?, risk_after_digest),
                    error_message = ?,
                    updated_at = ?
                WHERE action_id = ?
                  AND execution_epoch = ?
                  AND claim_token = ?
                """,
                (
                    state,
                    owner_instance_id,
                    lease_until,
                    effect_class,
                    compensation_state,
                    json.dumps(new_handle) if new_handle is not None else None,
                    json.dumps(new_result) if new_result is not None else None,
                    json.dumps(new_verif) if new_verif is not None else None,
                    json.dumps(new_proof) if new_proof is not None else None,
                    risk_before_digest,
                    risk_after_digest,
                    error,
                    now_iso,
                    action_id,
                    execution_epoch,
                    claim_token,
                ),
            )
            if cursor.rowcount == 0:
                raise AppError(
                    f"Action '{action_id}' lease lost (CAS epoch {execution_epoch} token mismatch): stale worker cannot commit",
                    code=ErrorCode.FORBIDDEN,
                    status=409,
                )
        else:
            conn.execute(
                """
                UPDATE resilience_actions SET
                    state = ?,
                    owner_instance_id = COALESCE(?, owner_instance_id),
                    lease_until = COALESCE(?, lease_until),
                    claim_token = COALESCE(?, claim_token),
                    effect_class = COALESCE(?, effect_class),
                    compensation_state = COALESCE(?, compensation_state),
                    effect_handle_json = ?,
                    execution_result_json = ?,
                    verification_result_json = ?,
                    decision_proof_json = ?,
                    risk_before_digest = COALESCE(?, risk_before_digest),
                    risk_after_digest = COALESCE(?, risk_after_digest),
                    error_message = ?,
                    updated_at = ?
                WHERE action_id = ?
                """,
                (
                    state,
                    owner_instance_id,
                    lease_until,
                    claim_token,
                    effect_class,
                    compensation_state,
                    json.dumps(new_handle) if new_handle is not None else None,
                    json.dumps(new_result) if new_result is not None else None,
                    json.dumps(new_verif) if new_verif is not None else None,
                    json.dumps(new_proof) if new_proof is not None else None,
                    risk_before_digest,
                    risk_after_digest,
                    error,
                    now_iso,
                    action_id,
                ),
            )

        # Release resource locks upon terminal state
        if state in terminal_states:
            resilience_resource_locks.release_action_locks(conn, action_id)

        _append_action_event(
            conn,
            action_id=action_id,
            event_type="STATE_TRANSITION",
            state=state,
            owner_instance_id=owner_instance_id or (str(row[8]) if row[8] else None),
            execution_epoch=int(execution_epoch if execution_epoch is not None else row[11] or 0),
            claim_token=claim_token or (str(row[10]) if row[10] else None),
            effect_handle=new_handle,
            created_at=now_iso,
        )

        _commit(conn)

    return get_action(action_id) or {}


def claim_action(
    action_id: str,
    *,
    owner_instance_id: str = "resilience-worker",
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> tuple[bool, dict[str, Any] | None, str]:
    """Compatibility claim API; production execution uses atomic admission."""
    return admit_and_claim_action(
        action_id,
        owner_instance_id=owner_instance_id,
        lease_seconds=lease_seconds,
        now=now,
        enforce_budgets=False,
    )


def admit_and_claim_action(
    action_id: str,
    *,
    owner_instance_id: str = "resilience-worker",
    lease_seconds: int = 60,
    now: datetime | None = None,
    enforce_budgets: bool = True,
) -> tuple[bool, dict[str, Any] | None, str]:
    """Atomically verify budgets, acquire locks, and claim a new execution epoch."""
    current = now or datetime.now(tz=timezone.utc)
    now_iso = _utc_iso(current)
    lease_until_iso = _utc_iso(current + timedelta(seconds=lease_seconds))
    claim_token = uuid.uuid4().hex

    action = get_action(action_id)
    if not action:
        return False, None, "action-not-found"

    lock_keys = resilience_resource_locks.derive_resource_locks_for_action(action)

    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current_row = conn.execute(
            "SELECT state, lease_until FROM resilience_actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if not current_row:
            _rollback(conn)
            return False, None, "action-not-found"
        current_state = str(current_row[0] or "")
        current_lease = str(current_row[1] or "")
        active_states = {"CLAIMED", "EXECUTING", "RECONCILING", "VERIFYING", "ASSESSING_EFFECT"}
        claimable = current_state == "PENDING" or (current_state in active_states and current_lease < now_iso)
        if not claimable:
            _rollback(conn)
            return False, get_action(action_id), "claim-rejected-not-pending-or-active-lease"
        claimed_state = "CLAIMED" if current_state == "PENDING" else "RECONCILING"

        if enforce_budgets:
            rate_ok, rate_reason = check_rate_limits(
                conn,
                action,
                exclude_action_id=action_id,
                now=current,
                commit_mutations=False,
            )
            if not rate_ok:
                _rollback(conn)
                return False, get_action(action_id), rate_reason

        if lock_keys:
            acquired, lock_reason = resilience_resource_locks.acquire_action_locks(
                conn,
                action_id,
                lock_keys,
                owner_instance_id=owner_instance_id,
                lease_until=lease_until_iso,
                now=current,
            )
            if not acquired:
                _rollback(conn)
                return False, get_action(action_id), lock_reason

        cursor = conn.execute(
            """
            UPDATE resilience_actions
            SET state = ?,
                owner_instance_id = ?,
                lease_until = ?,
                claim_token = ?,
                execution_epoch = execution_epoch + 1,
                updated_at = ?
            WHERE action_id = ?
              AND (
                  state = 'PENDING'
                  OR (state IN ('CLAIMED', 'EXECUTING', 'RECONCILING', 'VERIFYING', 'ASSESSING_EFFECT') AND lease_until < ?)
              )
            """,
            (
                claimed_state,
                owner_instance_id,
                lease_until_iso,
                claim_token,
                now_iso,
                action_id,
                now_iso,
            ),
        )
        if cursor.rowcount != 1:
            _rollback(conn)
            return False, get_action(action_id), "claim-rejected-not-pending-or-active-lease"
        _append_action_event(
            conn,
            action_id=action_id,
            event_type="ACTION_CLAIMED" if claimed_state == "CLAIMED" else "ACTION_TAKEOVER",
            state=claimed_state,
            owner_instance_id=owner_instance_id,
            execution_epoch=int(action.get("executionEpoch") or 0) + 1,
            claim_token=claim_token,
            effect_handle=action.get("effectHandle") if isinstance(action.get("effectHandle"), dict) else None,
            created_at=now_iso,
        )
        _commit(conn)

    return True, get_action(action_id), "admitted-and-claimed" if enforce_budgets else "claimed"


def renew_action_lease(
    action_id: str,
    execution_epoch: int,
    claim_token: str,
    *,
    owner_instance_id: str | None = None,
    lease_seconds: int = 120,
    now: datetime | None = None,
) -> bool:
    """CAS-renew an active action lease and all of its resource locks."""
    current = now or datetime.now(tz=timezone.utc)
    lease_until_iso = _utc_iso(current + timedelta(seconds=max(1, lease_seconds)))
    now_iso = _utc_iso(current)
    action = get_action(action_id)
    if not action:
        return False
    expected_lock_count = len(resilience_resource_locks.derive_resource_locks_for_action(action))
    effective_owner = str(owner_instance_id or action.get("ownerInstanceId") or "resilience-worker")

    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE resilience_actions
            SET lease_until = ?, owner_instance_id = ?, updated_at = ?
            WHERE action_id = ?
              AND execution_epoch = ?
              AND claim_token = ?
              AND state IN ('CLAIMED', 'EXECUTING', 'RECONCILING', 'VERIFYING', 'ASSESSING_EFFECT')
            """,
            (
                lease_until_iso,
                effective_owner,
                now_iso,
                action_id,
                execution_epoch,
                claim_token,
            ),
        )
        if cursor.rowcount != 1:
            _rollback(conn)
            return False
        renewed_locks = resilience_resource_locks.renew_action_locks(
            conn,
            action_id,
            owner_instance_id=effective_owner,
            lease_until=lease_until_iso,
        )
        if renewed_locks != expected_lock_count:
            _rollback(conn)
            return False
        _commit(conn)
    return True


def _run_with_action_lease_heartbeat(
    *,
    action_id: str,
    execution_epoch: int,
    claim_token: str,
    lease_seconds: int,
    operation_name: str,
    operation: Callable[[], Any],
    heartbeat_interval_seconds: float | None = None,
) -> Any:
    """Run a blocking operation while periodically renewing its fenced lease."""
    interval = heartbeat_interval_seconds
    if interval is None:
        interval = min(30.0, max(0.1, float(lease_seconds) / 3.0))
    if not renew_action_lease(action_id, execution_epoch, claim_token, lease_seconds=lease_seconds):
        raise AppError(
            f"Action '{action_id}' lease lost before {operation_name}",
            code=ErrorCode.FORBIDDEN,
            status=409,
        )

    stop = threading.Event()
    lease_lost = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(interval):
            if not renew_action_lease(action_id, execution_epoch, claim_token, lease_seconds=lease_seconds):
                lease_lost.set()
                return

    heartbeat_thread = threading.Thread(
        target=heartbeat,
        name=f"resilience-lease-{action_id[:24]}",
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        result = operation()
    finally:
        stop.set()
        heartbeat_thread.join(timeout=max(1.0, interval * 2.0))

    if lease_lost.is_set():
        raise AppError(
            f"Action '{action_id}' lease lost during {operation_name}; stale worker result fenced",
            code=ErrorCode.FORBIDDEN,
            status=409,
        )
    return result


def check_rate_limits(
    conn: sqlite3.Connection,
    action: dict[str, Any] | None = None,
    *,
    exclude_action_id: str | None = None,
    now: datetime | None = None,
    commit_mutations: bool = True,
) -> tuple[bool, str]:
    """Check active concurrent and hourly rate limits with atomic budget enforcement (Gate J)."""
    from deepseek_infra.infra.workspace import autonomous_action_policy

    limits = autonomous_action_policy.get_action_rate_limits()

    # 1. Global concurrent running actions
    excluded = str(exclude_action_id or "")
    row = conn.execute(
        """
        SELECT COUNT(*) FROM resilience_actions
        WHERE state IN ('CLAIMED', 'EXECUTING', 'RECONCILING', 'VERIFYING', 'ASSESSING_EFFECT')
          AND action_id != ?
        """,
        (excluded,),
    ).fetchone()
    active_count = int(row[0]) if row else 0

    if active_count >= limits["maxConcurrentActions"]:
        # Check if action is CRITICAL repair that can preempt a WARNING rebalance
        act_sev = str(action.get("severity") or action.get("severityBefore") or "").lower() if action else ""
        if action and str(action.get("type")) == "CREATE_REPAIR_JOB" and act_sev == "critical":
            # Attempt to preempt an active warning rebalance
            warning_reb = conn.execute(
                """
                SELECT action_id FROM resilience_actions
                WHERE action_type = 'CREATE_REBALANCE_JOB'
                  AND state IN ('CLAIMED', 'PENDING')
                ORDER BY created_at ASC LIMIT 1
                """
            ).fetchone()
            if warning_reb:
                preempt_id = warning_reb[0]
                conn.execute(
                    """
                    UPDATE resilience_actions
                    SET state = 'PREEMPTED', error_message = 'preempted-by-critical-repair', updated_at = ?
                    WHERE action_id = ?
                    """,
                    (_utc_iso(), preempt_id),
                )
                resilience_resource_locks.release_action_locks(conn, str(preempt_id))
                if commit_mutations:
                    _commit(conn)
                return True, "admitted-with-preemption"
        return False, f"max-concurrent-actions-exceeded:{active_count}>={limits['maxConcurrentActions']}"

    # 2. Per-target concurrent limit
    if action:
        raw_params = action.get("parameters")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        targets = [
            str(params.get("sourceTargetId") or action.get("source") or ""),
            str(params.get("destTargetId") or action.get("destination") or action.get("target") or ""),
        ]
        for tid in targets:
            if not tid:
                continue
            rows = conn.execute(
                """
                SELECT parameters_json FROM resilience_actions
                WHERE state IN ('CLAIMED', 'EXECUTING', 'RECONCILING', 'VERIFYING', 'ASSESSING_EFFECT')
                  AND action_id != ?
                """,
                (excluded,),
            ).fetchall()
            target_active = 0
            for r in rows:
                p = json.loads(r[0]) if r[0] else {}
                if p.get("sourceTargetId") == tid or p.get("destTargetId") == tid or p.get("targetId") == tid:
                    target_active += 1
            target_limit = int(limits.get("maxConcurrentPerTarget", 2))
            if target_active >= target_limit:
                return False, f"max-per-target-concurrent-actions-exceeded:{tid}:{target_active}>={target_limit}"

        policy_id = str(params.get("policyId") or action.get("policyId") or "")
        if policy_id:
            rows = conn.execute(
                """
                SELECT parameters_json FROM resilience_actions
                WHERE state IN ('CLAIMED', 'EXECUTING', 'RECONCILING', 'VERIFYING', 'ASSESSING_EFFECT')
                  AND action_id != ?
                """,
                (excluded,),
            ).fetchall()
            policy_active = sum(
                1
                for row_item in rows
                if str((json.loads(row_item[0]) if row_item[0] else {}).get("policyId") or "") == policy_id
            )
            policy_limit = int(limits.get("maxConcurrentPerPolicy", 2))
            if policy_active >= policy_limit:
                return False, f"max-per-policy-concurrent-actions-exceeded:{policy_id}:{policy_active}>={policy_limit}"

        raw_subject = action.get("riskSubject")
        subject = raw_subject if isinstance(raw_subject, dict) else {}
        failure_domain = str(params.get("failureDomain") or subject.get("failureDomain") or "")
        if failure_domain:
            rows = conn.execute(
                """
                SELECT parameters_json, risk_subject_json FROM resilience_actions
                WHERE state IN ('CLAIMED', 'EXECUTING', 'RECONCILING', 'VERIFYING', 'ASSESSING_EFFECT')
                  AND action_id != ?
                """,
                (excluded,),
            ).fetchall()
            active_domains: set[str] = set()
            for params_json, subject_json in rows:
                active_params = json.loads(params_json) if params_json else {}
                active_subject = json.loads(subject_json) if subject_json else {}
                active_domain = str(active_params.get("failureDomain") or active_subject.get("failureDomain") or "")
                if active_domain:
                    active_domains.add(active_domain)
            domain_limit = int(limits.get("maxSimultaneousFailureDomainsTouched", 1))
            if failure_domain not in active_domains and len(active_domains) >= domain_limit:
                return False, f"max-failure-domains-touched-exceeded:{failure_domain}:{len(active_domains)}>={domain_limit}"

    # 3. Hourly action throughput
    one_hour_ago = _utc_iso((now or datetime.now(tz=timezone.utc)) - timedelta(hours=1))
    row_hr = conn.execute(
        """
        SELECT COUNT(*) FROM resilience_actions
        WHERE created_at >= ?
          AND action_id != ?
          AND state NOT IN ('BLOCKED', 'STALE', 'SKIPPED_NO_LONGER_NEEDED', 'PREEMPTED')
        """,
        (one_hour_ago, excluded),
    ).fetchone()
    hourly_count = int(row_hr[0]) if row_hr else 0
    if hourly_count >= limits["maxActionsPerHour"]:
        return False, f"max-actions-per-hour-exceeded:{hourly_count}>={limits['maxActionsPerHour']}"

    return True, "admitted"


def check_action_freshness(action: dict[str, Any], fresh_risk_snapshot: dict[str, Any]) -> tuple[bool, str]:
    """Perform TOCTOU fresh risk evaluation before execution (Gate C)."""
    from deepseek_infra.infra.workspace import backup_targets

    act_type = str(action.get("type", "")).upper()
    params = action.get("parameters", {})
    risks = fresh_risk_snapshot.get("risks", [])

    if act_type == "CREATE_REPAIR_JOB":
        pid = str(params.get("policyId") or action.get("policyId") or "")
        matching = [
            r
            for r in risks
            if str(r.get("policyId")) == pid
            and str(r.get("type")) in {"REPLICA_LAG", "FAILURE_DOMAIN_VIOLATION"}
            and str(r.get("severity")).lower() not in {"healthy", "low"}
        ]
        if not matching:
            return False, "replica-risk-already-cleared"

    elif act_type == "CREATE_REBALANCE_JOB":
        src = str(params.get("sourceTargetId") or action.get("source") or action.get("target") or "")
        dst = str(params.get("destTargetId") or action.get("destination") or "")
        matching = [
            r
            for r in risks
            if str(r.get("target")) == src
            and str(r.get("type")) == "CAPACITY_EXHAUSTION"
            and str(r.get("severity")).lower() not in {"healthy", "low"}
        ]
        if not matching:
            return False, "capacity-risk-already-cleared"
        # Check if destination became draining or unconfigured
        dst_target = backup_targets.get_target(dst)
        if not dst_target or str(dst_target.get("status", "")).lower() == "draining":
            return False, "destination-target-draining-or-unavailable"

    elif act_type == "START_DR_DRILL":
        dr_risk = next((r for r in risks if str(r.get("type")) == "DR_STALENESS"), None)
        if not dr_risk or str(dr_risk.get("severity")).lower() in {"healthy", "low"}:
            return False, "dr-staleness-already-cleared"

    return True, "fresh"


def simulate_action(action: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Simulate action preconditions before execution (Gate H & K)."""
    from deepseek_infra.infra.workspace import (
        backup_capacity,
        backup_dr_ledger,
        backup_policies,
        backup_targets,
    )

    act_type = str(action.get("type", "")).upper()
    params = action.get("parameters", {})

    if act_type == "CREATE_REBALANCE_JOB":
        src = str(params.get("sourceTargetId") or action.get("source") or action.get("target") or "")
        dst = str(params.get("destTargetId") or action.get("destination") or "")
        if not src or not dst or src == dst:
            return False, {"simulationPassed": False, "error": "source-destination-identical-or-empty"}

        try:
            dst_target = backup_targets.get_target(dst)
        except Exception:
            dst_target = None
        if not dst_target or str(dst_target.get("status", "")).lower() == "draining":
            return False, {"simulationPassed": False, "error": f"destination-target-invalid-or-draining:{dst}"}

        cap = backup_capacity.get_target_capacity(dst, probe=False)
        free_pct = cap.get("freePercent")
        if free_pct is not None and free_pct <= 20.0:
            return False, {"simulationPassed": False, "error": f"destination-capacity-watermark-insufficient:{free_pct}%<=20%"}

        return True, {"simulationPassed": True, "source": src, "destination": dst, "destinationFreePercent": free_pct}

    elif act_type == "CREATE_REPAIR_JOB":
        pid = str(params.get("policyId") or action.get("policyId") or "")
        bid = str(params.get("backupId") or action.get("backupId") or "")
        dst = str(params.get("destTargetId") or action.get("destination") or action.get("target") or "")

        if not pid or not bid:
            return False, {"simulationPassed": False, "error": "repair-policy-or-backup-missing"}

        try:
            policy = backup_policies.get_policy(pid)
        except Exception:
            policy = None
        if not policy:
            return False, {"simulationPassed": False, "error": f"policy-not-found:{pid}"}

        pt = backup_dr_ledger.get_recovery_point(pid, bid)
        if not pt:
            return False, {"simulationPassed": False, "error": f"recovery-point-not-found:{pid}:{bid}"}

        if dst:
            try:
                dst_target = backup_targets.get_target(dst)
            except Exception:
                dst_target = None
            if not dst_target or str(dst_target.get("status", "")).lower() == "draining":
                return False, {"simulationPassed": False, "error": f"repair-destination-target-invalid:{dst}"}

        return True, {"simulationPassed": True, "policyId": pid, "backupId": bid, "destTargetId": dst}

    elif act_type == "START_DR_DRILL":
        return True, {"simulationPassed": True}

    return False, {"simulationPassed": False, "error": f"unsupported-simulation-type:{act_type}"}


def compensate_action(
    action_id: str,
    error_msg: str,
    *,
    effect_class: str = "NO_EFFECT",
    execution_epoch: int | None = None,
    claim_token: str | None = None,
) -> dict[str, Any]:
    """Execute typed compensation and transition to exact compensation states (Gate L)."""
    action = get_action(action_id)
    if not action:
        raise AppError(f"Action '{action_id}' not found", code=ErrorCode.NOT_FOUND, status=404)

    if (execution_epoch is None) != (claim_token is None):
        raise AppError(
            "Compensation fencing requires both execution_epoch and claim_token",
            code=ErrorCode.INVALID_REQUEST,
            status=400,
        )
    if execution_epoch is not None and claim_token is not None:
        if int(action.get("executionEpoch") or 0) != execution_epoch or str(action.get("claimToken") or "") != claim_token:
            raise AppError(
                f"Action '{action_id}' lease lost before compensation",
                code=ErrorCode.FORBIDDEN,
                status=409,
            )

    if effect_class == "NO_EFFECT":
        target_state = "FAILED_BEFORE_EFFECT"
        comp_state = "NONE"
        compensated_handle = None
    elif effect_class == "CANCELABLE":
        from deepseek_infra.infra.workspace import backup_replication

        raw_handle = action.get("effectHandle")
        handle = raw_handle if isinstance(raw_handle, dict) else {}
        kind = str(handle.get("kind") or "")
        if kind == "repair" and handle.get("repairId"):
            cancellation = backup_replication.cancel_repair_job(str(handle["repairId"]), reason=error_msg)
        elif kind == "rebalance" and handle.get("jobId"):
            cancellation = backup_replication.cancel_rebalance_job(str(handle["jobId"]), reason=error_msg)
        else:
            cancellation = {"status": "unknown", "reason": "cancelable-effect-handle-missing-or-unsupported"}

        cancellation_status = str(cancellation.get("status") or "unknown")
        if cancellation_status == "cancelled":
            target_state = "COMPENSATED"
            comp_state = "JOB_CANCELLED"
        elif cancellation_status == "not-cancelable":
            target_state = "COMPENSATION_REQUIRED"
            comp_state = "JOB_NOT_CANCELABLE"
        else:
            target_state = "EFFECT_UNKNOWN"
            comp_state = "REMOTE_EFFECT_UNCERTAIN"
        compensated_handle = dict(handle)
        compensated_handle["cancellationResult"] = {
            key: cancellation[key]
            for key in ("status", "phase", "reason", "repairId", "jobId")
            if key in cancellation
        }
    elif effect_class == "COMPENSATABLE":
        target_state = "COMPENSATION_REQUIRED"
        comp_state = "COMPENSATOR_NOT_IMPLEMENTED"
        compensated_handle = None
    elif effect_class == "EFFECT_UNKNOWN":
        target_state = "EFFECT_UNKNOWN"
        comp_state = "REMOTE_EFFECT_UNCERTAIN"
        compensated_handle = None
    else:
        target_state = "NEEDS_OPERATOR"
        comp_state = "MANUAL_INTERVENTION_REQUIRED"
        compensated_handle = None

    return update_action_state(
        action_id,
        target_state,
        execution_epoch=execution_epoch,
        claim_token=claim_token,
        effect_class=effect_class,
        compensation_state=comp_state,
        effect_handle=compensated_handle,
        error=error_msg,
    )


def rollback_action(action_id: str, reason: str = "") -> dict[str, Any]:
    """Compatibility rollback method mapping to safe compensation semantics."""
    return compensate_action(action_id, reason or "Action rolled back", effect_class="NO_EFFECT")


def get_action(action_id: str) -> dict[str, Any] | None:
    """Retrieve a single action journal record."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT action_id, plan_id, action_type, created_by, input_risk_digest, plan_digest,
                   state, parameters_json, owner_instance_id, lease_until, claim_token, execution_epoch,
                   effect_class, compensation_state, effect_handle_json, risk_subject_json, expected_effect,
                   severity_before, coordination_plan_id, execution_result_json, verification_result_json,
                   decision_proof_json, risk_before_digest, risk_after_digest, error_message,
                   created_at, updated_at
            FROM resilience_actions WHERE action_id = ?
            """,
            (action_id,),
        ).fetchone()

        if not row:
            return None

        return {
            "actionId": row[0],
            "planId": row[1],
            "type": row[2],
            "createdBy": row[3],
            "inputRiskDigest": row[4],
            "planDigest": row[5],
            "state": row[6],
            "parameters": json.loads(row[7]) if row[7] else {},
            "ownerInstanceId": row[8],
            "leaseUntil": row[9],
            "claimToken": row[10],
            "executionEpoch": int(row[11] or 0),
            "effectClass": row[12],
            "compensationState": row[13],
            "effectHandle": json.loads(row[14]) if row[14] else None,
            "riskSubject": json.loads(row[15]) if row[15] else None,
            "expectedEffect": row[16],
            "severityBefore": row[17],
            "coordinationPlanId": row[18],
            "executionResult": json.loads(row[19]) if row[19] else None,
            "verificationResult": json.loads(row[20]) if row[20] else None,
            "decisionProof": json.loads(row[21]) if row[21] else None,
            "riskBeforeDigest": row[22],
            "riskAfterDigest": row[23],
            "error": row[24],
            "createdAt": row[25],
            "updatedAt": row[26],
        }


def list_actions(
    *,
    state: str | None = None,
    action_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List historical action journal entries with optional filters."""
    query = (
        """
        SELECT action_id, plan_id, action_type, created_by, input_risk_digest, plan_digest,
               state, parameters_json, owner_instance_id, lease_until, claim_token, execution_epoch,
               effect_class, compensation_state, effect_handle_json, risk_subject_json, expected_effect,
               severity_before, coordination_plan_id, execution_result_json, verification_result_json,
               decision_proof_json, risk_before_digest, risk_after_digest, error_message,
               created_at, updated_at
        FROM resilience_actions
        """
    )
    clauses: list[str] = []
    params: list[Any] = []

    if state:
        clauses.append("state = ?")
        params.append(state)
    if action_type:
        clauses.append("action_type = ?")
        params.append(action_type.upper())

    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    results: list[dict[str, Any]] = []
    with _connect() as conn:
        for row in conn.execute(query, params).fetchall():
            results.append(
                {
                    "actionId": row[0],
                    "planId": row[1],
                    "type": row[2],
                    "createdBy": row[3],
                    "inputRiskDigest": row[4],
                    "planDigest": row[5],
                    "state": row[6],
                    "parameters": json.loads(row[7]) if row[7] else {},
                    "ownerInstanceId": row[8],
                    "leaseUntil": row[9],
                    "claimToken": row[10],
                    "executionEpoch": int(row[11] or 0),
                    "effectClass": row[12],
                    "compensationState": row[13],
                    "effectHandle": json.loads(row[14]) if row[14] else None,
                    "riskSubject": json.loads(row[15]) if row[15] else None,
                    "expectedEffect": row[16],
                    "severityBefore": row[17],
                    "coordinationPlanId": row[18],
                    "executionResult": json.loads(row[19]) if row[19] else None,
                    "verificationResult": json.loads(row[20]) if row[20] else None,
                    "decisionProof": json.loads(row[21]) if row[21] else None,
                    "riskBeforeDigest": row[22],
                    "riskAfterDigest": row[23],
                    "error": row[24],
                    "createdAt": row[25],
                    "updatedAt": row[26],
                }
            )
    return results


def list_action_events(action_id: str) -> list[dict[str, Any]]:
    """Return the immutable state/claim history used by takeover Evidence."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT event_id, event_type, state, owner_instance_id, execution_epoch,
                   claim_token_sha256, effect_handle_json, created_at
            FROM resilience_action_events
            WHERE action_id = ?
            ORDER BY event_id ASC
            """,
            (action_id,),
        ).fetchall()
    return [
        {
            "eventId": int(row[0]),
            "eventType": str(row[1]),
            "state": str(row[2]),
            "ownerInstanceId": row[3],
            "executionEpoch": int(row[4]),
            "claimTokenSha256": row[5],
            "effectHandle": json.loads(row[6]) if row[6] else None,
            "createdAt": str(row[7]),
        }
        for row in rows
    ]


def verify_action_outcome(
    action: dict[str, Any],
    execution_result: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Verify outcome post-conditions (Gate E)."""
    return resilience_outcome_verifier.verify_action_outcome(action, execution_result)


def verify_scoped_risk_reduction(
    action: dict[str, Any],
    risk_before_snapshot: dict[str, Any],
    risk_after_snapshot: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Verify scoped risk reduction on exact subject (Gate F)."""
    return resilience_outcome_verifier.verify_scoped_risk_reduction(action, risk_before_snapshot, risk_after_snapshot)


def execute_autonomous_action(
    action_id: str,
    *,
    instance_id: str = "resilience-worker",
    lease_seconds: int = 120,
) -> dict[str, Any]:
    """Execute an admitted action with full crash-recoverable closed-loop verified lifecycle."""
    action = get_action(action_id)
    if not action:
        raise AppError(f"Action '{action_id}' not found in journal", code=ErrorCode.NOT_FOUND, status=404)

    from deepseek_infra.infra.workspace import (
        autonomous_action_policy,
        backup_dr_readiness,
        backup_replication,
        resilience_risk_engine,
    )

    act_type = str(action.get("type", "")).upper()
    params_raw = action.get("parameters")
    params = params_raw if isinstance(params_raw, dict) else {}

    # 1. Policy Admission Check
    admitted, adm_reason = autonomous_action_policy.validate_action_admission(action)
    if not admitted:
        update_action_state(action_id, "BLOCKED", error=adm_reason)
        raise AppError(f"Autonomous action execution blocked: {adm_reason}", code=ErrorCode.FORBIDDEN, status=403)

    # 2. Atomic budget admission, resource locks, and execution-epoch claim.
    claimed, claimed_action, claim_reason = admit_and_claim_action(
        action_id,
        owner_instance_id=instance_id,
        lease_seconds=lease_seconds,
    )
    if not claimed or not claimed_action:
        cur_state = action.get("state") if action else "unknown"
        is_budget_rejection = claim_reason.startswith(("max-", "hourly-"))
        if is_budget_rejection:
            update_action_state(action_id, "PENDING", error=claim_reason)
        raise AppError(
            f"Action '{action_id}' could not be claimed (state: {cur_state}, reason: {claim_reason})",
            code=ErrorCode.FORBIDDEN if is_budget_rejection else ErrorCode.INVALID_REQUEST,
            status=429 if is_budget_rejection else 409,
        )
    action = claimed_action
    epoch = action["executionEpoch"]
    token = action["claimToken"]

    # 3. Effect Reconciliation for Recovered / Taken-Over Actions (Gate C)
    skip_execution_mutation = False
    resume_existing_job = False
    reconciled_job_info: dict[str, Any] = {}
    result_payload: dict[str, Any] = {}
    current_effect: str = str(action.get("effectClass") or "NO_EFFECT")

    if action.get("state") == "RECONCILING":
        from deepseek_infra.infra.workspace import resilience_effect_reconciler

        reconcile_directive, reconcile_details = resilience_effect_reconciler.reconcile_action_effect(
            action, instance_id=instance_id
        )
        if reconcile_directive == "EFFECT_UNKNOWN":
            err_msg = str(reconcile_details.get("error") or "effect reconciliation failed closed")
            update_action_state(
                action_id,
                "EFFECT_UNKNOWN",
                execution_epoch=epoch,
                claim_token=token,
                compensation_state="REMOTE_EFFECT_UNCERTAIN",
                error=err_msg,
            )
            raise AppError(f"Action effect reconciliation failed closed: {err_msg}", code=ErrorCode.INTERNAL, status=500)
        elif reconcile_directive == "TRIGGER_COMPENSATION":
            err_msg = str(reconcile_details.get("error") or "reconciliation-triggered-compensation")
            compensate_action(
                action_id,
                err_msg,
                effect_class=str(action.get("effectClass") or "CANCELABLE"),
                execution_epoch=epoch,
                claim_token=token,
            )
            raise AppError(f"Action compensation triggered during reconciliation: {err_msg}", code=ErrorCode.INTERNAL, status=500)
        elif reconcile_directive == "ADVANCE_TO_VERIFYING":
            result_payload = dict(reconcile_details)
            current_effect = str(action.get("effectClass") or "CANCELABLE")
            skip_execution_mutation = True
        elif reconcile_directive == "RESUME_EXECUTION":
            resume_existing_job = True
            reconciled_job_info = reconcile_details
            
    journal_mod = sys.modules[__name__]
    risk_before_snapshot = resilience_risk_engine.assess_risks(probe=False)
    risk_before_digest = str(risk_before_snapshot.get("riskDigest") or "")

    try:
        if not skip_execution_mutation:
            # 4. Fresh Risk Check & TOCTOU Fencing (Gate C)
            fresh, fresh_reason = journal_mod.check_action_freshness(action, risk_before_snapshot)
            if not fresh:
                updated = update_action_state(
                    action_id,
                    "SKIPPED_NO_LONGER_NEEDED" if "cleared" in fresh_reason else "REPLAN_REQUIRED",
                    execution_epoch=epoch,
                    claim_token=token,
                    risk_before_digest=risk_before_digest,
                    error=fresh_reason,
                )
                return updated

            # 5. Precondition Simulation (Gate H & K)
            sim_ok, sim_details = journal_mod.simulate_action(action)
            if not sim_ok:
                err = sim_details.get("error", "simulation-preconditions-unmet")
                update_action_state(action_id, "BLOCKED", execution_epoch=epoch, claim_token=token, error=err)
                raise AppError(f"Action precondition simulation failed: {err}", code=ErrorCode.INVALID_REQUEST, status=400)

            # 6. Execute Underlying Subsystem with Idempotency Key & Persist effectHandle (Gate C & D)
            update_action_state(action_id, "EXECUTING", execution_epoch=epoch, claim_token=token, effect_class="NO_EFFECT")
            current_effect = "NO_EFFECT"
            effect_handle: dict[str, Any] = {}

            if act_type == "CREATE_REBALANCE_JOB":
                policy_id = str(params.get("policyId") or action.get("policyId") or "")
                backup_id = str(params.get("backupId") or action.get("backupId") or "")
                source_id = str(params.get("sourceTargetId") or params.get("source") or action.get("source") or "")
                dest_id = str(params.get("destTargetId") or params.get("destination") or action.get("destination") or "")
                reason = str(params.get("reason") or "resilience-planner-rebalance")

                if resume_existing_job and reconciled_job_info.get("jobId"):
                    job = reconciled_job_info.get("job") or backup_replication.read_rebalance_job(str(reconciled_job_info["jobId"])) or {}
                else:
                    job = backup_replication.create_rebalance_job(
                        policy_id=policy_id,
                        backup_id=backup_id,
                        source_target_id=source_id,
                        dest_target_id=dest_id,
                        reason=reason,
                        resilience_action_id=action_id,
                    )
                current_effect = "CANCELABLE"
                effect_handle = {"kind": "rebalance", "jobId": job.get("jobId")}
                result_payload = {"job": job, "jobId": job.get("jobId")}
                update_action_state(action_id, "EXECUTING", execution_epoch=epoch, claim_token=token, effect_class=current_effect, effect_handle=effect_handle)

                # Execute rebalance to destination durability completion (Gate E)
                try:
                    reb_res = _run_with_action_lease_heartbeat(
                        action_id=action_id,
                        execution_epoch=epoch,
                        claim_token=token,
                        lease_seconds=lease_seconds,
                        operation_name="rebalance",
                        operation=lambda: backup_replication.execute_rebalance_job(str(job["jobId"])),
                    )
                    result_payload["executionStatus"] = reb_res.get("status")
                    result_payload["rebalanceResult"] = reb_res
                except AppError as exc:
                    if exc.status == 409:
                        raise
                    result_payload["error"] = str(exc)
                except Exception as exc:
                    result_payload["error"] = str(exc)

            elif act_type == "CREATE_REPAIR_JOB":
                policy_id = str(params.get("policyId") or action.get("policyId") or "")
                backup_id = str(params.get("backupId") or action.get("backupId") or "")
                source_id = str(params.get("sourceTargetId") or params.get("source") or action.get("source") or "")
                dest_id = str(
                    params.get("destTargetId")
                    or params.get("destination")
                    or params.get("targetId")
                    or action.get("destination")
                    or ""
                )
                if resume_existing_job and reconciled_job_info.get("repairId"):
                    job = reconciled_job_info.get("job") or backup_replication.read_repair_job(str(reconciled_job_info["repairId"])) or {}
                else:
                    job = backup_replication.create_repair_job(
                        policy_id=policy_id,
                        backup_id=backup_id,
                        source_target_id=source_id,
                        dest_target_id=dest_id,
                        resilience_action_id=action_id,
                    )
                current_effect = "CANCELABLE"
                effect_handle = {"kind": "repair", "repairId": job.get("repairId")}
                result_payload = {"job": job, "repairId": job.get("repairId")}
                update_action_state(action_id, "EXECUTING", execution_epoch=epoch, claim_token=token, effect_class=current_effect, effect_handle=effect_handle)

                # Execute replica repair to completion (Gate E)
                try:
                    rep_res = _run_with_action_lease_heartbeat(
                        action_id=action_id,
                        execution_epoch=epoch,
                        claim_token=token,
                        lease_seconds=lease_seconds,
                        operation_name="repair",
                        operation=lambda: backup_replication.execute_replica_repair(
                            policy_id=policy_id,
                            backup_id=backup_id,
                            dest_target_id=dest_id,
                            source_target_id=source_id,
                            run_id=str(job["repairId"]),
                        ),
                    )
                    result_payload["executionStatus"] = rep_res.get("status") or rep_res.get("phase")
                    result_payload["repairResult"] = rep_res
                except AppError as exc:
                    if exc.status == 409:
                        raise
                    result_payload["error"] = str(exc)
                except Exception as exc:
                    result_payload["error"] = str(exc)

            elif act_type == "START_DR_DRILL":
                effect_handle = {"kind": "drill", "resilienceActionId": action_id}
                drill_res = _run_with_action_lease_heartbeat(
                    action_id=action_id,
                    execution_epoch=epoch,
                    claim_token=token,
                    lease_seconds=lease_seconds,
                    operation_name="dr-drill",
                    operation=lambda: backup_dr_readiness.run_dr_drill(
                        backup_id=params.get("backupId"),
                        target_id=params.get("targetId"),
                        resilience_action_id=action_id,
                    ),
                )
                current_effect = "COMPENSATABLE"
                result_payload = drill_res
                update_action_state(action_id, "EXECUTING", execution_epoch=epoch, claim_token=token, effect_class=current_effect, effect_handle=effect_handle)

            else:
                raise AppError(f"Unsupported action execution type: {act_type}", code=ErrorCode.INVALID_REQUEST, status=400)

        # 7. Post-Condition Outcome Verification (Gate E)
        update_action_state(action_id, "VERIFYING", execution_epoch=epoch, claim_token=token, effect_class=current_effect)
        verified_ok, verif_details = _run_with_action_lease_heartbeat(
            action_id=action_id,
            execution_epoch=epoch,
            claim_token=token,
            lease_seconds=lease_seconds,
            operation_name="outcome-verification",
            operation=lambda: journal_mod.verify_action_outcome(action, result_payload),
        )
        if not verified_ok:
            verif_err = str(verif_details.get("error") or "post-condition-outcome-verification-failed")
            compensate_action(action_id, verif_err, effect_class=current_effect, execution_epoch=epoch, claim_token=token)
            raise AppError(f"Outcome verification failed: {verif_err}", code=ErrorCode.INTERNAL, status=500)

        # 8. Scoped Closed-Loop Risk Reduction Verification (Gate F)
        update_action_state(action_id, "ASSESSING_EFFECT", execution_epoch=epoch, claim_token=token, effect_class=current_effect)
        risk_after_snapshot = resilience_risk_engine.assess_risks(probe=False)
        risk_after_digest = str(risk_after_snapshot.get("riskDigest") or "")

        effect_reduced, reduction_details = journal_mod.verify_scoped_risk_reduction(
            action,
            risk_before_snapshot,
            risk_after_snapshot,
        )
        if not effect_reduced:
            red_err = str(reduction_details.get("reason") or "target-risk-not-improved")
            compensate_action(action_id, red_err, effect_class=current_effect, execution_epoch=epoch, claim_token=token)
            raise AppError(f"Scoped risk reduction failed: {red_err}", code=ErrorCode.INTERNAL, status=500)

        # 9. Build Decision Proof v3
        decision_proof = {
            "riskDigest": action.get("inputRiskDigest") or risk_before_digest,
            "riskBeforeDigest": risk_before_digest,
            "riskAfterDigest": risk_after_digest,
            "riskBefore": {"overallRisk": risk_before_snapshot.get("overallRisk")},
            "riskAfter": {"overallRisk": risk_after_snapshot.get("overallRisk")},
            "policyVersion": autonomous_action_policy.AUTOMATION_POLICY_VERSION,
            "actionAllowed": True,
            "simulationPassed": True,
            "executionVerified": True,
            "effectObserved": reduction_details.get("effectObserved") is True,
            "scopedRiskSubject": reduction_details.get("riskSubject"),
            "severityBefore": reduction_details.get("severityBefore"),
            "severityAfter": reduction_details.get("severityAfter"),
            "executedActionType": act_type,
            "actionId": action_id,
            "planId": action.get("planId"),
        }

        updated = update_action_state(
            action_id,
            "SUCCEEDED",
            execution_epoch=epoch,
            claim_token=token,
            effect_class=current_effect,
            result=result_payload,
            verification=verif_details,
            proof=decision_proof,
            risk_before_digest=risk_before_digest,
            risk_after_digest=risk_after_digest,
        )
        return updated

    except Exception as exc:
        if not isinstance(exc, AppError) or exc.status >= 500:
            compensate_action(action_id, str(exc), effect_class=current_effect, execution_epoch=epoch, claim_token=token)
        if not isinstance(exc, AppError):
            raise AppError(f"Action execution failed: {exc}", code=ErrorCode.INTERNAL, status=500) from exc
        raise

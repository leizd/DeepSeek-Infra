"""Durable SQLite scheduler for backup policies (4.4.5).

Tracks schedule slots, runs and leases in a single SQLite database. The
``UNIQUE(policy_id, slot_key)`` constraint makes concurrent workers claiming
the same schedule slot a no-op for all but one of them; fencing tokens and
lease expirations ensure a crashed worker can be taken over without ever
letting a stale worker publish over a newer one. Executing runs hold a
:class:`RunLeaseGuard` that renews the lease on a heartbeat and checkpoints
ownership against the current clock before every visible commit step.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_policies
from deepseek_infra.infra.workspace.backup_cron import iter_slots, next_slot, parse_cron

_logger = logging.getLogger("deepseek_infra.backup_worker")

BACKUP_SCHEDULER_DIR = config.ROOT / ".backup-scheduler"
SCHEDULER_DB_NAME = "scheduler.db"

RUN_PHASES = (
    "queued",
    "leased",
    "waiting-for-mirror",
    "snapshotting",
    "encrypting",
    "verifying",
    "publishing",
    "cataloging",
    "pruning",
    "reconciling",
    "complete",
    "deferred",
    "blocked",
    "blocked-retryable",
    "blocked-terminal",
    "superseded",
    "failed",
    "abandoned",
)
TERMINAL_PHASES = ("complete", "failed", "abandoned", "blocked-terminal", "superseded")
ACTIVE_PHASES = ("queued", "leased", "waiting-for-mirror", "snapshotting", "encrypting", "verifying", "publishing", "cataloging", "pruning", "reconciling")
BLOCKED_PHASES = ("blocked", "blocked-retryable")

DEFAULT_LEASE_SECONDS = 300
LEASE_HEARTBEAT_SECONDS = 60.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS backup_schedule_slots (
    policy_id TEXT NOT NULL,
    slot_key TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    local_date_time TEXT NOT NULL,
    timezone TEXT NOT NULL,
    status TEXT NOT NULL,
    run_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(policy_id, slot_key)
);
CREATE TABLE IF NOT EXISTS backup_runs (
    run_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    schedule_slot TEXT NOT NULL,
    phase TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    owner_instance_id TEXT,
    fencing_token INTEGER,
    lease_until TEXT,
    reason TEXT,
    error TEXT,
    backup_id TEXT,
    filename TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backup_counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS backup_target_health (
    target_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS retention_runs (
    retention_run_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    status TEXT NOT NULL,
    preview TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class ClaimedRun:
    run_id: str
    policy_id: str
    schedule_slot: str
    scheduled_for: str
    attempt: int
    fencing_token: int


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _connect() -> sqlite3.Connection:
    BACKUP_SCHEDULER_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(BACKUP_SCHEDULER_DIR / SCHEDULER_DB_NAME, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    for statement in _SCHEMA.strip().split(";"):
        if statement.strip():
            connection.execute(statement)
    return connection


def _next_token(connection: sqlite3.Connection, name: str) -> int:
    connection.execute(
        "INSERT INTO backup_counters(name, value) VALUES (?, 0) ON CONFLICT(name) DO NOTHING",
        (name,),
    )
    connection.execute("UPDATE backup_counters SET value = value + 1 WHERE name = ?", (name,))
    row = connection.execute("SELECT value FROM backup_counters WHERE name = ?", (name,)).fetchone()
    return int(row["value"])


def deterministic_jitter_seconds(policy_id: str, slot_key: str, jitter_seconds: int) -> int:
    if jitter_seconds <= 0:
        return 0
    digest = hashlib.sha256(f"{policy_id}|{slot_key}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % (jitter_seconds + 1)


def _row_to_run(row: sqlite3.Row) -> dict[str, Any]:
    phase = str(row["phase"])
    blocked = phase in BLOCKED_PHASES or phase == "blocked-terminal"
    return {
        "runId": row["run_id"],
        "policyId": row["policy_id"],
        "scheduleSlot": row["schedule_slot"],
        "phase": phase,
        "attempt": row["attempt"],
        "ownerInstanceId": row["owner_instance_id"],
        "fencingToken": row["fencing_token"],
        "leaseUntil": row["lease_until"],
        "nextRetryAt": row["lease_until"] if blocked else None,
        "blockedReason": row["reason"] if blocked else None,
        "reason": row["reason"],
        "error": row["error"],
        "backupId": row["backup_id"],
        "filename": row["filename"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _latest_slot_state(connection: sqlite3.Connection, policy_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM backup_schedule_slots WHERE policy_id = ? ORDER BY scheduled_for DESC LIMIT 1",
        (policy_id,),
    ).fetchone()


def _slot_recorded(connection: sqlite3.Connection, policy_id: str, slot_key: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM backup_schedule_slots WHERE policy_id = ? AND slot_key = ?",
            (policy_id, slot_key),
        ).fetchone()
        is not None
    )


def _record_skipped_slot(
    connection: sqlite3.Connection,
    policy_id: str,
    slot: Any,
    reason: str,
) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO backup_schedule_slots(policy_id, slot_key, scheduled_for, local_date_time, timezone, status, run_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (policy_id, slot.slot_key, _utc_iso(slot.scheduled_for), slot.local_iso, slot.timezone, reason, None, _utc_iso()),
    )


def claim_due_slots(
    policies: list[dict[str, Any]],
    *,
    instance_id: str,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> list[ClaimedRun]:
    """Claim due schedule slots for the given policies.

    Missed slots within the catch-up window coalesce to a single run; slots
    outside the window are recorded as skipped. Claiming is safe under
    concurrency: only the first insert for ``(policy_id, slot_key)`` wins.
    """
    current = now or datetime.now(tz=timezone.utc)
    claimed: list[ClaimedRun] = []
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for policy in policies:
            if not policy.get("enabled"):
                continue
            policy_id = str(policy.get("policyId") or "")
            schedule_cfg = policy.get("schedule") or {}
            try:
                schedule = parse_cron(str(schedule_cfg.get("cron") or ""))
            except AppError:
                continue
            timezone_name = str(schedule_cfg.get("timezone") or "UTC")
            catchup = int(schedule_cfg.get("catchupWindowSeconds") or 86400)
            jitter = int(schedule_cfg.get("jitterSeconds") or 0)
            misfire = str(schedule_cfg.get("misfirePolicy") or "skip")
            latest = _latest_slot_state(connection, policy_id)
            catchup_start = current - timedelta(seconds=catchup)
            # Start from the last recorded slot so out-of-window misses are
            # still recorded as skipped instead of silently dropped.
            iter_start = _parse_iso(str(latest["scheduled_for"])) if latest is not None else catchup_start
            slots = list(
                iter_slots(
                    schedule,
                    timezone_name,
                    start_utc=iter_start,
                    end_utc=current + timedelta(seconds=1),
                    misfire_policy=misfire,
                )
            )
            due = [slot for slot in slots if slot.scheduled_for + timedelta(seconds=deterministic_jitter_seconds(policy_id, slot.slot_key, jitter)) <= current]
            if not due:
                continue
            *skipped, runnable = due
            for slot in skipped:
                reason = "catchup-coalesced" if slot.scheduled_for >= catchup_start else "outside-catchup-window"
                _record_skipped_slot(connection, policy_id, slot, reason)
            slot = runnable
            if slot.scheduled_for < catchup_start:
                _record_skipped_slot(connection, policy_id, slot, "outside-catchup-window")
                continue
            if _slot_recorded(connection, policy_id, slot.slot_key):
                continue
            run_id = f"run_{uuid.uuid4().hex[:16]}"
            token = _next_token(connection, "fencing")
            attempt = 1
            cursor = connection.execute(
                "INSERT OR IGNORE INTO backup_schedule_slots(policy_id, slot_key, scheduled_for, local_date_time, timezone, status, run_id, created_at) VALUES (?,?,?,?,?,'claimed',?,?)",
                (policy_id, slot.slot_key, _utc_iso(slot.scheduled_for), slot.local_iso, slot.timezone, run_id, _utc_iso()),
            )
            if cursor.rowcount == 0:
                continue
            connection.execute(
                "INSERT INTO backup_runs(run_id, policy_id, schedule_slot, phase, attempt, owner_instance_id, fencing_token, lease_until, created_at, updated_at) VALUES (?,?,?,'leased',?,?,?,?,?,?)",
                (
                    run_id,
                    policy_id,
                    slot.slot_key,
                    attempt,
                    instance_id,
                    token,
                    _utc_iso(current + timedelta(seconds=lease_seconds)),
                    _utc_iso(),
                    _utc_iso(),
                ),
            )
            claimed.append(
                ClaimedRun(
                    run_id=run_id,
                    policy_id=policy_id,
                    schedule_slot=slot.slot_key,
                    scheduled_for=_utc_iso(slot.scheduled_for),
                    attempt=attempt,
                    fencing_token=token,
                )
            )
    return claimed


def claim_manual_run(policy: dict[str, Any], *, instance_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS, now: datetime | None = None) -> ClaimedRun:
    """Claim an ad-hoc manual run for a policy (POST .../run)."""
    current = now or datetime.now(tz=timezone.utc)
    policy_id = str(policy.get("policyId") or "")
    slot_key = f"manual/{uuid.uuid4().hex}"
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        token = _next_token(connection, "fencing")
        cursor = connection.execute(
            "INSERT OR IGNORE INTO backup_schedule_slots(policy_id, slot_key, scheduled_for, local_date_time, timezone, status, run_id, created_at) VALUES (?,?,?,?,?,'claimed',?,?)",
            (policy_id, slot_key, _utc_iso(current), _utc_iso(current), "UTC", run_id, _utc_iso()),
        )
        if cursor.rowcount == 0:
            raise AppError("Manual backup run slot was already claimed", code=ErrorCode.INVALID_REQUEST, status=409)
        connection.execute(
            "INSERT INTO backup_runs(run_id, policy_id, schedule_slot, phase, attempt, owner_instance_id, fencing_token, lease_until, created_at, updated_at) VALUES (?,?,?,'leased',?,?,?,?,?,?)",
            (run_id, policy_id, slot_key, 1, instance_id, token, _utc_iso(current + timedelta(seconds=lease_seconds)), _utc_iso(), _utc_iso()),
        )
    return ClaimedRun(run_id=run_id, policy_id=policy_id, schedule_slot=slot_key, scheduled_for=_utc_iso(current), attempt=1, fencing_token=token)


def reclaim_abandoned_slots(
    *,
    instance_id: str,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> list[ClaimedRun]:
    """Take over runs whose lease expired without a terminal phase."""
    current = now or datetime.now(tz=timezone.utc)
    reclaimed: list[ClaimedRun] = []
    placeholders = ",".join("?" for _ in ACTIVE_PHASES)
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            f"SELECT * FROM backup_runs WHERE phase IN ({placeholders}) AND lease_until < ?",
            (*ACTIVE_PHASES, _utc_iso(current)),
        ).fetchall()
        for row in rows:
            token = _next_token(connection, "fencing")
            connection.execute(
                "UPDATE backup_runs SET phase = 'abandoned', error = ?, updated_at = ? WHERE run_id = ? AND phase = ?",
                ("lease-expired", _utc_iso(), row["run_id"], row["phase"]),
            )
            run_id = f"run_{uuid.uuid4().hex[:16]}"
            attempt = int(row["attempt"]) + 1
            connection.execute(
                "INSERT INTO backup_runs(run_id, policy_id, schedule_slot, phase, attempt, owner_instance_id, fencing_token, lease_until, created_at, updated_at) VALUES (?,?,?,'leased',?,?,?,?,?,?)",
                (
                    run_id,
                    row["policy_id"],
                    row["schedule_slot"],
                    attempt,
                    instance_id,
                    token,
                    _utc_iso(current + timedelta(seconds=lease_seconds)),
                    _utc_iso(),
                    _utc_iso(),
                ),
            )
            connection.execute(
                "UPDATE backup_schedule_slots SET run_id = ?, status = 'claimed' WHERE policy_id = ? AND slot_key = ?",
                (run_id, row["policy_id"], row["schedule_slot"]),
            )
            slot_row = connection.execute(
                "SELECT scheduled_for FROM backup_schedule_slots WHERE policy_id = ? AND slot_key = ?",
                (row["policy_id"], row["schedule_slot"]),
            ).fetchone()
            reclaimed.append(
                ClaimedRun(
                    run_id=run_id,
                    policy_id=str(row["policy_id"]),
                    schedule_slot=str(row["schedule_slot"]),
                    scheduled_for=str(slot_row["scheduled_for"]) if slot_row else str(row["schedule_slot"]),
                    attempt=attempt,
                    fencing_token=token,
                )
            )
    return reclaimed


def _policy_max_attempts(policy: dict[str, Any]) -> int:
    retry = policy.get("retry")
    value = retry.get("maxAttempts") if isinstance(retry, dict) else None
    return max(1, int(value or 3))


def reclaim_blocked_slots(
    policies: list[dict[str, Any]],
    *,
    instance_id: str,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    probe_seconds: int = 300,
) -> list[ClaimedRun]:
    """Retry blocked runs once their target probes healthy again.

    Blocked runs stay parked (with ``nextRetryAt`` pushed forward) while the
    target stays offline; they become ``blocked-terminal`` when the policy is
    gone, the catch-up window has passed or attempts are exhausted.
    """
    from deepseek_infra.infra.workspace import backup_publish

    current = now or datetime.now(tz=timezone.utc)
    policy_map = {str(policy.get("policyId") or ""): policy for policy in policies if policy.get("enabled")}
    reclaimed: list[ClaimedRun] = []
    placeholders = ",".join("?" for _ in BLOCKED_PHASES)
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            f"SELECT * FROM backup_runs WHERE phase IN ({placeholders}) AND lease_until <= ?",
            (*BLOCKED_PHASES, _utc_iso(current)),
        ).fetchall()
        for row in rows:
            policy = policy_map.get(str(row["policy_id"]))
            slot_row = connection.execute(
                "SELECT scheduled_for FROM backup_schedule_slots WHERE policy_id = ? AND slot_key = ?",
                (row["policy_id"], row["schedule_slot"]),
            ).fetchone()
            scheduled_for = str(slot_row["scheduled_for"]) if slot_row else str(row["schedule_slot"])

            def _terminal(reason: str) -> None:
                connection.execute(
                    "UPDATE backup_runs SET phase = 'blocked-terminal', error = ?, reason = ?, updated_at = ? WHERE run_id = ?",
                    (str(row["error"] or "")[:500], reason, _utc_iso(), row["run_id"]),
                )
                connection.execute("UPDATE backup_schedule_slots SET status = 'failed' WHERE run_id = ?", (row["run_id"],))

            if policy is None:
                _terminal("policy-missing")
                continue
            catchup = int((policy.get("schedule") or {}).get("catchupWindowSeconds") or 86400)
            try:
                slot_time = _parse_iso(scheduled_for)
            except ValueError:
                slot_time = current
            if slot_time < current - timedelta(seconds=catchup):
                _terminal("catchup-window-exceeded")
                continue
            if int(row["attempt"]) >= _policy_max_attempts(policy):
                _terminal("max-attempts-exceeded")
                continue
            target_id = str(policy.get("targetId") or "managed-local")
            try:
                backup_publish.resolve_target(target_id)
            except AppError as exc:
                connection.execute(
                    "INSERT INTO backup_target_health(target_id, status, checked_at, detail) VALUES (?,?,?,?) ON CONFLICT(target_id) DO UPDATE SET status = excluded.status, checked_at = excluded.checked_at, detail = excluded.detail",
                    (target_id, "blocked", _utc_iso(), str(exc)[:200]),
                )
                connection.execute(
                    "UPDATE backup_runs SET lease_until = ?, updated_at = ? WHERE run_id = ?",
                    (_utc_iso(current + timedelta(seconds=probe_seconds)), _utc_iso(), row["run_id"]),
                )
                continue
            connection.execute(
                "INSERT INTO backup_target_health(target_id, status, checked_at, detail) VALUES (?,?,?,NULL) ON CONFLICT(target_id) DO UPDATE SET status = excluded.status, checked_at = excluded.checked_at, detail = excluded.detail",
                (target_id, "ok", _utc_iso()),
            )
            token = _next_token(connection, "fencing")
            connection.execute(
                "UPDATE backup_runs SET phase = 'abandoned', error = ?, updated_at = ? WHERE run_id = ?",
                ("blocked-retry", _utc_iso(), row["run_id"]),
            )
            run_id = f"run_{uuid.uuid4().hex[:16]}"
            attempt = int(row["attempt"]) + 1
            connection.execute(
                "INSERT INTO backup_runs(run_id, policy_id, schedule_slot, phase, attempt, owner_instance_id, fencing_token, lease_until, created_at, updated_at) VALUES (?,?,?,'leased',?,?,?,?,?,?)",
                (run_id, row["policy_id"], row["schedule_slot"], attempt, instance_id, token, _utc_iso(current + timedelta(seconds=lease_seconds)), _utc_iso(), _utc_iso()),
            )
            connection.execute(
                "UPDATE backup_schedule_slots SET run_id = ?, status = 'claimed' WHERE policy_id = ? AND slot_key = ?",
                (run_id, row["policy_id"], row["schedule_slot"]),
            )
            reclaimed.append(
                ClaimedRun(
                    run_id=run_id,
                    policy_id=str(row["policy_id"]),
                    schedule_slot=str(row["schedule_slot"]),
                    scheduled_for=scheduled_for,
                    attempt=attempt,
                    fencing_token=token,
                )
            )
    return reclaimed


def reclaim_deferred_slots(
    policies: list[dict[str, Any]],
    *,
    instance_id: str,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> list[ClaimedRun]:
    """Re-run slots deferred (e.g. by an active restore fence) inside their catch-up window."""
    current = now or datetime.now(tz=timezone.utc)
    catchup_by_policy = {
        str(policy.get("policyId") or ""): int((policy.get("schedule") or {}).get("catchupWindowSeconds") or 86400)
        for policy in policies
        if policy.get("enabled")
    }
    reclaimed: list[ClaimedRun] = []
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for policy_id, catchup in catchup_by_policy.items():
            rows = connection.execute(
                "SELECT * FROM backup_schedule_slots WHERE policy_id = ? AND status = 'deferred' AND scheduled_for >= ?",
                (policy_id, _utc_iso(current - timedelta(seconds=catchup))),
            ).fetchall()
            for slot in rows:
                previous = connection.execute(
                    "SELECT * FROM backup_runs WHERE run_id = ?",
                    (slot["run_id"],),
                ).fetchone()
                attempt = int(previous["attempt"]) + 1 if previous is not None else 1
                run_id = f"run_{uuid.uuid4().hex[:16]}"
                token = _next_token(connection, "fencing")
                connection.execute(
                    "INSERT INTO backup_runs(run_id, policy_id, schedule_slot, phase, attempt, owner_instance_id, fencing_token, lease_until, created_at, updated_at) VALUES (?,?,?,'leased',?,?,?,?,?,?)",
                    (
                        run_id,
                        policy_id,
                        slot["slot_key"],
                        attempt,
                        instance_id,
                        token,
                        _utc_iso(current + timedelta(seconds=lease_seconds)),
                        _utc_iso(),
                        _utc_iso(),
                    ),
                )
                connection.execute(
                    "UPDATE backup_schedule_slots SET run_id = ?, status = 'claimed' WHERE policy_id = ? AND slot_key = ?",
                    (run_id, policy_id, slot["slot_key"]),
                )
                reclaimed.append(
                    ClaimedRun(
                        run_id=run_id,
                        policy_id=policy_id,
                        schedule_slot=str(slot["slot_key"]),
                        scheduled_for=str(slot["scheduled_for"]),
                        attempt=attempt,
                        fencing_token=token,
                    )
                )
    return reclaimed


def _fetch_run(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM backup_runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise AppError("Backup run not found", code=ErrorCode.NOT_FOUND, status=404)
    return row


def assert_run_lease(run_id: str, instance_id: str, fencing_token: int, *, now: datetime | None = None) -> None:
    """Raise 409 when the caller no longer owns the run lease."""
    current = now or datetime.now(tz=timezone.utc)
    with _connect() as connection:
        row = _fetch_run(connection, run_id)
        if row["phase"] in TERMINAL_PHASES:
            raise AppError("Backup run is no longer active", code=ErrorCode.INVALID_REQUEST, status=409)
        if row["owner_instance_id"] != instance_id or int(row["fencing_token"]) != fencing_token:
            raise AppError("Backup run lease was lost to another worker", code=ErrorCode.INVALID_REQUEST, status=409)
        if str(row["lease_until"]) < _utc_iso(current):
            raise AppError("Backup run lease expired", code=ErrorCode.INVALID_REQUEST, status=409)


def renew_run_lease(run_id: str, instance_id: str, fencing_token: int, *, lease_seconds: int = DEFAULT_LEASE_SECONDS, now: datetime | None = None) -> None:
    current = now or datetime.now(tz=timezone.utc)
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE backup_runs SET lease_until = ?, updated_at = ? WHERE run_id = ? AND owner_instance_id = ? AND fencing_token = ? AND lease_until >= ? AND phase NOT IN ('complete', 'failed', 'abandoned')",
            (
                _utc_iso(current + timedelta(seconds=lease_seconds)),
                _utc_iso(current),
                run_id,
                instance_id,
                fencing_token,
                _utc_iso(current),
            ),
        )
        if cursor.rowcount == 0:
            raise AppError("Backup run lease renewal failed; ownership lost", code=ErrorCode.INVALID_REQUEST, status=409)


class RunLeaseGuard:
    """Active lease holder for an executing run.

    The heartbeat renews the lease every ``heartbeat_seconds``; the first
    renewal failure sets ``cancel_event`` so chunked helpers stop at their
    next boundary. ``checkpoint()`` asserts ownership against the current
    clock — never the run's start time — and must run before every
    externally visible commit step.
    """

    def __init__(
        self,
        run_id: str,
        instance_id: str,
        fencing_token: int,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        heartbeat_seconds: float = LEASE_HEARTBEAT_SECONDS,
        clock: Callable[[], datetime] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.run_id = run_id
        self.instance_id = instance_id
        self.fencing_token = fencing_token
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.cancel_event = cancel_event or threading.Event()
        self._clock = clock or (lambda: datetime.now(tz=timezone.utc))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._renewal_error: str | None = None
        self._writer: Any = None

    def now(self) -> datetime:
        return self._clock()

    def attach_writer(self, writer: Any) -> None:
        """Attach a target writer lease renewed and asserted alongside the run lease."""
        self._writer = writer

    def checkpoint(self) -> None:
        """Raise 409 when the lease is cancelled, expired or taken over."""
        if self.cancel_event.is_set():
            detail = self._renewal_error or "lease heartbeat failed"
            raise AppError(f"Backup run lease lost: {detail}", code=ErrorCode.INVALID_REQUEST, status=409)
        assert_run_lease(self.run_id, self.instance_id, self.fencing_token, now=self._clock())
        if self._writer is not None:
            self._writer.assert_owned()

    def start_heartbeat(self) -> None:
        if self._thread is not None or self.heartbeat_seconds <= 0:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._heartbeat_loop, name=f"backup-lease-heartbeat-{self.run_id[-8:]}", daemon=True)
        self._thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                renew_run_lease(self.run_id, self.instance_id, self.fencing_token, lease_seconds=self.lease_seconds, now=self._clock())
                if self._writer is not None:
                    self._writer.renew()
            except Exception as exc:
                self._renewal_error = str(exc)[:200]
                self.cancel_event.set()
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None


def _assert_run_owned_and_leased(connection: sqlite3.Connection, run_id: str, instance_id: str, fencing_token: int, now: datetime | None) -> None:
    row = _fetch_run(connection, run_id)
    if row["owner_instance_id"] != instance_id or int(row["fencing_token"]) != fencing_token:
        raise AppError("Backup run lease was lost to another worker", code=ErrorCode.INVALID_REQUEST, status=409)
    if str(row["lease_until"]) < _utc_iso(now):
        raise AppError("Backup run lease expired", code=ErrorCode.INVALID_REQUEST, status=409)


def record_run_phase(run_id: str, phase: str, *, instance_id: str | None = None, fencing_token: int | None = None, reason: str | None = None, now: datetime | None = None) -> None:
    if phase not in RUN_PHASES:
        raise AppError(f"Unknown backup run phase {phase}", code=ErrorCode.INVALID_PAYLOAD)
    with _connect() as connection:
        if instance_id is not None and fencing_token is not None:
            _assert_run_owned_and_leased(connection, run_id, instance_id, fencing_token, now)
        connection.execute(
            "UPDATE backup_runs SET phase = ?, reason = COALESCE(?, reason), updated_at = ? WHERE run_id = ?",
            (phase, reason, _utc_iso(), run_id),
        )
        if phase in TERMINAL_PHASES or phase == "deferred":
            status = {"complete": "complete", "failed": "failed", "abandoned": "failed", "deferred": "deferred", "blocked-terminal": "failed", "superseded": "complete"}.get(phase)
            if status:
                connection.execute(
                    "UPDATE backup_schedule_slots SET status = ? WHERE run_id = ?",
                    (status, run_id),
                )


def complete_run(run_id: str, *, backup_id: str, filename: str, instance_id: str, fencing_token: int, now: datetime | None = None) -> None:
    with _connect() as connection:
        row = _fetch_run(connection, run_id)
        if row["phase"] in TERMINAL_PHASES:
            raise AppError("Backup run is no longer active", code=ErrorCode.INVALID_REQUEST, status=409)
        _assert_run_owned_and_leased(connection, run_id, instance_id, fencing_token, now)
        connection.execute(
            "UPDATE backup_runs SET phase = 'complete', backup_id = ?, filename = ?, updated_at = ? WHERE run_id = ?",
            (backup_id, filename, _utc_iso(), run_id),
        )
        connection.execute("UPDATE backup_schedule_slots SET status = 'complete' WHERE run_id = ?", (run_id,))


def requeue_run(run_id: str, *, instance_id: str, fencing_token: int, retry_at: datetime, error: str | None = None, now: datetime | None = None) -> None:
    """Park an active run until its backoff expires; reclaim picks it up later."""
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE backup_runs SET phase = 'queued', lease_until = ?, error = ?, updated_at = ? WHERE run_id = ? AND owner_instance_id = ? AND fencing_token = ? AND lease_until >= ?",
            (_utc_iso(retry_at), (error or "")[:500] or None, _utc_iso(), run_id, instance_id, fencing_token, _utc_iso(now)),
        )
        if cursor.rowcount == 0:
            raise AppError("Backup run lease was lost to another worker", code=ErrorCode.INVALID_REQUEST, status=409)


def block_run(
    run_id: str,
    *,
    instance_id: str,
    fencing_token: int,
    error: str,
    reason: str,
    retry_at: datetime | None = None,
    terminal: bool = False,
    now: datetime | None = None,
) -> None:
    """Park a run whose target is unavailable; retryable blocks carry ``nextRetryAt``."""
    phase = "blocked-terminal" if terminal else "blocked-retryable"
    with _connect() as connection:
        _assert_run_owned_and_leased(connection, run_id, instance_id, fencing_token, now)
        connection.execute(
            "UPDATE backup_runs SET phase = ?, lease_until = ?, error = ?, reason = ?, updated_at = ? WHERE run_id = ?",
            (phase, _utc_iso(retry_at) if (retry_at is not None and not terminal) else None, error[:500], reason, _utc_iso(), run_id),
        )
        connection.execute("UPDATE backup_schedule_slots SET status = ? WHERE run_id = ?", ("failed" if terminal else "blocked", run_id))


def fail_run(run_id: str, *, error: str, instance_id: str | None = None, fencing_token: int | None = None, phase: str = "failed", reason: str | None = None, now: datetime | None = None) -> None:
    if phase not in {"failed", "deferred", "blocked", "blocked-terminal", "superseded"}:
        raise AppError("fail_run phase must be failed, deferred, blocked, blocked-terminal or superseded", code=ErrorCode.INVALID_PAYLOAD)
    with _connect() as connection:
        if instance_id is not None and fencing_token is not None:
            _assert_run_owned_and_leased(connection, run_id, instance_id, fencing_token, now)
        connection.execute(
            "UPDATE backup_runs SET phase = ?, error = ?, reason = COALESCE(?, reason), updated_at = ? WHERE run_id = ?",
            (phase, error[:500], reason, _utc_iso(), run_id),
        )
        connection.execute("UPDATE backup_schedule_slots SET status = ? WHERE run_id = ?", ({"blocked-terminal": "failed", "superseded": "complete"}.get(phase, phase), run_id))


def converge_completed_run(run_id: str, *, backup_id: str, filename: str) -> bool:
    """Administratively converge a published run to complete during target reconciliation."""
    with _connect() as connection:
        row = _fetch_run(connection, run_id)
        if row["phase"] in TERMINAL_PHASES:
            return False
        connection.execute(
            "UPDATE backup_runs SET phase = 'complete', backup_id = ?, filename = ?, updated_at = ? WHERE run_id = ?",
            (backup_id, filename, _utc_iso(), run_id),
        )
        connection.execute("UPDATE backup_schedule_slots SET status = 'complete' WHERE run_id = ?", (run_id,))
        return True


def get_run(run_id: str) -> dict[str, Any]:
    with _connect() as connection:
        return _row_to_run(_fetch_run(connection, run_id))


def list_runs(*, policy_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as connection:
        if policy_id:
            rows = connection.execute(
                "SELECT * FROM backup_runs WHERE policy_id = ? ORDER BY created_at DESC LIMIT ?",
                (policy_id, max(1, min(limit, 500))),
            ).fetchall()
        else:
            rows = connection.execute("SELECT * FROM backup_runs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
        return [_row_to_run(row) for row in rows]


def list_active_runs() -> list[dict[str, Any]]:
    """List only non-terminal writer-capable runs; no historical scan is needed."""
    placeholders = ",".join("?" for _ in ACTIVE_PHASES)
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT * FROM backup_runs WHERE phase IN ({placeholders}) ORDER BY created_at",  # noqa: S608 - static placeholders
            ACTIVE_PHASES,
        ).fetchall()
    return [_row_to_run(row) for row in rows]


def next_run_for_policy(policy: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any] | None:
    schedule_cfg = policy.get("schedule") or {}
    try:
        schedule = parse_cron(str(schedule_cfg.get("cron") or ""))
    except AppError:
        return None
    try:
        slot = next_slot(
            schedule,
            str(schedule_cfg.get("timezone") or "UTC"),
            after_utc=now or datetime.now(tz=timezone.utc),
            misfire_policy=str(schedule_cfg.get("misfirePolicy") or "skip"),
        )
    except AppError:
        return None
    if slot is None:
        return None
    jitter = deterministic_jitter_seconds(str(policy.get("policyId") or ""), slot.slot_key, int(schedule_cfg.get("jitterSeconds") or 0))
    effective = slot.scheduled_for + timedelta(seconds=jitter)
    return {
        "scheduledFor": _utc_iso(effective),
        "localDateTime": slot.local_iso,
        "timezone": slot.timezone,
        "slotKey": slot.slot_key,
        "jitterSeconds": jitter,
    }


def allocate_fencing_token() -> int:
    """Allocate a globally monotonic fencing token for ad-hoc target writers."""
    with _connect() as connection:
        return _next_token(connection, "fencing")


def record_target_health(target_id: str, status: str, detail: str | None = None) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO backup_target_health(target_id, status, checked_at, detail) VALUES (?,?,?,?) ON CONFLICT(target_id) DO UPDATE SET status = excluded.status, checked_at = excluded.checked_at, detail = excluded.detail",
            (target_id, status, _utc_iso(), detail),
        )


def target_health() -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM backup_target_health ORDER BY target_id").fetchall()
        return [{"targetId": row["target_id"], "status": row["status"], "checkedAt": row["checked_at"], "detail": row["detail"]} for row in rows]


def evaluate_write_placement(
    policy: dict[str, Any] | str,
    *,
    client: Any | None = None,
    failback_stability_seconds: int = 1800,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate and freeze write target placement for a backup run (4.5.5).

    Contract (4.5.5):
    1. If configured primary is healthy and accessible:
       - If currently operating in failover mode, evaluate failback governance
         (continuous primary stability >= stability window AND point convergence).
       - If failback eligible, revert to primary; otherwise remain on active failover target.
       - If not in failover, select primary.
    2. If primary is unavailable:
       - Transition to healthy required replica with freshest recovery point.
       - Result has isFailover=True, forceFull=True.
    """
    from deepseek_infra.infra.workspace import backup_policies, backup_publish, backup_write_continuity

    if isinstance(policy, str):
        policy = backup_policies.get_policy(policy)

    policy_id = str(policy.get("policyId") or "")
    configured_primary = str(policy.get("primaryTargetId") or policy.get("targetId") or "managed-local")
    primary_ok = True
    primary_error: str | None = None

    try:
        p_target = backup_publish.resolve_target(configured_primary)
        if p_target.store is not None:
            caps = p_target.store.capabilities()
            if not caps.scheduled_backup_ready:
                primary_ok = False
                primary_error = "unsupported-conditional-target"
        if primary_ok:
            live = backup_write_continuity.perform_liveness_preflight(configured_primary, policy_id=policy_id, target=p_target)
            if live.get("status") != "available":
                primary_ok = False
                primary_error = live.get("error") or "primary-unreachable"
    except Exception as exc:
        primary_ok = False
        primary_error = str(exc)

    if primary_ok:
        from deepseek_infra.infra.workspace import backup_capacity

        pred_bytes = backup_capacity.predict_next_backup_bytes(policy_id)
        if pred_bytes is not None:
            admitted, adm_reason = backup_capacity.check_target_capacity_admission(configured_primary, pred_bytes, policy=policy)
            if not admitted:
                primary_ok = False
                primary_error = f"primary-capacity-admission-failed: {adm_reason}"

    if primary_ok:
        record_target_health(configured_primary, "healthy", None)
        continuity = backup_write_continuity.get_write_continuity_state(policy_id)
        active_id = str(continuity.get("activeWriteTargetId") or configured_primary)

        if active_id != configured_primary and continuity.get("activeWriteTargetRole") == "failover":
            # Check failback governance
            eligible, fb_reason, _ = backup_write_continuity.evaluate_failback_eligibility(
                policy_id,
                stability_window_seconds=failback_stability_seconds,
                now=now,
            )
            if eligible:
                backup_write_continuity.execute_failback_transition(policy_id)
                return {
                    "configuredPrimaryTargetId": configured_primary,
                    "selectedWriteTargetId": configured_primary,
                    "isFailover": False,
                    "forceFull": False,
                    "reason": "governed-failback-reverted",
                    "candidateTargetIds": [configured_primary],
                }
            else:
                return {
                    "configuredPrimaryTargetId": configured_primary,
                    "selectedWriteTargetId": active_id,
                    "isFailover": True,
                    "forceFull": True,
                    "reason": f"failover-active-awaiting-failback-governance: {fb_reason}",
                    "candidateTargetIds": [configured_primary, active_id],
                }

        return {
            "configuredPrimaryTargetId": configured_primary,
            "selectedWriteTargetId": configured_primary,
            "isFailover": False,
            "forceFull": False,
            "reason": "primary-healthy",
            "candidateTargetIds": [configured_primary],
        }

    # Primary is unavailable
    record_target_health(configured_primary, "blocked", primary_error)
    backup_write_continuity.record_target_liveness(
        policy_id,
        configured_primary,
        status="unavailable",
        error=primary_error,
    )

    replication = policy.get("replication")
    rep_dict = replication if isinstance(replication, dict) else {}
    targets = list(rep_dict.get("targets") or []) if rep_dict.get("enabled") else []
    candidates: list[str] = []

    from deepseek_infra.infra.workspace import backup_targets
    all_target_records = {t["targetId"]: t for t in backup_targets.list_targets()}

    for t_entry in targets:
        if not isinstance(t_entry, dict):
            continue
        tid = str(t_entry.get("targetId") or "").strip()
        mode = str(t_entry.get("mode") or "required")
        if mode != "required" or not tid or tid == configured_primary:
            continue
        # Filter out draining/drained targets
        t_meta = all_target_records.get(tid) or {}
        if t_meta.get("drainState") in {"draining", "drained"}:
            continue

        try:
            r_target = backup_publish.resolve_target(tid)
            if r_target.store is not None:
                caps = r_target.store.capabilities()
                if not caps.scheduled_backup_ready:
                    continue
            live = backup_write_continuity.perform_liveness_preflight(tid, policy_id=policy_id, target=r_target)
            if live.get("status") == "available":
                candidates.append(tid)
        except Exception:
            continue

    if not candidates:
        return {
            "configuredPrimaryTargetId": configured_primary,
            "selectedWriteTargetId": configured_primary,
            "isFailover": False,
            "forceFull": False,
            "reason": f"primary-unavailable-no-replicas: {primary_error}",
            "candidateTargetIds": [configured_primary],
        }

    # Rank candidates using deterministic placement planner
    scored_candidates = plan_target_placement(
        policy,
        candidate_target_ids=candidates,
        primary_target_id=configured_primary,
        snapshot_kind="full",
        # This is placement for the next Full recovery point. Existing copies
        # belong to the parent/history and must not disqualify the very replica
        # that is eligible to receive the failover write.
        logical_recovery_point_id=None,
        required_bytes=None,
        force_full=True,
    )

    if not scored_candidates:
        return {
            "configuredPrimaryTargetId": configured_primary,
            "selectedWriteTargetId": configured_primary,
            "isFailover": False,
            "forceFull": False,
            "reason": f"primary-unavailable-no-eligible-candidates: {primary_error}",
            "candidateTargetIds": [configured_primary],
        }

    selected_replica = scored_candidates[0][1]

    # Record failover in write continuity state
    backup_write_continuity.execute_failover_transition(
        policy_id,
        selected_replica,
        reason=f"primary-unavailable: {primary_error}",
    )

    return {
        "configuredPrimaryTargetId": configured_primary,
        "selectedWriteTargetId": selected_replica,
        "isFailover": True,
        "forceFull": True,
        "reason": f"primary-unavailable: {primary_error}",
        "candidateTargetIds": [configured_primary] + [c[1] for c in scored_candidates],
    }


def plan_target_placement(
    policy: dict[str, Any],
    *,
    candidate_target_ids: list[str],
    primary_target_id: str,
    snapshot_kind: str = "full",
    logical_recovery_point_id: str | None,
    required_bytes: int | None = None,
    force_full: bool = False,
) -> list[tuple[tuple[Any, ...], str]]:
    """Rank targets against one explicit logical recovery point replica set.

    Ranking Criteria (tuple order):
    1. Recoverability / replica point lag ascending
    2. Failure-domain and region diversity gain
    3. Capacity headroom descending
    4. Matching RecoveryClass RTO evidence ascending
    5. Operator-supplied storage / egress estimate ascending
    6. Target priority descending and lexical targetId tie-break
    """
    from deepseek_infra.infra.workspace import (
        backup_capacity,
        backup_dr_ledger,
        backup_recovery_class,
        backup_replication,
        backup_targets,
    )

    policy_id = str(policy.get("policyId") or "")
    all_target_records = {t["targetId"]: t for t in backup_targets.list_targets()}
    replication = policy.get("replication") or {}
    placement = policy.get("placement") or {}
    max_copies_per_fd = placement.get("maxCopiesPerFailureDomain") or replication.get("maxCopiesPerFailureDomain")
    min_failure_domains = int(replication.get("minFailureDomains") or 1)
    min_regions = int(replication.get("minRegions") or 1)

    existing_copies = (
        backup_dr_ledger.list_logical_recovery_copies(logical_id=logical_recovery_point_id, limit=100)
        if logical_recovery_point_id
        else []
    )
    healthy_copies = [c for c in existing_copies if c.get("recoverable") and c.get("state") == "healthy"]
    existing_fds = {
        str((all_target_records.get(str(c.get("targetId"))) or {}).get("failureDomain") or "default")
        for c in healthy_copies
    }
    existing_regions = {
        str((all_target_records.get(str(c.get("targetId"))) or {}).get("region") or "default-region")
        for c in healthy_copies
    }
    existing_target_ids = {str(c.get("targetId") or "") for c in healthy_copies}

    prediction = backup_capacity.predict_next_backup_size(policy_id, snapshot_kind=snapshot_kind)
    predicted_value = prediction.get("predictedBytes")
    predicted_bytes = required_bytes if required_bytes is not None else (
        int(predicted_value) if isinstance(predicted_value, int) and not isinstance(predicted_value, bool) else None
    )
    recovery_objectives = policy.get("recoveryObjectives") or {}
    max_rto_seconds = recovery_objectives.get("maxRtoSeconds")
    cost_objectives = policy.get("costObjectives") or {}
    max_storage_cost = cost_objectives.get("maxEstimatedMonthlyStorageUsd")
    max_egress_cost = cost_objectives.get("maxEstimatedMonthlyEgressUsd")

    scored: list[tuple[tuple[Any, ...], str]] = []
    for tid in candidate_target_ids:
        t_meta = all_target_records.get(tid) or {}
        if t_meta.get("drainState") in {"draining", "drained"}:
            continue
        if tid in existing_target_ids:
            continue

        cand_fd = str(t_meta.get("failureDomain") or "default")
        cand_region = str(t_meta.get("region") or "default-region")

        # 1. Check maxCopiesPerFailureDomain constraint
        existing_in_fd = sum(
            1 for c in healthy_copies
            if str((all_target_records.get(str(c.get("targetId"))) or {}).get("failureDomain") or "default") == cand_fd
        )
        if max_copies_per_fd is not None and int(max_copies_per_fd) > 0:
            if existing_in_fd + 1 > int(max_copies_per_fd):
                continue

        # While a current replica set is below a diversity objective, only a
        # candidate that advances that objective is eligible.
        if len(existing_fds) < min_failure_domains and cand_fd in existing_fds:
            continue
        if len(existing_regions) < min_regions and cand_region in existing_regions:
            continue

        # 2. Check capacity admission
        admitted, _ = backup_capacity.check_target_capacity_admission(
            tid,
            predicted_bytes,
            policy=policy,
            force_full=force_full,
        )
        if not admitted:
            continue

        # Compute score components
        fd_gain = -1 if cand_fd not in existing_fds else 0
        region_gain = -1 if cand_region not in existing_regions else 0

        lag_info = backup_replication.calculate_replica_lag(policy_id, tid, primary_target_id=primary_target_id)
        lag_pts = int(lag_info.get("lagRecoveryPoints") or 0)

        priority = int(t_meta.get("priority") or 0)

        cap = backup_targets.probe_target_capacity(tid)
        free_pct = float(cap.get("freePercent") or 100.0)
        cap_score = -free_pct  # higher free% -> more negative -> ranked higher

        rto_p90 = 0
        if max_rto_seconds is not None:
            if predicted_bytes is None:
                continue
            recovery_class = backup_recovery_class.classify_recovery(
                target_kind=str(t_meta.get("kind") or "filesystem"),
                format_kind="object-set-v1",
                logical_bytes=predicted_bytes,
                chain_length=1,
                storage_protocol="s3" if t_meta.get("kind") == "s3" else "local",
            )
            rto = backup_recovery_class.calibrate_rto(
                target_id=tid,
                logical_bytes=predicted_bytes,
                recovery_class=recovery_class,
            )
            if rto.get("status") != "calibrated":
                continue
            rto_p90 = int(rto.get("p90Seconds") or 0)
            if rto_p90 <= 0 or rto_p90 > int(max_rto_seconds):
                continue

        cost = backup_capacity.estimate_transfer_cost(
            predicted_bytes or 0,
            source_target_id=primary_target_id,
            dest_target_id=tid,
        )
        require_known_rates = bool((policy.get("costObjectives") or {}).get("requireKnownRates"))
        if require_known_rates and str(cost.get("costStatus") or "") == "unavailable":
            continue
        storage_delta = cost.get("estimatedMonthlyStorageCostDelta")
        egress_delta = cost.get("estimatedOneTimeTransferCost")
        monthly_storage_cost = float(storage_delta) if isinstance(storage_delta, (int, float)) and not isinstance(storage_delta, bool) else 0.0
        estimated_egress_cost = float(egress_delta) if isinstance(egress_delta, (int, float)) and not isinstance(egress_delta, bool) else 0.0
        # Unknown economics must not silently win on a zero default when budgets exist.
        if str(cost.get("costStatus") or "") == "unavailable" and (max_storage_cost is not None or max_egress_cost is not None):
            continue
        if max_storage_cost is not None and monthly_storage_cost > float(max_storage_cost):
            continue
        if max_egress_cost is not None and estimated_egress_cost > float(max_egress_cost):
            continue

        copies = [c for c in healthy_copies if str(c.get("targetId")) == tid]
        healthy_count = len(copies)

        score_tuple = (
            lag_pts,
            fd_gain,
            region_gain,
            cap_score,
            rto_p90,
            monthly_storage_cost + estimated_egress_cost,
            -priority,
            -healthy_count,
            tid,
        )
        scored.append((score_tuple, tid))

    scored.sort()
    return scored



def record_retention_run(retention_run_id: str, *, policy_id: str, target_id: str, status: str, preview: dict[str, Any] | None = None) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO retention_runs(retention_run_id, policy_id, target_id, status, preview, created_at, updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(retention_run_id) DO UPDATE SET status = excluded.status, preview = excluded.preview, updated_at = excluded.updated_at",
            (retention_run_id, policy_id, target_id, status, json.dumps(preview or {}, ensure_ascii=False, sort_keys=True), _utc_iso(), _utc_iso()),
        )


def staging_root() -> Path:
    root = BACKUP_SCHEDULER_DIR / "staging"
    root.mkdir(parents=True, exist_ok=True)
    return root


def cleanup_run_staging(run_id: str) -> None:
    import shutil

    shutil.rmtree(staging_root() / run_id, ignore_errors=True)


def scheduler_thread_name(instance_id: str) -> str:
    return f"backup-worker-{instance_id[:8]}"


def instance_id_from_environment() -> str:
    return os.environ.get("DEEPSEEK_BACKUP_INSTANCE_ID") or f"instance_{uuid.uuid4().hex[:12]}"


def claim_due_drill_slots(
    policies: list[dict[str, Any]],
    *,
    instance_id: str,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Claim durable recovery-drill schedule slots (separate from backup slots).

    Slot keys are prefixed with ``recovery-drill/`` so they never collide with
    backup runs. UNIQUE(policy_id, slot_key) prevents duplicate execution across
    workers and process restarts.
    """
    del instance_id
    current = now or datetime.now(tz=timezone.utc)
    claimed: list[dict[str, Any]] = []
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for policy in policies:
            drill_cfg = policy.get("recoveryDrill") if isinstance(policy.get("recoveryDrill"), dict) else {}
            if not drill_cfg or not drill_cfg.get("enabled"):
                continue
            cron_text = str(drill_cfg.get("cron") or "").strip()
            if not cron_text:
                continue
            policy_id = str(policy.get("policyId") or "")
            try:
                schedule = parse_cron(cron_text)
            except AppError:
                continue
            # Prefer schedule timezone; fall back to policy schedule timezone then UTC.
            timezone_name = str(
                drill_cfg.get("timezone")
                or (policy.get("schedule") or {}).get("timezone")
                or "UTC"
            )
            catchup = 86400
            # Prefer last recovery-drill slot as iteration anchor
            catchup_start = current - timedelta(seconds=catchup)
            iter_start = catchup_start
            drill_latest = connection.execute(
                "SELECT * FROM backup_schedule_slots WHERE policy_id = ? AND slot_key LIKE 'recovery-drill/%' ORDER BY scheduled_for DESC LIMIT 1",
                (policy_id,),
            ).fetchone()
            if drill_latest is not None:
                parsed = _parse_iso(str(drill_latest["scheduled_for"]))
                if parsed is not None:
                    iter_start = parsed
            slots = list(
                iter_slots(
                    schedule,
                    timezone_name,
                    start_utc=iter_start,
                    end_utc=current + timedelta(seconds=1),
                    misfire_policy="skip",
                )
            )
            due = [slot for slot in slots if slot.scheduled_for <= current]
            if not due:
                continue
            slot = due[-1]
            drill_slot_key = f"recovery-drill/{slot.slot_key}"
            if _slot_recorded(connection, policy_id, drill_slot_key):
                continue
            if slot.scheduled_for < catchup_start:
                _record_skipped_slot(
                    connection,
                    policy_id,
                    type("S", (), {"slot_key": drill_slot_key, "scheduled_for": slot.scheduled_for, "local_iso": slot.local_iso, "timezone": slot.timezone})(),
                    "outside-catchup-window",
                )
                continue
            cursor = connection.execute(
                "INSERT OR IGNORE INTO backup_schedule_slots(policy_id, slot_key, scheduled_for, local_date_time, timezone, status, run_id, created_at) VALUES (?,?,?,?,?,'claimed',?,?)",
                (policy_id, drill_slot_key, _utc_iso(slot.scheduled_for), slot.local_iso, slot.timezone, f"drill_{uuid.uuid4().hex[:12]}", _utc_iso()),
            )
            if cursor.rowcount == 0:
                continue
            claimed.append(
                {
                    "policyId": policy_id,
                    "slotKey": drill_slot_key,
                    "scheduledFor": _utc_iso(slot.scheduled_for),
                    "timezone": timezone_name,
                }
            )
    return claimed


def worker_tick(
    *,
    instance_id: str,
    executor: Any,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict[str, int]:
    """One scheduler tick: reclaim abandoned runs, claim due slots, execute."""
    current = now or datetime.now(tz=timezone.utc)
    policies = backup_policies.enabled_policies()
    reclaimed = reclaim_abandoned_slots(instance_id=instance_id, now=current, lease_seconds=lease_seconds)
    blocked = reclaim_blocked_slots(policies, instance_id=instance_id, now=current, lease_seconds=lease_seconds)
    deferred = reclaim_deferred_slots(policies, instance_id=instance_id, now=current, lease_seconds=lease_seconds)
    claimed = claim_due_slots(policies, instance_id=instance_id, now=current, lease_seconds=lease_seconds)
    executed = 0
    for run in [*reclaimed, *blocked, *deferred, *claimed]:
        executor(run)
        executed += 1
    # Durable recovery-drill slots (bounded, no unbounded timers)
    drill_claimed = claim_due_drill_slots(policies, instance_id=instance_id, now=current)
    drills_executed = 0
    if drill_claimed:
        try:
            from deepseek_infra.infra.workspace import backup_recovery_drill

            for item in drill_claimed:
                try:
                    backup_recovery_drill.execute_scheduled_drill(str(item["policyId"]))
                    drills_executed += 1
                except Exception:
                    _logger.exception("scheduled recovery drill failed", extra={"policyId": item.get("policyId")})
        except Exception:
            _logger.exception("scheduled recovery drill driver failed")
    # Advance all storage-control maintenance queues (bounded and cross-process leased).
    repl_processed = 0
    maintenance_processed = 0
    try:
        from deepseek_infra.infra.workspace import backup_maintenance

        maintenance = backup_maintenance.maintenance_tick(instance_id=instance_id, limit_per_worker=5)
        replication_value = maintenance.get("replication")
        replication: dict[str, Any] = replication_value if isinstance(replication_value, dict) else {}
        repl_processed = int(replication.get("processed") or 0)
        maintenance_processed = int(maintenance.get("drainsProcessed") or 0)
        for key in ("replication", "repairs", "rebalances", "retirements"):
            worker_summary = maintenance.get(key)
            if isinstance(worker_summary, dict):
                maintenance_processed += int(worker_summary.get("processed") or 0)
    except Exception:
        pass
    return {
        "reclaimed": len(reclaimed),
        "blocked": len(blocked),
        "deferred": len(deferred),
        "claimed": len(claimed),
        "executed": executed,
        "drillsClaimed": len(drill_claimed),
        "drillsExecuted": drills_executed,
        "replicationProcessed": repl_processed,
        "maintenanceProcessed": maintenance_processed,
    }


class BackupWorker:
    """Embedded worker loop; also used by ``python -m deepseek_infra.backup_worker``."""

    def __init__(self, executor: Any, *, instance_id: str | None = None, tick_seconds: float = 30.0, lease_seconds: int = DEFAULT_LEASE_SECONDS, reconcile_on_start: bool = True) -> None:
        self.instance_id = instance_id or instance_id_from_environment()
        self.tick_seconds = tick_seconds
        self.lease_seconds = lease_seconds
        self.reconcile_on_start = reconcile_on_start
        self.tick_failures = 0
        self._executor = executor
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            from deepseek_infra.infra.workspace import backup_control

            backup_control.ensure_control_authority_ready()
        except Exception:
            _logger.exception(
                "backup worker control authority startup failed",
                extra={"instanceId": self.instance_id},
            )
        if self.reconcile_on_start:
            try:
                from deepseek_infra.infra.workspace import backup_reconcile

                backup_reconcile.reconcile_all_targets(instance_id=self.instance_id)
            except Exception:
                _logger.exception("backup worker startup reconciliation failed", extra={"instanceId": self.instance_id})
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=scheduler_thread_name(self.instance_id), daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worker_tick(instance_id=self.instance_id, executor=self._executor, lease_seconds=self.lease_seconds)
            except Exception:
                self.tick_failures += 1
                _logger.exception("backup worker tick failed", extra={"instanceId": self.instance_id, "tickFailures": self.tick_failures})
            else:
                self.tick_failures = 0
            self._stop.wait(self.tick_seconds)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

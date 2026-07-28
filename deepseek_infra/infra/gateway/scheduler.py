"""Local request scheduler: priority queue, rate limit, backpressure, retry, DLQ.

The most "traditional infra" layer in the runtime. A single in-process admission
controller sits in front of the upstream chokepoint so that concurrent agents, parallel
tools, flaky mobile networks, provider rate limits and rapid user clicks degrade
gracefully instead of melting down::

    Request Scheduler
    ├── Priority Queue        interactive > agent worker > background
    ├── Rate Limiter          token bucket (requests/sec + burst)
    ├── Backpressure          bounded waiting+in-flight; overflow -> fast 503 (shed)
    ├── Concurrency cap        max in-flight upstream requests (semaphore-like)
    ├── Retry Queue           exponential backoff lives in resiliency.open_with_resiliency
    ├── Dead Letter Queue     durable SQLite table for requests that exhausted retries
    └── Durable SQLite Queue  + background recovery of orphaned rows on boot

The admission path is purely in-memory (no per-request SQLite write) so it is fast and
test-transparent under the generous defaults (``rate_per_second=0`` = unlimited,
``max_concurrency=16``, ``max_queue_depth=256``). Only the *rare* terminal paths —
backpressure shed and retry-exhausted failures — touch the durable dead-letter table.
"""

from __future__ import annotations

import heapq
import itertools
import sqlite3
import threading
import time
import urllib.error
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from deepseek_infra.core.config import (
    GATEWAY_REQUEST_QUEUE_DB,
    SCHEDULER_ACQUIRE_TIMEOUT_SECONDS,
    SCHEDULER_DB,
    SCHEDULER_DIR,
    SCHEDULER_DLQ_ENABLED,
    SCHEDULER_DLQ_MAX_ROWS,
    SCHEDULER_ENABLED,
    SCHEDULER_MAX_CONCURRENCY,
    SCHEDULER_MAX_QUEUE_DEPTH,
    SCHEDULER_ORPHAN_SECONDS,
    SCHEDULER_RATE_BURST,
    SCHEDULER_RATE_PER_SECOND,
)
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import mutation_gate

# Priorities: lower value = higher precedence (admitted first).
PRIORITY_INTERACTIVE = 0
PRIORITY_AGENT = 10
PRIORITY_BACKGROUND = 20

DLQ_TABLE = "scheduler_dead_letters"
REQUEST_QUEUE_TABLE = "request_queue_items"
_INFRA_FAILURE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class SchedulerOverloaded(AppError):
    """Raised when the bounded queue is full and the request is shed (backpressure)."""

    def __init__(self, message: str = "Request scheduler is overloaded; please retry shortly") -> None:
        super().__init__(message, code=ErrorCode.RATE_LIMITED, status=503)


class SchedulerTimeout(AppError):
    """Raised when a request waits past the admission timeout without a free slot."""

    def __init__(self, message: str = "Timed out waiting for a request scheduler slot") -> None:
        super().__init__(message, code=ErrorCode.RATE_LIMITED, status=503)


# --- Rate limiter ---------------------------------------------------------------

class TokenBucket:
    """Classic token bucket. ``rate_per_second <= 0`` means unlimited (always allows)."""

    def __init__(self, rate_per_second: float, burst: int) -> None:
        self.rate = max(0.0, float(rate_per_second))
        self.capacity = max(1, int(burst))
        self.tokens = float(self.capacity)
        self._timestamp = time.monotonic()

    def _refill(self, now: float) -> None:
        if self.rate <= 0:
            return
        elapsed = max(0.0, now - self._timestamp)
        self._timestamp = now
        self.tokens = min(float(self.capacity), self.tokens + elapsed * self.rate)

    def try_take(self, now: float | None = None) -> bool:
        if self.rate <= 0:
            return True
        current = time.monotonic() if now is None else now
        self._refill(current)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def available(self) -> float:
        if self.rate <= 0:
            return float(self.capacity)
        self._refill(time.monotonic())
        return round(self.tokens, 3)


# --- Scheduler ------------------------------------------------------------------

class RequestScheduler:
    """Thread-safe priority admission controller with concurrency + rate + backpressure."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        max_concurrency: int | None = None,
        max_queue_depth: int | None = None,
        rate_per_second: float | None = None,
        rate_burst: int | None = None,
        acquire_timeout_seconds: float | None = None,
    ) -> None:
        self.enabled = SCHEDULER_ENABLED if enabled is None else bool(enabled)
        self.max_concurrency = max(1, int(SCHEDULER_MAX_CONCURRENCY if max_concurrency is None else max_concurrency))
        self.max_queue_depth = max(1, int(SCHEDULER_MAX_QUEUE_DEPTH if max_queue_depth is None else max_queue_depth))
        rate = SCHEDULER_RATE_PER_SECOND if rate_per_second is None else rate_per_second
        burst = SCHEDULER_RATE_BURST if rate_burst is None else rate_burst
        self.acquire_timeout = float(SCHEDULER_ACQUIRE_TIMEOUT_SECONDS if acquire_timeout_seconds is None else acquire_timeout_seconds)
        self._bucket = TokenBucket(rate, int(burst) or self.max_concurrency)
        self._cond = threading.Condition()
        self._seq = itertools.count()
        self._waiters: list[tuple[int, int]] = []
        self._in_flight = 0
        self._waiting = 0
        # cumulative stats
        self._admitted = 0
        self._shed = 0
        self._timed_out = 0
        self._cancelled = 0
        self._rate_waits = 0
        self._peak_in_flight = 0
        self._by_priority: dict[int, int] = {}

    # -- admission ---------------------------------------------------------------

    def _admit(self, priority: int, cancel_checker: Callable[[], None] | None) -> None:
        deadline = time.monotonic() + self.acquire_timeout if self.acquire_timeout > 0 else None
        overloaded = False
        with self._cond:
            if self._waiting + self._in_flight >= self.max_queue_depth:
                self._shed += 1
                overloaded = True
            else:
                ticket = (int(priority), next(self._seq))
                heapq.heappush(self._waiters, ticket)
                self._waiting += 1
                admitted = False
                try:
                    while True:
                        if cancel_checker is not None:
                            try:
                                cancel_checker()
                            except BaseException:
                                self._cancelled += 1
                                raise
                        if self._waiters[0] == ticket and self._in_flight < self.max_concurrency and self._bucket.try_take():
                            heapq.heappop(self._waiters)
                            self._waiting -= 1
                            self._in_flight += 1
                            self._admitted += 1
                            self._by_priority[priority] = self._by_priority.get(priority, 0) + 1
                            self._peak_in_flight = max(self._peak_in_flight, self._in_flight)
                            admitted = True
                            return
                        if self._waiters[0] == ticket and self._in_flight < self.max_concurrency:
                            # at the front with capacity but no rate token yet
                            self._rate_waits += 1
                        if deadline is not None:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                self._timed_out += 1
                                raise SchedulerTimeout()
                            self._cond.wait(min(remaining, 0.05))
                        else:
                            self._cond.wait(0.05)
                finally:
                    if not admitted:
                        try:
                            self._waiters.remove(ticket)
                            heapq.heapify(self._waiters)
                        except ValueError:
                            pass
                        self._waiting -= 1
                        self._cond.notify_all()
        if overloaded:
            record_dead_letter(kind="admission", reason="backpressure_shed", priority=priority)
            raise SchedulerOverloaded()

    def _release(self) -> None:
        with self._cond:
            if self._in_flight > 0:
                self._in_flight -= 1
            self._cond.notify_all()

    @contextmanager
    def lease(
        self,
        priority: int = PRIORITY_INTERACTIVE,
        *,
        kind: str = "request",
        key: str = "",
        cancel_checker: Callable[[], None] | None = None,
    ) -> Iterator[None]:
        """Hold one scheduler slot for the duration of the ``with`` block.

        Enforces (in order) backpressure, priority ordering, concurrency cap and the
        rate limit. On a terminal *infra* failure inside the block, records a dead
        letter. A no-op passthrough when the scheduler is disabled.
        """
        if not self.enabled:
            yield
            return
        self._admit(priority, cancel_checker)
        failure: BaseException | None = None
        try:
            yield
        except BaseException as exc:  # noqa: BLE001 - re-raised; we only observe it for DLQ
            failure = exc
            raise
        finally:
            self._release()
            if failure is not None and _is_infra_failure(failure):
                record_dead_letter(kind=kind, key=key, reason=_failure_reason(failure), priority=priority)

    # -- introspection -----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._cond:
            return {
                "enabled": self.enabled,
                "inFlight": self._in_flight,
                "waiting": self._waiting,
                "maxConcurrency": self.max_concurrency,
                "maxQueueDepth": self.max_queue_depth,
                "ratePerSecond": self._bucket.rate,
                "rateBurst": self._bucket.capacity,
                "availableTokens": self._bucket.available(),
                "admitted": self._admitted,
                "shed": self._shed,
                "timedOut": self._timed_out,
                "cancelled": self._cancelled,
                "rateLimitedWaits": self._rate_waits,
                "peakInFlight": self._peak_in_flight,
                "byPriority": dict(sorted(self._by_priority.items())),
            }


def _is_infra_failure(exc: BaseException) -> bool:
    if isinstance(exc, (SchedulerOverloaded, SchedulerTimeout)):
        return False
    if isinstance(exc, urllib.error.HTTPError):
        return int(getattr(exc, "code", 0) or 0) in _INFRA_FAILURE_STATUS
    if isinstance(exc, urllib.error.URLError):
        return True
    return isinstance(exc, (TimeoutError, ConnectionError))


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    reason = getattr(exc, "reason", None)
    return str(reason or exc.__class__.__name__)[:300]


def priority_for_payload(payload: dict[str, Any]) -> int:
    """Interactive chat outranks agent workers, which outrank background jobs."""
    if not isinstance(payload, dict):
        return PRIORITY_INTERACTIVE
    if payload.get("background") is True:
        return PRIORITY_BACKGROUND
    capability = str(payload.get("capability") or "").strip()
    if capability and capability != "full":
        return PRIORITY_AGENT
    return PRIORITY_INTERACTIVE


# --- Durable dead-letter queue (SQLite) -----------------------------------------

_DLQ_LOCK = threading.RLock()


def _dlq_connect() -> sqlite3.Connection:
    SCHEDULER_DIR.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(SCHEDULER_DB, timeout=10, check_same_thread=False)


def _ensure_dlq_db() -> None:
    with _DLQ_LOCK, _dlq_connect() as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DLQ_TABLE} (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                key TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                priority INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{DLQ_TABLE}_created ON {DLQ_TABLE}(created_at)")


def record_dead_letter(*, kind: str, reason: str, key: str = "", attempts: int = 0, priority: int = 0, detail: str = "") -> None:
    """Append one dead letter (best-effort; never raises into the request path)."""
    if not SCHEDULER_DLQ_ENABLED:
        return
    try:
        with mutation_gate.mutation_scope(root=SCHEDULER_DIR.parent):
            _ensure_dlq_db()
            with _DLQ_LOCK, _dlq_connect() as conn:
                conn.execute(
                    f"INSERT INTO {DLQ_TABLE} (id, kind, key, reason, attempts, priority, created_at, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex, str(kind), str(key), str(reason), int(attempts), int(priority), time.time(), str(detail)[:1000]),
                )
                conn.execute(
                    f"DELETE FROM {DLQ_TABLE} WHERE id NOT IN (SELECT id FROM {DLQ_TABLE} ORDER BY created_at DESC LIMIT ?)",
                    (int(SCHEDULER_DLQ_MAX_ROWS),),
                )
    except (sqlite3.Error, AppError):
        return


def dead_letters(limit: int = 50) -> list[dict[str, Any]]:
    if not SCHEDULER_DB.exists():
        return []
    capped = max(1, min(int(limit or 50), 1000))
    try:
        with _DLQ_LOCK, _dlq_connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT id, kind, key, reason, attempts, priority, created_at FROM {DLQ_TABLE} ORDER BY created_at DESC LIMIT ?",
                (capped,),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "key": row["key"],
            "reason": row["reason"],
            "attempts": int(row["attempts"] or 0),
            "priority": int(row["priority"] or 0),
            "createdAt": float(row["created_at"] or 0.0),
        }
        for row in rows
    ]


def dlq_status() -> dict[str, Any]:
    payload: dict[str, Any] = {"enabled": SCHEDULER_DLQ_ENABLED, "dbPath": str(SCHEDULER_DB), "count": 0, "byReason": {}}
    if not SCHEDULER_DB.exists():
        return payload
    try:
        with _DLQ_LOCK, _dlq_connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM {DLQ_TABLE}").fetchone()
            grouped = conn.execute(f"SELECT reason, COUNT(*) FROM {DLQ_TABLE} GROUP BY reason").fetchall()
    except sqlite3.Error as exc:
        payload["error"] = str(exc)
        return payload
    payload["count"] = int(total[0]) if total else 0
    payload["byReason"] = {str(reason): int(count) for reason, count in grouped}
    payload["recent"] = dead_letters(10)
    return payload


def recover_orphans() -> int:
    """Reconcile the durable request queue on boot: stale running/queued rows from a
    crashed process are marked failed and dead-lettered (background recovery)."""
    if not GATEWAY_REQUEST_QUEUE_DB.exists():
        return 0
    cutoff = time.time() - max(1, int(SCHEDULER_ORPHAN_SECONDS))
    orphans: list[tuple[str, str, int]] = []
    try:
        with mutation_gate.mutation_scope(root=SCHEDULER_DIR.parent):
            with _DLQ_LOCK, sqlite3.connect(GATEWAY_REQUEST_QUEUE_DB, timeout=10) as conn:
                rows = conn.execute(
                    f"SELECT queue_id, kind, attempt_count FROM {REQUEST_QUEUE_TABLE} "
                    "WHERE status IN ('running', 'queued', 'created') AND updated_at < ?",
                    (cutoff,),
                ).fetchall()
                orphans = [(str(r[0]), str(r[1]), int(r[2] or 0)) for r in rows]
                for queue_id, _kind, _attempts in orphans:
                    conn.execute(
                        f"UPDATE {REQUEST_QUEUE_TABLE} SET status='failed', last_error='recovered_on_startup', updated_at=? WHERE queue_id=?",
                        (time.time(), queue_id),
                    )
    except (sqlite3.Error, AppError):
        return 0
    for queue_id, kind, attempts in orphans:
        record_dead_letter(kind=kind or "request", key=queue_id, reason="recovered_on_startup", attempts=attempts)
    return len(orphans)


# --- Module singleton + convenience ---------------------------------------------

_scheduler = RequestScheduler()


def lease(
    priority: int = PRIORITY_INTERACTIVE,
    *,
    kind: str = "request",
    key: str = "",
    cancel_checker: Callable[[], None] | None = None,
) -> Any:
    return _scheduler.lease(priority, kind=kind, key=key, cancel_checker=cancel_checker)


def snapshot() -> dict[str, Any]:
    return _scheduler.snapshot()


def scheduler_status() -> dict[str, Any]:
    return {**_scheduler.snapshot(), "deadLetterQueue": dlq_status()}

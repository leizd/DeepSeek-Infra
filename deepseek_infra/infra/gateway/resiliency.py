"""Gateway request queue and retry helpers."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

from deepseek_infra.core.config import (
    GATEWAY_REQUEST_QUEUE_DB,
    GATEWAY_REQUEST_QUEUE_DIR,
    GATEWAY_REQUEST_QUEUE_ENABLED,
    GATEWAY_REQUEST_QUEUE_INITIAL_BACKOFF_SECONDS,
    GATEWAY_REQUEST_QUEUE_MAX_ATTEMPTS,
    GATEWAY_REQUEST_QUEUE_MAX_BACKOFF_SECONDS,
)
from deepseek_infra.infra.gateway.context_manager import stable_json_dumps

RETRYABLE_HTTP_STATUS = {408, 425, 429, 502, 503, 504}
_DB_LOCK = threading.RLock()


def open_with_resiliency(
    request: urllib.request.Request,
    *,
    timeout: int | float,
    kind: str,
    payload: dict[str, Any],
    diagnostics_callback: Callable[[dict[str, Any]], None] | None = None,
    cancel_checker: Callable[[], None] | None = None,
) -> Any:
    if not GATEWAY_REQUEST_QUEUE_ENABLED:
        response = urllib.request.urlopen(request, timeout=timeout)
        _record_diagnostics(
            diagnostics_callback,
            {"enabled": False, "kind": kind, "status": "succeeded", "attemptCount": 1, "retryCount": 0},
        )
        return response

    max_attempts = max(1, int(GATEWAY_REQUEST_QUEUE_MAX_ATTEMPTS))
    queue_id = _create_item(kind, payload, max_attempts=max_attempts)
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        _check_cancel(cancel_checker)
        _mark_running(queue_id, attempt)
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except Exception as exc:
            last_error = exception_summary(exc)
            retryable = is_retryable_exception(exc)
            if retryable and attempt < max_attempts:
                delay = retry_delay_seconds(attempt)
                _mark_queued(queue_id, attempt, delay, last_error)
                _record_diagnostics(
                    diagnostics_callback,
                    {
                        "enabled": True,
                        "queueId": queue_id,
                        "kind": kind,
                        "status": "queued",
                        "attemptCount": attempt,
                        "retryCount": attempt,
                        "nextAttemptInSeconds": delay,
                        "lastError": last_error,
                    },
                )
                sleep_with_cancel(delay, cancel_checker)
                continue
            _mark_failed(queue_id, attempt, last_error)
            _record_diagnostics(
                diagnostics_callback,
                {
                    "enabled": True,
                    "queueId": queue_id,
                    "kind": kind,
                    "status": "failed",
                    "attemptCount": attempt,
                    "retryCount": max(0, attempt - 1),
                    "retryable": retryable,
                    "lastError": last_error,
                },
            )
            raise
        _mark_succeeded(queue_id, attempt)
        _record_diagnostics(
            diagnostics_callback,
            {
                "enabled": True,
                "queueId": queue_id,
                "kind": kind,
                "status": "succeeded",
                "attemptCount": attempt,
                "retryCount": max(0, attempt - 1),
                "lastError": last_error,
            },
        )
        return response

    raise urllib.error.URLError(last_error or "DeepSeek request queue exhausted")


def request_queue_status() -> dict[str, Any]:
    if not GATEWAY_REQUEST_QUEUE_ENABLED:
        return {
            "enabled": False,
            "dbPath": str(GATEWAY_REQUEST_QUEUE_DB),
            "maxAttempts": int(GATEWAY_REQUEST_QUEUE_MAX_ATTEMPTS),
        }
    try:
        _ensure_db()
        with _connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) FROM request_queue_items GROUP BY status").fetchall()
            latest = conn.execute(
                "SELECT queue_id, kind, status, attempt_count, last_error, updated_at "
                "FROM request_queue_items ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error as exc:
        return {
            "enabled": True,
            "available": False,
            "dbPath": str(GATEWAY_REQUEST_QUEUE_DB),
            "error": str(exc),
            "maxAttempts": int(GATEWAY_REQUEST_QUEUE_MAX_ATTEMPTS),
        }

    counts = {str(status): int(count) for status, count in rows}
    payload: dict[str, Any] = {
        "enabled": True,
        "available": True,
        "dbPath": str(GATEWAY_REQUEST_QUEUE_DB),
        "maxAttempts": int(GATEWAY_REQUEST_QUEUE_MAX_ATTEMPTS),
        "initialBackoffSeconds": float(GATEWAY_REQUEST_QUEUE_INITIAL_BACKOFF_SECONDS),
        "maxBackoffSeconds": float(GATEWAY_REQUEST_QUEUE_MAX_BACKOFF_SECONDS),
        "counts": counts,
    }
    if latest:
        payload["latest"] = {
            "queueId": latest[0],
            "kind": latest[1],
            "status": latest[2],
            "attemptCount": int(latest[3] or 0),
            "lastError": latest[4] or "",
            "updatedAt": float(latest[5] or 0.0),
        }
    return payload


def gateway_status() -> dict[str, Any]:
    from deepseek_infra.core.config import GATEWAY_CONTEXT_MANAGER_ENABLED, GATEWAY_CONTEXT_WINDOW_MESSAGES
    from deepseek_infra.infra.gateway.scheduler import scheduler_status

    return {
        "contextManager": {
            "enabled": bool(GATEWAY_CONTEXT_MANAGER_ENABLED),
            "stableJson": bool(GATEWAY_CONTEXT_MANAGER_ENABLED),
            "toolOrder": "function.name",
            "slidingWindowMessages": int(GATEWAY_CONTEXT_WINDOW_MESSAGES),
        },
        "requestQueue": request_queue_status(),
        "scheduler": scheduler_status(),
    }


def diagnostics_from_attempts(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for index, attempt in enumerate(attempts):
        key = str(attempt.get("queueId") or f"inline-{index}")
        current = grouped.get(key)
        if current is None or int(attempt.get("attemptCount") or 0) >= int(current.get("attemptCount") or 0):
            grouped[key] = dict(attempt)

    final_attempts = list(grouped.values())
    total_attempt_count = sum(max(0, int(item.get("attemptCount") or 0)) for item in final_attempts)
    retry_count = sum(max(0, int(item.get("retryCount") or 0)) for item in final_attempts)
    last = attempts[-1] if attempts else {}
    from deepseek_infra.infra.gateway.scheduler import snapshot as scheduler_snapshot

    return {
        "requestQueueEnabled": bool(GATEWAY_REQUEST_QUEUE_ENABLED),
        "upstreamRequestCount": len(final_attempts),
        "attemptCount": total_attempt_count,
        "retryCount": retry_count,
        "queued": any(item.get("status") == "queued" for item in attempts),
        "lastStatus": last.get("status") or "",
        "lastQueueId": last.get("queueId") or "",
        "lastError": last.get("lastError") or "",
        "scheduler": scheduler_snapshot(),
    }


def diagnostics_with_gateway(diagnostics: dict[str, Any], attempts: list[dict[str, Any]]) -> dict[str, Any]:
    result = dict(diagnostics)
    result["gatewayResiliency"] = diagnostics_from_attempts(attempts)
    return result


def request_payload_summary(body: dict[str, Any], *, stream: bool, budget_key: str, tool_round: int) -> dict[str, Any]:
    raw_messages = body.get("messages")
    messages = raw_messages if isinstance(raw_messages, list) else []
    raw_tools = body.get("tools")
    tools = raw_tools if isinstance(raw_tools, list) else []
    fingerprint_source = {
        "model": body.get("model"),
        "stream": stream,
        "messages": messages,
        "tools": tools,
        "tool_choice": body.get("tool_choice"),
        "budgetKey": budget_key,
        "toolRound": tool_round,
    }
    fingerprint = hashlib.sha256(stable_json_dumps(fingerprint_source).encode("utf-8")).hexdigest()
    return {
        "fingerprint": fingerprint,
        "model": body.get("model"),
        "stream": stream,
        "budgetKey": budget_key,
        "toolRound": tool_round,
        "messageCount": len(messages),
        "toolCount": len(tools),
        "toolChoice": body.get("tool_choice"),
    }


def is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return int(getattr(exc, "code", 0) or 0) in RETRYABLE_HTTP_STATUS
    if isinstance(exc, urllib.error.URLError):
        return True
    return isinstance(exc, (TimeoutError, ConnectionError))


def exception_summary(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}: {exc.reason}"
    reason = getattr(exc, "reason", None)
    return str(reason or exc)


def retry_delay_seconds(attempt: int) -> float:
    initial = max(0.0, float(GATEWAY_REQUEST_QUEUE_INITIAL_BACKOFF_SECONDS))
    maximum = max(0.0, float(GATEWAY_REQUEST_QUEUE_MAX_BACKOFF_SECONDS))
    if initial <= 0 or maximum <= 0:
        return 0.0
    return min(maximum, initial * (2 ** max(0, attempt - 1)))


def sleep_with_cancel(seconds: float, cancel_checker: Callable[[], None] | None = None) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        _check_cancel(cancel_checker)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.25, remaining))


def _record_diagnostics(callback: Callable[[dict[str, Any]], None] | None, payload: dict[str, Any]) -> None:
    if callback is not None:
        callback(payload)


def _check_cancel(cancel_checker: Callable[[], None] | None) -> None:
    if cancel_checker is not None:
        cancel_checker()


def _connect() -> sqlite3.Connection:
    GATEWAY_REQUEST_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(GATEWAY_REQUEST_QUEUE_DB, timeout=10, check_same_thread=False)


def _ensure_db() -> None:
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS request_queue_items (
                queue_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                payload_json TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}',
                last_error TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_request_queue_status ON request_queue_items(status, next_attempt_at)")


def _create_item(kind: str, payload: dict[str, Any], *, max_attempts: int) -> str:
    _ensure_db()
    queue_id = uuid.uuid4().hex
    now = time.time()
    payload_json = stable_json_dumps(payload)
    idempotency_key = str(payload.get("fingerprint") or hashlib.sha256(payload_json.encode("utf-8")).hexdigest())
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO request_queue_items (
                queue_id, kind, status, attempt_count, max_attempts, next_attempt_at,
                created_at, updated_at, payload_json, result_json, last_error, idempotency_key
            )
            VALUES (?, ?, 'created', 0, ?, 0, ?, ?, ?, '{}', '', ?)
            """,
            (queue_id, kind, max_attempts, now, now, payload_json, idempotency_key),
        )
    return queue_id


def _mark_running(queue_id: str, attempt: int) -> None:
    _update_item(queue_id, "running", attempt, next_attempt_at=0, result={}, last_error=None)


def _mark_queued(queue_id: str, attempt: int, delay: float, last_error: str) -> None:
    _update_item(
        queue_id,
        "queued",
        attempt,
        next_attempt_at=time.time() + max(0.0, delay),
        result={"nextAttemptInSeconds": delay},
        last_error=last_error,
    )


def _mark_succeeded(queue_id: str, attempt: int) -> None:
    _update_item(queue_id, "succeeded", attempt, next_attempt_at=0, result={"ok": True}, last_error="")


def _mark_failed(queue_id: str, attempt: int, last_error: str) -> None:
    _update_item(queue_id, "failed", attempt, next_attempt_at=0, result={"ok": False}, last_error=last_error)


def _update_item(
    queue_id: str,
    status: str,
    attempt: int,
    *,
    next_attempt_at: float,
    result: dict[str, Any],
    last_error: str | None,
) -> None:
    with _DB_LOCK, _connect() as conn:
        if last_error is None:
            conn.execute(
                """
                UPDATE request_queue_items
                SET status = ?, attempt_count = ?, next_attempt_at = ?, updated_at = ?, result_json = ?
                WHERE queue_id = ?
                """,
                (status, attempt, next_attempt_at, time.time(), stable_json_dumps(result), queue_id),
            )
        else:
            conn.execute(
                """
                UPDATE request_queue_items
                SET status = ?, attempt_count = ?, next_attempt_at = ?, updated_at = ?, result_json = ?, last_error = ?
                WHERE queue_id = ?
                """,
                (status, attempt, next_attempt_at, time.time(), stable_json_dumps(result), last_error, queue_id),
            )

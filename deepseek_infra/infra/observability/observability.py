"""Local trace storage for chat and multi-agent calls."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.core.config import TRACE_DB, TRACE_DIR, TRACE_ENABLED, TRACE_INPUT_CHARS, TRACE_LIST_LIMIT, TRACE_OUTPUT_CHARS

logger = logging.getLogger("deepseek_infra.observability")

TRACE_RUNS_TABLE = "trace_runs"
TRACE_SPANS_TABLE = "trace_spans"
SENSITIVE_KEYS = {"apiKey", "api_key", "authorization", "tavilyApiKey", "token", "auth_token"}

_db_lock = threading.RLock()
_last_error = ""


@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: str
    created: bool


@dataclass(slots=True)
class TraceSpan:
    trace_id: str
    span_id: str
    name: str
    kind: str
    input_data: Any = None
    parent_span_id: str = ""
    started_epoch: float = 0.0
    started_monotonic: float = 0.0

    def finish(
        self,
        *,
        status: str = "ok",
        output_data: Any = None,
        usage: dict[str, Any] | None = None,
        diagnostics: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        if not self.trace_id or not self.span_id:
            return
        record_span(
            trace_id=self.trace_id,
            span_id=self.span_id,
            name=self.name,
            kind=self.kind,
            status=status,
            started_epoch=self.started_epoch,
            started_monotonic=self.started_monotonic,
            input_data=self.input_data,
            output_data=output_data,
            usage=usage,
            diagnostics=diagnostics,
            error=error,
            parent_span_id=self.parent_span_id,
        )


def ensure_trace(
    payload: dict[str, Any],
    *,
    kind: str,
    title: str = "",
    metadata: dict[str, Any] | None = None,
) -> TraceContext:
    if not TRACE_ENABLED:
        return TraceContext("", False)
    existing = str(payload.get("traceId") or "").strip()
    if existing:
        ensure_run(existing, kind=kind, title=title, metadata=metadata)
        return TraceContext(existing, False)
    trace_id = start_trace(kind=kind, title=title, metadata=metadata)
    if trace_id:
        payload["traceId"] = trace_id
    return TraceContext(trace_id, bool(trace_id))


def start_trace(*, kind: str, title: str = "", metadata: dict[str, Any] | None = None) -> str:
    if not TRACE_ENABLED:
        return ""
    trace_id = uuid.uuid4().hex
    ensure_run(trace_id, kind=kind, title=title, metadata=metadata)
    return trace_id


def ensure_run(trace_id: str, *, kind: str, title: str = "", metadata: dict[str, Any] | None = None) -> None:
    if not TRACE_ENABLED or not trace_id:
        return
    started_epoch = time.time()
    try:
        with _db_lock, connect_db() as conn:
            initialize_schema(conn)
            conn.execute(
                f"""
                INSERT OR IGNORE INTO {TRACE_RUNS_TABLE}
                    (trace_id, kind, title, status, started_at, started_epoch, completed_at, completed_epoch, duration_ms, metadata, error)
                VALUES (?, ?, ?, 'running', ?, ?, '', NULL, 0, ?, '')
                """,
                (
                    trace_id,
                    str(kind or "chat")[:64],
                    clip_text(str(title or ""), 240),
                    iso_from_epoch(started_epoch),
                    started_epoch,
                    encode_json(metadata or {}, limit=TRACE_INPUT_CHARS),
                ),
            )
            if title or metadata:
                conn.execute(
                    f"""
                    UPDATE {TRACE_RUNS_TABLE}
                    SET title = CASE WHEN title = '' THEN ? ELSE title END,
                        metadata = CASE WHEN metadata = '{{}}' THEN ? ELSE metadata END
                    WHERE trace_id = ?
                    """,
                    (clip_text(str(title or ""), 240), encode_json(metadata or {}, limit=TRACE_INPUT_CHARS), trace_id),
                )
    except Exception as exc:
        set_last_error(f"trace run init failed: {exc}")


def start_span(trace_id: str, *, name: str, kind: str, input_data: Any = None, parent_span_id: str = "") -> TraceSpan:
    if not TRACE_ENABLED or not trace_id:
        return TraceSpan("", "", "", "")
    return TraceSpan(
        trace_id=trace_id,
        span_id=uuid.uuid4().hex,
        name=str(name or kind or "span")[:120],
        kind=str(kind or "span")[:64],
        input_data=input_data,
        parent_span_id=str(parent_span_id or ""),
        started_epoch=time.time(),
        started_monotonic=time.monotonic(),
    )


def finish_trace(trace_id: str, *, status: str = "completed", metadata: dict[str, Any] | None = None, error: str = "") -> None:
    if not TRACE_ENABLED or not trace_id:
        return
    completed_epoch = time.time()
    try:
        with _db_lock, connect_db() as conn:
            initialize_schema(conn)
            row = conn.execute(
                f"SELECT started_epoch, metadata FROM {TRACE_RUNS_TABLE} WHERE trace_id = ?",
                (trace_id,),
            ).fetchone()
            started_epoch = float(row["started_epoch"] or completed_epoch) if row else completed_epoch
            duration_ms = max(0, int((completed_epoch - started_epoch) * 1000))
            decoded_metadata = decode_json(row["metadata"]) if row else {}
            current_metadata = decoded_metadata if isinstance(decoded_metadata, dict) else {}
            if metadata:
                sanitized_metadata = sanitize_value(metadata, limit=TRACE_INPUT_CHARS)
                if isinstance(sanitized_metadata, dict):
                    current_metadata.update(sanitized_metadata)
            conn.execute(
                f"""
                UPDATE {TRACE_RUNS_TABLE}
                SET status = ?, completed_at = ?, completed_epoch = ?, duration_ms = ?, metadata = ?, error = ?
                WHERE trace_id = ?
                """,
                (
                    str(status or "completed")[:32],
                    iso_from_epoch(completed_epoch),
                    completed_epoch,
                    duration_ms,
                    encode_json(current_metadata, limit=TRACE_INPUT_CHARS),
                    clip_text(str(error or ""), TRACE_OUTPUT_CHARS),
                    trace_id,
                ),
            )
    except Exception as exc:
        set_last_error(f"trace finish failed: {exc}")


def record_span(
    *,
    trace_id: str,
    span_id: str,
    name: str,
    kind: str,
    status: str,
    started_epoch: float,
    started_monotonic: float,
    input_data: Any = None,
    output_data: Any = None,
    usage: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
    error: str = "",
    parent_span_id: str = "",
) -> None:
    if not TRACE_ENABLED or not trace_id or not span_id:
        return
    completed_epoch = time.time()
    duration_ms = max(0, int((time.monotonic() - started_monotonic) * 1000)) if started_monotonic else 0
    usage_data = usage if isinstance(usage, dict) else {}
    diagnostics_data = diagnostics if isinstance(diagnostics, dict) else {}
    try:
        with _db_lock, connect_db() as conn:
            initialize_schema(conn)
            conn.execute(
                f"""
                INSERT OR REPLACE INTO {TRACE_SPANS_TABLE}
                    (
                        span_id, trace_id, parent_span_id, name, kind, status,
                        started_at, started_epoch, completed_at, completed_epoch, duration_ms,
                        input_json, output_json, usage_json, diagnostics_json,
                        cache_hit_rate, total_tokens, error
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    span_id,
                    trace_id,
                    parent_span_id,
                    str(name or "span")[:120],
                    str(kind or "span")[:64],
                    str(status or "ok")[:32],
                    iso_from_epoch(started_epoch),
                    started_epoch,
                    iso_from_epoch(completed_epoch),
                    completed_epoch,
                    duration_ms,
                    encode_json(input_data, limit=TRACE_INPUT_CHARS),
                    encode_json(output_data, limit=TRACE_OUTPUT_CHARS),
                    encode_json(usage_data, limit=TRACE_INPUT_CHARS),
                    encode_json(diagnostics_data, limit=TRACE_INPUT_CHARS),
                    cache_hit_rate_from(usage_data, diagnostics_data),
                    usage_int(usage_data, "total_tokens", "totalTokens"),
                    clip_text(str(error or ""), TRACE_OUTPUT_CHARS),
                ),
            )
    except Exception as exc:
        set_last_error(f"trace span write failed: {exc}")


def with_trace_diagnostics(diagnostics: dict[str, Any], trace_id: str) -> dict[str, Any]:
    result = dict(diagnostics)
    if TRACE_ENABLED and trace_id:
        result["traceId"] = trace_id
        result["traceEnabled"] = True
    else:
        result["traceEnabled"] = False
    return result


def trace_status() -> dict[str, Any]:
    if not TRACE_ENABLED:
        return {"enabled": False, "databasePath": str(TRACE_DB), "traceCount": 0, "spanCount": 0, "lastError": _last_error}
    try:
        with _db_lock, connect_db() as conn:
            initialize_schema(conn)
            trace_count = int(conn.execute(f"SELECT COUNT(*) FROM {TRACE_RUNS_TABLE}").fetchone()[0])
            span_count = int(conn.execute(f"SELECT COUNT(*) FROM {TRACE_SPANS_TABLE}").fetchone()[0])
    except Exception as exc:
        set_last_error(f"trace status failed: {exc}")
        trace_count = 0
        span_count = 0
    return {
        "enabled": TRACE_ENABLED,
        "databasePath": str(TRACE_DB),
        "traceCount": trace_count,
        "spanCount": span_count,
        "lastError": _last_error,
    }


def metrics_snapshot() -> dict[str, Any]:
    """Aggregate counters from the local trace store for /metrics and dashboards."""
    snapshot: dict[str, Any] = {
        "enabled": TRACE_ENABLED,
        "runs_total": 0,
        "runs_by_kind": {},
        "error_runs_total": 0,
        "model_calls_total": 0,
        "semantic_cache_checks_total": 0,
        "semantic_cache_hits_total": 0,
        "external_mcp_calls_total": 0,
        "external_mcp_errors_total": 0,
        "external_mcp_latency_ms_avg": 0.0,
        "a2a_tasks_total": 0,
        "a2a_task_errors_total": 0,
        "a2a_task_latency_ms_avg": 0.0,
        "a2a_peer_calls_total": 0,
        "a2a_active_tasks": 0,
        "a2a_stream_disconnects_total": 0,
        "tokens_total": 0,
        "run_latency_ms_avg": 0.0,
        "recovery": {"jobsByPhase": {}, "counters": {}, "stageDuration": {}, "stageThroughput": {}},
    }
    try:
        from deepseek_infra.infra.workspace import backup_recovery_telemetry, backups

        snapshot["recovery"] = backup_recovery_telemetry.metrics_snapshot(backups.RESTORE_DIR)
    except Exception:
        pass
    try:
        from deepseek_infra.infra.agent_runtime.a2a import a2a_status

        runtime = a2a_status()
        snapshot["a2a_active_tasks"] = int(runtime.get("activeTasks") or 0)
        snapshot["a2a_stream_disconnects_total"] = int(runtime.get("streamDisconnectsTotal") or 0)
    except Exception:
        pass
    if not TRACE_ENABLED:
        return snapshot
    try:
        with _db_lock, connect_db() as conn:
            initialize_schema(conn)
            snapshot["runs_total"] = int(conn.execute(f"SELECT COUNT(*) FROM {TRACE_RUNS_TABLE}").fetchone()[0])
            snapshot["runs_by_kind"] = {
                str(kind): int(count)
                for kind, count in conn.execute(f"SELECT kind, COUNT(*) FROM {TRACE_RUNS_TABLE} GROUP BY kind").fetchall()
            }
            snapshot["error_runs_total"] = int(
                conn.execute(f"SELECT COUNT(*) FROM {TRACE_RUNS_TABLE} WHERE status = 'error'").fetchone()[0]
            )
            avg_latency = conn.execute(f"SELECT AVG(duration_ms) FROM {TRACE_RUNS_TABLE}").fetchone()[0]
            snapshot["run_latency_ms_avg"] = round(float(avg_latency), 2) if avg_latency is not None else 0.0
            snapshot["model_calls_total"] = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {TRACE_SPANS_TABLE} "
                    "WHERE kind IN ('deepseek_api', 'deepseek_json', 'deepseek_stream')"
                ).fetchone()[0]
            )
            snapshot["semantic_cache_checks_total"] = int(
                conn.execute(f"SELECT COUNT(*) FROM {TRACE_SPANS_TABLE} WHERE kind = 'semantic_cache'").fetchone()[0]
            )
            snapshot["semantic_cache_hits_total"] = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {TRACE_SPANS_TABLE} WHERE kind = 'semantic_cache' AND status = 'hit'"
                ).fetchone()[0]
            )
            snapshot["external_mcp_calls_total"] = int(
                conn.execute(f"SELECT COUNT(*) FROM {TRACE_SPANS_TABLE} WHERE kind = 'mcp_external'").fetchone()[0]
            )
            snapshot["external_mcp_errors_total"] = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {TRACE_SPANS_TABLE} WHERE kind = 'mcp_external' AND status != 'ok'"
                ).fetchone()[0]
            )
            mcp_avg_latency = conn.execute(
                f"SELECT AVG(duration_ms) FROM {TRACE_SPANS_TABLE} WHERE kind = 'mcp_external'"
            ).fetchone()[0]
            snapshot["external_mcp_latency_ms_avg"] = round(float(mcp_avg_latency), 2) if mcp_avg_latency is not None else 0.0
            snapshot["a2a_tasks_total"] = int(
                conn.execute(f"SELECT COUNT(*) FROM {TRACE_SPANS_TABLE} WHERE kind = 'a2a_task'").fetchone()[0]
            )
            snapshot["a2a_task_errors_total"] = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {TRACE_SPANS_TABLE} WHERE kind = 'a2a_task' AND status != 'ok'"
                ).fetchone()[0]
            )
            a2a_avg_latency = conn.execute(
                f"SELECT AVG(duration_ms) FROM {TRACE_SPANS_TABLE} WHERE kind = 'a2a_task'"
            ).fetchone()[0]
            snapshot["a2a_task_latency_ms_avg"] = round(float(a2a_avg_latency), 2) if a2a_avg_latency is not None else 0.0
            snapshot["a2a_peer_calls_total"] = int(
                conn.execute(f"SELECT COUNT(*) FROM {TRACE_SPANS_TABLE} WHERE kind = 'a2a_peer_call'").fetchone()[0]
            )
            total_tokens = conn.execute(f"SELECT SUM(total_tokens) FROM {TRACE_SPANS_TABLE}").fetchone()[0]
            snapshot["tokens_total"] = int(total_tokens) if total_tokens is not None else 0
    except Exception as exc:
        set_last_error(f"metrics snapshot failed: {exc}")
    return snapshot


def list_traces(limit: int | None = None) -> list[dict[str, Any]]:
    if not TRACE_ENABLED:
        return []
    safe_limit = max(1, min(int(limit or TRACE_LIST_LIMIT), 1_000))
    try:
        with _db_lock, connect_db() as conn:
            initialize_schema(conn)
            rows = conn.execute(
                f"""
                SELECT r.*, COUNT(s.span_id) AS span_count
                FROM {TRACE_RUNS_TABLE} r
                LEFT JOIN {TRACE_SPANS_TABLE} s ON s.trace_id = r.trace_id
                GROUP BY r.trace_id
                ORDER BY r.started_epoch DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
    except Exception as exc:
        set_last_error(f"trace list failed: {exc}")
        return []
    return [public_run(row) for row in rows]


def get_trace(trace_id: str) -> dict[str, Any] | None:
    if not TRACE_ENABLED or not trace_id:
        return None
    try:
        with _db_lock, connect_db() as conn:
            initialize_schema(conn)
            run = conn.execute(f"SELECT * FROM {TRACE_RUNS_TABLE} WHERE trace_id = ?", (trace_id,)).fetchone()
            if run is None:
                return None
            spans = conn.execute(
                f"SELECT * FROM {TRACE_SPANS_TABLE} WHERE trace_id = ? ORDER BY started_epoch ASC",
                (trace_id,),
            ).fetchall()
    except Exception as exc:
        set_last_error(f"trace detail failed: {exc}")
        return None
    started_epoch = float(run["started_epoch"] or 0.0)
    public_spans = [public_span(row, started_epoch=started_epoch) for row in spans]
    return {**public_run(run), "spans": public_spans, "summary": summarize_spans(public_spans)}


def public_run(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "traceId": row["trace_id"],
        "kind": row["kind"],
        "title": row["title"],
        "status": row["status"],
        "startedAt": row["started_at"],
        "completedAt": row["completed_at"],
        "durationMs": int(row["duration_ms"] or 0),
        "metadata": decode_json(row["metadata"]),
        "error": row["error"],
        "spanCount": int(row["span_count"] if "span_count" in row.keys() else 0),
    }


def public_span(row: sqlite3.Row, *, started_epoch: float) -> dict[str, Any]:
    raw_usage = decode_json(row["usage_json"])
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    diagnostics = decode_json(row["diagnostics_json"])
    input_data = decode_json(row["input_json"])
    output_data = decode_json(row["output_json"])
    return {
        "spanId": row["span_id"],
        "traceId": row["trace_id"],
        "parentSpanId": row["parent_span_id"],
        "name": row["name"],
        "kind": row["kind"],
        "status": row["status"],
        "startedAt": row["started_at"],
        "completedAt": row["completed_at"],
        "offsetMs": max(0, int((float(row["started_epoch"] or started_epoch) - started_epoch) * 1000)),
        "durationMs": int(row["duration_ms"] or 0),
        "input": input_data,
        "output": output_data,
        "usage": usage,
        "diagnostics": diagnostics,
        "cacheHitRate": float(row["cache_hit_rate"] or 0.0),
        "totalTokens": int(row["total_tokens"] or usage_int(usage, "total_tokens", "totalTokens")),
        "error": row["error"],
    }


def summarize_spans(spans: list[dict[str, Any]]) -> dict[str, Any]:
    total_duration = sum(int(span.get("durationMs") or 0) for span in spans)
    total_tokens = sum(int(span.get("totalTokens") or 0) for span in spans)
    slowest = max(spans, key=lambda item: int(item.get("durationMs") or 0), default=None)
    return {
        "spanCount": len(spans),
        "totalSpanDurationMs": total_duration,
        "totalTokens": total_tokens,
        "slowestSpan": slowest["name"] if slowest else "",
        "slowestDurationMs": int(slowest.get("durationMs") or 0) if slowest else 0,
    }


def connect_db() -> sqlite3.Connection:
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(TRACE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TRACE_RUNS_TABLE} (
            trace_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            started_epoch REAL NOT NULL,
            completed_at TEXT NOT NULL,
            completed_epoch REAL,
            duration_ms INTEGER NOT NULL,
            metadata TEXT NOT NULL,
            error TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TRACE_SPANS_TABLE} (
            span_id TEXT PRIMARY KEY,
            trace_id TEXT NOT NULL,
            parent_span_id TEXT NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            started_epoch REAL NOT NULL,
            completed_at TEXT NOT NULL,
            completed_epoch REAL NOT NULL,
            duration_ms INTEGER NOT NULL,
            input_json TEXT NOT NULL,
            output_json TEXT NOT NULL,
            usage_json TEXT NOT NULL,
            diagnostics_json TEXT NOT NULL,
            cache_hit_rate REAL NOT NULL,
            total_tokens INTEGER NOT NULL,
            error TEXT NOT NULL
        )
        """
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TRACE_SPANS_TABLE}_trace ON {TRACE_SPANS_TABLE}(trace_id, started_epoch)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TRACE_RUNS_TABLE}_started ON {TRACE_RUNS_TABLE}(started_epoch)")


def encode_json(value: Any, *, limit: int) -> str:
    return json.dumps(sanitize_value(value, limit=limit), ensure_ascii=False, default=str)


def decode_json(value: Any) -> dict[str, Any] | list[Any] | str | int | float | bool | None:
    if value in (None, ""):
        return {}
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return {}


def sanitize_value(value: Any, *, limit: int) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if text_key in SENSITIVE_KEYS or text_key.lower() in SENSITIVE_KEYS:
                result[text_key] = "[redacted]"
                continue
            result[text_key] = sanitize_value(item, limit=limit)
        return result
    if isinstance(value, list):
        return [sanitize_value(item, limit=limit) for item in value[:100]]
    if isinstance(value, tuple):
        return [sanitize_value(item, limit=limit) for item in value[:100]]
    if isinstance(value, str):
        return clip_text(value, limit)
    if value is None or isinstance(value, bool | int | float):
        return value
    return clip_text(str(value), limit)


def clip_text(value: str, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


def cache_hit_rate_from(usage: dict[str, Any], diagnostics: dict[str, Any]) -> float:
    if isinstance(diagnostics, dict) and diagnostics.get("cacheHitRate") is not None:
        try:
            return float(diagnostics["cacheHitRate"])
        except (TypeError, ValueError):
            pass
    hit_tokens = usage_int(usage, "prompt_cache_hit_tokens", "promptCacheHitTokens")
    miss_tokens = usage_int(usage, "prompt_cache_miss_tokens", "promptCacheMissTokens")
    total = hit_tokens + miss_tokens
    return round((hit_tokens / total) * 100, 1) if total else 0.0


def usage_int(usage: dict[str, Any], *names: str) -> int:
    for name in names:
        try:
            return max(0, int(usage.get(name) or 0))
        except (TypeError, ValueError):
            continue
    return 0


def iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def set_last_error(message: str) -> None:
    global _last_error
    _last_error = message
    logger.warning("observability_error", extra={"detail": message})

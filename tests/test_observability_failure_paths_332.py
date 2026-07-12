from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from deepseek_infra.infra.observability import observability


def test_observability_disabled_paths_are_noops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "TRACE_ENABLED", False)
    payload: dict[str, object] = {}
    assert observability.ensure_trace(payload, kind="chat") == observability.TraceContext("", False)
    assert observability.start_trace(kind="chat") == ""
    observability.ensure_run("trace", kind="chat")
    observability.finish_trace("trace")
    observability.record_span(
        trace_id="trace",
        span_id="span",
        name="x",
        kind="test",
        status="ok",
        started_epoch=0,
        started_monotonic=0,
    )
    span = observability.start_span("trace", name="x", kind="test")
    assert span.trace_id == "" and span.span_id == ""
    span.finish()
    assert observability.list_traces() == []
    assert observability.get_trace("trace") is None
    assert observability.trace_status()["enabled"] is False
    assert observability.with_trace_diagnostics({}, "trace") == {"traceEnabled": False}


def test_observability_existing_trace_and_empty_span_finish(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(observability, "ensure_run", lambda trace_id, **_kwargs: calls.append(trace_id))
    payload = {"traceId": " existing "}
    assert observability.ensure_trace(payload, kind="chat") == observability.TraceContext("existing", False)
    assert calls == ["existing"]
    observability.TraceSpan("", "", "x", "test").finish()
    assert observability.with_trace_diagnostics({"ok": True}, "trace") == {"ok": True, "traceId": "trace", "traceEnabled": True}


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("ensure", "trace run init failed"),
        ("finish", "trace finish failed"),
        ("span", "trace span write failed"),
        ("status", "trace status failed"),
        ("metrics", "metrics snapshot failed"),
        ("list", "trace list failed"),
        ("get", "trace detail failed"),
    ],
)
def test_observability_database_failures_are_contained(monkeypatch: pytest.MonkeyPatch, operation: str, expected: str) -> None:
    monkeypatch.setattr(observability, "connect_db", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    observability._last_error = ""
    if operation == "ensure":
        observability.ensure_run("trace", kind="chat")
    elif operation == "finish":
        observability.finish_trace("trace")
    elif operation == "span":
        observability.record_span(
            trace_id="trace",
            span_id="span",
            name="x",
            kind="test",
            status="error",
            started_epoch=1,
            started_monotonic=0,
        )
    elif operation == "status":
        assert observability.trace_status()["traceCount"] == 0
    elif operation == "metrics":
        assert observability.metrics_snapshot()["runs_total"] == 0
    elif operation == "list":
        assert observability.list_traces() == []
    else:
        assert observability.get_trace("trace") is None
    assert expected in observability._last_error


def test_finish_trace_handles_missing_run_and_merges_only_dict_metadata(tmp_settings: Path) -> None:
    observability.finish_trace("missing", metadata={"new": True})
    trace_id = observability.start_trace(kind="chat", metadata={"first": 1})
    observability.finish_trace(trace_id, metadata={"second": 2}, error="x")
    trace = observability.get_trace(trace_id)
    assert trace is not None
    assert trace["metadata"] == {"first": 1, "second": 2}
    assert trace["error"] == "x"


def test_trace_lists_limit_and_missing_detail(tmp_settings: Path) -> None:
    first = observability.start_trace(kind="chat", title="one")
    second = observability.start_trace(kind="agent", title="two")
    observability.finish_trace(first)
    assert len(observability.list_traces(limit=0)) >= 1
    assert len(observability.list_traces(limit=10_000)) == 2
    assert observability.get_trace("") is None
    assert observability.get_trace("missing") is None
    assert observability.get_trace(second) is not None


def test_trace_sanitizers_cover_invalid_json_sensitive_values_and_clipping() -> None:
    assert observability.decode_json(None) == {}
    assert observability.decode_json("not-json") == {}
    assert observability.decode_json("1") == 1
    value = observability.sanitize_value(
        {"apiKey": "secret", "Token": "secret2", "items": ("abcdef", object()), "safe": None},
        limit=3,
    )
    assert value["apiKey"] == "[redacted]"
    assert value["Token"] == "[redacted]"
    assert value["items"][0].startswith("abc...[truncated")
    assert value["safe"] is None
    assert observability.clip_text("short", 10) == "short"
    assert observability.clip_text("longer", 3) == "lon...[truncated 3 chars]"


def test_trace_usage_and_cache_rate_reject_bad_counters() -> None:
    assert observability.cache_hit_rate_from({}, {"cacheHitRate": "bad"}) == 0.0
    assert observability.cache_hit_rate_from({"prompt_cache_hit_tokens": 3, "prompt_cache_miss_tokens": 1}, {}) == 75.0
    assert observability.usage_int({"first": "bad", "second": -5}, "first", "second") == 0
    assert observability.usage_int({"first": "bad"}, "first") == 0
    assert observability.iso_from_epoch(0).startswith("1970-01-01")


def test_trace_public_rows_and_summary_handle_partial_usage() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT 's' span_id, 't' trace_id, '' parent_span_id, 'name' name, 'kind' kind, 'ok' status, "
            "'start' started_at, 3.0 started_epoch, 'end' completed_at, 5 duration_ms, '{}' input_json, '{}' output_json, "
            "'[]' usage_json, '{}' diagnostics_json, 0.0 cache_hit_rate, 0 total_tokens, '' error"
        ).fetchone()
        assert row is not None
        public = observability.public_span(row, started_epoch=5.0)
    finally:
        conn.close()
    assert public["usage"] == {}
    assert public["offsetMs"] == 0
    assert observability.summarize_spans([])["slowestSpan"] == ""
    summary = observability.summarize_spans([{"name": "slow", "durationMs": 5, "totalTokens": 2}])
    assert summary["slowestSpan"] == "slow"


def test_connect_db_initializes_runtime_directory(tmp_settings: Path) -> None:
    connection = observability.connect_db()
    try:
        observability.initialize_schema(connection)
        connection.commit()
    finally:
        connection.close()
    assert observability.TRACE_DB.exists()

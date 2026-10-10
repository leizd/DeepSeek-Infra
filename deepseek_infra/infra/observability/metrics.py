"""Prometheus exposition for the local AI runtime (sourced from the trace store)."""

from __future__ import annotations

from typing import Any

from deepseek_infra.infra.observability.observability import metrics_snapshot
from deepseek_infra.infra.workspace import backup_recovery_telemetry


def _line(name: str, value: float | int, help_text: str, metric_type: str = "counter") -> list[str]:
    return [f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}", f"{name} {value}"]


def _recovery_lines(snapshot: dict[str, Any]) -> list[str]:
    recovery: dict[str, Any] = dict(snapshot["recovery"]) if isinstance(snapshot.get("recovery"), dict) else {}
    jobs: dict[str, Any] = dict(recovery["jobsByPhase"]) if isinstance(recovery.get("jobsByPhase"), dict) else {}
    counters: dict[str, Any] = dict(recovery["counters"]) if isinstance(recovery.get("counters"), dict) else {}
    durations: dict[str, Any] = dict(recovery["stageDuration"]) if isinstance(recovery.get("stageDuration"), dict) else {}
    throughput: dict[str, Any] = dict(recovery["stageThroughput"]) if isinstance(recovery.get("stageThroughput"), dict) else {}
    lines = [
        "# HELP deepseek_recovery_jobs Current retained Recovery Jobs by bounded phase.",
        "# TYPE deepseek_recovery_jobs gauge",
    ]
    for phase, value in sorted(jobs.items()):
        if phase in backup_recovery_telemetry.PHASES:
            lines.append(f'deepseek_recovery_jobs{{phase="{phase}"}} {int(value)}')
    counter_metrics = {
        "transferBytes": ("deepseek_recovery_transferred_bytes_total", "Ciphertext bytes transferred by Recovery Jobs."),
        "componentsTransferred": ("deepseek_recovery_components_transferred_total", "Recovery Components transferred from remote storage."),
        "transferRetry": ("deepseek_recovery_transfer_retries_total", "Recovery Component transfers resumed from a partial."),
        "componentsVerified": ("deepseek_recovery_components_verified_total", "Recovery Components verified."),
        "componentsFailed": ("deepseek_recovery_components_failed_total", "Recovery Components that failed."),
        "cacheHit": ("deepseek_recovery_cache_hits_total", "Verified Recovery cache hits."),
        "cacheMiss": ("deepseek_recovery_cache_misses_total", "Recovery cache misses."),
        "cacheCorruption": ("deepseek_recovery_cache_corruptions_total", "Corrupt Recovery cache entries rejected."),
        "integrityFailure": ("deepseek_recovery_integrity_failures_total", "Recovery integrity failures."),
        "pauseOutcome": ("deepseek_recovery_pauses_total", "Recovery Jobs converged to paused."),
        "abortOutcome": ("deepseek_recovery_aborts_total", "Recovery Jobs converged to aborted or rolled back."),
        "holdRenewalSuccess": ("deepseek_recovery_hold_renewal_success_total", "Successful Recovery hold renewals."),
        "holdRenewalFailure": ("deepseek_recovery_hold_renewal_failures_total", "Failed Recovery hold renewals."),
    }
    for key, (name, help_text) in counter_metrics.items():
        lines += _line(name, int(counters.get(key) or 0), help_text)
    lines += [
        "# HELP deepseek_recovery_stage_duration_seconds Recovery stage duration histogram.",
        "# TYPE deepseek_recovery_stage_duration_seconds histogram",
    ]
    for stage, raw in sorted(durations.items()):
        if stage not in backup_recovery_telemetry.STAGES or not isinstance(raw, dict):
            continue
        buckets: dict[str, Any] = dict(raw["buckets"]) if isinstance(raw.get("buckets"), dict) else {}
        for bound, count in buckets.items():
            le = "+Inf" if bound == "+Inf" else str(float(int(bound)) / 1_000)
            lines.append(f'deepseek_recovery_stage_duration_seconds_bucket{{stage="{stage}",le="{le}"}} {int(count)}')
        lines.append(f'deepseek_recovery_stage_duration_seconds_sum{{stage="{stage}"}} {float(raw.get("sumMs") or 0) / 1_000}')
        lines.append(f'deepseek_recovery_stage_duration_seconds_count{{stage="{stage}"}} {int(raw.get("count") or 0)}')
    lines += [
        "# HELP deepseek_recovery_stage_throughput_bytes_per_second Measured Recovery stage throughput samples.",
        "# TYPE deepseek_recovery_stage_throughput_bytes_per_second summary",
    ]
    for stage, raw in sorted(throughput.items()):
        if stage not in backup_recovery_telemetry.STAGES or not isinstance(raw, dict):
            continue
        lines.append(
            f'deepseek_recovery_stage_throughput_bytes_per_second_sum{{stage="{stage}"}} '
            f'{float(raw.get("sumBytesPerSecond") or 0)}'
        )
        lines.append(
            f'deepseek_recovery_stage_throughput_bytes_per_second_count{{stage="{stage}"}} {int(raw.get("count") or 0)}'
        )
    return lines


def render_prometheus() -> str:
    """Render the current local metrics snapshot as Prometheus text (v0.0.4)."""
    snapshot = metrics_snapshot()
    runs_by_kind = snapshot.get("runs_by_kind") or {}
    lines: list[str] = []
    lines += _line("ai_requests_total", int(snapshot.get("runs_total") or 0), "Total AI runs recorded by the local trace store.")
    lines += _line("ai_agent_runs_total", int(runs_by_kind.get("agent") or 0), "Multi-agent DAG runs.")
    lines += _line("ai_chat_runs_total", int(runs_by_kind.get("chat") or 0), "Single-turn chat runs.")
    lines += _line("ai_edge_runs_total", int(runs_by_kind.get("edge") or 0), "Edge (local model) runs.")
    lines += _line("ai_model_calls_total", int(snapshot.get("model_calls_total") or 0), "Upstream DeepSeek model API calls.")
    lines += _line(
        "ai_semantic_cache_checks_total",
        int(snapshot.get("semantic_cache_checks_total") or 0),
        "Semantic cache lookups performed before model calls.",
    )
    lines += _line("ai_semantic_cache_hits_total", int(snapshot.get("semantic_cache_hits_total") or 0), "Semantic cache hits.")
    lines += _line(
        "ai_external_mcp_calls_total",
        int(snapshot.get("external_mcp_calls_total") or 0),
        "Outbound external MCP tool calls recorded in traces.",
    )
    lines += _line(
        "ai_external_mcp_errors_total",
        int(snapshot.get("external_mcp_errors_total") or 0),
        "Outbound external MCP tool calls that ended in error.",
    )
    lines += _line("ai_a2a_tasks_total", int(snapshot.get("a2a_tasks_total") or 0), "A2A tasks recorded in traces.")
    lines += _line(
        "ai_a2a_task_errors_total",
        int(snapshot.get("a2a_task_errors_total") or 0),
        "A2A tasks that ended in cancellation or error.",
    )
    lines += _line(
        "ai_a2a_peer_calls_total",
        int(snapshot.get("a2a_peer_calls_total") or 0),
        "Outbound A2A peer calls recorded in traces.",
    )
    lines += _line("ai_error_runs_total", int(snapshot.get("error_runs_total") or 0), "Runs that ended in error.")
    lines += _line("ai_tokens_total", int(snapshot.get("tokens_total") or 0), "Total tokens across recorded spans.")
    lines += _line(
        "ai_run_latency_ms_avg",
        float(snapshot.get("run_latency_ms_avg") or 0.0),
        "Average run latency in milliseconds.",
        "gauge",
    )
    lines += _line(
        "ai_external_mcp_latency_ms_avg",
        float(snapshot.get("external_mcp_latency_ms_avg") or 0.0),
        "Average outbound external MCP tool-call latency in milliseconds.",
        "gauge",
    )
    lines += _line(
        "ai_a2a_task_latency_ms_avg",
        float(snapshot.get("a2a_task_latency_ms_avg") or 0.0),
        "Average A2A task latency in milliseconds.",
        "gauge",
    )
    lines += _line("ai_a2a_active_tasks", int(snapshot.get("a2a_active_tasks") or 0), "Currently active A2A tasks.", "gauge")
    lines += _line(
        "ai_a2a_stream_disconnects_total",
        int(snapshot.get("a2a_stream_disconnects_total") or 0),
        "A2A SSE streams closed before task terminal delivery.",
    )
    lines += _line("ai_trace_enabled", 1 if snapshot.get("enabled") else 0, "Whether local tracing is enabled.", "gauge")
    lines += _recovery_lines(snapshot)
    return "\n".join(lines) + "\n"

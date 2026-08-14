"""Bounded, redacted telemetry derived from durable Recovery Job sessions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_STAGE_SAMPLES = 32
DURATION_BUCKETS_MS = (1_000, 5_000, 15_000, 60_000, 300_000)

COUNTER_NAMES = frozenset(
    {
        "transferBytes",
        "componentsTransferred",
        "transferRetry",
        "componentsVerified",
        "componentsFailed",
        "cacheHit",
        "cacheMiss",
        "cacheCorruption",
        "integrityFailure",
        "pauseOutcome",
        "abortOutcome",
        "holdRenewalSuccess",
        "holdRenewalFailure",
    }
)
PHASES = frozenset(
    {
        "created",
        "preflighted",
        "fetching",
        "fetching-chain",
        "chain-fetched",
        "fetched",
        "fetching-controls",
        "controls-fetched",
        "planning-projection",
        "preview-planned",
        "fetching-selected-components",
        "components-fetched",
        "decrypting-controls",
        "decrypting-components",
        "decrypting-chain",
        "materializing",
        "verified",
        "preparing",
        "prepared",
        "committing",
        "complete",
        "paused",
        "aborting",
        "aborted",
        "rolled-back",
        "failed",
        "recovery-required",
        "unknown",
    }
)
STAGES = frozenset({"transfer", "crypto", "materialization", "preflight", "safety-backup", "commit"})
RESULTS = frozenset({"success", "failed", "paused", "aborted", "recovery-required"})
_PENDING_COUNTERS = "_recoveryTelemetryPendingCounters"

def _nonnegative(value: Any) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _phase(value: Any) -> str:
    candidate = str(value or "")
    return candidate if candidate in PHASES else "unknown"


def _stage(value: Any) -> str | None:
    candidate = str(value or "")
    return candidate if candidate in STAGES else None


def _result(value: Any) -> str | None:
    candidate = str(value or "")
    return candidate if candidate in RESULTS else None


def _timestamp(value: Any) -> str | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _component_keys(session: dict[str, Any], state: str) -> frozenset[str]:
    states = session.get("componentStates")
    if not isinstance(states, dict):
        return frozenset()
    return frozenset(
        str(key)
        for key, raw in states.items()
        if isinstance(raw, dict) and str(raw.get("state") or "") == state
    )


def _sanitize_sample(raw: Any) -> dict[str, int | str] | None:
    if not isinstance(raw, dict):
        return None
    stage = _stage(raw.get("stage"))
    result = _result(raw.get("result"))
    if stage is None or result is None:
        return None
    sample: dict[str, int | str] = {
        "sequence": max(1, _nonnegative(raw.get("sequence"))),
        "stage": stage,
        "result": result,
        "durationMs": _nonnegative(raw.get("durationMs")),
        "bytes": _nonnegative(raw.get("bytes")),
        "components": _nonnegative(raw.get("components")),
    }
    observed_at = _timestamp(raw.get("observedAt"))
    if observed_at is not None:
        sample["observedAt"] = observed_at
    return sample


def _sanitize(raw: Any) -> dict[str, Any]:
    value: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    raw_counters: dict[str, Any] = dict(value["counters"]) if isinstance(value.get("counters"), dict) else {}
    counters = {
        name: _nonnegative(raw_counters.get(name))
        for name in sorted(COUNTER_NAMES)
        if _nonnegative(raw_counters.get(name)) > 0
    }
    samples = [sample for item in value.get("samples") or [] if (sample := _sanitize_sample(item)) is not None]
    samples = sorted(samples, key=lambda item: int(item["sequence"]))[-MAX_STAGE_SAMPLES:]
    result: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "counters": counters,
        "samples": samples,
        "currentPhase": _phase(value.get("currentPhase")),
    }
    return result


def increment_counter(session: dict[str, Any], name: str, amount: int = 1) -> None:
    if name not in COUNTER_NAMES:
        raise ValueError(f"invalid recovery telemetry counter: {name}")
    if amount < 0:
        raise ValueError("recovery telemetry counter increment must be non-negative")
    raw_pending = session.get(_PENDING_COUNTERS)
    pending: dict[str, int] = dict(raw_pending) if isinstance(raw_pending, dict) else {}
    pending[name] = _nonnegative(pending.get(name)) + amount
    session[_PENDING_COUNTERS] = pending


def record_stage(
    session: dict[str, Any],
    *,
    stage: str,
    result: str,
    duration_ms: int,
    byte_count: int = 0,
    components: int = 0,
    observed_at: datetime | None = None,
) -> None:
    if stage not in STAGES or result not in RESULTS:
        raise ValueError("invalid recovery telemetry stage sample")
    telemetry = _sanitize(session.get("recoveryTelemetry"))
    samples = telemetry["samples"]
    sequence = max((int(item["sequence"]) for item in samples), default=0) + 1
    observed = _timestamp(observed_at or datetime.now(tz=timezone.utc))
    assert observed is not None
    samples.append(
        {
            "sequence": sequence,
            "stage": stage,
            "result": result,
            "durationMs": max(0, int(duration_ms)),
            "bytes": max(0, int(byte_count)),
            "components": max(0, int(components)),
            "observedAt": observed,
        }
    )
    telemetry["samples"] = samples[-MAX_STAGE_SAMPLES:]
    session["recoveryTelemetry"] = telemetry


def update_for_persist(existing: dict[str, Any], payload: dict[str, Any], *, now: datetime | None = None) -> None:
    """Merge monotonic telemetry and derive bounded state deltas before a write."""
    del now
    old = _sanitize(existing.get("recoveryTelemetry"))
    new = _sanitize(payload.get("recoveryTelemetry"))
    raw_pending = payload.pop(_PENDING_COUNTERS, {})
    pending = dict(raw_pending) if isinstance(raw_pending, dict) else {}
    counters = {
        name: max(_nonnegative(old["counters"].get(name)), _nonnegative(new["counters"].get(name)))
        + _nonnegative(pending.get(name))
        for name in sorted(COUNTER_NAMES)
    }
    counters["componentsVerified"] += len(_component_keys(payload, "verified") - _component_keys(existing, "verified"))
    counters["componentsFailed"] += len(_component_keys(payload, "failed") - _component_keys(existing, "failed"))

    samples_by_sequence = {int(sample["sequence"]): sample for sample in old["samples"]}
    next_sequence = max(samples_by_sequence, default=0) + 1
    for sample in new["samples"]:
        sequence = int(sample["sequence"])
        prior = samples_by_sequence.get(sequence)
        if prior is None:
            samples_by_sequence[sequence] = sample
        elif prior != sample:
            samples_by_sequence[next_sequence] = {**sample, "sequence": next_sequence}
            next_sequence += 1
    merged: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "counters": {name: value for name, value in counters.items() if value > 0},
        "samples": [samples_by_sequence[key] for key in sorted(samples_by_sequence)][-MAX_STAGE_SAMPLES:],
    }
    next_phase = _phase(payload.get("phase"))
    prior_phase = _phase(old.get("currentPhase"))

    if prior_phase != next_phase:
        if next_phase == "paused":
            merged["counters"]["pauseOutcome"] = _nonnegative(merged["counters"].get("pauseOutcome")) + 1
        if next_phase in {"aborted", "rolled-back"}:
            merged["counters"]["abortOutcome"] = _nonnegative(merged["counters"].get("abortOutcome")) + 1
    merged["currentPhase"] = next_phase
    payload["recoveryTelemetry"] = _sanitize(merged)


def _histogram(samples: list[dict[str, int | str]], stage: str) -> dict[str, Any] | None:
    durations = [int(item["durationMs"]) for item in samples if item["stage"] == stage and item["result"] == "success"]
    if not durations:
        return None
    buckets = {str(bound): sum(value <= bound for value in durations) for bound in DURATION_BUCKETS_MS}
    buckets["+Inf"] = len(durations)
    return {"count": len(durations), "sumMs": sum(durations), "buckets": buckets}


def metrics_snapshot(restore_dir: Path) -> dict[str, Any]:
    """Aggregate retained session telemetry without unbounded dimensions."""
    jobs_by_phase: dict[str, int] = {}
    counters: dict[str, int] = {}
    samples: list[dict[str, int | str]] = []
    if restore_dir.is_dir():
        for path in sorted(restore_dir.glob("*/remote-fetch.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            phase = _phase(raw.get("phase"))
            jobs_by_phase[phase] = jobs_by_phase.get(phase, 0) + 1
            telemetry = _sanitize(raw.get("recoveryTelemetry"))
            for name, value in telemetry["counters"].items():
                counters[name] = counters.get(name, 0) + _nonnegative(value)
            samples.extend(telemetry["samples"])
    stage_duration: dict[str, Any] = {}
    stage_throughput: dict[str, Any] = {}
    for stage in sorted(STAGES):
        histogram = _histogram(samples, stage)
        if histogram is not None:
            stage_duration[stage] = histogram
        throughput = [
            (int(item["bytes"]) * 1_000) / int(item["durationMs"])
            for item in samples
            if item["stage"] == stage and item["result"] == "success" and int(item["durationMs"]) > 0 and int(item["bytes"]) > 0
        ]
        if throughput:
            stage_throughput[stage] = {
                "count": len(throughput),
                "sumBytesPerSecond": round(sum(throughput), 3),
            }
    return {
        "jobsByPhase": jobs_by_phase,
        "counters": counters,
        "stageDuration": stage_duration,
        "stageThroughput": stage_throughput,
    }

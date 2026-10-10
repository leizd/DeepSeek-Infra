"""Durable Fleet resilience SLO samples and multi-window error-budget burn."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from deepseek_infra.core import config

LOGGER = logging.getLogger(__name__)

SLO_LEDGER_DIR = config.ROOT / ".resilience-slo"
SLO_LEDGER_DB = SLO_LEDGER_DIR / "slo.sqlite3"

RISK_DETECTION_LATENCY_MS = "risk_detection_latency_ms"
REMEDIATION_QUEUE_DELAY_MS = "remediation_queue_delay_ms"
REPAIR_TIME_MS = "repair_time_ms"
REBALANCE_TIME_MS = "rebalance_time_ms"
RISK_CLEAR_LATENCY_MS = "risk_clear_latency_ms"
DR_READINESS_AGE_HOURS = "dr_readiness_age_hours"
LEASE_TAKEOVER_TIME_MS = "lease_takeover_time_ms"
SCHEDULER_STARVATION_AGE_SECONDS = "scheduler_starvation_age_seconds"
EVIDENCE_FRESHNESS_SECONDS = "evidence_freshness_seconds"

CRITICAL_DURABILITY_RISK_MINUTES = "critical_durability_risk_minutes"
DR_STALE_MINUTES = "dr_stale_minutes"
FAILED_REMEDIATION_RATIO = "failed_remediation_ratio"
QUEUE_DELAY_VIOLATIONS = "queue_delay_violations"

DEFAULT_BURN_CONFIG: dict[str, float] = {
    "fastWindowSeconds": 3600.0,
    "slowWindowSeconds": 86400.0,
    "errorBudgetFraction": 0.01,
    "fastCriticalThreshold": 14.4,
    "slowCriticalThreshold": 6.0,
}

_LOCK = threading.RLock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS resilience_slo_samples (
    sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_key TEXT NOT NULL UNIQUE,
    metric_name TEXT NOT NULL,
    value REAL NOT NULL,
    policy_id TEXT,
    action_id TEXT,
    risk_subject_digest TEXT,
    outcome TEXT,
    metadata_json TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resilience_slo_metric_time
ON resilience_slo_samples(metric_name, observed_at);

CREATE TABLE IF NOT EXISTS resilience_slo_burn_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_key TEXT NOT NULL UNIQUE,
    indicator TEXT NOT NULL,
    bad_units REAL NOT NULL,
    total_units REAL NOT NULL,
    metadata_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resilience_slo_burn_time
ON resilience_slo_burn_observations(indicator, observed_at);
"""


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    SLO_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = sqlite3.connect(SLO_LEDGER_DB, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        burn_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(resilience_slo_burn_observations)")
        }
        if "started_at" not in burn_columns:
            conn.execute("ALTER TABLE resilience_slo_burn_observations ADD COLUMN started_at TEXT")
            conn.execute(
                "UPDATE resilience_slo_burn_observations SET started_at = observed_at WHERE started_at IS NULL"
            )
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def _require_nonnegative_finite(value: float | int, field: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return parsed


def _sample_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "sampleKey": str(row["sample_key"]),
        "metricName": str(row["metric_name"]),
        "value": float(row["value"]),
        "policyId": row["policy_id"],
        "actionId": row["action_id"],
        "riskSubjectDigest": row["risk_subject_digest"],
        "outcome": row["outcome"],
        "metadata": json.loads(str(row["metadata_json"])),
        "observedAt": str(row["observed_at"]),
    }


def record_sample(
    metric_name: str,
    value: float | int,
    *,
    observed_at: datetime | None = None,
    policy_id: str | None = None,
    action_id: str | None = None,
    risk_subject_digest: str | None = None,
    outcome: str | None = None,
    metadata: dict[str, Any] | None = None,
    sample_key: str | None = None,
) -> dict[str, Any]:
    """Idempotently persist one non-negative SLO measurement."""
    name = str(metric_name or "").strip()
    if not name:
        raise ValueError("metric_name is required")
    numeric = _require_nonnegative_finite(value, "value")
    key = str(sample_key or f"sample-{uuid.uuid4().hex}")
    timestamp = _utc_iso(observed_at)
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO resilience_slo_samples (
                sample_key, metric_name, value, policy_id, action_id,
                risk_subject_digest, outcome, metadata_json, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                name,
                numeric,
                policy_id,
                action_id,
                risk_subject_digest,
                outcome,
                metadata_json,
                timestamp,
            ),
        )
        row = conn.execute(
            "SELECT * FROM resilience_slo_samples WHERE sample_key = ?",
            (key,),
        ).fetchone()
        assert row is not None
        sample = _sample_from_row(row)
        expected = {
            "sampleKey": key,
            "metricName": name,
            "value": numeric,
            "policyId": policy_id,
            "actionId": action_id,
            "riskSubjectDigest": risk_subject_digest,
            "outcome": outcome,
            "metadata": json.loads(metadata_json),
            "observedAt": timestamp,
        }
        if sample != expected:
            raise ValueError(f"sample_key conflict: {key}")
        return sample


def try_record_sample(
    metric_name: str,
    value: float | int,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Isolate auxiliary SLO persistence from the source-of-truth control transaction."""
    try:
        return record_sample(metric_name, value, **kwargs)
    except Exception:
        LOGGER.exception("Failed to persist Fleet SLO sample", extra={"sloMetric": metric_name})
        return None


def list_samples(
    metric_name: str | None = None,
    *,
    since: datetime | None = None,
    limit: int = 10000,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if metric_name is not None:
        clauses.append("metric_name = ?")
        params.append(metric_name)
    if since is not None:
        clauses.append("observed_at >= ?")
        params.append(_utc_iso(since))
    query = "SELECT * FROM resilience_slo_samples"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY observed_at DESC, sample_id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 100000)))
    with _connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [_sample_from_row(row) for row in reversed(rows)]


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def _list_recent_samples_per_metric(*, limit_per_metric: int = 10000) -> list[dict[str, Any]]:
    """Bound reads without allowing one high-volume metric to evict another."""
    bounded_limit = max(1, min(int(limit_per_metric), 100000))
    samples: list[dict[str, Any]] = []
    with _connect() as conn:
        metric_rows = conn.execute(
            "SELECT DISTINCT metric_name FROM resilience_slo_samples ORDER BY metric_name"
        ).fetchall()
        for metric_row in metric_rows:
            rows = conn.execute(
                """
                SELECT * FROM resilience_slo_samples
                WHERE metric_name = ?
                ORDER BY observed_at DESC, sample_id DESC LIMIT ?
                """,
                (str(metric_row[0]), bounded_limit),
            ).fetchall()
            samples.extend(_sample_from_row(row) for row in reversed(rows))
    return samples


SLO_WINDOWS: dict[str, float | None] = {
    "1h": 3600.0,
    "24h": 86400.0,
    "7d": 7.0 * 86400.0,
    "30d": 30.0 * 86400.0,
    "lifetime": None,
}


def _sample_in_window(sample: dict[str, Any], *, now: datetime, window_seconds: float | None) -> bool:
    if window_seconds is None:
        return True
    observed = _parse_iso(str(sample.get("observedAt") or ""))
    if observed is None:
        return False
    return (now - observed).total_seconds() <= window_seconds


def _window_metric_summary(items: list[dict[str, Any]], *, now: datetime, window_seconds: float | None) -> dict[str, Any]:
    ordered = [item for item in items if _sample_in_window(item, now=now, window_seconds=window_seconds)]
    values = [float(item["value"]) for item in ordered]
    if not values:
        return {
            "p50": None,
            "p95": None,
            "p99": None,
            "samples": 0,
            "coverageSeconds": 0.0,
            "status": "INSUFFICIENT_DATA",
        }
    first_at = _parse_iso(str(ordered[0]["observedAt"]))
    last_at = _parse_iso(str(ordered[-1]["observedAt"]))
    coverage = 0.0
    if first_at is not None and last_at is not None:
        coverage = max(0.0, (last_at - first_at).total_seconds())
    return {
        "p50": round(_percentile(values, 0.50), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "p99": round(_percentile(values, 0.99), 3),
        "samples": len(values),
        "coverageSeconds": round(coverage, 3),
        "status": "OK",
    }


def get_fleet_slo_snapshot(*, now: datetime | None = None) -> dict[str, Any]:
    """Return stable operator fields plus time-windowed per-metric summaries."""
    current = now or datetime.now(tz=timezone.utc)
    samples = _list_recent_samples_per_metric()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        grouped.setdefault(str(sample["metricName"]), []).append(sample)

    windows: dict[str, dict[str, dict[str, Any]]] = {}
    for window_name, window_seconds in SLO_WINDOWS.items():
        windows[window_name] = {
            metric_name: _window_metric_summary(items, now=current, window_seconds=window_seconds)
            for metric_name, items in grouped.items()
        }

    window_24h = windows["24h"]
    summaries: dict[str, dict[str, Any]] = {}
    for metric_name, items in grouped.items():
        bounded = [item for item in items if _sample_in_window(item, now=current, window_seconds=86400.0)]
        if not bounded:
            continue
        values = [float(item["value"]) for item in bounded]
        summaries[metric_name] = {
            "count": len(values),
            "p50": round(_percentile(values, 0.50), 3),
            "p95": round(_percentile(values, 0.95), 3),
            "latest": round(values[-1], 3),
            "lastObservedAt": bounded[-1]["observedAt"],
        }

    def p95(metric_name: str) -> float:
        windowed = window_24h.get(metric_name) or {}
        if windowed.get("status") == "INSUFFICIENT_DATA":
            return 0.0
        return float(windowed.get("p95") or summaries.get(metric_name, {}).get("p95") or 0.0)

    def latest(metric_name: str) -> float:
        return float(summaries.get(metric_name, {}).get("latest") or 0.0)

    evidence_items = [
        item for item in grouped.get(EVIDENCE_FRESHNESS_SECONDS, []) if _sample_in_window(item, now=current, window_seconds=None)
    ]
    latest_evidence_at = _parse_iso(str(evidence_items[-1]["observedAt"])) if evidence_items else None
    evidence_freshness_seconds: float | None = (
        max(0.0, (current.astimezone(timezone.utc) - latest_evidence_at).total_seconds())
        if latest_evidence_at is not None
        else None
    )

    return {
        "riskDetectionP95Ms": p95(RISK_DETECTION_LATENCY_MS),
        "remediationQueueDelayP95Ms": p95(REMEDIATION_QUEUE_DELAY_MS),
        "repairP95Ms": p95(REPAIR_TIME_MS),
        "rebalanceP95Ms": p95(REBALANCE_TIME_MS),
        "riskClearP95Ms": p95(RISK_CLEAR_LATENCY_MS),
        "drFreshnessHours": latest(DR_READINESS_AGE_HOURS),
        "leaseTakeoverP95Ms": p95(LEASE_TAKEOVER_TIME_MS),
        "schedulerStarvationAgeSeconds": latest(SCHEDULER_STARVATION_AGE_SECONDS),
        "evidenceFreshnessSeconds": (
            round(evidence_freshness_seconds, 3) if evidence_freshness_seconds is not None else None
        ),
        "sampleCounts": {name: int(summary["count"]) for name, summary in summaries.items()},
        "metrics": summaries,
        "windows": windows,
        "evaluatedAt": _utc_iso(current),
    }


def record_burn_observation(
    indicator: str,
    *,
    bad_units: float | int,
    total_units: float | int,
    started_at: datetime | None = None,
    observed_at: datetime | None = None,
    observation_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    name = str(indicator or "").strip()
    if not name:
        raise ValueError("indicator is required")
    bad = _require_nonnegative_finite(bad_units, "bad_units")
    total = _require_nonnegative_finite(total_units, "total_units")
    if bad > total:
        raise ValueError("bad_units cannot exceed total_units")
    key = str(observation_key or f"burn-{uuid.uuid4().hex}")
    current = observed_at or datetime.now(tz=timezone.utc)
    started = started_at or current
    if started > current:
        raise ValueError("started_at cannot be after observed_at")
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
    started_iso = _utc_iso(started)
    observed_iso = _utc_iso(current)
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO resilience_slo_burn_observations (
                observation_key, indicator, bad_units, total_units,
                metadata_json, started_at, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                name,
                bad,
                total,
                metadata_json,
                started_iso,
                observed_iso,
            ),
        )
        row = conn.execute(
            "SELECT * FROM resilience_slo_burn_observations WHERE observation_key = ?",
            (key,),
        ).fetchone()
        assert row is not None
        actual = (
            str(row["indicator"]),
            float(row["bad_units"]),
            float(row["total_units"]),
            json.loads(str(row["metadata_json"])),
            str(row["started_at"] or row["observed_at"]),
            str(row["observed_at"]),
        )
        expected = (name, bad, total, json.loads(metadata_json), started_iso, observed_iso)
        if actual != expected:
            raise ValueError(f"observation_key conflict: {key}")


def try_record_burn_observation(indicator: str, **kwargs: Any) -> bool:
    """Best-effort adapter for control paths whose durable state already committed."""
    try:
        record_burn_observation(indicator, **kwargs)
    except Exception:
        LOGGER.exception(
            "Failed to persist Fleet SLO burn observation",
            extra={"sloIndicator": indicator},
        )
        return False
    return True


def _window_burn(
    conn: sqlite3.Connection,
    *,
    since: datetime,
    until: datetime,
    error_budget_fraction: float,
) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT indicator, bad_units, total_units, started_at, observed_at
        FROM resilience_slo_burn_observations
        WHERE observed_at >= ? AND started_at <= ?
        ORDER BY indicator, observed_at, observation_id
        """,
        (_utc_iso(since), _utc_iso(until)),
    ).fetchall()
    totals: dict[str, list[float]] = {}
    for row in rows:
        started = _parse_iso(str(row["started_at"] or row["observed_at"]))
        observed = _parse_iso(str(row["observed_at"]))
        if started is None or observed is None:
            continue
        if observed > started:
            overlap_start = max(started, since)
            overlap_end = min(observed, until)
            overlap_seconds = max(0.0, (overlap_end - overlap_start).total_seconds())
            fraction = overlap_seconds / (observed - started).total_seconds()
        else:
            fraction = 1.0 if since <= observed <= until else 0.0
        values = totals.setdefault(str(row["indicator"]), [0.0, 0.0])
        values[0] += float(row["bad_units"] or 0.0) * fraction
        values[1] += float(row["total_units"] or 0.0) * fraction
    return {
        indicator: (bad / total / error_budget_fraction) if total > 0 else 0.0
        for indicator, (bad, total) in totals.items()
    }


def compute_burn_rates(
    *,
    now: datetime | None = None,
    config_override: dict[str, float] | None = None,
) -> dict[str, Any]:
    config_values = {**DEFAULT_BURN_CONFIG, **(config_override or {})}
    current = now or datetime.now(tz=timezone.utc)
    fast_window = max(1.0, float(config_values["fastWindowSeconds"]))
    slow_window = max(fast_window, float(config_values["slowWindowSeconds"]))
    budget_fraction = float(config_values["errorBudgetFraction"])
    if not 0 < budget_fraction < 1:
        raise ValueError("errorBudgetFraction must be between 0 and 1")
    with _connect() as conn:
        fast_by_indicator = _window_burn(
            conn,
            since=current - timedelta(seconds=fast_window),
            until=current,
            error_budget_fraction=budget_fraction,
        )
        slow_by_indicator = _window_burn(
            conn,
            since=current - timedelta(seconds=slow_window),
            until=current,
            error_budget_fraction=budget_fraction,
        )
    fast_threshold = float(config_values["fastCriticalThreshold"])
    slow_threshold = float(config_values["slowCriticalThreshold"])
    indicators = sorted(set(fast_by_indicator) | set(slow_by_indicator))
    critical = [
        indicator
        for indicator in indicators
        if fast_by_indicator.get(indicator, 0.0) > fast_threshold
        and slow_by_indicator.get(indicator, 0.0) > slow_threshold
    ]
    return {
        "fast": round(max(fast_by_indicator.values(), default=0.0), 6),
        "slow": round(max(slow_by_indicator.values(), default=0.0), 6),
        "fastWindowSeconds": fast_window,
        "slowWindowSeconds": slow_window,
        "errorBudgetFraction": budget_fraction,
        "fastCriticalThreshold": fast_threshold,
        "slowCriticalThreshold": slow_threshold,
        "byIndicator": {
            indicator: {
                "fast": round(fast_by_indicator.get(indicator, 0.0), 6),
                "slow": round(slow_by_indicator.get(indicator, 0.0), 6),
            }
            for indicator in indicators
        },
        "criticalIndicators": critical,
        "status": "CRITICAL" if critical else "OK",
        "evaluatedAt": _utc_iso(current),
    }


def record_evidence_verification(
    *,
    proof_sha256: str,
    scenario: str,
    verified_at: datetime | None = None,
) -> dict[str, Any]:
    digest = str(proof_sha256 or "").lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("proof_sha256 must be a lowercase SHA-256 hex digest")
    current = verified_at or datetime.now(tz=timezone.utc)
    timestamp = _utc_iso(current)
    return record_sample(
        EVIDENCE_FRESHNESS_SECONDS,
        0.0,
        observed_at=current,
        metadata={"proofSha256": digest, "scenario": str(scenario)},
        sample_key=f"evidence:{scenario}:{digest}:{timestamp}",
    )


def latest_evidence_verification() -> dict[str, Any] | None:
    samples = list_samples(EVIDENCE_FRESHNESS_SECONDS)
    return samples[-1] if samples else None


def milliseconds_between(start: str | None, end: datetime) -> float | None:
    started = _parse_iso(start)
    if started is None:
        return None
    return max(0.0, (end.astimezone(timezone.utc) - started).total_seconds() * 1000.0)

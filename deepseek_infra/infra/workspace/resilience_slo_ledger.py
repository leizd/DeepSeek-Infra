"""Durable Fleet resilience SLO samples and multi-window error-budget burn."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from deepseek_infra.core import config

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
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                timestamp,
            ),
        )
        row = conn.execute(
            "SELECT * FROM resilience_slo_samples WHERE sample_key = ?",
            (key,),
        ).fetchone()
        assert row is not None
        return _sample_from_row(row)


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
    query += " ORDER BY observed_at ASC, sample_id ASC LIMIT ?"
    params.append(max(1, min(int(limit), 100000)))
    with _connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [_sample_from_row(row) for row in rows]


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def get_fleet_slo_snapshot(*, now: datetime | None = None) -> dict[str, Any]:
    """Return stable operator fields plus detailed per-metric summaries."""
    current = now or datetime.now(tz=timezone.utc)
    samples = list_samples()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        grouped.setdefault(str(sample["metricName"]), []).append(sample)

    summaries: dict[str, dict[str, Any]] = {}
    for metric_name, items in grouped.items():
        values = [float(item["value"]) for item in items]
        summaries[metric_name] = {
            "count": len(values),
            "p50": round(_percentile(values, 0.50), 3),
            "p95": round(_percentile(values, 0.95), 3),
            "latest": round(values[-1], 3),
            "lastObservedAt": items[-1]["observedAt"],
        }

    def p95(metric_name: str) -> float:
        return float(summaries.get(metric_name, {}).get("p95") or 0.0)

    def latest(metric_name: str) -> float:
        return float(summaries.get(metric_name, {}).get("latest") or 0.0)

    return {
        "riskDetectionP95Ms": p95(RISK_DETECTION_LATENCY_MS),
        "remediationQueueDelayP95Ms": p95(REMEDIATION_QUEUE_DELAY_MS),
        "repairP95Ms": p95(REPAIR_TIME_MS),
        "rebalanceP95Ms": p95(REBALANCE_TIME_MS),
        "riskClearP95Ms": p95(RISK_CLEAR_LATENCY_MS),
        "drFreshnessHours": latest(DR_READINESS_AGE_HOURS),
        "leaseTakeoverP95Ms": p95(LEASE_TAKEOVER_TIME_MS),
        "schedulerStarvationAgeSeconds": latest(SCHEDULER_STARVATION_AGE_SECONDS),
        "evidenceFreshnessSeconds": latest(EVIDENCE_FRESHNESS_SECONDS),
        "sampleCounts": {name: int(summary["count"]) for name, summary in summaries.items()},
        "metrics": summaries,
        "evaluatedAt": _utc_iso(current),
    }


def record_burn_observation(
    indicator: str,
    *,
    bad_units: float | int,
    total_units: float | int,
    observed_at: datetime | None = None,
    observation_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    bad = _require_nonnegative_finite(bad_units, "bad_units")
    total = _require_nonnegative_finite(total_units, "total_units")
    if bad > total:
        raise ValueError("bad_units cannot exceed total_units")
    key = str(observation_key or f"burn-{uuid.uuid4().hex}")
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO resilience_slo_burn_observations (
                observation_key, indicator, bad_units, total_units,
                metadata_json, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                str(indicator),
                bad,
                total,
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                _utc_iso(observed_at),
            ),
        )


def _window_burn(
    conn: sqlite3.Connection,
    *,
    since: datetime,
    error_budget_fraction: float,
) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT indicator, SUM(bad_units) AS bad, SUM(total_units) AS total
        FROM resilience_slo_burn_observations
        WHERE observed_at >= ?
        GROUP BY indicator ORDER BY indicator
        """,
        (_utc_iso(since),),
    ).fetchall()
    result: dict[str, float] = {}
    for row in rows:
        total = float(row["total"] or 0.0)
        bad = float(row["bad"] or 0.0)
        result[str(row["indicator"])] = (bad / total / error_budget_fraction) if total > 0 else 0.0
    return result


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
            error_budget_fraction=budget_fraction,
        )
        slow_by_indicator = _window_burn(
            conn,
            since=current - timedelta(seconds=slow_window),
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
    return record_sample(
        EVIDENCE_FRESHNESS_SECONDS,
        0.0,
        observed_at=verified_at,
        metadata={"proofSha256": digest, "scenario": str(scenario)},
        sample_key=f"evidence:{scenario}:{digest}",
    )


def latest_evidence_verification() -> dict[str, Any] | None:
    samples = list_samples(EVIDENCE_FRESHNESS_SECONDS)
    return samples[-1] if samples else None


def milliseconds_between(start: str | None, end: datetime) -> float | None:
    started = _parse_iso(start)
    if started is None:
        return None
    return max(0.0, (end.astimezone(timezone.utc) - started).total_seconds() * 1000.0)

"""Durability-constrained placement optimizer (4.7.5 Gates J, M)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import resilience_cost_model

OPTIMIZER_DIR = config.ROOT / ".resilience-optimizer"
OPTIMIZER_DB = OPTIMIZER_DIR / "optimizer.sqlite3"

_LOCK = threading.RLock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS resilience_optimization_outcomes (
    plan_digest TEXT PRIMARY KEY,
    predicted_savings REAL,
    realized_savings REAL,
    prediction_error REAL,
    recorded_at TEXT NOT NULL
);
"""


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    OPTIMIZER_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = sqlite3.connect(OPTIMIZER_DB, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def _constraint_violations(candidate: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    min_copies = int(baseline.get("minCommittedCopies") or 0)
    min_domains = int(baseline.get("minFailureDomains") or 0)
    candidate_copies = int(candidate.get("committedCopies") or 0)
    candidate_domains = int(candidate.get("failureDomains") or 0)
    if candidate_copies < min_copies:
        violations.append("MIN_COMMITTED_COPIES_REDUCED")
    if candidate_domains < min_domains:
        violations.append("MIN_FAILURE_DOMAINS_REDUCED")
    baseline_copies = int(baseline.get("committedCopies") or min_copies)
    if candidate_copies < baseline_copies:
        violations.append("DEGRADED_BASELINE_COPIES_REDUCED")
    baseline_domains = int(baseline.get("failureDomains") or min_domains)
    if candidate_domains < baseline_domains:
        violations.append("DEGRADED_BASELINE_DOMAINS_REDUCED")
    safety = baseline.get("forecastSafetyHeadroomBytes")
    forecast_free = candidate.get("forecastFreeBytes")
    if safety is not None and forecast_free is not None and int(forecast_free) < int(safety):
        violations.append("FORECAST_SAFETY_HEADROOM_REDUCED")
    if candidate.get("breaksDrDependency") is True:
        violations.append("ACTIVE_DR_DEPENDENCY_BROKEN")
    if candidate.get("mutatesAuthority") is True:
        violations.append("AUTHORITY_MUTATION_FORBIDDEN")
    return violations


def _candidate_cost(candidate: dict[str, Any], catalog: dict[str, Any] | None) -> dict[str, Any]:
    target_id = str(candidate.get("targetId") or "")
    return resilience_cost_model.estimate_target_cost(
        target_id,
        stored_bytes=int(candidate.get("storedBytes") or 0),
        replication_bytes=int(candidate.get("replicationBytes") or 0),
        egress_bytes=int(candidate.get("egressBytes") or 0),
        retrieval_bytes=int(candidate.get("retrievalBytes") or 0),
        catalog=catalog,
    )


def evaluate_candidate(
    candidate: dict[str, Any],
    *,
    baseline: dict[str, Any],
    catalog: dict[str, Any] | None,
) -> dict[str, Any]:
    violations = _constraint_violations(candidate, baseline)
    cost = _candidate_cost(candidate, catalog)
    accepted = not violations and cost.get("status") == "OK"
    score = None
    if accepted:
        capacity_risk = 0.0 if int(candidate.get("forecastFreeBytes") or 0) > int(baseline.get("forecastSafetyHeadroomBytes") or 0) else 1.0
        transfer_cost = float(cost.get("egress") or 0) + float(cost.get("replicationTransfer") or 0)
        domain_penalty = 0.0 if int(candidate.get("failureDomains") or 0) >= int(baseline.get("minFailureDomains") or 0) else 10.0
        score = float(cost.get("monthlyCost") or 0) + capacity_risk + transfer_cost + domain_penalty
    return {
        "candidate": candidate,
        "accepted": accepted,
        "violations": violations,
        "cost": cost,
        "score": score,
    }


def optimize_placement(
    *,
    baseline: dict[str, Any],
    candidates: list[dict[str, Any]],
    catalog: dict[str, Any] | None = None,
    source_snapshot_digest: str | None = None,
    authority_head_digest: str | None = None,
    forecast_digest: str | None = None,
) -> dict[str, Any]:
    """Return the cheapest durable candidate. Safety is a hard constraint, not a weight."""
    price_catalog = catalog if catalog is not None else resilience_cost_model.get_price_catalog()
    evaluated = [
        evaluate_candidate(candidate, baseline=baseline, catalog=price_catalog)
        for candidate in sorted(candidates, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    ]
    accepted = [item for item in evaluated if item["accepted"]]
    accepted.sort(key=lambda item: (float(item["score"] or 0), str(item["candidate"].get("targetId") or "")))
    rejected = [item for item in evaluated if not item["accepted"]]
    chosen = accepted[0] if accepted else None
    plan = {
        "status": "OK" if chosen is not None else "NO_SAFE_CANDIDATE",
        "baseline": baseline,
        "selected": chosen,
        "rejected": rejected,
        "sourceSnapshotDigest": source_snapshot_digest,
        "authorityHeadDigest": authority_head_digest,
        "forecastDigest": forecast_digest,
        "priceCatalogDigest": None if price_catalog is None else price_catalog.get("priceCatalogDigest"),
    }
    plan["candidatePlanDigest"] = _digest(
        {
            "selected": None if chosen is None else chosen["candidate"],
            "baseline": baseline,
            "sourceSnapshotDigest": source_snapshot_digest,
            "authorityHeadDigest": authority_head_digest,
            "forecastDigest": forecast_digest,
            "priceCatalogDigest": plan["priceCatalogDigest"],
        }
    )
    return plan


def record_realized_optimization(
    plan_digest: str,
    *,
    predicted_savings: float,
    realized_savings: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    digest = str(plan_digest or "")
    if not digest:
        raise ValueError("planDigest is required")
    predicted = float(predicted_savings)
    realized = float(realized_savings)
    error = realized - predicted
    timestamp = _utc_iso(now)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO resilience_optimization_outcomes (
                plan_digest, predicted_savings, realized_savings, prediction_error, recorded_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(plan_digest) DO UPDATE SET
                predicted_savings = excluded.predicted_savings,
                realized_savings = excluded.realized_savings,
                prediction_error = excluded.prediction_error,
                recorded_at = excluded.recorded_at
            """,
            (digest, predicted, realized, error, timestamp),
        )
    return {
        "planDigest": digest,
        "predictedSavings": predicted,
        "realizedSavings": realized,
        "predictionError": error,
        "recordedAt": timestamp,
    }


def get_realized_optimization(plan_digest: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM resilience_optimization_outcomes WHERE plan_digest = ?",
            (plan_digest,),
        ).fetchone()
    if row is None:
        return None
    return {
        "planDigest": str(row["plan_digest"]),
        "predictedSavings": row["predicted_savings"],
        "realizedSavings": row["realized_savings"],
        "predictionError": row["prediction_error"],
        "recordedAt": str(row["recorded_at"]),
    }

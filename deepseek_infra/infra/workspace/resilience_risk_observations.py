"""Durable exact-subject risk lifecycle for Fleet Risk Debt (4.7.4 Gate E)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from deepseek_infra.core import config

RISK_LEDGER_DIR = config.ROOT / ".resilience-risk"
RISK_LEDGER_DB = RISK_LEDGER_DIR / "risk.sqlite3"

_LOCK = threading.RLock()
_SEVERITY_ORDER = {"healthy": 0, "low": 0, "warning": 1, "degraded": 2, "critical": 3, "blocked": 4}
_OPEN_SEVERITIES = frozenset({"warning", "degraded", "critical", "blocked"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resilience_risk_observations (
    risk_subject_digest TEXT PRIMARY KEY,
    subject_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    open_since_at TEXT,
    last_seen_at TEXT NOT NULL,
    observation_count INTEGER NOT NULL,
    current_severity TEXT NOT NULL,
    peak_severity TEXT NOT NULL,
    status TEXT NOT NULL,
    last_cleared_at TEXT,
    reopen_count INTEGER NOT NULL DEFAULT 0,
    policy_id TEXT,
    backup_id TEXT,
    target_id TEXT,
    failure_domain TEXT,
    last_snapshot_digest TEXT,
    last_snapshot_observation_key TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resilience_risk_observations_status
ON resilience_risk_observations(status, open_since_at);
"""


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
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
    RISK_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = sqlite3.connect(RISK_LEDGER_DB, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(resilience_risk_observations)")}
        if "last_snapshot_observation_key" not in columns:
            conn.execute(
                "ALTER TABLE resilience_risk_observations ADD COLUMN last_snapshot_observation_key TEXT"
            )
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def canonical_risk_subject(risk: dict[str, Any]) -> dict[str, str | None]:
    """Return the stable scope whose unresolved lifetime accrues Risk Debt."""
    policy_id = str(risk.get("policyId") or "")
    backup_id = str(risk.get("backupId") or "")
    target_id = str(risk.get("targetId") or risk.get("target") or "")
    failure_domain = str(risk.get("failureDomain") or "")
    return {
        "type": str(risk.get("type") or "").upper(),
        "policyId": policy_id or None,
        "backupId": backup_id or None,
        "targetId": target_id or None,
        "failureDomain": failure_domain or None,
    }


def risk_subject_digest(subject_or_risk: dict[str, Any]) -> str:
    subject = (
        subject_or_risk
        if set(subject_or_risk).issubset({"type", "policyId", "backupId", "targetId", "failureDomain"})
        else canonical_risk_subject(subject_or_risk)
    )
    raw = json.dumps(subject, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "riskSubjectDigest": str(row["risk_subject_digest"]),
        "riskSubject": json.loads(str(row["subject_json"])),
        "firstSeenAt": str(row["first_seen_at"]),
        "openSinceAt": row["open_since_at"],
        "lastSeenAt": str(row["last_seen_at"]),
        "observationCount": int(row["observation_count"]),
        "currentSeverity": str(row["current_severity"]),
        "peakSeverity": str(row["peak_severity"]),
        "status": str(row["status"]),
        "lastClearedAt": row["last_cleared_at"],
        "reopenCount": int(row["reopen_count"]),
        "policyId": row["policy_id"],
        "backupId": row["backup_id"],
        "targetId": row["target_id"],
        "failureDomain": row["failure_domain"],
        "lastSnapshotDigest": row["last_snapshot_digest"],
        "lastSnapshotObservationKey": row["last_snapshot_observation_key"],
        "updatedAt": str(row["updated_at"]),
    }


def get_observation(subject_digest: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM resilience_risk_observations WHERE risk_subject_digest = ?",
            (subject_digest,),
        ).fetchone()
    return _row_to_record(row) if row is not None else None


def observation_for_risk(risk: dict[str, Any]) -> dict[str, Any] | None:
    return get_observation(risk_subject_digest(canonical_risk_subject(risk)))


def observe_risk_snapshot(
    snapshot: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Atomically observe OPEN/CLEARED/REOPENED exact-subject lifecycles."""
    observed_at = _utc_iso(now)
    snapshot_digest = str(snapshot.get("riskDigest") or "")
    snapshot_generated_at = str(snapshot.get("generatedAt") or observed_at)
    snapshot_observation_key = hashlib.sha256(
        json.dumps(
            {"generatedAt": snapshot_generated_at, "riskDigest": snapshot_digest},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    raw_risks = snapshot.get("risks")
    risks = raw_risks if isinstance(raw_risks, list) else []
    records: list[dict[str, Any]] = []
    slo_samples: list[dict[str, Any]] = []
    burn_observations: list[dict[str, Any]] = []
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for raw_risk in risks:
            if not isinstance(raw_risk, dict):
                continue
            subject = canonical_risk_subject(raw_risk)
            digest = risk_subject_digest(subject)
            severity = str(raw_risk.get("severity") or "healthy").lower()
            is_open = severity in _OPEN_SEVERITIES
            existing = conn.execute(
                "SELECT * FROM resilience_risk_observations WHERE risk_subject_digest = ?",
                (digest,),
            ).fetchone()
            if existing is None and not is_open:
                continue
            if (
                existing is not None
                and str(existing["last_snapshot_observation_key"] or "") == snapshot_observation_key
            ):
                records.append(_row_to_record(existing))
                continue

            if existing is None:
                first_seen = observed_at
                open_since = observed_at
                count = 1
                peak = severity
                status = "OPEN"
                last_cleared = None
                reopen_count = 0
                detected_at = _parse_iso(
                    raw_risk.get("detectedAt")
                    or raw_risk.get("observedAt")
                    or raw_risk.get("generatedAt")
                    or snapshot.get("generatedAt")
                )
                current_time = _parse_iso(observed_at)
                if detected_at is not None and current_time is not None:
                    slo_samples.append(
                        {
                            "metric": "risk_detection_latency_ms",
                            "value": max(0.0, (current_time - detected_at).total_seconds() * 1000.0),
                            "key": f"risk-detected:{digest}:{observed_at}",
                            "digest": digest,
                        }
                    )
            else:
                first_seen = str(existing["first_seen_at"])
                open_since = existing["open_since_at"]
                count = int(existing["observation_count"]) + 1
                existing_peak = str(existing["peak_severity"])
                peak = severity if _SEVERITY_ORDER.get(severity, 0) > _SEVERITY_ORDER.get(existing_peak, 0) else existing_peak
                last_cleared = existing["last_cleared_at"]
                reopen_count = int(existing["reopen_count"])
                previous_status = str(existing["status"])
                if is_open and previous_status == "CLEARED":
                    status = "REOPENED"
                    open_since = observed_at
                    reopen_count += 1
                    detected_at = _parse_iso(
                        raw_risk.get("detectedAt")
                        or raw_risk.get("observedAt")
                        or raw_risk.get("generatedAt")
                        or snapshot.get("generatedAt")
                    )
                    current_time = _parse_iso(observed_at)
                    if detected_at is not None and current_time is not None:
                        slo_samples.append(
                            {
                                "metric": "risk_detection_latency_ms",
                                "value": max(0.0, (current_time - detected_at).total_seconds() * 1000.0),
                                "key": f"risk-reopened:{digest}:{observed_at}",
                                "digest": digest,
                            }
                        )
                elif is_open:
                    status = previous_status if previous_status in {"OPEN", "REOPENED"} else "OPEN"
                    open_since = open_since or observed_at
                else:
                    status = "CLEARED"
                    previous_open_since = _parse_iso(str(existing["open_since_at"] or ""))
                    current_time = _parse_iso(observed_at)
                    if previous_open_since is not None and current_time is not None:
                        slo_samples.append(
                            {
                                "metric": "risk_clear_latency_ms",
                                "value": max(0.0, (current_time - previous_open_since).total_seconds() * 1000.0),
                                "key": f"risk-cleared:{digest}:{observed_at}",
                                "digest": digest,
                            }
                        )
                    open_since = None
                    last_cleared = observed_at

                previous_seen = _parse_iso(str(existing["last_seen_at"] or ""))
                current_time = _parse_iso(observed_at)
                if previous_seen is not None and current_time is not None:
                    elapsed_minutes = max(0.0, (current_time - previous_seen).total_seconds() / 60.0)
                    previous_severity = str(existing["current_severity"] or "healthy").lower()
                    if elapsed_minutes > 0:
                        burn_observations.append(
                            {
                                "indicator": "critical_durability_risk_minutes",
                                "bad": elapsed_minutes if previous_severity in {"critical", "blocked"} else 0.0,
                                "total": elapsed_minutes,
                                "key": f"critical-risk:{digest}:{observed_at}",
                                "startedAt": previous_seen,
                            }
                        )
                    if elapsed_minutes > 0 and "DR" in str(subject.get("type") or ""):
                        burn_observations.append(
                            {
                                "indicator": "dr_stale_minutes",
                                "bad": elapsed_minutes if previous_severity in _OPEN_SEVERITIES else 0.0,
                                "total": elapsed_minutes,
                                "key": f"dr-stale:{digest}:{observed_at}",
                                "startedAt": previous_seen,
                            }
                        )

            conn.execute(
                """
                INSERT INTO resilience_risk_observations (
                    risk_subject_digest, subject_json, first_seen_at, open_since_at,
                    last_seen_at, observation_count, current_severity, peak_severity,
                    status, last_cleared_at, reopen_count, policy_id, backup_id,
                    target_id, failure_domain, last_snapshot_digest,
                    last_snapshot_observation_key, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(risk_subject_digest) DO UPDATE SET
                    subject_json = excluded.subject_json,
                    open_since_at = excluded.open_since_at,
                    last_seen_at = excluded.last_seen_at,
                    observation_count = excluded.observation_count,
                    current_severity = excluded.current_severity,
                    peak_severity = excluded.peak_severity,
                    status = excluded.status,
                    last_cleared_at = excluded.last_cleared_at,
                    reopen_count = excluded.reopen_count,
                    policy_id = excluded.policy_id,
                    backup_id = excluded.backup_id,
                    target_id = excluded.target_id,
                    failure_domain = excluded.failure_domain,
                    last_snapshot_digest = excluded.last_snapshot_digest,
                    last_snapshot_observation_key = excluded.last_snapshot_observation_key,
                    updated_at = excluded.updated_at
                """,
                (
                    digest,
                    json.dumps(subject, ensure_ascii=False, sort_keys=True),
                    first_seen,
                    open_since,
                    observed_at,
                    count,
                    severity,
                    peak,
                    status,
                    last_cleared,
                    reopen_count,
                    subject.get("policyId"),
                    subject.get("backupId"),
                    subject.get("targetId"),
                    subject.get("failureDomain"),
                    snapshot_digest,
                    snapshot_observation_key,
                    observed_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM resilience_risk_observations WHERE risk_subject_digest = ?",
                (digest,),
            ).fetchone()
            assert row is not None
            records.append(_row_to_record(row))
    from deepseek_infra.infra.workspace import resilience_slo_ledger

    for sample in slo_samples:
        resilience_slo_ledger.try_record_sample(
            str(sample["metric"]),
            float(sample["value"]),
            observed_at=now,
            risk_subject_digest=str(sample["digest"]),
            sample_key=str(sample["key"]),
        )
    for observation in burn_observations:
        resilience_slo_ledger.try_record_burn_observation(
            str(observation["indicator"]),
            bad_units=float(observation["bad"]),
            total_units=float(observation["total"]),
            started_at=observation.get("startedAt"),
            observed_at=now,
            observation_key=str(observation["key"]),
        )
    return records


def list_open_observations() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM resilience_risk_observations
            WHERE status IN ('OPEN', 'REOPENED')
            ORDER BY open_since_at ASC, risk_subject_digest ASC
            """
        ).fetchall()
    return [_row_to_record(row) for row in rows]

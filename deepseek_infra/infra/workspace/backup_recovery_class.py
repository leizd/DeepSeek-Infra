"""RecoveryClass calibration and RTO estimation (4.5.1 Gate F).

Provides low-cardinality classification of recovery scenarios and statistical
calibration (P50/P90) of recovery time objectives (RTO).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


def size_bucket(bytes_count: int) -> str:
    """Categorize backup size into low-cardinality buckets."""
    if bytes_count < 10 * 1024 * 1024:
        return "small"
    if bytes_count < 100 * 1024 * 1024:
        return "medium"
    return "large"


def chain_depth_bucket(depth: int) -> str:
    """Categorize chain depth into low-cardinality buckets."""
    if depth <= 3:
        return "shallow"
    if depth <= 10:
        return "moderate"
    return "deep"


@dataclass(frozen=True)
class RecoveryClass:
    target_kind: str  # "filesystem" | "s3" | "managed-local"
    format_kind: str  # "single-file" | "object-set-v1"
    size_bucket: str  # "small" | "medium" | "large"
    chain_depth_bucket: str  # "shallow" | "moderate" | "deep"
    storage_protocol: str = "local"  # "local" | "s3"

    @property
    def tag(self) -> str:
        return f"{self.target_kind}:{self.format_kind}:{self.size_bucket}:{self.chain_depth_bucket}"

    @property
    def key(self) -> str:
        return self.tag

    @property
    def size_category(self) -> str:
        return self.size_bucket

    @property
    def chain_depth(self) -> str:
        return self.chain_depth_bucket

    def __str__(self) -> str:
        return self.tag

    def to_dict(self) -> dict[str, str]:
        return {
            "targetKind": self.target_kind,
            "formatKind": self.format_kind,
            "sizeBucket": self.size_bucket,
            "chainDepthBucket": self.chain_depth_bucket,
            "storageProtocol": self.storage_protocol,
            "tag": self.tag,
        }


def classify_recovery(
    *,
    target_kind: str = "filesystem",
    format_kind: str | None = None,
    logical_bytes: int = 0,
    chain_length: int = 1,
    storage_protocol: str = "local",
) -> RecoveryClass:
    """Classify a recovery scenario into a deterministic RecoveryClass."""
    f_kind = format_kind
    if not f_kind:
        if storage_protocol and "object-set" in storage_protocol:
            f_kind = "object-set-v1"
        else:
            f_kind = "single-file"
    return RecoveryClass(
        target_kind=str(target_kind or "filesystem"),
        format_kind=str(f_kind),
        size_bucket=size_bucket(max(0, logical_bytes)),
        chain_depth_bucket=chain_depth_bucket(max(1, chain_length)),
        storage_protocol=str(storage_protocol or "local"),
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * percentile
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    d0 = sorted_vals[int(f)] * (c - k)
    d1 = sorted_vals[int(c)] * (k - f)
    return d0 + d1


DEFAULT_TRANSFER_SPEED = 35.0 * 1024 * 1024  # 35 MB/s
DEFAULT_CRYPTO_SPEED = 70.0 * 1024 * 1024   # 70 MB/s
DEFAULT_MATERIALIZE_SPEED = 140.0 * 1024 * 1024  # 140 MB/s
MIN_OVERHEAD_SECONDS = 2.0


def calibrate_rto(
    samples_or_target_id: Any = None,
    logical_bytes: int = 0,
    recovery_class: RecoveryClass | None = None,
    *,
    chain_length: int = 1,
    target_id: str | None = None,
    samples: list[dict[str, Any]] | None = None,
    target_class: RecoveryClass | None = None,
) -> dict[str, Any]:
    """Calculate calibrated P50/P90 RTO estimates.

    Never claims SLA (always isSla: False).
    """
    from deepseek_infra.infra.workspace import backup_dr_ledger

    active_class = recovery_class or target_class
    if active_class is None:
        active_class = classify_recovery(
            target_kind="filesystem" if target_id != "managed-local" else "managed-local",
            logical_bytes=logical_bytes,
            chain_length=chain_length,
        )

    t_id = target_id or (samples_or_target_id if isinstance(samples_or_target_id, str) else None)
    active_samples: list[dict[str, Any]] = []
    if isinstance(samples_or_target_id, list):
        active_samples = samples_or_target_id
    elif samples is not None:
        active_samples = samples
    elif t_id is not None:
        target_samples = backup_dr_ledger.list_stage_samples(target_id=t_id)
        if len(target_samples) >= 3:
            active_samples = target_samples
        else:
            active_samples = backup_dr_ledger.list_stage_samples()
    else:
        active_samples = backup_dr_ledger.list_stage_samples()

    transfer_speeds: list[float] = []
    crypto_speeds: list[float] = []
    materialize_speeds: list[float] = []

    for sample in active_samples:
        rc = sample.get("recoveryClass")
        rc_key = rc.get("tag") if isinstance(rc, dict) else str(rc or "")
        if active_class and rc_key and rc_key != active_class.key and rc_key != "default":
            continue
        stage = str(sample.get("stage") or "")
        b_count = int(sample.get("bytes") or sample.get("bytesTransferred") or sample.get("bytes_transferred") or 0)
        d_ms = float(sample.get("durationMs") or sample.get("duration_ms") or 0.0)
        if d_ms <= 0 or b_count <= 0:
            continue
        speed = b_count / (d_ms / 1000.0)
        if stage == "transfer":
            transfer_speeds.append(speed)
        elif stage == "crypto":
            crypto_speeds.append(speed)
        elif stage in ("materialize", "materialization"):
            materialize_speeds.append(speed)

    sample_count = max(len(transfer_speeds), len(crypto_speeds), len(materialize_speeds))
    matching_stages = sum(1 for speeds in (transfer_speeds, crypto_speeds, materialize_speeds) if speeds)
    l_bytes = max(1000, logical_bytes)

    # Planning heuristic uses defaults only — never labelled calibrated RTO/SLA.
    t_default = DEFAULT_TRANSFER_SPEED
    c_default = DEFAULT_CRYPTO_SPEED
    m_default = DEFAULT_MATERIALIZE_SPEED
    planning_p50 = max(
        1,
        int(round((l_bytes / t_default) + (l_bytes / c_default) + (l_bytes / m_default) + MIN_OVERHEAD_SECONDS)),
    )
    planning_heuristic = {
        "status": "planning-heuristic",
        "isSla": False,
        "p50Seconds": planning_p50,
        "estimatedSeconds": planning_p50,
        "transferBytesPerSecond": t_default,
        "cryptoBytesPerSecond": c_default,
        "materializeBytesPerSecond": m_default,
    }

    if matching_stages < 3 or sample_count < 1:
        return {
            "status": "unavailable",
            "reason": "insufficient-matching-evidence",
            "isSla": False,
            "confidence": "none",
            "sampleCount": sample_count,
            "recoveryClass": active_class.key,
            "planningHeuristic": planning_heuristic,
        }

    if sample_count >= 10:
        confidence = "high"
    elif sample_count >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    # P50 calculation from matching evidence only
    t_speed_p50 = _percentile(transfer_speeds, 0.50) if transfer_speeds else DEFAULT_TRANSFER_SPEED
    c_speed_p50 = _percentile(crypto_speeds, 0.50) if crypto_speeds else DEFAULT_CRYPTO_SPEED
    m_speed_p50 = _percentile(materialize_speeds, 0.50) if materialize_speeds else DEFAULT_MATERIALIZE_SPEED

    p50_seconds = max(1, int(round((l_bytes / t_speed_p50) + (l_bytes / c_speed_p50) + (l_bytes / m_speed_p50) + MIN_OVERHEAD_SECONDS)))

    # P90 calculation (slower speeds = higher duration)
    t_speed_p90 = _percentile(transfer_speeds, 0.10) if transfer_speeds else (DEFAULT_TRANSFER_SPEED * 0.7)
    c_speed_p90 = _percentile(crypto_speeds, 0.10) if crypto_speeds else (DEFAULT_CRYPTO_SPEED * 0.7)
    m_speed_p90 = _percentile(materialize_speeds, 0.10) if materialize_speeds else (DEFAULT_MATERIALIZE_SPEED * 0.7)

    p90_seconds = max(p50_seconds, int(round((l_bytes / t_speed_p90) + (l_bytes / c_speed_p90) + (l_bytes / m_speed_p90) + MIN_OVERHEAD_SECONDS * 1.5)))

    return {
        "status": "calibrated",
        "isSla": False,
        "confidence": confidence,
        "sampleCount": sample_count,
        "p50Seconds": p50_seconds,
        "p90Seconds": p90_seconds,
        "estimatedSeconds": p50_seconds,
        "recoveryClass": active_class.key,
        "stageEstimates": {
            "transfer": {"p50Seconds": max(1, int(round(l_bytes / t_speed_p50)))},
            "crypto": {"p50Seconds": max(1, int(round(l_bytes / c_speed_p50)))},
            "materialization": {"p50Seconds": max(1, int(round(l_bytes / m_speed_p50)))},
        },
        "planningHeuristic": planning_heuristic,
    }

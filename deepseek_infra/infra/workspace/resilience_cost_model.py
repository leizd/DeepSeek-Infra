"""Versioned, digest-bound storage and egress price catalog (4.7.5 Gate I)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from deepseek_infra.core import config

COST_MODEL_DIR = config.ROOT / ".resilience-cost"
COST_MODEL_DB = COST_MODEL_DIR / "cost.sqlite3"
GIB = 1024**3

_LOCK = threading.RLock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS resilience_price_catalogs (
    price_catalog_version INTEGER PRIMARY KEY,
    catalog_json TEXT NOT NULL,
    catalog_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def catalog_digest(catalog: dict[str, Any]) -> str:
    payload = {
        "priceCatalogVersion": int(catalog.get("priceCatalogVersion") or 0),
        "targets": catalog.get("targets") or {},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    COST_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = sqlite3.connect(COST_MODEL_DB, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def put_price_catalog(catalog: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    version = int(catalog.get("priceCatalogVersion") or 0)
    if version < 1:
        raise ValueError("priceCatalogVersion must be >= 1")
    targets = catalog.get("targets")
    if not isinstance(targets, dict) or not targets:
        raise ValueError("targets are required")
    normalized = {"priceCatalogVersion": version, "targets": targets}
    digest = catalog_digest(normalized)
    rendered = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO resilience_price_catalogs (
                price_catalog_version, catalog_json, catalog_digest, created_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(price_catalog_version) DO UPDATE SET
                catalog_json = excluded.catalog_json,
                catalog_digest = excluded.catalog_digest
            """,
            (version, rendered, digest, _utc_iso(now)),
        )
    return {**normalized, "priceCatalogDigest": digest}


def get_price_catalog(version: int | None = None) -> dict[str, Any] | None:
    with _connect() as conn:
        if version is None:
            row = conn.execute(
                "SELECT * FROM resilience_price_catalogs ORDER BY price_catalog_version DESC LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM resilience_price_catalogs WHERE price_catalog_version = ?",
                (int(version),),
            ).fetchone()
    if row is None:
        return None
    parsed = json.loads(str(row["catalog_json"]))
    parsed["priceCatalogDigest"] = str(row["catalog_digest"])
    return parsed


def _gib(bytes_value: int) -> float:
    return float(max(0, bytes_value)) / float(GIB)


def estimate_target_cost(
    target_id: str,
    *,
    stored_bytes: int = 0,
    replication_bytes: int = 0,
    egress_bytes: int = 0,
    retrieval_bytes: int = 0,
    request_count: int = 0,
    catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = catalog if catalog is not None else get_price_catalog()
    if source is None:
        return {
            "targetId": target_id,
            "status": "UNKNOWN_COST",
            "storage": None,
            "replicationTransfer": None,
            "egress": None,
            "retrieval": None,
            "requestCost": None,
            "monthlyCost": None,
            "priceCatalogDigest": None,
        }
    raw_targets = source.get("targets")
    targets = raw_targets if isinstance(raw_targets, dict) else {}
    prices = targets.get(target_id)
    if not isinstance(prices, dict):
        return {
            "targetId": target_id,
            "status": "UNKNOWN_COST",
            "storage": None,
            "replicationTransfer": None,
            "egress": None,
            "retrieval": None,
            "requestCost": None,
            "monthlyCost": None,
            "priceCatalogDigest": source.get("priceCatalogDigest"),
        }
    storage_rate = prices.get("storagePerGiBMonth")
    egress_rate = prices.get("egressPerGiB")
    request_rate = prices.get("requestCost")
    retrieval_rate = prices.get("retrievalPerGiB")
    if storage_rate is None or egress_rate is None:
        return {
            "targetId": target_id,
            "status": "UNKNOWN_COST",
            "storage": None,
            "replicationTransfer": None,
            "egress": None,
            "retrieval": None,
            "requestCost": None,
            "monthlyCost": None,
            "priceCatalogDigest": source.get("priceCatalogDigest"),
        }
    storage = _gib(stored_bytes) * float(storage_rate)
    replication = _gib(replication_bytes) * float(egress_rate)
    egress = _gib(egress_bytes) * float(egress_rate)
    retrieval = _gib(retrieval_bytes) * float(retrieval_rate if retrieval_rate is not None else egress_rate)
    requests = float(request_count) * float(request_rate or 0)
    monthly = storage + replication + egress + retrieval + requests
    return {
        "targetId": target_id,
        "status": "OK",
        "storage": round(storage, 6),
        "replicationTransfer": round(replication, 6),
        "egress": round(egress, 6),
        "retrieval": round(retrieval, 6),
        "requestCost": round(requests, 6),
        "monthlyCost": round(monthly, 6),
        "priceCatalogDigest": source.get("priceCatalogDigest"),
        "priceCatalogVersion": source.get("priceCatalogVersion"),
    }

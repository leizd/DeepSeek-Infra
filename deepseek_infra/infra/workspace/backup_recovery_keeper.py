"""Autonomous Recovery Lease Keeper daemon and reconciliation (4.5.1 Gate A).

Scans active durable recovery sessions, reconciles leases against remote hold manifests,
and keeps recovery holds alive for active, paused, and recovery-required jobs.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import (
    backup_recovery_job,
    backup_recovery_lease,
    backup_recovery_telemetry,
    backup_targets,
)

_logger = logging.getLogger("deepseek_infra.recovery_keeper")

STAGING_ROOT = config.ROOT / ".restore-staging"
DEFAULT_TICK_SECONDS = 300  # 5 minutes
DEFAULT_RENEW_INTERVAL_SECONDS = 900  # 15 minutes
RECOVERY_REQUIRED_TTL_SECONDS = 24 * 3600  # 24 hours

NON_TERMINAL_PHASES = {"initializing", "fetching", "paused", "recovery-required"}


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        cleaned = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def scan_durable_recovery_sessions() -> dict[str, dict[str, Any]]:
    """Scan all durable recovery sessions from staging root.

    Returns dict mapping restoreId -> session payload (including '_path').
    """
    root = STAGING_ROOT
    if not root.is_dir():
        return {}
    results: dict[str, dict[str, Any]] = {}
    for p in sorted(root.glob("*/remote-fetch.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                r_id = str(data.get("restoreId") or p.parent.name)
                data["_path"] = p
                results[r_id] = data
        except (OSError, json.JSONDecodeError):
            continue
    return results


def reconcile_durable_recovery_leases(
    *,
    now: datetime | None = None,
    min_interval_seconds: float | int = DEFAULT_RENEW_INTERVAL_SECONDS,
    min_renew_age_seconds: float | int | None = None,
) -> dict[str, Any]:
    """Scan and renew due recovery leases across all non-terminal durable recovery jobs.

    Safe to run at startup or periodically in a maintenance loop.
    """
    min_interval = min_renew_age_seconds if min_renew_age_seconds is not None else min_interval_seconds
    current_time = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    sessions = scan_durable_recovery_sessions()
    scanned = len(sessions)
    retained: list[str] = []
    renewed: list[str] = []
    failed: list[dict[str, str]] = []
    skipped_terminal: list[str] = []
    skipped_local: list[str] = []

    for restore_id, session in sessions.items():
        path = session.get("_path")
        if not isinstance(path, Path):
            path = STAGING_ROOT / restore_id / "remote-fetch.json"

        phase = str(session.get("phase") or "")
        if phase in backup_recovery_lease.TERMINAL_PHASES or phase not in NON_TERMINAL_PHASES:
            skipped_terminal.append(restore_id)
            continue

        target_id = str(session.get("targetId") or "managed-local")
        if target_id == "managed-local" or target_id.startswith("local"):
            skipped_local.append(restore_id)
            continue

        holds = list(session.get("holds") or [])
        hold_keys = list(session.get("holdKeys") or [])
        if not hold_keys and session.get("holdKey"):
            hold_keys = [str(session["holdKey"])]
        if not holds and not hold_keys:
            retained.append(restore_id)
            continue

        ttl_seconds = (
            RECOVERY_REQUIRED_TTL_SECONDS
            if phase == "recovery-required"
            else backup_recovery_lease.DEFAULT_TTL_SECONDS
        )

        try:
            store = backup_targets.open_target_store(target_id, write_intent=False)
            with backup_recovery_job.session_lock(path):
                try:
                    fresh = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(fresh, dict):
                        session = fresh
                except (OSError, json.JSONDecodeError):
                    pass

                did_renew = False
                if holds:
                    updated_holds = []
                    for h_entry in holds:
                        if isinstance(h_entry, dict) and "holdKey" in h_entry:
                            new_h = backup_recovery_lease.renew_recovery_hold(
                                store,
                                h_entry,
                                ttl_seconds=ttl_seconds,
                            )
                            updated_holds.append(new_h)
                            did_renew = True
                        else:
                            updated_holds.append(h_entry)
                    if did_renew:
                        session["holds"] = updated_holds
                else:
                    did_renew = backup_recovery_lease.renew_session(
                        store,
                        session,
                        now=current_time,
                        ttl_seconds=ttl_seconds,
                        min_interval_seconds=int(min_interval),
                    )

                if did_renew:
                    backup_recovery_telemetry.increment_counter(session, "holdRenewalSuccess")
                    session["updatedAt"] = _utc_iso(current_time)
                    tmp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
                    tmp.write_text(json.dumps({k: v for k, v in session.items() if k != "_path"}, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                    tmp.replace(path)
                    renewed.append(restore_id)
                else:
                    retained.append(restore_id)
        except Exception as exc:
            _logger.warning("Failed to renew recovery lease for job %s: %s", restore_id, exc)
            with backup_recovery_job.session_lock(path):
                backup_recovery_telemetry.increment_counter(session, "holdRenewalFailure")
                session["updatedAt"] = _utc_iso(current_time)
                try:
                    tmp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
                    tmp.write_text(json.dumps({k: v for k, v in session.items() if k != "_path"}, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                    tmp.replace(path)
                except OSError:
                    pass
            failed.append({"restoreId": restore_id, "error": str(exc)})

    return {
        "scanned": scanned,
        "renewed": len(renewed),
        "retained": len(retained),
        "skipped": len(skipped_terminal) + len(skipped_local),
        "failed": len(failed),
        "details": {
            "renewed": renewed,
            "retained": retained,
            "skipped": skipped_terminal + skipped_local,
            "failed": failed,
        },
    }


def get_recovery_lease_health() -> dict[str, Any]:
    """Get status overview of active recovery leases."""
    sessions = scan_durable_recovery_sessions()
    active_count = sum(1 for s in sessions.values() if s.get("phase") in NON_TERMINAL_PHASES)
    return {
        "status": "ok",
        "activeLeases": active_count,
        "totalSessions": len(sessions),
        "renewedCount": 0,
    }


class RecoveryLeaseKeeper:
    """Daemon thread for periodic background renewal of active recovery holds."""

    def __init__(
        self,
        *,
        tick_interval_seconds: float | int = DEFAULT_TICK_SECONDS,
        min_renew_interval_seconds: float | int = DEFAULT_RENEW_INTERVAL_SECONDS,
    ) -> None:
        self.tick_interval_seconds = float(tick_interval_seconds)
        self.min_renew_interval_seconds = float(min_renew_interval_seconds)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="recovery-lease-keeper",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def step(self) -> dict[str, Any]:
        return reconcile_durable_recovery_leases(
            min_interval_seconds=self.min_renew_interval_seconds,
        )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                reconcile_durable_recovery_leases(
                    min_interval_seconds=self.min_renew_interval_seconds,
                )
            except Exception as exc:
                _logger.error("Error in recovery lease keeper tick: %s", exc)
            self._stop_event.wait(self.tick_interval_seconds)


_GLOBAL_KEEPER: RecoveryLeaseKeeper | None = None
_GLOBAL_KEEPER_LOCK = threading.Lock()


def get_global_recovery_keeper() -> RecoveryLeaseKeeper:
    global _GLOBAL_KEEPER
    with _GLOBAL_KEEPER_LOCK:
        if _GLOBAL_KEEPER is None:
            _GLOBAL_KEEPER = RecoveryLeaseKeeper()
        return _GLOBAL_KEEPER

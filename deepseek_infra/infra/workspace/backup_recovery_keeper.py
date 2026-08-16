"""Autonomous Recovery Lease Keeper daemon and reconciliation (4.5.1/4.5.2).

Scans active durable recovery sessions, reconciles leases against remote hold
manifests, and keeps recovery holds alive for any non-terminal job that still
holds a remote hold. A single global supervisor is started with the application
lifecycle — never one daemon thread per restore.
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

DEFAULT_TICK_SECONDS = 300  # 5 minutes
DEFAULT_RENEW_INTERVAL_SECONDS = 900  # 15 minutes
RECOVERY_REQUIRED_TTL_SECONDS = 24 * 3600  # 24 hours
KEEPER_FAILURE_DEGRADE_THRESHOLD = 3


# Overridable by tests (conftest monkeypatches STAGING_ROOT).
STAGING_ROOT = config.ROOT / ".restore-staging"


def _staging_root() -> Path:
    override = globals().get("STAGING_ROOT")
    if isinstance(override, Path):
        return override
    return config.ROOT / ".restore-staging"


def _health_state_path() -> Path:
    """Prefer the same DR dir isolation as the evidence ledger (tmp-safe)."""
    try:
        from deepseek_infra.infra.workspace import backup_dr_ledger

        return Path(backup_dr_ledger.BACKUP_DR_DIR) / "lease-keeper-health.json"
    except Exception:
        return config.ROOT / ".backup-dr" / "lease-keeper-health.json"


# Explicit terminal only — any other phase with a remote hold is protected.
TERMINAL_PHASES = frozenset(backup_recovery_lease.TERMINAL_PHASES)


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


def _session_has_remote_hold(session: dict[str, Any]) -> bool:
    holds = list(session.get("holds") or [])
    hold_keys = list(session.get("holdKeys") or [])
    if not hold_keys and session.get("holdKey"):
        hold_keys = [str(session["holdKey"])]
    if holds or hold_keys:
        return True
    return False


def _is_protected_phase(phase: str) -> bool:
    return phase not in TERMINAL_PHASES and phase != ""


def _is_local_target(target_id: str) -> bool:
    return target_id == "managed-local" or target_id.startswith("local")


def scan_durable_recovery_sessions() -> dict[str, dict[str, Any]]:
    """Scan all durable recovery sessions from staging root.

    Returns dict mapping restoreId -> session payload (including '_path').
    """
    root = _staging_root()
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


class _KeeperHealthState:
    """Process-local + durable health counters for the global keeper."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path_key: str | None = None
        self.last_tick_at: str | None = None
        self.last_successful_tick_at: str | None = None
        self.consecutive_failures = 0
        self.protected_jobs = 0
        self.renewed_leases = 0
        self.last_failure: str | None = None
        self.keeper_running = False
        self.total_renewed = 0
        self._bind_path()

    def _bind_path(self) -> None:
        """Rebind when ROOT/tmp isolation changes (tests patch config.ROOT)."""
        key = str(_health_state_path().resolve()) if _health_state_path() else ""
        if key == self._path_key:
            return
        self._path_key = key
        self.last_tick_at = None
        self.last_successful_tick_at = None
        self.consecutive_failures = 0
        self.protected_jobs = 0
        self.renewed_leases = 0
        self.last_failure = None
        self.total_renewed = 0
        self._load()

    def _load(self) -> None:
        path = _health_state_path()
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        self.last_tick_at = raw.get("lastTickAt") if isinstance(raw.get("lastTickAt"), str) else None
        self.last_successful_tick_at = (
            raw.get("lastSuccessfulTickAt") if isinstance(raw.get("lastSuccessfulTickAt"), str) else None
        )
        self.consecutive_failures = max(0, int(raw.get("consecutiveFailures") or 0))
        self.protected_jobs = max(0, int(raw.get("protectedJobs") or 0))
        self.renewed_leases = max(0, int(raw.get("renewedLeases") or 0))
        self.total_renewed = max(0, int(raw.get("totalRenewed") or 0))
        self.last_failure = raw.get("lastFailure") if isinstance(raw.get("lastFailure"), str) else None

    def _persist(self) -> None:
        path = _health_state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schemaVersion": 1,
                "lastTickAt": self.last_tick_at,
                "lastSuccessfulTickAt": self.last_successful_tick_at,
                "consecutiveFailures": self.consecutive_failures,
                "protectedJobs": self.protected_jobs,
                "renewedLeases": self.renewed_leases,
                "totalRenewed": self.total_renewed,
                "lastFailure": self.last_failure,
                "keeperRunning": self.keeper_running,
                "updatedAt": _utc_iso(),
            }
            tmp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    def mark_running(self, running: bool) -> None:
        with self._lock:
            self._bind_path()
            self.keeper_running = running
            self._persist()

    def record_tick(self, summary: dict[str, Any], *, error: str | None = None) -> None:
        with self._lock:
            self._bind_path()
            now = _utc_iso()
            self.last_tick_at = now
            if error:
                self.consecutive_failures += 1
                self.last_failure = error[:500]
            else:
                failed = int(summary.get("failed") or 0)
                if failed > 0:
                    self.consecutive_failures += 1
                    details = summary.get("details") if isinstance(summary.get("details"), dict) else {}
                    failed_list = details.get("failed") if isinstance(details, dict) else []
                    if isinstance(failed_list, list) and failed_list:
                        first = failed_list[0]
                        self.last_failure = str(first.get("error") if isinstance(first, dict) else first)[:500]
                    else:
                        self.last_failure = f"renewal-failures:{failed}"
                else:
                    self.consecutive_failures = 0
                    self.last_failure = None
                    self.last_successful_tick_at = now
                renewed = int(summary.get("renewed") or 0)
                self.renewed_leases = renewed
                self.total_renewed += renewed
                self.protected_jobs = int(summary.get("protected") or 0)
            self._persist()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._bind_path()
            return {
                "lastTickAt": self.last_tick_at,
                "lastSuccessfulTickAt": self.last_successful_tick_at,
                "consecutiveFailures": self.consecutive_failures,
                "protectedJobs": self.protected_jobs,
                "renewedLeases": self.renewed_leases,
                "totalRenewed": self.total_renewed,
                "lastFailure": self.last_failure,
                "keeperRunning": self.keeper_running,
            }


_HEALTH = _KeeperHealthState()


def reconcile_durable_recovery_leases(
    *,
    now: datetime | None = None,
    min_interval_seconds: float | int = DEFAULT_RENEW_INTERVAL_SECONDS,
    min_renew_age_seconds: float | int | None = None,
) -> dict[str, Any]:
    """Scan and renew due recovery leases across all non-terminal durable recovery jobs.

    Protection rule: session holds a remote hold AND phase is not terminal.
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
    skipped_no_hold: list[str] = []
    protected: list[str] = []

    for restore_id, session in sessions.items():
        path = session.get("_path")
        if not isinstance(path, Path):
            path = _staging_root() / restore_id / "remote-fetch.json"

        phase = str(session.get("phase") or "")
        if not _is_protected_phase(phase):
            skipped_terminal.append(restore_id)
            continue

        target_id = str(session.get("targetId") or session.get("activeSourceTargetId") or "managed-local")
        if _is_local_target(target_id):
            skipped_local.append(restore_id)
            continue

        if not _session_has_remote_hold(session):
            skipped_no_hold.append(restore_id)
            continue

        protected.append(restore_id)
        holds = list(session.get("holds") or [])
        hold_keys = list(session.get("holdKeys") or [])
        if not hold_keys and session.get("holdKey"):
            hold_keys = [str(session["holdKey"])]

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
                        phase = str(session.get("phase") or phase)
                        if not _is_protected_phase(phase) or not _session_has_remote_hold(session):
                            skipped_terminal.append(restore_id)
                            if restore_id in protected:
                                protected.remove(restore_id)
                            continue
                        holds = list(session.get("holds") or [])
                        hold_keys = list(session.get("holdKeys") or [])
                        if not hold_keys and session.get("holdKey"):
                            hold_keys = [str(session["holdKey"])]
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
                    tmp.write_text(
                        json.dumps({k: v for k, v in session.items() if k != "_path"}, ensure_ascii=False, indent=2, sort_keys=True)
                        + "\n",
                        encoding="utf-8",
                    )
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
                    tmp.write_text(
                        json.dumps({k: v for k, v in session.items() if k != "_path"}, ensure_ascii=False, indent=2, sort_keys=True)
                        + "\n",
                        encoding="utf-8",
                    )
                    tmp.replace(path)
                except OSError:
                    pass
            failed.append({"restoreId": restore_id, "error": str(exc)})

    summary = {
        "scanned": scanned,
        "renewed": len(renewed),
        "retained": len(retained),
        "protected": len(protected),
        "skipped": len(skipped_terminal) + len(skipped_local) + len(skipped_no_hold),
        "failed": len(failed),
        "details": {
            "renewed": renewed,
            "retained": retained,
            "protected": protected,
            "skipped": skipped_terminal + skipped_local + skipped_no_hold,
            "failed": failed,
        },
    }
    _HEALTH.record_tick(summary)
    return summary


def get_recovery_lease_health() -> dict[str, Any]:
    """Report durable lease-keeper health for DR readiness."""
    sessions = scan_durable_recovery_sessions()
    protected_count = 0
    for session in sessions.values():
        phase = str(session.get("phase") or "")
        target_id = str(session.get("targetId") or session.get("activeSourceTargetId") or "managed-local")
        if _is_protected_phase(phase) and not _is_local_target(target_id) and _session_has_remote_hold(session):
            protected_count += 1

    snap = _HEALTH.snapshot()
    consecutive = int(snap.get("consecutiveFailures") or 0)
    running = bool(snap.get("keeperRunning"))
    if consecutive >= KEEPER_FAILURE_DEGRADE_THRESHOLD:
        status = "degraded"
        reason = "keeper-consecutive-failures"
    elif not running and protected_count > 0:
        status = "degraded"
        reason = "keeper-not-running"
    else:
        status = "ok"
        reason = None

    return {
        "status": status,
        "reason": reason,
        "activeLeases": protected_count,
        "totalSessions": len(sessions),
        "renewedCount": int(snap.get("totalRenewed") or 0),
        "lastTickAt": snap.get("lastTickAt"),
        "lastSuccessfulTickAt": snap.get("lastSuccessfulTickAt"),
        "consecutiveFailures": consecutive,
        "protectedJobs": max(protected_count, int(snap.get("protectedJobs") or 0)),
        "renewedLeases": int(snap.get("renewedLeases") or 0),
        "lastFailure": snap.get("lastFailure"),
        "keeperRunning": running,
    }


class RecoveryLeaseKeeper:
    """Single daemon thread for periodic background renewal of active recovery holds."""

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
        _HEALTH.mark_running(True)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        _HEALTH.mark_running(False)

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
                _HEALTH.record_tick({}, error=str(exc)[:500])
            self._stop_event.wait(self.tick_interval_seconds)


_GLOBAL_KEEPER: RecoveryLeaseKeeper | None = None
_GLOBAL_KEEPER_LOCK = threading.Lock()


def get_global_recovery_keeper() -> RecoveryLeaseKeeper:
    global _GLOBAL_KEEPER
    with _GLOBAL_KEEPER_LOCK:
        if _GLOBAL_KEEPER is None:
            _GLOBAL_KEEPER = RecoveryLeaseKeeper()
        return _GLOBAL_KEEPER


def start_global_recovery_keeper(*, reconcile_first: bool = True) -> RecoveryLeaseKeeper:
    """Startup reconciliation + unique global maintenance supervisor."""
    keeper = get_global_recovery_keeper()
    if reconcile_first:
        try:
            reconcile_durable_recovery_leases(min_renew_age_seconds=0)
        except Exception:
            _logger.exception("recovery lease startup reconciliation failed")
    keeper.start()
    return keeper


def stop_global_recovery_keeper(timeout: float = 5.0) -> None:
    keeper = get_global_recovery_keeper()
    keeper.stop(timeout=timeout)

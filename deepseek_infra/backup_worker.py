"""Standalone durable backup worker: ``python -m deepseek_infra.backup_worker``."""

from __future__ import annotations

import os
import time

from deepseek_infra.infra.workspace import backup_executor, backup_scheduler


def create_worker(instance_id: str | None = None) -> backup_scheduler.BackupWorker:
    resolved = instance_id or backup_scheduler.instance_id_from_environment()
    tick_seconds = float(os.environ.get("DEEPSEEK_BACKUP_TICK_SECONDS", "30"))
    return backup_scheduler.BackupWorker(
        lambda run: backup_executor.execute_run(run, instance_id=resolved),
        instance_id=resolved,
        tick_seconds=tick_seconds,
    )


def _startup_control_authority_allows_workers() -> bool:
    """Authority Verdict before worker loops (4.6.4)."""
    try:
        from deepseek_infra.infra.workspace import backup_control_recovery

        verdict = backup_control_recovery.resolve_startup_authority_verdict()
        return bool(verdict.get("allowWorkers"))
    except Exception:
        return False


def start_embedded_worker() -> backup_scheduler.BackupWorker | None:
    """Start the embedded worker when DEEPSEEK_BACKUP_WORKER=embedded."""
    mode = os.environ.get("DEEPSEEK_BACKUP_WORKER", "disabled").strip().lower()
    if mode != "embedded":
        return None
    if not _startup_control_authority_allows_workers():
        return None
    worker = create_worker()
    worker.start()
    return worker


def main() -> int:
    if not _startup_control_authority_allows_workers():
        return 2
    worker = create_worker()
    worker.start()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

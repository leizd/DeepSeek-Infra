"""Bounded parallel component transport for object-set publish and restore.

Object-set components are independently encrypted, content-addressed, and
invisible until commit. They are natural parallel transport units. This
scheduler enforces a global worker count and in-flight byte budget so parallel
transport never degenerates into unbounded ``gather(*everything)``.

Semantics preserved (4.4.15 contract):

* ``ObjectSetDigest`` is a sorted commitment over ciphertext digests, so
  completion order never changes it.
* The publish caller holds the writer lease; individual tasks do not fence.
* The caller advances its journal to ``objects-published`` only after
  :meth:`ComponentTransferScheduler.run` returns (the barrier). A crash leaves
  the journal at ``started`` with completed components content-addressed and
  cheaply re-verifiable on restart.

The byte budget bounds the *transport working set* per worker (chunk buffers),
not the full component size: a 64 MiB component streamed in 1 MiB chunks holds
only a few MiB of in-flight memory at a time.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_WORKERS = 4
DEFAULT_MAX_IN_FLIGHT_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_OPEN_FILES = 64
DEFAULT_WORKING_SET_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    """Frozen transport profile; safe to embed in a run plan for retry parity."""

    max_workers: int = DEFAULT_MAX_WORKERS
    max_in_flight_bytes: int = DEFAULT_MAX_IN_FLIGHT_BYTES
    max_open_files: int = DEFAULT_MAX_OPEN_FILES

    def __post_init__(self) -> None:
        if self.max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if self.max_in_flight_bytes < DEFAULT_WORKING_SET_BYTES:
            raise ValueError("max_in_flight_bytes must cover at least one working set")
        if self.max_open_files < 1:
            raise ValueError("max_open_files must be >= 1")

    @property
    def byte_permits(self) -> int:
        return max(1, self.max_in_flight_bytes // DEFAULT_WORKING_SET_BYTES)

    def as_dict(self) -> dict[str, Any]:
        return {
            "maxWorkers": self.max_workers,
            "maxInFlightBytes": self.max_in_flight_bytes,
            "maxOpenFiles": self.max_open_files,
            "workingSetBytes": DEFAULT_WORKING_SET_BYTES,
        }


@dataclass(frozen=True, slots=True)
class TransferTask:
    """One independently schedulable component transfer unit."""

    component_id: str
    ciphertext_digest: str
    ciphertext_size: int
    execute: Callable[[], None]
    working_set_bytes: int = DEFAULT_WORKING_SET_BYTES
    open_files: int = 1
    priority: int = 1

    def __post_init__(self) -> None:
        if self.working_set_bytes <= 0:
            raise ValueError("working_set_bytes must be positive")
        if self.open_files <= 0:
            raise ValueError("open_files must be positive")


class TransferTelemetry:
    """Thread-safe counters observed by tests and reported to run telemetry."""

    __slots__ = (
        "_lock",
        "active",
        "max_active",
        "in_flight_bytes",
        "max_in_flight_bytes",
        "open_files",
        "max_open_files",
        "completed",
        "failed",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.in_flight_bytes = 0
        self.max_in_flight_bytes = 0
        self.open_files = 0
        self.max_open_files = 0
        self.completed = 0
        self.failed = 0

    def enter(self, working_set_bytes: int, open_files: int) -> None:
        with self._lock:
            self.active += 1
            if self.active > self.max_active:
                self.max_active = self.active
            self.in_flight_bytes += working_set_bytes
            if self.in_flight_bytes > self.max_in_flight_bytes:
                self.max_in_flight_bytes = self.in_flight_bytes
            self.open_files += open_files
            if self.open_files > self.max_open_files:
                self.max_open_files = self.open_files

    def exit(self, working_set_bytes: int, open_files: int, *, success: bool) -> None:
        with self._lock:
            self.active -= 1
            self.in_flight_bytes -= working_set_bytes
            self.open_files -= open_files
            if success:
                self.completed += 1
            else:
                self.failed += 1

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self.active,
                "maxActive": self.max_active,
                "inFlightBytes": self.in_flight_bytes,
                "maxInFlightBytes": self.max_in_flight_bytes,
                "openFiles": self.open_files,
                "maxOpenFiles": self.max_open_files,
                "completed": self.completed,
                "failed": self.failed,
            }


class ComponentTransferScheduler:
    """Execute component transfer tasks under a bounded worker/byte budget.

    Upload and download differ only in task direction; both share concurrency,
    byte budget, cancel, and telemetry. On the first task failure the scheduler
    sets the cancel event (no new tasks start), drains in-flight tasks to a
    clean stop, then re-raises the first error. Completion order does not affect
    the result — the caller's barrier (journal/commit) fires only after run()
    returns.
    """

    __slots__ = ("_config",)

    def __init__(self, config: SchedulerConfig | None = None) -> None:
        self._config = config or SchedulerConfig()

    @property
    def config(self) -> SchedulerConfig:
        return self._config

    def run(
        self,
        tasks: Sequence[TransferTask],
        *,
        cancel_event: threading.Event | None = None,
    ) -> TransferTelemetry:
        if not tasks:
            return TransferTelemetry()
        cancel = cancel_event if cancel_event is not None else threading.Event()
        telemetry = TransferTelemetry()
        budget_cond = threading.Condition()
        first_error: list[BaseException] = []
        max_bytes = self._config.max_in_flight_bytes
        max_open_files = self._config.max_open_files
        ordered_tasks = sorted(enumerate(tasks), key=lambda item: (item[1].priority, item[0]))
        for _, task in ordered_tasks:
            if task.working_set_bytes > max_bytes:
                raise ValueError(f"task {task.component_id} working set exceeds scheduler byte budget")
            if task.open_files > max_open_files:
                raise ValueError(f"task {task.component_id} open files exceed scheduler FD budget")

        def _run_one(task: TransferTask) -> None:
            # Gate on the in-flight byte budget before claiming a slot.
            with budget_cond:
                while not cancel.is_set() and (
                    telemetry.in_flight_bytes + task.working_set_bytes > max_bytes
                    or telemetry.open_files + task.open_files > max_open_files
                ):
                    budget_cond.wait(timeout=0.1)
                if cancel.is_set():
                    return
                telemetry.enter(task.working_set_bytes, task.open_files)

            success = False
            try:
                if cancel.is_set():
                    return
                task.execute()
                success = True
            except BaseException as exc:  # noqa: BLE001 - propagate first error, drain the rest
                with budget_cond:
                    if not first_error:
                        first_error.append(exc)
                cancel.set()
            finally:
                with budget_cond:
                    telemetry.exit(task.working_set_bytes, task.open_files, success=success)
                    budget_cond.notify_all()

        import concurrent.futures

        max_workers = max(1, self._config.max_workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_run_one, task) for _, task in ordered_tasks]
            for future in concurrent.futures.as_completed(futures):
                future.result()

        if first_error:
            raise first_error[0]
        return telemetry


def default_scheduler() -> ComponentTransferScheduler:
    return ComponentTransferScheduler(SchedulerConfig())

"""Tests for the bounded component transfer scheduler.

These tests use instrumented active/max-active counters rather than elapsed
time, so they are stable under CI jitter. The scheduler's contract is:

* bounded concurrency (max_workers)
* bounded in-flight bytes (max_in_flight_bytes)
* stable priority admission and bounded open files (max_open_files)
* first error propagates, in-flight tasks drain cleanly
* cancel stops new tasks
* completion order is irrelevant to the result
"""

from __future__ import annotations

import threading
import time

import pytest

from deepseek_infra.infra.workspace.backup_component_transport import (
    DEFAULT_WORKING_SET_BYTES,
    ComponentTransferScheduler,
    SchedulerConfig,
    TransferTask,
    TransferTelemetry,
)


def _counting_task_factory():
    """Return (make_task, snapshot) where snapshot() -> (active, max_active, started, finished)."""
    lock = threading.Lock()
    state = {"active": 0, "max_active": 0, "started": 0, "finished": 0}

    def make_task(
        component_id: str,
        *,
        hold: float = 0.03,
        working_set: int = DEFAULT_WORKING_SET_BYTES,
        open_files: int = 1,
        priority: int = 1,
    ):
        def execute() -> None:
            with lock:
                state["active"] += 1
                state["started"] += 1
                if state["active"] > state["max_active"]:
                    state["max_active"] = state["active"]
            if hold:
                time.sleep(hold)
            with lock:
                state["active"] -= 1
                state["finished"] += 1

        return TransferTask(
            component_id=component_id,
            ciphertext_digest=component_id.rjust(64, "0"),
            ciphertext_size=working_set,
            execute=execute,
            working_set_bytes=working_set,
            open_files=open_files,
            priority=priority,
        )

    def snapshot() -> tuple[int, int, int, int]:
        with lock:
            return (state["active"], state["max_active"], state["started"], state["finished"])

    return make_task, snapshot


def test_scheduler_respects_max_workers():
    make_task, snapshot = _counting_task_factory()
    scheduler = ComponentTransferScheduler(SchedulerConfig(max_workers=3, max_in_flight_bytes=1024 * 1024 * 1024))
    tasks = [make_task(f"p{i:04d}") for i in range(12)]

    telemetry = scheduler.run(tasks)

    _, max_active, started, finished = snapshot()
    assert max_active >= 2, "tasks must actually run in parallel"
    assert max_active <= 3, "concurrency must not exceed max_workers"
    assert started == 12
    assert finished == 12
    assert telemetry.completed == 12
    assert telemetry.failed == 0
    assert telemetry.max_active <= 3


def test_scheduler_respects_byte_budget():
    make_task, snapshot = _counting_task_factory()
    # 8 MiB working set, 16 MiB budget -> at most 2 concurrent by bytes.
    working_set = 8 * 1024 * 1024
    scheduler = ComponentTransferScheduler(
        SchedulerConfig(max_workers=8, max_in_flight_bytes=16 * 1024 * 1024)
    )
    tasks = [make_task(f"p{i:04d}", working_set=working_set) for i in range(10)]

    telemetry = scheduler.run(tasks)

    _, max_active, _, _ = snapshot()
    assert max_active <= 2, "byte budget must cap concurrency below max_workers"
    assert telemetry.max_in_flight_bytes <= 16 * 1024 * 1024
    assert telemetry.completed == 10


def test_scheduler_honors_stable_priority_order():
    started: list[str] = []
    tasks = []
    for component_id, priority in (("output-a", 2), ("support-a", 1), ("control", 0), ("support-b", 1), ("output-b", 2)):
        tasks.append(
            TransferTask(
                component_id=component_id,
                ciphertext_digest=component_id.rjust(64, "0"),
                ciphertext_size=DEFAULT_WORKING_SET_BYTES,
                execute=lambda value=component_id: started.append(value),
                priority=priority,
            )
        )

    scheduler = ComponentTransferScheduler(SchedulerConfig(max_workers=1))
    scheduler.run(tasks)

    assert started == ["control", "support-a", "support-b", "output-a", "output-b"]


def test_scheduler_respects_independent_fd_budget():
    make_task, snapshot = _counting_task_factory()
    scheduler = ComponentTransferScheduler(
        SchedulerConfig(max_workers=6, max_in_flight_bytes=1024 * 1024 * 1024, max_open_files=3)
    )
    tasks = [make_task(f"p{i:04d}", open_files=1) for i in range(12)]

    telemetry = scheduler.run(tasks)

    _, max_active, _, _ = snapshot()
    assert max_active <= 3, "FD budget must cap concurrency independently of workers and bytes"
    assert telemetry.max_open_files <= 3
    assert telemetry.open_files == 0
    assert telemetry.completed == 12


def test_scheduler_barrier_propagates_first_error():
    make_task, snapshot = _counting_task_factory()
    boom = {"fired": False}

    def failing_execute() -> None:
        boom["fired"] = True
        raise ValueError("component p0003 failed its commitment")

    tasks = [
        make_task("p0000"),
        make_task("p0001"),
        TransferTask(
            component_id="p0003",
            ciphertext_digest="0" * 64,
            ciphertext_size=DEFAULT_WORKING_SET_BYTES,
            execute=failing_execute,
        ),
        make_task("p0004"),
        make_task("p0005"),
        make_task("p0006"),
    ]

    scheduler = ComponentTransferScheduler(SchedulerConfig(max_workers=2, max_in_flight_bytes=1024 * 1024 * 1024))
    with pytest.raises(ValueError, match="commitment"):
        scheduler.run(tasks)

    _, _, _, finished = snapshot()
    assert boom["fired"] is True
    # In-flight tasks drain to a clean stop; not all 6 need to finish.
    assert finished >= 1


def test_scheduler_cancel_event_stops_new_tasks():
    make_task, snapshot = _counting_task_factory()
    cancel = threading.Event()
    gate = threading.Event()

    def slow_execute() -> None:
        gate.set()
        time.sleep(0.05)

    tasks = [
        TransferTask(
            component_id="p0000",
            ciphertext_digest="0" * 64,
            ciphertext_size=DEFAULT_WORKING_SET_BYTES,
            execute=slow_execute,
        ),
        *[make_task(f"p{i:04d}", hold=0) for i in range(1, 20)],
    ]

    scheduler = ComponentTransferScheduler(SchedulerConfig(max_workers=1, max_in_flight_bytes=1024 * 1024 * 1024))

    # Run in a thread so we can trip the cancel after the first task starts.
    result: dict[str, object] = {}

    def _runner() -> None:
        try:
            telemetry = scheduler.run(tasks, cancel_event=cancel)
            result["telemetry"] = telemetry
        except BaseException as exc:  # noqa: BLE001
            result["error"] = exc

    runner = threading.Thread(target=_runner)
    runner.start()
    assert gate.wait(timeout=2.0)
    cancel.set()
    runner.join(timeout=5.0)

    _, _, started, _ = snapshot()
    assert started < 20, "cancel must stop new tasks from starting"
    assert "error" not in result


def test_scheduler_empty_tasks_returns_clean_telemetry():
    scheduler = ComponentTransferScheduler()
    telemetry = scheduler.run([])
    assert telemetry.completed == 0
    assert telemetry.max_active == 0
    assert telemetry.max_in_flight_bytes == 0


def test_scheduler_completion_order_is_independent():
    """Tasks with varying delays all complete; the set of results is order-free."""
    make_task, snapshot = _counting_task_factory()
    # Alternating fast/slow tasks: completion order is nondeterministic but
    # the final finished count and telemetry must be deterministic.
    tasks = []
    for i in range(8):
        hold = 0.04 if i % 2 == 0 else 0.01
        tasks.append(make_task(f"p{i:04d}", hold=hold))

    scheduler = ComponentTransferScheduler(SchedulerConfig(max_workers=4, max_in_flight_bytes=1024 * 1024 * 1024))
    telemetry = scheduler.run(tasks)

    _, _, _, finished = snapshot()
    assert finished == 8
    assert telemetry.completed == 8
    assert telemetry.failed == 0


def test_scheduler_config_validates():
    with pytest.raises(ValueError):
        SchedulerConfig(max_workers=0)
    with pytest.raises(ValueError):
        SchedulerConfig(max_in_flight_bytes=1024)
    with pytest.raises(ValueError):
        SchedulerConfig(max_open_files=0)


def test_transfer_task_validates_open_files():
    with pytest.raises(ValueError):
        TransferTask(
            component_id="invalid",
            ciphertext_digest="0" * 64,
            ciphertext_size=1,
            execute=lambda: None,
            open_files=0,
        )

    with pytest.raises(ValueError, match="working_set_bytes"):
        TransferTask(
            component_id="invalid-working-set",
            ciphertext_digest="0" * 64,
            ciphertext_size=1,
            execute=lambda: None,
            working_set_bytes=0,
        )


def test_scheduler_config_byte_permits():
    config = SchedulerConfig(max_in_flight_bytes=32 * 1024 * 1024)
    assert config.byte_permits == 4


def test_scheduler_config_as_dict_is_transport_profile():
    config = SchedulerConfig()
    assert ComponentTransferScheduler(config).config is config
    profile = config.as_dict()
    assert profile["maxWorkers"] == 4
    assert profile["maxInFlightBytes"] == 256 * 1024 * 1024
    assert profile["maxOpenFiles"] == 64
    assert profile["workingSetBytes"] == DEFAULT_WORKING_SET_BYTES


def test_scheduler_rejects_tasks_that_exceed_individual_budgets():
    config = SchedulerConfig(max_in_flight_bytes=DEFAULT_WORKING_SET_BYTES, max_open_files=1)
    scheduler = ComponentTransferScheduler(config)

    with pytest.raises(ValueError, match="byte budget"):
        scheduler.run(
            [
                TransferTask(
                    component_id="too-large",
                    ciphertext_digest="0" * 64,
                    ciphertext_size=1,
                    execute=lambda: None,
                    working_set_bytes=DEFAULT_WORKING_SET_BYTES + 1,
                )
            ]
        )
    with pytest.raises(ValueError, match="FD budget"):
        scheduler.run(
            [
                TransferTask(
                    component_id="too-many-files",
                    ciphertext_digest="1" * 64,
                    ciphertext_size=1,
                    execute=lambda: None,
                    open_files=2,
                )
            ]
        )


def test_scheduler_honors_cancel_after_budget_admission(monkeypatch: pytest.MonkeyPatch):
    cancel = threading.Event()
    executed: list[str] = []
    real_enter = TransferTelemetry.enter

    def enter_and_cancel(self: TransferTelemetry, working_set_bytes: int, open_files: int) -> None:
        real_enter(self, working_set_bytes, open_files)
        cancel.set()

    monkeypatch.setattr(TransferTelemetry, "enter", enter_and_cancel)
    telemetry = ComponentTransferScheduler().run(
        [
            TransferTask(
                component_id="cancelled-after-admission",
                ciphertext_digest="2" * 64,
                ciphertext_size=1,
                execute=lambda: executed.append("ran"),
            )
        ],
        cancel_event=cancel,
    )

    assert executed == []
    assert telemetry.failed == 1
    assert telemetry.active == 0

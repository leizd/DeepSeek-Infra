from __future__ import annotations

import sqlite3
import threading
import time
import urllib.error
from pathlib import Path

import pytest

import deepseek_infra.infra.gateway.scheduler as scheduler
from deepseek_infra.core.errors import ErrorCode
from deepseek_infra.infra.gateway.scheduler import (
    PRIORITY_AGENT,
    PRIORITY_BACKGROUND,
    PRIORITY_INTERACTIVE,
    RequestScheduler,
    SchedulerOverloaded,
    SchedulerTimeout,
    TokenBucket,
    priority_for_payload,
)


# --- token bucket ---------------------------------------------------------------

def test_token_bucket_consumes_and_refills() -> None:
    bucket = TokenBucket(rate_per_second=10, burst=2)
    base = time.monotonic()
    assert bucket.try_take(now=base) is True
    assert bucket.try_take(now=base) is True
    assert bucket.try_take(now=base) is False  # burst exhausted
    # 0.2s later at 10/s -> ~2 tokens refilled
    assert bucket.try_take(now=base + 0.25) is True


def test_token_bucket_unlimited_when_rate_zero() -> None:
    bucket = TokenBucket(rate_per_second=0, burst=1)
    assert all(bucket.try_take() for _ in range(100))
    assert bucket.available() == 1.0


# --- priority mapping -----------------------------------------------------------

def test_priority_for_payload() -> None:
    assert priority_for_payload({}) == PRIORITY_INTERACTIVE
    assert priority_for_payload({"capability": "full"}) == PRIORITY_INTERACTIVE
    assert priority_for_payload({"capability": "researcher"}) == PRIORITY_AGENT
    assert priority_for_payload({"background": True}) == PRIORITY_BACKGROUND


# --- admission ------------------------------------------------------------------

def test_disabled_scheduler_is_passthrough() -> None:
    sched = RequestScheduler(enabled=False)
    with sched.lease(kind="t"):
        pass
    assert sched.snapshot()["admitted"] == 0


def test_default_lease_is_transparent() -> None:
    sched = RequestScheduler()  # generous defaults
    for _ in range(5):
        with sched.lease(PRIORITY_INTERACTIVE, kind="t"):
            pass
    snap = sched.snapshot()
    assert snap["admitted"] == 5 and snap["inFlight"] == 0 and snap["shed"] == 0


def test_concurrency_cap_serializes_inflight() -> None:
    sched = RequestScheduler(max_concurrency=1, rate_per_second=0, acquire_timeout_seconds=5)
    events: list[str] = []
    release_a = threading.Event()
    a_entered = threading.Event()

    def worker_a() -> None:
        with sched.lease(kind="a"):
            events.append("enter-a")
            a_entered.set()
            release_a.wait(2)
            events.append("exit-a")

    def worker_b() -> None:
        with sched.lease(kind="b"):
            events.append("enter-b")

    ta = threading.Thread(target=worker_a)
    tb = threading.Thread(target=worker_b)
    ta.start()
    a_entered.wait(2)
    tb.start()
    time.sleep(0.1)
    # B cannot enter while A holds the only slot.
    assert "enter-b" not in events
    release_a.set()
    ta.join(2)
    tb.join(2)
    assert events == ["enter-a", "exit-a", "enter-b"]
    assert sched.snapshot()["peakInFlight"] == 1


def test_priority_queue_admits_high_priority_first() -> None:
    sched = RequestScheduler(max_concurrency=1, rate_per_second=0, acquire_timeout_seconds=5)
    admitted: list[str] = []
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with sched.lease(PRIORITY_INTERACTIVE, kind="hold"):
            held.set()
            release.wait(2)

    th = threading.Thread(target=holder)
    th.start()
    held.wait(2)

    def waiter(priority: int, tag: str) -> None:
        with sched.lease(priority, kind=tag):
            admitted.append(tag)

    threads = []
    for priority, tag in [(PRIORITY_BACKGROUND, "bg"), (PRIORITY_AGENT, "agent"), (PRIORITY_INTERACTIVE, "inter")]:
        t = threading.Thread(target=waiter, args=(priority, tag))
        t.start()
        threads.append(t)
        time.sleep(0.05)  # ensure all three enqueue before release
    release.set()
    for t in threads:
        t.join(2)
    th.join(2)
    assert admitted == ["inter", "agent", "bg"]


def test_backpressure_sheds_with_appstyle_503(tmp_settings: Path) -> None:
    sched = RequestScheduler(max_concurrency=1, max_queue_depth=1, acquire_timeout_seconds=5)
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with sched.lease(kind="hold"):
            held.set()
            release.wait(2)

    th = threading.Thread(target=holder)
    th.start()
    held.wait(2)
    with pytest.raises(SchedulerOverloaded) as cm:
        with sched.lease(kind="overflow"):
            pass
    assert cm.value.code == ErrorCode.RATE_LIMITED
    assert cm.value.status == 503
    assert sched.snapshot()["shed"] == 1
    release.set()
    th.join(2)


def test_rate_limiter_throttles_sequential_leases() -> None:
    sched = RequestScheduler(max_concurrency=8, rate_per_second=20, rate_burst=1, acquire_timeout_seconds=5)
    started = time.monotonic()
    for _ in range(3):
        with sched.lease(kind="r"):
            pass
    elapsed = time.monotonic() - started
    # burst=1 then ~20/s -> 2 extra tokens take ~0.1s; assert it actually waited.
    assert elapsed >= 0.05
    assert sched.snapshot()["rateLimitedWaits"] > 0


def test_acquire_timeout_raises() -> None:
    sched = RequestScheduler(max_concurrency=1, acquire_timeout_seconds=0.2)
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with sched.lease(kind="hold"):
            held.set()
            release.wait(2)

    th = threading.Thread(target=holder)
    th.start()
    held.wait(2)
    with pytest.raises(SchedulerTimeout):
        with sched.lease(kind="late"):
            pass
    assert sched.snapshot()["timedOut"] == 1
    release.set()
    th.join(2)


def test_cancellation_unblocks_waiter_and_cleans_up() -> None:
    sched = RequestScheduler(max_concurrency=1, acquire_timeout_seconds=5)

    class Cancelled(Exception):
        pass

    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with sched.lease(kind="hold"):
            held.set()
            release.wait(2)

    th = threading.Thread(target=holder)
    th.start()
    held.wait(2)

    cancel_flag = {"cancel": False}
    outcome: list[str] = []

    def cancel_checker() -> None:
        if cancel_flag["cancel"]:
            raise Cancelled()

    def canceled_waiter() -> None:
        try:
            with sched.lease(kind="c", cancel_checker=cancel_checker):
                outcome.append("admitted")
        except Cancelled:
            outcome.append("cancelled")

    tc = threading.Thread(target=canceled_waiter)
    tc.start()
    time.sleep(0.05)
    cancel_flag["cancel"] = True
    tc.join(2)
    assert outcome == ["cancelled"]
    assert sched.snapshot()["cancelled"] == 1
    # waiter was removed: the slot is free for a new request once the holder exits.
    release.set()
    th.join(2)
    assert sched.snapshot()["waiting"] == 0


# --- durable dead-letter queue --------------------------------------------------

def test_dead_letter_queue_persists_and_reports(tmp_settings: Path) -> None:
    scheduler.record_dead_letter(kind="deepseek_json", reason="retry_exhausted", key="abc", attempts=6, priority=0)
    scheduler.record_dead_letter(kind="admission", reason="backpressure_shed", priority=10)
    status = scheduler.dlq_status()
    assert status["count"] == 2
    assert status["byReason"] == {"retry_exhausted": 1, "backpressure_shed": 1}
    recent = scheduler.dead_letters(10)
    assert {row["reason"] for row in recent} == {"retry_exhausted", "backpressure_shed"}
    assert recent[0]["kind"] in {"deepseek_json", "admission"}


def test_lease_dead_letters_on_infra_failure(tmp_settings: Path) -> None:
    sched = RequestScheduler(max_concurrency=2)
    with pytest.raises(urllib.error.URLError):
        with sched.lease(kind="deepseek_json", key="k1"):
            raise urllib.error.URLError("connection reset")
    # A client-side ValueError is NOT an infra failure and must not be dead-lettered.
    with pytest.raises(ValueError):
        with sched.lease(kind="deepseek_json", key="k2"):
            raise ValueError("bad json")
    reasons = [row["reason"] for row in scheduler.dead_letters(10)]
    assert any("connection reset" in r or "URLError" in r for r in reasons)
    assert scheduler.dlq_status()["count"] == 1


def test_recover_orphans_reconciles_stale_running_rows(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue_db = tmp_settings / ".request-queue" / "queue.sqlite3"
    queue_db.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(scheduler, "GATEWAY_REQUEST_QUEUE_DB", queue_db)
    monkeypatch.setattr(scheduler, "SCHEDULER_ORPHAN_SECONDS", 60)
    stale = time.time() - 600
    fresh = time.time()
    with sqlite3.connect(queue_db) as conn:
        conn.execute(
            f"""CREATE TABLE {scheduler.REQUEST_QUEUE_TABLE} (
                queue_id TEXT PRIMARY KEY, kind TEXT, status TEXT, attempt_count INTEGER,
                updated_at REAL, last_error TEXT DEFAULT '')"""
        )
        conn.execute(f"INSERT INTO {scheduler.REQUEST_QUEUE_TABLE} VALUES ('q1','deepseek_json','running',3,?, '')", (stale,))
        conn.execute(f"INSERT INTO {scheduler.REQUEST_QUEUE_TABLE} VALUES ('q2','deepseek_stream','queued',1,?, '')", (stale,))
        conn.execute(f"INSERT INTO {scheduler.REQUEST_QUEUE_TABLE} VALUES ('q3','deepseek_json','running',1,?, '')", (fresh,))
        conn.execute(f"INSERT INTO {scheduler.REQUEST_QUEUE_TABLE} VALUES ('q4','deepseek_json','succeeded',1,?, '')", (stale,))

    recovered = scheduler.recover_orphans()
    assert recovered == 2  # q1 + q2 (stale running/queued); q3 fresh, q4 done
    with sqlite3.connect(queue_db) as conn:
        statuses = dict(conn.execute(f"SELECT queue_id, status FROM {scheduler.REQUEST_QUEUE_TABLE}").fetchall())
    assert statuses["q1"] == "failed" and statuses["q2"] == "failed"
    assert statuses["q3"] == "running" and statuses["q4"] == "succeeded"
    assert scheduler.dlq_status()["byReason"].get("recovered_on_startup") == 2


def test_recover_orphans_no_queue_db_is_noop(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler, "GATEWAY_REQUEST_QUEUE_DB", tmp_settings / "missing.sqlite3")
    assert scheduler.recover_orphans() == 0


def test_scheduler_status_shape() -> None:
    status = scheduler.scheduler_status()
    for key in ("enabled", "inFlight", "maxConcurrency", "maxQueueDepth", "deadLetterQueue"):
        assert key in status
    assert "count" in status["deadLetterQueue"]

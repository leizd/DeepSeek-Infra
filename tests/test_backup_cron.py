from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace.backup_cron import (
    iter_slots,
    load_timezone,
    next_slot,
    parse_cron,
)


UTC = timezone.utc


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def test_parse_cron_accepts_standard_expressions() -> None:
    schedule = parse_cron("0 3 * * *")
    assert schedule.minutes == frozenset({0})
    assert schedule.hours == frozenset({3})
    assert not schedule.dom_restricted
    assert not schedule.dow_restricted
    stepped = parse_cron("*/15 1-6 1,15 */2 0,7")
    assert 0 in stepped.minutes and 45 in stepped.minutes
    assert 6 in stepped.hours
    assert stepped.days_of_month == frozenset({1, 15})
    assert 0 in stepped.days_of_week and 7 not in stepped.days_of_week
    assert stepped.dom_restricted and stepped.dow_restricted


def test_parse_cron_rejects_invalid_expressions() -> None:
    for bad in ("", "* * * *", "61 * * * *", "* 25 * * *", "* * 0 * *", "* * * 13 *", "* * * * 8", "a b c d e", "*/0 * * * *", "5-1 * * * *"):
        with pytest.raises(AppError):
            parse_cron(bad)


def test_load_timezone_rejects_unknown_names() -> None:
    with pytest.raises(AppError):
        load_timezone("Not/AZone")
    assert load_timezone("Asia/Singapore").key == "Asia/Singapore"


def test_dom_and_dow_use_standard_or_semantics() -> None:
    schedule = parse_cron("0 0 1 * 1")
    # 2026-06-01 is a Monday and the 1st: matches both.
    # 2026-06-08 is a Monday but not the 1st: matches via OR semantics.
    start = _utc("2026-06-07T00:00:00Z")
    slots = list(iter_slots(schedule, "UTC", start_utc=start, end_utc=_utc("2026-06-09T00:00:00Z")))
    assert [slot.local_iso for slot in slots] == ["2026-06-08T00:00"]


def test_daily_slots_respect_timezone_offsets() -> None:
    schedule = parse_cron("0 3 * * *")
    slots = list(iter_slots(schedule, "Asia/Singapore", start_utc=_utc("2026-01-01T00:00:00Z"), end_utc=_utc("2026-01-04T00:00:00Z")))
    # 03:00 SGT on Jan 1 falls at 19:00 UTC on Dec 31 (before the window).
    assert [slot.local_iso for slot in slots] == ["2026-01-02T03:00", "2026-01-03T03:00", "2026-01-04T03:00"]
    first = slots[0]
    assert first.scheduled_for == _utc("2026-01-01T19:00:00Z")
    assert first.slot_key == "2026-01-02T03:00@Asia/Singapore"


def test_dst_spring_gap_skips_by_default() -> None:
    schedule = parse_cron("30 2 * * *")
    # US/Eastern 2026-03-08: 02:30 does not exist.
    slots = list(iter_slots(schedule, "America/New_York", start_utc=_utc("2026-03-07T00:00:00Z"), end_utc=_utc("2026-03-10T00:00:00Z")))
    locals_seen = [slot.local_iso for slot in slots]
    assert "2026-03-08T02:30" not in locals_seen
    assert locals_seen == ["2026-03-07T02:30", "2026-03-09T02:30"]
    assert slots[0].scheduled_for == _utc("2026-03-07T07:30:00Z")
    assert slots[1].scheduled_for == _utc("2026-03-09T06:30:00Z")


def test_dst_spring_gap_runs_once_when_configured() -> None:
    schedule = parse_cron("30 2 * * *")
    slots = list(
        iter_slots(
            schedule,
            "America/New_York",
            start_utc=_utc("2026-03-08T00:00:00Z"),
            end_utc=_utc("2026-03-09T12:00:00Z"),
            misfire_policy="run-once",
        )
    )
    assert [slot.local_iso for slot in slots] == ["2026-03-08T02:30", "2026-03-09T02:30"]
    assert slots[0].misfire_adjusted
    # The gap-adjusted run fires at the first valid local time after the gap (03:00 EDT).
    assert slots[0].scheduled_for == _utc("2026-03-08T07:00:00Z")


def test_dst_fall_back_runs_repeated_wall_time_once() -> None:
    schedule = parse_cron("30 1 * * *")
    # US/Eastern 2026-11-01: 01:30 occurs twice; only the first occurrence runs.
    slots = list(iter_slots(schedule, "America/New_York", start_utc=_utc("2026-10-31T00:00:00Z"), end_utc=_utc("2026-11-03T00:00:00Z")))
    locals_seen = [slot.local_iso for slot in slots]
    assert locals_seen == ["2026-10-31T01:30", "2026-11-01T01:30", "2026-11-02T01:30"]
    assert slots[1].scheduled_for == _utc("2026-11-01T05:30:00Z")


def test_iter_slots_rejects_windows_beyond_lookahead() -> None:
    schedule = parse_cron("0 0 * * *")
    with pytest.raises(AppError):
        list(iter_slots(schedule, "UTC", start_utc=_utc("2026-01-01T00:00:00Z"), end_utc=_utc("2026-01-01T00:00:00Z") + timedelta(days=401)))


def test_iter_slots_empty_for_inverted_window() -> None:
    schedule = parse_cron("0 0 * * *")
    assert list(iter_slots(schedule, "UTC", start_utc=_utc("2026-01-02T00:00:00Z"), end_utc=_utc("2026-01-01T00:00:00Z"))) == []


def test_next_slot_finds_first_future_slot() -> None:
    schedule = parse_cron("0 3 * * *")
    slot = next_slot(schedule, "UTC", after_utc=_utc("2026-06-01T04:00:00Z"))
    assert slot is not None
    assert slot.local_iso == "2026-06-02T03:00"


def test_next_slot_handles_naive_datetimes() -> None:
    schedule = parse_cron("0 3 * * *")
    slot = next_slot(schedule, "UTC", after_utc=datetime(2026, 6, 1, 4, 0, 0))
    assert slot is not None

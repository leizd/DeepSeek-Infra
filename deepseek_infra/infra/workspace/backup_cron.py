"""Cron parsing and IANA-timezone schedule slot computation for backup policies."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from deepseek_infra.core.errors import AppError, ErrorCode

_MAX_LOOKAHEAD_DAYS = 400


@dataclass(frozen=True, slots=True)
class CronSchedule:
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool


@dataclass(frozen=True, slots=True)
class ScheduleSlot:
    scheduled_for: datetime
    local_iso: str
    timezone: str
    slot_key: str
    misfire_adjusted: bool


def _parse_field(field: str, minimum: int, maximum: int, *, allow_seven: bool = False) -> frozenset[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError("empty cron field part")
        base = part
        step = 1
        if "/" in part:
            base, raw_step = part.split("/", 1)
            step = int(raw_step)
            if step <= 0:
                raise ValueError("cron step must be positive")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            raw_start, raw_end = base.split("-", 1)
            start, end = int(raw_start), int(raw_end)
        else:
            start = end = int(base)
        if start < minimum or end > maximum or start > end:
            raise ValueError(f"cron field value out of range {minimum}-{maximum}: {part}")
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError("cron field selects no values")
    if allow_seven and 7 in values:
        values.discard(7)
        values.add(0)
    return frozenset(values)


def parse_cron(text: str) -> CronSchedule:
    parts = str(text or "").split()
    if len(parts) != 5:
        raise AppError("Cron expression must have five fields", code=ErrorCode.INVALID_PAYLOAD)
    try:
        minutes = _parse_field(parts[0], 0, 59)
        hours = _parse_field(parts[1], 0, 23)
        dom = _parse_field(parts[2], 1, 31)
        months = _parse_field(parts[3], 1, 12)
        dow = _parse_field(parts[4], 0, 7, allow_seven=True)
    except (ValueError, TypeError) as exc:
        raise AppError(f"Invalid cron expression: {exc}", code=ErrorCode.INVALID_PAYLOAD) from exc
    return CronSchedule(
        minutes=minutes,
        hours=hours,
        days_of_month=dom,
        months=months,
        days_of_week=dow,
        dom_restricted=parts[2] != "*",
        dow_restricted=parts[4] != "*",
    )


def load_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or ""))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AppError(f"Unknown IANA timezone: {name}", code=ErrorCode.INVALID_PAYLOAD) from exc


def _date_matches(schedule: CronSchedule, day: datetime) -> bool:
    if day.month not in schedule.months:
        return False
    dom_match = day.day in schedule.days_of_month
    cron_weekday = day.isoweekday() % 7
    dow_match = cron_weekday in schedule.days_of_week
    if schedule.dom_restricted and schedule.dow_restricted:
        return dom_match or dow_match
    return dom_match and dow_match


def _resolve_local(naive: datetime, tz: ZoneInfo) -> datetime | None:
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        roundtrip = aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
        if roundtrip == naive:
            return naive.replace(tzinfo=tz, fold=0)
    return None


def _gap_transition(naive: datetime, tz: ZoneInfo) -> datetime:
    candidate = naive
    for _ in range(180):
        candidate += timedelta(minutes=1)
        resolved = _resolve_local(candidate, tz)
        if resolved is not None:
            return resolved
    raise AppError("Unable to resolve DST transition for schedule slot", code=ErrorCode.INVALID_REQUEST)


def iter_slots(
    schedule: CronSchedule,
    tz_name: str,
    *,
    start_utc: datetime,
    end_utc: datetime,
    misfire_policy: str = "skip",
) -> Iterator[ScheduleSlot]:
    tz = load_timezone(tz_name)
    start = _as_utc(start_utc)
    end = _as_utc(end_utc)
    if end <= start:
        return
    if (end - start).days > _MAX_LOOKAHEAD_DAYS:
        raise AppError("Schedule slot window exceeds 400 days", code=ErrorCode.INVALID_REQUEST)
    first_day = start.astimezone(tz).date() - timedelta(days=1)
    last_day = end.astimezone(tz).date() + timedelta(days=1)
    emitted: set[datetime] = set()
    day = first_day
    while day <= last_day:
        probe = datetime.combine(day, time(0, 0))
        if _date_matches(schedule, probe):
            for hour in sorted(schedule.hours):
                for minute in sorted(schedule.minutes):
                    naive = datetime.combine(day, time(hour, minute))
                    misfire_adjusted = False
                    aware = _resolve_local(naive, tz)
                    if aware is None:
                        if misfire_policy != "run-once":
                            continue
                        aware = _gap_transition(naive, tz)
                        misfire_adjusted = True
                    utc_instant = aware.astimezone(timezone.utc)
                    if utc_instant in emitted:
                        continue
                    if start <= utc_instant < end:
                        emitted.add(utc_instant)
                        yield ScheduleSlot(
                            scheduled_for=utc_instant,
                            local_iso=naive.isoformat(timespec="minutes"),
                            timezone=tz_name,
                            slot_key=f"{naive:%Y-%m-%dT%H:%M}@{tz_name}",
                            misfire_adjusted=misfire_adjusted,
                        )
        day += timedelta(days=1)


def next_slot(
    schedule: CronSchedule,
    tz_name: str,
    *,
    after_utc: datetime,
    misfire_policy: str = "skip",
) -> ScheduleSlot | None:
    for slot in iter_slots(
        schedule,
        tz_name,
        start_utc=after_utc,
        end_utc=_as_utc(after_utc) + timedelta(days=_MAX_LOOKAHEAD_DAYS),
        misfire_policy=misfire_policy,
    ):
        return slot
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

"""Leveranspolicy för notiser: tysta timmar, dygnsgräns och omförsök (rena funktioner)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo


def parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def in_quiet_hours(local: datetime, quiet_start: str, quiet_end: str) -> bool:
    """Sant om klockslaget ligger i det tysta fönstret (som får passera midnatt)."""
    start, end = parse_hhmm(quiet_start), parse_hhmm(quiet_end)
    if start == end:
        return False
    now = local.time().replace(second=0, microsecond=0)
    if start < end:
        return start <= now < end
    return now >= start or now < end


def local_day_start(now: datetime, tz: ZoneInfo) -> datetime:
    """Lokal midnatt (i UTC) för dygnet som ``now`` ligger i."""
    local = now.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def local_week_start(now: datetime, tz: ZoneInfo) -> datetime:
    """Måndag 00:00 lokal tid (i UTC) för veckan som ``now`` ligger i."""
    local = now.astimezone(tz)
    monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.astimezone(UTC)


def local_month_start(now: datetime, tz: ZoneInfo) -> datetime:
    local = now.astimezone(tz)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def retry_delay(attempts: int) -> timedelta:
    """Exponentiell väntan mellan leveransförsök: 2, 4, 8 … högst 60 minuter."""
    return timedelta(minutes=min(60, 2 ** max(1, min(attempts, 6))))


def remaining_today(max_per_day: int, sent_today: int) -> int | None:
    """Antal notiser som får skickas till i dag. None = ingen gräns (max_per_day = 0)."""
    if max_per_day <= 0:
        return None
    return max(0, max_per_day - sent_today)

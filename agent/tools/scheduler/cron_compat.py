"""Cron parser compatibility for the scheduler.

The production path prefers the ``croniter`` package. A small fallback keeps
the scheduler importable in lean Web/runtime environments and supports the
common 5-field cron shapes used by reminders, such as ``30 9 * * *``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Set

try:  # pragma: no cover - exercised when the optional package is installed.
    from croniter import croniter as _external_croniter
except Exception:  # pragma: no cover - exact ImportError text varies.
    _external_croniter = None


class _FallbackCroniter:
    def __init__(self, expression: str, start_time: datetime | None = None):
        self.expression = str(expression or "").strip()
        self.current = start_time or datetime.now()
        fields = self.expression.split()
        if len(fields) != 5:
            raise ValueError(f"unsupported cron expression: {self.expression!r}")
        self.minutes = _parse_field(fields[0], 0, 59)
        self.hours = _parse_field(fields[1], 0, 23)
        self.days = _parse_field(fields[2], 1, 31)
        self.months = _parse_field(fields[3], 1, 12)
        self.weekdays = _parse_field(fields[4], 0, 7)
        if 7 in self.weekdays:
            self.weekdays.add(0)

    def get_next(self, ret_type=datetime):
        candidate = (self.current + timedelta(minutes=1)).replace(second=0, microsecond=0)
        deadline = candidate + timedelta(days=366)
        while candidate <= deadline:
            if self._matches(candidate):
                self.current = candidate
                return candidate if ret_type is datetime else candidate.timestamp()
            candidate += timedelta(minutes=1)
        raise ValueError(f"no next cron time found within 366 days: {self.expression!r}")

    def _matches(self, value: datetime) -> bool:
        cron_weekday = (value.weekday() + 1) % 7
        return (
            value.minute in self.minutes
            and value.hour in self.hours
            and value.day in self.days
            and value.month in self.months
            and cron_weekday in self.weekdays
        )


def _parse_field(field: str, minimum: int, maximum: int) -> Set[int]:
    raw = str(field or "").strip()
    if not raw:
        raise ValueError("empty cron field")
    values: Set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            step = int(step_text)
            if step <= 0:
                raise ValueError("cron step must be positive")
        if part == "*":
            start, end = minimum, maximum
        elif "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(part)
        if start < minimum or end > maximum or start > end:
            raise ValueError(f"cron field out of range: {raw!r}")
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError("empty cron value set")
    return values


def croniter(expression: str, start_time: datetime | None = None):
    if _external_croniter is not None:
        return _external_croniter(expression, start_time)
    return _FallbackCroniter(expression, start_time)


def using_external_croniter() -> bool:
    return _external_croniter is not None

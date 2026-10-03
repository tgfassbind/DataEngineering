from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo


class Source(str, Enum):
    """The two places a day's CSV can be downloaded from."""
    PERMALINK = "permalink" # current calendar month -> one CSV per day
    ARCHIVE = "archive" # earlier months -> one ZIP per month


def today_in(tz_name: str, reference_date: date | None = None) -> date:
    """Return the date that counts as "today" for routing."""
    if reference_date is not None:
        return reference_date
    return datetime.now(ZoneInfo(tz_name)).date()


def expand_range(start: date, end: date) -> list[date]:
    """Return every day from start to end, both included."""
    if end < start:
        raise ValueError(f"end date {end} is before start date {start}")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def route(day: date, today: date) -> Source:
    """Decided per date, so a range across a month boundary uses both sources.

    Rules:
      1. date in the current month -> PERMALINK
      2. today is the 1st and date is yesterday -> PERMALINK
         (yesterday belongs to the previous month, whose archive ZIP is only
         published by the first working day of the new month; downloading a
         whole month's archive for one day would also be wasteful)
      3. any other date in an earlier month -> ARCHIVE
    """
    # Rule 1: same year and month as today
    if (day.year, day.month) == (today.year, today.month):
        return Source.PERMALINK
    # Earlier month
    if (day.year, day.month) < (today.year, today.month):
        # Rule 2: on the 1st, yesterday
        if today.day == 1 and day == today - timedelta(days=1):
            return Source.PERMALINK
        # Rule 3: everything else in the past
        return Source.ARCHIVE
    # Later month: no data can exist yet.
    raise ValueError(f"{day} is in a future month (today is {today})")


def group_by_month(days: list[date]) -> "OrderedDict[tuple[int, int], list[date]]":
    """Group days by (year, month), oldest month first, days sorted.

    Used for the archive path: each monthly ZIP is downloaded once and all
    requested days of that month are taken from it, instead of downloading
    the same ZIP once per day.
    """
    groups: OrderedDict[tuple[int, int], list[date]] = OrderedDict()
    for d in sorted(days):
        groups.setdefault((d.year, d.month), []).append(d)
    return groups

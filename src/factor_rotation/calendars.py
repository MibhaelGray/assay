"""The NYSE trading calendar, read once.

A lookup through pandas_market_calendars costs about a tenth of a second, and one dashboard build makes dozens.
This holds every session from 2018 to three years ahead and slices it. A date outside that goes to the library.
"""

from datetime import date, datetime, time, timedelta
from functools import lru_cache

import pandas as pd
import pandas_market_calendars as mcal

from .config import NY


@lru_cache(maxsize=1)
def _sessions() -> pd.DatetimeIndex:
    return mcal.get_calendar("XNYS").valid_days(date(2018, 1, 1), datetime.now(NY).date() + timedelta(days=3 * 365))


class _NYSE:
    def valid_days(self, start, end) -> pd.DatetimeIndex:
        days = _sessions()
        first, last = (pd.Timestamp(pd.Timestamp(d).date(), tz="UTC") for d in (start, end))
        if first < pd.Timestamp(2018, 1, 1, tz="UTC") or last > days[-1]:
            return mcal.get_calendar("XNYS").valid_days(start, end)
        return days[days.searchsorted(first):days.searchsorted(last, side="right")]


NYSE = _NYSE()

CLOSE = time(16, 0)   # New York: the regular close


@lru_cache(maxsize=1)
def _early() -> dict[date, time]:
    """Sessions that close early (the day after Thanksgiving, Christmas Eve, some July 3rds) -> their close, New York."""
    cal = mcal.get_calendar("XNYS")
    closes = cal.early_closes(cal.schedule(date(2018, 1, 1), datetime.now(NY).date() + timedelta(days=3 * 365)))["market_close"]
    return {ts.date(): ts.tz_convert("America/New_York").time() for ts in closes}


def close_time(day: date) -> time:
    """The regular session's close on `day`, New York time: 4:00 PM, or 1:00 PM on a half day."""
    return _early().get(day, CLOSE)


def is_half_day(day: date) -> bool:
    return day in _early()

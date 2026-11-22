"""Dated events the lab can test around. So far: earnings reports, from Nasdaq's calendar (2010 on).

Each report has its date, the reported and forecast earnings per share, the surprise in percent and the number of
estimates. The calendar doesn't say whether a past report came before the open or after the close, so the market's
reaction is on the report date or the session after; `window` covers both and an idea decides from that day's own
price and volume, never from the day after. Like the prices, the calendar lists only companies that still trade.
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from . import DATA
from .panel import Panel

log = logging.getLogger(__name__)
CACHE = DATA / "earnings"


def _num(text) -> float:
    t = str(text or "").replace("$", "").replace(",", "").strip()
    if not t or t == "N/A":
        return float("nan")
    try:
        return -float(t.strip("()")) if t.startswith("(") else float(t)
    except ValueError:
        return float("nan")


def _day(day: pd.Timestamp) -> list[dict]:
    from ..smallcap import sources
    path = CACHE / f"{day:%Y-%m-%d}.json"
    if path.exists():
        return json.loads(path.read_text())
    for attempt in range(4):
        try:
            data = json.loads(sources._get(sources.EARNINGS.format(day=f"{day:%Y-%m-%d}")))["data"] or {}
            break
        except Exception as e:
            log.warning("lab_earnings_retry day=%s error=%r", day.date(), e)
            time.sleep(10 * (attempt + 1))
    else:
        return []
    rows = [{"symbol": r["symbol"].strip(), "eps": _num(r.get("eps")), "forecast": _num(r.get("epsForecast")),
             "surprise": _num(r.get("surprise")), "estimates": _num(r.get("noOfEsts")), "time": r.get("time", "")}
            for r in data.get("rows") or []]
    path.write_text(json.dumps(rows))
    return rows


def pull(sessions: pd.DatetimeIndex, threads: int = 4) -> int:
    """Fetch every session's reports that aren't on disk yet. The last ten days are always fetched again."""
    CACHE.mkdir(parents=True, exist_ok=True)
    for day in sessions[-10:]:
        (CACHE / f"{day:%Y-%m-%d}.json").unlink(missing_ok=True)
    todo = [d for d in sessions if not (CACHE / f"{d:%Y-%m-%d}.json").exists()]
    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(_day, todo))
    return len(todo)


def earnings() -> pd.DataFrame:
    """Every report on disk: date, symbol, eps, forecast, surprise (percent), estimates."""
    rows = []
    for path in sorted(CACHE.glob("*.json")):
        for r in json.loads(path.read_text()):
            rows.append({"date": pd.Timestamp(path.stem), **r})
    return pd.DataFrame(rows)


def reports(p: Panel, value: str = "flag") -> pd.DataFrame:
    """Reports laid out like the price tables: `value` on each stock's report date, gaps elsewhere. `value="flag"`
    puts 1.0 there; "surprise" the surprise in percent; "eps", "forecast", "estimates" likewise."""
    key = ("reports", value)
    if key not in p._cache:
        e = earnings()
        e = e[e.symbol.isin(p.close.columns) & e.date.isin(p.dates)].assign(flag=1.0)
        e = e.dropna(subset=[value]).drop_duplicates(["date", "symbol"], keep="last")
        p._cache[key] = e.pivot(index="date", columns="symbol", values=value).reindex(index=p.dates, columns=p.close.columns)
    return p._cache[key]


def window(p: Panel, before: int = 0, after: int = 1) -> pd.DataFrame:
    """True on the sessions from `before` ahead of a report date to `after` past it. The calendar doesn't say whether
    a past report came before the open or after the close, so the market's reaction falls on the report date or the
    session after: `window(p)` covers both. `before` > 0 looks ahead to a date the market knew in advance (companies
    confirm report dates two to four weeks out), not to anything about the report."""
    flag = reports(p).notna()
    out = flag.copy()
    for k in range(1, after + 1):
        out |= flag.shift(k, fill_value=False)
    for k in range(1, before + 1):
        out |= flag.shift(-k, fill_value=False)
    return out


def latest(p: Panel, value: str = "surprise", sessions: int = 1) -> pd.DataFrame:
    """The most recent report's `value`, carried for `sessions` sessions after the report date (so it can be read on
    the reaction day whichever of the two it is)."""
    return reports(p, value).ffill(limit=sessions)

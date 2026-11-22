"""The journal's shared pieces: the trading calendar, the nightly brief, and the vocabulary of a proposal.

  brief -> journal/briefs/<as_of>.json   the night's market inputs: regime, factor pairs, technicals, forced flows

The rest of the journal is written elsewhere: the screen (screen.py), the earnings radar (earnings.py) and the
paper book with its research proposals (paper/). This module holds what they share: SETUPS and REASONING (the
vocabulary of a proposal), what counts as a tradable symbol, and the helpers that read a plan's legs and stop.
"""

import json
import os
import re
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd

from . import storage
from .analytics import flows
from .analytics.technicals import factor_pair_technicals, regime, symbol_technicals
from .config import NY
from .bars import bars_path, load
from .calendars import NYSE, close_time, is_half_day  # noqa: F401  (re-exported)
from .universe import CONSTITUENTS, ETFS, INDEXES, bar_universe, idea_universe

JOURNAL = Path(os.environ.get("FR_JOURNAL_DIR", "journal"))


def _calendar():
    return NYSE


def is_trading_day(day: date) -> bool:
    return not _calendar().valid_days(day, day).empty


def next_trading_day(as_of: date) -> date:
    days = _calendar().valid_days(as_of + pd.Timedelta(days=1), as_of + pd.Timedelta(days=10))
    return days[0].date()


def previous_trading_day(day: date) -> date:
    days = _calendar().valid_days(day - pd.Timedelta(days=10), day - pd.Timedelta(days=1))
    return days[-1].date()


# A side of a trade is one symbol, or a basket held in equal weight. Any US-listed stock or ETF can be a leg or
# a member, inside the standard universe or not; bars for names outside it are pulled with the paper book's
# legs (book.symbols_held). Indexes can't be bought, so they can't be legs.
BASKET_SIZE = (3, 10)
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,6}$")
NOT_TRADABLE = {*INDEXES, "SPX", "NDX", "RUT", "DJI", "SOX", "VIX1D", "VIX9D", "VIX6M", "FVX", "TYX"}


def _json_default(x):
    if isinstance(x, (np.floating, np.integer)):
        return None if isinstance(x, np.floating) and np.isnan(x) else x.item()
    if isinstance(x, (pd.Timestamp, datetime, date)):
        return x.isoformat()
    if isinstance(x, np.bool_):
        return bool(x)
    raise TypeError(type(x))


def _records(df: pd.DataFrame) -> list[dict]:
    df = df.copy()
    numeric = df.select_dtypes("number").columns
    df[numeric] = df[numeric].round(5)
    clean = df.replace([np.inf, -np.inf], np.nan).astype(object).where(df.notna(), None)
    return clean.to_dict("records")


LEVERAGED_ASSETS = JOURNAL / "reference" / "leveraged_assets.json"

# The setup a proposal came from, kept with it so results can be compared by setup. The screen (`fr screen`)
# finds the first sixteen, and the live screen adds live_move; "catalyst" and "other" cover the research pass's own ideas.
SETUPS = {
    "earnings_gap_held": "gapped at least 1.5 ATR on earnings, closed strong on heavy volume and held",
    "earnings_gap_faded": "gapped on earnings and closed weak, or good news sold / bad news bought",
    "breakout": "closed above its 52-week or 20-day high on heavy volume",
    "breakdown": "closed below its 52-week or 20-day low on heavy volume",
    "reclaim_200": "closed back above its 200-day after a month or more below, on heavy volume",
    "lost_200": "closed below its 200-day after a month or more above, on heavy volume",
    "relative_strength": "rose at least 1 ATR on a day SPY fell 1% or more",
    "relative_weakness": "fell at least 1 ATR on a day SPY rose 1% or more",
    "turn": "stretched (RSI or distance from the 20-day) and turned: a strong close against a losing streak",
    "stretched": "stretched with no turn yet",
    "basket_breakout": "a basket's spread against its hedge made a 20-day high on a big day",
    "basket_breakdown": "a basket's spread against its hedge made a 20-day low on a big day",
    "basket_big_move": "a basket's spread moved at least 2 of its daily standard deviations",
    "basket_stretched": "a basket's spread at least 2 standard deviations from its 50-day average",
    "factor_turn": "a stretched factor pair turned against the stretch",
    "factor_stretched": "a factor pair at least 2 standard deviations from its 50-day average",
    "live_move": "moving at least 2 ATR right now: outside regular hours on steady trading, or today on heavy volume",
    "catalyst": "built around a scheduled event",
    "other": "none of the above",
}

# The five steps behind every take, kept with it so the reasoning can be checked against the outcome.
REASONING = {
    "what_changed": "what the screen or the tape showed, with numbers",
    "story": "why it moved, with sources and dates",
    "chart": "what the technicals said, for and against",
    "coming": "events inside the time limit",
    "case_against": "the strongest case against, and why it didn't win",
}

def build_brief(as_of: date, write: bool = True) -> dict:
    """The night's market inputs, saved to the journal. `write=False` is the dashboard's unsaved version from the
    latest bars, which may be a part-finished day: the research passes read only what is saved."""
    all_bars = load(bar_universe())
    all_bars = all_bars[all_bars["date"].dt.date <= as_of]
    bars = all_bars[all_bars["symbol"].isin(idea_universe())]
    techs = symbol_technicals(bars)
    pairs = factor_pair_technicals(bars)
    reference = json.loads(LEVERAGED_ASSETS.read_text()) if LEVERAGED_ASSETS.exists() else None
    brief = {
        "as_of": as_of.isoformat(),
        "built_at": datetime.now(NY).isoformat(),
        "bars_through": str(bars["date"].max().date()) if not bars.empty else None,
        "regime": regime(bars) if not bars.empty else {},
        "factor_pairs": _records(pairs),
        "etf_technicals": _records(techs[techs["symbol"].isin(ETFS)]),
        "constituent_technicals": _records(techs[techs["symbol"].isin(CONSTITUENTS)]),
        "forced_flows": flows.build(all_bars, as_of, reference) if not all_bars.empty else {},
    }
    if write:
        path = JOURNAL / "briefs" / f"{as_of}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(brief, indent=2, default=_json_default) + "\n")
    return brief


def _side(idea: dict, side: str) -> list[str]:
    v = idea.get(side)
    return list(v) if isinstance(v, list) else ([v] if v else [])


def _legs(idea: dict) -> list[str]:
    return [*_side(idea, "long"), *_side(idea, "short")]


def _weights(idea: dict) -> dict[str, float]:
    """Each symbol's weight: +1 split evenly over the long side, -1 over the short side."""
    w = {}
    for side, sign in (("long", 1), ("short", -1)):
        members = _side(idea, side)
        for s in members:
            w[s] = sign / len(members)
    return w


def _bars_settled(trade_date: date) -> bool:
    """Whether the stored bars were pulled after this session's close. A dashboard refresh during the session
    stores a partial bar for the day, which is no result."""
    pulled = storage.modified(bars_path("SPY"))
    close = datetime.combine(trade_date, time(16, 15), NY)
    return pulled is not None and pulled.astimezone(NY) >= close


def sessions_from(start: date, n: int) -> list[date]:
    """The first n trading sessions starting with `start`."""
    return [d.date() for d in _calendar().valid_days(start, start + pd.Timedelta(days=n * 2 + 10))[:n]]


def _rule_hit(rule: dict, price: float) -> bool:
    return price < rule["below"] if "below" in rule else price > rule["above"]


def rule_text(rule: dict) -> str:
    side = "below" if "below" in rule else "above"
    return f"{rule['symbol']} {side} {rule[side]:,.2f}"


def stop_text(idea: dict) -> str:
    stop = idea.get("stop") or {}
    parts = [f"closes {rule_text(r)}" for r in stop.get("rules", [])]
    if "spread_pct" in stop:
        parts.append(f"the idea is down {abs(stop['spread_pct']):g}% from entry at a close")
    return " or ".join(parts)

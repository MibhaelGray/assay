"""The latest prices for the live screen: what each name is doing right now, in whichever session is trading.

A pass doesn't judge yesterday's close (the owner, 2026-11-15). Before each of the three daily passes the screen
looks at every name's latest price: after-hours for the night pass, pre-market plus the first minutes of the
session for the morning pass, the morning's trading for the midday pass.

Yahoo's 5-minute bars with pre-market and after-hours, one pull for the whole universe (about 10 seconds for
900 names). Two things Yahoo doesn't give:
- Volume outside regular hours: the bars are there but their volume is mostly zero (tested 2026-11-15: SPY's
  48 after-hours bars summed to 0 shares). So how actively a name is trading outside regular hours is counted in
  bars: how many 5-minute bars printed a trade. SPY prints in every one; a thin name in two or three.
- IBKR's overnight session (8:00 PM to 3:50 AM ET). None of the three passes starts inside it.

Yahoo finds candidates and sizes trades. It never prices an order: that takes the gateway's bid and ask.
"""

import logging
from datetime import date, datetime, time

import pandas as pd

from . import bars
from .config import CT, NY

log = logging.getLogger(__name__)

OPEN, CLOSE = time(9, 30), time(16, 0)
BAR = pd.Timedelta(minutes=5)
SESSION_MINUTES = 390
# The share of a day's volume that has usually traded by each minute after the open: heavy at the open, quiet
# over lunch, and about a seventh of the day in the last fifteen minutes with the closing auction. Used to turn
# "volume so far" into a full day's worth, so a name isn't called quiet at noon for having traded half a day.
# A rule of thumb for US stocks, not fitted to our names; the screen says "projected" wherever it is used.
VOLUME_CURVE = [(0, 0.0), (15, 0.07), (30, 0.12), (60, 0.20), (90, 0.27), (120, 0.33), (150, 0.38), (180, 0.43),
                (210, 0.48), (240, 0.53), (270, 0.58), (300, 0.64), (330, 0.71), (360, 0.80), (375, 0.86), (390, 1.0)]


def volume_fraction(minutes: float) -> float:
    """The usual share of the day's volume traded `minutes` after the open."""
    if minutes <= 0:
        return 0.0
    if minutes >= SESSION_MINUTES:
        return 1.0
    for (m0, f0), (m1, f1) in zip(VOLUME_CURVE, VOLUME_CURVE[1:]):
        if m0 <= minutes <= m1:
            return f0 + (f1 - f0) * (minutes - m0) / (m1 - m0)
    return 1.0


def snapshot(df: pd.DataFrame) -> dict | None:
    """One name's latest session from its 5-minute bars (indexed by New York time, pre-market and after-hours
    included): the regular session so far, the trading before and after it, and the latest price of all.

    {"date", "price", "at", "phase": "pre" | "regular" | "post",
     "regular": {"open", "high", "low", "close", "volume", "minutes", "fraction"} | None,
     "pre": {"last", "bars"} | None, "post": {"last", "bars"} | None}
    `minutes` is how far into the session the last regular bar ends and `fraction` the usual share of a day's
    volume by then; `bars` counts the 5-minute bars that printed a trade."""
    df = df.dropna(subset=["Close"])
    if df.empty:
        return None
    day = df.index[-1].date()
    df = df[df.index.date == day]
    t = df.index.time
    pre, reg, post = df[t < OPEN], df[(t >= OPEN) & (t < CLOSE)], df[t >= CLOSE]
    out = {"date": day, "price": float(df["Close"].iloc[-1]), "at": (df.index[-1] + BAR).isoformat(),
           "phase": "post" if len(post) else "regular" if len(reg) else "pre", "regular": None, "pre": None, "post": None}
    if len(reg):
        end = reg.index[-1] + BAR
        minutes = min(SESSION_MINUTES, (end.hour - OPEN.hour) * 60 + end.minute - OPEN.minute)
        out["regular"] = {"open": float(reg["Open"].iloc[0]), "high": float(reg["High"].max()), "low": float(reg["Low"].min()),
                          "close": float(reg["Close"].iloc[-1]), "volume": float(reg["Volume"].sum()), "minutes": int(minutes),
                          "fraction": round(volume_fraction(minutes), 4)}
    for name, part in (("pre", pre), ("post", post)):
        if len(part):
            out[name] = {"last": float(part["Close"].iloc[-1]), "bars": int(len(part))}
    return out


def fetch(symbols: list[str]) -> dict[str, dict]:
    """symbol -> snapshot of its latest session, for every name Yahoo answers for. On a weekend that session is
    Friday's, after-hours included: the latest prices there are."""
    import yfinance as yf

    tickers = {bars._ticker(s): s for s in symbols}
    try:
        with bars._yahoo:
            data = yf.download(list(tickers), period="1d", interval="5m", prepost=True, auto_adjust=False, group_by="ticker",
                               threads=bars.PULL_THREADS, progress=False)
    except Exception as e:  # no network yet, or Yahoo refusing
        log.warning("live_prices_failed error=%r", e)
        return {}
    if data is None or data.empty:
        return {}
    idx = pd.DatetimeIndex(data.index)
    data.index = (idx.tz_localize("UTC") if idx.tz is None else idx).tz_convert(NY)
    have = set(data.columns.get_level_values(0)) if isinstance(data.columns, pd.MultiIndex) else set()
    out = {}
    for ticker, symbol in tickers.items():
        df = data[ticker] if ticker in have else data if len(tickers) == 1 else None
        snap = snapshot(df) if df is not None and "Close" in df else None
        if snap:
            out[symbol] = snap
    return out


def prices(symbols: list[str]) -> dict[str, float]:
    """symbol -> its latest price in any session. For sizing a trade and marking a verdict, never for an order."""
    return {s: v["price"] for s, v in fetch(symbols).items()}


def provisional_bar(snap: dict) -> dict | None:
    """Today's daily bar so far, from a snapshot with regular-session trading: the session's open, high and low,
    the latest regular price as the close, and the volume scaled up to a full day's worth."""
    r = snap.get("regular")
    if not r or not r["fraction"]:
        return None
    return {"date": pd.Timestamp(snap["date"]), "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
            "volume": r["volume"] / r["fraction"]}


def clock(at: str | datetime) -> str:
    """A time for a reader: 6:55 PM CT."""
    at = datetime.fromisoformat(at) if isinstance(at, str) else at
    return f"{at.astimezone(CT):%-I:%M %p} CT"


def session_of(d: date) -> str:
    return pd.Timestamp(d).strftime("%a %b %-d")

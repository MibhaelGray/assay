"""From an idea's daily scores to a daily return after costs.

An idea hands over one table of scores, sessions down and stocks across: a higher score means "more worth buying",
a gap means "not in play today". Every score is known at that day's close. The engine then:

  1. Picks names each day. `select="rank"` buys the top `frac` (or top `n`) and shorts the bottom; `select="sign"`
     buys every positive score and shorts every negative one (for event ideas, where most days most names are gaps).
  2. Enters at the next session's open (`entry="open"`, the default and the honest one) or at the same close
     (`entry="close"`: only for ideas whose score can truly be worked out a few minutes before the close).
  3. Holds `hold` sessions and leaves at that session's close (`exit="close"`) or at the following open
     (`exit="open"`). A fresh slice of the book goes on every day, so `hold` slices overlap, each 1/hold of the book.
  4. Charges each trade a cost that depends on how much the stock trades (COST_BPS), and each night short a borrow fee.

Sizes: `side="ls"` puts half the book long and half short. `side="long"` or `"short"` puts all of it one way, or with
`hedge=True` half in the names and half the other way in the market's benchmark fund. Returns are per dollar of
book, so a 1.0 Sharpe here needs no leverage to read.

Nothing here looks ahead: day d's return uses only weights decided on day d-1 or earlier.
"""

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .panel import Panel

VERSION = 1   # raise when a change here would alter results; it is part of every trial's fingerprint
KEYS = {"side", "select", "frac", "n", "hold", "entry", "exit", "hedge", "max_w", "min_names", "cost_mult", "weight"}
# One-way cost in basis points (half the spread, slippage and commission) by the dollars a stock trades on a median
# day. Orders here hit the bid or ask, so half a spread each way is the floor. `cost_mult=2` is the stress test.
COST_BPS = ((200e6, 2.0), (50e6, 4.0), (20e6, 6.0), (5e6, 12.0), (1e6, 30.0), (0.0, 60.0))
FUND_BPS = 1.0
BORROW = ((20e6, 0.01), (0.0, 0.04))   # yearly fee to borrow a short, by the same measure; hard-to-borrow names cost more than this


@dataclass
class Result:
    net: pd.Series        # daily return after costs
    gross: pd.Series
    cost: pd.Series
    turnover: pd.Series   # dollars traded per dollar of book, one way
    names: pd.Series      # positions opened that day
    bench: pd.Series      # the benchmark's close-to-close return
    spec: dict


def cost_table(p: Panel) -> pd.DataFrame:
    """One-way cost as a fraction, per stock per day, from the dollars it traded up to that day."""
    if "cost" not in p._cache:
        adv = p.adv.fillna(0).to_numpy()
        c = np.full(adv.shape, COST_BPS[-1][1])
        for floor, bps in reversed(COST_BPS):
            c = np.where(adv >= floor, bps, c)
        from .panel import MARKETS
        fee = MARKETS.get(p.market, {}).get("fee_bps", 0.0)
        table = pd.DataFrame((c + fee) / 1e4, index=p.close.index, columns=p.close.columns)
        table[[f for f in p.funds if f in table]] = (FUND_BPS + fee) / 1e4
        p._cache["cost"] = table
    return p._cache["cost"]


def borrow_table(p: Panel) -> pd.DataFrame:
    if "borrow" not in p._cache:
        adv = p.adv.fillna(0).to_numpy()
        p._cache["borrow"] = pd.DataFrame(np.where(adv >= BORROW[0][0], BORROW[0][1], BORROW[1][1]) / 252,
                                          index=p.close.index, columns=p.close.columns)
    return p._cache["borrow"]


def pick(score: pd.DataFrame, side: str = "ls", select: str = "rank", frac: float = 0.1, n: int | None = None,
         max_w: float = 0.05, min_names: int = 20, weight: str = "equal") -> pd.DataFrame:
    """Each day's target weights within one fully invested book: longs sum to at most +1, shorts to at most -1
    (run() halves them for a long-short book). No name is over `max_w` of its side, so a day with three events
    leaves most of the book in cash rather than a third of it in each."""
    s = score.to_numpy(dtype=float)
    live = ~np.isnan(s)
    count = live.sum(axis=1, keepdims=True)
    if select == "rank":
        order = score.rank(axis=1, method="first").to_numpy()
        k = np.full_like(count, n, dtype=float) if n else np.ceil(count * frac)
        k = np.where(count >= min_names, np.minimum(k, count // 2), 0)
        long, short = live & (order > count - k), live & (order <= k)
    elif select == "sign":
        long, short = live & (s > 0), live & (s < 0)
    else:
        raise ValueError(select)
    if weight == "score":   # in proportion to the size of the score, within a side
        mag = np.abs(np.nan_to_num(s))
        wl = np.where(long, mag, 0.0)
        ws = np.where(short, mag, 0.0)
        wl = np.minimum(wl / np.maximum(wl.sum(axis=1, keepdims=True), 1e-12), max_w)
        ws = np.minimum(ws / np.maximum(ws.sum(axis=1, keepdims=True), 1e-12), max_w)
    else:
        wl = np.where(long, np.minimum(1 / np.maximum(long.sum(axis=1, keepdims=True), 1), max_w), 0.0)
        ws = np.where(short, np.minimum(1 / np.maximum(short.sum(axis=1, keepdims=True), 1), max_w), 0.0)
    if side == "long":
        ws = np.zeros_like(ws)
    elif side == "short":
        wl = np.zeros_like(wl)
    elif side != "ls":
        raise ValueError(side)
    return pd.DataFrame(wl - ws, index=score.index, columns=score.columns)


def run(p: Panel, score: pd.DataFrame, *, hold: int = 3, side: str = "ls", select: str = "rank", frac: float = 0.1,
        n: int | None = None, entry: str = "open", exit: str = "close", hedge: bool = False, max_w: float = 0.05,
        min_names: int = 20, cost_mult: float = 1.0, weight: str = "equal") -> Result:
    if not 1 <= hold <= 10:
        raise ValueError("hold is 1 to 10 sessions")
    score = score.reindex(index=p.close.index)
    w = pick(score, side, select, frac, n, max_w, min_names, weight)
    scale = 0.5 if side == "ls" or hedge else 1.0
    opened = (w != 0).sum(axis=1)
    w = (w * scale).reindex(columns=p.close.columns, fill_value=0.0)
    if hedge and side != "ls":
        w[p.bench] = -w.sum(axis=1)
    used = (w != 0).any(axis=0).to_numpy()                 # only the columns that ever hold a position
    w = w.loc[:, used]
    cc, oc, co = (getattr(p, f).loc[:, used].fillna(0.0) for f in ("cc", "oc", "co"))
    W = w.to_numpy()

    def lag(k: int) -> np.ndarray:
        out = np.zeros_like(W)
        out[k:] = W[:-k]
        return out

    R_cc, R_oc, R_co = cc.to_numpy(), oc.to_numpy(), co.to_numpy()
    pnl = np.zeros(len(w))
    if entry == "open":
        pnl += (lag(1) * R_oc).sum(axis=1)
        for k in range(2, hold + 1):
            pnl += (lag(k) * R_cc).sum(axis=1)
        if exit == "open":
            pnl += (lag(hold + 1) * R_co).sum(axis=1)
        last = hold + 1 if exit == "open" else hold      # the lag of the slice that leaves today
        nights = hold if exit == "open" else hold - 1
    elif entry == "close":
        full = hold if exit == "close" else hold - 1
        for k in range(1, full + 1):
            pnl += (lag(k) * R_cc).sum(axis=1)
        if exit == "open":
            pnl += (lag(hold) * R_co).sum(axis=1)
        last, nights = hold, hold
    else:
        raise ValueError(entry)
    pnl /= hold

    C = cost_table(p).loc[:, used].to_numpy() * cost_mult
    first = 1 if entry == "open" else 0                   # the lag of the slice that goes on today
    contiguous = (entry == "open" and exit == "open") or (entry == "close" and exit == "close")
    if contiguous:
        # A name picked again as its slice ends is simply kept, so only the change in the whole book is traded.
        book = sum(lag(k) for k in range(first, first + hold)) / hold if first else (W + sum(lag(k) for k in range(1, hold))) / hold
        prev = np.zeros_like(book)
        prev[1:] = book[:-1]
        traded = np.abs(book - prev)
    else:
        # The position is flat between leaving and coming back (overnight, or over the day), so both trades are paid.
        traded = (np.abs(lag(first) if first else W) + np.abs(lag(last))) / hold
    cost = (traded * C).sum(axis=1)
    held = sum(np.abs(np.minimum(lag(k), 0)) for k in range(1, hold + 1)) / hold
    held[:, [w.columns.get_loc(f) for f in p.funds if f in w.columns]] *= 0.25   # an index fund borrows cheaply
    borrow = (held * borrow_table(p).loc[:, used].to_numpy()).sum(axis=1) * (nights / hold)
    idx = w.index
    gross = pd.Series(pnl, idx)
    cost = pd.Series(cost + borrow, idx)
    spec = dict(hold=hold, side=side, select=select, frac=frac, n=n, entry=entry, exit=exit, hedge=hedge, max_w=max_w,
                min_names=min_names, cost_mult=cost_mult, weight=weight)
    return Result(net=gross - cost, gross=gross, cost=cost, turnover=pd.Series(traded.sum(axis=1), idx),
                  names=opened, bench=p.cc[p.bench].fillna(0.0), spec=spec)


def forward(p: Panel, hold: int, entry: str = "open", exit: str = "close") -> pd.DataFrame:
    """What each stock returned over the hold that a score on that day would get: the quick look an idea file can use
    to check a signal before building a book on it. Not a backtest (no costs, no overlap)."""
    if entry == "open":
        start = p.open.shift(-1)
        end = p.close.shift(-hold) if exit == "close" else p.open.shift(-hold - 1)
    else:
        start = p.close
        end = p.close.shift(-hold) if exit == "close" else p.open.shift(-hold)
    return end / start - 1


def sessions_per_year() -> float:
    return 252.0


def annual(x: float) -> float:
    return x * sessions_per_year()


def root_time(x: float) -> float:
    return x * math.sqrt(sessions_per_year())

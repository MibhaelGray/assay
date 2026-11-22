import pandas as pd

from ..registry import Idea

# Daily price limits: Taiwan 7% until 2015-06-01 and 10% after; Korea 15% until 2015-06-15 and 30% after.
LIMITS = {"tw": [("2015-06-01", 0.07, 0.10)], "kr": [("2015-06-15", 0.15, 0.30)]}

IDEA = Idea(
    id="limit_up", family="microstructure", market="tw",
    title="Buy stocks the day after they close at (or near) the daily price limit, hedged with the index fund",
    why=("Taiwan and Korea cap how far a stock may move in a day. A stock that closes locked at its upper limit had more "
         "buyers than the cap let trade, so part of the day's price move is pushed into the next session (the delayed "
         "price discovery found in Taiwan by Kim and Rhee 1997 and Chen 1998). Buying at a locked close is mostly not "
         "possible, so the test enters at the next open, which gives up the gap and asks whether the move goes on after "
         "it. Stocks that rose nearly to the limit without locking could be bought at the close, so that is tested too. "
         "IBKR allows no shorting in either market: long only, against the index fund (standing for a future)."),
    source="Kim and Rhee 1997 (Journal of Finance), Chen 1998; the 'magnet effect' of price limits",
    universe=dict(min_price=1, min_adv=2e6),
    base=dict(side="long", select="sign", hedge=True, hold=1, max_w=0.1, min_names=1, how="locked"),
    variants={"locked_h1": {}, "locked_h2": dict(hold=2), "locked_h3": dict(hold=3),
              "locked_h1_open_exit": dict(exit="open"),
              "near_close_entry_h1": dict(how="near", entry="close"),
              "near_close_entry_h3": dict(how="near", entry="close", hold=3),
              "locked_liquid_h2": dict(hold=2, universe=dict(min_adv=10e6))},
)


def _limit(p) -> pd.Series:
    rule = LIMITS.get(p.market, LIMITS["tw"])[0]
    return pd.Series([rule[1] if d < pd.Timestamp(rule[0]) else rule[2] for d in p.dates], index=p.dates)


def score(p, u, how="locked"):
    price = p.price
    move = price / price.shift(1) - 1               # split-adjusted, so a dividend day is off by the dividend
    limit = _limit(p)
    at_limit = move.ge(limit * 0.97, axis=0)        # tick sizes round the limit price down a little
    locked = at_limit & (p.high.sub(p.close).abs() <= p.close * 1e-6)   # closed at the high: no sellers left
    if how == "locked":
        hit = locked
    else:                                           # rose most of the way but did not lock: buyable at the close
        hit = move.ge(limit * 0.7, axis=0) & ~at_limit
    return move.where(hit & u)

import numpy as np
import pandas as pd

from .. import DATA, feat
from ..registry import Idea

IDEA = Idea(
    id="us_lead", family="cross-market", market="jp",
    title="Asian and Australian markets from their open to their close, in the direction New York moved overnight",
    why=("Tokyo, Hong Kong, Seoul, Taipei and Sydney open a few hours after New York closes. The opening gap prices most "
         "of the US move, but local investors who trade through the day (and foreign money that arrives during it) may "
         "keep adjusting, so the rest of the session could continue the US move. This is a question about how fast "
         "local prices absorb overnight US news. Entry is at the local open, after the US close is known, and exit at "
         "the local close. In Korea and Taiwan the short days stand for an index future (no ETF shorting). Run per market as us_lead@hk and so on; every market gets the same variants."),
    source="Lead-lag between US and Asian markets: Eun and Shim 1989; Becker, Finnerty and Gupta 1990",
    base=dict(universe=dict(funds="bench"), side="ls", select="sign", hold=1, entry="open", exit="close", min_names=1, max_w=1.0, cut=0.0, level="market"),
    variants={"market_all": {}, "market_big_1pct": dict(cut=0.01), "market_big_2pct": dict(cut=0.02),
              "market_long_only_up": dict(side="long"),
              "stocks_beta": dict(level="stock", select="rank", frac=0.1, min_names=20, max_w=0.05,
                                  universe=dict(min_price=1, min_adv=10e6, top=500)),
              "stocks_beta_big_1pct": dict(level="stock", select="rank", frac=0.1, min_names=20, max_w=0.05, cut=0.01,
                                           universe=dict(min_price=1, min_adv=10e6, top=500))},
)


def _us(dates: pd.DatetimeIndex) -> pd.Series:
    """SPY's close-to-close return on each US date, set against the local session of the same date: the local
    trade it drives opens on the next local session, after that US close."""
    spy = pd.read_parquet(DATA / "us" / "close.parquet", columns=["SPY"])["SPY"]
    return (spy / spy.shift(1) - 1).reindex(dates)


def score(p, u, cut=0.0, level="market"):
    us = _us(p.dates)
    big = us.where(us.abs() >= cut)
    if level == "market":
        out = pd.DataFrame(np.nan, index=p.dates, columns=u.columns)
        for c in u.columns:
            out[c] = big
        return out
    # Stock level: how much each stock's own open-to-close has moved with the prior US day, over the last 250
    # sessions (known at today's close), times tonight's US move. Long the names expected to continue the most.
    oc = p.oc[u.columns]
    lagged = us.shift(1)    # the US day that came before each local session
    cov = oc.mul(lagged, axis=0).rolling(250, min_periods=200).mean() - oc.rolling(250, min_periods=200).mean().mul(lagged.rolling(250, min_periods=200).mean(), axis=0)
    beta = cov.div(lagged.rolling(250, min_periods=200).var(), axis=0)
    return feat.within(beta.mul(big, axis=0), u)

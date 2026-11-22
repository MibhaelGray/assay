import numpy as np

from .. import feat
from ..registry import Idea

IDEA = Idea(
    id="rev_resid", family="reversal",
    title="Buy the week's losers and short its winners, after taking out the market and the sector",
    why=("A stock pushed down by someone who needed to sell, with no news about the company, tends to come back once the "
         "selling stops; whoever takes the other side is paid for it. Taking out the market and sector move leaves the part "
         "most likely to be that kind of pressure. Known since Lehmann (1990) and Jegadeesh (1990); the open question is "
         "whether anything is left after costs in names liquid enough to trade."),
    source="Lehmann 1990; Jegadeesh 1990; Da, Liu, Schaumburg 2014 (residual reversal)",
    universe=dict(min_price=5, min_adv=20e6, top=1000),
    base=dict(side="ls", frac=0.1, hold=3, lookback=5),
    variants={"lb5_h3": {}, "lb5_h1": dict(hold=1), "lb5_h5": dict(hold=5), "lb1_h1": dict(lookback=1, hold=1),
              "lb3_h3": dict(lookback=3), "lb10_h5": dict(lookback=10, hold=5),
              "lb5_h3_open_exit": dict(exit="open"),
              "lb1_h1_at_close": dict(lookback=1, hold=1, entry="close"),
              "mid_liquidity": dict(universe=dict(min_adv=5e6, max_adv=20e6, top=None)),
              "top300": dict(universe=dict(top=300))},
)


def score(p, u, lookback=5):
    move = feat.residual(p).rolling(lookback, min_periods=lookback).sum()
    typical = feat.vol(p, 60).shift(lookback) * np.sqrt(lookback)
    return feat.sector_demean(feat.within(-(move / typical), u), p.sector())

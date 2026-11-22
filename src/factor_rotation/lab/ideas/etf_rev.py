import numpy as np

from .. import feat
from ..registry import Idea

FUNDS = "XLK XLF XLE XLV XLI XLY XLP XLU XLB SMH XBI KRE XOP GDX ITB XRT IYT".split()

IDEA = Idea(
    id="etf_rev", family="reversal",
    title="Among sector and industry funds, buy the week's laggards against the leaders",
    why=("Sector funds trade for a basis point or two, so a reversal too small to survive a stock's spread could survive "
         "here. Flows into and out of sector funds push them away from the market for days at a time and then relax. Against "
         "it: at one month, sectors show momentum (Moskowitz and Grinblatt 1999), so the week may simply be noise."),
    source="own hypothesis; costs are the reason to try it at fund level",
    universe=dict(funds=FUNDS),
    base=dict(side="ls", n=3, hold=3, lookback=5, min_names=10, max_w=0.34),
    variants={"lb5_h3": {}, "lb5_h1": dict(hold=1), "lb5_h5": dict(hold=5), "lb1_h1": dict(lookback=1, hold=1),
              "lb10_h5": dict(lookback=10, hold=5), "lb5_h3_at_close": dict(entry="close"),
              "follow_lb5_h5": dict(flip=True, hold=5)},
)


def score(p, u, lookback=5, flip=False):
    move = feat.residual(p)[u.columns].rolling(lookback, min_periods=lookback).sum()
    z = move / (feat.vol(p, 60)[u.columns].shift(lookback) * np.sqrt(lookback))
    return z if flip else -z

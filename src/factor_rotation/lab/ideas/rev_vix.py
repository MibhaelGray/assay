import numpy as np

from .. import feat
from ..registry import Idea

IDEA = Idea(
    id="rev_vix", family="reversal",
    title="The same loser-minus-winner trade, but only on days the market is frightened",
    why=("The reversal trade is payment for supplying liquidity, and that payment rises when dealers are stretched. Nagel "
         "(2012) found its return tracks the VIX closely. If most of the edge sits in stressed weeks, trading only then "
         "should keep the return and cut the cost of trading in quiet weeks, when the spread eats it."),
    source="Nagel 2012, Evaporating Liquidity",
    universe=dict(min_price=5, min_adv=20e6, top=1000),
    base=dict(side="ls", frac=0.1, hold=3, lookback=5, rule="above_avg"),
    variants={"above_60d_avg": {}, "vix_over_20": dict(rule="over", level=20), "vix_over_25": dict(rule="over", level=25),
              "backwardated": dict(rule="backwardated"), "above_avg_h1": dict(hold=1), "above_avg_h5": dict(hold=5)},
)


def score(p, u, lookback=5, rule="above_avg", level=20):
    move = feat.residual(p).rolling(lookback, min_periods=lookback).sum()
    typical = feat.vol(p, 60).shift(lookback) * np.sqrt(lookback)
    s = feat.sector_demean(feat.within(-(move / typical), u), p.sector())
    vix = feat.gauge(p, "VIX")
    if rule == "above_avg":
        on = vix > vix.rolling(60).mean()
    elif rule == "over":
        on = vix > level
    else:
        on = vix > feat.gauge(p, "VIX3M")
    return s.where(on, axis=0)

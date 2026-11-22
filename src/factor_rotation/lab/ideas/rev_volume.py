import numpy as np

from .. import feat
from ..registry import Idea

IDEA = Idea(
    id="rev_volume", family="reversal",
    title="Quiet moves reverse, loud moves carry on",
    why=("A price move on thin volume is more likely to be one trader's pressure, which fades; a move on heavy volume is "
         "more likely to be news, which the price keeps adjusting to (Llorente, Michaely, Saar, Wang 2002). So fade the "
         "week's residual move when the week's volume was below normal, and follow it when volume was well above."),
    source="Llorente et al. 2002; Campbell, Grossman, Wang 1993",
    universe=dict(min_price=5, min_adv=20e6, top=1000),
    base=dict(side="ls", frac=0.1, hold=3, lookback=5, mode="quiet_fade"),
    variants={"quiet_fade": {}, "quiet_fade_h1": dict(hold=1), "quiet_fade_h5": dict(hold=5),
              "loud_follow": dict(mode="loud_follow"), "loud_follow_h5": dict(mode="loud_follow", hold=5),
              "loud_fade": dict(mode="loud_fade")},
)


def score(p, u, lookback=5, mode="quiet_fade"):
    move = feat.residual(p).rolling(lookback, min_periods=lookback).sum()
    typical = feat.vol(p, 60).shift(lookback) * np.sqrt(lookback)
    z = feat.within(move / typical, u)
    recent = p.volume.rolling(lookback).mean() / p.volume.shift(lookback).rolling(60, min_periods=40).mean()
    recent = recent.reindex(columns=z.columns)
    if mode == "quiet_fade":
        return feat.sector_demean((-z).where(recent < 0.8), p.sector())
    if mode == "loud_follow":
        return feat.sector_demean(z.where(recent > 1.5), p.sector())
    return feat.sector_demean((-z).where(recent > 1.5), p.sector())

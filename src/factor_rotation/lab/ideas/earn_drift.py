import numpy as np
import pandas as pd

from .. import events, feat
from ..registry import Idea

IDEA = Idea(
    id="earn_drift", family="earnings",
    title="After an earnings report the market liked (or hated), follow it for a few days",
    why=("Prices keep moving in the direction of an earnings surprise for weeks (Ball and Brown 1968; Bernard and Thomas "
         "1989), and the first days carry the largest share. The market's own reaction on the day is a better read of the "
         "surprise than the headline number (Chan, Jegadeesh, Lakonishok 1996; Brandt et al. 2008), and agreement between "
         "the two should be best. The drift has shrunk in large stocks; smaller ones are where it should survive."),
    source="Bernard and Thomas 1989; Brandt, Kishore, Santa-Clara, Venkatachalam 2008 (earnings announcement return)",
    universe=dict(min_price=5, min_adv=5e6),
    base=dict(side="ls", select="sign", hold=5, sigmas=2.0, need="both", max_w=0.05),
    variants={"both_h5": {}, "both_h3": dict(hold=3), "both_h1": dict(hold=1), "reaction_h5": dict(need="reaction"),
              "surprise_h5": dict(need="surprise"), "both_h5_big": dict(sigmas=3.0),
              "both_h5_longs": dict(side="long", hedge=True), "both_h5_shorts": dict(side="short", hedge=True),
              "both_h5_smaller": dict(universe=dict(min_adv=2e6, max_adv=20e6)),
              "both_h5_liquid": dict(universe=dict(min_adv=50e6)),
              "disagree_h5": dict(need="disagree")},
)


def score(p, u, sigmas=2.0, need="both"):
    """Decided on the first session of the report date and the one after on which the stock moves `sigmas` of its own
    daily range on 1.5x its normal volume: that session is taken as the reaction."""
    z = feat.residual(p) / feat.vol(p, 60).shift(1)
    live = events.window(p) & (feat.rel_volume(p) >= 1.5)
    surprise = events.latest(p, "surprise")
    up, down = live & (z >= sigmas), live & (z <= -sigmas)
    if need == "both":
        up, down = up & (surprise > 0), down & (surprise < 0)
    elif need == "surprise":
        big = events.window(p) & surprise.notna()
        up, down = big & (surprise >= 10), big & (surprise <= -10)
    elif need == "disagree":   # a beat the market sold, a miss it bought: follow the market, not the number
        up, down = up & (surprise < 0), down & (surprise > 0)
    s = pd.DataFrame(np.nan, index=p.dates, columns=p.close.columns).mask(up, 1.0).mask(down, -1.0)
    first = s.notna() & ~s.shift(1).notna()        # the first of the two sessions only
    return feat.within(s.where(first), u)

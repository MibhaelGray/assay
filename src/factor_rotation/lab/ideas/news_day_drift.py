import numpy as np
import pandas as pd

from .. import feat
from ..registry import Idea

IDEA = Idea(
    id="news_day_drift", family="momentum",
    title="A huge move on huge volume that closes at its extreme keeps going for a few days",
    why=("A move of three or more of a stock's own daily ranges on several times its normal volume is almost always news. "
         "Prices are slow to take in news in full (the same slowness behind the drift after earnings), and a close at the "
         "day's extreme says the buying or selling had not finished by the bell. Follow it from the next open."),
    source="Chan 2003 (news and drift); Bernard and Thomas 1989; this project's small-cap study found the opposite sign in $250M-$1B names",
    universe=dict(min_price=5, min_adv=10e6),
    base=dict(side="ls", select="sign", hold=3, sigmas=3.0, volume=2.5, location=0.75, max_w=0.1),
    variants={"h3": {}, "h1": dict(hold=1), "h5": dict(hold=5), "h5_bigger": dict(hold=5, sigmas=4.0, volume=4.0),
              "h3_longs_hedged": dict(side="long", hedge=True), "h3_shorts_hedged": dict(side="short", hedge=True),
              "h3_any_close": dict(location=0.0), "fade_h3": dict(flip=True)},
)


def score(p, u, sigmas=3.0, volume=2.5, location=0.75, flip=False):
    z = feat.residual(p) / feat.vol(p, 60).shift(1)
    loud = feat.rel_volume(p) >= volume
    where = feat.close_location(p)
    up = (z >= sigmas) & loud & (where >= location)
    down = (z <= -sigmas) & loud & (where <= 1 - location)
    s = pd.DataFrame(np.nan, index=p.dates, columns=p.close.columns).mask(up, 1.0).mask(down, -1.0)
    return feat.within(-s if flip else s, u)

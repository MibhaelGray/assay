import numpy as np
import pandas as pd

from .. import feat
from ..registry import Idea

IDEA = Idea(
    id="breakout", family="momentum",
    title="A first new high on strong volume, bought and hedged with the market",
    why=("Investors anchor on the past year's high and are slow to bid through it, so good news near the high is taken in "
         "late (George and Hwang 2004). A close at a new high on heavy volume, the first in a month, is the moment that "
         "resistance gives way. The effect is documented over months; whether the first week holds any of it is the test."),
    source="George and Hwang 2004 (52-week high)",
    universe=dict(min_price=5, min_adv=10e6),
    base=dict(side="long", select="sign", hedge=True, hold=5, window=252, volume=1.5, max_w=0.1),
    variants={"y1_h5": {}, "y1_h3": dict(hold=3), "y1_h1": dict(hold=1), "d55_h5": dict(window=55), "d55_h3": dict(window=55, hold=3),
              "y1_h5_loud": dict(volume=3.0), "y1_low_short_h5": dict(low=True, side="short")},
)


def score(p, u, window=252, volume=1.5, low=False):
    c = p.close
    past = c.shift(1).rolling(window, min_periods=window)
    new = (c < past.min()) if low else (c > past.max())
    first = new & ~(new.shift(1).rolling(20, min_periods=1).max().fillna(0).astype(bool))
    hit = first & (feat.rel_volume(p) >= volume)
    s = pd.DataFrame(np.nan, index=p.dates, columns=c.columns).mask(hit, -1.0 if low else 1.0)
    return feat.within(s, u)

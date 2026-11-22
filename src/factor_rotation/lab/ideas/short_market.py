import numpy as np
import pandas as pd

from ..registry import Idea
from ..sources import short_volume

IDEA = Idea(
    id="short_market", family="short-flow",
    title="Buy the index when off-exchange short volume across large stocks is unusually high",
    why=("Added up across the largest stocks, the share of off-exchange volume marked short is a gauge of how much buying "
         "market makers absorbed that day (they sell short to fill buyers). SqueezeMetrics' dark-pool index is this "
         "number, and its claim is that high readings come before above-average index returns over the following days "
         "and weeks. The mirror test is that low readings come before weak ones."),
    source="SqueezeMetrics 2018 (DIX white paper)",
    universe=dict(funds=["SPY", "IWM"]),
    base=dict(side="long", select="sign", hold=5, entry="open", max_w=1.0, fund="SPY", top=0.2),
    variants={"high_h5": {}, "high_h3": dict(hold=3), "high_h1": dict(hold=1), "top_tenth_h5": dict(top=0.1),
              "low_h5_short": dict(low=True, side="short"), "high_iwm_h5": dict(fund="IWM"), "middle_h5": dict(middle=True)},
)


def score(p, u, fund="SPY", top=0.2, low=False, middle=False):
    short, total = short_volume.tables(p)
    big = p.universe(min_price=5, min_adv=20e6, top=500)
    dollars = p.price[big.columns].where(big)
    index = (short[big.columns] * dollars).sum(axis=1) / (total[big.columns] * dollars).sum(axis=1).replace(0, np.nan)
    place = index.rolling(250, min_periods=120).rank(pct=True)      # where today sits in the past year
    on = (place <= top) if low else (place.between(0.4, 0.6) if middle else (place >= 1 - top))
    s = pd.DataFrame(np.nan, index=p.dates, columns=u.columns)
    s[fund] = np.where(on, -1.0 if low else 1.0, np.nan)
    return s

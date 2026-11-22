import numpy as np
import pandas as pd

from .. import events, feat
from ..registry import Idea

IDEA = Idea(
    id="earn_premium", family="earnings",
    title="Own stocks over their earnings report, hedged with the market",
    why=("Stocks earn more than usual in the days around their report, on average and whatever the news: a report draws "
         "attention and buying from people who cannot easily short, and holders are paid for carrying the uncertainty "
         "(Beaver 1968; Frazzini and Lamont 2007; Barber, De George, Lehavy, Trueman 2013). Report dates are known weeks "
         "ahead, so this needs no forecast. The run-up variant owns the week before and leaves before the news."),
    source="Frazzini and Lamont 2007; Barber et al. 2013",
    universe=dict(min_price=5, min_adv=10e6),
    base=dict(side="long", select="sign", hedge=True, hold=2, entry="close", mode="over", max_w=0.05),
    variants={"over_2d": {}, "over_2d_liquid": dict(universe=dict(min_adv=50e6)),
              "over_2d_small": dict(universe=dict(min_adv=2e6, max_adv=20e6)),
              "over_2d_past_beaters": dict(mode="over_beaters"), "runup_5d": dict(mode="runup", hold=5),
              "runup_3d": dict(mode="runup", hold=3, days=3), "over_2d_high_volume_names": dict(mode="over_attention")},
)


def score(p, u, mode="over", days=5):
    flag = events.reports(p).notna()
    if mode == "runup":
        # in at the close `days`+1 sessions before the report date, out at the close the session before it
        on = flag.shift(-(days + 1), fill_value=False)
    else:
        on = flag.shift(-1, fill_value=False)       # in at the close before the report date, out two closes later
    s = pd.DataFrame(np.nan, index=p.dates, columns=p.close.columns).mask(on, 1.0)
    if mode == "over_beaters":       # only names whose last two reports both beat the forecast
        beat = events.reports(p, "surprise") > 0
        last_two = beat.where(events.reports(p, "surprise").notna()).ffill().fillna(False).astype(bool)
        prior = events.reports(p, "surprise").shift(1).ffill()   # the previous value carried forward
        s = s.where(last_two.shift(1, fill_value=False) & (prior > 0))
    elif mode == "over_attention":   # names whose past reports drew the most extra volume (Frazzini-Lamont's sort)
        jump = feat.rel_volume(p).where(events.window(p)).rolling(260, min_periods=1).mean()
        s = s.where(feat.rank(feat.within(jump.shift(2), u)) >= 0.67)
    return feat.within(s, u)

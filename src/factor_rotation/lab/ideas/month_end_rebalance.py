import numpy as np
import pandas as pd

from ..registry import Idea

IDEA = Idea(
    id="month_end_rebalance", family="calendar",
    title="At month end, lean against whichever of shares and bonds won the month",
    why=("Pension and balanced funds hold fixed mixes of shares and bonds and trade back to them at month end. After a "
         "month in which shares beat bonds by a lot they must sell shares and buy bonds in the last days, and the reverse. "
         "The size of the flow is known from the month's returns before it happens. Trade the index fund against the "
         "long bond fund for the last few sessions, in the direction the rebalancing pushes."),
    source="Etula, Rinne, Suominen, Vaittinen 2020 (Dash for Cash); Harvey et al. 2021 on rebalancing flows",
    universe=dict(funds=["SPY", "TLT"]),
    base=dict(side="ls", select="sign", hold=1, entry="close", max_w=1.0, last=3, gap=0.03),
    variants={"last3_gap3": {}, "last5_gap3": dict(last=5), "last3_gap5": dict(gap=0.05), "last3_any": dict(gap=0.0),
              "last2_gap3": dict(last=2), "first3_reverse_gap3": dict(first=3)},
)


def score(p, u, last=3, gap=0.03, first=0):
    days = pd.Series(1, index=p.dates)
    month = p.dates.to_period("M")
    from_end = days[::-1].groupby(month[::-1]).cumsum()[::-1]
    from_start = days.groupby(month).cumsum()
    c = p.close[["SPY", "TLT"]]
    start = c.groupby(month).transform("first")
    lead = (c["SPY"] / start["SPY"]) - (c["TLT"] / start["TLT"])       # shares less bonds, month to date
    s = pd.DataFrame(np.nan, index=p.dates, columns=u.columns)
    if first:      # the mirror: the pressure lifts in the first days of the new month
        prior = lead.groupby(month).transform("last").shift(1).where(from_start == 1).ffill()
        on = (from_start.shift(-1) <= first) & (from_start.shift(-1) >= 1) & (prior.shift(-1).abs() >= gap)
        side = np.sign(prior.shift(-1))
    else:
        on = (from_end.shift(-1) <= last) & (from_end > 1) & (lead.abs() >= gap)   # tomorrow is one of the last sessions
        side = -np.sign(lead)
    s["SPY"] = np.where(on, side, np.nan)
    s["TLT"] = np.where(on, -side, np.nan)
    return s

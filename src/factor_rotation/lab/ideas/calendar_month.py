import numpy as np
import pandas as pd

from ..registry import Idea

IDEA = Idea(
    id="turn_of_month", family="calendar",
    title="Hold the index only around the turn of the month",
    why=("Pay cheques, pension contributions and month-end rebalancing put money into shares in a few days around the "
         "month's end. Since Ariel (1987) and Lakonishok and Smidt (1988) most of the market's return has come in the last "
         "day of the month and the first three of the next. The trade is in the market four days in twenty-one, so the "
         "test is whether those days still earn more than the rest."),
    source="Ariel 1987; Lakonishok and Smidt 1988; McConnell and Xu 2008",
    universe=dict(funds=["SPY", "IWM", "QQQ"]),
    base=dict(side="long", select="sign", hold=1, entry="close", max_w=1.0, fund="SPY", before=1, after=3),
    variants={"spy_-1_+3": {}, "iwm_-1_+3": dict(fund="IWM"), "qqq_-1_+3": dict(fund="QQQ"),
              "spy_-3_+3": dict(before=3), "spy_-1_+1": dict(after=1), "spy_rest_of_month": dict(rest=True)},
)


def score(p, u, fund="SPY", before=1, after=3, rest=False):
    days = pd.Series(1, index=p.dates)
    month = p.dates.to_period("M")
    from_start = days.groupby(month).cumsum()
    from_end = days[::-1].groupby(month[::-1]).cumsum()[::-1]
    inside = (from_end <= before) | (from_start <= after)
    if rest:
        inside = ~inside
    s = pd.DataFrame(np.nan, index=p.dates, columns=u.columns)
    s[fund] = np.where(inside.shift(-1, fill_value=False), 1.0, np.nan)   # in at today's close when tomorrow is inside
    return s

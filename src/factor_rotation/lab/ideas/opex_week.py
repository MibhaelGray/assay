import numpy as np
import pandas as pd

from ..registry import Idea

IDEA = Idea(
    id="opex_week", family="calendar",
    title="Hold the index in the week of the monthly option expiry",
    why=("Stivers and Sun (2013) found large stocks earn most of their return in the week of the third Friday, when "
         "dealers unwind the hedges behind expiring options. The following week has tended to be weak. Both are known "
         "dates. Tested on the index fund only; no option data is used."),
    source="Stivers and Sun 2013, Returns and option activity over the option-expiration week",
    universe=dict(funds=["SPY", "QQQ", "IWM"]),
    base=dict(side="long", select="sign", hold=1, entry="close", max_w=1.0, fund="SPY", week="expiry"),
    variants={"spy_expiry_week": {}, "qqq_expiry_week": dict(fund="QQQ"), "iwm_expiry_week": dict(fund="IWM"),
              "spy_week_after_short": dict(week="after", side="short"), "spy_week_before": dict(week="before")},
)


def score(p, u, fund="SPY", week="expiry"):
    d = pd.Series(p.dates, index=p.dates)
    friday = d + pd.to_timedelta(4 - d.dt.dayofweek, unit="D")          # the Friday of each session's week
    third = (friday.dt.day >= 15) & (friday.dt.day <= 21)
    if week == "after":
        prior = friday - pd.Timedelta(days=7)
        third = (prior.dt.day >= 15) & (prior.dt.day <= 21)
    elif week == "before":
        nxt = friday + pd.Timedelta(days=7)
        third = (nxt.dt.day >= 15) & (nxt.dt.day <= 21)
    s = pd.DataFrame(np.nan, index=p.dates, columns=u.columns)
    s[fund] = np.where(third.shift(-1, fill_value=False), 1.0 if week != "after" else -1.0, np.nan)
    return s

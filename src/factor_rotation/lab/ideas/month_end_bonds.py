import numpy as np
import pandas as pd

from ..registry import Idea

IDEA = Idea(
    id="month_end_bonds", family="calendar",
    title="Own long Treasuries over the last sessions of the month",
    why=("Bond indexes are rebuilt at the last close of each month: the month's new issues come in, bonds that have "
         "fallen under a year to run drop out, and the index's duration steps up. Funds that track the index must buy "
         "long-dated Treasuries at that close whatever the price, and dealers and others who know it buy ahead of them. "
         "Hartley and Schwarz (2019) find Treasury returns bunched in the last few sessions of the month for this "
         "reason. The date is fixed and the buyer is forced, which is the kind of flow that can last. Test the long "
         "bond fund (TLT) over the last 1, 3 and 5 sessions, the 7-10 year fund (IEF) over the last 3, and the reverse "
         "(short TLT) in the first 3 sessions of the new month when the buying is done. `tlt_other_days` is the control: "
         "bonds rose through most of 2010-2020, so the month-end days have to beat simply owning TLT the rest of the "
         "time, per day held, or this is just being long bonds."),
    source="Hartley and Schwarz 2019, Predictable end-of-month Treasury returns",
    universe=dict(funds=["TLT", "IEF"]),
    base=dict(side="long", select="sign", hold=1, entry="close", max_w=1.0, fund="TLT", last=3, when="end"),
    variants={"tlt_last3": {}, "tlt_last1": dict(last=1), "tlt_last5": dict(last=5), "ief_last3": dict(fund="IEF"),
              "tlt_first3_short": dict(when="start", side="short"), "tlt_other_days": dict(when="other")},
)


def score(p, u, fund="TLT", last=3, when="end"):
    days = pd.Series(1, index=p.dates)
    month = p.dates.to_period("M")
    from_end = days[::-1].groupby(month[::-1]).cumsum()[::-1]     # 1 on the month's last session
    from_start = days.groupby(month).cumsum()                     # 1 on its first
    nxt_end, nxt_start = from_end.shift(-1), from_start.shift(-1)  # tomorrow's place in its month: a calendar fact
    if when == "end":          # tomorrow is one of the last `last` sessions: own it from tonight's close
        on, sign = nxt_end <= last, 1.0
    elif when == "start":      # tomorrow is one of the first `last` sessions of the month: short
        on, sign = nxt_start <= last, -1.0
    else:                      # every session that is not one of the last `last`
        on, sign = nxt_end > last, 1.0
    s = pd.DataFrame(np.nan, index=p.dates, columns=u.columns)
    s[fund] = np.where(on, sign, np.nan)
    return s

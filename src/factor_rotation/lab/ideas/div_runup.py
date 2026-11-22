import numpy as np
import pandas as pd

from .. import feat
from ..registry import Idea

IDEA = Idea(
    id="div_runup", family="calendar",
    title="Own regular dividend payers into the ex-dividend date; short them after it",
    why=("Hartzmark and Solomon (2013) find dividend payers earn abnormal returns in the days before the ex-dividend "
         "date and on it, and give some back in the weeks after: buyers who want the payment (income funds, retail, "
         "holders reinvesting) arrive before the date and nobody is paid to stand in their way for so small a move. "
         "The ex-day itself has long shown a price drop smaller than the dividend (Elton and Gruber 1970), because the "
         "marginal holder is taxed on it. The date and amount are declared weeks ahead, so this is a calendar fact, "
         "like a report date. The losing side is the income buyer who pays up before the date and the holder who "
         "sells after it. The test: is the run-up there in 2010-2021 in liquid names, is it in the days before "
         "(`pre3`), on the day (`exday`) or both (`thru3`, `thru5`); does it grow with the size of the payment "
         "(`thru3_high_yield`), as demand for the dividend should; and is there a fall afterwards (`post3_short`, "
         "`pre_post_ls`). Ex-dates and amounts are read from the price history itself: the gap between the "
         "total-return close and the split-only close steps on each ex-date by the dividend over the prior close. Only "
         "payers with a payment about a quarter earlier are used, so the date was predictable and one-off specials "
         "are left out."),
    source="Hartzmark and Solomon 2013 (The dividend month premium); Elton and Gruber 1970",
    universe=dict(min_price=5, min_adv=20e6),
    base=dict(side="long", select="sign", hedge=True, hold=3, entry="close", max_w=0.05, lead=3, min_yield=0.0),
    variants={"thru3": {},
              "pre3": dict(lead=4),
              "exday": dict(lead=1, hold=1),
              "thru5": dict(lead=5, hold=5),
              "thru3_high_yield": dict(min_yield=0.0075),
              "thru3_liquid": dict(universe=dict(min_adv=50e6)),
              "post3_short": dict(lead=0, side="short"),
              "pre_post_ls": dict(side="ls", hedge=False, post=True)},
)


def ex_dates(p) -> pd.DataFrame:
    """The dividend as a share of the prior close, on each regular payer's ex-date; gaps elsewhere."""
    if "ex_div" not in p._cache:
        f = p.close / p.price                       # total-return factor; it steps up on an ex-date
        y = 1 - f.shift(1) / f
        y = y.where((y > 0.0005) & (y < 0.03))      # under 5 bp is rounding; over 3% is a special or a spin-off
        paid = y.notna().astype(float)
        regular = paid.shift(55).rolling(16, min_periods=1).max() > 0   # a payment 55 to 70 sessions earlier
        p._cache["ex_div"] = y.where(regular)
    return p._cache["ex_div"]


def score(p, u, lead=3, min_yield=0.0, post=False):
    y = ex_dates(p)
    ahead = y.shift(-lead) if lead else y           # in at the close `lead` sessions before the ex-date (0 = its close)
    sign = 1.0 if lead else -1.0                    # after the date the trade is a short
    s = pd.DataFrame(np.nan, index=p.dates, columns=p.close.columns).mask(ahead > max(min_yield, 0.0), sign)
    if post:                                        # long into the date, short for the same number of sessions after it
        s = s.mask(y.notna(), -1.0)
    return feat.within(s, u)

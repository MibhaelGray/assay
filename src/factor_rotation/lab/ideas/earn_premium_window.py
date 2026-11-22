import numpy as np
import pandas as pd

from .. import events, feat
from ..registry import Idea

IDEA = Idea(
    id="earn_premium_window", family="earnings",
    title="Where in the window the earnings-report premium is earned: before the report or over it",
    why=("Follow-up to `earn_premium_sharp:early_entry`, the one change that helped (0.76 to 0.86 after costs, 24 to 29 "
         "bp a round trip before costs, by entering one session earlier). Two stories give the same average and "
         "different trades. If the premium is attention building (buyers arriving ahead of a known date), it should "
         "keep growing as the entry moves earlier, and a good part should be earned before the report, where there is "
         "no earnings gap to carry and the risk is a fraction of the report day's. If it is pay for carrying the "
         "report's uncertainty, the days before should be empty and the premium should sit on the report sessions "
         "alone. `pre_1d` and `pre_2d` own only the sessions before the report date and are out at the close before "
         "it. `from_3d` and `from_4d` enter three and four sessions ahead and hold through the report as before. Same "
         "high-attention names, same universe, nothing else changed. Four variants, fixed before running; the first "
         "session's plain run-up test (all names, 3 and 5 sessions, 0.2-0.3 before costs) says not to expect much "
         "from the days before."),
    source="Frazzini and Lamont 2007; Barber, De George, Lehavy, Trueman 2013; lab session 2026-11-22",
    universe=dict(min_price=5, min_adv=10e6),
    base=dict(side="long", select="sign", hedge=True, hold=1, entry="close", max_w=0.05, lead=2),
    variants={"pre_1d": {}, "pre_2d": dict(lead=3, hold=2),
              "from_3d": dict(lead=3, hold=4), "from_4d": dict(lead=4, hold=5)},
)


def score(p, u, lead=2):
    flag = events.reports(p).notna()
    on = flag.shift(-lead, fill_value=False)        # in at the close `lead` sessions before the report date
    s = pd.DataFrame(np.nan, index=p.dates, columns=p.close.columns).mask(on, 1.0)
    # names whose past reports drew the most extra volume, measured two sessions back (the same sort as the lead)
    jump = feat.rel_volume(p).where(events.window(p)).rolling(260, min_periods=1).mean()
    s = s.where(feat.rank(feat.within(jump.shift(2), u)) >= 0.67)
    return feat.within(s, u)

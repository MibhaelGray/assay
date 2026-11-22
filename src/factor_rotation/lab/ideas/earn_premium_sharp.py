import numpy as np
import pandas as pd

from .. import events, feat
from ..registry import Idea

IDEA = Idea(
    id="earn_premium_sharp", family="earnings",
    title="The earnings-report premium in high-attention names, three sharpenings",
    why=("`earn_premium:over_2d_high_volume_names` earned 24 bp a round trip before costs and gave 9 of them to costs "
         "(0.76 discovery, 0.82 validation). Three changes, each with a reason, none a search. (a) Liquidity: the same "
         "trade costs a third as much in names trading $50M a day or more; the question is whether the premium is still "
         "there, because the story (buyers drawn by attention who cannot easily be met by short sellers) should be "
         "weaker where shorting is easy. `mid_only` is the other half, to show where the premium lives. (b) Timing: "
         "attention and option buying build over the days before a report (Frazzini and Lamont measure the premium "
         "over the whole month), so entering one session earlier should add return for the same cost. (c) Run-up: a "
         "stock already up more than 10% in the week before its report has had its attention buying, and Aboody, "
         "Lehavy and Trueman (2010) find the strongest pre-report runners give it back over the report."),
    source="Frazzini and Lamont 2007; Aboody, Lehavy, Trueman 2010; lab session 2026-11-22",
    universe=dict(min_price=5, min_adv=10e6),
    base=dict(side="long", select="sign", hedge=True, hold=2, entry="close", max_w=0.05, lead=1, runup_cap=None),
    variants={"liquid": dict(universe=dict(min_adv=50e6)),
              "very_liquid": dict(universe=dict(min_adv=200e6)),
              "mid_only": dict(universe=dict(max_adv=50e6)),
              "early_entry": dict(lead=2, hold=3),
              "no_runup": dict(runup_cap=0.10),
              "liquid_no_runup": dict(runup_cap=0.10, universe=dict(min_adv=50e6))},
)


def score(p, u, lead=1, runup_cap=None):
    flag = events.reports(p).notna()
    on = flag.shift(-lead, fill_value=False)        # in at the close `lead` sessions before the report date
    s = pd.DataFrame(np.nan, index=p.dates, columns=p.close.columns).mask(on, 1.0)
    # names whose past reports drew the most extra volume, measured two sessions back (the same sort as the lead)
    jump = feat.rel_volume(p).where(events.window(p)).rolling(260, min_periods=1).mean()
    s = s.where(feat.rank(feat.within(jump.shift(2), u)) >= 0.67)
    if runup_cap is not None:                       # skip names already up this much over the last five sessions
        s = s.where(feat.ret(p, 5) <= runup_cap)
    return feat.within(s, u)

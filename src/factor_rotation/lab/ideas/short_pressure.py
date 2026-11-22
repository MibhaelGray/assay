from .. import feat
from ..registry import Idea
from ..sources import short_volume

IDEA = Idea(
    id="short_pressure", family="short-flow",
    title="Rank stocks by how much of the day's off-exchange volume was sold short",
    why=("Two stories pull opposite ways, and the test is which one the data backs. (1) Short sellers are informed: days "
         "of heavy shorting come before weak returns over the next week (Boehmer, Jones, Zhang 2008; Diether, Lee, Werner "
         "2009). (2) In FINRA's off-exchange data most 'short' volume is a market maker selling short to fill a buyer, so "
         "a high share is really heavy buying, often retail, and the stock tends to follow it up (the reasoning behind "
         "SqueezeMetrics' dark-pool index). Each stock is measured against its own last 60 sessions, so names that are "
         "always heavily shorted don't dominate."),
    source="Boehmer, Jones, Zhang 2008; Diether, Lee, Werner 2009; SqueezeMetrics 2018 (DIX white paper)",
    universe=dict(min_price=5, min_adv=20e6, top=1000),
    base=dict(side="ls", frac=0.1, hold=3, mode="fade", days=1),
    variants={"fade_h3": {}, "fade_h1": dict(hold=1), "fade_h5": dict(hold=5), "fade_5d_h5": dict(days=5, hold=5),
              "follow_h3": dict(mode="follow"), "follow_h1": dict(mode="follow", hold=1), "follow_h5": dict(mode="follow", hold=5),
              "follow_5d_h5": dict(mode="follow", days=5, hold=5),
              "fade_into_rally_h3": dict(mode="fade_rally"), "follow_on_dip_h3": dict(mode="follow_dip"),
              "follow_h3_mid": dict(mode="follow", universe=dict(min_adv=5e6, max_adv=20e6, top=None)),
              "fade_h3_mid": dict(universe=dict(min_adv=5e6, max_adv=20e6, top=None))},
)


def score(p, u, mode="fade", days=1):
    ratio = short_volume.ratio(p).rolling(days, min_periods=days).mean()
    z = feat.within(feat.ts_z(ratio, 60), u)
    move = feat.within(feat.residual(p).rolling(days, min_periods=days).sum(), u)
    if mode == "fade":             # heavy shorting -> sell
        return -z
    if mode == "follow":           # heavy "shorting" is a market maker filling buyers -> buy
        return z
    if mode == "fade_rally":       # shorts leaning into a stock that rose: the informed-seller case at its cleanest
        return (-z).where(move > 0)
    return z.where(move < 0)       # buying absorbed on a down day

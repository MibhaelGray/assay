from .. import feat
from ..registry import Idea
from ..sources import short_volume

IDEA = Idea(
    id="short_slow", family="short-flow",
    title="Short-sale pressure as a slow signal: weeks of heavy shorting, held five sessions",
    why=("`short_pressure` found that a day of unusually heavy shorting comes before weak returns (0.7-1.05 before "
         "costs) but the daily ranking replaced the whole book every day and costs took all of it. If short sellers are "
         "informed about something that takes weeks to come out (Boehmer, Jones and Zhang find the effect lasts 20 "
         "sessions and more), a 10- or 20-day average of the short share against the stock's own earlier 120 sessions "
         "should carry the same information and change slowly, so a book held five sessions and rolled open to open "
         "keeps most of its names and pays a fraction of the cost. The warning sign is already in the ledger: the "
         "5-day average had no before-cost return. If the slow versions are empty too, the information is in the "
         "single day and cannot be traded at these costs. `level20` drops the own-norm (Boehmer's sort is on the level). "
         "`short_only` tests the side the story is about. `spike_short` is the other way to cut trading: only the most "
         "extreme single days, where an informed seller should show most clearly, each paying its own round trip. "
         "The data starts in August 2018, so discovery is three years and a t of 3 is out of reach; this run can kill "
         "the direction or keep it alive, not pass it."),
    source="Boehmer, Jones, Zhang 2008; Diether, Lee, Werner 2009; lab session 2026-11-22 (short_pressure)",
    universe=dict(min_price=5, min_adv=20e6, top=1000),
    base=dict(side="ls", frac=0.1, hold=5, entry="open", exit="open", mode="own_norm", days=20),
    variants={"avg20": {}, "avg10": dict(days=10),
              "avg20_top300": dict(universe=dict(top=300)),
              "avg20_short_only": dict(side="short", hedge=True),
              "level20": dict(mode="level"),
              "spike_short": dict(mode="spike", select="sign", side="short", hedge=True)},
)


def score(p, u, mode="own_norm", days=20, spike=3.0):
    r = short_volume.ratio(p)
    if mode == "spike":            # a single day three deviations over the stock's own last 60 sessions: short it
        z = feat.within(feat.ts_z(r, 60), u)
        return (-z).where(z >= spike)
    avg = r.rolling(days, min_periods=int(days * 0.8)).mean()
    if mode == "level":            # the plain level, across stocks
        return -feat.within(avg, u)
    past = r.shift(days)           # the 120 sessions before the window being averaged
    z = (avg - past.rolling(120, min_periods=60).mean()) / past.rolling(120, min_periods=60).std()
    return -feat.within(z, u)      # heavy shorting -> sell

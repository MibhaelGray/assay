from .. import feat
from ..registry import Idea

IDEA = Idea(
    id="overnight_persist", family="overnight",
    title="Stocks that have been rising overnight keep rising overnight",
    why=("Some stocks are bought mostly by people who trade at the open (retail, overseas) and sold through the day by "
         "institutions, and that split persists for months: Lou, Polk and Skouras (2019) found a stock's overnight return "
         "over the past month predicts its overnight return next month, and the reverse for its intraday return. The trade "
         "is held close to open only, so it turns the whole book over every day; cost is the question."),
    source="Lou, Polk, Skouras 2019, A Tug of War",
    universe=dict(min_price=5, min_adv=20e6, top=1000),
    base=dict(side="ls", frac=0.1, hold=1, entry="close", exit="open", lookback=20),
    variants={"lb20": {}, "lb60": dict(lookback=60), "lb20_top300": dict(universe=dict(top=300)),
              "lb60_long_hedged": dict(lookback=60, side="long", hedge=True),
              "intraday_fade_lb20": dict(mode="intraday", entry="open", exit="close")},
)


def score(p, u, lookback=20, mode="overnight"):
    if mode == "overnight":
        return feat.within(p.co.rolling(lookback, min_periods=lookback).mean(), u)
    # the daytime half of the same finding: names that rise overnight give it back from the open to the close
    return feat.within(-p.co.rolling(lookback, min_periods=lookback).mean(), u)

import numpy as np

from .. import events, feat
from ..registry import Idea

IDEA = Idea(
    id="rev_no_news", family="reversal",
    title="Fade the week's moves only where there was no earnings report behind them",
    why=("The reversal trade should work on moves made by pressure and fail on moves made by news, because news does not "
         "come back. Taking out stocks that reported in the window removes the largest known news days and should leave a "
         "cleaner trade (Da, Liu, Schaumburg 2014; Hameed and Mian 2015). The mirror variant fades only the stocks that "
         "did report, which should lose if the reasoning is right."),
    source="Da, Liu, Schaumburg 2014; Hameed and Mian 2015",
    universe=dict(min_price=5, min_adv=20e6, top=1000),
    base=dict(side="ls", frac=0.1, hold=5, lookback=5, keep="no_report"),
    variants={"no_report_h5": {}, "no_report_h3": dict(hold=3), "reported_h5": dict(keep="report"),
              "no_report_h5_exit_open": dict(exit="open"), "no_report_h5_mid": dict(universe=dict(min_adv=5e6, max_adv=20e6, top=None)),
              "no_report_lb10_h5": dict(lookback=10)},
)


def score(p, u, lookback=5, keep="no_report"):
    move = feat.residual(p).rolling(lookback, min_periods=lookback).sum()
    typical = feat.vol(p, 60).shift(lookback) * np.sqrt(lookback)
    reported = events.window(p, before=0, after=lookback)     # a report date inside the lookback window
    z = feat.within(-(move / typical), u)
    z = z.where(~reported.reindex(columns=z.columns) if keep == "no_report" else reported.reindex(columns=z.columns))
    return feat.sector_demean(z, p.sector())

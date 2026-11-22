import pandas as pd

from .. import feat
from ..registry import Idea

IDEA = Idea(
    id="weekday_season", family="calendar",
    title="Stocks that do well on a given weekday keep doing well on that weekday",
    why=("Keloharju, Linnainmaa and Nyberg (2016) found that a stock's average return on, say, Mondays over past years "
         "predicts its return next Monday against other stocks, and likewise for every weekday. They tie it to recurring "
         "flows (weekly option expiries, payroll and fund flows that arrive on fixed days). It needs no news and resets "
         "every day, so the whole book turns over daily and cost decides it."),
    source="Keloharju, Linnainmaa, Nyberg 2016, Return seasonalities; Heston, Korajczyk, Sadka 2010 for the intraday form",
    universe=dict(min_price=5, min_adv=20e6, top=1000),
    base=dict(side="ls", frac=0.1, hold=1, entry="close", weeks=52),
    variants={"w52": {}, "w104": dict(weeks=104), "w52_top300": dict(universe=dict(top=300)),
              "w52_open_entry": dict(entry="open"), "w26": dict(weeks=26), "w52_fifth": dict(frac=0.2)},
)


def score(p, u, weeks=52):
    r = feat.residual(p)
    out = pd.DataFrame(float("nan"), index=r.index, columns=r.columns)
    weekday = pd.Series(r.index.dayofweek, index=r.index)
    for day in range(5):
        rows = weekday.index[weekday == day]
        out.loc[rows] = r.loc[rows].rolling(weeks, min_periods=weeks * 3 // 4).mean().to_numpy()
    # out[t] = the average on t's weekday up to and including t; the next session is a different weekday, so tomorrow's
    # score is the latest value seen for tomorrow's weekday
    nxt = weekday.shift(-1)
    score = pd.DataFrame(float("nan"), index=r.index, columns=r.columns)
    for day in range(5):
        seen = out.where(weekday == day, axis=0).ffill()
        rows = nxt.index[nxt == day]
        score.loc[rows] = seen.loc[rows]
    return feat.within(score, u)

import numpy as np
import pandas as pd

from ..registry import Idea

IDEA = Idea(
    id="overnight_index", family="overnight",
    title="Own the index only from the close to the next open",
    why=("For decades nearly all of the US market's gain came overnight and almost none between the open and the close "
         "(Cooper, Cliff, Gulen 2008; Lou, Polk, Skouras 2019). If that still holds, owning the index fund only overnight "
         "earns the market's return with far less time at risk. It trades twice a day, every day, so even a one basis "
         "point cost each way is five percent a year."),
    source="Cooper, Cliff, Gulen 2008",
    universe=dict(funds=["SPY", "QQQ", "IWM"]),
    base=dict(side="long", select="sign", hold=1, entry="close", exit="open", max_w=1.0, fund="SPY"),
    variants={"spy_night": {}, "qqq_night": dict(fund="QQQ"), "iwm_night": dict(fund="IWM"),
              "spy_day": dict(entry="open", exit="close"), "iwm_day": dict(fund="IWM", entry="open", exit="close")},
)


def score(p, u, fund="SPY"):
    s = pd.DataFrame(np.nan, index=p.dates, columns=u.columns)
    s[fund] = 1.0
    return s

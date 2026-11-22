import numpy as np
import pandas as pd

from .. import feat
from ..registry import Idea

IDEA = Idea(
    id="vix_fear_buy", family="volatility",
    title="Buy the index for a few days when fear spikes",
    why=("When near-dated fear (VIX) rises above three-month fear (VIX3M), or jumps well above its own recent level, "
         "investors are paying up for protection and dealers are short of risk capacity; the days after have paid more "
         "than usual for holding shares. The risk is that the few times it fails are crashes."),
    source="Whaley 2009; Nagel 2012; common practitioner rule",
    universe=dict(funds=["SPY", "IWM"]),
    base=dict(side="long", select="sign", hold=3, entry="close", max_w=1.0, fund="SPY", rule="backwardated"),
    variants={"backwardated_h3": {}, "backwardated_h5": dict(hold=5), "backwardated_h1": dict(hold=1),
              "spike_h3": dict(rule="spike"), "spike_h5": dict(rule="spike", hold=5),
              "calm_h3": dict(rule="calm"), "backwardated_iwm_h3": dict(fund="IWM")},
)


def score(p, u, fund="SPY", rule="backwardated"):
    vix = feat.gauge(p, "VIX")
    if rule == "backwardated":
        on = vix > feat.gauge(p, "VIX3M")
    elif rule == "spike":
        on = vix > 1.25 * vix.shift(1).rolling(10).mean()
    else:
        on = vix < 0.9 * feat.gauge(p, "VIX3M")
    s = pd.DataFrame(np.nan, index=p.dates, columns=u.columns)
    s[fund] = np.where(on, 1.0, np.nan)
    return s

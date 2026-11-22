import pandas as pd

from .. import feat
from ..registry import Idea

IDEA = Idea(
    id="sector_lead", family="lead-lag",
    title="When a sector's big names move, its smaller names catch up the next day",
    why=("News about an industry reaches its most-followed stocks first; the smaller ones in the same sector take it in "
         "over the following day or two (Hou 2007; Lo and MacKinlay 1990). So rank the less-traded names by how far their "
         "sector's leaders moved today, or by how far they lag those leaders, and hold a day or two."),
    source="Hou 2007, Industry information diffusion; Lo and MacKinlay 1990",
    universe=dict(min_price=5, min_adv=5e6),
    base=dict(side="ls", frac=0.1, hold=1, mode="follow", days=1),
    variants={"follow_h1": {}, "follow_h2": dict(hold=2), "gap_h1": dict(mode="gap"), "gap_h2": dict(mode="gap", hold=2),
              "follow_3d_h3": dict(days=3, hold=3), "gap_3d_h3": dict(mode="gap", days=3, hold=3),
              "follow_h1_at_close": dict(entry="close")},
)


def score(p, u, mode="follow", days=1):
    r = feat.within(feat.residual(p).rolling(days, min_periods=days).sum(), u)
    adv = p.adv.reindex(columns=r.columns).where(u)
    sectors = p.sector().reindex(r.columns)
    out = pd.DataFrame(float("nan"), index=r.index, columns=r.columns)
    for name, cols in sectors.groupby(sectors).groups.items():
        cols = list(cols)
        if not name or len(cols) < 30:
            continue
        size = adv[cols].rank(axis=1, pct=True)
        leaders = r[cols].where(size >= 0.7).mean(axis=1)
        followers = size < 0.7
        s = pd.DataFrame({c: leaders for c in cols}) if mode == "follow" else r[cols].rsub(leaders, axis=0)
        out[cols] = s.where(followers)
    return out

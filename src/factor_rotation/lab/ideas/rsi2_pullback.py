from .. import feat
from ..registry import Idea

IDEA = Idea(
    id="rsi2_pullback", family="reversal",
    title="Buy a sharp two-day dip in a stock that is in an uptrend, hedged with the market",
    why=("In a rising stock a sharp short dip is more often profit-taking than a change of story, and buyers who missed the "
         "move step in. Connors' RSI(2) rule is the popular form. It differs from the plain reversal trade in being long "
         "only, conditioned on trend, and judged against the stock's own history rather than against other stocks."),
    source="Connors and Alvarez 2009; widely traded, so likely crowded",
    universe=dict(min_price=5, min_adv=20e6, top=1000),
    base=dict(side="long", select="rank", n=20, hedge=True, hold=3, level=10, min_names=1),
    variants={"n20_h3": {}, "n20_h1": dict(hold=1), "n20_h5": dict(hold=5), "n20_h3_under5": dict(level=5),
              "n20_h3_at_close": dict(entry="close"), "n20_h3_no_trend": dict(trend=False),
              "n20_h3_top300": dict(universe=dict(top=300))},
)


def score(p, u, level=10, trend=True):
    r = feat.rsi(p, 2)
    ok = r < level
    if trend:
        ok &= p.close > p.close.rolling(200, min_periods=200).mean()
    return feat.within((100 - r).where(ok), u)

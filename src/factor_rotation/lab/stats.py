"""What a daily return series is worth, and how much to discount it for the number of things tried.

`summary` is the report card for one period. `deflated` answers the question a long search has to ask: given that N
variants have been run, how likely is it that this one's Sharpe is real rather than the luckiest of N? It is the
deflated Sharpe ratio of Bailey and Lopez de Prado (2014): the chance the true Sharpe is above the best Sharpe luck
alone would be expected to produce over that many tries.
"""

import math

import numpy as np
import pandas as pd
from scipy.stats import norm

EULER = 0.5772156649
YEAR = 252


def newey_west_t(r: np.ndarray, lags: int) -> float:
    """t of the mean, with a standard error that allows for returns that overlap `lags` days."""
    n = len(r)
    if n < 30 or r.std() == 0:
        return float("nan")
    x = r - r.mean()
    var = (x * x).sum() / n
    for k in range(1, min(lags, n - 1) + 1):
        var += 2 * (1 - k / (lags + 1)) * (x[k:] * x[:-k]).sum() / n
    return float(r.mean() / math.sqrt(max(var, 1e-18) / n))


def summary(result, start: str | None = None, end: str | None = None) -> dict:
    """One period's report card for an engine.Result."""
    net, gross = result.net.loc[start:end], result.gross.loc[start:end]
    turnover, names, bench = result.turnover.loc[start:end], result.names.loc[start:end], result.bench.loc[start:end]
    live = turnover.ne(0).cummax() & turnover[::-1].ne(0).cummax()[::-1]   # from the first trade to the last
    net, gross, turnover, names, bench = net[live], gross[live], turnover[live], names[live], bench[live]
    n = len(net)
    if n < 120 or net.std() == 0:
        return {"days": n}
    years = n / YEAR
    sharpe = net.mean() / net.std() * math.sqrt(YEAR)
    cost = gross - net
    twice = gross - 2 * cost
    equity = (1 + net).cumprod()
    beta = float(np.cov(net, bench)[0, 1] / bench.var()) if bench.var() else 0.0
    alpha = net - beta * bench
    by_year = {}
    for year, r in net.groupby(net.index.year):
        if len(r) >= 60:
            by_year[int(year)] = {"ret": round(float(r.sum()), 4), "sharpe": round(float(r.mean() / r.std() * math.sqrt(YEAR)), 2) if r.std() else 0.0}
    round_trips = turnover.sum() / 2
    return {
        "from": str(net.index[0].date()), "to": str(net.index[-1].date()), "days": n,
        "ret": round(float(net.mean() * YEAR), 4), "vol": round(float(net.std() * math.sqrt(YEAR)), 4),
        "sharpe": round(float(sharpe), 2),
        "t": round(newey_west_t(net.to_numpy(), result.spec["hold"]), 2),
        "sharpe_gross": round(float(gross.mean() / gross.std() * math.sqrt(YEAR)), 2) if gross.std() else 0.0,
        "sharpe_2x_cost": round(float(twice.mean() / twice.std() * math.sqrt(YEAR)), 2) if twice.std() else 0.0,
        "drawdown": round(float((equity / equity.cummax() - 1).min()), 4),
        "good_years": round(sum(v["ret"] > 0 for v in by_year.values()) / max(len(by_year), 1), 2),
        "by_year": by_year,
        "beta": round(beta, 3),
        "sharpe_alpha": round(float(alpha.mean() / alpha.std() * math.sqrt(YEAR)), 2) if alpha.std() else 0.0,
        "bench_ret": round(float(bench.mean() * YEAR), 4),
        "turnover": round(float(turnover.sum() / years), 1),                # dollars traded a year per dollar of book
        "cost": round(float(cost.mean() * YEAR), 4),                       # yearly drag
        "bp_per_trade": round(float(net.sum() / round_trips * 1e4), 1) if round_trips else 0.0,   # net, per round trip
        "bp_per_trade_gross": round(float(gross.sum() / round_trips * 1e4), 1) if round_trips else 0.0,
        "names": round(float(names[names > 0].mean()), 1) if (names > 0).any() else 0.0,
        "active": round(float((names > 0).mean()), 2),                      # share of days a new position went on
        "daily_sharpe": float(net.mean() / net.std()), "skew": round(float(net.skew()), 2), "kurt": round(float(net.kurt() + 3), 2),
    }


def expected_best(n_trials: int, days: int) -> float:
    """The best daily Sharpe that luck alone would be expected to produce over `n_trials` tries of an idea with no
    edge. A Sharpe measured over `days` sessions on pure noise scatters around zero with variance 1/days; the best of
    N such draws is what this returns. Variants of one idea are alike, so N overstates the independent tries and the
    haircut errs on the strict side."""
    if n_trials < 2:
        return 0.0
    return math.sqrt(1 / days) * ((1 - EULER) * norm.ppf(1 - 1 / n_trials) + EULER * norm.ppf(1 - 1 / (n_trials * math.e)))


def deflated(daily_sharpe: float, days: int, skew: float, kurt: float, n_trials: int) -> float:
    """The chance this Sharpe is real, after allowing for every variant in the ledger. 0.95 is the bar."""
    if days < 120:
        return float("nan")
    spread = math.sqrt(max(1e-12, 1 - skew * daily_sharpe + (kurt - 1) / 4 * daily_sharpe ** 2))
    return float(norm.cdf((daily_sharpe - expected_best(n_trials, days)) * math.sqrt(days - 1) / spread))

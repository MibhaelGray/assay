"""Building blocks that idea files share. Everything is a table with sessions down and symbols across, and every
value on a row uses data up to that row's close and nothing later."""

import numpy as np
import pandas as pd

from .panel import Panel


def ret(p: Panel, n: int) -> pd.DataFrame:
    """Return over the last n sessions."""
    return p.close / p.close.shift(n) - 1


def vol(p: Panel, n: int = 20) -> pd.DataFrame:
    """Standard deviation of daily returns over the last n sessions."""
    return p.cc.rolling(n, min_periods=int(n * 0.8)).std()


def atr(p: Panel, n: int = 14) -> pd.DataFrame:
    """Average true range over n sessions, as a share of the close."""
    prev = p.close.shift(1)
    tr = np.maximum(p.high - p.low, np.maximum((p.high - prev).abs(), (p.low - prev).abs()))
    return tr.rolling(n, min_periods=n).mean() / p.close


def beta(p: Panel, n: int = 120) -> pd.DataFrame:
    """Each stock's beta to the benchmark over the last n sessions."""
    key = ("beta", n)
    if key not in p._cache:
        b = p.cc[p.bench]
        cov = p.cc.rolling(n, min_periods=n // 2).cov(b)
        p._cache[key] = cov.div(b.rolling(n, min_periods=n // 2).var(), axis=0).clip(-1, 4)
    return p._cache[key]


def residual(p: Panel, n: int = 120) -> pd.DataFrame:
    """Daily return with the market's part taken out: return less beta x the benchmark's return. The beta is
    yesterday's, so today's return isn't used to explain itself."""
    key = ("resid", n)
    if key not in p._cache:
        p._cache[key] = p.cc - beta(p, n).shift(1).mul(p.cc[p.bench], axis=0)
    return p._cache[key]


def within(df: pd.DataFrame, mask: pd.DataFrame) -> pd.DataFrame:
    """Keep only the names in play (the universe table)."""
    return df.reindex(columns=mask.columns).where(mask)


def demean(df: pd.DataFrame) -> pd.DataFrame:
    """Less the day's average across names."""
    return df.sub(df.mean(axis=1), axis=0)


def sector_demean(df: pd.DataFrame, sectors: pd.Series) -> pd.DataFrame:
    """Less the day's average across the names in the same sector."""
    out = df.copy()
    groups = sectors.reindex(df.columns).fillna("")
    for _, cols in groups.groupby(groups).groups.items():
        cols = list(cols)
        out[cols] = df[cols].sub(df[cols].mean(axis=1), axis=0)
    return out


def zscore(df: pd.DataFrame) -> pd.DataFrame:
    """Across names, each day: distance from the day's average in standard deviations, clipped at 3."""
    return df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0).clip(-3, 3)


def rank(df: pd.DataFrame) -> pd.DataFrame:
    """Across names, each day: 0 for the lowest to 1 for the highest."""
    return df.rank(axis=1, pct=True)


def ts_z(df: pd.DataFrame, n: int = 60) -> pd.DataFrame:
    """Each name against its own last n sessions (not counting today)."""
    past = df.shift(1)
    return (df - past.rolling(n, min_periods=n // 2).mean()) / past.rolling(n, min_periods=n // 2).std()


def rel_volume(p: Panel, n: int = 20) -> pd.DataFrame:
    """Today's volume over the average of the n sessions before."""
    return p.volume / p.volume.shift(1).rolling(n, min_periods=n // 2).mean()


def close_location(p: Panel) -> pd.DataFrame:
    """Where the close sits in the day's range: 0 at the low, 1 at the high."""
    return (p.close - p.low) / (p.high - p.low).replace(0, np.nan)


def rsi(p: Panel, n: int = 2) -> pd.DataFrame:
    d = p.close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, min_periods=n).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1 / n, min_periods=n).mean()
    return 100 - 100 / (1 + up / down.replace(0, np.nan))


def gauge(p: Panel, name: str) -> pd.Series:
    """A market-wide series kept beside the stocks: VIX, VIX3M, TNX, or any fund's close."""
    return p.close[name]

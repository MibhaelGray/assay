"""Daily short-sale volume from FINRA (free, published each evening for that day).

For every stock, the shares sold short and the total shares reported off-exchange that day (FINRA's trade reporting
facilities, roughly 40-50% of all US volume). The share sold short is a daily read on how hard a name is being
pressed. It is known after the close, so a score built on day t's figure trades at the open of t+1 at the earliest.
Not the same thing as short interest (the twice-monthly count of shares still short): this is flow, that is stock.
"""

import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from .. import DATA
from ..panel import Panel

log = logging.getLogger(__name__)
URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{day:%Y%m%d}.txt"
CACHE = DATA / "short_volume"
FIRST = "2018-08-01"     # the oldest day FINRA's file server still answers for is found by pull(); earlier days are skipped


def _day(args) -> bool:
    day, keep = args
    import urllib.error
    import urllib.request
    path = CACHE / f"{day:%Y-%m-%d}.parquet"
    if path.exists():
        return True
    for attempt in range(3):
        try:
            req = urllib.request.Request(URL.format(day=day), headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read().decode("utf-8", errors="ignore")
            break
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return False
            time.sleep(5 * (attempt + 1))
        except Exception:
            time.sleep(5 * (attempt + 1))
    else:
        return False
    d = pd.read_csv(io.StringIO(text), sep="|", usecols=["Symbol", "ShortVolume", "TotalVolume"], dtype={"Symbol": str})
    d = d.dropna(subset=["Symbol"])
    d = d[d.Symbol.isin(keep)]
    d.to_parquet(path, index=False)
    return True


def pull(market: str = "us") -> dict:
    from .. import panel
    p = panel.load(market, holdout=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    keep = set(p.close.columns)
    days = [d for d in p.dates if d >= pd.Timestamp(FIRST)]
    with ThreadPoolExecutor(6) as pool:
        got = list(pool.map(_day, [(d, keep) for d in days]))
    return {"source": "short_volume", "days": len(days), "have": int(sum(got))}


def tables(p: Panel) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(shares sold short, total shares FINRA reported) on each day, laid out like the price tables."""
    if "short_volume" not in p._cache:
        parts = []
        for path in sorted(CACHE.glob("*.parquet")):
            d = pd.read_parquet(path)
            d["date"] = pd.Timestamp(path.stem)
            parts.append(d)
        long = pd.concat(parts).drop_duplicates(["date", "Symbol"])
        short = long.pivot(index="date", columns="Symbol", values="ShortVolume").reindex(index=p.dates, columns=p.close.columns)
        total = long.pivot(index="date", columns="Symbol", values="TotalVolume").reindex(index=p.dates, columns=p.close.columns)
        p._cache["short_volume"] = (short, total)
    return p._cache["short_volume"]


def ratio(p: Panel) -> pd.DataFrame:
    """Share of FINRA-reported volume that was a short sale, each day."""
    short, total = tables(p)
    return short / total.where(total > 0)

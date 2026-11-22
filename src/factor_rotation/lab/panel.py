"""The lab's price history: one wide table per field (sessions down, symbols across) for one market.

Prices come from Yahoo, daily, from 2010. `open/high/low/close` are adjusted for splits and dividends, so a return
between any two of them is a total return. `price` is the close adjusted for splits only, so `price x volume` is the
dollars that really traded that day; the tradable-universe rule and the cost model read those two.

The one thing this history cannot give: companies that no longer trade. Yahoo drops a stock when it is delisted, so
every past year here holds only names that survived to today. That flatters anything that buys losers (the ones that
went to zero are missing) and it is why a good backtest here is a reason to test forward on paper, never proof.

Markets are listed in MARKETS. Each names where its symbols come from, its hedge and the Yahoo suffix; adding one is
one entry plus a symbol list.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from functools import cached_property

import numpy as np
import pandas as pd

from . import DATA, HOLDOUT_START, START

log = logging.getLogger(__name__)

FIELDS = ("open", "high", "low", "close", "price", "volume")
CHUNK = 80
US_MIN_CAP = 300e6
SYMBOL = re.compile(r"^[A-Z]{1,5}([/.][A-Z])?$")
NOT_COMMON = re.compile(r"\bwarrants?\b|\bunits?\b|\brights?\b|preferred|depositary sh.*preferred|\bnotes\b|%|debenture|"
                        r"acquisition corp|\betf\b|\bfund\b", re.I)
# Hedges, sector funds and a few gauges, kept beside the stocks. Never part of the stock universe.
US_FUNDS = ("SPY QQQ IWM MDY DIA RSP XLK XLF XLE XLV XLI XLY XLP XLU XLB XLRE XLC SMH XBI KRE XOP GDX ITB XRT IYT "
            "TLT IEF HYG LQD GLD SLV USO UUP EFA EEM FXI EWJ VXX").split()
# `fx`: the gauge holding units of local currency per US dollar, so "dollars traded" means dollars in every market.
# `fee_bps`: IBKR's commission where it is charged as a share of the trade, added to every one-way cost (in the US
# it is a fraction of a cent a share and is already inside engine.COST_BPS).
MARKETS = {
    "us": {"bench": "SPY", "suffix": "", "funds": US_FUNDS, "index": {"VIX": "^VIX", "VIX3M": "^VIX3M", "TNX": "^TNX"}},
    # Tokyo: the TOPIX members (about 1,670 Prime Market names) from JPX's list of listed issues, written to
    # data/lab/jp/listing.json. Hedge: the TOPIX fund 1306. IBKR charges about 0.05-0.08% of the trade a side.
    "jp": {"bench": "1306", "suffix": ".T", "funds": ["1306", "1321", "2516"], "index": {"USDJPY": "JPY=X", "N225": "^N225"}, "fx": "USDJPY", "fee_bps": 6.0},
    # Added 2026-11-22 (lab.listings writes each listing.json). `fee_bps` is IBKR's commission plus the market's
    # transaction taxes, as an average per side: a tax charged on one side only is split across both.
    # Hong Kong: every Main Board and GEM share in HKEX's list of securities. Stamp duty 0.10% each side (0.13% from
    # August 2021 to November 2023) plus about 0.05% commission and 0.01% of levies.
    "hk": {"bench": "2800", "suffix": ".HK", "funds": ["2800", "2828", "3033"], "index": {"USDHKD": "HKD=X"}, "fx": "USDHKD", "fee_bps": 16.0},
    # Korea: every KOSPI (.KS) and KOSDAQ (.KQ) share in KRX's KIND list. Sell tax about 0.20% (higher before 2021),
    # commission about 0.08%. IBKR allows no shorting there: run long-only or hedged with the KODEX 200 fund (069500).
    "kr": {"bench": "069500", "suffix": ".KS", "funds": ["069500"], "index": {"USDKRW": "KRW=X"}, "fx": "USDKRW", "fee_bps": 18.0},
    # Taiwan: every TWSE (.TW) and TPEx (.TWO) share in TWSE's ISIN lists. Sell tax 0.30%, commission about 0.10%.
    # No shorting at IBKR; hedge with the Taiwan 50 fund (0050).
    "tw": {"bench": "0050", "suffix": ".TW", "funds": ["0050"], "index": {"USDTWD": "TWD=X"}, "fx": "USDTWD", "fee_bps": 25.0},
    # Australia: every name in the ASX's list of listed companies. IBKR about 0.08% a side, no tax.
    "au": {"bench": "STW", "suffix": ".AX", "funds": ["STW", "IOZ"], "index": {"USDAUD": "AUD=X"}, "fx": "USDAUD", "fee_bps": 8.0},
    # Canada: the S&P/TSX Composite members (about 220; Wikipedia's list). Commission is about a cent a share.
    "ca": {"bench": "XIU", "suffix": ".TO", "funds": ["XIU", "XIC"], "index": {"USDCAD": "CAD=X"}, "fx": "USDCAD", "fee_bps": 2.0},
    # London: the FTSE 100 and FTSE 250 members (Wikipedia's lists). Prices are in pence (fx_scale 100). Stamp duty
    # is 0.5% on every purchase of shares (a US resident has no CFDs to avoid it), so 25 bp a side plus commission.
    "uk": {"bench": "ISF", "suffix": ".L", "funds": ["ISF", "MIDD"], "index": {"USDGBP": "GBP=X"}, "fx": "USDGBP", "fx_scale": 100, "fee_bps": 30.0},
}


def _dir(market: str):
    return DATA / market


def us_listing() -> pd.DataFrame:
    """Every US-listed common stock worth US_MIN_CAP or more today, from Nasdaq's screener."""
    from ..smallcap import sources
    rows = [r for r in sources.listing() if r["cap"] >= US_MIN_CAP and SYMBOL.match(r["symbol"])
            and not NOT_COMMON.search(r["name"])]
    return pd.DataFrame(rows).drop_duplicates("symbol").set_index("symbol")


def _yahoo(symbol: str, market: str, own: dict | None = None) -> str:
    """Yahoo's ticker. A listing may give each name its own (`yahoo`), for markets with two boards (.KS and .KQ)."""
    m = MARKETS[market]
    if own and symbol in own:
        return own[symbol]
    return m["index"].get(symbol) or symbol.replace("/", "-").replace(".", "-") + m["suffix"]


def per_dollar(close: pd.DataFrame, market: str) -> pd.Series | None:
    """Units of the prices' currency per US dollar each session (pence in London), or None in the US."""
    m = MARKETS.get(market, {})
    if not m.get("fx"):
        return None
    return close[m["fx"]].ffill() * m.get("fx_scale", 1)


def pull(market: str = "us", symbols: list[str] | None = None, fresh: bool = False) -> dict:
    """Download the history and write the wide tables. Chunks are kept on disk, so a stopped pull picks up where it
    left off; `fresh` throws them away first."""
    import yfinance as yf

    m, root = MARKETS[market], _dir(market)
    chunks = root / "chunks"
    if fresh and chunks.exists():
        for f in chunks.glob("*.parquet"):
            f.unlink()
    chunks.mkdir(parents=True, exist_ok=True)
    own = {}
    if market != "us" and (root / "listing.json").exists():
        listing = pd.read_json(root / "listing.json", dtype={"symbol": str})
        if "yahoo" in listing:
            own = dict(zip(listing.symbol, listing.yahoo))
    if symbols is None:
        meta = us_listing() if market == "us" else pd.read_json(root / "listing.json", dtype={"symbol": str}).set_index("symbol")
        meta.reset_index().to_json(root / "meta.json", orient="records")
        symbols = list(meta.index) + list(m["funds"]) + list(m["index"])
    # Names already downloaded, or asked for and not on Yahoo, are skipped, so a longer list only fetches what is new.
    tried_path = chunks / "tried.json"
    if fresh and tried_path.exists():
        tried_path.unlink()
    have = set(json.loads(tried_path.read_text())) if tried_path.exists() else set()
    for f in chunks.glob("*.parquet"):
        have |= set(pd.read_parquet(f, columns=["symbol"])["symbol"].unique())
    symbols = [s for s in dict.fromkeys(symbols) if s not in have]
    groups = [symbols[i:i + CHUNK] for i in range(0, len(symbols), CHUNK)]
    first = max([int(f.stem) for f in chunks.glob("*.parquet")], default=-1) + 1
    for n, group in enumerate(groups):
        path = chunks / f"{first + n:04d}.parquet"
        tickers = {_yahoo(s, market, own): s for s in group}
        parts = []
        for attempt in range(6):
            try:
                raw = yf.download(list(tickers), start=START, auto_adjust=False, group_by="ticker", threads=4,
                                  progress=False, timeout=30)
            except Exception as e:  # a dropped connection: wait and go again
                log.warning("lab_pull_retry chunk=%s error=%r", n, e)
                time.sleep(30 * (attempt + 1))
                continue
            parts = []
            for ticker, symbol in tickers.items():
                if ticker not in raw.columns.get_level_values(0):
                    continue
                d = raw[ticker].dropna(subset=["Close"])
                if d.empty:
                    continue
                factor = d["Adj Close"] / d["Close"]
                parts.append(pd.DataFrame({"symbol": symbol, "date": d.index, "open": d["Open"] * factor,
                                           "high": d["High"] * factor, "low": d["Low"] * factor, "close": d["Adj Close"],
                                           "price": d["Close"], "volume": d["Volume"]}))
            if len(parts) >= 0.3 * len(group) or len(group) < 10:
                break
            # Yahoo's rate limit comes back as an almost empty answer, not an error: wait it out and ask again.
            log.warning("lab_pull_thin chunk=%s names=%s of %s", n, len(parts), len(group))
            time.sleep(60 * (attempt + 1))
        else:
            raise RuntimeError(f"Yahoo gave almost nothing for chunk {n} after six tries (rate limit?); run the pull again later")
        pd.concat(parts).to_parquet(path, index=False) if parts else pd.DataFrame(columns=["symbol"]).to_parquet(path)
        have |= set(group)
        tried_path.write_text(json.dumps(sorted(have)))
        log.info("lab_pull chunk=%s/%s names=%s", n + 1, len(groups), len(parts))
        time.sleep(2)
    return build(market)


def build(market: str = "us") -> dict:
    """The wide tables from the chunks, with plainly broken bars removed."""
    root = _dir(market)
    long = pd.concat(pd.read_parquet(f) for f in sorted((root / "chunks").glob("*.parquet")))
    long = long.dropna(subset=["date"]).drop_duplicates(["symbol", "date"])
    long["date"] = pd.to_datetime(long["date"]).dt.tz_localize(None).dt.normalize()
    bench = MARKETS[market]["bench"]
    sessions = long.loc[long.symbol == bench, "date"]
    days = sorted(sessions)
    wide = {f: long.pivot(index="date", columns="symbol", values=f) for f in FIELDS}
    gauges = [g for g in MARKETS[market]["index"] if g in wide["close"]]
    for f in FIELDS:   # a gauge (a currency, the VIX) keeps its own calendar: carry its last value onto this market's sessions
        if gauges:
            wide[f][gauges] = wide[f][gauges].ffill()
        wide[f] = wide[f].reindex(days)
    # A bar is broken when a price is missing or not positive, the high is under the low, or the close moved more
    # than 12x or under a twelfth in a day (a missed split). Broken bars become gaps in every field.
    c = wide["close"]
    jump = c / c.shift(1)
    bad = (wide["open"] <= 0) | (wide["low"] <= 0) | (c <= 0) | (wide["high"] < wide["low"]) | (jump > 12) | (jump < 1 / 12)
    out = {}
    for f in FIELDS:
        t = wide[f].mask(bad)
        t.astype("float32" if f != "volume" else "float64").to_parquet(root / f"{f}.parquet")
        out[f] = t.shape
    info = {"market": market, "sessions": len(sessions), "first": str(min(sessions).date()), "last": str(max(sessions).date()),
            "symbols": int(c.shape[1]), "broken_bars": int(bad.sum().sum()), "built": pd.Timestamp.now().isoformat(timespec="minutes")}
    (root / "info.json").write_text(json.dumps(info, indent=1))
    return info


@dataclass
class Panel:
    market: str
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    price: pd.DataFrame
    volume: pd.DataFrame
    meta: pd.DataFrame
    bench: str
    funds: list[str] = field(default_factory=list)
    _cache: dict = field(default_factory=dict, repr=False)

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.close.index

    @cached_property
    def stocks(self) -> list[str]:
        """Columns that are stocks: everything but the funds and gauges."""
        skip = set(self.funds)
        return [s for s in self.close.columns if s not in skip]

    @cached_property
    def dollars(self) -> pd.DataFrame:
        """US dollars traded each day (local currency turned into dollars at that day's rate)."""
        rate = per_dollar(self.close, self.market)
        local = self.price * self.volume
        return local.div(rate, axis=0) if rate is not None else local

    @cached_property
    def adv(self) -> pd.DataFrame:
        """Median dollars traded over the last 20 sessions, through each day's close."""
        return self.dollars.rolling(20, min_periods=15).median()

    @cached_property
    def cc(self) -> pd.DataFrame:
        """Close-to-close return."""
        return self.close / self.close.shift(1) - 1

    @cached_property
    def oc(self) -> pd.DataFrame:
        """Open-to-close return, the same day."""
        return self.close / self.open - 1

    @cached_property
    def co(self) -> pd.DataFrame:
        """Overnight return: the prior close to this open."""
        return self.open / self.close.shift(1) - 1

    def universe(self, min_price: float = 5.0, min_adv: float = 5e6, top: int | None = None, bottom_of: int | None = None,
                 max_adv: float | None = None, history: int = 60) -> pd.DataFrame:
        """Who is tradable on each day, known at that day's close: a stock with `history` sessions behind it, priced
        `min_price` or more, with a median `min_adv` dollars a day. `top` keeps the N most traded; `max_adv` caps the
        size from above (for small-cap tests)."""
        key = ("u", min_price, min_adv, top, max_adv, history)
        if key not in self._cache:
            adv, price = self.adv[self.stocks], self.price[self.stocks]
            rate = per_dollar(self.close, self.market)
            if rate is not None:   # `min_price` is in dollars everywhere
                price = price.div(rate, axis=0)
            ok = (price >= min_price) & (adv >= min_adv) & (self.close[self.stocks].notna().rolling(history).sum() >= history * 0.95)
            if max_adv:
                ok &= adv <= max_adv
            if top:
                ok &= adv.where(ok).rank(axis=1, ascending=False) <= top
            self._cache[key] = ok
        return self._cache[key]

    def sector(self) -> pd.Series:
        return self.meta["sector"].reindex(self.close.columns).fillna("")

    def cut(self, start: str | None, end: str | None) -> "Panel":
        """The same panel between two dates."""
        return Panel(self.market, *(getattr(self, f).loc[start:end] for f in FIELDS), self.meta, self.bench, self.funds)


_loaded: dict = {}


def load(market: str = "us", holdout: bool = False) -> Panel:
    """The market's history. Without `holdout` it stops the day before the holdout years begin, so nothing that runs
    through here can see them; only registry.holdout() asks for the rest."""
    key = (market, holdout)
    if key not in _loaded:
        root, m = _dir(market), MARKETS[market]
        tables = [pd.read_parquet(root / f"{f}.parquet").astype("float64") for f in FIELDS]
        if not holdout:
            end = pd.Timestamp(HOLDOUT_START) - pd.Timedelta(days=1)
            tables = [t.loc[:end] for t in tables]
        meta = pd.read_json(root / "meta.json", dtype={"symbol": str}).set_index("symbol") if (root / "meta.json").exists() else pd.DataFrame(columns=["sector"])
        _loaded[key] = Panel(market, *tables, meta, m["bench"], list(m["funds"]) + list(m["index"]))
    return _loaded[key]


def synthetic(days: int = 600, names: int = 60, seed: int = 0) -> Panel:
    """A made-up market for tests: random walks with a known pattern nowhere in them."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2015-01-01", periods=days)
    cols = [f"S{i:02d}" for i in range(names)] + ["SPY"]
    r = rng.normal(0.0003, 0.02, (days, names + 1))
    close = pd.DataFrame(100 * np.exp(np.cumsum(r, axis=0)), index=dates, columns=cols)
    open_ = close.shift(1).fillna(100) * (1 + rng.normal(0, 0.005, close.shape))
    high, low = np.maximum(open_, close) * 1.005, np.minimum(open_, close) * 0.995
    volume = pd.DataFrame(1e6, index=dates, columns=cols)
    return Panel("test", open_, high, low, close, close.copy(), volume, pd.DataFrame({"sector": "x"}, index=cols), "SPY", ["SPY"])

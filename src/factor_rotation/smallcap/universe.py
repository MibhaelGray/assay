"""Who is in the small-cap book's universe (docs/small-cap-book.md, agreed 2026-11-19).

US companies worth $250M to $1B, priced $2 or more, with over $1M traded on a median day of the last twenty.
Biotech and pharma, banks and savings institutions and REITs are left out; other finance stays. So is anything that
isn't an operating company (closed-end funds, business development companies, royalty trusts and the like), whose
price follows what it holds, not news about a business: by name, and by the list in NOT_OPERATING for the ones whose
name and industry label don't show it (checked by hand on 2026-11-20; research_notes/Small cap screen design/). Rebuilt once a
week: the listing and market values from Nasdaq's screener, a year of daily bars from Yahoo, and each name's
analyst count from Yahoo, refreshed monthly (the count is recorded beside every verdict, to compare thinly and
well covered names later; it is not a filter)."""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from .. import bars
from ..config import NY
from . import HEDGE, ROOT, sources

log = logging.getLogger(__name__)

PATH = ROOT / "universe.json"
CAP = (250e6, 1e9)
MIN_PRICE = 2.0
MIN_DOLLAR_VOLUME = 1e6    # median of close x volume over the last 20 sessions
REBUILD_DAYS = 7
ANALYST_DAYS = 28
SYMBOL = re.compile(r"^[A-Z]{1,5}$")
NOT_COMMON = re.compile(r"\bwarrants?\b|\bunits?\b|\brights?\b|preferred|depositary|\bnotes\b|%|debenture|acquisition corp", re.I)
EXCLUDED = (("biotech and pharma", re.compile(r"biotech|pharmaceutical", re.I)),
            ("banks", re.compile(r"\bbanks?\b|savings institutions", re.I)),
            ("REITs", re.compile(r"real estate investment trust", re.I)))
NOT_OPERATING_NAME = re.compile(r"\bfunds?\b|\btrust\b|municipal|bancorp|bancshares|\bpharma", re.I)
_CEF = "BLW BST HQH IQI OIA VKI BCX ARDC DLY ETW EVN FTHY GBAB HQL IIM NIE OXLC RFI RMT TYG XFLT BME BSTZ DSL GUT LEO NXP VCV VGM VKQ VMO ASA"
_BDC = "BBDC BCSF CION FDUS MFIC NMFC SLRC TCPC CCAP CGBD GAIN GLAD PFLT RWAY GSBD KBDC NCDL"
NOT_OPERATING = {**{s: "closed-end fund" for s in _CEF.split()}, **{s: "business development company" for s in _BDC.split()},
                 **{s: "REIT" for s in ("GOOD", "OPI", "HPP", "AHRT")}, "SBR": "royalty trust", "PLBC": "bank",
                 "OBIO": "biotech", "SGP": "pharma", "FWDI": "crypto-treasury company (tracks its token holdings)",
                 "NANO": "recent SPAC merger (its price history is the cash shell)"}


def excluded(industry: str) -> str | None:
    """Why an industry is left out, or None."""
    return next((label for label, pattern in EXCLUDED if pattern.search(industry or "")), None)


def not_operating(row: dict) -> str | None:
    """Why a name isn't an operating company we research, or None."""
    if row["symbol"] in NOT_OPERATING:
        return NOT_OPERATING[row["symbol"]]
    return "fund, trust, bank or pharma by name" if NOT_OPERATING_NAME.search(row["name"]) else None


def eligible(row: dict) -> bool:
    """The listing rules: a US operating company's common stock in the size band, outside the excluded industries."""
    return (row["country"] == "United States" and CAP[0] <= row["cap"] < CAP[1] and bool(SYMBOL.match(row["symbol"]))
            and not NOT_COMMON.search(row["name"]) and excluded(row["industry"]) is None and not_operating(row) is None)


def sec_check(rows: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Each row's SEC company number (CIK) and industry code, and the rows the SEC's records leave out (edgar.left_out):
    REITs, banks, pharma and biotech, blank-check shells, registered funds. Unchanged if EDGAR doesn't answer."""
    from . import edgar
    try:
        tickers = edgar.ciks()
    except Exception as e:
        log.warning("smallcap_universe_sec_failed error=%r", e)
        return rows, {}
    docs, _ = edgar.companies({r["symbol"]: tickers[r["symbol"]] for r in rows if r["symbol"] in tickers})
    kept, out = [], {}
    for r in rows:
        d = docs.get(r["symbol"])
        if d:
            r = {**r, "cik": d["cik"], "sic": d["sic"]}
        if why := edgar.left_out(d):
            out[r["symbol"]] = why
        else:
            kept.append(r)
    return kept, out


def liquidity(symbols: list[str]) -> dict[str, dict]:
    """Each symbol's last close and median daily dollar volume over its last 20 stored sessions."""
    px = bars.load(symbols)
    out = {}
    if px.empty:
        return out
    for s, g in px.sort_values("date").groupby("symbol"):
        g = g.tail(20)
        if len(g) >= 15:
            out[s] = {"price": round(float(g["close"].iloc[-1]), 2),
                      "dollar_volume": round(float((g["close"] * g["volume"]).median()))}
    return out


def _analysts(symbol: str) -> int | None:
    import yfinance as yf
    try:
        info = yf.Ticker(symbol).info
    except Exception:
        return None
    return int(info.get("numberOfAnalystOpinions") or 0) if info.get("marketCap") else None


def load() -> dict | None:
    return json.loads(PATH.read_text()) if PATH.exists() else None


def stale(now: datetime | None = None) -> bool:
    doc = load()
    return not doc or (now or datetime.now(NY)) - datetime.fromisoformat(doc["built"]) > timedelta(days=REBUILD_DAYS)


def symbols() -> list[str]:
    return [r["symbol"] for r in (load() or {}).get("names", [])]


def build(now: datetime | None = None, listing=None, analysts=None, pull=True) -> dict:
    """Rebuild the universe file. `listing` and `analysts` stand in for the feeds in tests; `pull=False` uses the
    bars already stored."""
    now = now or datetime.now(NY)
    rows = [r for r in (listing if listing is not None else sources.listing()) if eligible(r)]
    names = [r["symbol"] for r in rows]
    if pull and names:
        have = set(bars.load(names)["symbol"]) if not bars.load(names).empty else set()
        if new := [s for s in names if s not in have]:
            bars.collect(new, period="1y")
        if old := [s for s in names if s in have]:
            bars.collect_recent(old)
        bars.collect_recent([HEDGE])
    liq = liquidity(names)
    kept = [{**r, **liq[r["symbol"]]} for r in rows
            if r["symbol"] in liq and liq[r["symbol"]]["price"] >= MIN_PRICE and liq[r["symbol"]]["dollar_volume"] > MIN_DOLLAR_VOLUME]
    sec_out = {}
    if pull and kept:
        kept, sec_out = sec_check(kept)
    before = {r["symbol"]: r for r in (load() or {}).get("names", [])}
    fresh = lambda r: r.get("analysts") is not None and r.get("analysts_at") and \
        now - datetime.fromisoformat(r["analysts_at"]) < timedelta(days=ANALYST_DAYS)   # noqa: E731
    due = [r["symbol"] for r in kept if not fresh(before.get(r["symbol"], {}))]
    if analysts is None:
        with ThreadPoolExecutor(max_workers=4) as pool:
            counted = dict(zip(due, pool.map(_analysts, due)))
    else:
        counted = {s: analysts.get(s) for s in due}
    for r in kept:
        old, n = before.get(r["symbol"], {}), counted.get(r["symbol"])
        r["analysts"] = n if n is not None else old.get("analysts")
        r["analysts_at"] = now.isoformat(timespec="seconds") if n is not None else old.get("analysts_at")
    doc = {"built": now.isoformat(timespec="seconds"),
           "rules": {"cap": list(CAP), "min_price": MIN_PRICE, "min_dollar_volume": MIN_DOLLAR_VOLUME,
                     "excluded": [label for label, _ in EXCLUDED] + ["funds, trusts and other non-operating companies"]},
           "listed_in_band": len(rows), "left_out_by_sec": sec_out, "names": sorted(kept, key=lambda r: r["symbol"])}
    ROOT.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(doc, indent=1) + "\n")
    log.info("smallcap_universe names=%d listed=%d analysts_counted=%d", len(kept), len(rows), len(due))
    return doc


def summary(doc: dict) -> str:
    names = doc["names"]
    known = [r["analysts"] for r in names if r.get("analysts") is not None]
    thin = sum(1 for n in known if n <= 2)
    return (f"Small-cap universe: {len(names)} names (of {doc['listed_in_band']} listed in the size band), built {doc['built'][:10]}. "
            + (f"Analyst counts for {len(known)}: {thin} have two or fewer." if known else "No analyst counts yet."))

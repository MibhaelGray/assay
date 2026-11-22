"""Symbol lists for the foreign markets, written to data/lab/<market>/listing.json for panel.pull.

Each comes from the exchange's own list where one is free (Hong Kong, Korea, Taiwan, Australia) and from Wikipedia's
index member lists otherwise (Canada, London). Today's lists, so the history holds only names that still trade.
Needs openpyxl and lxml: `uv run --with openpyxl --with lxml fr lab fetch listing --market hk`.
"""

import io
import json
import re

import pandas as pd
import requests

from . import DATA

HEADERS = {"User-Agent": "Mozilla/5.0"}


def _get(url: str) -> bytes:
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.content


def _wiki(url: str, ticker_cols=("Ticker", "Code", "EPIC")) -> list[str]:
    for t in pd.read_html(io.StringIO(_get(url).decode("utf-8"))):
        col = next((c for c in t.columns if str(c).split("[")[0].strip() in ticker_cols), None)
        if col is not None and len(t) >= 30:
            return [str(x).strip() for x in t[col].dropna()]
    raise RuntimeError(f"no ticker table on {url}")


def jp() -> list[dict]:
    """Every domestic share on the Prime, Standard and Growth markets, from JPX's list of listed issues."""
    page = _get("https://www.jpx.co.jp/english/markets/statistics-equities/misc/01.html").decode("utf-8", errors="ignore")
    link = re.search(r'href="([^"]*data_e\.xlsx?)"', page).group(1)
    t = pd.read_excel(io.BytesIO(_get("https://www.jpx.co.jp" + link)), dtype=str)
    t = t[t["Section/Products"].str.contains("Prime|Standard|Growth")]
    return [{"symbol": c, "name": n, "sector": s17, "industry": s33, "size": size, "section": sec.split(" Market")[0], "country": "Japan", "cap": 0.0}
            for c, n, sec, s33, s17, size in zip(t["Local Code"], t["Name (English)"], t["Section/Products"], t["33 Sector(name)"],
                                                 t["17 Sector(name)"], t["Size (New Index Series)"])]


def hk() -> list[dict]:
    t = pd.read_excel(io.BytesIO(_get("https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx")), header=2)
    t = t[(t["Category"] == "Equity") & (t["Trading Currency"] == "HKD") & (t["Stock Code"] < 10000)]
    return [{"symbol": f"{int(c):04d}", "name": n, "sector": "", "board": s} for c, n, s in zip(t["Stock Code"], t["Name of Securities"], t["Sub-Category"])]


def kr() -> list[dict]:
    out = []
    for kind, suffix in (("stockMkt", ".KS"), ("kosdaqMkt", ".KQ")):
        raw = _get(f"https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13&marketType={kind}")
        t = pd.read_html(io.StringIO(raw.decode("euc-kr", errors="ignore")))[0]
        for code, sector in zip(t["종목코드"].astype(str), t["업종"]):
            if re.fullmatch(r"\d{6}", code.zfill(6)):
                out.append({"symbol": code.zfill(6), "name": "", "sector": str(sector), "yahoo": code.zfill(6) + suffix})
    return out


def tw() -> list[dict]:
    out = []
    for mode, suffix in (("2", ".TW"), ("4", ".TWO")):
        raw = _get(f"https://isin.twse.com.tw/isin/e_C_public.jsp?strMode={mode}")
        t = pd.read_html(io.StringIO(raw.decode("big5", errors="ignore")), header=0)[0]
        for name, cfi, group in zip(t.iloc[:, 0].astype(str), t["CFICode"].astype(str), t["Industrial Group"]):
            code, _, label = name.partition("　")
            if cfi.startswith("ES") and re.fullmatch(r"\d{4}", code.strip()):
                out.append({"symbol": code.strip(), "name": label.strip(), "sector": str(group), "yahoo": code.strip() + suffix})
    return out


def au() -> list[dict]:
    t = pd.read_csv(io.BytesIO(_get("https://www.asx.com.au/asx/research/ASXListedCompanies.csv")), skiprows=1)
    return [{"symbol": str(c).strip(), "name": n, "sector": str(g)} for n, c, g in t.itertuples(index=False) if isinstance(c, str)]


def ca() -> list[dict]:
    return [{"symbol": s, "name": "", "sector": ""} for s in _wiki("https://en.wikipedia.org/wiki/S%26P/TSX_Composite_Index")]


def uk() -> list[dict]:
    names = _wiki("https://en.wikipedia.org/wiki/FTSE_100_Index") + _wiki("https://en.wikipedia.org/wiki/FTSE_250_Index")
    return [{"symbol": s.rstrip("."), "name": "", "sector": ""} for s in dict.fromkeys(names)]


SOURCES = {"jp": jp, "hk": hk, "kr": kr, "tw": tw, "au": au, "ca": ca, "uk": uk}


def write(market: str) -> int:
    rows = list({r["symbol"]: r for r in SOURCES[market]()}.values())
    root = DATA / market
    root.mkdir(parents=True, exist_ok=True)
    (root / "listing.json").write_text(json.dumps(rows))
    return len(rows)

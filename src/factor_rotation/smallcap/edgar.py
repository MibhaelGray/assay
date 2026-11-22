"""SEC EDGAR for the small-cap book: each company's recent filings, read from the SEC itself (free, no key).

The SEC asks every automated request to name a contact (https://www.sec.gov/os/accessing-edgar-data). The owner keeps
it in .env as SEC_USER_AGENT; it goes only to sec.gov, in the request header, and is never printed or written down.
Requests are spaced to RATE a second (the SEC allows 10).

What the screen reads from here (screen.py; reports/Small cap screen design.md):
    events()      the filing route: an initial Schedule 13D, a late-filing notice (NT 10-K / NT 10-Q), and 8-K items
                  for bankruptcy, default, exit costs, impairments, a delisting notice or financials that can't be
                  relied on; plus the time of any earnings 8-K (item 2.02) and which 8-Ks came in at all
    purchases()   open-market buys (Form 4, code P) by officers and directors
The universe uses each company's industry code (SIC) and form types to keep funds, REITs, banks and pharma out.
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

from ..config import Settings

log = logging.getLogger(__name__)

TICKERS = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
RATE = 8
REPO = Path(__file__).resolve().parents[3]

INITIAL_13D = {"SCHEDULE 13D", "SC 13D"}
LATE = {"NT 10-K": "late annual report notice (NT 10-K)", "NT 10-Q": "late quarterly report notice (NT 10-Q)"}
DISTRESS = {"1.03": "bankruptcy or receivership (8-K 1.03)", "2.04": "debt default or acceleration (8-K 2.04)",
            "2.05": "exit or restructuring costs (8-K 2.05)", "2.06": "material impairment (8-K 2.06)",
            "3.01": "delisting notice or listing-rule failure (8-K 3.01)",
            "4.02": "past financials can no longer be relied on (8-K 4.02)"}
EARNINGS_ITEM = "2.02"
# Industry codes for what the book leaves out: blank-check shells, REITs, banks and savings institutions, pharma and
# biotech, and research labs (mostly biotech). A company filing N-CSR or N-2 is a registered fund.
LEFT_OUT_SIC = {6770: "blank-check company", 6798: "REIT", **{c: "bank" for c in range(6021, 6037)},
                **{c: "pharma or biotech" for c in range(2834, 2837)}, 8731: "research lab (biotech)"}
FUND_FORMS = {"N-CSR", "N-CSRS", "N-2", "N-2/A", "N-PORT-P"}

_lock = threading.Lock()
_last = [0.0]


def user_agent() -> str:
    agent = os.environ.get("SEC_USER_AGENT")
    for path in (Path(".env"), REPO / ".env"):
        if agent or not path.is_file():
            continue
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key == "SEC_USER_AGENT":
                agent = value.strip().strip('"')
                break
    if not agent:
        raise RuntimeError("SEC_USER_AGENT isn't set in .env; the SEC refuses requests that name no contact")
    return agent


def _get(url: str, timeout: int = 30, tries: int = 4) -> bytes:
    """One request, spaced to RATE a second, retried with a growing pause on a dropped connection, a failed name
    lookup, a timeout, or the SEC's own 429 or 5xx (this Mac's DNS drops out for seconds at a time)."""
    for attempt in range(tries):
        with _lock:
            wait = _last[0] + 1 / RATE - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": user_agent()})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500 or attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == tries - 1:
                raise
        time.sleep(2 * 2 ** attempt)
    raise RuntimeError("unreachable")


def ciks() -> dict[str, int]:
    """Ticker -> the SEC's company number (CIK), for every company the SEC lists with a ticker."""
    rows = json.loads(_get(TICKERS)).values()
    return {r["ticker"].upper(): int(r["cik_str"]) for r in rows}


def accepted(f: dict) -> datetime:
    return datetime.fromisoformat(f["accepted"].replace("Z", "+00:00"))


def company(cik: int) -> dict:
    """A company's industry code and its most recent filings (the SEC's last thousand or so), newest first."""
    d = json.loads(_get(SUBMISSIONS.format(cik=cik)))
    r = d["filings"]["recent"]
    filings = [{"form": r["form"][i], "filed": r["filingDate"][i], "accepted": r["acceptanceDateTime"][i],
                "items": [x.strip() for x in (r["items"][i] or "").split(",") if x.strip()],
                "accession": r["accessionNumber"][i], "doc": r["primaryDocument"][i]} for i in range(len(r["form"]))]
    return {"cik": cik, "sic": int(d["sic"]) if str(d.get("sic") or "").isdigit() else None, "filings": filings}


def companies(by_symbol: dict[str, int]) -> tuple[dict[str, dict], list[str]]:
    """company() for each symbol, a few at a time. Returns what came back and the symbols that failed."""
    out, failed = {}, []

    def one(item):
        s, cik = item
        try:
            return s, company(cik)
        except Exception as e:
            log.warning("edgar_company_failed symbol=%s error=%r", s, e)
            return s, None

    with ThreadPoolExecutor(max_workers=4) as pool:
        for s, doc in pool.map(one, by_symbol.items()):
            (out.__setitem__(s, doc) if doc else failed.append(s))
    return out, failed


def left_out(doc: dict | None) -> str | None:
    """Why the SEC's own records say a company isn't one the book researches, or None."""
    if not doc:
        return None
    if doc.get("sic") in LEFT_OUT_SIC:
        return f"{LEFT_OUT_SIC[doc['sic']]} (SEC industry code {doc['sic']})"
    if any(f["form"] in FUND_FORMS for f in doc["filings"]):
        return "registered fund (files N-CSR or N-2 with the SEC)"
    return None


def events(doc: dict, since: datetime) -> dict:
    """What a company filed at or after `since`: the filing-route events, the earnings 8-K's time, and every 8-K."""
    filing, earnings, eight_k = [], None, []
    for f in doc["filings"]:
        at = accepted(f)
        if at < since:
            continue
        form = f["form"]
        if form in INITIAL_13D:
            filing.append({"what": f"activist stake: initial Schedule 13D", "at": at.isoformat()})
        elif form in LATE:
            filing.append({"what": LATE[form], "at": at.isoformat()})
        elif form in ("8-K", "8-K/A"):
            eight_k.append({"items": f["items"], "at": at.isoformat()})
            filing += [{"what": DISTRESS[i], "at": at.isoformat()} for i in f["items"] if i in DISTRESS]
            if EARNINGS_ITEM in f["items"] and (earnings is None or at < earnings):
                earnings = at
    return {"filing": filing, "earnings": earnings.isoformat() if earnings else None, "eight_k": eight_k}


def _cache_path() -> Path:
    return Path(Settings().data_root) / "smallcap" / "form4.json"


def parse_form4(xml: bytes | str) -> dict:
    """The owners and open-market purchases (code P, shares acquired) on one Form 4."""
    root = ET.fromstring(xml)
    owners = []
    for o in root.findall("reportingOwner"):
        rel = o.find("reportingOwnerRelationship")
        flag = lambda k: ((rel.findtext(k) if rel is not None else None) or "").strip().lower() in ("1", "true")   # noqa: E731
        owners.append({"name": (o.findtext("reportingOwnerId/rptOwnerName") or "").strip(), "director": flag("isDirector"),
                       "officer": flag("isOfficer"), "ten_pct": flag("isTenPercentOwner"),
                       "title": ((rel.findtext("officerTitle") if rel is not None else "") or "").strip()})
    buys = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if t.findtext("transactionCoding/transactionCode") != "P" or \
                t.findtext("transactionAmounts/transactionAcquiredDisposedCode/value") != "A":
            continue
        try:
            shares = float(t.findtext("transactionAmounts/transactionShares/value") or 0)
            price = float(t.findtext("transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        buys.append({"date": t.findtext("transactionDate/value"), "shares": shares, "price": price, "value": shares * price})
    return {"owners": owners, "buys": buys}


def purchases(docs: dict[str, dict], filed_since: date) -> list[dict]:
    """Every open-market purchase on a Form 4 filed on or after `filed_since` for these companies, one row per buyer.
    Parsed Form 4s are cached by accession number, so each is fetched once."""
    path = _cache_path()
    cache = json.loads(path.read_text()) if path.exists() else {}
    rows, fetched = [], 0
    for symbol, doc in docs.items():
        for f in doc["filings"]:
            if f["form"] != "4" or f["filed"] < filed_since.isoformat():
                continue
            acc = f["accession"]
            if acc not in cache:
                raw = f["doc"].split("/", 1)[1] if f["doc"].startswith("xsl") and "/" in f["doc"] else f["doc"]
                try:
                    cache[acc] = parse_form4(_get(ARCHIVE.format(cik=doc["cik"], acc=acc.replace("-", ""), doc=raw)))
                    fetched += 1
                except Exception as e:
                    log.warning("edgar_form4_failed symbol=%s accession=%s error=%r", symbol, acc, e)
                    continue
            parsed = cache[acc]
            if not parsed["buys"]:
                continue
            for o in parsed["owners"]:
                rows.append({"symbol": symbol, "insider": o["name"], "director": o["director"], "officer": o["officer"],
                             "ten_pct_only": o["ten_pct"] and not (o["director"] or o["officer"]), "title": o["title"],
                             "value": sum(b["value"] for b in parsed["buys"]) / max(1, len(parsed["owners"])),
                             "filed": f["filed"], "accepted": accepted(f).isoformat(), "accession": acc})
    if fetched:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache))
    return rows


def since_close(day: date) -> datetime:
    """4:00 PM New York on `day`, in UTC: where a pass's filing window starts."""
    from ..config import NY
    return datetime(day.year, day.month, day.day, 16, 0, tzinfo=NY).astimezone(timezone.utc)

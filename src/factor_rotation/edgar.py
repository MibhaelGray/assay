"""SEC EDGAR filings for the swing book's disclosure door (free, no API key).

The SEC asks every automated client to identify itself in the HTTP User-Agent header
(https://www.sec.gov/os/accessing-edgar-data). Set SEC_USER_AGENT in .env (for example,
"Assay you@example.com"); it is sent only to sec.gov and never logged or written out.

Unlike per-company submissions loops (1,200+ requests), live screens query the SEC's EFTS full-text
search index by date range and form type (a handful of JSON calls, ~2-5 seconds total), match the
universe's CIKs from a daily-cached company_tickers.json, and cache parsed Form 4 XML files by
accession number so each Form 4 is fetched only once.
"""

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
RATE = 8                      # SEC permits 10 requests/second; stay safely under it
TICKERS_TTL = 24 * 3600       # refresh ticker->CIK map once a day
SUBMISSIONS_TTL = 6 * 3600    # refresh per-company submissions list every 6 hours
PAGE_SIZE = 100
MAX_PAGES = 15                # safety cap on EFTS pagination per query
MAX_FORM4_FETCH = 60          # cap uncached Form 4 XML fetches in a single screen call
FORM4_SINGLE_MIN = 100_000.0  # one officer/director open-market buy
FORM4_CLUSTER_MIN = 50_000.0  # combined open-market buys by 2+ officers/directors
SEC_HOSTS = {"sec.gov", "www.sec.gov", "data.sec.gov", "efts.sec.gov"}
BOILERPLATE_STOP = re.compile(
    r"^\s*(?:forward[- ]looking statements|cautionary (?:note|statement) regarding forward|safe harbor statement|"
    r"reconciliation of gaap|non-gaap financial measures reconciliation|about [a-z0-9 .,&'-]{2,40}$)",
    re.I,
)
EX99_RE = re.compile(r"(?:ex|exhibit)?[-_.]?99[-_.]?1\b|ex99|earnings|release|press", re.I)
XBRL_TABLE_RE = re.compile(r"^R\d+\.htm$|^Financial_Report|^FilingSummary|index\.htm", re.I)

REPO = Path(__file__).resolve().parents[2]

INITIAL_13D = {"SCHEDULE 13D", "SC 13D"}
LATE_FORMS = {
    "NT 10-K": "late annual report notice (NT 10-K)",
    "NT 10-Q": "late quarterly report notice (NT 10-Q)",
}
# High-signal material 8-K item codes (filtering out routine 1.01/5.02/7.01/8.01/9.01 noise).
MATERIAL_8K = {
    "1.02": "termination of a material agreement (8-K 1.02)",
    "1.03": "bankruptcy or receivership (8-K 1.03)",
    "2.04": "debt default or acceleration (8-K 2.04)",
    "2.05": "exit or restructuring costs (8-K 2.05)",
    "2.06": "material impairment (8-K 2.06)",
    "3.01": "delisting notice or listing-rule failure (8-K 3.01)",
    "4.02": "past financials can no longer be relied on (8-K 4.02)",
}
EARNINGS_ITEM = "2.02"

_lock = threading.Lock()
_last = [0.0]


def user_agent() -> str:
    """Contact header for sec.gov, from SEC_USER_AGENT in the environment or .env."""
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
        raise RuntimeError("SEC_USER_AGENT isn't set in .env; the SEC refuses requests with no contact header")
    return agent


def _get(url: str, timeout: int = 20, tries: int = 3) -> bytes:
    """Rate-limited GET to sec.gov with backoff on 429, 5xx, or transient network errors."""
    ua = user_agent()
    for attempt in range(tries):
        with _lock:
            wait = _last[0] + 1.0 / RATE - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if (e.code != 429 and e.code < 500) or attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == tries - 1:
                raise
        time.sleep(1.5 * 2 ** attempt)
    raise RuntimeError("unreachable")


def _cache_dir() -> Path:
    return Path(settings().data_root) / "edgar"


def ticker_ciks(fetcher=_get) -> dict[str, int]:
    """Normalized ticker -> SEC CIK integer, cached daily under data/edgar/tickers.json."""
    path = _cache_dir() / "tickers.json"
    if path.exists() and time.time() - path.stat().st_mtime < TICKERS_TTL:
        raw = json.loads(path.read_text())
    else:
        raw = json.loads(fetcher(TICKERS_URL))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw))
    out: dict[str, int] = {}
    for row in raw.values():
        t = str(row["ticker"]).upper()
        cik = int(row["cik_str"])
        out[t] = cik
        if "-" in t:
            out.setdefault(t.replace("-", "."), cik)
    return out


def _efts_hits(forms: str, start: date, end: date, fetcher=_get) -> list[dict]:
    """Paginate SEC EFTS search-index for `forms` between `start` and `end` (inclusive)."""
    hits: list[dict] = []
    for page in range(MAX_PAGES):
        q = urllib.parse.urlencode({
            "forms": forms,
            "dateRange": "custom",
            "startdt": start.isoformat(),
            "enddt": end.isoformat(),
            "from": page * PAGE_SIZE,
            "size": PAGE_SIZE,
        })
        payload = json.loads(fetcher(f"{EFTS_URL}?{q}"))
        batch = (payload.get("hits") or {}).get("hits") or []
        hits.extend(batch)
        total = ((payload.get("hits") or {}).get("total") or {}).get("value", 0)
        if not batch or len(hits) >= total:
            break
    return hits


def _doc_url(cik: int, hit_id: str, adsh: str) -> str:
    """Direct SEC archive URL from an EFTS hit's `_id` (`adsh:filename`) and issuer CIK."""
    doc = hit_id.split(":", 1)[1] if ":" in hit_id else ""
    if doc.startswith("xsl") and "/" in doc:
        doc = doc.split("/", 1)[1]
    acc = adsh.replace("-", "")
    return ARCHIVE_URL.format(cik=cik, acc=acc, doc=doc) if doc else f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"


def parse_form4(xml: bytes | str) -> dict:
    """Reporting owners and open-market purchases (code P, acquired) from one Form 4 XML."""
    root = ET.fromstring(xml)
    owners = []
    for o in root.findall("reportingOwner"):
        rel = o.find("reportingOwnerRelationship")
        flag = lambda k: ((rel.findtext(k) if rel is not None else None) or "").strip().lower() in ("1", "true")  # noqa: E731
        owners.append({
            "name": (o.findtext("reportingOwnerId/rptOwnerName") or "").strip(),
            "director": flag("isDirector"),
            "officer": flag("isOfficer"),
            "ten_pct": flag("isTenPercentOwner"),
            "title": ((rel.findtext("officerTitle") if rel is not None else "") or "").strip(),
        })
    buys = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = (tx.findtext("transactionCoding/transactionCode") or "").strip().upper()
        acq = (tx.findtext("transactionAmounts/transactionAcquiredDisposedCode/value") or "").strip().upper()
        if code != "P" or acq != "A":
            continue
        try:
            shares = float(tx.findtext("transactionAmounts/transactionShares/value") or 0)
            price = float(tx.findtext("transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        if shares > 0 and price > 0:
            buys.append({
                "date": (tx.findtext("transactionDate/value") or "").strip(),
                "shares": shares,
                "price": price,
                "value": shares * price,
            })
    return {"owners": owners, "buys": buys}


def _match_symbol(ciks: list[str], cik_to_sym: dict[int, str]) -> tuple[str, int] | None:
    for raw in ciks:
        if str(raw).isdigit() and (c := int(raw)) in cik_to_sym:
            return cik_to_sym[c], c
    return None


def fetch_recent(symbols: list[str], as_of: date, lookback_days: int = 2, form4_lookback_days: int = 3,
                 fetcher=_get, max_form4_fetch: int = MAX_FORM4_FETCH) -> dict:
    """Recent high-signal SEC EDGAR filings for `symbols`, keyed by symbol.

    Never raises on missing SEC_USER_AGENT or transient SEC outages: returns {"ok": False, "error": ..., "by_symbol": {}}
    so a screen can degrade cleanly to Door 2 + Yahoo earnings if EDGAR is unreachable.
    """
    start = as_of - timedelta(days=lookback_days)
    f4_start = as_of - timedelta(days=form4_lookback_days)
    try:
        t2c = ticker_ciks(fetcher)
    except Exception as e:  # noqa: BLE001
        log.warning("edgar_tickers_failed error=%r", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200], "by_symbol": {}}

    cik_to_sym: dict[int, str] = {}
    for sym in symbols:
        c = t2c.get(sym) or t2c.get(sym.replace(".", "-"))
        if c is not None:
            cik_to_sym.setdefault(c, sym)

    by_symbol: dict[str, list[dict]] = {}
    counts = {"8k_material": 0, "8k_earnings": 0, "13d": 0, "late": 0, "form4_buys": 0}
    seen_acc: set[tuple[str, str, str]] = set()

    def add(sym: str, event: dict) -> None:
        key = (sym, event["setup"], event.get("adsh") or event["url"])
        if key in seen_acc:
            return
        seen_acc.add(key)
        by_symbol.setdefault(sym, []).append(event)

    try:
        # 1. Material 8-Ks and earnings 8-Ks (Item 2.02)
        for h in _efts_hits("8-K", start, as_of, fetcher):
            src = h.get("_source") or {}
            if src.get("sequence") != 1:
                continue
            hit = _match_symbol(src.get("ciks") or [], cik_to_sym)
            if not hit:
                continue
            sym, cik = hit
            items = list(src.get("items") or [])
            adsh = src.get("adsh") or ""
            url = _doc_url(cik, h.get("_id") or "", adsh)
            fdate = src.get("file_date") or as_of.isoformat()
            mat = [i for i in items if i in MATERIAL_8K]
            if mat:
                desc = "; ".join(MATERIAL_8K[i] for i in mat)
                add(sym, {"setup": "edgar_8k", "direction": "short", "filed": fdate, "adsh": adsh, "items": mat,
                          "url": url, "why": f"filed {src.get('form', '8-K')} on {fdate}: {desc}"})
                counts["8k_material"] += 1
            if EARNINGS_ITEM in items:
                add(sym, {"setup": "edgar_earnings", "direction": "long", "filed": fdate, "adsh": adsh, "items": [EARNINGS_ITEM],
                          "url": url, "why": f"filed 8-K Item 2.02 (earnings release) on {fdate}"})
                counts["8k_earnings"] += 1

        # 2. Initial Schedule 13Ds and NT 10-K / NT 10-Q late-filing notices
        for h in _efts_hits("SC 13D,SCHEDULE 13D,NT 10-K,NT 10-Q", start, as_of, fetcher):
            src = h.get("_source") or {}
            if src.get("sequence") != 1:
                continue
            form = (src.get("form") or "").upper()
            if form not in INITIAL_13D and form not in LATE_FORMS:
                continue
            hit = _match_symbol(src.get("ciks") or [], cik_to_sym)
            if not hit:
                continue
            sym, cik = hit
            adsh = src.get("adsh") or ""
            url = _doc_url(cik, h.get("_id") or "", adsh)
            fdate = src.get("file_date") or as_of.isoformat()
            if form in INITIAL_13D:
                add(sym, {"setup": "edgar_13d", "direction": "long", "filed": fdate, "adsh": adsh, "url": url,
                          "why": f"filed initial activist Schedule 13D on {fdate}"})
                counts["13d"] += 1
            else:
                add(sym, {"setup": "edgar_late_filing", "direction": "short", "filed": fdate, "adsh": adsh, "url": url,
                          "why": f"filed {LATE_FORMS[form]} on {fdate}"})
                counts["late"] += 1

        # 3. Form 4 open-market insider purchases (cached by accession number)
        f4_path = _cache_dir() / "form4.json"
        f4_cache = json.loads(f4_path.read_text()) if f4_path.exists() else {}
        fetched = 0
        buys_by_sym: dict[str, list[dict]] = {}
        for h in _efts_hits("4", f4_start, as_of, fetcher):
            src = h.get("_source") or {}
            if src.get("sequence") != 1 or (src.get("form") or "").upper() != "4":
                continue
            hit = _match_symbol(src.get("ciks") or [], cik_to_sym)
            if not hit:
                continue
            sym, cik = hit
            adsh = src.get("adsh") or ""
            if not adsh:
                continue
            url = _doc_url(cik, h.get("_id") or "", adsh)
            if adsh not in f4_cache:
                if fetched >= max_form4_fetch:
                    continue
                try:
                    f4_cache[adsh] = parse_form4(fetcher(url))
                    fetched += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("edgar_form4_failed symbol=%s adsh=%s error=%r", sym, adsh, e)
                    continue
            parsed = f4_cache[adsh]
            if not parsed.get("buys"):
                continue
            insiders = [o for o in parsed.get("owners") or [] if o.get("officer") or o.get("director")]
            if not insiders:
                continue
            total_val = sum(b["value"] for b in parsed["buys"])
            if total_val <= 0:
                continue
            buys_by_sym.setdefault(sym, []).append({
                "names": [o["name"] or o["title"] or "insider" for o in insiders],
                "titles": [o["title"] for o in insiders if o.get("title")],
                "value": total_val,
                "filed": src.get("file_date") or as_of.isoformat(),
                "adsh": adsh,
                "url": url,
            })
        if fetched:
            f4_path.parent.mkdir(parents=True, exist_ok=True)
            f4_path.write_text(json.dumps(f4_cache))
        for sym, rows in buys_by_sym.items():
            total_val = sum(r["value"] for r in rows)
            distinct = {n.upper() for r in rows for n in r["names"]}
            qualifies = total_val >= FORM4_SINGLE_MIN or (len(distinct) >= 2 and total_val >= FORM4_CLUSTER_MIN)
            if not qualifies:
                continue
            rows.sort(key=lambda r: -r["value"])
            who = ", ".join(dict.fromkeys(r["names"][0] for r in rows[:3]))
            cluster = f"cluster of {len(distinct)} insiders" if len(distinct) >= 2 else who
            add(sym, {
                "setup": "edgar_insider_buy",
                "direction": "long",
                "filed": max(r["filed"] for r in rows),
                "adsh": rows[0]["adsh"],
                "url": rows[0]["url"],
                "urls": [r["url"] for r in rows[:3]],
                "value": round(total_val),
                "insiders": len(distinct),
                "why": f"Form 4 open-market purchase ({cluster}) totalling ${total_val:,.0f} (latest filed {max(r['filed'] for r in rows)})",
            })
            counts["form4_buys"] += 1
    except Exception as e:  # noqa: BLE001
        log.warning("edgar_fetch_failed error=%r", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200], "by_symbol": by_symbol, "counts": counts}

    return {
        "ok": True,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": [start.isoformat(), as_of.isoformat()],
        "matched_ciks": len(cik_to_sym),
        "counts": counts,
        "by_symbol": by_symbol,
    }


class _HTMLText(HTMLParser):
    """Strip HTML/iXBRL tags into clean readable paragraphs without external dependencies."""

    SKIP = {"script", "style", "head", "title", "ix:header", "ix:hidden", "noscript"}
    BREAK = {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr", "section", "article"}
    CELL = {"td", "th"}

    def __init__(self):
        super().__init__()
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in self.SKIP or any(k.lower() == "style" and "display:none" in (v or "").replace(" ", "").lower() for k, v in attrs):
            self._skip += 1
            return
        if self._skip:
            return
        if t in self.BREAK:
            self.parts.append("\n")
        elif t in self.CELL:
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in self.SKIP and self._skip:
            self._skip -= 1
        elif not self._skip and t in self.BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: bytes | str, max_chars: int = 8_000, trim_boilerplate: bool = True) -> str:
    """Convert raw SEC HTML or plain text into concise, readable Markdown-friendly text."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    if "<html" in text[:4000].lower() or "<p" in text[:4000].lower() or "<div" in text[:4000].lower() or "<table" in text[:4000].lower():
        parser = _HTMLText()
        parser.feed(text)
        text = "".join(parser.parts)
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line.replace("\xa0", " ")).strip(" |")
        line = re.sub(r"(?:\s*\|\s*){2,}", " | ", line)
        if not line:
            continue
        if trim_boilerplate and sum(len(x) + 1 for x in lines) >= 3_000 and BOILERPLATE_STOP.match(line):
            break
        lines.append(line)
        if sum(len(x) + 1 for x in lines) >= max_chars + 400:
            break
    out = "\n".join(lines)
    if len(out) > max_chars:
        cut = out.rfind("\n", 0, max_chars)
        out = out[:cut if cut > max_chars // 2 else max_chars].rstrip() + "\n[... trimmed for brevity ...]"
    return out


def read_url(url: str, max_chars: int = 14_000, fetcher=_get) -> str:
    """Fetch a sec.gov filing URL using SEC_USER_AGENT and return clean readable text."""
    parsed = urllib.parse.urlparse(url.strip())
    host = parsed.netloc.lower()
    if parsed.scheme != "https" or host not in SEC_HOSTS:
        raise ValueError(f"fr edgar read only fetches https://sec.gov URLs (got {url!r})")
    raw = fetcher(url.strip())
    return html_to_text(raw, max_chars=max_chars, trim_boilerplate=False)


def company_submissions(cik: int, fetcher=_get) -> dict:
    """Per-company recent SEC filings metadata, cached for SUBMISSIONS_TTL seconds."""
    path = _cache_dir() / "submissions" / f"CIK{cik:010d}.json"
    if path.exists() and time.time() - path.stat().st_mtime < SUBMISSIONS_TTL:
        return json.loads(path.read_text())
    data = json.loads(fetcher(SUBMISSIONS_URL.format(cik=cik)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def _pick_exhibit(cik: int, acc: str, primary_doc: str, form: str, fetcher=_get) -> tuple[str, str]:
    """For 8-K / 6-K filings, prefer Exhibit 99.1 (the actual press release) over the 1-page cover sheet."""
    acc_no = acc.replace("-", "")
    default = (primary_doc, ARCHIVE_URL.format(cik=cik, acc=acc_no, doc=primary_doc))
    if not form.upper().startswith(("8-K", "6-K")):
        return default
    try:
        idx = json.loads(fetcher(INDEX_URL.format(cik=cik, acc=acc_no)))
    except Exception:  # noqa: BLE001
        return default
    items = ((idx.get("directory") or {}).get("item")) or []
    candidates = []
    for it in items:
        name = str(it.get("name") or "")
        low = name.lower()
        if not low.endswith((".htm", ".html", ".txt")) or name == primary_doc or XBRL_TABLE_RE.search(name):
            continue
        if low.endswith(".txt") and acc_no in low.replace("-", ""):
            continue  # skip the raw full-submission .txt bundle
        try:
            size = int(it.get("size") or 0)
        except ValueError:
            size = 0
        if size < 400:
            continue
        score = (2 if EX99_RE.search(name) else 0) + (1 if low.endswith((".htm", ".html")) else 0)
        candidates.append((score, size, name))
    if not candidates:
        return default
    candidates.sort(reverse=True)
    best = candidates[0][2]
    if candidates[0][0] >= 2:
        return best, ARCHIVE_URL.format(cik=cik, acc=acc_no, doc=best)
    return default


def _cached_doc_text(cik: int, acc: str, doc: str, url: str, max_chars: int, fetcher=_get) -> str:
    acc_no = acc.replace("-", "")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", doc)
    path = _cache_dir() / "docs" / f"{acc_no}_{safe}.txt"
    if path.exists():
        text = path.read_text()
    else:
        text = html_to_text(fetcher(url), max_chars=12_000, trim_boilerplate=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if len(text) > max_chars:
        cut = text.rfind("\n", 0, max_chars)
        return text[:cut if cut > max_chars // 2 else max_chars].rstrip() + "\n[... trimmed for brevity ...]"
    return text


def recent_filings_text(symbol: str, cik: int, as_of: date, *, earnings_days: int = 45, material_days: int = 21,
                        max_filings: int = 3, primary_chars: int = 6_000, secondary_chars: int = 2_600,
                        fetcher=_get) -> list[dict]:
    """Recent primary SEC filing text (preferring Exhibit 99.1 press releases) for one company."""
    try:
        sub = company_submissions(cik, fetcher=fetcher)
    except Exception as e:  # noqa: BLE001
        log.warning("edgar_submissions_failed symbol=%s cik=%s error=%r", symbol, cik, e)
        return []
    rec = (sub.get("filings") or {}).get("recent") or {}
    forms = rec.get("form") or []
    dates = rec.get("filingDate") or []
    accs = rec.get("accessionNumber") or []
    docs = rec.get("primaryDocument") or []
    items_col = rec.get("items") or []

    earn_cut = (as_of - timedelta(days=earnings_days)).isoformat()
    mat_cut = (as_of - timedelta(days=material_days)).isoformat()
    recent_cut = (as_of - timedelta(days=7)).isoformat()

    picked: list[dict] = []
    have_earnings = False
    for i, form in enumerate(forms):
        fdate = dates[i] if i < len(dates) else ""
        if not fdate or fdate < earn_cut:
            break
        fu = str(form).upper()
        acc = accs[i] if i < len(accs) else ""
        pdoc = docs[i] if i < len(docs) else ""
        raw_items = items_col[i] if i < len(items_col) else []
        items = raw_items if isinstance(raw_items, list) else [x.strip() for x in str(raw_items).split(",") if x.strip()]
        if not acc or not pdoc:
            continue
        is_earn_8k = fu in ("8-K", "8-K/A") and EARNINGS_ITEM in items
        is_mat_8k = fu in ("8-K", "8-K/A") and any(x in MATERIAL_8K for x in items)
        is_recent_8k = fu == "8-K" and fdate >= recent_cut
        is_6k = fu in ("6-K", "6-K/A") and fdate >= mat_cut
        is_13d_or_late = fu in INITIAL_13D or fu in LATE_FORMS
        if is_earn_8k and not have_earnings:
            picked.append({"form": fu, "filed": fdate, "acc": acc, "primary_doc": pdoc, "items": items, "kind": "earnings"})
            have_earnings = True
        elif fdate >= mat_cut and (is_mat_8k or is_13d_or_late or is_recent_8k or is_6k):
            picked.append({"form": fu, "filed": fdate, "acc": acc, "primary_doc": pdoc, "items": items,
                           "kind": "earnings" if (is_6k and not have_earnings) else "material"})
            if is_6k and not have_earnings:
                have_earnings = True
        if len(picked) >= max_filings:
            break

    out: list[dict] = []
    for idx, row in enumerate(picked):
        doc, url = _pick_exhibit(cik, row["acc"], row["primary_doc"], row["form"], fetcher=fetcher)
        limit = primary_chars if (idx == 0 or row["kind"] == "earnings") else secondary_chars
        try:
            body = _cached_doc_text(cik, row["acc"], doc, url, max_chars=limit, fetcher=fetcher)
        except Exception as e:  # noqa: BLE001
            log.warning("edgar_doc_failed symbol=%s url=%s error=%r", symbol, url, e)
            continue
        if not body.strip():
            continue
        out.append({
            "form": row["form"],
            "filed": row["filed"],
            "items": row["items"],
            "doc": doc,
            "url": url,
            "text": body,
        })
    return out

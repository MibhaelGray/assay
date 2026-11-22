"""The small-cap book's free feeds. Each returns plain rows and raises on a bad reply; callers decide what a
failure costs. No request carries anything about the owner or the book."""

import json
import re
import urllib.error
import urllib.request
from datetime import date, datetime

AGENT = "Mozilla/5.0"
LISTING = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=25&download=true"
EARNINGS = "https://api.nasdaq.com/api/calendar/earnings?date={day}"
# OpenInsider's screener: open-market purchases (Form 4, code P) filed in the last `fd` days, $25k or more.
INSIDERS = ("http://openinsider.com/screener?s=&o=&pl=&ph=&ll=&lh=&fd={days}&fdr=&td=0&tdr=&fdlyl=&fdlyh=&daysago=&xp=1"
            "&vl=25&vh=&ocl=&och=&sic1=-1&sicl=100&sich=9999&grp=0&nfl=&nfh=&nil=&nih=&nol=&noh=&v2l=&v2h=&oc2l=&oc2h="
            "&sortcol=0&cnt=1000&page=1")


def _get(url: str, timeout: int = 60, tries: int = 3) -> str:
    """One request, retried with a pause when the connection or the name lookup drops out."""
    import time
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "application/json, text/html"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == tries - 1:
                raise
            time.sleep(3 * 2 ** attempt)
    raise RuntimeError("unreachable")


def _money(text) -> float | None:
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(text)))
    except ValueError:
        return None


def listing() -> list[dict]:
    """Every US-listed stock Nasdaq's screener knows: symbol, name, market value, country, sector, industry."""
    rows = json.loads(_get(LISTING))["data"]["rows"]
    return [{"symbol": r["symbol"].strip(), "name": r["name"].strip(), "cap": _money(r.get("marketCap")) or 0.0,
             "country": r.get("country") or "", "sector": r.get("sector") or "", "industry": r.get("industry") or ""}
            for r in rows]


def earnings(day: date) -> dict[str, str]:
    """Who reports on `day`: symbol -> "before" (before the open), "after" (after the close) or "unknown"."""
    rows = (json.loads(_get(EARNINGS.format(day=day.isoformat())))["data"] or {}).get("rows") or []
    when = {"time-pre-market": "before", "time-after-hours": "after"}
    return {r["symbol"].strip(): when.get(r.get("time"), "unknown") for r in rows}


def parse_insiders(html: str) -> list[dict]:
    """The purchase rows of an OpenInsider screener page."""
    start = html.find('class="tinytable"')
    if start < 0 or "<tbody>" not in html[start:]:
        return []
    body = html[start:].split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 13 or not cells[7].startswith("P"):
            continue
        try:
            filed = datetime.strptime(cells[1][:10], "%Y-%m-%d").date()
            traded = datetime.strptime(cells[2][:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        symbol = cells[3].rsplit(">", 1)[-1].strip()
        out.append({"symbol": symbol, "filed": filed.isoformat(), "traded": traded.isoformat(), "insider": cells[5],
                    "title": cells[6], "price": _money(cells[8]), "shares": _money(cells[9]), "value": _money(cells[12])})
    return out


def insiders(days: int = 7) -> list[dict]:
    return parse_insiders(_get(INSIDERS.format(days=days)))

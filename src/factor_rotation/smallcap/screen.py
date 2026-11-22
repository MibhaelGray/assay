"""The small-cap book's screen: what happened in the universe that research could understand.

Three passes a day, like the swing book's, on regular-hours prices only (the owner, 2026-11-20: no overnight or
pre-market): `fr smallcap screen --pass morning` (about 9:30 AM CT) and `--pass midday` (about 1:00 PM CT) read the
session so far, with volume projected to a full day; `--pass night` (after the close) reads the day's close.

Since 2026-11-20 (reports/Small cap screen design.md) names come in because something was disclosed, not because the
price jumped: moves with identifiable news tend to continue and moves without it tend to fade. Four routes, in tiers:

  A  filing          since the last session's close: an initial Schedule 13D (an activist stake), a late-filing notice
                     (NT 10-K / NT 10-Q), or an 8-K for bankruptcy, default, exit costs, an impairment, a delisting notice
                     or financials that can't be relied on (edgar.py)
  A  insider_buying  officers or directors bought in the open market (Form 4, code P) within five sessions, one of
                     them $50,000 or more, or two or more together $25,000 or more, with at least one filed since the
                     last session's close
  B  earnings_reaction  the company reported since the last session's close, however the stock moved; the night pass
                     includes reports out after today's close (an earnings 8-K seen), entered at the next open
  C  news_move       no report, but the stock moved at least 5% beyond what IWM's move implies for it (its beta over
                     120 sessions), at least one typical day's range, on 1.5 times its usual volume

One ranked list: tier A, then B, then C, and within a tier the move's size in typical ranges, up to MAX. The list is
shown to the judge in a shuffled order, so its position can't steer the ranking; the screen's own order is kept in
`screen_order` for scoring. A name judged in the last three sessions, or earlier in this session, is left out unless a
new filing or report has come in since its verdict. Each candidate carries a few base rates from the book's own
history (`facts`), for the judge to weigh as facts, not rules. The screen says nothing about direction.

A morning or midday verdict is entered at the live price when it is recorded (record.py); a night verdict at the next
session's open.
"""

import json
import logging
import random
from datetime import date, datetime, time

import pandas as pd

from .. import bars, journal
from ..config import NY
from . import HEDGE, ROOT, edgar, sources, universe

log = logging.getLogger(__name__)

SCREENS = ROOT / "screens"
VERDICTS = ROOT / "verdicts"
MAX = 20
CLOSE_PRINTED = time(16, 5)     # New York: today's daily bar is in by then
NEWS_MOVE = (0.05, 1.0)         # beyond the beta-implied move: at least this much, and this many typical daily ranges
NEWS_VOLUME = 1.5               # times the 20-day average
OLD_NEWS_BAR = (0.05, 1.5, 2.0)   # the bar before 2026-11-20 (against IWM one for one), kept as a tag to compare
INSIDER_ONE, INSIDER_GROUP = 50_000, 25_000
INSIDER_SESSIONS = 5
BETA_DAYS = 120
JUDGED_SESSIONS = 3
PASSES = ("morning", "midday", "night")
OPEN, LAST_ENTRY = time(9, 30), time(15, 50)   # New York: a morning or midday pass reads and enters inside these
TIER = {"filing": "A", "insider_buying": "A", "earnings_reaction": "B", "news_move": "C"}
# Base rates from the book's own five-year history (research_notes/Small cap screen design/own_data_*.md), next-open
# entry, against IWM, before costs. Small next to a 1% round trip: facts for the judge, not signals.
FACT_NEW_HIGH = "An up move that closes at a new 55-day high has usually given back 30-45 bp the next session."
FACT_NEW_LOW = "A drop to a new 55-day low, or to more than 2 typical ranges below the 50-day average, has usually fallen another 25-50 bp the next session."
FACT_NO_NEWS_UP = "A heavy-volume up move with no report has usually faded 40-50 bp the next session, with nothing reliable after."
FACT_HOT_DAY = "After a high-attention day the stock has tended to gain overnight and give it back between the next open and close."
FACT_EARNINGS = "After earnings, this universe has shown no reliable drift from the next open; the move happens on the reaction day."


def sessions(now: datetime | None = None, slot: str = "night") -> tuple[date, date]:
    """(the session whose prices the screen reads, the session its verdicts are for). A morning or midday pass reads
    the session that is trading and is for it; it raises outside regular hours."""
    now = now or datetime.now(NY)
    today = now.date()
    if slot != "night":
        if not journal.is_trading_day(today) or not OPEN <= now.time() <= LAST_ENTRY:
            raise ValueError(f"the {slot} small-cap pass runs during regular hours (8:30 AM to 2:50 PM CT on a trading day)")
        return today, today
    seen = today if journal.is_trading_day(today) and now.time() >= CLOSE_PRINTED else journal.previous_trading_day(today)
    return seen, journal.next_trading_day(seen)


def key(session: str, slot: str) -> str:
    """A pass's file name: the session alone for the night pass, `<session>-<pass>` for the others."""
    return session if slot == "night" else f"{session}-{slot}"


def day_share(now: datetime) -> float:
    """The usual share of a day's volume traded by `now`: 1 once the session is over."""
    from ..live import volume_fraction
    if now.time() >= time(16, 0):
        return 1.0
    minutes = (datetime.combine(now.date(), now.time()) - datetime.combine(now.date(), OPEN)).total_seconds() / 60
    return max(0.05, volume_fraction(minutes))


def beta(g: pd.DataFrame, h: pd.DataFrame, days: int = BETA_DAYS) -> float:
    """The stock's beta to IWM over the `days` sessions before its latest bar, pulled a third of the way toward 1 (Blume's
    adjustment: a raw estimate on a stock that barely tracks IWM is mostly noise) and held to 0-3; 1 with under 60 sessions."""
    a = g.set_index("date")["close"].pct_change()
    b = h.set_index("date")["close"].pct_change()
    j = pd.concat([a, b], axis=1, join="inner").dropna()
    j = j[j.index < g["date"].max()].tail(days)
    if len(j) < 60 or not j.iloc[:, 1].var():
        return 1.0
    raw = j.cov().iloc[0, 1] / j.iloc[:, 1].var()
    return float(min(3.0, max(0.0, 2 / 3 * raw + 1 / 3)))


def numbers(g: pd.DataFrame, share: float = 1.0) -> dict | None:
    """One name's numbers on its latest bar, or None with too little history. `share` is the part of a day's volume
    usually traded by now, so a session still trading is set against a full day's volume."""
    g = g.sort_values("date")
    if len(g) < 30:
        return None
    last, prev = g.iloc[-1], g.iloc[-2]
    past = g.iloc[-21:-1]
    before = g["close"].shift(1)
    true_range = pd.concat([g["high"] - g["low"], (g["high"] - before).abs(), (g["low"] - before).abs()], axis=1).max(axis=1)
    atr = float(true_range.iloc[-15:-1].mean())
    avg_volume = float(past["volume"].mean())
    closes = g["close"]
    prior55 = closes.iloc[-56:-1]
    ma50 = float(closes.iloc[-51:-1].mean()) if len(g) >= 51 else None
    return {"date": last["date"].date(), "close": round(float(last["close"]), 2), "prev_close": round(float(prev["close"]), 2),
            "ret": float(last["close"] / prev["close"] - 1), "gap": float(last["open"] / prev["close"] - 1),
            "intraday": float(last["close"] / last["open"] - 1) if last["open"] else 0.0,
            "atr_pct": atr / float(prev["close"]) if prev["close"] else None,
            "volume_ratio": float(last["volume"]) / share / avg_volume if avg_volume else None,
            "above_50d": bool(last["close"] > closes.tail(50).mean()) if len(g) >= 50 else None,
            "above_200d": bool(last["close"] > closes.tail(200).mean()) if len(g) >= 200 else None,
            "month_ret": float(last["close"] / closes.iloc[-22] - 1) if len(g) >= 22 else None,
            "new_high_55": bool(len(prior55) >= 55 and last["close"] > prior55.max()),
            "new_low_55": bool(len(prior55) >= 55 and last["close"] < prior55.min()),
            "ranges_below_50d": (ma50 - float(last["close"])) / atr if ma50 and atr else None,
            "off_52w_high": float(last["close"] / closes.tail(252).max() - 1)}


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _move_words(n: dict, hedge_ret: float, b: float, live: bool = False) -> str:
    return (f"{_pct(n['ret'])} {'so far today' if live else 'on the day'} ({_pct(n['ret'] - b * hedge_ret)} beyond what IWM's "
            f"{_pct(hedge_ret)} implies at its beta of {b:.1f}; {_pct(n['ret'] - hedge_ret)} against IWM one for one), "
            f"{n['excess_ranges']:.1f} typical daily ranges, on {n['volume_ratio']:.1f} times usual volume"
            f"{' (projected to a full day)' if live else ''}; opened {_pct(n['gap'])} from the last close, then "
            f"{_pct(n['intraday'])} from the open; {'last' if live else 'closed at'} {n['close']:.2f}")


def facts(n: dict, setup: str, filed_8k: bool) -> list[str]:
    """The base rates that apply to this candidate's day, for the judge."""
    out = []
    up, big = n["ret"] > 0, n["excess_ranges"] >= 1.0   # the extension base rates were measured on big-move days
    if big and up and n["new_high_55"]:
        out.append(FACT_NEW_HIGH)
    if big and not up and (n["new_low_55"] or (n["ranges_below_50d"] or 0) > 2):
        out.append(FACT_NEW_LOW)
    if setup == "news_move" and up and n["volume_ratio"] >= 2:
        out.append(FACT_NO_NEWS_UP)
    if n["volume_ratio"] >= 2 and abs(n["ret"]) >= 0.05:
        out.append(FACT_HOT_DAY)
    if setup == "earnings_reaction":
        out.append(FACT_EARNINGS)
    out.append("An 8-K came in since the last close." if filed_8k else "No 8-K since the last close.")
    return out


def judged_recently(for_session: date, this: str | None = None) -> dict[str, datetime]:
    """Names judged in the last JUDGED_SESSIONS sessions before `for_session`, or by an earlier pass for it, with when
    their latest verdict was recorded."""
    if not VERDICTS.exists():
        return {}
    files = [p for p in VERDICTS.glob("*.json") if p.stem != this]
    before = sorted({p.stem[:10] for p in files if p.stem[:10] < for_session.isoformat()})[-JUDGED_SESSIONS:]
    out: dict[str, datetime] = {}
    for p in files:
        if p.stem[:10] not in before and p.stem[:10] != for_session.isoformat():
            continue
        doc = json.loads(p.read_text())
        at = datetime.fromisoformat(doc["recorded"]) if doc.get("recorded") else datetime.fromisoformat(p.stem[:10]).replace(tzinfo=NY)
        for v in doc.get("verdicts", []):
            out[v["name"]] = max(out.get(v["name"], at), at)
    return out


def insider_groups(rows: list[dict], since: datetime) -> dict[str, dict]:
    """Purchases by officers and directors, by symbol: the total, the buyers, whether one was filed at or after `since`.
    A holder who is only a 10% owner (a parent, a fund) is not an officer or director buying."""
    out: dict[str, dict] = {}
    for r in rows:
        if not r.get("value") or r["value"] <= 0 or r.get("ten_pct_only"):
            continue
        g = out.setdefault(r["symbol"], {"value": 0.0, "buyers": {}, "new": False, "last_filed": r["filed"], "latest": r["accepted"]})
        g["value"] += r["value"]
        g["buyers"].setdefault(r["insider"], {"title": r["title"] or ("director" if r.get("director") else "officer"), "value": 0.0})["value"] += r["value"]
        g["new"] = g["new"] or datetime.fromisoformat(r["accepted"]) >= since
        g["last_filed"] = max(g["last_filed"], r["filed"])
        g["latest"] = max(g["latest"], r["accepted"])
    keep = {}
    for s, g in out.items():
        top = max(b["value"] for b in g["buyers"].values())
        if g["new"] and (top >= INSIDER_ONE or (len(g["buyers"]) >= 2 and g["value"] >= INSIDER_GROUP)):
            g["cfo"] = any("financial" in b["title"].lower() or "cfo" in b["title"].lower() for b in g["buyers"].values())
            keep[s] = g
    return keep


def candidates(names: list[dict], px: pd.DataFrame, seen: date, reported: set[str], reports_tonight: set[str],
               insiders: dict[str, dict], skip: dict[str, datetime] | set[str], share: float = 1.0,
               events: dict[str, dict] | None = None) -> tuple[list[dict], dict]:
    """Every name that shows a setup on `seen`, in the screen's order, and the counts for the summary. `events` holds
    each name's filings since the last close (edgar.events)."""
    live, events = share < 1.0, events or {}
    by_symbol = {s: g for s, g in px.groupby("symbol")} if not px.empty else {}
    hedge = numbers(by_symbol[HEDGE], share) if HEDGE in by_symbol else None
    if not hedge or hedge["date"] != seen:
        raise RuntimeError(f"no {HEDGE} bar for {seen}: the daily bars aren't in yet")
    found, counts = [], {"stale": 0, "judged_recently": 0, "reports_tonight": 0, "back_on_new_filing": 0}
    for row in names:
        s = row["symbol"]
        n = numbers(by_symbol[s], share) if s in by_symbol else None
        if not n or n["date"] != seen or not n["atr_pct"] or not n["volume_ratio"]:
            counts["stale"] += 1
            continue
        b = beta(by_symbol[s], by_symbol[HEDGE])
        excess, own = n["ret"] - hedge["ret"], n["ret"] - b * hedge["ret"]
        n["excess_ranges"] = abs(own) / n["atr_pct"]
        ev = events.get(s) or {}
        matched: list[tuple[str, str, str | None]] = []   # (setup, why, when the information came out)
        if ev.get("filing"):
            what = "; ".join(sorted({f["what"] for f in ev["filing"]}))
            matched.append(("filing", f"Filed with the SEC since the last close: {what}. ", max(f["at"] for f in ev["filing"])))
        if s in insiders:
            g = insiders[s]
            buyers = "; ".join(f"{who} ({x['title']}) ${x['value']:,.0f}" for who, x in
                               sorted(g["buyers"].items(), key=lambda kv: -kv[1]["value"])[:4])
            matched.append(("insider_buying",
                            f"Officers or directors bought ${g['value']:,.0f} of stock in the open market over {INSIDER_SESSIONS} sessions "
                            f"({len(g['buyers'])} buyer{'s' if len(g['buyers']) != 1 else ''}: {buyers}; latest filing {g['last_filed']}"
                            f"{'; a CFO among them' if g.get('cfo') else ''}); the stock is {_pct(n['off_52w_high'])} from its 52-week high. ",
                            g.get("latest")))
        if s in reported:
            matched.append(("earnings_reaction", "Reported earnings since the last close. ", ev.get("earnings")))
        elif abs(own) >= NEWS_MOVE[0] and n["excess_ranges"] >= NEWS_MOVE[1] and n["volume_ratio"] >= NEWS_VOLUME:
            if s in reports_tonight:
                if not matched:
                    counts["reports_tonight"] += 1   # today's move is ahead of a report due after the close, not news
                    continue
            else:
                matched.append(("news_move", "No earnings report, but a large move on heavy volume. ", None))
        if not matched:
            continue
        matched.sort(key=lambda m: TIER[m[0]])
        setup = matched[0][0]
        when = max((m[2] for m in matched if m[2]), default=None)
        if s in skip:
            last = skip[s] if isinstance(skip, dict) else None
            if not (last and when and datetime.fromisoformat(when) > last):
                counts["judged_recently"] += 1
                continue
            counts["back_on_new_filing"] += 1
        old_bar = abs(excess) >= OLD_NEWS_BAR[0] and abs(excess) / n["atr_pct"] >= OLD_NEWS_BAR[1] and n["volume_ratio"] >= OLD_NEWS_BAR[2]
        found.append({"name": s, "company": row["name"], "setup": setup, "tier": TIER[setup], "also": [m[0] for m in matched[1:]],
                      "why": "".join(m[1] for m in matched) + _move_words(n, hedge["ret"], b, live),
                      "strength": round(n["excess_ranges"], 2), "news_since": when,
                      "facts": facts(n, setup, bool(ev.get("eight_k"))),
                      "numbers": {"close": n["close"], "ret_pct": round(n["ret"] * 100, 2), "vs_iwm_pct": round(excess * 100, 2),
                                  "beta": round(b, 2), "vs_beta_pct": round(own * 100, 2), "gap_pct": round(n["gap"] * 100, 2),
                                  "intraday_pct": round(n["intraday"] * 100, 2),
                                  "atr_pct": round(n["atr_pct"] * 100, 2), "volume_ratio": round(n["volume_ratio"], 2),
                                  "above_50d": n["above_50d"], "above_200d": n["above_200d"], "new_high_55": n["new_high_55"],
                                  "new_low_55": n["new_low_55"], "off_52w_high_pct": round(n["off_52w_high"] * 100, 1),
                                  "market_value": round(row["cap"]), "dollar_volume": row["dollar_volume"],
                                  "analysts": row.get("analysts"), "sector": row["sector"], "industry": row["industry"],
                                  "old_news_bar": old_bar, "eight_k_since_close": len(ev.get("eight_k") or []),
                                  **({"insider_value": round(insiders[s]["value"]), "insider_buyers": len(insiders[s]["buyers"])}
                                     if s in insiders else {})}})
    return shortlist(found, most=len(found)), counts


def shortlist(found: list[dict], most: int = MAX) -> list[dict]:
    """One list: tier A (filings, insider buying), then B (earnings), then C (price moves), each by the move's size."""
    return sorted(found, key=lambda c: (c.get("tier") or TIER.get(c["setup"], "C"), -c["strength"]))[:most]


def fill_from_quotes(symbols: list[str], seen: date) -> set[str]:
    """Adds the bar for `seen` from Yahoo's quote to each stored history that ends the session before. Returns the
    symbols it filled."""
    before, done = journal.previous_trading_day(seen), set()
    for i in range(0, len(symbols), 150):
        chunk = symbols[i:i + 150]
        quotes = bars._quotes([bars._ticker(s) for s in chunk])
        for s in chunk:
            q, path = quotes.get(bars._ticker(s)), bars.bars_path(s)
            bar = bars._quote_bar(s, q) if q else None
            stored = bars._read(path)
            if not bar or bar["date"].date() != seen or stored is None or stored.empty or stored["date"].iloc[-1].date() != before:
                continue
            if abs(bar["close"] / stored["close"].iloc[-1] - 1) >= bars.SUSPECT_MOVE:
                continue   # big enough to be a split: leave it to the full pull
            row = pd.DataFrame([bar])[stored.columns].astype(stored.dtypes.to_dict())
            bars._store(pd.concat([stored, row], ignore_index=True), path)
            done.add(s)
    return done


def swing_names() -> set[str]:
    """Symbols the swing book holds or is still trying to enter: the two books don't judge the same name."""
    try:
        from ..paper import book
        return {leg["symbol"] for t in book.trades() if t["state"] != "closed" for leg in t.get("legs") or t.get("held") or []}
    except Exception:
        return set()


def sec_events(names: list[dict], seen: date, now: datetime) -> tuple[dict, dict, list[str]]:
    """From EDGAR: each name's filings since the last session's close, the insider groups, and notes on what failed."""
    notes = []
    since = edgar.since_close(journal.previous_trading_day(seen))
    try:
        tickers = edgar.ciks()
    except Exception as e:
        return {}, {}, [f"SEC EDGAR didn't answer ({type(e).__name__}), so there are no filing candidates and insider buying comes from OpenInsider."]
    want = {r["symbol"]: r.get("cik") or tickers.get(r["symbol"].replace(".", "-")) for r in names}
    docs, failed = edgar.companies({s: c for s, c in want.items() if c})
    if missing := sorted(s for s, c in want.items() if not c):
        log.info("smallcap_no_cik symbols=%s", missing)
    if failed:
        notes.append(f"EDGAR didn't answer for {len(failed)} of {len(want)} names, so their filings weren't read: {', '.join(failed[:10])}.")
    events = {s: edgar.events(d, since) for s, d in docs.items()}
    start = seen
    for _ in range(INSIDER_SESSIONS - 1):
        start = journal.previous_trading_day(start)
    groups = insider_groups(edgar.purchases(docs, start), since)
    return events, groups, notes


def build(now: datetime | None = None, write: bool = True, pull: bool = True, slot: str = "night") -> dict:
    now = now or datetime.now(NY)
    seen, for_session = sessions(now, slot)
    live = slot != "night"
    share = day_share(now) if live else 1.0
    if pull and universe.stale(now):
        universe.build(now)
    doc = universe.load()
    if not doc or not doc["names"]:
        raise RuntimeError("the small-cap universe is empty; run `fr smallcap universe`")
    names = doc["names"]
    symbols = [r["symbol"] for r in names]
    notes = []
    if pull:
        # Yahoo's daily history can lack the day's prices for hours after the close on quiet names (half the
        # universe on 2026-11-19); its quote has them, so the day's bar is built from that.
        want = [*symbols, HEDGE]
        if live:   # today's bar so far, from Yahoo's quote; whatever it can't place gets the 5-day pull
            bars.collect_quotes(want)
        for _ in range(3):
            bars.collect_recent(want)
            px = bars.load(want)
            have = set(px[px["date"] == pd.Timestamp(seen)]["symbol"]) if not px.empty else set()
            want = [s for s in want if s not in have]
            if want:
                want = [s for s in want if s not in fill_from_quotes(want, seen)]
            if len(want) <= len(symbols) * 0.03:
                break
        if len(want) > len(symbols) * 0.10:
            notes.append(f"{len(want)} of {len(symbols)} names had no bar for {seen} after three pulls and Yahoo's quotes, so the screen is partial.")
    events, groups, sec_notes = sec_events(names, seen, now) if pull else ({}, {}, [])
    notes += sec_notes
    if pull and not events and not groups:
        try:   # EDGAR is down: OpenInsider's purchases, by the same rule
            rows = [{**r, "ten_pct_only": (r.get("title") or "").strip() in ("10%", ""),
                     "accepted": datetime.fromisoformat(r["filed"]).replace(hour=16, tzinfo=NY).isoformat()} for r in sources.insiders(7)]
            groups = insider_groups(rows, edgar.since_close(journal.previous_trading_day(seen)))
        except Exception as e:
            notes.append(f"OpenInsider didn't load either ({type(e).__name__}), so there are no insider-buying candidates.")
    try:
        today, before = sources.earnings(seen), sources.earnings(journal.previous_trading_day(seen))
        reported = {s for s, when in today.items() if when != "after"} | {s for s, when in before.items() if when == "after"}
        tonight = {s for s, when in today.items() if when == "after"}
    except Exception as e:
        reported, tonight = set(), set()
        notes.append(f"Nasdaq's earnings calendar didn't load ({type(e).__name__}); reports come from earnings 8-Ks alone.")
    filed = {s for s, ev in events.items() if ev.get("earnings")}   # an earnings 8-K since the last close: the report is out
    reported, tonight = reported | filed, tonight - filed
    held = swing_names()
    ranked, counts = candidates(names, bars.load([*symbols, HEDGE]), seen, reported, tonight, groups,
                                judged_recently(for_session, key(for_session.isoformat(), slot)), share, events)
    counts["in_swing_book"] = sum(1 for c in ranked if c["name"] in held)
    ranked = [c for c in ranked if c["name"] not in held]
    top = ranked[:MAX]
    shown = top[:]
    random.Random(f"{for_session}-{slot}").shuffle(shown)   # the judge sees them in no particular order
    out = {"session": for_session.isoformat(), "pass": slot, "prices_from": seen.isoformat(), "built": now.isoformat(timespec="seconds"),
           "live": live, "volume_share": round(share, 3), "screen_version": "2026-11-20 filings-first",
           "universe": len(names), "found": len(ranked), "counts": counts, "notes": notes,
           "routes": {setup: sum(1 for c in top if c["setup"] == setup) for setup in TIER},
           "screen_order": [c["name"] for c in top], "candidates": shown,
           "left_out": [{"name": c["name"], "setup": c["setup"], "strength": c["strength"]} for c in ranked[MAX:]]}
    if write:
        SCREENS.mkdir(parents=True, exist_ok=True)
        (SCREENS / f"{key(for_session.isoformat(), slot)}.json").write_text(json.dumps(out, indent=1) + "\n")
    return out


def load(session: str, slot: str = "night") -> dict | None:
    path = SCREENS / f"{key(session, slot)}.json"
    return json.loads(path.read_text()) if path.exists() else None


SETUP_WORDS = {"filing": "a filing with the SEC", "insider_buying": "insider buying", "earnings_reaction": "an earnings report",
               "news_move": "a large move on heavy volume with no report"}


def summary(doc: dict) -> str:
    when = (f"on the session so far ({doc.get('volume_share', 1) * 100:.0f}% of a usual day's volume has traded)" if doc.get("live")
            else f"on {doc['prices_from']} closing prices")
    c = doc["counts"]
    lines = [f"Small-cap {doc.get('pass', 'night')} screen for the {doc['session']} session, {when}: {len(doc['candidates'])} "
             f"candidates from {doc['found']} names with a setup, in a universe of {doc['universe']}. They are listed in no particular order.",
             f"Not shown: {c['judged_recently']} judged in the last {JUDGED_SESSIONS} sessions with nothing new since, "
             f"{c['reports_tonight']} moving ahead of a report due after today's close, "
             f"{c.get('in_swing_book', 0)} the swing book holds or is entering, {c['stale']} with no bar for the day"
             + (f". Back despite a recent verdict, because something new was filed: {c['back_on_new_filing']}" if c.get("back_on_new_filing") else "")
             + ".", *doc["notes"], ""]
    for c in doc["candidates"]:
        n = c["numbers"]
        cover = "analyst count unknown" if n["analysts"] is None else f"{n['analysts']} analyst{'s' if n['analysts'] != 1 else ''}"
        also = f" (also {', '.join(SETUP_WORDS.get(a, a) for a in c.get('also') or [])})" if c.get("also") else ""
        lines += [f"- {c['name']} ({c['company']}): {SETUP_WORDS.get(c['setup'], c['setup'])}{also}",
                  f"   {c['why']}.",
                  f"   {n['sector']}, {n['industry']}; worth ${n['market_value'] / 1e6:,.0f}M; trades ${n['dollar_volume'] / 1e6:,.1f}M a day; {cover}."]
        if c.get("facts"):
            lines.append("   Base rates from this universe's history: " + " ".join(c["facts"]))
    return "\n".join(lines)

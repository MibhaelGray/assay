"""Keeps the small-cap pass's verdicts: `fr smallcap record <draft.json>`.

The draft judges every candidate of the session's screen and nothing else:

    {"session": "2026-11-20", "market": "a paragraph on the tape",
     "verdicts": [{"name": "ABCD", "verdict": "take" | "pass", "direction": "long" | "short", "conviction": 1-5,
                   "rank": 1, "thesis": "...", "dilution": "cash runway, share-sale programs, warrants",
                   "plan": {"horizon_days": 1-5, "stop_pct": 2-15}, "wrong_if": "..."}]}

Every verdict carries a direction (which way the pass would trade it if it had to), a conviction, a rank (1 is
best, no ties) and a dilution note; a take also carries a plan and a `wrong_if`. A shadow run: nothing recorded
here is traded.

When it is entered (the owner, 2026-11-20: like the swing book, regular hours only): a morning or midday verdict is
entered at the live price when it is recorded, and the draft says `"pass": "morning"` or `"midday"`. Each name and IWM
are priced from the paper gateway's bid and ask, a long at the ask and a short at the bid, so the spread is paid as a
real order would pay it; Yahoo's last price stands in when the gateway has no book. A night verdict (`"pass": "night"`,
the default) is entered at the next session's open and must be recorded before it.
"""

import json
from datetime import date, datetime, time
from pathlib import Path

from ..config import NY
from . import screen

MAX_TAKES = 5
NEWS = ("new", "stale", "none")   # behind a price move: fresh information, old or recycled, or nothing found
HORIZON = (1, 5)        # sessions, counting the first
STOP = (2.0, 15.0)      # percent against the entry, on the stock itself
DEFAULT_HORIZON = 3     # a pass is scored at this hold beside the takes' own plans
OPEN = time(9, 30)
GATEWAY_CLIENT = 94   # the entry prices' own gateway connection


def _gateway(symbols: list[str]) -> tuple[dict, dict]:
    import asyncio
    from ..paper.gateway import IBMarket, QUOTE_TRIES

    async def ask():
        async with IBMarket(client_id=GATEWAY_CLIENT) as m:
            await m.quotes(symbols, False, tries=QUOTE_TRIES, need_books=True)
            return m.books, m.missing
    try:
        return asyncio.run(ask())
    except Exception as e:  # noqa: BLE001 - no gateway means Yahoo's prices, not a failed record
        return {}, {s: f"the gateway couldn't be asked ({type(e).__name__})" for s in symbols}


def entries(verdicts: list[dict], now: datetime, books=None, yahoo=None) -> dict[str, dict]:
    """Each verdict's entry: its own price and IWM's, the side it would trade at, and where the prices came from."""
    from .. import bars
    from . import HEDGE

    names = sorted({v["name"] for v in verdicts} | {HEDGE})
    got, missing = (books or _gateway)(names)
    quotes = (yahoo or (lambda xs: {s: q.get("regularMarketPrice") for s, q in bars._quotes(xs).items()}))(
        [s for s in names if s not in got or not got[s] or not all(got[s])])

    def price(symbol: str, side: str) -> tuple[float | None, str, float | None]:
        book_ = got.get(symbol)
        if book_ and all(book_) and book_[1] >= book_[0] > 0:
            bid, ask = book_
            return (ask if side == "long" else bid), "gateway", round((ask - bid) / ((ask + bid) / 2) * 100, 3)
        q = quotes.get(symbol)
        return (float(q) if q else None), "yahoo", None

    out = {}
    for v in verdicts:
        hedge_side = "short" if v["direction"] == "long" else "long"
        own, src, spread = price(v["name"], v["direction"])
        hedge, hsrc, _ = price(HEDGE, hedge_side)
        out[v["name"]] = {"at": now.isoformat(timespec="seconds"), "price": own, "hedge_price": hedge, "source": src,
                          "hedge_source": hsrc, "spread_pct": spread, "why_no_book": missing.get(v["name"]) if src != "gateway" else None}
    return out


def _text(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def check(draft: dict, shown: dict) -> list[dict]:
    """The draft's verdicts, checked against the screen. Raises ValueError naming everything wrong at once."""
    wrong = []
    verdicts = draft.get("verdicts")
    if not isinstance(verdicts, list) or not verdicts:
        raise ValueError("the draft has no verdicts")
    want = [c["name"] for c in shown["candidates"]]
    got = [v.get("name") for v in verdicts if isinstance(v, dict)]
    if missing := [n for n in want if n not in got]:
        wrong.append(f"no verdict for: {', '.join(missing)}")
    if extra := [str(n) for n in got if n not in want]:
        wrong.append(f"not on tonight's screen: {', '.join(extra)}")
    if len(set(got)) != len(got):
        wrong.append("a name is judged twice")
    ranks = [v.get("rank") for v in verdicts if isinstance(v, dict)]
    if sorted(r for r in ranks if isinstance(r, int)) != list(range(1, len(verdicts) + 1)):
        wrong.append(f"ranks must be 1 to {len(verdicts)}, each used once")
    setups = {c["name"]: c["setup"] for c in shown["candidates"]}
    out = []
    for v in verdicts:
        if not isinstance(v, dict):
            wrong.append("a verdict is not an object")
            continue
        who = v.get("name")
        if v.get("verdict") not in ("take", "pass"):
            wrong.append(f"{who}: verdict must be take or pass")
        if v.get("direction") not in ("long", "short"):
            wrong.append(f"{who}: direction must be long or short, on a pass too")
        if not isinstance(v.get("conviction"), int) or not 1 <= v["conviction"] <= 5:
            wrong.append(f"{who}: conviction must be a whole number from 1 to 5")
        for field in ("thesis", "dilution"):
            if not _text(v.get(field)):
                wrong.append(f"{who}: {field} is missing")
        if setups.get(who) == "news_move" and v.get("news") not in NEWS:
            wrong.append(f"{who}: a price-move candidate needs news: new, stale or none (what the research found behind the move)")
        row = {k: v.get(k) for k in ("name", "verdict", "direction", "conviction", "rank", "thesis", "dilution")}
        if v.get("news") in NEWS:
            row["news"] = v["news"]
        if v.get("verdict") == "take":
            plan = v.get("plan") or {}
            h, stop = plan.get("horizon_days"), plan.get("stop_pct")
            if not isinstance(h, int) or not HORIZON[0] <= h <= HORIZON[1]:
                wrong.append(f"{who}: a take's plan.horizon_days must be {HORIZON[0]} to {HORIZON[1]}")
            if not isinstance(stop, (int, float)) or not STOP[0] <= abs(stop) <= STOP[1]:
                wrong.append(f"{who}: a take's plan.stop_pct must be {STOP[0]:g} to {STOP[1]:g}")
            if not _text(v.get("wrong_if")):
                wrong.append(f"{who}: a take needs wrong_if")
            row |= {"plan": {"horizon_days": h, "stop_pct": abs(stop) if isinstance(stop, (int, float)) else stop},
                    "wrong_if": v.get("wrong_if")}
        out.append(row)
    if sum(1 for v in out if v["verdict"] == "take") > MAX_TAKES:
        wrong.append(f"more than {MAX_TAKES} takes; take the best few")
    if wrong:
        raise ValueError("; ".join(wrong))
    return sorted(out, key=lambda v: v["rank"])


def record(path: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(NY)
    draft = json.loads(Path(path).read_text())
    session, slot = draft.get("session"), draft.get("pass") or "night"
    if slot not in screen.PASSES:
        raise ValueError(f"pass must be one of: {', '.join(screen.PASSES)}")
    shown = screen.load(session, slot) if _text(session) else None
    if not shown:
        raise ValueError(f"no small-cap {slot} screen for session {session!r}; run `uv run fr smallcap screen --pass {slot}` first")
    if slot == "night" and now >= datetime.combine(date.fromisoformat(session), OPEN, NY):
        raise ValueError(f"the {session} session has opened; night verdicts are scored from its open and must be recorded before it")
    if slot != "night":
        try:
            today, _ = screen.sessions(now, slot)
        except ValueError as e:
            raise ValueError(f"{e}; a {slot} verdict is entered at the live price, so it can't be recorded now") from None
        if today.isoformat() != session:
            raise ValueError(f"the {slot} screen is for {session}, not today's session")
    verdicts = check(draft, shown)
    priced = entries(verdicts, now) if slot != "night" else {}
    if slot != "night" and (bad := [n for n, e in priced.items() if not e["price"] or not e["hedge_price"]]):
        raise ValueError(f"no live price for {', '.join(bad)} or IWM from the gateway or Yahoo; try again in a minute")
    by_name = {c["name"]: c for c in shown["candidates"]}
    order = {n: i for i, n in enumerate(shown.get("screen_order") or [], 1)}   # the screen's own rank, scored apart from the judge's
    doc = {"session": session, "pass": slot, "prices_from": shown["prices_from"], "recorded": now.isoformat(timespec="seconds"),
           "market": draft.get("market") if _text(draft.get("market")) else None, "shadow": True,
           "screen_version": shown.get("screen_version", "2026-11-19 price-first"),
           "verdicts": [{**v, "company": by_name[v["name"]]["company"], "setup": by_name[v["name"]]["setup"],
                         "why": by_name[v["name"]]["why"], "numbers": by_name[v["name"]]["numbers"],
                         **({k: by_name[v["name"]][k] for k in ("tier", "also", "facts", "news_since") if k in by_name[v["name"]]}),
                         **({"screen_rank": order[v["name"]]} if v["name"] in order else {}),
                         **({"entry": priced[v["name"]]} if v["name"] in priced else {})} for v in verdicts]}
    screen.VERDICTS.mkdir(parents=True, exist_ok=True)
    (screen.VERDICTS / f"{screen.key(session, slot)}.json").write_text(json.dumps(doc, indent=1) + "\n")
    return doc


def summary(doc: dict) -> str:
    takes = [v for v in doc["verdicts"] if v["verdict"] == "take"]
    lines = [f"Recorded {len(doc['verdicts'])} small-cap {doc.get('pass', 'night')} verdicts for the {doc['session']} session: {len(takes)} takes, "
             f"{len(doc['verdicts']) - len(takes)} passes. A shadow run: nothing is traded."]
    lines += [f"  take #{v['rank']}: {v['direction']} {v['name']}, conviction {v['conviction']}, {v['plan']['horizon_days']} sessions, "
              f"stop {v['plan']['stop_pct']:g}%" + (f", entered at {v['entry']['price']:.2f} ({v['entry']['source']})" if v.get("entry") else "")
              for v in takes]
    return "\n".join(lines)

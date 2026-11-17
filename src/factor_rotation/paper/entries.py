"""The three-pass book's orders: `fr paper enter`, run every minute by launchd.

A pass (night, morning or midday) records its verdicts with `fr paper propose`. This job notices, usually within
a minute, and trades them: the takes go in, and the open trades the pass said to exit come out. The pass never
sends an order itself; this is the only place its decisions become orders. Stops and time limits stay with the
3:45 PM ET close job (book.close_session).

    journal/paper/entries/<session>.json
        trades   every trade made from the session's takes, written before its first order goes out
        marks    per pass: the latest price of every leg of every verdict, taken when the pass is first handled,
                 so a take and a pass are scored from the same moment (learn.py)
        exits    trades closed because a pass said so
        runs     each run that did something: orders, quotes, what waited and why

Every order needs the gateway's own bid and ask, in every session, regular hours included (the owner's rule,
2026-11-15; until then regular-hours orders were market and auction orders with no quote). A buy is priced at
the ask and a sell at the bid, with the limit LIMIT_SLIP beyond so it fills at once. A leg with no quote, or a
spread over rules.MAX_CROSS_SPREAD, isn't sent; while nothing of its trade has filled the whole trade waits, so
a pair never goes in one side at a time. Whatever is left is tried again every RETRY until the session's entry
window ends (ENTRY_END), in whichever session is trading:
  overnight    8:00 PM-3:50 AM ET  IBKR's OVERNIGHT venue, Sunday to Thursday nights
  pre-market   4:00-9:10 AM ET
  regular      9:30 AM-3:40 PM ET
  after-hours  4:00-8:00 PM ET     (for the next session's night pass)
The one exception to the quote rule is the close job's exits for stops and time limits, which go into the closing
auction whatever the gateway says: a trade past its stop must not be left open for want of a quote.

An exit a pass asks for is sent here in the regular session only, with a quote like any other order. If it can't
be sent, or doesn't fill, the close job sends it into the closing auction. A take a pass stands down before any
of it has filled is cancelled and never enters.

A trade entered by the night pass counts its sessions from the session it was entered for. One entered by the
morning or midday pass is already hours into its session, so its time limit counts from the next session; its
stop is still checked at its first close.
"""

import asyncio
import logging
from datetime import date, datetime, time, timedelta

from .. import drawdown, journal, live
from ..config import CT, NY
from . import book, early, proposals, rules

log = logging.getLogger(__name__)

ENTRY_END = time(15, 40)          # New York: the last 20 minutes belong to the close job
RETRY = timedelta(minutes=15)     # between tries for what hasn't filled
PASS_MAX_NEW = 3                  # takes entered per pass: the best one to three (the owner, 2026-11-15)
DATA_CLIENT_ID = 77               # the gateway quotes' own connection
SETTLE = 3                        # seconds after sending before the fills are pulled


def venue(now: datetime) -> str | None:
    """Which session an order can trade in at `now` (New York), or None between sessions."""
    if journal.is_trading_day(now.date()) and time(9, 30) <= now.time() < time(16, 0):
        return "regular" if now.time() < ENTRY_END else None
    return early.venue(now)


def entry_session(now: datetime) -> date:
    """The session whose entries are going out at `now`: today until its close, then the next one."""
    today = now.date()
    return today if journal.is_trading_day(today) and now.time() < time(16, 0) else journal.next_trading_day(today)


def first_session(kind: str, session: date) -> date:
    """The first session a trade's time limit counts."""
    return session if kind == "night" else journal.next_trading_day(session)


def path(session: date):
    return book.ROOT / "entries" / f"{session}.json"


def load(session: date) -> dict | None:
    return book._read(path(session))


def _blank(session: date) -> dict:
    return {"session": session.isoformat(), "handled": [], "trades": [], "marks": {}, "exits": [], "runs": [], "last_attempt": None}


def marks(session: date | str, kind: str) -> dict | None:
    """{"at", "prices"} taken when the session's `kind` pass was first handled, or None."""
    doc = load(date.fromisoformat(str(session)))
    return (doc or {}).get("marks", {}).get(kind)


def waiting(session: date) -> list[dict]:
    """The session's trades still trying to enter: not stood down, and with shares left to fill."""
    out = []
    for t in (load(session) or {}).get("trades", []):
        got = early.entered(t)
        if not t.get("stood_down") and any(leg["shares"] > got[leg["symbol"]] for leg in t["legs"]):
            out.append({**t, "filled": got})
    return out


async def gateway_books(symbols: list[str], overnight: bool) -> tuple[dict[str, tuple[float, float]], dict[str, str]]:
    """(symbol -> bid, ask; symbol -> why it has none) from the paper gateway in the session that is trading."""
    from ..intraday.extended import IBMarket
    try:
        async with IBMarket(client_id=DATA_CLIENT_ID) as m:
            await m.quotes(symbols, overnight, tries=early.QUOTE_TRIES, need_books=True)
            return m.books, m.missing
    except Exception as e:  # noqa: BLE001 - no quotes means no orders at this run, not a failed run
        log.warning("entries_gateway_quotes_failed %r", e)
        why = f"the gateway couldn't be asked for quotes ({type(e).__name__}{': ' + str(e) if str(e) else ''})"
        return {}, {s: why for s in symbols}


def _order(trade_id: str, symbol: str, action: str, shares: int, book_: tuple[float, float], where: str, now: datetime,
           session: date) -> dict:
    px = early.crossing(action, book_)
    o = {"trade_id": trade_id, "symbol": symbol, "action": action, "shares": shares, "kind": "LMT",
         "limit": early.limit_price(action, px), "venue": where, "ref_price": round(px, 4), "priced_on": "gateway bid/ask",
         **early.book_fields(book_)}
    if where == "extended" and now.time() < early.PREMARKET_END:
        o["good_till"] = f"{session:%Y%m%d} {early.GOOD_TILL} US/Eastern"
    return o


STALE_SESSION = "in use somewhere else"   # the gateway's words for IBKR error 10197 (intraday/extended.py)
HEAL_EVERY = timedelta(minutes=60)


def heal_gateway(missing: dict[str, str], now: datetime | None = None) -> str | None:
    """Restart the paper gateway when it refuses every quote with error 10197 ("competing live session").

    On 2026-11-16 the gateway gave that answer for ten hours, from just after IBKR's nightly reset, with the live
    login in use nowhere; restarting the container cleared it in ten seconds. A real competing login isn't cured
    by a restart, so this tries at most once an hour and says so each time. Returns what it did, or None."""
    import os
    import shutil
    import subprocess

    if not missing or not all(STALE_SESSION in (why or "") for why in missing.values()):
        return None
    now = now or datetime.now(NY)
    stamp = book.ROOT / "gateway_restart.json"
    last = (book._read(stamp) or {}).get("at")
    if last and now - datetime.fromisoformat(last) < HEAL_EVERY:
        return None
    docker = shutil.which("docker") or next((p for p in ("/usr/local/bin/docker", "/opt/homebrew/bin/docker",
                                                         os.path.expanduser("~/.docker/bin/docker"),
                                                         "/Applications/Docker.app/Contents/Resources/bin/docker") if os.path.exists(p)), None)
    name = os.environ.get("FR_PAPER_GATEWAY_CONTAINER", "deploy-ib-gateway-paper-1")
    if not docker:
        return "the gateway refused every quote with a competing-session error, and docker wasn't found to restart it"
    book._write(stamp, {"at": now.isoformat(timespec="seconds"), "why": "every quote refused with IBKR error 10197"})
    done = subprocess.run([docker, "restart", name], capture_output=True, text=True, timeout=120)
    return (f"the gateway refused every quote with a competing-session error (IBKR 10197), so the paper gateway was restarted"
            if done.returncode == 0 else f"tried to restart the paper gateway after a competing-session error, but docker said: {done.stderr.strip()[:200]}")


def _why_blocked(symbols: list[str], books: dict, missing: dict[str, str]) -> str:
    parts = []
    wide = [f"{x} {rules.spread_pct(books[x]):.1%}" for x in symbols if x not in missing]
    if wide:
        parts.append(f"spread wider than {rules.MAX_CROSS_SPREAD:.0%} ({', '.join(wide)})")
    reasons: dict[str, list[str]] = {}
    for x in symbols:
        if x in missing:
            reasons.setdefault(missing[x], []).append(x)
    parts += [f"no quote for {' or '.join(names)} from the gateway because {reason}" for reason, names in reasons.items()]
    return "; ".join(parts)


async def run(now: datetime | None = None, broker=None, dry: bool = False, quotes=None, prices=None) -> dict:
    """One run. Returns the run's record; a run that did something is also kept in the session's file.
    `dry` is a run by hand: it works out the trades and orders and prints them, and writes and sends nothing.
    `quotes` stands in for `gateway_books` and `prices` for `live.prices` in tests."""
    now = now or datetime.now(NY)
    session = entry_session(now)
    where = venue(now)
    s = book.PaperSettings()
    dry = dry or (broker is None and not s.send)
    record = {"at": now.isoformat(timespec="seconds"), "venue": where, "dry_run": dry, "passes": [], "new": [], "stood_down": [],
              "exits": [], "cancelled": 0, "orders": [], "skipped": [], "warnings": []}
    state = load(session) or _blank(session)
    doc = proposals.load(session)
    recorded = [p["pass"] for p in (doc or {}).get("passes", []) if p["pass"] in proposals.NEW_PASSES]
    fresh_passes = [k for k in recorded if k not in state["handled"]]
    reviews = proposals.reviews(session)

    def left_of(trades: list[dict]) -> list[tuple[dict, dict, int]]:
        out = []
        for t in trades:
            got = early.entered(t)
            out += [(t, leg, leg["shares"] - got[leg["symbol"]]) for leg in t["legs"] if leg["shares"] > got[leg["symbol"]]]
        return out

    active = [t for t in state["trades"] if not t.get("stood_down")]
    exiting = {e["trade_id"] for e in state["exits"]}
    wants_out = [t for t in book.open_trades() if (reviews.get(t["id"]) or {}).get("action") == "exit" and t["id"] not in exiting]
    last = datetime.fromisoformat(state["last_attempt"]) if state["last_attempt"] else None
    retry_due = last is None or now - last >= RETRY
    work = bool(left_of(active)) or bool(wants_out)
    if not fresh_passes and not (work and retry_due and where):
        record["note"] = ("nothing recorded for " + session.isoformat() if not recorded and not state["trades"] else
                          "nothing new" if not work else f"waiting: the next try is {RETRY.seconds // 60} minutes after the last"
                          if where else "between sessions; what is left waits")
        return record

    # 1. A pass seen for the first time: mark every verdict's legs at the latest price, so takes and passes are
    #    scored from the same moment.
    for kind in fresh_passes:
        props = [p for p in doc["proposals"] if p.get("pass") == kind]
        symbols = sorted({x for p in props for x in [*p["long"], *p["short"]]} | {"SPY"})
        px = await asyncio.to_thread(prices or live.prices, symbols)
        state["marks"][kind] = {"at": now.isoformat(timespec="seconds"), "source": "Yahoo, latest price in any session",
                                "prices": {k: round(v, 4) for k, v in px.items()}}
        if lost := sorted(set(symbols) - set(px)):
            record["warnings"].append(f"The {kind} pass has no price at the verdict for {', '.join(lost)}, so ideas with those legs can't be scored.")
        record["passes"].append(kind)

    # 2. A take a pass has since stood down, before any of it filled: it never enters.
    for t in active:
        r = reviews.get(t["id"]) or {}
        if r.get("action") == "exit" and not any(early.entered(t).values()):
            t["stood_down"] = r["reason"]
            record["stood_down"].append(t["id"])
    active = [t for t in state["trades"] if not t.get("stood_down")]

    # 3. The new passes' takes become trades, sized now, inside the same limits as ever.
    blocked_why = (f"halted: {book.HALT.read_text().strip() or 'journal/paper/HALT exists'}" if book.HALT.exists()
                   else (why := drawdown.paused("swing")) and f"paused: {why}")
    mine = {t["id"] for t in state["trades"]}
    held = [t for t in book.open_trades() if t["id"] not in mine]
    for kind in fresh_passes:
        takes = [p for p in doc["proposals"] if p.get("pass") == kind and p["decision"] == "take"]
        if takes and blocked_why:
            record["skipped"] += [{"name": p["id"], "reason": blocked_why} for p in takes]
        elif takes:
            used = {x for t in [*held, *active] for x in rules.core_symbols(t)}
            made, skipped = rules.proposal_trades(takes, session.isoformat(), state["marks"][kind]["prices"], used,
                                                  start=len(state["trades"]) + 1, room=PASS_MAX_NEW)
            for t in made:   # S, not the first book's A: the gateway still reports that book's fills under its ids
                t["id"] = t["id"].replace(f"{session.isoformat()}-A", f"{session.isoformat()}-S", 1)
            kept, capped = rules.apply_caps(made, [*held, *active], max_gross=book.capital())
            for t in kept:
                t.update({"pass": kind, "first_session": first_session(kind, session).isoformat()})
            state["trades"] += kept
            active += kept
            record["new"] += [t["id"] for t in kept]
            record["skipped"] += [*skipped, *capped]
        state["handled"].append(kind)

    left = left_of(active)
    send_exits = wants_out if where == "regular" else []
    if where and (left or send_exits or record["stood_down"]):
        async with (broker or book._broker(dry=dry)) as b:
            record["cancelled"] = await b.cancel({t["id"] for t in state["trades"]})
            if not dry:
                await asyncio.sleep(1)
                await book._pull(b, session)
            left = left_of(active)
            symbols = sorted({leg["symbol"] for _, leg, _ in left} | {h["symbol"] for t in send_exits for h in t["held"]})
            books, missing = await (quotes or gateway_books)(symbols, where == "overnight") if symbols else ({}, {})
            missing = {x: missing.get(x) or "the gateway gave no bid and ask" for x in symbols if rules.spread_pct(books.get(x)) is None}
            if missing:
                record["no_quote"] = missing
            usable = lambda x: x not in missing and rules.crossable(books[x])  # noqa: E731
            orders = []
            # Entries. If nothing of a trade has filled, the whole trade waits on one bad leg.
            for t in active:
                mine_left = [(leg, n) for u, leg, n in left if u["id"] == t["id"]]
                bad = [leg["symbol"] for leg, _ in mine_left if not usable(leg["symbol"])]
                none_in = not any(early.entered(t).values())
                if bad:
                    waits = "the whole trade waits" if none_in else "that leg waits" if len(bad) == 1 else "those legs wait"
                    record["warnings"].append(f"{t['id']} {t['name']}: {_why_blocked(bad, books, missing)}, so {waits} for the next try.")
                if bad and none_in:
                    continue
                orders += [_order(t["id"], leg["symbol"], "BUY" if leg["side"] == "long" else "SELL", n, books[leg["symbol"]], where, now, session)
                           for leg, n in mine_left if leg["symbol"] not in bad]
            # Exits a pass asked for: all of a trade's legs or none, so a pair doesn't come out one side at a time.
            for t in send_exits:
                legs = [h for h in t["held"] if h["shares"] > h["exit_shares"]]
                bad = [h["symbol"] for h in legs if not usable(h["symbol"])]
                if bad:
                    record["warnings"].append(f"{t['id']} {t['name']}: the exit a pass asked for wasn't sent ({_why_blocked(bad, books, missing)}); "
                                              "the 2:45 PM CT close job sends it into the closing auction.")
                    continue
                r = reviews[t["id"]]
                record["exits"].append({"trade_id": t["id"], "book": t["book"], "session_number": book._session(t, session), "intraday": True,
                                        "at": f"{now.astimezone(CT):%-I:%M %p} CT", "pass": r.get("pass"),
                                        "reason": f"review: {r['reason']}"})
                orders += [_order(t["id"], h["symbol"], "SELL" if h["side"] == "long" else "BUY", h["shares"] - h["exit_shares"],
                                  books[h["symbol"]], where, now, session) for h in legs]
            state["exits"] += record["exits"]
            if orders:
                if not dry:  # on disk before anything goes
                    state["last_attempt"] = now.isoformat(timespec="seconds")
                    _save(session, state, {**record, "orders": orders, "note": "sending"})
                record["orders"] = await b.place(orders)
                record["warnings"] += book._order_warnings(record["orders"])
                if not dry:  # an order priced to cross fills at once, so the books aren't left behind
                    await asyncio.sleep(SETTLE)
                    await book._pull(b, session)
    elif left and not where:
        record["warnings"].append(f"{len({t['id'] for t, _, _ in left})} trade(s) are waiting to enter; no session is trading at "
                                  f"{now.astimezone(CT):%-I:%M %p} CT.")
    state["last_attempt"] = now.isoformat(timespec="seconds")
    if not dry:
        _save(session, state, record)
    return record


def _save(session: date, state: dict, record: dict) -> None:
    """Keep the state, and the run if it did something worth reading back."""
    did = any(record[k] for k in ("passes", "new", "stood_down", "exits", "orders", "skipped", "cancelled")) or record.get("no_quote")
    runs = [r for r in state["runs"] if r["at"] != record["at"]]
    if not did and runs and runs[-1].get("warnings") == record["warnings"]:
        runs[-1]["repeated_until"] = record["at"]   # a wait that hasn't changed: one line, not one a quarter-hour
    elif did or record["warnings"]:
        runs.append(record)
    book._write(path(session), {**state, "runs": runs})

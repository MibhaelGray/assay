"""The three-pass book's orders: `fr paper enter`, run every minute by launchd.

A pass (night, morning or midday) records its verdicts with `fr paper propose`. This job notices, usually within
a minute, and trades them: the takes go in, and the open trades the pass said to exit come out. The pass never
sends an order itself; this is the only place its decisions become orders. In the regular session it also checks
every open trade against its stop every STOP_EVERY (the owner, 2026-11-18) and takes a trade past it out at once,
with a quote like any other order; until then stops were checked only at the close. Time limits, and any stop exit
that couldn't be sent during the day, stay with the 3:45 PM ET close job (book.close_session).

    journal/paper/entries/<session>.json
        trades   every trade made from the session's takes, written before its first order goes out
        marks    per pass: the latest price of every leg of every verdict, taken when the pass is first handled,
                 so a take and a pass are scored from the same moment (learn.py)
        exits    trades closed during the day, because a pass said so or because they were past their stop
        last_stop_check  when the regular session last checked the stops
        runs     each run that did something: orders, quotes, what waited and why

Every order needs the gateway's own bid and ask, in every session, regular hours included (the owner's rule,
2026-11-15; until then regular-hours orders were market and auction orders with no quote). A buy is priced at
the ask and a sell at the bid, with the limit LIMIT_SLIP beyond so it fills at once. A trade goes in at once
or not at all (the owner, 2026-11-18: the book only makes sense with instant fills): if any leg's spread is over
rules.MAX_CROSS_SPREAD before anything of it has filled, the trade is passed on and never enters. Overnight, a
leg the gateway sends no bid and ask for while it quotes SPY has no overnight market, and that is passed on the
same way (the owner, 2026-11-18: kill it, don't wait). Any other leg with no quote is a gateway fault (a login
elsewhere, the gateway down), not the market, so that trade still waits for the next try; nothing of a pair
goes in one side at a time. An order hits the bid or the ask and that is all (the owner, 2026-11-16):
whatever it didn't fill at once is cancelled before the run ends, so nothing is ever left resting in the book.
(IBKR's overnight venue takes only limit orders, so the order is a limit at the bid or ask; cancelling the
rest makes it a hit and nothing more.) Whatever is left is tried again every RETRY, or every FOLLOW while a
trade is part of the way in, until the session's entry window ends (entry_end: 20 minutes before the close), in whichever session is trading:
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

from .. import bars, drawdown, journal, live
from ..config import CT, NY
from . import book, proposals, rules
from .gateway import CANARY, gateway_books, no_market

log = logging.getLogger(__name__)

ENTRY_GAP = timedelta(minutes=20)  # the last 20 minutes of the session belong to the close job
RETRY = timedelta(minutes=15)     # between tries for what hasn't filled
FOLLOW = timedelta(seconds=50)    # ... and every run (a minute) while a trade is part of the way in: it hits again for the rest
PASS_MAX_NEW: int | None = None   # takes entered per pass: every one (the owner, 2026-11-16; three before that).
                                  # The book's own limits still hold: its capital, 60 open trades, 10% in one symbol
LIMIT_SLIP = 0.005                # how far past the bid or ask the limit sits, so a small move still fills
PREMARKET_END = time(9, 10)       # New York: the last pre-market order, good until GOOD_TILL so it never meets the open
GOOD_TILL = "09:25:00"
SETTLE = 3                        # seconds after sending before the fills are pulled
STOP_EVERY = timedelta(minutes=10)  # how often the regular session checks open trades against their stops (2 minutes until 2026-11-22)
NO_FILL_TRIES = 3                 # tries that hit the bid or ask and filled nothing before a trade is passed on (the owner,
                                  # 2026-11-22: a quote with nothing behind it, as XLU's was overnight, isn't chased)
STOP_SLACK = timedelta(seconds=15) # launchd's minute is not exact; a check due in a few seconds counts as due


def venue(now: datetime) -> str | None:
    """Which session an order can trade in at `now` (New York), or None between sessions."""
    t, wd, today = now.time(), now.weekday(), now.date()
    trading = journal.is_trading_day(today)
    close = journal.close_time(today)
    if trading and time(9, 30) <= t < close:
        return "regular" if t < entry_end(today) else None
    after_end = time(20, 0) if close == time(16, 0) else time(close.hour + 4, close.minute)   # a half day's after-hours ends at 5:00 PM
    if trading and (close <= t < after_end or time(4, 0) <= t < PREMARKET_END):
        return "extended"       # after-hours and pre-market, on SMART
    if (t >= time(20, 0) and wd in (6, 0, 1, 2, 3)) or (t < time(3, 50) and wd in (0, 1, 2, 3, 4)):
        return "overnight"      # IBKR's OVERNIGHT venue, Sunday to Thursday nights
    return None


def entry_end(day: date) -> time:
    """The last moment of the regular session an entry may go out: 20 minutes before the close (3:40 PM ET, or
    12:40 PM ET on a half day)."""
    return (datetime.combine(day, journal.close_time(day)) - ENTRY_GAP).time()


def books_running() -> tuple[str, ...]:
    """The books that trade, the swing book first."""
    from .. import portfolio
    return tuple(sorted(portfolio.running(), key=lambda b: b != book.MAIN))


def make_trades(which: str, kind: str, takes: list[dict], session: date, state: dict, others: list[dict], volumes, record: dict,
                blocked_why: str | None) -> list[dict]:
    """One book's trades from a pass's takes: sized by their risk on that book's capital (rules.side_dollars), inside
    that book's caps, and added to the session's state. `others` are the trades already open or waiting, in any book."""
    if not takes:
        return []
    if blocked_why:
        record["skipped"] += [{"name": p["id"], "reason": blocked_why, "pass": kind, "book": which} for p in takes]
        return []
    cap = book.capital(which)
    same = [t for t in others if book.book_of(t) == which]
    used = {x for t in same for x in rules.core_symbols(t)}
    traded = (volumes or dollar_volumes)(sorted({s for p in takes for s in [*p["long"], *p["short"]]}))
    letter = book.ID_LETTER[which]
    start = sum(book.book_of(t) == which for t in state["trades"]) + 1
    made, skipped = rules.proposal_trades(takes, session.isoformat(), state["marks"][kind]["prices"], used, start=start,
                                          room=PASS_MAX_NEW or len(takes), capital=cap, volumes=traded)
    for t in made:   # S or M, not the first book's A: the gateway still reports that book's fills under its ids
        t["id"] = t["id"].replace(f"{session.isoformat()}-A", f"{session.isoformat()}-{letter}", 1)
        if which != book.MAIN:
            t["book"] = which
    kept, capped = rules.apply_caps(made, same, max_gross=cap, max_symbol=cap * rules.MAX_SYMBOL_SHARE)
    for t in kept:
        t.update({"pass": kind, "first_session": first_session(kind, session).isoformat(), "lead": rules.lead_leg(t, traded)})
    state["trades"] += kept
    record["new"] += [t["id"] for t in kept]
    record["skipped"] += [{**x, "pass": kind, **({"book": which} if which != book.MAIN else {})} for x in [*skipped, *capped]]
    return kept


def entered(trade: dict) -> dict[str, int]:
    """symbol -> shares of each leg filled on entry so far, from the stored fills."""
    fills = book._fills_by_ref()
    out = {}
    for leg in trade["legs"]:
        side = "BOT" if leg["side"] == "long" else "SLD"
        out[leg["symbol"]] = int(round(sum(f["shares"] for f in fills.get((trade["id"], leg["symbol"]), []) if f["side"] == side)))
    return out


def crossing(action: str, book_: tuple[float, float] | None) -> float | None:
    """The price an order crosses at: the ask for a buy, the bid for a sell. None without a book: no order."""
    if rules.spread_pct(book_) is None:
        return None
    return book_[1] if action == "BUY" else book_[0]


def limit_price(action: str, price: float) -> float:
    return round(price * (1 + LIMIT_SLIP) if action == "BUY" else price * (1 - LIMIT_SLIP), 2)


def book_fields(book_: tuple[float, float] | None) -> dict:
    """The quote an order saw, kept with it, so the spread rule can be checked against what happened."""
    if not book_:
        return {}
    sp = rules.spread_pct(book_)
    return {"bid": book_[0], "ask": book_[1], "spread_pct": round(sp * 100, 3) if sp is not None else None}


def entry_session(now: datetime) -> date:
    """The session whose entries are going out at `now`: today until its close, then the next one."""
    today = now.date()
    return today if journal.is_trading_day(today) and now.time() < journal.close_time(today) else journal.next_trading_day(today)


def first_session(kind: str, session: date) -> date:
    """The first session a trade's time limit counts."""
    return session if kind in proposals.NIGHT_KINDS else journal.next_trading_day(session)


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


def waiting(session: date, which: str | None = None) -> list[dict]:
    """The session's trades still trying to enter: not stood down, and with shares left to fill. One book's, or both."""
    out = []
    for t in (load(session) or {}).get("trades", []):
        got = entered(t)
        if which is not None and book.book_of(t) != which:
            continue
        if not t.get("stood_down") and any(leg["shares"] > got[leg["symbol"]] for leg in t["legs"]):
            out.append({**t, "filled": got})
    return out


def _order(trade_id: str, symbol: str, action: str, shares: int, book_: tuple[float, float], where: str, now: datetime,
           session: date) -> dict:
    px = crossing(action, book_)
    o = {"trade_id": trade_id, "symbol": symbol, "action": action, "shares": shares, "kind": "LMT",
         "limit": limit_price(action, px), "venue": where, "ref_price": round(px, 4), "priced_on": "gateway bid/ask",
         **book_fields(book_)}
    if where == "extended" and now.time() < PREMARKET_END:
        o["good_till"] = f"{session:%Y%m%d} {GOOD_TILL} US/Eastern"
    return o


def stopped(now: datetime, skip: set[str], prices=None) -> tuple[list[tuple[dict, str]], list[str]]:
    """The open trades past their stop at the latest prices: [(trade, reason)], and the ids that couldn't be
    checked for want of a price. `skip` is trades already on their way out or still entering. Prices are the
    close job's own (Yahoo's latest minute, book.quotes), so the stop means the same at 10:00 AM as at 2:45 PM."""
    today = now.date()
    held = [t for t in book.open_trades() if t["id"] not in skip and date.fromisoformat(t["entry_date"]) <= today]
    need = lambda t: {h["symbol"] for h in t["held"]} | {r["symbol"] for r in (t.get("stop") or {}).get("rules", [])}  # noqa: E731
    symbols = sorted({s for t in held for s in need(t)})
    if not symbols:
        return [], []
    px = (prices or book.quotes)(symbols)
    when = f"{now.astimezone(CT):%-I:%M %p} CT"
    out, unchecked = [], []
    for t in held:
        if need(t) - set(px):
            unchecked.append(t["id"])
        elif reason := rules.stop_reason(t, t["held"], px, when):
            out.append((t, reason))
    return out, unchecked


STALE_SESSION = "in use somewhere else"   # the gateway's words for IBKR error 10197 (gateway.QUOTE_REFUSED)
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
    return ("the gateway refused every quote with a competing-session error (IBKR 10197), so the paper gateway was restarted"
            if done.returncode == 0 else f"tried to restart the paper gateway after a competing-session error, but docker said: {done.stderr.strip()[:200]}")


def retry_soon(now: datetime | None = None) -> None:
    """Make the next run try again: after the gateway is restarted the wait for the next try is a minute, not RETRY."""
    session = entry_session(now or datetime.now(NY))
    state = load(session)
    if state and state.get("last_attempt"):
        book._write(path(session), {**state, "last_attempt": None})


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


def dollar_volumes(symbols: list[str], sessions: int = 20) -> dict[str, float]:
    """Each symbol's average daily dollar volume over its last `sessions` daily bars, for the sizing's liquidity
    limit. A symbol with no bars is left out, so it sets no limit."""
    df = bars.load(symbols)
    if df.empty:
        return {}
    last = df.sort_values("date").groupby("symbol").tail(sessions)
    return {s: float(v) for s, v in (last["close"] * last["volume"]).groupby(last["symbol"]).mean().items() if v > 0}


async def run(now: datetime | None = None, broker=None, dry: bool = False, quotes=None, prices=None, volumes=None,
              stop_prices=None) -> dict:
    """One run. Returns the run's record; a run that did something is also kept in the session's file.
    `dry` is a run by hand: it works out the trades and orders and prints them, and writes and sends nothing.
    `quotes` stands in for `gateway_books`, `prices` for `live.prices`, `volumes` for `dollar_volumes` and
    `stop_prices` for `book.quotes` in tests."""
    now = now or datetime.now(NY)
    session = entry_session(now)
    where = venue(now)
    s = book.PaperSettings()
    dry = dry or (broker is None and not s.send)
    record = {"at": now.isoformat(timespec="seconds"), "venue": where, "dry_run": dry, "passes": [], "new": [], "stood_down": [],
              "exits": [], "cancelled": 0, "orders": [], "skipped": [], "passed": [], "warnings": []}
    state = load(session) or _blank(session)
    doc = proposals.load(session)
    recorded = [p["pass"] for p in (doc or {}).get("passes", []) if p["pass"] in proposals.NEW_PASSES]
    fresh_passes = [k for k in recorded if k not in state["handled"]]
    reviews = proposals.reviews(session)

    def left_of(trades: list[dict]) -> list[tuple[dict, dict, int]]:
        out = []
        for t in trades:
            got = entered(t)
            out += [(t, leg, leg["shares"] - got[leg["symbol"]]) for leg in t["legs"] if leg["shares"] > got[leg["symbol"]]]
        return out

    active = [t for t in state["trades"] if not t.get("stood_down")]
    exiting = {e["trade_id"] for e in state["exits"]}
    wants_out = [t for t in book.open_trades() if (reviews.get(t["id"]) or {}).get("action") == "exit" and t["id"] not in exiting]
    last = datetime.fromisoformat(state["last_attempt"]) if state["last_attempt"] else None
    part_in = any(any(entered(t).values()) for t in {u["id"]: u for u, _, _ in left_of(active)}.values())
    every = FOLLOW if part_in else RETRY
    retry_due = last is None or now - last >= every
    # Stops, every STOP_EVERY in the regular session (the owner, 2026-11-18): a trade past its stop comes out at
    # once, not at the close. Trades still entering, or already on their way out, are left alone.
    stops: list[tuple[dict, str]] = []
    checked = state.get("last_stop_check")
    stop_due = where == "regular" and (not checked or now - datetime.fromisoformat(checked) >= STOP_EVERY - STOP_SLACK)
    if stop_due:
        entering = {t["id"] for t, _, _ in left_of(active)}
        stops, unchecked = await asyncio.to_thread(stopped, now, exiting | entering | {t["id"] for t in wants_out}, stop_prices)
        state["last_stop_check"] = now.isoformat(timespec="seconds")
        if unchecked:
            record["warnings"].append(f"No price for every leg of {', '.join(unchecked)}, so {'its' if len(unchecked) == 1 else 'their'} "
                                      "stop wasn't checked this time; the next check tries again.")
    work = bool(left_of(active)) or bool(wants_out)
    if not fresh_passes and not ((work and retry_due or stops) and where):
        if stop_due and not dry:
            if record["warnings"]:
                _save(session, state, record)
            else:   # only the check's time: a check that found nothing adds no line to the runs
                book._write(path(session), state)
        record["note"] = ("nothing recorded for " + session.isoformat() if not recorded and not state["trades"] else
                          "nothing new" if not work else f"waiting: the next try is {f'{every.seconds // 60} minutes' if every >= timedelta(minutes=2) else 'a minute'} after the last"
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
        if r.get("action") == "exit" and not any(entered(t).values()):
            t["stood_down"] = r["reason"]
            record["stood_down"].append(t["id"])
    active = [t for t in state["trades"] if not t.get("stood_down")]

    # 3. The new passes' takes become trades, sized now by their risk (rules.side_dollars), inside the caps.
    halted = f"halted: {book.HALT.read_text().strip() or 'journal/paper/HALT exists'}" if book.HALT.exists() else None
    blocked_why = halted or ((why := drawdown.paused(book.MAIN)) and f"paused: {why}")   # the swing book's, for the phone note
    mine = {t["id"] for t in state["trades"]}
    held = [t for t in book.open_trades() if t["id"] not in mine]
    for kind in fresh_passes:
        takes = [p for p in doc["proposals"] if p.get("pass") == kind and p["decision"] == "take"]
        record.setdefault("takes", {})[kind] = len(takes)
        for which in books_running():   # every book trades the same takes, each sized on its own capital and inside its own limits
            made = make_trades(which, kind, takes, session, state, [*held, *active], volumes, record,
                               halted or ((w := drawdown.paused(which)) and f"paused: {w}"))
            active += made
        state["handled"].append(kind)

    left = left_of(active)
    # Why each exit goes: a pass's review, or a stop the check above found. Both only in the regular session.
    why_out = {t["id"]: (f"review: {reviews[t['id']]['reason']}", reviews[t["id"]].get("pass")) for t in wants_out}
    why_out.update({t["id"]: (reason, None) for t, reason in stops})
    send_exits = [*wants_out, *(t for t, _ in stops)] if where == "regular" else []
    if where and (left or send_exits or record["stood_down"]):
        async with (broker or book._broker(dry=dry)) as b:
            record["cancelled"] = await b.cancel({t["id"] for t in state["trades"]})
            if not dry:
                await asyncio.sleep(1)
                await book._pull(b, session)
            left = left_of(active)
            symbols = sorted({leg["symbol"] for _, leg, _ in left} | {h["symbol"] for t in send_exits for h in t["held"]})
            asked = sorted({*symbols, CANARY}) if where == "overnight" and symbols else symbols
            books, missing = await (quotes or gateway_books)(asked, where == "overnight") if symbols else ({}, {})
            missing = {x: missing.get(x) or "the gateway gave no bid and ask" for x in symbols if rules.spread_pct(books.get(x)) is None}
            # Overnight, with SPY quoted the gateway works: a name it sent nothing for has no overnight market.
            no_overnight = {x for x, why in missing.items() if where == "overnight" and x != CANARY
                            and rules.spread_pct(books.get(CANARY)) is not None and no_market(why)}
            if missing:
                record["no_quote"] = missing
            usable = lambda x: x not in missing and rules.crossable(books[x])  # noqa: E731
            orders, committed = [], {}
            # Entries. If nothing of a trade has filled, the whole trade waits on one bad leg. Otherwise the lead leg
            # is ordered in full and the others only up to the share of it that has filled (rules.entry_due); a
            # follower that can't be sent holds the lead back too, so no leg gets ahead.
            for t in active:
                mine_left = [(leg, n) for u, leg, n in left if u["id"] == t["id"]]
                if not mine_left:
                    continue
                if "lead" not in t:   # a trade made before 2026-11-16
                    t["lead"] = rules.lead_leg(t, (volumes or dollar_volumes)([leg["symbol"] for leg in t["legs"]]))
                bad = [leg["symbol"] for leg, _ in mine_left if not usable(leg["symbol"])]
                got = entered(t)
                none_in = not any(got.values())
                wide = [x for x in bad if x not in missing or x in no_overnight]
                if wide and none_in:
                    # Instant fills or nothing (the owner, 2026-11-18): a trade that can't go in at once because a
                    # leg's spread is too wide, or it has no overnight market, is passed on, not left to wait.
                    t["stood_down"] = f"passed: {_why_blocked(wide, books, missing)} when it was due to enter"
                    t["passed_on_spread"] = now.isoformat(timespec="seconds")
                    record["passed"].append(t["id"])
                    record["warnings"].append(f"{t['id']} {t['name']}: {_why_blocked(wide, books, missing)}, so the trade is passed on.")
                    continue
                if bad:
                    waits = ("the whole trade waits" if none_in else "the rest of the trade waits" if set(bad) - {t["lead"]}
                             else "the other legs only catch up to it")
                    record["warnings"].append(f"{t['id']} {t['name']}: {_why_blocked(bad, books, missing)}, so {waits} for the next try.")
                if bad and none_in:
                    continue
                due = [(leg, n) for leg, n in rules.entry_due(t, got, lead=not set(bad) - {t["lead"]}) if leg["symbol"] not in bad]
                committed[t["id"]] = {**got, **{leg["symbol"]: got[leg["symbol"]] + n for leg, n in due}}
                orders += [_order(t["id"], leg["symbol"], "BUY" if leg["side"] == "long" else "SELL", n, books[leg["symbol"]], where, now, session)
                           for leg, n in due]
            # Exits a pass asked for, and trades past their stop: all of a trade's legs or none, so a pair doesn't
            # come out one side at a time.
            for t in send_exits:
                legs = [h for h in t["held"] if h["shares"] > h["exit_shares"]]
                bad = [h["symbol"] for h in legs if not usable(h["symbol"])]
                reason, by_pass = why_out[t["id"]]
                if bad:
                    what = "the exit a pass asked for" if reason.startswith("review") else "its stop exit"
                    then = ("the next stop check tries again, and " if not reason.startswith("review") else "") + \
                        "the 2:45 PM CT close job sends it into the closing auction"
                    record["warnings"].append(f"{t['id']} {t['name']}: {what} wasn't sent ({_why_blocked(bad, books, missing)}); {then}.")
                    continue
                record["exits"].append({"trade_id": t["id"], "book": t["book"], "session_number": book._session(t, session), "intraday": True,
                                        "at": f"{now.astimezone(CT):%-I:%M %p} CT", "pass": by_pass, "reason": reason})
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
                    # The other legs follow what the leads just filled, in the same run: a lead that fills at once
                    # has its hedge seconds behind it, not a try later.
                    follow = [_order(t["id"], leg["symbol"], "BUY" if leg["side"] == "long" else "SELL", n, books[leg["symbol"]], where, now, session)
                              for t in active if t["id"] in committed
                              for leg, n in rules.entry_due(t, entered(t), committed[t["id"]], lead=False) if usable(leg["symbol"])]
                    if follow:
                        sent = await b.place(follow)
                        record["orders"] += sent
                        record["warnings"] += book._order_warnings(sent)
                        await asyncio.sleep(SETTLE)
                        await book._pull(b, session)
                    # Nothing rests: what an order didn't fill at once is cancelled, and the next try hits again.
                    record["cancelled_unfilled"] = await b.cancel({o["trade_id"] for o in record["orders"]})
                    if record["cancelled_unfilled"]:
                        await asyncio.sleep(1)
                        await book._pull(b, session)
                    # A quote that never fills isn't chased: a trade whose orders hit the bid or ask NO_FILL_TRIES
                    # times with nothing filled is passed on, like one whose spread was too wide. A trade that is
                    # part of the way in keeps going, so it is never left half built.
                    tried = {o["trade_id"] for o in record["orders"]} & set(committed)
                    for t in active:
                        if t["id"] not in tried or any(entered(t).values()):
                            continue
                        t["empty_tries"] = t.get("empty_tries", 0) + 1
                        if t["empty_tries"] >= NO_FILL_TRIES:
                            t["stood_down"] = (f"passed: no fill in {NO_FILL_TRIES} tries at the bid or ask "
                                               f"({t['lead']} quoted, but nothing traded against it)")
                            record["passed"].append(t["id"])
                            record["warnings"].append(f"{t['id']} {t['name']}: {NO_FILL_TRIES} tries hit the quote and nothing filled, "
                                                      "so the trade is passed on.")
    elif left and not where:
        record["warnings"].append(f"{len({t['id'] for t, _, _ in left})} trade(s) are waiting to enter; no session is trading at "
                                  f"{now.astimezone(CT):%-I:%M %p} CT.")
    state["last_attempt"] = now.isoformat(timespec="seconds")
    if not dry:
        _save(session, state, record)
        for kind in record["passes"]:
            _tell(kind, record, state, where, blocked_why)
    return record


def traded_note(kind: str, record: dict, state: dict, where: str | None, blocked_why: str | None) -> tuple[str, str]:
    """(title, text) for the owner's phone once the order job has acted on a pass: how many takes went in, part of
    the way in, were passed on or wait. Counts only: alerts go through a public server (watch.py), so no names,
    shares or dollars."""
    label = "late" if kind.startswith("late") else kind
    takes = record.get("takes", {}).get(kind, 0)
    title = f"{label.capitalize()} pass traded"
    if not takes:
        return title, f"The {label} pass took no trades."
    mine = [t for t in state["trades"] if t.get("pass") == kind and t["id"] in record["new"] and book.book_of(t) == book.MAIN]
    full = part = passed = waiting = 0
    for t in mine:
        got = entered(t)
        if t["id"] in record["passed"]:
            passed += 1
        elif all(got[leg["symbol"]] >= leg["shares"] for leg in t["legs"]):
            full += 1
        elif any(got.values()):
            part += 1
        else:
            waiting += 1
    skipped = sum(1 for x in record["skipped"] if x.get("pass") == kind and not x.get("book"))
    bits = [f"{full} in"]
    if part:
        bits.append(f"{part} partly in (the rest follows within minutes)")
    if passed:
        bits.append(f"{passed} passed on (a spread over {rules.MAX_CROSS_SPREAD:.0%}, or no overnight market)")
    if waiting:
        bits.append(f"{waiting} waiting " + ("for a quote" if where else "for the next session"))
    if skipped:
        why = f"the book is {blocked_why.split(':')[0]}" if blocked_why else "the book's limits"
        bits.append(f"{skipped} not entered ({why})")
    return title, f"{takes} take{'' if takes == 1 else 's'}: " + ", ".join(bits) + "."


def _tell(kind: str, record: dict, state: dict, where: str | None, blocked_why: str | None) -> None:
    """Send traded_note to the owner's phone; a failed send never fails the run."""
    try:
        from .. import watch
        watch.send(*traded_note(kind, record, state, where, blocked_why))
    except Exception as e:  # noqa: BLE001
        log.warning("traded_note_failed %r", e)


def _save(session: date, state: dict, record: dict) -> None:
    """Keep the state, and the run if it did something worth reading back."""
    did = any(record[k] for k in ("passes", "new", "stood_down", "passed", "exits", "orders", "skipped", "cancelled")) or record.get("no_quote")
    runs = [r for r in state["runs"] if r["at"] != record["at"]]
    if not did and runs and runs[-1].get("warnings") == record["warnings"]:
        runs[-1]["repeated_until"] = record["at"]   # a wait that hasn't changed: one line, not one a quarter-hour
    elif did or record["warnings"]:
        runs.append(record)
    book._write(path(session), {**state, "runs": runs})

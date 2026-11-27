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
RETRY_FAULT = timedelta(minutes=3)  # shorter wait after a transient gateway fault (timeout or competing login)
FOLLOW = timedelta(seconds=50)    # ... and every run (a minute) while a trade is part of the way in: it hits again for the rest
PASS_MAX_NEW: int | None = None   # takes entered per pass: every one (the owner, 2026-11-16; three before that).
                                  # The book's own limits still hold: its capital, 60 open trades, 10% in one symbol
LIMIT_SLIP = 0.005                # extended/overnight: how far past the bid or ask the limit sits
LIMIT_SLIP_REGULAR = 0.0015       # regular hours: tight slip so fast morning stocks don't sweep thin levels (2026-11-23)
MID_WAIT = 10                     # seconds to try a midpoint limit in regular hours before stepping to the bid/ask
MIN_MID_SPREAD = 0.02             # only try midpoint when the book is at least this wide in dollars
REGULAR_ENTRY_START = time(9, 35) # New York (8:35 AM CT): when deferred overnight takes enter, after opening auctions settle
MAX_CHASE_ATR = 1.0               # stand down a deferred or delayed trade if it already ran this many ATRs in our direction
DRIFT_GUARD_AFTER = timedelta(minutes=5)  # apply the chasing guard once entry is delayed past this long after the pass mark
PREMARKET_END = time(9, 10)       # New York: the last pre-market order, good until GOOD_TILL so it never meets the open
GOOD_TILL = "09:25:00"
SETTLE = 3                        # seconds after sending before the fills are pulled
# Regular-session stop checks, all day (the owner, 2026-11-26). From 2026-11-23 the opening half-hour was checked
# every 2 minutes to cut overshoot, but replaying every verdict at 5, 10, 30 and 60 minutes moved the average result
# by a few bp and the overshoot hardly at all, the early overshoots traced to quotes being refused (10197), not to
# the cadence, and the scoring replay (learn._checks) uses this same step, so takes and passes are checked alike.
STOP_EVERY = timedelta(minutes=10)
NO_FILL_TRIES = 3                 # tries that hit the bid or ask and filled nothing before deferring (overnight) or passing on
WIDE_SPREAD_TRIES = 3             # regular-hours tries on a wide spread before standing down, in case the book tightens
STOP_SLACK = timedelta(seconds=15) # launchd's minute is not exact; a check due in a few seconds counts as due


def spread_limit(where: str) -> float:
    """Maximum bid-ask spread for entries in `where`: 0.5% in regular hours and overnight, 2% in extended hours."""
    if where == "overnight":
        return rules.MAX_OVERNIGHT_SPREAD
    if where == "regular":
        return rules.MAX_REGULAR_SPREAD
    return rules.MAX_CROSS_SPREAD


def venue(now: datetime) -> str | None:
    """Which session an order can trade in at `now` (New York), or None between sessions."""
    t, today = now.time(), now.date()
    trading = journal.is_trading_day(today)
    close = journal.close_time(today)
    if trading and time(9, 30) <= t < close:
        return "regular" if t < entry_end(today) else None
    after_end = time(20, 0) if close == time(16, 0) else time(close.hour + 4, close.minute)   # a half day's after-hours ends at 5:00 PM
    if trading and (close <= t < after_end or time(4, 0) <= t < PREMARKET_END):
        return "extended"       # after-hours and pre-market, on SMART
    if (t >= time(20, 0) and journal.is_trading_day(today + timedelta(days=1))) or (t < time(3, 50) and trading):
        return "overnight"      # IBKR's OVERNIGHT venue, the night before any NYSE trading day
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
                blocked_why: str | None, leaving: set[str] = frozenset()) -> list[dict]:
    """One book's trades from a pass's takes: sized by their risk on that book's capital (rules.side_dollars), inside
    that book's caps, and added to the session's state. `others` are the trades already open or waiting, in any book;
    `leaving` names trades already queued to exit (review exit, intraday stop, or exit sent) so they don't count
    against gross/symbol caps while waiting for their exit fills, though their symbols stay reserved until closed."""
    if not takes:
        return []
    if blocked_why:
        record["skipped"] += [{"name": p["id"], "reason": blocked_why, "pass": kind, "book": which} for p in takes]
        return []
    cap = book.capital(which)
    same = [t for t in others if book.book_of(t) == which]
    used = {x for t in same for x in rules.core_symbols(t)}
    counted = [t for t in same if t["id"] not in leaving]
    traded = (volumes or dollar_volumes)(sorted({s for p in takes for s in [*p["long"], *p["short"]]}))
    letter = book.ID_LETTER[which]
    start = sum(book.book_of(t) == which for t in state["trades"]) + 1
    made, skipped = rules.proposal_trades(takes, session.isoformat(), state["marks"][kind]["prices"], used, start=start,
                                          room=PASS_MAX_NEW or len(takes), capital=cap, volumes=traded)
    for t in made:   # S or M, not the first book's A: the gateway still reports that book's fills under its ids
        t["id"] = t["id"].replace(f"{session.isoformat()}-A", f"{session.isoformat()}-{letter}", 1)
        if which != book.MAIN:
            t["book"] = which
    kept, capped = rules.apply_caps(made, counted, max_gross=cap * rules.MAX_GROSS_SHARE, max_symbol=cap * rules.MAX_SYMBOL_SHARE)
    by_prop = {p["id"]: p for p in takes}
    for t in kept:
        nums = (by_prop.get(t.get("proposal_id")) or {}).get("signal", {}).get("numbers") or {}
        daily_move = nums.get("atr_pct") or nums.get("sd_20d")
        t.update({"pass": kind, "first_session": first_session(kind, session).isoformat(), "lead": rules.lead_leg(t, traded),
                  **({"atr_pct": round(float(daily_move), 5)} if daily_move else {})})
    state["trades"] += kept
    record["new"] += [t["id"] for t in kept]
    record["skipped"] += [{**x, "pass": kind, **({"book": which} if which != book.MAIN else {})} for x in [*skipped, *capped]]
    return kept


def entered(trade: dict, fills: dict | None = None) -> dict[str, int]:
    """symbol -> shares of each leg filled on entry so far, from the stored fills."""
    fills = book._fills_by_ref() if fills is None else fills
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


def limit_price(action: str, price: float, where: str = "extended") -> float:
    slip = LIMIT_SLIP_REGULAR if where == "regular" else LIMIT_SLIP
    return round(price * (1 + slip) if action == "BUY" else price * (1 - slip), 2)


def _mid_order(trade_id: str, symbol: str, action: str, shares: int, book_: tuple[float, float], where: str,
               now: datetime, session: date) -> dict | None:
    """A passive midpoint limit order for regular hours when the spread is wide enough to sit strictly inside."""
    if where != "regular" or book_[1] - book_[0] < MIN_MID_SPREAD:
        return None
    mid = round((book_[0] + book_[1]) / 2, 2)
    if not (book_[0] < mid < book_[1]):
        return None
    return {"trade_id": trade_id, "symbol": symbol, "action": action, "shares": shares, "kind": "LMT",
            "limit": mid, "venue": where, "ref_price": mid, "priced_on": "gateway midpoint", **book_fields(book_)}


def _daily_move(trade: dict) -> float | None:
    """The trade's typical daily move (as a decimal fraction), from its screen ATR/sd or implied by its stop."""
    if (v := trade.get("atr_pct")) and v > 0:
        return float(v)
    if pct := abs(((trade.get("stop") or {}).get("spread_pct") or 0)):
        return pct / 100 / rules.STOP_MOVES
    return None


def _chased(trade: dict, state: dict, books: dict, now: datetime) -> str | None:
    """When a deferred or delayed trade is ready to enter, check whether the spread already ran > MAX_CHASE_ATR
    in our direction since the pass was marked. Returns the reason to stand it down, or None."""
    m = (state.get("marks") or {}).get(trade.get("pass"))
    if not m or not m.get("prices"):
        return None
    mark_at = datetime.fromisoformat(m["at"]) if m.get("at") else None
    delayed = bool(trade.get("deferred_from") or trade.get("defer_regular")
                   or (mark_at is not None and now - mark_at >= DRIFT_GUARD_AFTER))
    if not delayed:
        return None
    daily = _daily_move(trade)
    if not daily:
        return None
    entry = m["prices"]
    longs = [l["symbol"] for l in trade["legs"] if l["side"] == "long"]
    shorts = [l["symbol"] for l in trade["legs"] if l["side"] == "short"]
    legs = [*longs, *shorts]
    if any(s not in entry or s not in books or not entry[s] for s in legs):
        return None
    w = journal._weights({"long": longs or None, "short": shorts or None})
    move = sum(w[s] * (((books[s][0] + books[s][1]) / 2) / entry[s] - 1) for s in legs)
    if move > MAX_CHASE_ATR * daily:
        return f"moved {move:+.1%} ({move / daily:+.1f} ATR) in our direction before entry; not chasing"
    return None


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
         "limit": limit_price(action, px, where), "venue": where, "ref_price": round(px, 4), "priced_on": "gateway bid/ask",
         **book_fields(book_)}
    if where == "extended" and now.time() < PREMARKET_END:
        o["good_till"] = f"{session:%Y%m%d} {GOOD_TILL} US/Eastern"
    return o


async def _gateway_stop_prices(symbols: list[str], quotes_fn=None) -> dict[str, float]:
    """Current midpoints from IBKR's paper gateway for stop checks, falling back to Yahoo for any symbol missed."""
    books, _ = await (quotes_fn or gateway_books)(symbols, False)
    px = {s: round((b[0] + b[1]) / 2, 4) for s, b in books.items() if rules.spread_pct(b) is not None}
    if missing := [s for s in symbols if s not in px]:
        px.update(await asyncio.to_thread(book.quotes, missing))
    return px


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
GATEWAY_DOWN = "couldn't be asked"        # gateway_books timeout / connection failure (gateway.py)
HEAL_EVERY = timedelta(minutes=60)


def _is_transient_fault(reason: str) -> bool:
    return STALE_SESSION in (reason or "") or GATEWAY_DOWN in (reason or "")


def heal_gateway(missing: dict[str, str], now: datetime | None = None) -> str | None:
    """Restart the paper gateway when it refuses every quote with error 10197 ("competing live session") or
    every quote failed because the gateway hung/timed out.

    On 2026-11-16 the gateway gave that answer for ten hours, from just after IBKR's nightly reset, with the live
    login in use nowhere; restarting the container cleared it in ten seconds. A real competing login isn't cured
    by a restart, so this tries at most once an hour and says so each time. Returns what it did, or None."""
    from . import gateway

    if not missing:
        return None
    stale = all(STALE_SESSION in (why or "") for why in missing.values())
    down = all(GATEWAY_DOWN in (why or "") for why in missing.values())
    if not (stale or down):
        return None
    cause = "every quote refused with IBKR error 10197" if stale else "the gateway hung or timed out asking for quotes"
    _, msg = gateway.restart_container(cause, now=now, cooldown=HEAL_EVERY)
    return msg


def retry_soon(now: datetime | None = None, note: str | None = None) -> None:
    """Make the next run try again: after the gateway is restarted the wait for the next try is a minute, not RETRY.
    When `note` is given, persist it into the latest run's warnings so the dashboard and report see it too."""
    session = entry_session(now or datetime.now(NY))
    state = load(session)
    if not state:
        return
    updated = {**state, "last_attempt": None}
    if note and updated.get("runs"):
        runs = list(updated["runs"])
        last_run = dict(runs[-1])
        warns = list(last_run.get("warnings") or [])
        if note not in warns:
            warns.append(note)
        last_run["warnings"] = warns
        runs[-1] = last_run
        updated["runs"] = runs
    if state.get("last_attempt") or note:
        book._write(path(session), updated)


def _why_blocked(symbols: list[str], books: dict, missing: dict[str, str], limit: float = rules.MAX_CROSS_SPREAD) -> str:
    parts = []
    wide = [f"{x} {rules.spread_pct(books[x]):.1%}" for x in symbols if x not in missing]
    if wide:
        pct = f"{limit * 100:g}%"
        parts.append(f"spread wider than {pct} ({', '.join(wide)})")
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
              "exits": [], "cancelled": 0, "orders": [], "skipped": [], "passed": [], "deferred": [], "warnings": []}
    state = load(session) or _blank(session)
    doc = proposals.load(session)
    recorded = [p["pass"] for p in (doc or {}).get("passes", []) if p["pass"] in proposals.NEW_PASSES]
    fresh_passes = [k for k in recorded if k not in state["handled"]]
    reviews = proposals.reviews(session)

    fills_map = book._fills_by_ref()

    def left_of(trades: list[dict]) -> list[tuple[dict, dict, int]]:
        out = []
        for t in trades:
            got = entered(t, fills_map)
            out += [(t, leg, leg["shares"] - got[leg["symbol"]]) for leg in t["legs"] if leg["shares"] > got[leg["symbol"]]]
        return out

    def ready_now(t: dict) -> bool:
        return not t.get("defer_regular") or (where == "regular" and now.time() >= REGULAR_ENTRY_START)

    active = [t for t in state["trades"] if not t.get("stood_down")]
    ready_active = [t for t in active if ready_now(t)]
    exiting = {e["trade_id"] for e in state["exits"]}
    wants_out = [t for t in book.open_trades() if (reviews.get(t["id"]) or {}).get("action") == "exit" and t["id"] not in exiting]
    last = datetime.fromisoformat(state["last_attempt"]) if state["last_attempt"] else None
    part_in = any(any(entered(t, fills_map).values()) for t in {u["id"]: u for u, _, _ in left_of(ready_active)}.values())
    just_ready = any(t.get("defer_regular") for t in ready_active)
    every = FOLLOW if part_in else (RETRY_FAULT if state.get("last_fault") or state.get("last_wide") else RETRY)
    retry_due = just_ready or last is None or now - last >= every
    # Stops: every STOP_EVERY through the regular session; quoted from IBKR's paper gateway first, falling back
    # to Yahoo only if IBKR misses a symbol.
    stops: list[tuple[dict, str]] = []
    checked = state.get("last_stop_check")
    stop_due = where == "regular" and (not checked or now - datetime.fromisoformat(checked) >= STOP_EVERY - STOP_SLACK)
    if stop_due:
        # Only skip trades with zero filled shares (nothing to stop); partially-filled trades ARE checked.
        entering = {t["id"] for t in active if not any(entered(t, fills_map).values())}
        skip_ids = exiting | entering | {t["id"] for t in wants_out}
        if stop_prices is not None:
            stops, unchecked = await asyncio.to_thread(stopped, now, skip_ids, stop_prices)
        else:
            held_stop = [t for t in book.open_trades() if t["id"] not in skip_ids and date.fromisoformat(t["entry_date"]) <= now.date()]
            need_syms = sorted({s for t in held_stop for s in ({h["symbol"] for h in t["held"]} | {r["symbol"] for r in (t.get("stop") or {}).get("rules", [])})})
            px_stop = await _gateway_stop_prices(need_syms, quotes) if need_syms else {}
            stops, unchecked = stopped(now, skip_ids, lambda _: px_stop)
        state["last_stop_check"] = now.isoformat(timespec="seconds")
        if unchecked:
            record["warnings"].append(f"No price for every leg of {', '.join(unchecked)}, so {'its' if len(unchecked) == 1 else 'their'} "
                                      "stop wasn't checked this time; the next check tries again.")
    work = bool(left_of(ready_active)) or (where == "regular" and bool(wants_out))
    has_deferred = any(t.get("defer_regular") for t in active)
    if not fresh_passes and not ((work and retry_due or stops) and where):
        if stop_due and not dry:
            if record["warnings"]:
                _save(session, state, record)
            else:   # only the check's time: a check that found nothing adds no line to the runs
                book._write(path(session), state)
        record["note"] = ("nothing recorded for " + session.isoformat() if not recorded and not state["trades"] else
                          "queued trades wait for 8:35 AM CT" if not work and has_deferred else
                          "nothing new" if not work else f"waiting: the next try is {f'{every.seconds // 60} minutes' if every >= timedelta(minutes=2) else 'a minute'} after the last"
                          if where else "between sessions; what is left waits")
        return record

    # 1. A pass seen for the first time: mark every verdict's legs (and default screen hedge) at the latest price,
    #    so takes, passes, and outright-vs-hedged comparisons are scored from the same moment.
    for kind in fresh_passes:
        props = [p for p in doc["proposals"] if p.get("pass") == kind]
        def_hedges = {h for p in props if (h := (p.get("signal") or {}).get("hedge"))}
        symbols = sorted({x for p in props for x in [*p["long"], *p["short"]]} | def_hedges | {"SPY"})
        px = await asyncio.to_thread(prices or live.prices, symbols)
        state["marks"][kind] = {"at": now.isoformat(timespec="seconds"), "source": "Yahoo, latest price in any session",
                                "prices": {k: round(v, 4) for k, v in px.items()}}
        if lost := sorted(set(symbols) - set(px)):
            record["warnings"].append(f"The {kind} pass has no price at the verdict for {', '.join(lost)}, so ideas with those legs can't be scored.")
        record["passes"].append(kind)

    # 2. A take a pass has since stood down, before any of it filled: it never enters.
    for t in active:
        r = reviews.get(t["id"]) or {}
        if r.get("action") == "exit" and not any(entered(t, fills_map).values()):
            t["stood_down"] = r["reason"]
            t.pop("defer_regular", None)
            record["stood_down"].append(t["id"])
    active = [t for t in state["trades"] if not t.get("stood_down")]

    # 3. The new passes' takes become trades, sized now by their risk (rules.side_dollars), inside the caps.
    halted = f"halted: {book.HALT.read_text().strip() or 'journal/paper/HALT exists'}" if book.HALT.exists() else None
    blocked_why = halted or ((why := drawdown.paused(book.MAIN)) and f"paused: {why}")   # the swing book's, for the phone note
    mine = {t["id"] for t in state["trades"]}
    held = [t for t in book.open_trades() if t["id"] not in mine]
    leaving = exiting | {t["id"] for t in wants_out} | {t["id"] for t, _ in stops}
    for kind in fresh_passes:
        takes = [p for p in doc["proposals"] if p.get("pass") == kind and p["decision"] == "take"]
        record.setdefault("takes", {})[kind] = len(takes)
        for which in books_running():   # every book trades the same takes, each sized on its own capital and inside its own limits
            made = make_trades(which, kind, takes, session, state, [*held, *active], volumes, record,
                               halted or ((w := drawdown.paused(which)) and f"paused: {w}"), leaving=leaving)
            active += made
        state["handled"].append(kind)

    ready_active = [t for t in active if ready_now(t)]
    left = left_of(ready_active)
    # Why each exit goes: a pass's review, or a stop the check above found. Both only in the regular session.
    why_out = {t["id"]: (f"review: {reviews[t['id']]['reason']}", reviews[t["id"]].get("pass")) for t in wants_out}
    why_out.update({t["id"]: (reason, None) for t, reason in stops})
    send_exits = [*wants_out, *(t for t, _ in stops)] if where == "regular" else []
    no_overnight: set[str] = set()
    wide_retry = False
    if where and (left or send_exits or record["stood_down"]):
        async with (broker or book._broker(dry=dry)) as b:
            record["cancelled"] = await b.cancel({t["id"] for t in state["trades"]})
            if not dry:
                await asyncio.sleep(1)
                await book._pull_fills(b, session)
                fills_map = book._fills_by_ref()
            left = left_of(ready_active)
            symbols = sorted({leg["symbol"] for _, leg, _ in left} | {h["symbol"] for t in send_exits for h in t["held"]})
            asked = sorted({*symbols, CANARY}) if where == "overnight" and symbols else symbols
            books, missing = await (quotes or gateway_books)(asked, where == "overnight") if symbols else ({}, {})
            missing = {x: missing.get(x) or "the gateway gave no bid and ask" for x in symbols if rules.spread_pct(books.get(x)) is None}
            # Overnight, with SPY quoted the gateway works: a name it sent nothing for has no overnight market.
            no_overnight = {x for x, why in missing.items() if where == "overnight" and x != CANARY
                            and rules.spread_pct(books.get(CANARY)) is not None and no_market(why)}
            if missing:
                record["no_quote"] = missing
            entry_limit = spread_limit(where)
            usable_entry = lambda x: x not in missing and rules.crossable(books[x], entry_limit)  # noqa: E731
            usable_exit = lambda x: x not in missing and rules.crossable(books[x], rules.MAX_CROSS_SPREAD)  # noqa: E731
            mid_orders, cross_orders, committed = [], [], {}
            mid_trades: dict[str, list[dict]] = {}
            mid_target: dict[tuple[str, str], int] = {}
            pending_exits: list[tuple[dict, dict]] = []
            # Entries. Overnight uses a 0.5% cap and queues wider/illiquid trades for 8:35 AM CT instead of killing
            # them; regular hours uses 0.5% too, retries briefly if a book flashes wide, tries midpoint first, then
            # crosses at bid/ask.
            for t in ready_active:
                mine_left = [(leg, n) for u, leg, n in left if u["id"] == t["id"]]
                if not mine_left:
                    continue
                if t.get("defer_regular"):
                    t["deferred_from"] = t.pop("defer_regular")
                if "lead" not in t:   # a trade made before 2026-11-16
                    t["lead"] = rules.lead_leg(t, (volumes or dollar_volumes)([leg["symbol"] for leg in t["legs"]]))
                got = entered(t, fills_map)
                none_in = not any(got.values())
                if none_in:
                    book_block = halted or ((w := drawdown.paused(book.book_of(t))) and f"paused: {w}")
                    if book_block:
                        record["warnings"].append(f"{t['id']} {t['name']}: not entering while book is {book_block}.")
                        continue
                bad = [leg["symbol"] for leg, _ in mine_left if not usable_entry(leg["symbol"])]
                wide = [x for x in bad if x not in missing or x in no_overnight]
                if wide and none_in:
                    why = _why_blocked(wide, books, missing, entry_limit)
                    if where == "overnight":
                        t["defer_regular"] = f"queued for 8:35 AM CT: {why}"
                        record["deferred"].append(t["id"])
                        record["warnings"].append(f"{t['id']} {t['name']}: {why}, so the trade queues for 8:35 AM CT.")
                    elif where == "regular":
                        t["wide_tries"] = t.get("wide_tries", 0) + 1
                        if t["wide_tries"] < WIDE_SPREAD_TRIES:
                            wide_retry = True
                            record["warnings"].append(f"{t['id']} {t['name']}: {why} (try {t['wide_tries']} of {WIDE_SPREAD_TRIES}), "
                                                      "so the whole trade waits for the spread to tighten.")
                        else:
                            t["stood_down"] = f"passed: {why} in {WIDE_SPREAD_TRIES} tries"
                            t["passed_on_spread"] = now.isoformat(timespec="seconds")
                            record["passed"].append(t["id"])
                            record["warnings"].append(f"{t['id']} {t['name']}: {why} in {WIDE_SPREAD_TRIES} tries, so the trade is passed on.")
                    else:
                        t["stood_down"] = f"passed: {why} when it was due to enter"
                        t["passed_on_spread"] = now.isoformat(timespec="seconds")
                        record["passed"].append(t["id"])
                        record["warnings"].append(f"{t['id']} {t['name']}: {why}, so the trade is passed on.")
                    continue
                if bad:
                    waits = ("the whole trade waits" if none_in else "the rest of the trade waits" if set(bad) - {t["lead"]}
                             else "the other legs only catch up to it")
                    record["warnings"].append(f"{t['id']} {t['name']}: {_why_blocked(bad, books, missing, entry_limit)}, so {waits} for the next try.")
                if bad and none_in:
                    continue
                t.pop("wide_tries", None)
                if none_in and (chase := _chased(t, state, books, now)):
                    t["stood_down"] = f"passed: {chase}"
                    record["passed"].append(t["id"])
                    record["warnings"].append(f"{t['id']} {t['name']}: {chase}, so the trade is passed on.")
                    continue
                due = [(leg, n) for leg, n in rules.entry_due(t, got, lead=not set(bad) - {t["lead"]}) if leg["symbol"] not in bad]
                committed[t["id"]] = {**got, **{leg["symbol"]: got[leg["symbol"]] + n for leg, n in due}}
                for leg, n in due:
                    action = "BUY" if leg["side"] == "long" else "SELL"
                    if not dry and (mo := _mid_order(t["id"], leg["symbol"], action, n, books[leg["symbol"]], where, now, session)):
                        mid_orders.append(mo)
                        mid_trades.setdefault(t["id"], []).append(leg)
                        mid_target[(t["id"], leg["symbol"])] = got[leg["symbol"]] + n
                    else:
                        cross_orders.append(_order(t["id"], leg["symbol"], action, n, books[leg["symbol"]], where, now, session))
            # Exits a pass asked for, and trades past their stop: always cross immediately (never wait at midpoint).
            for t in send_exits:
                legs = [h for h in t["held"] if h["shares"] > h["exit_shares"]]
                bad = [h["symbol"] for h in legs if not usable_exit(h["symbol"])]
                reason, by_pass = why_out[t["id"]]
                if bad:
                    what = "the exit a pass asked for" if reason.startswith("review") else "its stop exit"
                    then = ("the next stop check tries again, and " if not reason.startswith("review") else "") + \
                        "the 2:45 PM CT close job sends it into the closing auction"
                    record["warnings"].append(f"{t['id']} {t['name']}: {what} wasn't sent ({_why_blocked(bad, books, missing)}); {then}.")
                    continue
                ex_rec = {"trade_id": t["id"], "book": t["book"], "session_number": book._session(t, session), "intraday": True,
                          "at": f"{now.astimezone(CT):%-I:%M %p} CT", "pass": by_pass, "reason": reason}
                if dry:
                    record["exits"].append(ex_rec)
                else:
                    pending_exits.append((t, ex_rec))
                cross_orders += [_order(t["id"], h["symbol"], "SELL" if h["side"] == "long" else "BUY", h["shares"] - h["exit_shares"],
                                        books[h["symbol"]], where, now, session) for h in legs]
            # Regular-hours midpoint attempt: try inside the spread for MID_WAIT seconds, cancel unfilled, then step to bid/ask.
            if mid_orders:
                state["last_attempt"] = now.isoformat(timespec="seconds")
                _save(session, state, {**record, "orders": mid_orders, "note": "sending midpoint"})
                sent_mid = await b.place(mid_orders)
                record["orders"] += sent_mid
                record["warnings"] += book._order_warnings(sent_mid)
                await asyncio.sleep(MID_WAIT)
                await book._pull_fills(b, session)
                if await b.cancel({o["trade_id"] for o in sent_mid}):
                    await asyncio.sleep(1)
                    await book._pull_fills(b, session)
                fills_map = book._fills_by_ref()
                need_fb = sorted({
                    leg["symbol"]
                    for t in ready_active if t["id"] in mid_trades
                    for leg in mid_trades[t["id"]]
                    if min(mid_target[(t["id"], leg["symbol"])], leg["shares"]) > entered(t, fills_map)[leg["symbol"]]
                })
                if need_fb and quotes is None:
                    fb_books, fb_missing = await gateway_books(need_fb, False)
                    for sym in need_fb:
                        if sym in fb_books and rules.spread_pct(fb_books[sym]) is not None:
                            books[sym] = fb_books[sym]
                            missing.pop(sym, None)
                        else:
                            books.pop(sym, None)
                            missing[sym] = fb_missing.get(sym) or "the gateway gave no bid and ask"
                for t in ready_active:
                    if t["id"] not in mid_trades:
                        continue
                    got = entered(t, fills_map)
                    for leg in mid_trades[t["id"]]:
                        target = min(mid_target[(t["id"], leg["symbol"])], leg["shares"])
                        if (rem := target - got[leg["symbol"]]) > 0 and usable_entry(leg["symbol"]):
                            action = "BUY" if leg["side"] == "long" else "SELL"
                            cross_orders.append(_order(t["id"], leg["symbol"], action, rem, books[leg["symbol"]], where, now, session))
            if cross_orders:
                if not dry:  # on disk before anything goes
                    state["last_attempt"] = now.isoformat(timespec="seconds")
                    _save(session, state, {**record, "orders": [*record["orders"], *cross_orders], "note": "sending"})
                sent = await b.place(cross_orders)
                record["orders"] += sent
                record["warnings"] += book._order_warnings(sent)
                if not dry:  # an order priced to cross fills at once, so the books aren't left behind
                    await asyncio.sleep(SETTLE)
                    await book._pull_fills(b, session)
                    fills_map = book._fills_by_ref()
            if not dry and record["orders"]:
                # The other legs follow what the leads just filled, in the same run: a lead that fills at once
                # has its hedge seconds behind it, not a try later.
                follow = [_order(t["id"], leg["symbol"], "BUY" if leg["side"] == "long" else "SELL", n, books[leg["symbol"]], where, now, session)
                          for t in ready_active if t["id"] in committed
                          for leg, n in rules.entry_due(t, entered(t, fills_map), committed[t["id"]], lead=False) if usable_entry(leg["symbol"])]
                if follow:
                    sent = await b.place(follow)
                    record["orders"] += sent
                    record["warnings"] += book._order_warnings(sent)
                    await asyncio.sleep(SETTLE)
                    await book._pull_fills(b, session)
                # Nothing rests: what an order didn't fill at once is cancelled, and the next try hits again.
                record["cancelled_unfilled"] = await b.cancel({o["trade_id"] for o in record["orders"]})
                if record["cancelled_unfilled"]:
                    await asyncio.sleep(1)
                    await book._pull_fills(b, session)
                fills_map = book._fills_by_ref()
                # Confirm intraday exits actually filled before marking the trade closing; an unfilled/rejected
                # exit stays open so the next check retries immediately instead of abandoning it until 2:45 PM CT.
                for t, ex_rec in pending_exits:
                    now_out = {}
                    for h in t["held"]:
                        exit_side = "SLD" if h["side"] == "long" else "BOT"
                        now_out[h["symbol"]] = int(round(sum(
                            f["shares"] for f in fills_map.get((t["id"], h["symbol"]), []) if f["side"] == exit_side
                        )))
                    progressed = any(now_out[h["symbol"]] > h["exit_shares"] for h in t["held"])
                    all_done = all(now_out[h["symbol"]] >= h["shares"] for h in t["held"])
                    if progressed or all_done:
                        record["exits"].append(ex_rec)
                        state["exits"].append(ex_rec)
                        if not all_done:
                            record["warnings"].append(f"{t['id']} {t['name']}: intraday exit only partially filled; remaining shares will close at the close job.")
                    else:
                        record["warnings"].append(f"{t['id']} {t['name']}: intraday exit filled nothing, so the trade stays open and will retry.")
                # A quote that never fills isn't chased: overnight it queues for 8:35 AM CT; in regular hours it is passed on.
                tried = {o["trade_id"] for o in record["orders"]} & set(committed)
                for t in ready_active:
                    if t["id"] not in tried or any(entered(t, fills_map).values()):
                        continue
                    t["empty_tries"] = t.get("empty_tries", 0) + 1
                    if t["empty_tries"] >= NO_FILL_TRIES:
                        if where == "overnight":
                            t["defer_regular"] = f"queued for 8:35 AM CT: no fill in {NO_FILL_TRIES} overnight tries"
                            t["empty_tries"] = 0
                            record["deferred"].append(t["id"])
                            record["warnings"].append(f"{t['id']} {t['name']}: {NO_FILL_TRIES} overnight tries filled nothing, "
                                                      "so the trade queues for 8:35 AM CT.")
                        else:
                            t["stood_down"] = (f"passed: no fill in {NO_FILL_TRIES} tries at the bid or ask "
                                               f"({t['lead']} quoted, but nothing traded against it)")
                            record["passed"].append(t["id"])
                            record["warnings"].append(f"{t['id']} {t['name']}: {NO_FILL_TRIES} tries hit the quote and nothing filled, "
                                                      "so the trade is passed on.")
    elif left and not where:
        record["warnings"].append(f"{len({t['id'] for t, _, _ in left})} trade(s) are waiting to enter; no session is trading at "
                                  f"{now.astimezone(CT):%-I:%M %p} CT.")
    non_market = [w for s, w in (record.get("no_quote") or {}).items() if s not in no_overnight]
    state["last_fault"] = bool(non_market) and all(_is_transient_fault(w) for w in non_market)
    state["last_wide"] = wide_retry
    state["last_attempt"] = now.isoformat(timespec="seconds")
    if not dry:
        _save(session, state, record)
        for kind in record["passes"]:
            _tell(kind, record, state, where, blocked_why)
    return record


def traded_note(kind: str, record: dict, state: dict, where: str | None, blocked_why: str | None) -> tuple[str, str]:
    """(title, text) for the owner's phone once the order job has acted on a pass: how many takes went in, part of
    the way in, queued for regular hours, were passed on or wait. Counts only: alerts go through a public server
    (watch.py), so no names, shares or dollars."""
    label = "late" if kind.startswith("late") else kind
    takes = record.get("takes", {}).get(kind, 0)
    title = f"{label.capitalize()} pass traded"
    if not takes:
        return title, f"The {label} pass took no trades."
    mine = [t for t in state["trades"] if t.get("pass") == kind and t["id"] in record["new"] and book.book_of(t) == book.MAIN]
    full = part = deferred = passed = waiting = 0
    for t in mine:
        got = entered(t)
        if t["id"] in record.get("deferred", []):
            deferred += 1
        elif t["id"] in record["passed"]:
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
    if deferred:
        bits.append(f"{deferred} queued for 8:35 AM CT")
    if passed:
        pct = f"{spread_limit(where) * 100:g}%" if where else f"{rules.MAX_CROSS_SPREAD * 100:g}%"
        bits.append(f"{passed} passed on (a spread over {pct}, or chased)")
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
    did = any(record[k] for k in ("passes", "new", "stood_down", "passed", "deferred", "exits", "orders", "skipped", "cancelled")) or record.get("no_quote")
    runs = [r for r in state["runs"] if r["at"] != record["at"]]
    if not did and runs and runs[-1].get("warnings") == record["warnings"]:
        runs[-1]["repeated_until"] = record["at"]   # a wait that hasn't changed: one line, not one a quarter-hour
    elif did or record["warnings"]:
        runs.append(record)
    book._write(path(session), {**state, "runs": runs})

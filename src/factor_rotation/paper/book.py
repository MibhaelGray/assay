"""The paper book's jobs and records, in journal/paper/:

    proposals/<session>.json     each pass's verdict on each candidate (daily.py, proposals.py); only takes are traded
    pass_reviews/<session>.json  each pass's hold, exit or extend for every open trade (daily.py)
    entries/<session>.json       the order job's trades, written before their first order goes out, and each run (entries.py)
    exits/<session>.json         write-once at 3:45 PM ET: each trade leaving at the close, why, and the orders sent
    fills/<session>.json         the day's fills, commissions, positions and account value from the paper account
    HALT                         the kill switch: while it exists no new trades open (exits still go out)
    PAUSE-<book>                 the 10% pause (drawdown.py): the book opens nothing new until resumed

A trade's state comes from these files: planned, open once its entry fills are in, closing once an exit is
sent, closed once the exit fills are in. Results are in bp of one side's notional, next to a model result
from daily bars (the same shares, first open to last close), so the difference is the cost of real fills.

Each job's record carries a `warnings` list in plain words (an order that wasn't accepted, a leg with no quote,
a stop that couldn't be checked); `alerts()` hands the latest to the dashboard and the report.
"""

import asyncio
import json
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import sleep

import pandas as pd

from .. import bars, journal, storage
from ..config import NY
from . import proposals, rules
from .broker import DryRun, IBPaper, PaperSettings, unref

log = logging.getLogger(__name__)
ROOT = journal.JOURNAL / "paper"
HALT = ROOT / "HALT"
# Price pulls: (tries, seconds between). The Mac may have just woken for the job with no network yet. The
# paper gateway can't stand in: it has no market data while the live session is logged in.
CLOSE_QUOTES = (3, 15)     # the 3:45 PM job must send by 3:49 PM
# An order that will never fill. A market-on-close order sent before the close sits at "PendingSubmit", so
# that status is no cause for alarm; a message from the gateway always is worth reading.
DEAD = {"Cancelled", "ApiCancelled", "Inactive", "not sent"}


def _read(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, default=journal._json_default) + "\n")
    return path


def _all(kind: str) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((ROOT / kind).glob("*.json"))]


# ---- Prices -----------------------------------------------------------------------------------------------
def _yahoo(symbols: list[str]) -> dict[str, float]:
    """One pull of Yahoo's 1-minute bars, pre-market included: today's latest price for each symbol it has.
    A bar from an earlier day is no quote, so a name that hasn't traded yet today is left out."""
    import yfinance as yf

    tickers = {bars._ticker(s): s for s in symbols}
    try:
        data = yf.download(list(tickers), period="1d", interval="1m", prepost=True, group_by="ticker", progress=False, threads=True)
    except Exception as e:  # no network yet, or Yahoo refusing
        log.warning("quotes_failed error=%r", e)
        return {}
    today = datetime.now(NY).date()
    out = {}
    for t, s in tickers.items():
        try:
            col = data[t]["Close"]
        except KeyError:
            try:
                col = data["Close"]
            except KeyError:
                continue
        if isinstance(col, pd.DataFrame):
            col = col.iloc[:, 0]
        col = col.dropna()
        if not len(col):
            continue
        stamp = col.index[-1]
        if getattr(stamp, "tzinfo", None) is not None and stamp.tz_convert(NY).date() != today:
            continue
        out[s] = float(col.iloc[-1])
    return out


def quotes(symbols: list[str], tries: int = 1, wait: float = 0) -> dict[str, float]:
    """The latest price today for each symbol, asking again for the ones still missing up to `tries` times."""
    out: dict[str, float] = {}
    for attempt in range(tries):
        missing = [s for s in symbols if s not in out]
        if not missing:
            break
        if attempt:
            log.warning("quotes_retry attempt=%d missing=%d", attempt + 1, len(missing))
            sleep(wait)
        out.update(_yahoo(missing))
    return out


def _order_warnings(orders: list[dict]) -> list[str]:
    out = []
    for o in orders:
        order = f"{o['trade_id']}: {o['action']} {o['shares']} {o['symbol']}"
        if o["status"] in DEAD:
            out.append(f"{order} was not accepted ({o['status']}{': ' + o['note'] if o.get('note') else ''}).")
        elif o.get("note"):
            out.append(f"{order} went in with a message from the gateway: {o['note']}")
    return out


# ---- State ------------------------------------------------------------------------------------------------
def _cleanup_ref(f: dict) -> tuple[str, str] | None:
    """A by-hand order labelled "cleanup-<trade id>" (no "|SYMBOL"), as the 2026-11-16 CTVA cover was: it belongs to
    that trade, in the fill's own symbol. The sync rewrites fills from the account, so the label is read, not edited."""
    ref = f.get("order_ref") or ""
    return (ref[len("cleanup-"):], f["symbol"]) if ref.startswith("cleanup-") and f.get("symbol") else None


def _fills_by_ref() -> dict[tuple[str, str], list[dict]]:
    out: dict[tuple[str, str], list[dict]] = {}
    seen = set()  # the gateway can hand back an earlier day's execution; count each once, on its first day
    for day in _all("fills"):
        for f in day["fills"]:
            key = unref(f["order_ref"]) or _cleanup_ref(f)
            if key and f["exec_id"] not in seen:
                seen.add(f["exec_id"])
                out.setdefault(key, []).append({**f, "session": day["session"]})
    return out


def _avg(fills: list[dict]) -> tuple[int, float | None, float]:
    shares = sum(f["shares"] for f in fills)
    px = sum(f["shares"] * f["price"] for f in fills) / shares if shares else None
    return int(round(shares)), px, sum(f.get("commission") or 0 for f in fills)


def recorded() -> list[dict]:
    """Every trade that has had an order sent for it, oldest first. The order job writes a trade down before its
    first order goes out (entries.py), so it is in the book from then on. A take stood down before anything
    filled was never in the book."""
    out = []
    fills = _fills_by_ref()
    for doc in _all("entries"):
        for t in doc.get("trades", []):
            if t.get("stood_down") and not any(fills.get((t["id"], leg["symbol"])) for leg in t["legs"]):
                continue
            out.append(t)
    return sorted(out, key=lambda t: (t["entry_date"], t["id"]))


def trades() -> list[dict]:
    """Every paper trade with its fills, exit and state, oldest first."""
    fills = _fills_by_ref()
    exits = {e["trade_id"]: {**e, "session": d["session"]}
             for d in [*_all("entries"), *_all("exits")] for e in d.get("exits", [])}
    synced = {d["session"] for d in _all("fills")}
    longer = {e["trade_id"]: e["sessions"] for d in _all("exits") for e in d.get("extended", [])}
    out = []
    for t in recorded():
        t = {**t, "planned_horizon_days": t["horizon_days"], "horizon_days": t["horizon_days"] + longer.get(t["id"], 0),
             "extended": t["id"] in longer}
        held, out_legs, commission = [], [], 0.0
        for leg in t["legs"]:
            entry_side, exit_side = ("BOT", "SLD") if leg["side"] == "long" else ("SLD", "BOT")
            fs = fills.get((t["id"], leg["symbol"]), [])
            n_in, px_in, c_in = _avg([f for f in fs if f["side"] == entry_side])
            n_out, px_out, c_out = _avg([f for f in fs if f["side"] == exit_side])
            commission += c_in + c_out
            if n_in:
                held.append({"symbol": leg["symbol"], "side": leg["side"], "shares": n_in, "entry_price": px_in,
                             "exit_shares": n_out, "exit_price": px_out})
            out_legs.append(leg["symbol"])
        ex = exits.get(t["id"])
        if ex and held and all(h["exit_shares"] >= h["shares"] for h in held):
            state = "closed"
        elif ex:
            state = "closing"
        elif held:
            state = "open"
        elif t["entry_date"] in synced:
            state = "unfilled"
        else:
            state = "planned"
        out.append({**t, "held": held, "state": state, "exit": ex, "commission": round(commission, 2)})
    return out


def open_trades() -> list[dict]:
    return [t for t in trades() if t["state"] == "open"]


def _first(t: dict) -> date:
    """The first session a trade's time limit counts: its entry session, or the one after for a trade the morning
    or midday pass entered partway through a session (entries.first_session)."""
    return date.fromisoformat(t.get("first_session") or t["entry_date"])


def capital() -> float:
    """The swing book's capital, which is also the most it may hold, long plus short."""
    from .. import portfolio
    return portfolio.capital("swing")


def _session(t: dict, today: date) -> int:
    """Which session of the trade `today` is, counting its first session as 1; 0 before that (an early entry
    filled the evening before its session, or a trade entered partway through today)."""
    if _first(t) > today:
        return 0
    days = journal.sessions_from(_first(t), 30)
    return days.index(today) + 1 if today in days else len(days) + 1


# ---- Jobs -------------------------------------------------------------------------------------------------
def _broker(readonly: bool = False, dry: bool = False):
    """The paper gateway, or the stand-in that sends nothing. `dry` is a job run by hand: it prints the orders
    it would send whatever .env says, so only the scheduled jobs and an explicit --execute can trade."""
    s = PaperSettings()
    if dry:
        return DryRun(s)
    return IBPaper(s, readonly=readonly) if s.send or readonly else DryRun(s)


async def sync(today: date, broker=None) -> dict:
    """Pull today's fills, positions and account value; keep fills already stored."""
    async with (broker or IBPaper(readonly=True)) as b:
        return await _pull(b, today)


async def _pull(b, today: date) -> dict:
    """`sync` on a connection that is already open."""
    path = ROOT / "fills" / f"{today}.json"
    new = await b.fills()
    positions = await b.positions()
    nav = await b.net_liquidation()
    # IBKR's own prices for what the account holds. With nothing held there is nothing to price, and the gateway
    # doesn't answer the request at all: it timed out after 30 seconds on every pull of an empty account.
    marks = await b.marks() if positions and hasattr(b, "marks") else {}
    old = (_read(path) or {}).get("fills", [])
    seen = {f["exec_id"] for f in new}
    payload = {"session": today.isoformat(), "pulled_at": datetime.now(NY).isoformat(),
               "fills": [*[f for f in old if f["exec_id"] not in seen], *new], "positions": positions, "net_liquidation": nav,
               "marks": marks}
    _write(path, payload)
    payload["mismatches"] = reconcile(positions)
    return payload


def _gaps(positions: dict[str, float]) -> dict[str, tuple[float, float]]:
    """symbol -> (account, books) wherever the account's position differs from what the books think they hold."""
    expected: dict[str, float] = {}
    for t in trades():
        for h in t["held"]:
            sign = 1 if h["side"] == "long" else -1
            expected[h["symbol"]] = expected.get(h["symbol"], 0) + sign * (h["shares"] - h["exit_shares"])
    def gaps(want: dict[str, float]) -> dict[str, tuple[float, float]]:
        names = {s for s, v in want.items() if v} | set(positions)
        return {s: (positions.get(s, 0), want.get(s, 0)) for s in sorted(names) if abs(positions.get(s, 0) - want.get(s, 0)) > 1e-6}

    # Positions the first book still held when the book was restarted (book.json "legacy") sit in the account
    # until they are closed. They belong to no trade here, so they are expected while they are there.
    from .. import portfolio

    legacy = ((portfolio.restart() or {}).get("legacy") or {}).get("positions") or {}
    plain = gaps(expected)
    if not legacy or not plain:
        return plain
    carried = dict(expected)
    for s, v in legacy.items():
        carried[s] = carried.get(s, 0) + v
    with_legacy = gaps(carried)
    return with_legacy if len(with_legacy) < len(plain) else plain


def reconcile(positions: dict[str, float]) -> list[str]:
    """Differences between the account's positions and what the books think they hold."""
    return [f"{s}: account {a:g}, books {b:g}" for s, (a, b) in _gaps(positions).items()]


async def close_session(today: date, flatten: bool = False, broker=None, dry: bool = False) -> dict:
    """Check each open trade at 3:45 PM ET and send market-on-close exits for those leaving today."""
    path = ROOT / "exits" / f"{today}.json"
    if path.exists():
        raise FileExistsError(f"{path} exists; today's exits already went out")
    s = PaperSettings()
    dry = dry or (broker is None and not s.send)
    exits, extended, retried, unchecked, orders, warnings = [], [], [], [], [], []
    review = {} if flatten else proposals.reviews(today)
    moc = lambda t: [{"trade_id": t["id"], "symbol": h["symbol"], "action": "SELL" if h["side"] == "long" else "BUY",  # noqa: E731
                      "shares": h["shares"] - h["exit_shares"], "kind": "MOC"} for h in t["held"] if h["shares"] > h["exit_shares"]]
    async with (broker or _broker(dry=dry)) as b:
        off: set[str] = set()
        if not dry:
            synced = await _pull(b, today)  # today's entry fills, so day-one stops and broken pairs are seen
            off = set(_gaps(synced["positions"]))
        held = open_trades()
        stop_of = lambda t: t.get("stop") or {}  # noqa: E731
        symbols = sorted({h["symbol"] for t in held for h in t["held"]} | {r["symbol"] for t in held for r in stop_of(t).get("rules", [])})
        prices = await asyncio.to_thread(quotes, symbols, *CLOSE_QUOTES) if symbols else {}
        for t in held:
            k = _session(t, today)
            if k == 0 and not flatten and date.fromisoformat(t["entry_date"]) > today:
                continue   # filled early for a later session; its stop and time limit start there
            # k is also 0 for a trade the morning or midday pass entered today: its time limit starts tomorrow,
            # but its stop and a leg that never filled are checked at this close like any other trade's.
            reason = "flatten: every trade closed by hand" if flatten else rules.exit_reason(t, k, t["held"], prices)
            if not reason and t.get("stood_down") and not flatten:   # the entry job would have closed it at the open
                reason = f"stood down after filling early ({t['stood_down']}); the entry job didn't close it at the open"
            # Stops, broken pairs and the time limit come first. The review only adds an earlier exit, or lets
            # a trade at its time limit run on if it is in profit and hasn't been extended before.
            r = review.get(t["id"]) or {}
            if r.get("action") == "exit" and not reason:
                reason = f"review: {r['reason']}"
            elif r.get("action") == "extend" and reason and reason.startswith("time limit"):
                pnl = rules.spread_return(t["held"], prices)
                if pnl is not None and pnl > 0 and not t["extended"]:
                    extended.append({"trade_id": t["id"], "sessions": r["sessions"], "reason": r["reason"]})
                    reason = None
                else:
                    reason += "; the extension asked for was refused: " + ("no price to show it's in profit" if pnl is None else "it isn't in profit" if pnl <= 0 else "it was extended before")
            if not reason:
                need = ({h["symbol"] for h in t["held"]} if "spread_pct" in stop_of(t) else set()) | {r["symbol"] for r in stop_of(t).get("rules", [])}
                if need - set(prices):
                    unchecked.append(t["id"])
                continue
            exits.append({"trade_id": t["id"], "book": t["book"], "session_number": k, "reason": reason})
            orders += moc(t)
        # An exit sent earlier whose fills never arrived leaves its trade "closing" for good. Send it again, but
        # only if the account still holds what the books say: if it doesn't, the fills were missed, not the exit.
        for t in ([] if dry else [t for t in trades() if t["state"] == "closing"]):
            if any(h["symbol"] in off for h in t["held"]):
                warnings.append(f"{t['id']} {t['name']}: the exit sent {t['exit']['session']} shows no fills, but the account's positions "
                                "don't match the books, so it wasn't sent again. Run fr paper status.")
                continue
            retried.append({"trade_id": t["id"], "reason": f"the exit sent {t['exit']['session']} never filled; sent again"})
            warnings.append(f"{t['id']} {t['name']}: the exit sent {t['exit']['session']} never filled, so it was sent again for today's close.")
            orders += moc(t)
        if unchecked:
            missing = sorted(set(symbols) - set(prices))
            warnings.append(f"No price for {', '.join(missing)} after {CLOSE_QUOTES[0]} tries, so the stop on {len(unchecked)} "
                            f"trade{'' if len(unchecked) == 1 else 's'} wasn't checked today: {', '.join(unchecked)}.")
        payload = {"session": today.isoformat(), "built_at": datetime.now(NY).isoformat(), "dry_run": dry, "prices": prices,
                   "exits": exits, "extended": extended, "retried": retried, "stops_not_checked": unchecked, "reviewed": sorted(review),
                   "orders": [], "warnings": warnings}
        if orders:
            if not dry:
                _write(path, payload)  # on disk before anything is sent, so a job that dies while sending can't send twice
            payload["orders"] = await b.place(orders)
            payload["warnings"] += _order_warnings(payload["orders"])
    if not dry:
        _write(path, payload)
    return payload


def alerts() -> list[str]:
    """The warnings from the latest session's jobs: the order job's last run, then the close job."""
    out = []
    entries = sorted((ROOT / "entries").glob("*.json"))
    if entries:
        runs = (_read(entries[-1]) or {}).get("runs") or []
        out += runs[-1].get("warnings", []) if runs else []
    exits = sorted((ROOT / "exits").glob("*.json"))
    if exits:
        out += (_read(exits[-1]) or {}).get("warnings", [])
    return out


# ---- Results ----------------------------------------------------------------------------------------------
def rows() -> list[dict]:
    """One row per trade for the dashboard and the report."""
    ts = trades()
    symbols = sorted({leg["symbol"] for t in ts for leg in t["legs"]})
    px = bars.load(symbols) if symbols else pd.DataFrame()
    bar = {(r.symbol, r.date.date()): r for r in px.itertuples()} if not px.empty else {}
    last = {}
    if not px.empty:
        latest = px.sort_values("date").groupby("symbol").last()
        last = {s: (r.close, r.date.date()) for s, r in latest.iterrows()}
    # The close before each symbol's latest bar: what a leg's change on the day is measured from.
    before = {s: float(g["close"].iloc[-2]) for s, g in px.sort_values("date").groupby("symbol") if len(g) > 1} if not px.empty else {}
    today = datetime.now(NY).date()
    out = []
    for t in ts:
        pnl = model = room = day_pnl = None
        days = journal.sessions_from(_first(t), t["horizon_days"])
        missing = sorted({leg["symbol"] for leg in t["legs"]} - {h["symbol"] for h in t["held"]}) if t["held"] else []
        stop_pct = (t.get("stop") or {}).get("spread_pct")
        detail = []
        if t["held"]:
            closed = t["state"] == "closed"
            for h in t["held"]:
                close, on = last.get(h["symbol"], (None, None))
                # A close from before the entry says nothing about this trade: it is still at its fill.
                px = h["exit_price"] if closed else close if close is not None and on >= date.fromisoformat(t["entry_date"]) else h["entry_price"]
                sign = 1 if h["side"] == "long" else -1
                # On the day: from the last close, or from the fill for a leg entered on the latest bar's session.
                held_before = close is not None and on > date.fromisoformat(t["entry_date"]) and h["symbol"] in before
                base = before[h["symbol"]] if held_before else h["entry_price"]
                detail.append({"symbol": h["symbol"], "side": h["side"], "shares": h["shares"], "entry": h["entry_price"], "last": px,
                               "pnl": round(sign * h["shares"] * (px - h["entry_price"]), 2),
                               "value": round(sign * h["shares"] * px, 2),
                               "day_pnl": None if closed else round(sign * h["shares"] * (px - base), 2)})
            gross = sum(leg["pnl"] for leg in detail)
            if not closed:
                day_pnl = round(sum(leg["day_pnl"] for leg in detail), 2)
            pnl = gross - t["commission"]
            if not closed and not missing and stop_pct is not None:
                room = round(gross / t["notional"] * 100 - stop_pct, 1)
            if closed and not t["exit"].get("intraday"):  # the model is open to close; a trade closed at 9:40 AM has none
                d0, d1 = date.fromisoformat(t["entry_date"]), date.fromisoformat(t["exit"]["session"])
                try:
                    model = sum((1 if h["side"] == "long" else -1) * h["shares"] *
                                (bar[(h["symbol"], d1)].close - bar[(h["symbol"], d0)].open) for h in t["held"])
                except KeyError:
                    model = None
        n = t["notional"]
        out.append({
            "id": t["id"], "book": t["book"], "name": t["name"], "setup": t["setup"], "direction": t["direction"],
            "entry_date": t["entry_date"], "exit_date": (t["exit"] or {}).get("session"), "state": t["state"],
            "horizon_days": t["horizon_days"], "stop": journal.stop_text(t) or "—", "reason": (t["exit"] or {}).get("reason"),
            "legs": ", ".join(f"{'+' if l['side'] == 'long' else '−'}{l['shares']} {l['symbol']}" for l in t["legs"]),
            "pnl": None if pnl is None else round(pnl, 2), "bps": None if pnl is None else round(pnl / n * 1e4, 1),
            "day_pnl": day_pnl,
            "model_bps": None if model is None else round(model / n * 1e4, 1),
            "slippage_bps": None if model is None or pnl is None else round((pnl - model) / n * 1e4, 1),
            "commission": t["commission"], "why": t["why"], "source": t["source"],
            "proposal_id": t.get("proposal_id"), "conviction": t.get("conviction"), "wrong_if": t.get("wrong_if"),
            "planned_horizon_days": t["planned_horizon_days"], "extended": t["extended"],
            "leg_detail": detail, "missing": missing, "session_number": sum(d <= today for d in days), "exit_by": days[-1].isoformat(),
            "stop_pct": stop_pct, "stop_room_pct": room,
        })
    return out


# ---- Profit and loss over time ------------------------------------------------------------------------------
def _book_fills() -> list[dict]:
    """Every fill on one of the book's orders with its session and time, oldest first. The intraday book's fills
    (trade ids I-... and X-...) can land in the same fills files when a pull runs; they are its own book's."""
    out = []
    mine = {t["id"] for t in recorded()}   # a pull also brings back fills on orders that aren't this book's:
    for (trade_id, _), fs in _fills_by_ref().items():   # the first book's, after the restart, and the other way round
        if trade_id.startswith(("I-", "X-")) or trade_id not in mine:
            continue
        for f in fs:
            d = date.fromisoformat(f["session"])
            at = datetime.fromisoformat(f["time"]).astimezone(NY) if f.get("time") else datetime.combine(d, time(9, 30), NY)
            if f.get("time") and at.time() >= time(17, 0):   # well after the closing auction: the next session's,
                d = max(d, journal.sessions_from(at.date() + timedelta(days=1), 1)[0])   # whichever day's file holds it
            out.append({**f, "session": d, "at": at})
    return sorted(out, key=lambda f: f["at"])


def _pnl_of(fills: list[dict], mark) -> float:
    """Cash from these fills after commissions, plus what is still held at `mark(symbol, its latest fill)`."""
    cash, held, latest = 0.0, {}, {}
    for f in fills:
        sign = 1 if f["side"] == "BOT" else -1
        cash -= sign * f["shares"] * f["price"] + (f.get("commission") or 0)
        held[f["symbol"]] = held.get(f["symbol"], 0.0) + sign * f["shares"]
        latest[f["symbol"]] = f
    return round(cash + sum(q * mark(s, latest[s]) for s, q in held.items() if q), 2)


def pnl_by_session(fills: list[dict] | None = None) -> list[dict]:
    """The book's running profit and loss after commissions at each session's close, starting from a zero on
    the session before its first fill. `final` is False until that session's bars have been pulled after the close.
    `fills` replaces the swing book's own (portfolio.py passes the intraday book's, or both)."""
    fills = _book_fills() if fills is None else fills
    if not fills:
        return []
    px = bars.load(sorted({"SPY", *(f["symbol"] for f in fills)}))
    closes = {s: g.assign(day=g["date"].dt.date).set_index("day")["close"].sort_index() for s, g in px.groupby("symbol")} if not px.empty else {}
    today = datetime.now(NY).date()
    first = fills[0]["session"]
    traded = {f["session"] for f in fills}
    spy = set(closes["SPY"].index) if "SPY" in closes else set()
    days = [d for d in journal.sessions_from(first, (today - first).days + 1) if d <= today and (d in spy or d in traded)]
    def spy_on(d: date) -> float | None:
        s = closes.get("SPY")
        s = s[s.index <= d] if s is not None else None
        return round(float(s.iloc[-1]), 2) if s is not None and len(s) else None

    start = journal.previous_trading_day(first)
    out = [{"date": start.isoformat(), "pnl": 0.0, "final": True, "spy": spy_on(start)}]
    for d in days:
        def mark(symbol: str, latest: dict) -> float:
            s = closes.get(symbol)
            s = s[s.index <= d] if s is not None else None
            return float(s.iloc[-1]) if s is not None and len(s) and s.index[-1] >= latest["session"] else latest["price"]
        out.append({"date": d.isoformat(), "pnl": _pnl_of([f for f in fills if f["session"] <= d], mark),
                    "final": d in spy and journal._bars_settled(d), "spy": spy_on(d)})
    return out


def pnl_intraday(sessions: int = 5, fills: list[dict] | None = None) -> dict:
    """The book's running profit and loss at the end of each 15-minute bar of the last `sessions` sessions, from
    Yahoo and not stored. `missing` lists held names Yahoo gave no bars for; they are marked at their last fill."""
    fills = _book_fills() if fills is None else fills
    if not fills:
        return {"points": [], "missing": []}
    symbols = {f["symbol"] for f in fills}
    closes = bars.intraday_closes(sorted({"SPY", *symbols}), sessions)
    times = sorted({t for s in closes.values() for t in s.index})
    last_bar = {}
    for t in times:
        last_bar[t.date()] = t
    bar = pd.Timedelta(minutes=15)
    now = pd.Timestamp(datetime.now(NY))
    points, missing = [], set()
    for t in times:
        # The closing auction prints just after the last bar ends, so the session's last point counts all its fills.
        cutoff = datetime.combine(t.date(), time(23, 59), NY) if t == last_bar[t.date()] else t + bar
        done = [f for f in fills if f["at"] <= cutoff]
        if not done:
            continue

        def mark(symbol: str, latest: dict) -> float:
            s = closes.get(symbol)
            if s is None:
                missing.add(symbol)
                return latest["price"]
            s = s[s.index <= t]
            return float(s.iloc[-1]) if len(s) and s.index[-1] + bar >= latest["at"] else latest["price"]
        spy = closes.get("SPY")
        spy = spy[spy.index <= t] if spy is not None else None
        points.append({"t": min(t + bar, now).isoformat(), "session": t.date().isoformat(), "pnl": _pnl_of(done, mark),
                       "spy": round(float(spy.iloc[-1]), 2) if spy is not None and len(spy) else None})
    return {"points": points, "missing": sorted(missing)}


def marks_info() -> dict:
    """When the prices marking open trades were pulled, and whether that was during the session."""
    pulled = storage.modified(bars.bars_path("SPY"))
    if pulled is None:
        return {"marks_at": None, "marks_intraday": False}
    p = pulled.astimezone(NY)
    return {"marks_at": p.isoformat(), "marks_intraday": journal.is_trading_day(p.date()) and time(9, 30) <= p.time() < time(16, 0)}


def symbols_held() -> set[str]:
    """Every leg of a trade that hasn't closed, so a dashboard refresh marks them all, and every leg of a
    proposal from the last month, taken or passed, so its plan can be scored on daily bars."""
    since = (datetime.now(NY).date() - pd.Timedelta(days=31)).isoformat()
    recent = {x for p in proposals.every() if p["session"] >= since for x in [*p["long"], *p["short"]]}
    return {leg["symbol"] for t in trades() if t["state"] != "closed" for leg in t["legs"]} | recent


def summary_text() -> str:
    rs = rows()
    if not rs:
        return "No paper trades yet."
    g = pd.DataFrame(rs)
    closed = g[g["state"] == "closed"]
    slip = closed["slippage_bps"].dropna()
    return (f"{len(g)} trades, {int((g['state'] == 'open').sum())} open, {len(closed)} closed"
            + (f", hit {(closed['pnl'] > 0).mean():.0%}, P&L ${closed['pnl'].sum():,.0f}, avg {closed['bps'].mean():+.0f} bp" if len(closed) else "")
            + (f", fills vs model {slip.mean():+.1f} bp over {len(slip)}" if len(slip) else ""))

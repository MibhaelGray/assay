"""The paper book as a portfolio, for the dashboard's Portfolio view: positions and profit-and-loss history.

A position is one symbol: the shares still held across the book's open trades, their average cost, the latest
daily-bar price, and what they have made today and in all. The history is the book's running profit and loss
after commissions, from its fills, at each session's close (book.pnl_by_session) or at each 15-minute bar
(book.pnl_intraday).

The book is a sub-account of one IBKR paper account: its value is its capital plus its realized and open profit
and loss, after commissions, from its own fills. Whatever the account is worth beyond the book is "unassigned":
the account's value before the book began, interest, borrow fees, and any position the book doesn't claim. It
should move slowly; a jump there means something is off the books (accounts()).

The functions still take a book's name and return rows per book, because the dashboard's data is keyed that way
and the first design ran two books; `running()` says which exist (one, "swing", since the 2026-11-15 restart).
"""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import bars, drawdown
from .config import NY
from .paper import book

# The book's capital when there is no journal/paper/book.json to say (a test, a scratch journal). The running
# book's capital comes from book.json ($1,000,000 since the 2026-11-15 restart).
CAPITAL = {"swing": 900_000.0}


def restart() -> dict | None:
    """journal/paper/book.json, written when the paper book was started again from zero on 2026-11-15:
    {"started", "design", "capital": {book: dollars}}. None for the first book."""
    path = book.ROOT / "book.json"   # book.ROOT, so a test or a scratch journal that moves the book moves this too
    return json.loads(path.read_text()) if path.exists() else None


def capital(book_: str) -> float:
    """A book's capital: what the restart gave it, or the first book's share."""
    return float(((restart() or {}).get("capital") or CAPITAL).get(book_, 0.0))


def running() -> tuple[str, ...]:
    """The books that are running. After the 2026-11 restart only the swing book is."""
    return tuple((restart() or {}).get("capital") or CAPITAL)


def fills(which: str = "swing") -> list[dict]:
    """A book's fills, oldest first. One book runs, so every name ("swing", "all") is the swing book's."""
    return book._book_fills() if book.ROOT.exists() else []


def _held(fs: list[dict]) -> dict[str, dict]:
    """symbol -> signed shares held and their average cost, from fills in time order."""
    out: dict[str, dict] = {}
    for f in fs:
        _apply(out, f)
    return out


def deployed(fs: list[dict], now: datetime | None = None) -> dict:
    """How much of a book's money was at work: gross exposure at cost (longs plus shorts, each at its average
    cost) after every fill, averaged over the time since the book's first fill, the peak, and what is out now.
    Calendar time, so hours a book sits flat are counted against it.
    Return on capital deployed is the book's profit and loss over this average."""
    if not fs:
        return {"now": 0.0, "peak": 0.0, "average": 0.0, "since": None}
    now = now or datetime.now(NY)
    state: dict[str, dict] = {}
    gross = peak = area = 0.0
    last = fs[0]["at"]
    for f in fs:
        area += gross * (f["at"] - last).total_seconds()
        _apply(state, f)
        gross = sum(abs(p["shares"]) * p["cost"] for p in state.values() if round(p["shares"]))
        peak, last = max(peak, gross), f["at"]
    area += gross * max((now - last).total_seconds(), 0)
    span = (now - fs[0]["at"]).total_seconds()
    return {"now": round(gross, 2), "peak": round(peak, 2), "average": round(area / span, 2) if span > 0 else round(gross, 2),
            "since": fs[0]["at"].isoformat()}


def _apply(out: dict[str, dict], f: dict) -> None:
    """One fill into the running positions (see _held)."""
    sign = 1 if f["side"] == "BOT" else -1
    p = out.setdefault(f["symbol"], {"shares": 0.0, "cost": 0.0, "realized": 0.0, "commission": 0.0, "since": f["at"],
                                     "since_session": f["session"]})
    p["commission"] += f.get("commission") or 0
    q, n = p["shares"], sign * f["shares"]
    if q == 0 or (q > 0) == (n > 0):          # opening or adding
        if q == 0:
            p["since"], p["since_session"] = f["at"], f["session"]
        p["cost"] = (abs(q) * p["cost"] + abs(n) * f["price"]) / (abs(q) + abs(n))
        p["shares"] = q + n
    else:                                       # closing, perhaps through zero
        closed = min(abs(q), abs(n))
        p["realized"] += closed * (f["price"] - p["cost"]) * (1 if q > 0 else -1)
        p["shares"] = q + n
        if abs(n) > abs(q):
            p["cost"], p["since"], p["since_session"] = f["price"], f["at"], f["session"]


def positions() -> list[dict]:
    """One row per symbol a book still holds."""
    by_book = {b: _held(fills(b)) for b in running()}
    symbols = sorted({s for held in by_book.values() for s, p in held.items() if round(p["shares"])})
    px = bars.load(symbols) if symbols else pd.DataFrame()
    last, prev = {}, {}
    if not px.empty:
        for s, g in px.sort_values("date").groupby("symbol"):
            last[s] = (float(g["close"].iloc[-1]), g["date"].iloc[-1].date())
            if len(g) > 1:
                prev[s] = float(g["close"].iloc[-2])
    names = {}
    if book.ROOT.exists():
        for t in book.trades():
            if t["state"] != "closed":
                for leg in t["held"]:
                    names.setdefault(("swing", leg["symbol"]), []).append(t["name"])
    out = []
    for b, held in by_book.items():
        for s, p in held.items():
            q = round(p["shares"])
            if not q:
                continue
            price, on = last.get(s, (p["cost"], None))
            # A close from before the entry's session says nothing about it: a fill at 11:49 PM belongs to the next
            # day's session, so the close earlier that day is not its mark.
            if on is not None and on < p["since_session"]:
                price = p["cost"]
            base = prev.get(s) if on is not None and p["since_session"] < on and s in prev else p["cost"]
            value = q * price
            out.append({"book": b, "symbol": s, "side": "long" if q > 0 else "short", "shares": abs(q), "avg_cost": round(p["cost"], 4),
                        "last": round(price, 4), "price_date": on.isoformat() if on else None, "value": round(value, 2),
                        "pnl": round(q * (price - p["cost"]), 2), "pnl_pct": round((price / p["cost"] - 1) * (1 if q > 0 else -1) * 100, 2),
                        "day_pnl": round(q * (price - base), 2) + 0.0, "since": p["since"].date().isoformat(),
                        "trades": sorted(set(names.get((b, s), [])))})
    return sorted(out, key=lambda r: -abs(r["value"]))


def _syncs(newest_first: bool) -> list[Path]:
    root = book.ROOT / "fills"
    return sorted(root.glob("*.json"), reverse=newest_first) if root.exists() else []


def _last_sync() -> dict:
    """The newest fills file that carries the account's value: when, what it was worth, and IBKR's marks."""
    for path in _syncs(True):
        doc = json.loads(path.read_text())
        if doc.get("net_liquidation") is not None:
            return doc
    return {}


def _first_sync() -> dict:
    """The oldest fills file that carries the account's value: what the account was worth when the books began,
    the fixed base each book's return on the whole account is measured against."""
    for path in _syncs(False):
        doc = json.loads(path.read_text())
        if doc.get("net_liquidation") is not None:
            return doc
    return {}


def accounts(pos: list[dict] | None = None) -> dict:
    """Each book as a sub-account of the paper account, and what's left over.
    Open positions are marked at IBKR's prices from the last sync, the prices the account's own value was struck
    at, so the books and the account agree to the cent when nothing is off the books; a symbol IBKR gave no
    price for falls back to its latest daily close (`pos`, from positions())."""
    sync, first = _last_sync(), _first_sync()
    base = first.get("net_liquidation")
    marks = sync.get("marks") or {}
    fallback = {(r["book"], r["symbol"]): r["last"] for r in (pos if pos is not None else positions())}
    books = []
    for b in running():
        fs = fills(b)
        held = _held(fs)
        use = deployed(fs)
        realized = sum(p["realized"] for p in held.values())
        commission = sum(p["commission"] for p in held.values())
        open_, at_close = 0.0, []
        for s, p in held.items():
            q = round(p["shares"])
            if not q:
                continue
            price = marks.get(s)
            if price is None:
                price = fallback.get((b, s), p["cost"])
                at_close.append(s)
            open_ += q * (price - p["cost"])
        value = capital(b) + realized + open_ - commission
        books.append({"book": b, "capital": capital(b), "realized": round(realized, 2), "open": round(open_, 2),
                      "commission": round(commission, 2), "pnl": round(value - capital(b), 2), "value": round(value, 2),
                      "return_pct": round((value / capital(b) - 1) * 100, 3),
                      "account_pct": round((value - capital(b)) / base * 100, 3) if base else None,
                      "deployed_now": use["now"], "deployed_peak": use["peak"], "deployed_avg": use["average"],
                      "deployed_pct": round((value - capital(b)) / use["average"] * 100, 3) if use["average"] else None,
                      "marked_at_close": sorted(at_close), **_pause(b, value)})
    nav = sync.get("net_liquidation")
    pnl = sum(x["pnl"] for x in books)
    out = {"net_liquidation": nav, "pulled_at": sync.get("pulled_at"), "books": books, "capital": sum(capital(b) for b in running()),
           "start_value": base, "start_session": first.get("session"), "pnl": round(pnl, 2),
           "account_pct": round(pnl / base * 100, 3) if base else None}
    out["unassigned"] = round(nav - sum(x["value"] for x in books), 2) if nav is not None else None
    return out


def _pause(b: str, value: float) -> dict:
    """The 10% pause's numbers for the dashboard: the book's high, where it pauses, and the pause if it is on."""
    try:
        d = drawdown.status(b, value)
    except Exception:
        return {}
    return {"peak": d["peak"], "pause_at": d["pause_at"], "pause_limit": d["limit"], "paused": d["paused"]}


def rows() -> list[dict]:
    """The dashboard's rows: positions, each book's daily history, one summary per book, and the accounts."""
    pos = positions()
    out = [{"section": "portfolio_position", **r} for r in pos]
    acct = {"section": "portfolio_accounts", **accounts(pos)}
    out.append(acct)   # filled in below with each book's change on the day
    for which in ("all", *running()):
        fs = fills(which)
        history = book.pnl_by_session(fs)
        # The day's change: the book's profit and loss now (IBKR's prices, as in its value) less what it was at the
        # close before the latest session. One figure, so the strip and the books agree.
        before = [r for r in history if r["date"] < history[-1]["date"]] if history else []
        now = acct["pnl"] if which == "all" else next(b["pnl"] for b in acct["books"] if b["book"] == which)
        target = acct if which == "all" else next(b for b in acct["books"] if b["book"] == which)
        target["day_pnl"] = round(now - before[-1]["pnl"], 2) if before else None
        if which == "all":
            acct["day_session"] = history[-1]["date"] if history else None
        out += [{"section": "portfolio_pnl", "book": which, **r} for r in history]
        held = _held(fs)
        out.append({"section": "portfolio_book", "book": which, "fills": len(fs),
                    "realized": round(sum(p["realized"] for p in held.values()), 2),
                    "commission": round(sum(p["commission"] for p in held.values()), 2),
                    "total": history[-1]["pnl"] if history else None})
    return out


def pnl_intraday(which: str) -> dict:
    return book.pnl_intraday(fills=fills(which if which in running() else "all"))

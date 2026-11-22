"""The small-cap shadow run as a book, for the dashboard's Portfolio view: the same rows the swing book gives
(an account, positions, a daily profit-and-loss history, trades), built from the recorded verdicts and daily bars.

Nothing here is traded. Each take is held as if it had been: bought or sold at the open of its session at its size
in the planned $25,000 book (score.size), against the same dollars of IWM (the small-cap ETF) the other way, and
closed at its time limit or its stop exactly as score.py scores it. Marks are the latest daily bar, which the
dashboard's refresh keeps current through the day. No commissions or spreads.
"""

from datetime import date, datetime

import pandas as pd

from .. import bars, journal
from ..config import NY
from . import HEDGE, score, screen

BOOK = "smallcap"


def _bars(names: list[str]) -> dict[str, pd.DataFrame]:
    px = bars.load(sorted({*names, HEDGE, "SPY"}))
    if px.empty:
        return {}
    return {s: g.sort_values("date").reset_index(drop=True) for s, g in px.groupby("symbol")}


def _trades(docs: list[dict], px: dict[str, pd.DataFrame]) -> list[dict]:
    """Every take that has reached its session, followed day by day: its legs, its marks and how it ended."""
    scored = {(r["key"], r["name"]): r for d in docs for r in score.results(d, pd.concat(px.values()) if px else pd.DataFrame())}
    out = []
    for d in docs:
        start = pd.Timestamp(d["session"])
        for v in d["verdicts"]:
            if v["verdict"] != "take" or v["name"] not in px or HEDGE not in px:
                continue
            own = px[v["name"]][px[v["name"]]["date"] >= start].head(v["plan"]["horizon_days"]).reset_index(drop=True)
            hedge = px[HEDGE][px[HEDGE]["date"] >= start].head(v["plan"]["horizon_days"]).reset_index(drop=True)
            k = screen.key(d["session"], d.get("pass") or "night")
            r = scored.get((k, v["name"]))
            if own.empty or hedge.empty or own["date"].iloc[0] != start or not r:
                continue
            sign = 1 if v["direction"] == "long" else -1
            dollars = score.size(v["conviction"], v["plan"]["stop_pct"], (v.get("numbers") or {}).get("dollar_volume"))
            entry, hedge_entry = r["entry"], r["hedge_entry"]
            shares, hedge_shares = round(dollars / entry), round(dollars / hedge_entry, 2)
            n = min(len(own), len(hedge))
            stopped = False
            exit_px = None
            if r["plan"]:
                n, stopped = r["plan"]["sessions"], r["plan"]["stopped"]
                if stopped:
                    exit_px = r["plan"]["exit_price"]
            marks = []   # (date, stock price, hedge price) at each close held, the stop's price on the day it hit
            for i in range(n):
                stock = exit_px if (stopped and i == n - 1) else float(own["close"].iloc[i])
                marks.append((own["date"].iloc[i].date(), stock, float(hedge["close"].iloc[i])))
            out.append({"verdict": v, "session": d["session"], "key": k, "entered_at": (v.get("entry") or {}).get("at"), "sign": sign, "dollars": dollars, "entry": entry,
                        "hedge_entry": hedge_entry, "shares": shares, "hedge_shares": hedge_shares, "marks": marks,
                        "closed": bool(r["plan"]), "stopped": stopped})
    return out


def _pnl(t: dict, upto: int) -> tuple[float, float]:
    """(the stock leg's, the hedge leg's) profit at mark `upto`."""
    _, stock, hedge = t["marks"][upto]
    return t["sign"] * t["shares"] * (stock - t["entry"]), -t["sign"] * t["hedge_shares"] * (hedge - t["hedge_entry"])


def _day(t: dict, i: int) -> tuple[float, float]:
    now = _pnl(t, i)
    if i == 0:
        return now
    before = _pnl(t, i - 1)
    return now[0] - before[0], now[1] - before[1]


def rows(docs: list[dict] | None = None, px: dict[str, pd.DataFrame] | None = None, now: datetime | None = None) -> list[dict]:
    """The small-cap book's rows for the dashboard, in the swing book's shapes under `sc_` section names."""
    import json

    if docs is None:
        docs = [json.loads(p.read_text()) for p in sorted(screen.VERDICTS.glob("*.json"))] if screen.VERDICTS.exists() else []
    if not docs:
        return []
    names = sorted({v["name"] for d in docs for v in d["verdicts"] if v["verdict"] == "take"})
    px = _bars(names) if px is None else px
    now = now or datetime.now(NY)
    trades = _trades(docs, px)
    latest = max((t["marks"][-1][0] for t in trades), default=None)
    first = date.fromisoformat(docs[0]["session"])
    capital = score.CAPITAL

    paper, held = [], {}
    realized = open_ = 0.0
    for t in trades:
        v, i = t["verdict"], len(t["marks"]) - 1
        live = not t["closed"] and t["marks"][-1][0] == latest
        stock_pnl, hedge_pnl = _pnl(t, i)
        day_stock, day_hedge = _day(t, i) if t["marks"][-1][0] == latest else (0.0, 0.0)
        last, hedge_last = t["marks"][-1][1], t["marks"][-1][2]
        pnl = stock_pnl + hedge_pnl
        stock_side, hedge_side = ("long", "short") if t["sign"] > 0 else ("short", "long")
        legs = [{"symbol": v["name"], "side": stock_side, "shares": t["shares"], "entry": t["entry"], "last": last,
                 "pnl": round(stock_pnl, 2), "value": round(t["sign"] * t["shares"] * last, 2), "day_pnl": round(day_stock, 2)},
                {"symbol": HEDGE, "side": hedge_side, "shares": t["hedge_shares"], "entry": t["hedge_entry"], "last": hedge_last,
                 "pnl": round(hedge_pnl, 2), "value": round(-t["sign"] * t["hedge_shares"] * hedge_last, 2), "day_pnl": round(day_hedge, 2)}]
        long_leg, short_leg = (legs[0], legs[1]) if t["sign"] > 0 else (legs[1], legs[0])
        level = t["entry"] * (1 - t["sign"] * v["plan"]["stop_pct"] / 100)
        room = (last / level - 1) * 100 if t["sign"] > 0 else (level / last - 1) * 100
        exit_by = journal.sessions_from(date.fromisoformat(t["session"]), v["plan"]["horizon_days"])[-1]
        stock_move = t["sign"] * (last / t["entry"] - 1) * 100
        reason = None
        if t["closed"]:
            reason = (f"Stopped: the stock moved {abs(stock_move):.1f}% against the entry, past the {v['plan']['stop_pct']:g}% stop"
                      if t["stopped"] else f"Time limit: held {len(t['marks'])} session{'s' if len(t['marks']) != 1 else ''}")
        paper.append({"section": "sc_paper", "book": BOOK, "id": f"{t['key']}-{v['name']}", "proposal_id": None,
                      "name": v["name"], "setup": v["setup"], "conviction": v["conviction"], "direction": v["direction"],
                      "state": "open" if live else "closed" if t["closed"] else "open",
                      "legs": f"+{long_leg['shares']:g} {long_leg['symbol']}, −{short_leg['shares']:g} {short_leg['symbol']}",
                      "leg_detail": legs, "pnl": round(pnl, 2), "bps": round(pnl / t["dollars"] * 1e4, 1),
                      "day_pnl": round(day_stock + day_hedge, 2) if live else None, "session_number": len(t["marks"]),
                      "horizon_days": v["plan"]["horizon_days"], "planned_horizon_days": v["plan"]["horizon_days"], "extended": False,
                      "stop": f"the stock moves {v['plan']['stop_pct']:g}% against the entry", "stop_pct": v["plan"]["stop_pct"],
                      "stop_room_pct": round(room, 1), "exit_by": exit_by.isoformat(),
                      "source": f"small-cap {(t['key'][11:] or 'night')} pass for {t['session']}, rank {v['rank']}"
                                + (f", entered at {t['entry']:.2f}" if t["entered_at"] else ", entered at the open"), "why": v["thesis"], "wrong_if": v.get("wrong_if"),
                      "entry_date": t["session"], "exit_date": t["marks"][-1][0].isoformat() if t["closed"] else None, "reason": reason,
                      "slippage_bps": None, "model_bps": None, "missing": [], "commission": 0.0})
        if t["closed"]:
            realized += pnl
            continue
        open_ += pnl
        for leg in legs:
            q = leg["shares"] * (1 if leg["side"] == "long" else -1)
            h = held.setdefault(leg["symbol"], {"shares": 0.0, "cost": 0.0, "last": leg["last"], "pnl": 0.0, "day_pnl": 0.0, "since": t["session"], "trades": []})
            h["shares"] += q
            h["cost"] += q * leg["entry"]
            h["pnl"] += leg["pnl"]
            h["day_pnl"] += leg["day_pnl"]
            h["since"] = min(h["since"], t["session"])
            h["trades"].append(v["name"])

    positions = []
    for s, h in held.items():
        if abs(h["shares"]) < 1e-9:
            continue
        avg = h["cost"] / h["shares"]
        positions.append({"section": "sc_portfolio_position", "book": BOOK, "symbol": s, "side": "long" if h["shares"] > 0 else "short",
                          "shares": round(abs(h["shares"]), 2), "avg_cost": round(avg, 4), "last": round(h["last"], 4),
                          "price_date": latest.isoformat() if latest else None, "value": round(h["shares"] * h["last"], 2),
                          "pnl": round(h["pnl"], 2), "pnl_pct": round((h["last"] / avg - 1) * (1 if h["shares"] > 0 else -1) * 100, 2),
                          "day_pnl": round(h["day_pnl"], 2), "since": h["since"], "trades": sorted(set(h["trades"]))})
    positions.sort(key=lambda r: -abs(r["value"]))

    # The book's total at each close since the first session, with SPY's close beside it as the chart expects.
    spy = px.get("SPY")
    spy_on = (lambda d: float(spy[spy["date"] == pd.Timestamp(d)]["close"].iloc[0]) if spy is not None and (spy["date"] == pd.Timestamp(d)).any() else None)
    start = journal.previous_trading_day(first)
    days = [start, *journal.sessions_from(first, 400)]
    days = [d for d in days if latest is None or d <= latest]
    history, deployed = [], []
    for d in days:
        total, gross = 0.0, 0.0
        for t in trades:
            idx = [i for i, m in enumerate(t["marks"]) if m[0] <= d]
            if not idx:
                continue
            total += sum(_pnl(t, idx[-1]))
            if not (t["closed"] and t["marks"][-1][0] < d):
                gross += 2 * t["dollars"]
        history.append({"section": "sc_portfolio_pnl", "book": BOOK, "date": d.isoformat(), "pnl": round(total, 2),
                        "final": d < now.date() or now.time() >= datetime.strptime("16:00", "%H:%M").time(), "spy": spy_on(d)})
        if d >= first:
            deployed.append(gross)
    pnl_total = realized + open_
    value = capital + pnl_total
    peak = max([capital, *(capital + r["pnl"] for r in history)])
    day_pnl = round(history[-1]["pnl"] - history[-2]["pnl"], 2) if len(history) >= 2 else None
    deployed_now = sum(2 * t["dollars"] for t in trades if not t["closed"] and t["marks"][-1][0] == latest)
    avg = sum(deployed) / len(deployed) if deployed else 0.0
    bk = {"book": BOOK, "capital": capital, "realized": round(realized, 2), "open": round(open_, 2), "commission": 0.0,
          "pnl": round(pnl_total, 2), "value": round(value, 2), "return_pct": round(pnl_total / capital * 100, 3),
          "account_pct": round(pnl_total / capital * 100, 3), "deployed_now": round(deployed_now, 2),
          "deployed_peak": round(max(deployed, default=0.0), 2), "deployed_avg": round(avg, 2),
          "deployed_pct": round(pnl_total / avg * 100, 3) if avg else None, "marked_at_close": [],
          "peak": round(peak, 2), "pause_at": round(peak - capital * 0.10, 2), "pause_limit": capital * 0.10, "paused": False,
          "day_pnl": day_pnl}
    account = {"section": "sc_portfolio_accounts", "shadow": True, "net_liquidation": None, "pulled_at": now.isoformat(timespec="seconds"),
               "books": [bk], "capital": capital, "start_value": capital, "start_session": first.isoformat(), "pnl": round(pnl_total, 2),
               "account_pct": bk["return_pct"], "unassigned": None, "day_pnl": day_pnl, "day_session": latest.isoformat() if latest else None,
               "waiting": sum(1 for d in docs for v in d["verdicts"] if v["verdict"] == "take") - len(trades)}
    book_row = {"section": "sc_portfolio_book", "book": BOOK, "fills": sum(2 + (2 if t["closed"] else 0) for t in trades),
                "realized": round(realized, 2), "commission": 0.0, "total": round(pnl_total, 2)}
    return [account, book_row, *positions, *history, *paper]

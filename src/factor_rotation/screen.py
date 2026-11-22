"""The candidate screen: where a research pass should look.

Code checks every stock, ETF, themed basket and factor pair we have bars for against a fixed list of setups
(journal.SETUPS) and writes the best 40 or so to journal/screens/<as_of>.json, each with the numbers that
triggered it and a one-line chart verdict. The pass researches only those. A breakout or breakdown also says
how tight the base was and which way the trend ran (TIGHT_BASE and ADJUST below). The screen never decides: it says
where to look, and every recorded idea carries its setup so results can be compared setup by setup.

Two ways to run it. `build()` is the nightly screen on settled daily bars. `build_live(slot)` is the screen for
one of the three daily passes (night, morning, midday): the same setups on the latest prices, which may be
after-hours, pre-market or a session still trading (live.py), plus the names moving sharply right now.

The night pass's takes trade in IBKR's overnight session, where a leg wider than 2% is passed on (paper/entries.py).
So the night screen asks the gateway for each candidate's overnight bid and ask before the pass sees it, and a
candidate whose own legs are wider than that, or have no overnight market, is dropped and the next name moves up:
no research on a name the book can't trade tonight (the owner, 2026-11-18). The default hedge's spread is shown,
not gated, since the pass can choose another hedge or none. If the gateway can't be asked, nothing is dropped and
the screen says so.
"""

import json
from datetime import date, datetime

import numpy as np
import pandas as pd

from . import bars, earnings, journal, live
from .analytics.technicals import atr, rsi
from .config import NY
from .market import _regime, basket_library
from .universe import BARS_ONLY, BROAD, ETFS, FACTOR_PAIRS, INDUSTRIES, SECTORS, screen_universe

MIN_DOLLAR_VOLUME = 20e6
OVERNIGHT_CLIENT = 93   # the night screen's spread check: its own gateway connection, apart from the order job's
OVERNIGHT_ROUNDS = 6    # times the shortlist is refilled after dropping names that can't trade overnight
TOP = 40   # 20 until 2026-11-16: more verdicts a pass, so the ranking test speaks sooner (the owner)
MORE = 15
HEAVY_VOLUME = 1.5
# How much each setup counts when ranking, before its strength (0-1) is added. Earnings and relative strength
# carry the most information; a stretch with no turn is only context.
WEIGHTS = {
    "earnings_gap_held": 3.0, "earnings_gap_faded": 2.6, "relative_strength": 2.5, "relative_weakness": 2.5,
    "basket_breakout": 2.2, "basket_breakdown": 2.2, "breakout": 2.1, "breakdown": 2.1, "turn": 2.0,
    "reclaim_200": 2.0, "lost_200": 2.0, "basket_big_move": 1.8, "factor_turn": 1.6,
    "stretched": 1.0, "basket_stretched": 1.0, "factor_stretched": 1.0, "live_move": 2.4,
}
# The live screen. A name is "moving now" when its latest price is LIVE_MOVE_ATR of its daily ATRs from the last
# regular close. Outside regular hours Yahoo gives no volume, so the move has to come with steady trading: a
# print in at least LIVE_MIN_BARS five-minute bars (50 minutes' worth; the after-hours session has 48 bars).
# During the session it needs LIVE_HEAVY times its usual volume, projected to a full day.
# A name a pass has judged in the last REJUDGE_SESSIONS sessions isn't a candidate again unless it has moved
# REJUDGE_ATR ATRs since that verdict: three passes a day would otherwise judge one idea three times and count
# it three times in the take-against-pass test.
LIVE_MOVE_ATR = 2.0
LIVE_MIN_BARS = 10
LIVE_HEAVY = 2.0
REJUDGE_ATR = 1.5
REJUDGE_SESSIONS = 2
SLOTS = ("night", "late", "late2", "late3", "late4", "morning", "midday")   # proposals.NEW_PASSES
EARNINGS_SETUPS = {"earnings_gap_held", "earnings_gap_faded"}
# Two things about the chart behind a breakout or breakdown, recorded on every stock and ETF candidate.
# Tightness: the average daily range (true range over the prior close) of the 10 sessions before today, over
# the same for the 50 sessions before those. Under 0.75 the name had gone quiet before it broke.
# Trend: "up" when the 50-day average is above the 200-day and higher than 20 sessions ago, "down" for the
# mirror image, otherwise "mixed".
# They move the ranking only a little, and only where five years of our own bars (November 2021 to November
# 2026, about 3,600 breakouts) gave a reason: a breakout inside a downtrend lagged SPY by about 30 bp over
# the next five sessions, in four years of five, where the rest led by about 35 bp. A tight base led a loose one on
# average, but in only three years of five, so it earns a smaller nudge. A breakout with the trend did no better
# than one in a mixed trend, and breakdowns didn't depend on trend, so neither is counted. The record of
# proposals decides whether any of this stays.
TIGHT_BASE = 0.75
LOOSE_BASE = 1.0
ADJUST = {"tight_base": 0.15, "breakout_in_downtrend": -0.3}
BASE_SETUPS = {"breakout", "breakdown"}

# The shortlist is TOP slots, and each should hold a different idea.
# - A name the paper book holds, or closed within RECENT_EXIT sessions, isn't a candidate: an earnings setup
#   re-lists the same name for three nights. Fresh signals on those names go to `in_book` for the pre-close review.
# - No setup takes more than MAX_PER_SETUP slots while other setups have names to give, so one kind of day (a
#   market-wide turn, a heavy earnings week) can't crowd the others out. The list is still always full when it can
#   be (the owner, 2026-11-16): slots the cap leaves empty go to the best of what it cut, so if TOP names pass
#   the screen, TOP are judged.
# - Funds that gave the same signal and move together (TWIN_CORR over 60 sessions) are one idea: the best-ranked
#   stays and names the others in `moved_with`.
# - A stock's default hedge is the market, sector or industry fund it has moved with most over 60 sessions,
#   unless its basket's own hedge is within KEEP_BASKET_WITHIN of that. Under MIN_HEDGE_CORR nothing hedges
#   it: SPY stays as the default and `hedge_fit.weak` says so. A fund is always set against SPY.
MAX_PER_SETUP = 10   # a quarter of the list, as 5 was of 20
RECENT_EXIT = 3
TWIN_CORR = 0.9
MIN_HEDGE_CORR = 0.4
KEEP_BASKET_WITHIN = 0.05
HEDGE_FUNDS = [*BROAD, *SECTORS, *(f for f in INDUSTRIES if f != "ARKK")]


def _fit(a: pd.Series, b: pd.Series) -> dict | None:
    """Correlation and beta of a's daily returns on b's, or None with under 30 sessions in common."""
    x, y = a.to_numpy(float), b.to_numpy(float)  # a screen makes thousands of these; pandas took over half its time
    both = ~(np.isnan(x) | np.isnan(y))
    if both.sum() < 30:
        return None
    x, y = x[both] - x[both].mean(), y[both] - y[both].mean()
    xx, yy, xy = float(x @ x), float(y @ y), float(x @ y)
    if not xx or not yy:
        return None
    return {"corr_60d": round(xy / (xx * yy) ** 0.5, 2), "beta_60d": round(xy / yy, 2)}


def _pick_hedge(sym: str, r60: pd.DataFrame, basket_hedge: str | None) -> tuple[str, dict | None]:
    """(hedge, how well it fits) for one name from its last 60 sessions of returns."""
    if sym == "SPY":
        return "SPY", None
    if sym in ETFS:
        fit = _fit(r60[sym], r60["SPY"])
        if fit:
            fit = {**fit, "chosen": "a fund is set against the market", "weak": fit["corr_60d"] < MIN_HEDGE_CORR}
        return "SPY", fit
    fits = {f: fit for f in {*HEDGE_FUNDS, *([basket_hedge] if basket_hedge else [])}
            if f in r60 and (fit := _fit(r60[sym], r60[f]))}
    if not fits:
        return basket_hedge or "SPY", None
    best = max(sorted(fits), key=lambda f: fits[f]["corr_60d"])
    if basket_hedge in fits and fits[basket_hedge]["corr_60d"] >= fits[best]["corr_60d"] - KEEP_BASKET_WITHIN \
            and fits[basket_hedge]["corr_60d"] >= MIN_HEDGE_CORR:
        return basket_hedge, {**fits[basket_hedge], "chosen": "its basket's hedge", "weak": False}
    if fits[best]["corr_60d"] >= MIN_HEDGE_CORR:
        return best, {**fits[best], "chosen": f"the best match of {len(fits)} funds", "weak": False}
    spy = fits.get("SPY") or {"corr_60d": None, "beta_60d": None}
    return "SPY", {**spy, "chosen": f"no fund moves with it (best {best} {fits[best]['corr_60d']:+.2f}); SPY by default", "weak": True}


def _fit_text(hedge: str, fit: dict | None) -> str | None:
    if not fit or fit["corr_60d"] is None:
        return None
    text = f"hedge {hedge}: correlation {fit['corr_60d']:+.2f}, beta {fit['beta_60d']:.2f}"
    return text + ("; it barely moves with the hedge, so this is close to an outright position" if fit["weak"] else "")


def _book_names(sessions: list[date], as_of: date) -> dict[str, str]:
    """Names the paper book holds, or closed within the last RECENT_EXIT sessions, and why each is left out."""
    from .paper import book, rules

    if not book.ROOT.exists():
        return {}
    recent = {d.isoformat() for d in [d for d in sessions if d <= as_of][-RECENT_EXIT:]}
    out = {}
    for t in book.trades():
        if t["state"] == "unfilled":
            continue
        if t["state"] != "closed":
            why = f"the book holds it ({t['id']})"
        elif (t.get("exit") or {}).get("session") in recent:
            why = f"the book closed it on {t['exit']['session']} ({t['id']}: {t['exit']['reason']})"
        else:
            continue
        for sym in rules.core_symbols(t):
            out[sym] = why
    return out


def _judged(session: date) -> dict[str, dict]:
    """name -> the latest verdict a pass gave it within the last REJUDGE_SESSIONS sessions up to `session`."""
    from .paper import proposals

    if not (proposals.root() / "proposals").exists():
        return {}
    since = journal.previous_trading_day(session).isoformat() if REJUDGE_SESSIONS > 1 else session.isoformat()
    out = {}
    for p in proposals.every():
        if p.get("reviewed") and since <= p["session"] <= session.isoformat():
            n = (p.get("signal") or {}).get("numbers") or {}
            out[p["name"]] = {"session": p["session"], "pass": p.get("pass"), "decision": p["decision"], "setup": p["setup"],
                              "direction": p["direction"], "price": n.get("price") or n.get("close"), "atr_pct": n.get("atr_pct")}
    return out


def _judged_already(c: dict, was: dict) -> str | None:
    """Why this candidate isn't judged again, or None if it has changed enough since its last verdict."""
    said = f"the {was['pass'] or 'last'} pass for {was['session']} judged it ({was['decision']})"
    now = (c.get("numbers") or {}).get("price")
    if c["kind"] in ("stock", "etf") and now and was.get("price") and was.get("atr_pct"):
        moved = (now / was["price"] - 1) / was["atr_pct"]
        return None if abs(moved) >= REJUDGE_ATR else f"{said} and it has moved {moved:+.1f} ATR since, under {REJUDGE_ATR:g}"
    if (c["setup"], c["direction"]) != (was["setup"], was["direction"]):
        return None
    return f"{said} on the same setup"


def _shortlist(ranked: list[dict], r60: pd.DataFrame, in_book: dict[str, str], judged: dict[str, dict] | None = None,
               again: list[dict] | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """(candidates, the rest in rank order, signals on names the book holds) from every hit, best first.
    `judged` (the live screen) leaves out names a recent pass already gave a verdict; they are added to `again`."""
    held, kept = [], []
    for c in ranked:
        if c["kind"] in ("stock", "etf") and c["name"] in in_book:
            held.append({**{k: c[k] for k in ("name", "kind", "setup", "direction", "score", "why")}, "in_book": in_book[c["name"]]})
            continue
        if judged and c["name"] in judged and (why := _judged_already(c, judged[c["name"]])):
            if again is not None:
                again.append({**{k: c[k] for k in ("name", "kind", "setup", "direction", "score", "why")}, "judged": why})
            continue
        if c["kind"] == "etf":
            twin = next((k for k in kept if k["kind"] == "etf" and k["setup"] == c["setup"] and k["direction"] == c["direction"]
                         and c["name"] in r60 and k["name"] in r60 and r60[c["name"]].corr(r60[k["name"]]) >= TWIN_CORR), None)
            if twin:
                twin.setdefault("moved_with", []).append(c["name"])
                continue
        kept.append(c)
    for c in kept:
        if c.get("moved_with"):
            c["why"] += f"; {', '.join(c['moved_with'])} gave the same signal and move with it"
    top, rest, used = [], [], {}
    for c in kept:
        if len(top) < TOP and used.get(c["setup"], 0) < MAX_PER_SETUP:
            top.append(c)
            used[c["setup"]] = used.get(c["setup"], 0) + 1
        else:
            rest.append(c)
    if len(top) < TOP and rest:   # the cap left slots empty: fill them, and keep the whole list in rank order
        fill = rest[:TOP - len(top)]
        rest = rest[len(fill):]
        order = {id(c): i for i, c in enumerate(kept)}
        top = sorted(top + fill, key=lambda c: order[id(c)])
    return top, rest, held


def _score(hit: dict) -> float:
    return WEIGHTS[hit["setup"]] + min(hit["strength"], 6) / 6 + sum((hit.get("adjust") or {}).values())


def _pct(x: float, places: int = 1) -> str:
    return f"{x * 100:+.{places}f}%"


def _groups() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for b in basket_library()["baskets"]:
        for m in b["members"]:
            out.setdefault(m["symbol"], []).append(b)
    return out


class _Days:
    """Trading-session arithmetic around tonight."""

    def __init__(self, as_of: date):
        # One calendar lookup up front: building the exchange calendar per report is slow.
        self.as_of = as_of
        cal = journal._calendar().valid_days(as_of - pd.Timedelta(days=800), as_of + pd.Timedelta(days=90))
        self.sessions = [d.date() for d in cal]
        self.open = set(self.sessions)
        self.next = next(d for d in self.sessions if d > as_of)
        self.ahead = [d for d in self.sessions if d >= self.next]

    def reaction(self, report: dict) -> date | None:
        """The first session that trades on a report."""
        d = date.fromisoformat(report["date"])
        if report["session"] == "after_close":
            return next((s for s in self.sessions if s > d), None)
        return d if report["session"] == "before_open" and d in self.open else None

    def sessions_away(self, d: date) -> int | None:
        """1 = the next session."""
        return self.ahead.index(d) + 1 if d in self.ahead else None


def _next_report(symbol: str, reports: dict, days: _Days) -> dict | None:
    best = None
    for r in reports.get(symbol, []):
        d = days.reaction(r)
        if d and d > days.as_of and (best is None or d < best["reacts"]):
            best = {"date": r["date"], "when": r["session"], "reacts": d, "sessions_away": days.sessions_away(d)}
    return best


def _verdict(f: dict) -> tuple[str, dict]:
    """One line on the chart, and the numbers behind it."""
    c = f["close"]
    if f["sma200"] is None:
        trend = "above the 50-day; under 200 days of history" if c > f["sma50"] else "below the 50-day; under 200 days of history"
    elif c > f["sma50"] and c > f["sma200"]:
        trend = "above the 50 and 200-day"
    elif c < f["sma50"] and c < f["sma200"]:
        trend = "below the 50 and 200-day"
    elif c > f["sma50"]:
        trend = "above the 50-day, below the 200-day"
    else:
        trend = "below the 50-day, above the 200-day"
    levels = [("20-day high", f["hi20"]), ("52-week high", f["hi52"]), ("20-day low", f["lo20"]), ("52-week low", f["lo52"]),
              ("50-day", f["sma50"]), ("200-day", f["sma200"])]
    above = [(v, k) for k, v in levels if v is not None and v > c]
    below = [(v, k) for k, v in levels if v is not None and v < c]
    parts = [trend, f"{f['dist20_atr']:+.1f} ATR from the 20-day, RSI {f['rsi']:.0f}", f"volume {f['vol_ratio']:.1f}x"]
    if f["trend"]:
        parts.insert(1, _trend_text(f))
    if f["tightness"] is not None:
        parts.append(f"daily ranges of the 10 sessions before today {f['tightness']:.2f}x the 50 before")
    if above:
        v, k = min(above)
        parts.append(f"resistance {v:,.2f} ({k}, {(v / c - 1) * 100:+.1f}%)")
    if below:
        v, k = max(below)
        parts.append(f"support {v:,.2f} ({k}, {(v / c - 1) * 100:+.1f}%)")
    parts.append(f"ATR {f['atr']:,.2f} ({f['atr_pct'] * 100:.1f}%)")
    nums = {k: f[k] for k in ("close", "ret_1d", "ret_5d", "ret_20d", "atr", "atr_pct", "rsi", "dist20_atr", "vol_ratio",
                              "sma50", "sma200", "hi52", "lo52", "usd_volume", "tightness", "trend", "sma50_change_20d")}
    if x := f.get("live"):
        nums.update({"price": x["price"], "price_at": x["at"], "live_phase": x["phase"], "live_move": x["move"],
                     "live_move_atr": x["move_atr"], "live_bars": x["bars"]})
        if x["phase"] != "regular":
            parts.insert(0, _live_text(x))
    if f.get("partial"):
        nums["session_minutes"] = f["partial"]["minutes"]
        parts.insert(0, f"today so far, {f['partial']['minutes']} minutes into the session, with volume projected to a full day")
    return "; ".join(parts), journal._records(pd.DataFrame([nums]))[0]


def _live_text(x: dict) -> str:
    """One name's trading outside regular hours, in words."""
    session = "after-hours" if x["phase"] == "post" else "pre-market"
    return (f"{session} at {live.clock(x['at'])}: {x['price']:,.2f}, {_pct(x['move'])} from the last regular close "
            f"({x['move_atr']:+.1f} ATR), a trade in {x['bars']} five-minute bars")


def _trend_text(f: dict) -> str:
    side = "above" if f["sma50"] > f["sma200"] else "below"
    way = "rising" if f["sma50_change_20d"] > 0 else "falling"
    return f"50-day {side} the 200-day and {way} (trend {f['trend']})"


def _base_text(f: dict) -> str:
    t = f["tightness"]
    word = "a tight base" if t <= TIGHT_BASE else "a loose base" if t > LOOSE_BASE else "an ordinary base"
    return f"{word}: daily ranges of the 10 sessions before today {t:.2f}x the 50 before"


def _features(df: pd.DataFrame) -> dict | None:
    df = df.sort_values("date").reset_index(drop=True)
    if len(df) < 60:
        return None
    c, o, h, lo, v = (df[k].to_numpy(float) for k in ("close", "open", "high", "low", "volume"))
    a = atr(df).to_numpy()
    sma = lambda n: df["close"].rolling(n).mean().to_numpy()  # noqa: E731
    s20, s50, s200 = sma(20), sma(50), sma(200)
    rng = h[-1] - lo[-1]
    prior_vol = v[-21:-1].mean()
    # True range as a share of the prior close; tr[-1] is today, which the base leaves out.
    tr = np.maximum.reduce([h[1:] - lo[1:], np.abs(h[1:] - c[:-1]), np.abs(lo[1:] - c[:-1])]) / c[:-1]
    tightness = None
    if len(tr) >= 61 and tr[-61:-11].mean() > 0:
        tightness = float(tr[-11:-1].mean() / tr[-61:-11].mean())
    trend, s50_change = None, None
    if len(c) >= 221 and not np.isnan(s200[-1]) and not np.isnan(s50[-21]):
        s50_change = float(s50[-1] / s50[-21] - 1)
        trend = "up" if s50[-1] > s200[-1] and s50_change > 0 else "down" if s50[-1] < s200[-1] and s50_change < 0 else "mixed"
    below200 = 0
    for i in range(len(c) - 2, -1, -1):
        if np.isnan(s200[i]) or c[i] >= s200[i]:
            break
        below200 += 1
    above200 = 0
    for i in range(len(c) - 2, -1, -1):
        if np.isnan(s200[i]) or c[i] <= s200[i]:
            break
        above200 += 1
    return {
        "df": df, "c": c, "o": o, "h": h, "l": lo, "v": v, "a": a, "dates": df["date"].dt.date.to_numpy(),
        "close": c[-1], "ret_1d": c[-1] / c[-2] - 1, "ret_5d": c[-1] / c[-6] - 1, "ret_20d": c[-1] / c[-21] - 1,
        "gap": o[-1] / c[-2] - 1, "atr": a[-1], "atr_pct": a[-1] / c[-1], "atr_pct_prev": a[-2] / c[-2],
        "rsi": float(rsi(df["close"]).iloc[-1]), "rsi_prev": float(rsi(df["close"]).iloc[-2]),
        "range_pos": (c[-1] - lo[-1]) / rng if rng > 0 else 0.5,
        "vol_ratio": v[-1] / prior_vol if prior_vol > 0 else 0.0, "usd_volume": float(np.median((c * v)[-20:])),
        "sma50": s50[-1], "sma200": None if np.isnan(s200[-1]) else s200[-1],
        "dist20_atr": (c[-1] - s20[-1]) / a[-1], "dist20_atr_prev": (c[-2] - s20[-2]) / a[-2],
        "hi20": h[-21:-1].max(), "lo20": lo[-21:-1].min(), "hi52": h[-253:-1].max(), "lo52": lo[-253:-1].min(),
        "hi52_close": c[-253:-1].max(), "lo52_close": c[-253:-1].min(),
        "days_below_200": below200, "days_above_200": above200, "above200_now": s200[-1] < c[-1] if not np.isnan(s200[-1]) else None,
        "down_streak": int(c[-2] < c[-3] < c[-4] < c[-5]), "up_streak": int(c[-2] > c[-3] > c[-4] > c[-5]),
        "tightness": tightness, "trend": trend, "sma50_change_20d": s50_change,
    }


def _symbol_hits(sym: str, f: dict, hedge: str, hedge_ret: pd.Series, spy_ret1: float, reports: dict,
                 days: _Days) -> list[dict]:
    hits = []

    def add(setup: str, direction: int, strength: float, why: str) -> None:
        adjust = {}
        if setup in BASE_SETUPS:
            if f["tightness"] is not None:
                why += f"; from {_base_text(f)}"
                if f["tightness"] <= TIGHT_BASE:
                    adjust["tight_base"] = ADJUST["tight_base"]
            if f["trend"]:
                against = (direction > 0 and f["trend"] == "down") or (direction < 0 and f["trend"] == "up")
                why += f"; {_trend_text(f)}{', against this trade' if against else ''}"
                if setup == "breakout" and f["trend"] == "down":
                    adjust["breakout_in_downtrend"] = ADJUST["breakout_in_downtrend"]
        if f.get("partial"):  # the session is still trading: nothing has closed yet
            why = why.replace("closed", "trading")
        hits.append({"setup": setup, "direction": "long" if direction > 0 else "short", "strength": float(abs(strength)), "why": why,
                     "adjust": adjust})

    ret1, atrp = f["ret_1d"], f["atr_pct_prev"]
    # Earnings in the last three sessions: gapped and held, or gapped and faded, or the reaction ran against the news.
    if sym not in ETFS:
        for r in reports.get(sym, []):
            k_day = days.reaction(r)
            if k_day is None or k_day not in set(f["dates"][-3:]):
                continue
            k = int(np.where(f["dates"] == k_day)[0][0])
            c, o, h, lo, v, a = f["c"], f["o"], f["h"], f["l"], f["v"], f["a"]
            gap = o[k] / c[k - 1] - 1
            gap_atr = gap / (a[k - 1] / c[k - 1])
            rng = h[k] - lo[k]
            pos = (c[k] - lo[k]) / rng if rng > 0 else 0.5
            volr = v[k] / v[max(0, k - 20):k].mean()
            day_ret = c[k] / c[k - 1] - 1
            rel = day_ret - float(hedge_ret.get(pd.Timestamp(k_day), 0.0))
            ago = len(c) - 1 - k
            when = {0: "today", 1: "yesterday"}.get(ago, f"{ago} sessions ago")
            head = f"reported {r['date']} {r['session'].replace('_', ' ')}, reacted {when}: gap {_pct(gap)} ({gap_atr:+.1f} ATR), " \
                   f"day {_pct(day_ret)}, {_pct(rel)} against {hedge}, volume {volr:.1f}x"
            if r.get("surprise_pct") is not None:
                head += f", EPS surprise {r['surprise_pct']:+.0f}%"
            if abs(gap_atr) >= 1.5:
                d = 1 if gap > 0 else -1
                strong = pos >= 2 / 3 if d > 0 else pos <= 1 / 3
                weak = pos <= 1 / 3 if d > 0 else pos >= 2 / 3
                mid = lo[k] + rng / 2
                held = d * (c[-1] - mid) > 0
                if strong and volr >= 2 and held:
                    add("earnings_gap_held", d, gap_atr, f"{head}; closed in the {'top' if d > 0 else 'bottom'} third and still "
                        f"{'above' if d > 0 else 'below'} the middle of that day's range ({mid:,.2f})")
                elif weak:
                    add("earnings_gap_faded", -d, gap_atr, f"{head}; closed in the {'bottom' if d > 0 else 'top'} third, "
                        f"against the gap")
            surprise = r.get("surprise_pct")
            rel_atr = rel / (a[k - 1] / c[k - 1])
            if surprise is not None and surprise > 0 and rel_atr <= -1:
                add("earnings_gap_faded", -1, rel_atr, f"{head}; beat estimates and still fell against {hedge} (good news sold)")
            elif surprise is not None and surprise < 0 and rel_atr >= 1:
                add("earnings_gap_faded", 1, rel_atr, f"{head}; missed estimates and still rose against {hedge} (bad news bought)")
    heavy = f["vol_ratio"] >= HEAVY_VOLUME
    move_atr = ret1 / atrp
    if heavy and f["close"] > f["hi52_close"]:
        add("breakout", 1, move_atr, f"closed at {f['close']:,.2f}, above the 52-week closing high {f['hi52_close']:,.2f}, "
            f"on {f['vol_ratio']:.1f}x volume ({_pct(ret1)}, {move_atr:+.1f} ATR)")
    elif heavy and f["close"] > f["hi20"] and ret1 > 0:
        add("breakout", 1, move_atr, f"closed at {f['close']:,.2f}, above the 20-day high {f['hi20']:,.2f}, on "
            f"{f['vol_ratio']:.1f}x volume ({_pct(ret1)}, {move_atr:+.1f} ATR)")
    if heavy and f["close"] < f["lo52_close"]:
        add("breakdown", -1, move_atr, f"closed at {f['close']:,.2f}, below the 52-week closing low {f['lo52_close']:,.2f}, "
            f"on {f['vol_ratio']:.1f}x volume ({_pct(ret1)}, {move_atr:+.1f} ATR)")
    elif heavy and f["close"] < f["lo20"] and ret1 < 0:
        add("breakdown", -1, move_atr, f"closed at {f['close']:,.2f}, below the 20-day low {f['lo20']:,.2f}, on "
            f"{f['vol_ratio']:.1f}x volume ({_pct(ret1)}, {move_atr:+.1f} ATR)")
    if heavy and f["above200_now"] and f["days_below_200"] >= 20:
        add("reclaim_200", 1, move_atr, f"closed above the 200-day ({f['sma200']:,.2f}) after {f['days_below_200']} sessions "
            f"below, on {f['vol_ratio']:.1f}x volume")
    if heavy and f["above200_now"] is False and f["days_above_200"] >= 20:
        add("lost_200", -1, move_atr, f"closed below the 200-day ({f['sma200']:,.2f}) after {f['days_above_200']} sessions "
            f"above, on {f['vol_ratio']:.1f}x volume")
    floor = max(atrp, 0.01)
    if spy_ret1 <= -0.01 and ret1 >= floor:
        add("relative_strength", 1, (ret1 - spy_ret1) / atrp, f"{_pct(ret1)} ({move_atr:+.1f} ATR) on a day SPY fell {_pct(spy_ret1)}")
    if spy_ret1 >= 0.01 and ret1 <= -floor:
        add("relative_weakness", -1, (ret1 - spy_ret1) / atrp, f"{_pct(ret1)} ({move_atr:+.1f} ATR) on a day SPY rose {_pct(spy_ret1)}")
    # Stretched: RSI or distance from the 20-day. A turn needs yesterday's stretch, a close in the day's top or
    # bottom quarter against it, and a losing (winning) streak or a move of a full ATR.
    was_low = f["rsi_prev"] <= 25 or f["dist20_atr_prev"] <= -3
    was_high = f["rsi_prev"] >= 75 or f["dist20_atr_prev"] >= 3
    if was_low and ret1 > 0 and f["range_pos"] >= 0.75 and (f["down_streak"] or move_atr >= 1):
        add("turn", 1, f["dist20_atr_prev"], f"was stretched down (RSI {f['rsi_prev']:.0f}, {f['dist20_atr_prev']:+.1f} ATR from "
            f"the 20-day) and turned: {_pct(ret1)}, closed in the top quarter of the day")
    elif was_high and ret1 < 0 and f["range_pos"] <= 0.25 and (f["up_streak"] or move_atr <= -1):
        add("turn", -1, f["dist20_atr_prev"], f"was stretched up (RSI {f['rsi_prev']:.0f}, {f['dist20_atr_prev']:+.1f} ATR from "
            f"the 20-day) and turned: {_pct(ret1)}, closed in the bottom quarter of the day")
    elif f["rsi"] <= 25 or f["dist20_atr"] <= -3:
        add("stretched", 1, f["dist20_atr"] / 3, f"stretched down: RSI {f['rsi']:.0f}, {f['dist20_atr']:+.1f} ATR from the 20-day, no turn yet")
    elif f["rsi"] >= 75 or f["dist20_atr"] >= 3:
        add("stretched", -1, f["dist20_atr"] / 3, f"stretched up: RSI {f['rsi']:.0f}, {f['dist20_atr']:+.1f} ATR from the 20-day, no turn yet")
    # Moving now (the live screen only): outside regular hours on steady trading, or today on heavy volume.
    if x := f.get("live"):
        news = f"; {x['reported']}" if x.get("reported") else ""
        if x["phase"] != "regular" and abs(x["move_atr"]) >= LIVE_MOVE_ATR and x["bars"] >= LIVE_MIN_BARS:
            add("live_move", 1 if x["move"] > 0 else -1, x["move_atr"], _live_text(x) + news)
        elif x["phase"] == "regular" and abs(move_atr) >= LIVE_MOVE_ATR and f["vol_ratio"] >= LIVE_HEAVY:
            add("live_move", 1 if ret1 > 0 else -1, move_atr, f"{_pct(ret1)} today ({move_atr:+.1f} ATR) at {live.clock(x['at'])} on "
                f"{f['vol_ratio']:.1f}x volume{', projected to a full day' if f.get('partial') else ''}{news}")
    return hits


def _spread_hits(name: str, spread: pd.Series, stretched_setup: str, big_setup: str | None) -> tuple[list[dict], dict]:
    """Setups on a spread's daily returns (a basket against its hedge, or a factor pair)."""
    spread = spread.dropna()
    idx = (1 + spread).cumprod()
    sd = spread.iloc[-21:-1].std()
    today = spread.iloc[-1]
    mean50, std50 = idx.rolling(50).mean(), idx.rolling(50).std()
    z = ((idx - mean50) / std50).iloc[-2:]
    z_prev, z_now = float(z.iloc[0]), float(z.iloc[1])
    before = spread.iloc[-61:-11].std() if len(spread) >= 61 else None
    tightness = float(spread.iloc[-11:-1].std() / before) if before else None
    nums = {"today": today, "sd_20d": sd, "today_sd": today / sd if sd else None, "tightness": tightness, "ret_5d": idx.iloc[-1] / idx.iloc[-6] - 1,
            "ret_20d": idx.iloc[-1] / idx.iloc[-21] - 1, "z_50d": z_now, "z_50d_prev": z_prev,
            "new_20d_high": bool(idx.iloc[-1] > idx.iloc[-21:-1].max()), "new_20d_low": bool(idx.iloc[-1] < idx.iloc[-21:-1].min())}
    base = f"{name}: {_pct(today)} today ({today / sd:+.1f} sd), 5-day {_pct(nums['ret_5d'])}, 20-day {_pct(nums['ret_20d'])}, z {z_now:+.1f}"
    if tightness is not None:
        base += f", the 10 sessions before today {tightness:.2f}x as volatile as the 50 before"
    hits = []
    if big_setup:  # baskets
        if nums["new_20d_high"] and today >= sd:
            hits.append({"setup": "basket_breakout", "direction": "long", "strength": today / sd, "why": f"{base}; a 20-day high"})
        elif nums["new_20d_low"] and today <= -sd:
            hits.append({"setup": "basket_breakdown", "direction": "short", "strength": -today / sd, "why": f"{base}; a 20-day low"})
        elif abs(today) >= 2 * sd:
            hits.append({"setup": big_setup, "direction": "long" if today > 0 else "short", "strength": abs(today) / sd, "why": base})
    else:  # factor pairs: a stretch that turned against itself
        if abs(z_prev) >= 2 and np.sign(today) == -np.sign(z_prev) and abs(today) >= sd:
            hits.append({"setup": "factor_turn", "direction": "long" if today > 0 else "short", "strength": abs(z_prev),
                         "why": f"{base}; was stretched at z {z_prev:+.1f} and turned"})
    if not hits and abs(z_now) >= 2:
        hits.append({"setup": stretched_setup, "direction": "long" if z_now < 0 else "short", "strength": abs(z_now) / 2,
                     "why": f"{base}; stretched, no turn yet"})
    return hits, nums


def _names() -> list[str]:
    return sorted({*screen_universe(), *_groups(), *earnings.symbols()} - set(BARS_ONLY))


def build() -> dict:
    """The nightly screen, on settled daily bars."""
    return _run(bars.load(_names()))


def _with_live(frame: pd.DataFrame, snaps: dict[str, dict]) -> tuple[pd.DataFrame, dict[str, dict], dict[str, dict]]:
    """(the daily bars with today's bar so far added, symbol -> its trading outside regular hours, symbol -> how
    far into the session its bar is). A day whose stored bar was pulled after the close keeps that bar."""
    last = frame.groupby("symbol")["date"].max()
    add, drop, partial = [], [], {}
    for sym, snap in snaps.items():
        if sym not in last.index:
            continue
        day = pd.Timestamp(snap["date"])
        bar = live.provisional_bar(snap)
        if bar is None or day < last[sym] or (day == last[sym] and journal._bars_settled(snap["date"])):
            continue
        if day == last[sym]:   # a partial bar stored by a dashboard refresh during the session
            drop.append((sym, day))
        add.append({**bar, "symbol": sym})
        if snap["regular"]["minutes"] < live.SESSION_MINUTES:
            partial[sym] = {"minutes": snap["regular"]["minutes"], "fraction": snap["regular"]["fraction"]}
    if drop:
        gone = pd.MultiIndex.from_tuples(drop)
        frame = frame[~pd.MultiIndex.from_frame(frame[["symbol", "date"]]).isin(gone)]
    if add:
        frame = pd.concat([frame, pd.DataFrame(add)], ignore_index=True)
    return frame, snaps, partial


def _live_of(sym: str, f: dict, snap: dict | None, reports: dict) -> dict | None:
    """One name's latest price against its last regular close, for the live screen: after-hours once its day's
    bar is complete, pre-market before a session has a bar, otherwise the session's own move so far."""
    if not snap or snap["date"] < f["dates"][-1]:
        return None
    same_day = snap["date"] == f["dates"][-1]
    if same_day and snap.get("post"):
        phase, ext = "post", snap["post"]
    elif not same_day and snap.get("pre") and not snap.get("regular"):
        phase, ext = "pre", snap["pre"]
    elif same_day and snap.get("regular"):
        phase, ext = "regular", None
    else:
        return None
    price = ext["last"] if ext else snap["regular"]["close"]
    move = price / f["close"] - 1 if ext else f["ret_1d"]
    atrp = f["atr_pct"] if ext else f["atr_pct_prev"]
    out = {"phase": phase, "price": price, "at": snap["at"], "move": move, "move_atr": move / atrp if atrp else 0.0,
           "bars": ext["bars"] if ext else 0}
    today = snap["date"].isoformat()
    said = next((r for r in reports.get(sym, []) if r["date"] == today), None)
    if said:
        out["reported"] = f"reported today {said['session'].replace('_', ' ')}" + (
            f", EPS surprise {said['surprise_pct']:+.0f}%" if said.get("surprise_pct") is not None else "")
    return out


def _gateway_overnight(symbols: list[str]) -> tuple[dict, dict]:
    """(symbol -> bid, ask; symbol -> why none) on IBKR's overnight venue, SPY included."""
    import asyncio

    from .paper.gateway import CANARY, IBMarket

    async def ask():
        async with IBMarket(client_id=OVERNIGHT_CLIENT) as m:
            await m.quotes(sorted({*symbols, CANARY}), overnight=True, tries=2, need_books=True)
            return m.books, m.missing
    try:
        return asyncio.run(ask())
    except Exception as e:  # noqa: BLE001 - no check means nothing dropped, said in the screen
        return {}, {s: f"the gateway couldn't be asked ({type(e).__name__})" for s in symbols}


class OvernightCheck:
    """Called with each shortlist; returns the candidates to drop, {name: why}. `books` stands in for the gateway:
    a function from symbols to (books, missing), as _gateway_overnight."""

    def __init__(self, books=None):
        self.ask = books or _gateway_overnight
        self.books: dict = {}
        self.missing: dict = {}
        self.unchecked: str | None = None

    def _quote(self, symbols: set[str]) -> None:
        from .paper.gateway import CANARY
        todo = sorted(symbols - set(self.books) - set(self.missing))
        if not todo or self.unchecked:
            return
        books, missing = self.ask(todo)
        if CANARY not in self.books and not books.get(CANARY):
            self.unchecked = (missing.get(CANARY) or next(iter(missing.values()), None)
                              or "the gateway sent no quote for SPY")
            return
        self.books.update(books)
        self.missing.update({s: w for s, w in missing.items() if s not in books})

    def spread(self, symbol: str) -> float | None:
        from .paper import rules
        return rules.spread_pct(self.books.get(symbol))

    def __call__(self, top: list[dict]) -> dict[str, dict]:
        from .paper import rules
        from .paper.gateway import no_market
        legs = {}
        for c in top:
            sides = rules._sides(c)
            if sides is None:
                continue
            hedge = None if c["kind"] == "factor" else (c.get("hedge") or "SPY")
            own = [x for x in (*sides[0], *sides[1]) if x != hedge or x == c["name"]]
            legs[c["name"]] = (own, hedge if hedge and hedge not in own else None)
        self._quote({x for own, h in legs.values() for x in (*own, *([h] if h else []))})
        if self.unchecked:
            return {}
        drop = {}
        for c in top:
            if c["name"] not in legs:
                continue
            own, hedge = legs[c["name"]]
            wide = {x: v for x in own if (v := self.spread(x)) is not None and v > rules.MAX_CROSS_SPREAD}
            none = [x for x in own if x in self.missing and no_market(self.missing[x])]
            if wide or none:
                why = [f"{x} {v:.1%} wide" for x, v in wide.items()] + [f"{x} has no overnight bid and ask" for x in none]
                drop[c["name"]] = {"name": c["name"], "kind": c["kind"], "setup": c["setup"], "direction": c["direction"],
                                   "why": "; ".join(why)}
                continue
            c["overnight"] = {x: _pct_or_none(self.spread(x)) for x in own}
            if hedge:
                hs = self.spread(hedge)
                c["overnight_hedge"] = {"symbol": hedge, "spread": _pct_or_none(hs),
                                        "tradeable": hs is not None and hs <= rules.MAX_CROSS_SPREAD}
        return drop

    def report(self, dropped: dict[str, dict]) -> dict:
        if self.unchecked:
            return {"checked": False, "why": f"overnight spreads weren't checked: {self.unchecked}", "dropped": []}
        return {"checked": True, "quoted": len(self.books), "dropped": list(dropped.values())}


def _pct_or_none(x: float | None) -> str | None:
    return None if x is None else f"{x:.2%}"


def build_live(slot: str, now: datetime | None = None, snaps: dict[str, dict] | None = None, books=None) -> dict:
    """The screen for one of the three daily passes, on the latest prices. `snaps` stands in for the Yahoo pull and
    `books` for the night screen's overnight quotes (OvernightCheck)."""
    from .paper import proposals

    if slot not in SLOTS:
        raise ValueError(f"slot must be one of: {', '.join(SLOTS)}")
    now = now or datetime.now(NY)
    session = proposals.pass_session(slot, now)
    names = _names()
    snaps = live.fetch(names) if snaps is None else snaps
    if "SPY" not in snaps:
        raise RuntimeError("no live prices from Yahoo (SPY is missing), so there is no live screen")
    frame, snaps, partial = _with_live(bars.load(names), snaps)
    check = (OvernightCheck(books) if slot == "night" else None)
    payload = _run(frame, {"slot": slot, "session": session, "snaps": snaps, "partial": partial, "check": check})
    spy = snaps["SPY"]
    payload.update({"session": session.isoformat(), "slot": slot, "live": True, "phase": spy["phase"], "prices_at": spy["at"],
                    "session_minutes": (spy.get("regular") or {}).get("minutes") if spy["phase"] == "regular" else None,
                    "priced": len(snaps)})
    return payload


def _run(frame: pd.DataFrame, live_: dict | None = None) -> dict:
    groups = _groups()
    baskets = basket_library()["baskets"]
    as_of_ts = frame.loc[frame["symbol"] == "SPY", "date"].max()
    as_of = as_of_ts.date()
    frame = frame[frame["date"] <= as_of_ts]
    days = _Days(as_of)
    snaps, partial = (live_["snaps"], live_["partial"]) if live_ else ({}, {})
    after_close = bool(live_) and snaps["SPY"]["phase"] == "post"
    reports = earnings.load().get("names", {})
    close = frame.pivot_table(index="date", columns="symbol", values="close").sort_index()
    rets = close.pct_change()
    spy_ret1 = float(rets["SPY"].iloc[-1])
    r60 = rets.iloc[-60:]

    found, feats, held_back = [], {}, []
    for sym, df in frame.groupby("symbol"):
        if df["date"].max() != as_of_ts:
            continue
        f = _features(df)
        if f is None:
            continue
        if live_:
            f["partial"] = partial.get(sym)
            f["live"] = _live_of(sym, f, snaps.get(sym), reports)
        feats[sym] = f
        is_etf = sym in ETFS
        if not is_etf and f["usd_volume"] < MIN_DOLLAR_VOLUME:
            continue
        hedge, fit = _pick_hedge(sym, r60, groups[sym][0]["hedge"] if sym in groups else None)
        hits = _symbol_hits(sym, f, hedge, rets[hedge] if hedge in rets else rets["SPY"], spy_ret1, reports, days)
        if not hits:
            continue
        hits.sort(key=lambda x: -_score(x))
        best = hits[0]
        verdict, nums = _verdict(f)
        if text := _fit_text(hedge, fit):
            verdict += f"; {text}"
        nxt = _next_report(sym, reports, days) if not is_etf else None
        cand = {"name": sym, "kind": "etf" if is_etf else "stock", **best, "hedge": None if sym == "SPY" else hedge, "hedge_fit": fit,
                "baskets": [b["name"] for b in groups.get(sym, [])], "chart": verdict, "numbers": nums,
                "next_earnings": {k: v for k, v in nxt.items() if k != "reacts"} if nxt else None,
                "also": [f"{h['setup']} ({h['direction']})" for h in hits[1:]]}
        # A report dated today after the close is out by the time the night pass runs: the name is judged on its reaction.
        out_already = after_close and nxt and nxt["date"] == as_of.isoformat() and nxt["when"] == "after_close"
        if nxt and nxt["sessions_away"] is not None and nxt["sessions_away"] <= 1 and best["setup"] not in EARNINGS_SETUPS \
                and not out_already:
            held_back.append({**cand, "held_back": f"reports {nxt['date']} {nxt['when'].replace('_', ' ')}, inside the next session"})
            continue
        found.append(cand)

    for b in baskets:
        members = [m["symbol"] for m in b["members"]]
        if any(s not in feats for s in members) or b["hedge"] not in rets:
            continue
        spread = rets[members].mean(axis=1, skipna=False) - rets[b["hedge"]]
        hits, nums = _spread_hits(f"{b['name']} against {b['hedge']}", spread.iloc[-260:], "basket_stretched", "basket_big_move")
        if not hits:
            continue
        today = sorted(((s, feats[s]["ret_1d"]) for s in members), key=lambda x: x[1])
        up50 = sum(feats[s]["close"] > feats[s]["sma50"] for s in members)
        chart = (f"{up50} of {len(members)} members above their 50-day; best {today[-1][0]} {_pct(today[-1][1])}, "
                 f"worst {today[0][0]} {_pct(today[0][1])}; spread {nums['today_sd']:+.1f} sd today, z {nums['z_50d']:+.1f}")
        reporting = [s for s in members if (n := _next_report(s, reports, days)) and n["sessions_away"] is not None
                     and n["sessions_away"] <= 5]
        found.append({"name": b["name"], "basket_id": b["id"], "kind": "basket", "members": members, "hedge": b["hedge"],
                      **hits[0], "chart": chart, "numbers": journal._records(pd.DataFrame([nums]))[0],
                      "members_reporting_within_5": reporting, "also": [], "members_flagged": []})

    for p in FACTOR_PAIRS:
        if p.long not in rets or p.short not in rets:
            continue
        hits, nums = _spread_hits(f"{p.long}/{p.short} ({p.description})", (rets[p.long] - rets[p.short]).iloc[-260:],
                                  "factor_stretched", None)
        if hits:
            found.append({"name": f"{p.long}/{p.short}", "factor": p.name, "kind": "factor", "long": p.long, "short": p.short,
                          **hits[0], "chart": f"{p.description}; z {nums['z_50d']:+.1f} vs 50-day, 20-day {_pct(nums['ret_20d'])}",
                          "numbers": journal._records(pd.DataFrame([nums]))[0], "also": []})

    # A stock moving with its flagged basket is folded into the basket, unless its own news (earnings) is the setup.
    flagged = {c["name"]: c for c in found if c["kind"] == "basket"}
    kept = []
    for c in found:
        if c["kind"] == "stock" and c["setup"] not in EARNINGS_SETUPS:
            home = next((flagged[b] for b in c["baskets"] if b in flagged and flagged[b]["direction"] == c["direction"]), None)
            if home:
                home["members_flagged"].append(f"{c['name']}: {c['setup']}")
                continue
        kept.append(c)
    for c in kept:
        c["score"] = round(_score(c), 3)
        c["strength"] = round(c["strength"], 2)
    kept.sort(key=lambda c: -c["score"])
    counts: dict[str, int] = {}
    for c in kept:
        counts[c["setup"]] = counts.get(c["setup"], 0) + 1

    in_book, judged = _book_names(days.sessions, as_of), (_judged(live_["session"]) if live_ else None)
    check = (live_ or {}).get("check")
    dropped: dict[str, dict] = {}
    for _ in range(OVERNIGHT_ROUNDS if check else 1):
        again: list[dict] = []
        top, rest, held = _shortlist([c for c in kept if c["name"] not in dropped], r60, in_book, judged, again)
        if not check:
            break
        new = check(top)
        if not new:
            break
        dropped.update(new)

    liquid = {s for s, f in feats.items() if s not in ETFS and f["usd_volume"] >= MIN_DOLLAR_VOLUME}
    upcoming = []
    for s in liquid:
        n = _next_report(s, reports, days)
        if n and n["sessions_away"] is not None and n["sessions_away"] <= 5:
            upcoming.append({"symbol": s, "date": n["date"], "when": n["when"], "sessions_away": n["sessions_away"],
                             "baskets": [b["name"] for b in groups.get(s, [])], "usd_volume": round(feats[s]["usd_volume"])})
    upcoming.sort(key=lambda r: (r["sessions_away"], -r["usd_volume"]))

    brief_path = journal.JOURNAL / "briefs" / f"{as_of}.json"
    calendar = json.loads(brief_path.read_text()).get("forced_flows", {}).get("calendar", {}) if brief_path.exists() else {}
    events = [{"event": k, **v} for k, v in calendar.items() if isinstance(v, dict) and v.get("sessions_away", 99) <= 5]

    return {
        "as_of": as_of.isoformat(),
        "built_at": datetime.now(NY).isoformat(timespec="seconds"),
        "bars_settled": journal._bars_settled(as_of),
        "next_session": days.next.isoformat(),
        "screened": len(feats),
        "hits_by_setup": counts,
        "spy_ret_1d": round(spy_ret1, 5),
        "candidates": top,
        "more": [{k: c[k] for k in ("name", "kind", "setup", "direction", "score", "why")} for c in rest[:MORE]],
        "held_back": held_back,
        "in_book": held,
        "judged_recently": again,
        "earnings_ahead": upcoming[:40],
        "calendar_ahead": events,
        "regime": _regime_notes(as_of),
        **({"overnight": check.report(dropped)} if check else {}),
    }


def _regime_notes(as_of: date) -> list[str]:
    rows = [r for r in _regime(as_of) if r.get("pct_above_50") is not None]
    if len(rows) < 6:
        return []
    now, prev, wk = rows[-1], rows[-2], rows[-6]
    notes = []
    if (now["pct_above_50"] - 0.5) * (prev["pct_above_50"] - 0.5) < 0:
        notes.append(f"Breadth crossed 50%: {prev['pct_above_50']:.0%} to {now['pct_above_50']:.0%} of our names above their 50-day")
    if now.get("sector_corr_20d") is not None and wk.get("sector_corr_20d") is not None \
            and abs(now["sector_corr_20d"] - wk["sector_corr_20d"]) >= 0.08:
        notes.append(f"Sector correlation moved from {wk['sector_corr_20d']:.2f} to {now['sector_corr_20d']:.2f} in five sessions")
    ratio = lambda r: r["vix"] / r["vix3m"] if r.get("vix") and r.get("vix3m") else None  # noqa: E731
    if ratio(now) and ratio(prev) and (ratio(now) - 1) * (ratio(prev) - 1) < 0:
        notes.append(f"VIX/VIX3M crossed 1: {ratio(prev):.2f} to {ratio(now):.2f}")
    elif ratio(now) and ratio(now) >= 1:
        notes.append(f"VIX/VIX3M still inverted at {ratio(now):.2f}")
    return notes


def write() -> tuple[str, dict]:
    payload = build()
    path = journal.JOURNAL / "screens" / f"{payload['as_of']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, default=journal._json_default) + "\n")
    return str(path), payload


def live_path(session: date | str, slot: str):
    """Where a pass's live screen is kept. Its own folder, so the nightly screens stay one file a night."""
    return journal.JOURNAL / "screens" / "live" / f"{session}-{slot}.json"


def write_live(slot: str, now: datetime | None = None) -> tuple[str, dict]:
    payload = build_live(slot, now)
    path = live_path(payload["session"], slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, default=journal._json_default) + "\n")
    return str(path), payload


def summary(payload: dict) -> str:
    if payload.get("live"):
        what = {"post": "after-hours", "pre": "pre-market", "regular": "the session so far"}[payload["phase"]]
        lines = [f"Live screen for the {payload['slot']} pass, session {payload['session']}: prices at "
                 f"{live.clock(payload['prices_at'])} ({what}), {payload['screened']} names, {sum(payload['hits_by_setup'].values())} hits"]
    else:
        lines = [f"Screen for {payload['as_of']} (next session {payload['next_session']}): {payload['screened']} names, "
                 f"{sum(payload['hits_by_setup'].values())} hits"]
    for c in payload["candidates"]:
        lines.append(f"{c['score']:.2f}  {c['name']:<28} {c['setup']:<20} {c['direction']:<5}  {c['why']}")
    if payload["held_back"]:
        lines.append("Held back (earnings inside the next session): " + ", ".join(c["name"] for c in payload["held_back"]))
    if payload.get("in_book"):
        lines.append("Left out, already in the book: " + ", ".join(f"{c['name']} ({c['setup']} {c['direction']})" for c in payload["in_book"]))
    if payload.get("judged_recently"):
        lines.append("Left out, judged by a recent pass: " + ", ".join(c["name"] for c in payload["judged_recently"]))
    if (o := payload.get("overnight")) and not o["checked"]:
        lines.append(o["why"].capitalize())
    elif o and o["dropped"]:
        lines.append("Left out, can't trade overnight inside a 2% spread: " + ", ".join(f"{d['name']} ({d['why']})" for d in o["dropped"]))
    wide_hedge = [f"{c['name']} (hedge {c['overnight_hedge']['symbol']} {c['overnight_hedge']['spread'] or 'no quote'})"
                  for c in payload["candidates"] if c.get("overnight_hedge") and not c["overnight_hedge"]["tradeable"]]
    if wide_hedge:
        lines.append("Default hedge too wide overnight, choose another or none: " + ", ".join(wide_hedge))
    lines += payload["regime"]
    return "\n".join(lines)

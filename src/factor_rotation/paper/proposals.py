"""What a research pass records: one written verdict per candidate, and only the takes are traded.

    journal/paper/proposals/<session>.json

`<session>` is the session the trades enter for. Each of the three daily passes (daily.py) judges every candidate
on its live screen: take or pass, a conviction from 1 to 5, a rank, and for a take the five steps of reasoning, what
would prove it wrong, the legs and the plan (time limit and stop, inside the outer limits in rules.py). Code copies
the screen's numbers into each record and fills in the mechanical legs and plan for a pass, so the passes can be
scored the same way as the takes (learn.py). This module holds the checks every verdict goes through
(`_proposal`), the ranking rule, and which session a pass belongs to.

The pass only writes this file, through `fr paper propose`. It never sizes a trade or sends an order: the order
job (entries.py) does that under the broker's guards.
"""

import json
from datetime import date, datetime, time
from pathlib import Path

from .. import bars, earnings, journal
from ..universe import ETFS
from . import rules

DECISIONS = ("take", "pass")


def root() -> Path:
    return journal.JOURNAL / "paper"


def load(session: date) -> dict | None:
    path = root() / "proposals" / f"{session}.json"
    return json.loads(path.read_text()) if path.exists() else None


def every() -> list[dict]:
    """Every proposal ever recorded, oldest first."""
    return [p for path in sorted((root() / "proposals").glob("*.json")) for p in json.loads(path.read_text())["proposals"]]


# ---- The three daily passes (since the 2026-11-15 redesign) -------------------------------------------------
# Each pass screens the latest prices, judges, and its takes are traded as soon as it has recorded (entries.py).
# When each may record, New York time. A pass belongs to the session its trades enter for: the night pass to
# the next session (or today's, once it is past midnight), the two day passes to today's.
# A fourth kind, "late", is a second pass in the night window, run by hand (the owner, 2026-11-16): the same rules
# as the night pass, for names the passes before it did not judge. It can be run more than once a session (the owner,
# 2026-11-17): each run records under the next free name of LATE (`next_late`).
LATE = ("late", "late2", "late3", "late4")
NIGHT_KINDS = ("night", *LATE)
NEW_PASSES = (*NIGHT_KINDS, "morning", "midday")
NIGHT_FROM, NIGHT_UNTIL = time(16, 0), time(3, 50)     # the close to the end of IBKR's overnight session
MORNING_WINDOW = (time(9, 30), time(12, 30))
MIDDAY_WINDOW = (time(12, 30), time(15, 30))           # recorded in time for entries before the 3:40 PM cutoff


def pass_session(kind: str, now: datetime) -> date:
    """The session a pass run at `now` belongs to. Raises if that pass can't run now."""
    today, t = now.date(), now.time()
    open_day = journal.is_trading_day(today)
    if kind in NIGHT_KINDS:
        if t < NIGHT_UNTIL:
            return today if open_day else journal.next_trading_day(today)
        if t >= NIGHT_FROM or not open_day:
            return journal.next_trading_day(today)
        raise ValueError(f"the {kind} pass runs between the 3:00 PM CT close and 2:50 AM CT, not during the session")
    window = {"morning": MORNING_WINDOW, "midday": MIDDAY_WINDOW}.get(kind)
    if window is None:
        raise ValueError(f"pass must be one of: {', '.join(NEW_PASSES)}")
    if not open_day or not window[0] <= t <= window[1]:
        ct = lambda x: f"{(x.hour - 1) % 12 or 12}:{x.minute:02d} {'PM' if x.hour - 1 >= 12 else 'AM'}"  # noqa: E731
        raise ValueError(f"the {kind} pass runs on a trading day between {ct(window[0])} and {ct(window[1])} CT")
    return today


def next_late(now: datetime) -> str:
    """The name a late pass started at `now` records under: the first of LATE its session has not used."""
    used = {p["pass"] for p in (load(pass_session("late", now)) or {}).get("passes", [])}
    free = [k for k in LATE if k not in used]
    if not free:
        raise ValueError(f"the session already has {len(LATE)} late passes, the most it can hold")
    return free[0]


def _text(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


PASS_CASE = ("case_for", "case_against", "why_not")   # a pass's short case (since 2026-11-22)

def _side(v, who: str, side: str) -> list[str]:
    names = [v] if isinstance(v, str) else list(v or [])
    if any(not isinstance(s, str) or not journal.SYMBOL.match(s) for s in names) or len(set(names)) != len(names):
        raise ValueError(f"{who}: the {side} side must be a ticker symbol or a list of distinct ones")
    if len(names) > journal.BASKET_SIZE[1]:
        raise ValueError(f"{who}: the {side} side holds more than {journal.BASKET_SIZE[1]} names")
    if bad := sorted(set(names) & journal.NOT_TRADABLE):
        raise ValueError(f"{who}: {', '.join(bad)} is an index and can't be traded")
    return names


def _plan(plan, legs: list[str], who: str) -> dict:
    """A plan inside the outer limits: a time limit of 1 to MAX_HORIZON sessions and a stop on the trade's loss
    from entry between the two STOP_LIMITS, optionally with price levels on a leg's close."""
    if not isinstance(plan, dict):
        raise ValueError(f"{who}: a take needs a plan: {{\"horizon_days\": 3, \"stop\": {{\"spread_pct\": -4}}}}")
    h = plan.get("horizon_days")
    if not isinstance(h, int) or isinstance(h, bool) or not 1 <= h <= rules.MAX_HORIZON:
        raise ValueError(f"{who}: horizon_days must be 1-{rules.MAX_HORIZON} sessions, counting the first")
    stop = plan.get("stop")
    tight, wide = rules.STOP_LIMITS
    pct = stop.get("spread_pct") if isinstance(stop, dict) else None
    if not isinstance(pct, (int, float)) or isinstance(pct, bool) or not -wide <= pct <= -tight:
        raise ValueError(f"{who}: stop.spread_pct must be between -{tight:g} and -{wide:g}: the loss from entry, in percent, that ends the trade")
    out = {"horizon_days": h, "stop": {"spread_pct": float(pct)}}
    if stop.get("rules"):
        for r in stop["rules"]:
            sides = [k for k in ("below", "above") if isinstance(r, dict) and k in r]
            if len(sides) != 1 or r.get("symbol") not in legs or not isinstance(r[sides[0]], (int, float)) or r[sides[0]] <= 0:
                raise ValueError(f"{who}: stop rule {r!r} needs a leg's symbol and one positive below or above price")
        out["stop"]["rules"] = stop["rules"]
    return out


def reports_inside(symbols: list[str], session: date, horizon: int) -> list[str]:
    """Stock legs whose earnings reaction falls inside the time limit, as plain sentences."""
    from ..screen import _Days, _next_report

    stocks = [s for s in symbols if s not in ETFS]
    reports = earnings.load().get("names", {})
    if not stocks or not reports:
        return []
    days = _Days(journal.previous_trading_day(session))
    out = []
    for s in stocks:
        n = _next_report(s, reports, days)
        if n and n["sessions_away"] is not None and n["sessions_away"] <= horizon:
            out.append(f"{s} reports {n['date']} {n['when'].replace('_', ' ')}, which trades on session {n['sessions_away']}")
    return out


def pair_stats(longs: list[str], shorts: list[str]) -> dict | None:
    """How well the short side hedges the long side: correlation and beta of daily returns over 60 sessions.
    None for a one-sided trade or when the bars aren't there."""
    if not longs or not shorts:
        return None
    try:
        df = bars.load([*longs, *shorts])
        rets = df.pivot_table(index="date", columns="symbol", values="close").sort_index().pct_change().iloc[-60:]
        a, b = rets[longs].mean(axis=1, skipna=False), rets[shorts].mean(axis=1, skipna=False)
    except Exception:
        return None
    both = (a.notna() & b.notna())
    if both.sum() < 30 or not b[both].var():
        return None
    a, b = a[both], b[both]
    return {"corr_60d": round(float(a.corr(b)), 2), "beta_60d": round(float(a.cov(b) / b.var()), 2), "sessions": int(both.sum())}


def _signal(c: dict) -> dict:
    """What the screen saw, kept with the verdict so the two can be compared later."""
    return {k: c[k] for k in ("why", "chart", "numbers", "score", "adjust", "hedge", "hedge_fit", "moved_with", "members",
                                 "next_earnings", "also", "door", "route", "primary_links") if k in c}


def _from_screen(c: dict, rank: int, screen: dict) -> dict:
    from ..screen import route_of
    sides = rules._sides(c)
    source = f"live screen {screen['session']} {screen['slot']} #{rank}" if screen.get("live") else f"screen {screen['as_of']} #{rank}"
    door, route = (c["door"], c["route"]) if c.get("door") and c.get("route") else route_of(c["setup"], c["kind"])
    return {"name": c["name"], "kind": c["kind"], "setup": c["setup"], "door": door, "route": route,
            "direction": c["direction"], "source": source, "signal": _signal(c),
            "long": sides[0] if sides else [], "short": sides[1] if sides else [], "plan": rules.default_plan(c) if sides else None}


def _proposal(raw: dict, session: date, screen: dict | None, cands: dict) -> dict:
    who = str(raw.get("candidate") or raw.get("name") or "a proposal")
    if raw.get("candidate"):
        if raw["candidate"] not in cands:
            held = next((c for c in (screen or {}).get("in_book", []) if c["name"] == raw["candidate"]), None)
            if held:
                raise ValueError(f"{who} was left out of the screen for {session}: {held['in_book']}. Leave it out of the draft; "
                                 "every pass reviews the trades the book holds")
            raise ValueError(f"{who} is not a candidate on the screen for {session}. Record it as your own idea: "
                             "name, setup, direction, long and/or short")
        p = _from_screen(cands[raw["candidate"]][1], cands[raw["candidate"]][0], screen)
    else:
        if not _text(raw.get("name")) or raw.get("setup") not in journal.SETUPS or raw.get("direction") not in ("long", "short"):
            raise ValueError(f"{who}: an own idea needs name, direction (long or short) and setup, one of: {', '.join(journal.SETUPS)}")
        p = {"name": raw["name"].strip(), "kind": "own", "setup": raw["setup"], "door": "own", "route": "own",
             "direction": raw["direction"], "source": "own", "signal": None, "long": [], "short": [], "plan": None}
    if raw.get("decision") not in DECISIONS:
        raise ValueError(f"{who}: decision must be take or pass")
    c = raw.get("conviction")
    if not isinstance(c, int) or isinstance(c, bool) or not 1 <= c <= 5:
        raise ValueError(f"{who}: conviction must be 1-5, how strongly you believe the trade works, for a pass too")
    if not _text(raw.get("reason")):
        raise ValueError(f"{who}: give the reason for the verdict in a sentence")
    take = raw["decision"] == "take"
    if take and c < rules.MIN_CONVICTION:
        raise ValueError(f"{who}: a take needs conviction {rules.MIN_CONVICTION} or more; below that it is a pass")
    if "hedge" in raw:  # the candidate against a different fund, or with "hedge": null, on its own
        c0 = cands[raw["candidate"]][1] if raw.get("candidate") else None
        if not c0 or c0["kind"] == "factor" or (c0["kind"] == "basket" and not raw["hedge"]):
            raise ValueError(f"{who}: hedge swaps the fund against a stock, ETF or basket candidate; otherwise give long and short")
        hedge = _side(raw["hedge"], who, "hedge")[:1]
        sides = rules._sides({**c0, "hedge": hedge[0] if hedge else c0["name"]})
        if sides is None:
            raise ValueError(f"{who}: fewer than 3 basket members are left after dropping the ones reporting; give long and short")
        p["long"], p["short"] = sides
    for side in ("long", "short"):  # a side the pass gives replaces that side; "short": [] drops it
        if side in raw:
            p[side] = _side(raw[side], who, side)
    if set(p["long"]) & set(p["short"]):
        raise ValueError(f"{who}: a name is on both sides")
    legs = [*p["long"], *p["short"]]
    if "plan" in raw or take:
        if not legs:
            raise ValueError(f"{who}: give the legs (long and/or short); the screen has no tradable default for it")
        p["plan"] = _plan(raw.get("plan"), legs, who)
    p.update({"decision": raw["decision"], "conviction": c, "reason": raw["reason"].strip(), "reviewed": True,
              "reasoning": None, "wrong_if": None, "hedge_why": None, "through_earnings": None})
    if take:
        reasoning = raw.get("reasoning")
        if not isinstance(reasoning, dict) or any(not _text(reasoning.get(k)) for k in journal.REASONING):
            raise ValueError(f"{who}: a take needs reasoning with each of: {', '.join(journal.REASONING)}")
        if not _text(raw.get("wrong_if")):
            raise ValueError(f"{who}: a take needs wrong_if: what you would see, inside the time limit, that proves it wrong")
        if not _text(raw.get("hedge_why")):   # every take, hedged or outright (the owner, 2026-11-16)
            raise ValueError(f"{who}: a take needs hedge_why: why it is better hedged against this fund than outright, or "
                             "outright than hedged (the S&P 500 is the benchmark)")
        if (inside := reports_inside(legs, session, p["plan"]["horizon_days"])) and not _text(raw.get("through_earnings")):
            raise ValueError(f"{who}: {'; '.join(inside)}, inside the {p['plan']['horizon_days']}-session time limit. Shorten the "
                             "time limit, or give through_earnings saying why the trade holds through the report")
        p.update({"reasoning": {k: reasoning[k].strip() for k in journal.REASONING}, "wrong_if": raw["wrong_if"].strip(),
                  "hedge_why": (raw.get("hedge_why") or "").strip() or None, "through_earnings": (raw.get("through_earnings") or "").strip() or None})
    else:
        # Every pass keeps a short case, for the record (the owner, 2026-11-22): the best case for it, the case
        # against, and why it didn't make the cut. No pass reads them; they are kept for later study.
        case = raw.get("pass_case")
        if not isinstance(case, dict) or any(not _text(case.get(k)) for k in PASS_CASE):
            raise ValueError(f"{who}: a pass needs pass_case with each of: {', '.join(PASS_CASE)} (a sentence or two each)")
        p["pass_case"] = {k: case[k].strip() for k in PASS_CASE}
        if isinstance(raw.get("reasoning"), dict):
            p["reasoning"] = {k: str(v).strip() for k, v in raw["reasoning"].items() if k in journal.REASONING and _text(v)}
    p["stats"] = pair_stats(p["long"], p["short"])
    return p


def _ranks(raws: list[dict]) -> list[int]:
    """The pass's ranking of the verdicts in one draft: 1 is the idea it believes in most. Every verdict is ranked,
    passes too, so each pass gives a whole list to score against the ideas' plan results (learn.rank_table) and not
    just a take or two."""
    given = [r.get("rank") for r in raws]
    n = len(raws)
    if any(not isinstance(g, int) or isinstance(g, bool) for g in given) or sorted(given) != list(range(1, n + 1)):
        raise ValueError(f"rank every verdict in the draft from 1 (the idea you believe in most) to {n} (the least), passes and "
                         "own ideas too, with no ties and no gaps: the ranking is scored against each idea's plan result")
    return given


def reviews(session: date) -> dict[str, dict]:
    """The latest decision on each trade for a session, by trade id: each pass's reviews in the order the passes
    recorded (daily.record)."""
    path = root() / "pass_reviews" / f"{session}.json"
    out = {}
    for p in json.loads(path.read_text())["passes"] if path.exists() else []:
        out.update({r["trade_id"]: {**r, "pass": p["pass"]} for r in p["reviews"]})
    from . import book   # the passes review the swing book's trades; the small book's twin of each gets the same decision
    for small, main in book.twins().items():
        if main in out and small not in out:
            out[small] = {**out[main], "trade_id": small}
    return out

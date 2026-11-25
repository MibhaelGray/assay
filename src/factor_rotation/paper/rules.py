"""The paper book's rules, as plain functions with no broker or file access, so they can be tested alone.

A trade is a dict:
    {"id", "book", "source", "proposal_id", "conviction", "name", "setup", "direction", "why", "wrong_if",
     "entry_date", "horizon_days", "stop": {"spread_pct": -3.0} and/or {"rules": [...]},
     "notional": 30000, "sizing": {...}, "legs": [{"symbol", "side": "long" | "short", "shares", "ref_price"}], "hedge"}

Each side of a trade gets `notional` dollars: a pair is that much long and that much short, an outright trade
that much on one side, and a basket side splits it evenly across its members. The swing book sizes each trade by
its risk (see side_dollars); the first book gave every trade a flat $10,000. Results are quoted in bp of one
side's notional (long return minus short return). `book` is always "dynamic", the automatic book's name
in the files; the selective book, which traded the recorded ideas as they stood, was retired on 2026-11-09.
"""

import math

from ..universe import ETFS

# Sizing and caps. Paper money, but the caps keep a bug from building a silly book. The book trades only what
# the research pass takes (proposals.py), so it holds few trades and each is large enough that the broker's $1
# minimum commission is about 4 bp a round trip, not the 16 bp it costs at $2,500 a side.
NOTIONAL = 10_000          # dollars per side: the first book's flat size, and the size when no capital is given
# The swing book's sizing (the owner, 2026-11-16). Every trade loses the same share of the book if it hits its
# stop, and a stronger idea risks more: dollars a side = capital x RISK x CONVICTION_RISK / the stop. So a jumpy
# name with an 8% stop gets less than a quiet one with a 3% stop, and a conviction 5 twice a conviction 3. At
# RISK 0.15% a typical trade (conviction 3, 5% stop) is 3% of the book a side, and the book is full, long plus
# short, at about 16 of them. In shares of capital, so the same rule sizes a $15,000 account.
RISK = 0.0015                                  # of capital, lost at the stop by a conviction-3 trade
CONVICTION_RISK = {3: 1.0, 4: 1.5, 5: 2.0}     # multiples of RISK
MAX_SIDE = 0.06                                # of capital: the most on one side, however tight the stop
MAX_ADV = 0.0025                               # of a leg's average daily dollar volume: never large for its stock
MAX_GROSS_SHARE = 1.5                          # of capital: long plus short (~0.75x each side; leaves buffer under 2x Reg-T)
MAX_SYMBOL_SHARE = 0.10                        # of capital: net exposure to one symbol, hedges across trades included
MAX_OPEN = 60              # open trades
MAX_GROSS = 900_000        # dollars, long plus short
MAX_SYMBOL = 30_000        # dollars of net exposure to one symbol
MIN_CONVICTION = 3         # a take below this is a pass
# The outer limits on a plan the research pass writes. Inside them the plan is the pass's call; the pass can
# end a trade early at the pre-close review but can never widen a stop, and can extend only a winner, once.
# How every order outside the regular session is priced, in both books (the user's rule, 2026-11-12): to fill at
# once, a buy goes in at the ask and a sell at the bid, with the limit a little beyond so it fills even if the
# quote moves. The one exception is a book wider than a limit, as a share of the midpoint (see crossable).
# Crossing costs about half the spread on the way in. The swing book's exits are in regular hours, where spreads
# are tiny, so a 2% spread costs it about 1% against stops of 4-8% and moves over days. The intraday book's
# overnight trades pay on the way out too, so its limit is half that (intraday/extended.py).
# The user's numbers, 2026-11-12: swing 2%, intraday 1%. (Briefly 1% and 0.5% that day, which on IBKR's quotes
# would have held back all three of the night's off-hours entries, sending two to the open.)
# The owner, 2026-11-16 (after an hour at 1.5%): 2% stands, since a trade may be held up to ten sessions. Inside it,
# hit the bid or the ask; over it, send nothing, and nothing is ever left resting (entries.py cancels what an order
# didn't fill at once). If one leg of a trade can't be hit, none of its legs is sent.
MAX_CROSS_SPREAD = 0.02
# Overnight entries on IBKR's thin OVERNIGHT venue pay far wider spreads than regular hours (~67 bp vs ~12 bp),
# so overnight entries only cross inside MAX_OVERNIGHT_SPREAD; wider or illiquid night takes queue for 8:35 AM CT
# in regular hours instead of being passed on permanently (2026-11-23).
MAX_OVERNIGHT_SPREAD = 0.005
# Regular-hours entries share the same 0.5% cap (2026-11-25, after MMS crossed a 1.43% morning book); wider books
# retry briefly in case the spread tightens before standing down. Exits keep MAX_CROSS_SPREAD so a trade isn't stuck.
MAX_REGULAR_SPREAD = 0.005


def spread_pct(book_: tuple[float, float] | None) -> float | None:
    """The bid-ask spread as a share of the midpoint, or None without a usable book."""
    if not book_:
        return None
    bid, ask = book_
    mid = (bid + ask) / 2
    return (ask - bid) / mid if bid > 0 and ask >= bid else None


def crossable(book_: tuple[float, float] | None, limit: float = MAX_CROSS_SPREAD) -> bool:
    """Whether an order should cross this book: a usable quote no wider than `limit`."""
    s = spread_pct(book_)
    return s is not None and s <= limit


MAX_HORIZON = 10           # sessions, counting the first
STOP_LIMITS = (1.0, 8.0)   # percent loss from entry: no tighter than the first, no wider than the second
MAX_EXTENSION = 3          # sessions

# How long each setup is held and how far the spread may fall before it is stopped, as a multiple of the
# trade's typical daily move (ATR % for a stock, the spread's daily sd for a basket or factor pair). The
# earnings horizon matches the drift measured in the gap table (next open through four sessions).
HORIZON = {
    "edgar_8k": 5, "edgar_13d": 5, "edgar_late_filing": 5, "edgar_insider_buy": 5,
    "earnings_gap_held": 4, "earnings_gap_faded": 4, "earnings_report": 4, "peer_readthrough": 3,
    "breakout": 5, "breakdown": 5, "reclaim_200": 5,
    "lost_200": 5, "relative_strength": 3, "relative_weakness": 3, "turn": 3, "stretched": 3,
    "basket_breakout": 5, "basket_breakdown": 5, "basket_big_move": 2, "basket_stretched": 3,
    "factor_turn": 5, "factor_stretched": 3, "live_move": 3, "overnight_move": 3,
}
STOP_MOVES = 1.5
STOP_RANGE = (2.0, 8.0)    # percent
DEFAULT_STOP = 3.0         # percent, when the screen gives no daily move


def _stop_pct(daily_move: float | None) -> float:
    if not daily_move or math.isnan(daily_move):
        return -DEFAULT_STOP
    lo, hi = STOP_RANGE
    return -round(min(hi, max(lo, STOP_MOVES * daily_move * 100)), 1)


def default_plan(c: dict) -> dict:
    """The mechanical plan for a screen candidate: its setup's time limit and a stop sized on its daily move.
    A candidate the research pass passes on is scored against this plan."""
    n = c.get("numbers") or {}
    daily = n.get("atr_pct") if c["kind"] in ("stock", "etf") else n.get("sd_20d")
    return {"horizon_days": HORIZON.get(c["setup"], 3), "stop": {"spread_pct": _stop_pct(daily)}}


def _sides(c: dict) -> tuple[list[str], list[str]] | None:
    """(long symbols, short symbols) for a screen candidate, or None if it can't be traded as a pair."""
    long_dir = c["direction"] == "long"
    if c["kind"] == "factor":
        a, b = c["long"], c["short"]
        return ([a], [b]) if long_dir else ([b], [a])
    if c["kind"] == "basket":
        reporting = set(c.get("members_reporting_within_5") or [])
        members = [m for m in c["members"] if m not in reporting]
        if len(members) < 3:
            return None
        return (members, [c["hedge"]]) if long_dir else ([c["hedge"]], members)
    name, hedge = c["name"], c.get("hedge") or "SPY"
    if name == hedge:
        return ([name], []) if long_dir else ([], [name])
    return ([name], [hedge]) if long_dir else ([hedge], [name])


def size_legs(longs: list[str], shorts: list[str], prices: dict[str, float], notional: float = NOTIONAL) -> list[dict] | None:
    """Whole-share legs for each side; None if a price is missing or a leg rounds to zero shares."""
    legs = []
    for side, names in (("long", longs), ("short", shorts)):
        for s in names:
            px = prices.get(s)
            if not px or px <= 0:
                return None
            shares = math.floor(notional / len(names) / px)
            if shares < 1:
                return None
            legs.append({"symbol": s, "side": side, "shares": shares, "ref_price": round(px, 4)})
    return legs


def side_dollars(conviction: int, stop_pct: float, capital: float, longs: list[str], shorts: list[str],
                 volumes: dict[str, float] | None = None) -> tuple[int, dict]:
    """Dollars for each side of a take, and how they were set. `stop_pct` is the plan's stop (a loss, so its
    sign doesn't matter); `volumes` is each leg's average daily dollar volume, where known. The size is the
    smallest of: the risk budget over the stop, MAX_SIDE of capital, and MAX_ADV of the thinnest leg's volume
    (a basket side splits its dollars, so each member counts for its share). Rounded down to $100 on a $1,000,000
    book and in proportion on a smaller one ($1 on $25,000), so a small book's sizes aren't rounded away."""
    risk = capital * RISK * CONVICTION_RISK[max(min(conviction, 5), 3)]
    limits = {"risk": risk / (abs(stop_pct) / 100), "side cap": capital * MAX_SIDE}
    for side in (longs, shorts):
        for s in side:
            if v := (volumes or {}).get(s):
                limits[f"liquidity: {s}"] = MAX_ADV * v * len(side)
    by = min(limits, key=limits.get)
    step = 100 if capital >= 250_000 else max(1, 10 ** math.floor(math.log10(capital)) // 10_000)
    return math.floor(limits[by] / step) * step, {"risk_dollars": round(risk), "stop_pct": -abs(stop_pct), "limited_by": by}


# Entering a trade with more than one leg (the owner, 2026-11-16, after the night the ARKK hedge of a Red Cat short
# filled in seconds and the short took ninety minutes). No leg may get ahead of the leg that is hardest to fill:
# that leg, the lead, is ordered in full, and every other leg is only ever ordered up to the share of the lead that
# has filled. A stuck lead then means nothing happened, not that the book holds a hedge with nothing against it.
FOLLOW_STEP = 0.10         # a follower's top-up goes out once it is this share of its leg, or completes it


def lead_leg(trade: dict, volumes: dict[str, float] | None = None) -> str:
    """The leg hardest to fill: the one whose dollars are largest against its average daily dollar volume. A leg
    with no volume on record counts as the hardest. Ties go to the trade's own names over its hedge fund, then to
    the order the legs are in."""
    core = core_symbols(trade)

    def ease(item: tuple[int, dict]) -> tuple[float, bool, int]:
        i, leg = item
        v = (volumes or {}).get(leg["symbol"])
        return (-(leg["shares"] * leg["ref_price"] / v) if v else -math.inf, leg["symbol"] not in core, i)
    return min(enumerate(trade["legs"]), key=ease)[1]["symbol"]


def entry_due(trade: dict, filled: dict[str, int], committed: dict[str, int] | None = None, lead: bool = True) -> list[tuple[dict, int]]:
    """(leg, shares) to order now. The lead (trade["lead"]) gets whatever of it is left. Every other leg is
    brought up to the lead's filled share, rounded down, once the top-up is FOLLOW_STEP of the leg or the lead is
    done. `filled` is shares filled by symbol; `committed` is what the followers already have or have on order
    (default: `filled`), and `lead=False` leaves the lead out, for a second round after its order has gone."""
    committed = committed or filled
    head = next(leg for leg in trade["legs"] if leg["symbol"] == trade["lead"])
    share = min(filled.get(head["symbol"], 0) / head["shares"], 1.0)
    out = []
    for leg in trade["legs"]:
        if leg is head:
            if lead and (n := leg["shares"] - filled.get(leg["symbol"], 0)) > 0:
                out.append((leg, n))
            continue
        target = leg["shares"] if share >= 1 else math.floor(leg["shares"] * share)
        n = target - committed.get(leg["symbol"], 0)
        if n >= 1 and (target == leg["shares"] or n >= leg["shares"] * FOLLOW_STEP):
            out.append((leg, n))
    return out


def core_symbols(trade: dict) -> set[str]:
    """The symbols a trade is about, without a hedge ETF on the other side."""
    sides = {s: {l["symbol"] for l in trade["legs"] if l["side"] == s} for s in ("long", "short")}
    if trade.get("hedge") and len(sides["long"]) + len(sides["short"]) > 1:
        return (sides["long"] | sides["short"]) - {trade["hedge"]}
    return sides["long"] | sides["short"]


def _hedge(longs: list[str], shorts: list[str]) -> str | None:
    """The side that is one ETF hedging stocks or a basket on the other. Two ETFs against each other are a
    pair with no hedge: the trade is about both."""
    for hedge, other in ((shorts, longs), (longs, shorts)):
        if len(hedge) == 1 and hedge[0] in ETFS and other and not all(s in ETFS for s in other):
            return hedge[0]
    return None


def proposal_trades(proposals: list[dict], entry_date: str, prices: dict[str, float], held: set[str],
                    start: int = 1, room: int | None = None, capital: float | None = None,
                    volumes: dict[str, float] | None = None) -> tuple[list[dict], list[dict]]:
    """The research pass's takes as trades, strongest conviction first, up to `room` of them (all, by default), each
    with the plan the pass wrote. Skips a take whose stock, basket members or pair funds are already in an open trade
    or one taken today (`held` is those core symbols), and anything it can't size. `start` numbers the ids after
    trades already made for the session. With `capital` each take is sized by its risk
    (side_dollars, with `volumes` for the liquidity limit); without it every take gets the flat NOTIONAL.
    Returns (trades, skipped with reasons)."""
    trades, skipped, used = [], [], set(held)
    takes = [p for p in proposals if p["decision"] == "take"]
    # Strongest conviction first, then the pass's own ranking; a stable sort, so unranked takes keep the pass's order.
    for p in sorted(takes, key=lambda p: (-p["conviction"], p.get("rank") or 10**6)):
        if room is not None and len(trades) >= room:
            skipped.append({"name": p["id"], "reason": f"the {room} takes with the most conviction came first"})
            continue
        longs, shorts = p["long"], p["short"]
        hedge = _hedge(longs, shorts)
        symbols = {*longs, *shorts} - {hedge}
        if clash := symbols & used:
            skipped.append({"name": p["id"], "reason": f"already holds {', '.join(sorted(clash))}"})
            continue
        notional, sizing = (side_dollars(p["conviction"], p["plan"]["stop"]["spread_pct"], capital, longs, shorts, volumes)
                            if capital else (NOTIONAL, None))
        legs = size_legs(longs, shorts, prices, notional)
        if legs is None:
            skipped.append({"name": p["id"], "reason": "no price, or a share costs more than its side's notional"})
            continue
        trades.append({
            "id": f"{entry_date}-A{len(trades) + start:02d}", "book": "dynamic", "source": f"proposal {p['id']}", "proposal_id": p["id"],
            "conviction": p["conviction"], "name": p["name"], "setup": p["setup"], "direction": p["direction"], "why": p["reason"],
            "wrong_if": p["wrong_if"], "entry_date": entry_date, "horizon_days": p["plan"]["horizon_days"], "stop": p["plan"]["stop"],
            "notional": notional, **({"sizing": sizing} if sizing else {}), "legs": legs, "hedge": hedge,
        })
        used |= symbols
    return trades, skipped


def exposure(trades: list[dict]) -> tuple[float, dict[str, float]]:
    """Gross dollars and net dollars by symbol, at reference prices."""
    gross, net = 0.0, {}
    for t in trades:
        for leg in t["legs"]:
            v = leg["shares"] * leg["ref_price"]
            gross += v
            net[leg["symbol"]] = net.get(leg["symbol"], 0.0) + (v if leg["side"] == "long" else -v)
    return gross, net


def apply_caps(new: list[dict], open_trades: list[dict], max_gross: float = MAX_GROSS,
               max_symbol: float = MAX_SYMBOL) -> tuple[list[dict], list[dict]]:
    """Keep new trades, in the order given, while every cap holds. `max_gross` is MAX_GROSS_SHARE of the book's
    capital and `max_symbol` the most net exposure to one symbol (MAX_SYMBOL_SHARE of its capital)."""
    kept, skipped = [], []
    for t in new:
        trial = [*open_trades, *kept, t]
        gross, net = exposure(trial)
        if len(trial) > MAX_OPEN:
            skipped.append({"name": t["id"], "reason": f"over {MAX_OPEN} open trades"})
        elif gross > max_gross:
            skipped.append({"name": t["id"], "reason": f"gross would be ${gross:,.0f}, over ${max_gross:,.0f}"})
        elif (big := [s for s, v in net.items() if abs(v) > max_symbol and s in {l['symbol'] for l in t['legs']}]):
            skipped.append({"name": t["id"], "reason": f"net exposure to {', '.join(big)} over ${max_symbol:,.0f}"})
        else:
            kept.append(t)
    return kept, skipped


def spread_return(held: list[dict], prices: dict[str, float]) -> float | None:
    """The trade's profit in dollars from its entry fills to `prices` (divide by its notional for the return on
    one side); None if a price is missing.
    `held` is the filled legs: {"symbol", "side", "shares", "entry_price"}."""
    pnl = 0.0
    for leg in held:
        px = prices.get(leg["symbol"])
        if px is None:
            return None
        sign = 1 if leg["side"] == "long" else -1
        pnl += sign * leg["shares"] * (px - leg["entry_price"])
    return pnl


def stop_reason(trade: dict, held: list[dict], prices: dict[str, float], when: str) -> str | None:
    """Why the trade is past its stop at `prices` ("stopped: ..."), or None. `when` is the time named in the
    reason ("2:45 PM CT"). Checked by the close job and, during the day, by the order job (entries.py)."""
    stop = trade.get("stop") or {}
    for rule in stop.get("rules", []):
        px = prices.get(rule["symbol"])
        side = "below" if "below" in rule else "above"
        if px is not None and ((px < rule["below"]) if side == "below" else (px > rule["above"])):
            return f"stopped: {rule['symbol']} at {px:,.2f} at {when}, {side} {rule[side]:,.2f}"
    if "spread_pct" in stop:
        pnl = spread_return(held, prices)
        if pnl is not None and pnl / trade["notional"] * 100 <= stop["spread_pct"]:
            return f"stopped: down {abs(pnl / trade['notional'] * 100):.1f}% at {when}, past the {abs(stop['spread_pct']):g}% stop"
    return None


def exit_reason(trade: dict, session: int, held: list[dict], prices: dict[str, float]) -> str | None:
    """Why this trade exits at today's close, or None to keep holding. `session` is 1 on the entry day."""
    filled = {leg["symbol"] for leg in held}
    if filled != {leg["symbol"] for leg in trade["legs"]}:
        missing = sorted({leg["symbol"] for leg in trade["legs"]} - filled)
        return f"broken: {', '.join(missing)} never filled"
    if reason := stop_reason(trade, held, prices, "2:45 PM CT"):
        return reason
    if session >= trade["horizon_days"]:
        return f"time limit: session {session} of {trade['horizon_days']}"
    return None

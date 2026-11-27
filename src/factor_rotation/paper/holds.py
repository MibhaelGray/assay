"""Which hold period pays: every idea scored at every hold from 1 to 10 sessions.

The pass gives each idea its own time limit, so the record only ever shows one hold per idea. This asks the
other question (2026-11-14): if every idea had been held 1 session, 2, ... 10, where does the result build and
where does it fade, and does the research's edge (takes over passes, the better-ranked over the worse) last 2
sessions or 10? `fr paper holds` prints it.

How an idea is anchored (fixed 2026-11-26 to remove session-open look-ahead on morning/midday passes):
  - Filled takes: exact IBKR entry fill prices from the paper book (`book.trades()`), and for the `own limit`
    column, actual closed IBKR P/L in bp after commissions.
  - Passes and unfilled takes (three-pass swing book): the pass's timestamped `marks` prices from
    `entries/<session>.json` (the exact moment the pass was acted on, never the 8:30 AM open).
  - Legacy first-book proposals (no `pass` field and no marks): first session's open.

Hold sessions count from `entries.first_session(pass, session)` so a morning or midday verdict's 1-session hold
is the next close, matching live trading. Twice over:
  - held: no stop, out at the close of session h.
  - with its stop: replayed on the same 10-minute intraday checks as live stops when 5-minute bars are stored
    (including the remainder of the entry day for morning/midday passes), otherwise checked at each close.

No pass reads this (the skill says so): a pass that had seen "3 sessions is best" off a few nights would chase it.
"""

from datetime import date, datetime
from statistics import mean

from .. import bars, journal
from ..config import NY
from . import book, entries, learn, proposals, rules

HOLDS = list(range(1, rules.MAX_HORIZON + 1))
THIRDS = ("top third", "middle third", "bottom third")


def _prices(symbols: list[str]) -> dict[str, dict[date, tuple[float, float, float | None]]]:
    """symbol -> day -> (open, close, raw_close)."""
    px = bars.load(symbols)
    if px.empty:
        return {}
    has_raw = "raw_close" in px.columns
    if has_raw:
        return {s: {d.date(): (float(o), float(c), (None if rc != rc else float(rc)))
                    for d, o, c, rc in zip(g["date"], g["open"], g["close"], g["raw_close"])}
                for s, g in px.groupby("symbol")}
    return {s: {d.date(): (float(o), float(c), None) for d, o, c in zip(g["date"], g["open"], g["close"])}
            for s, g in px.groupby("symbol")}


def _bar_factor(bar: tuple | None) -> float:
    """close / raw_close for a bar tuple (open, close[, raw_close]); 1.0 when raw_close is absent."""
    if not bar or len(bar) < 3 or bar[2] is None or bar[2] <= 0:
        return 1.0
    return float(bar[1]) / float(bar[2])


def _anchor_factor(sym_px: dict, anchor_day: date) -> float:
    """Adjustment factor close/raw_close at or right after `anchor_day`."""
    if not sym_px:
        return 1.0
    if anchor_day in sym_px:
        return _bar_factor(sym_px[anchor_day])
    later = [d for d in sorted(sym_px) if d >= anchor_day]
    if later:
        return _bar_factor(sym_px[later[0]])
    earlier = sorted(sym_px)
    return _bar_factor(sym_px[earlier[-1]]) if earlier else 1.0


def _entry_for(p: dict, px: dict, trade: dict | None) -> tuple[dict[str, float], list[date], datetime | None, str] | None:
    """(entry prices by symbol, hold close dates, pass mark timestamp, source label), or None if unpriceable."""
    w = journal._weights({"long": p["long"] or None, "short": p["short"] or None})
    if not w:
        return None
    session = date.fromisoformat(p["session"])
    kind = p.get("pass")
    m = entries.marks(session, kind) if kind else None
    mark_at = datetime.fromisoformat(m["at"]).astimezone(NY) if m and m.get("at") else None

    # 1. Filled take: exact IBKR entry fill prices.
    if trade and trade.get("held"):
        fills = {h["symbol"]: float(h["entry_price"]) for h in trade["held"] if h.get("entry_price")}
        if all(s in fills for s in w):
            first = entries.first_session(kind, session) if kind else session
            return fills, journal.sessions_from(first, HOLDS[-1]), mark_at, "ibkr_fill"

    # 2. Three-pass proposal (pass or unfilled take): pass-time marks only, never session open.
    if kind:
        if not m or not m.get("prices"):
            return None
        prices = m["prices"]
        if any(s not in prices or not prices[s] for s in w):
            return None
        first = entries.first_session(kind, session)
        return {s: float(prices[s]) for s in w}, journal.sessions_from(first, HOLDS[-1]), mark_at, "pass_mark"

    # 3. Legacy first-book proposal (no `pass`): session open.
    first_bar = {s: px.get(s, {}).get(session) for s in w}
    if any(v is None for v in first_bar.values()):
        return None
    return {s: float(first_bar[s][0]) for s in w}, journal.sessions_from(session, HOLDS[-1]), None, "session_open"


def _path(p: dict, px: dict, settled, trade: dict | None = None,
          ix: dict | None = None, today: date | None = None) -> tuple[list[float], list[float], str] | None:
    """(held bps by hold 1..k, stopped bps by hold 1..k, entry_source), or None if no first hold has settled."""
    w = journal._weights({"long": p["long"] or None, "short": p["short"] or None})
    info = _entry_for(p, px, trade)
    if info is None:
        return None
    entry_px, hold_days, mark_at, source = info
    session = date.fromisoformat(p["session"])
    stop_pct = ((p.get("plan") or {}).get("stop") or {}).get("spread_pct")
    today = today or datetime.now(NY).date()
    legs = list(w)
    replay = bool(ix) and stop_pct is not None and session >= learn.INTRADAY_FROM and p.get("pass") is not None

    # Align unadjusted entry fills/marks to adjusted daily bars via entry-date close/raw_close factor so
    # historical dividends never distort returns while mid-hold dividends/splits stay exact.
    anchor_day = journal.previous_trading_day(session) if p.get("pass") in proposals.NIGHT_KINDS else session
    f0 = {s: (1.0 if source == "session_open" else _anchor_factor(px.get(s, {}), anchor_day)) for s in w}
    adj_entry = {s: entry_px[s] * f0[s] for s in w}

    # For morning/midday passes, check day-0 (the remainder of `session`) for an intraday or close stop before hold 1.
    hit: float | None = None
    if stop_pct is not None and hold_days and hold_days[0] != session and settled(session):
        day0 = {s: px.get(s, {}).get(session) for s in w}
        if all(v is not None for v in day0.values()):
            if replay:
                day_scale = {s: _bar_factor(day0[s]) / f0[s] for s in w}
                how, intr = learn._intraday_hit(session, legs, w, entry_px, stop_pct, ix, mark_at, today, scale=day_scale)
                if how == "wait":
                    return None
                if intr:
                    hit = round(intr[1] * 1e4, 1)
            if hit is None:
                v0 = round(sum(w[s] * (day0[s][1] / adj_entry[s] - 1) for s in w) * 1e4, 1)
                if v0 / 100 <= stop_pct:
                    hit = v0

    held: list[float] = []
    stopped: list[float] = []
    for d in hold_days:
        bar = {s: px.get(s, {}).get(d) for s in w}
        if any(v is None for v in bar.values()) or not settled(d):
            break
        if hit is None and stop_pct is not None and replay:
            day_scale = {s: _bar_factor(bar[s]) / f0[s] for s in w}
            how, intr = learn._intraday_hit(d, legs, w, entry_px, stop_pct, ix, mark_at if d == session else None, today, scale=day_scale)
            if how == "wait":
                break
            if intr:
                hit = round(intr[1] * 1e4, 1)
        v = round(sum(w[s] * (bar[s][1] / adj_entry[s] - 1) for s in w) * 1e4, 1)
        held.append(v)
        if hit is None and stop_pct is not None and v / 100 <= stop_pct:
            hit = v
        stopped.append(v if hit is None else hit)
    if not held:
        return None
    return held, stopped, source


def _stopped(path: list[float], stop_pct: float | None) -> list[float]:
    """The path with a close-only stop applied: from the close that is past the stop, the result stays there."""
    out, hit = [], None
    for v in path:
        if hit is None and stop_pct is not None and v / 100 <= stop_pct:
            hit = v
        out.append(v if hit is None else hit)
    return out


def rows() -> list[dict]:
    """One row per idea that could be traded as written: who it was, fill status, and its two paths."""
    props = {p["id"]: p for p in proposals.every()}
    symbols = sorted({s for p in props.values() for s in [*p["long"], *p["short"]]})
    px = _prices(symbols)
    ix = bars.load_intraday([s for s in symbols if s in set(learn.intraday_symbols())]) if symbols else {}
    trades_by_pid = {t["proposal_id"]: t for t in book.trades()
                     if book.book_of(t) == book.MAIN and t.get("proposal_id")}
    seen: dict[date, bool] = {}

    def settled(d: date) -> bool:
        if d not in seen:
            seen[d] = journal._bars_settled(d)
        return seen[d]

    out = []
    for r in learn.outcomes():
        p = props[r["id"]]
        if not p["plan"] or r["plan_status"] in (None, "skipped"):
            continue
        trade = trades_by_pid.get(p["id"])
        res = _path(p, px, settled, trade=trade, ix=ix)
        if not res:
            continue
        held_path, stop_path, source = res
        third = THIRDS[min(2, (r["rank"] - 1) * 3 // r["ranked"])] if r.get("rank") else None
        filled = r.get("fill_status") == "filled"
        # For filled takes, own_limit reflects actual closed IBKR P/L (after commissions) rather than model plan_bps.
        own_bps = r["paper_bps"] if filled else (r["plan_bps"] if r.get("plan_mature", True) else None)
        out.append({
            "id": r["id"], "session": r["session"], "pass": r.get("pass"), "name": r["name"], "setup": r["setup"],
            "taken": r["verdict"] == "take", "filled": filled,
            "fill_status": r.get("fill_status"), "unfilled_reason": r.get("unfilled_reason"),
            "entry_source": source, "own": p["kind"] == "own",
            "rank": r.get("rank"), "third": third, "limit": p["plan"]["horizon_days"],
            "plan_bps": r["plan_bps"] if r.get("plan_mature", True) else None,
            "own_bps": own_bps,
            "held": held_path, "stopped": stop_path,
        })
    return out


def _by_hold(rs: list[dict], key: str) -> dict[int, dict]:
    return {h: learn._stat([r[key][h - 1] for r in rs if len(r[key]) >= h]) for h in HOLDS}


def _nights(values: list[float]) -> dict:
    """The average of one number per night, and its t."""
    if not values:
        return {"nights": 0}
    m = mean(values)
    sd = (sum((v - m) ** 2 for v in values) / (len(values) - 1)) ** 0.5 if len(values) > 2 else 0
    return {"nights": len(values), "mean": round(m, 3), "t": round(m / (sd / len(values) ** 0.5), 2) if sd else None}


def _edges(rs: list[dict]) -> dict:
    """Per hold, session by session: the rank correlation between the pass's rank and the held result, and the takes'
    average less the rest's. Multiple passes on the same session are averaged into one session observation."""
    nights: dict[str, list[dict]] = {}
    for r in rs:
        nights.setdefault(r["session"], []).append(r)
    rank, takes, filled_takes = {}, {}, {}
    for h in HOLDS:
        ics, gaps, fgaps = [], [], []
        for _, group in sorted(nights.items()):
            have = [r for r in group if len(r["held"]) >= h]
            # Average pass-level rank correlations within the session so 3 passes/day count as 1 session.
            by_pass: dict[str | None, list[dict]] = {}
            for r in have:
                if r["rank"]:
                    by_pass.setdefault(r.get("pass"), []).append(r)
            pass_ics = []
            for pgroup in by_pass.values():
                if len(pgroup) >= learn.MIN_RANKED:
                    ic = learn._spearman([-r["rank"] for r in pgroup], [r["held"][h - 1] for r in pgroup])
                    if ic is not None:
                        pass_ics.append(ic)
            if pass_ics:
                ics.append(mean(pass_ics))
            took = [r["held"][h - 1] for r in have if r["taken"]]
            ftook = [r["held"][h - 1] for r in have if r["filled"]]
            rest = [r["held"][h - 1] for r in have if not r["taken"]]
            if took and rest:
                gaps.append(mean(took) - mean(rest))
            if ftook and rest:
                fgaps.append(mean(ftook) - mean(rest))
        rank[h], takes[h], filled_takes[h] = _nights(ics), _nights(gaps), _nights(fgaps)
    return {"rank": rank, "takes_less_rest": takes, "filled_takes_less_rest": filled_takes}


def _cohort(rs: list[dict]) -> dict:
    has_fills = any(r["filled"] for r in rs)
    unfilled = [r for r in rs if r["taken"] and not r["filled"]]
    reasons: dict[str, int] = {}
    for r in unfilled:
        k = r.get("unfilled_reason") or "other"
        reasons[k] = reasons.get(k, 0) + 1
    groups = {
        "all ideas": rs,
        **({"filled takes": [r for r in rs if r["filled"]],
            "unfilled takes": unfilled} if has_fills else {}),
        "takes": [r for r in rs if r["taken"]],
        "not taken": [r for r in rs if not r["taken"]],
        **{t: [r for r in rs if r["third"] == t] for t in THIRDS},
        # where the idea came from: a candidate the screen put up, or one the pass found itself (kind "own")
        "from the screen": [r for r in rs if not r["own"]],
        "the pass's own ideas": [r for r in rs if r["own"]],
    }

    def own_stat(name: str, g: list[dict]) -> dict:
        key = "own_bps" if name == "filled takes" else "plan_bps"
        done = [r for r in g if r[key] is not None]
        return ({**learn._stat([r[key] for r in done]), "sessions": round(mean(r["limit"] for r in done), 1)}
                if done else {"n": 0})

    return {"ideas": len(rs), "has_fills": has_fills, "unfilled_reasons": reasons,
            "held": {name: _by_hold(g, "held") for name, g in groups.items()},
            "stopped": {name: _by_hold(g, "stopped") for name, g in groups.items()},
            "own_limit": {name: own_stat(name, g) for name, g in groups.items()},
            "by_setup": {s: _by_hold([r for r in rs if r["setup"] == s], "held") for s in sorted({r["setup"] for r in rs})},
            "edges": _edges(rs)}


def table(rs: list[dict] | None = None) -> dict:
    """`so_far`: every idea, each hold counting the ideas that have reached it. `complete`: only the ideas with
    all 10 sessions in, so every hold is the same ideas."""
    rs = rows() if rs is None else rs
    return {"holds": HOLDS, "nights": len({r["session"] for r in rs}), "nights_needed": learn.RANK_NIGHTS,
            "so_far": _cohort(rs), "complete": _cohort([r for r in rs if len(r["held"]) == HOLDS[-1]])}


def _cells(by_hold: dict[int, dict], fmt=lambda s: f"{s['mean']:+.0f}") -> str:
    return "".join(f"{fmt(by_hold[h]) if by_hold[h].get('n') else '.':>7}" for h in HOLDS)


def _block(c: dict) -> list[str]:
    head = f"  {'sessions held':<22}" + "".join(f"{h:>7}" for h in HOLDS) + "   own limit"
    own = lambda name: (f"   {c['own_limit'][name]['mean']:+.0f} ({c['own_limit'][name]['sessions']:g} sessions, n={c['own_limit'][name]['n']})"  # noqa: E731
                        if c["own_limit"][name].get("n") else "   .")
    order = [name for name in c["held"] if c["held"][name][1].get("n") and (name != "takes" or not c.get("has_fills"))]
    lines = ["  Average result, bp, held with no stop", head,
             f"  {'ideas counted':<22}" + _cells(c["held"]["all ideas"], lambda s: str(s["n"]))]
    lines += [f"  {name:<22}" + _cells(c["held"][name]) + own(name) for name in order]
    if c.get("has_fills") and c.get("unfilled_reasons"):
        parts = ", ".join(f"{k.replace('_', ' ')} {n}" for k, n in sorted(c["unfilled_reasons"].items(), key=lambda x: -x[1]))
        lines.append(f"  unfilled breakdown: {parts} (execution leak diagnostic, marked at pass time)")
    lines += ["", "  Average result, bp, with each idea's own stop", head]
    lines += [f"  {name:<22}" + _cells(c["stopped"][name]) for name in order]
    lines += ["", "  By setup, bp, held with no stop (all ideas)", head]
    lines += [f"  {name[:22]:<22}" + _cells(s) for name, s in c["by_setup"].items()]
    e = c["edges"]
    counted = lambda key: f"  {'  sessions counted':<22}" + "".join(f"{e[key][h]['nights'] or '.':>7}" for h in HOLDS)  # noqa: E731
    lines += ["", "  The research's edge at each hold, worked session by session and averaged", head.split("   own")[0],
              f"  {'rank correlation':<22}" + "".join(f"{e['rank'][h]['mean']:>+7.2f}" if e["rank"][h]["nights"] else f"{'.':>7}" for h in HOLDS),
              counted("rank")]
    if c.get("has_fills") and any(e["filled_takes_less_rest"][h]["nights"] for h in HOLDS):
        lines += [f"  {'filled takes - rest':<22}" + "".join(f"{e['filled_takes_less_rest'][h]['mean']:>+7.0f}" if e["filled_takes_less_rest"][h]["nights"] else f"{'.':>7}" for h in HOLDS),
                  counted("filled_takes_less_rest")]
    lines += [f"  {'takes less the rest':<22}" + "".join(f"{e['takes_less_rest'][h]['mean']:>+7.0f}" if e["takes_less_rest"][h]["nights"] else f"{'.':>7}" for h in HOLDS),
              counted("takes_less_rest")]
    return lines


def report() -> str:
    t = table()
    a, b = t["so_far"], t["complete"]
    lines = [f"Hold periods: {a['ideas']} ideas from {t['nights']} session{'s' if t['nights'] != 1 else ''}, each scored at holds 1 to {HOLDS[-1]}. "
             "No research pass sees this.",
             "Anchored on exact IBKR entry fills for filled takes (own limit = actual IBKR exit after commissions), and pass-time marks for passes and unfilled takes (no session-open look-ahead).",
             f"An anecdote before about {t['nights_needed']} sessions; a dot means no ideas yet.", ""]
    if not a["ideas"]:
        return "\n".join(lines + ["  no idea has a settled first session yet"])
    lines += ["Every idea so far (a hold counts only the ideas that have reached it, so the columns are not the same ideas)", *_block(a), ""]
    lines += ([f"The same {b['ideas']} ideas at every hold (all {HOLDS[-1]} sessions in): the clean comparison", *_block(b)] if b["ideas"]
              else [f"No idea has all {HOLDS[-1]} sessions in yet. Once some do, they are shown here on their own: the same ideas at every hold."])
    return "\n".join(lines)

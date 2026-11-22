"""What the book learns from: post-mortems, and every proposal's result.

    journal/paper/postmortems/<date>.json     a tag and a one-line lesson for each finished trade
    journal/paper/lessons.md                  the standing lessons, kept by the research pass and read at its start

Every proposal, taken or passed, is scored against its own plan (from the price when its pass was acted on to
the stop or the time limit, before costs), so the passes are a control for the takes: if the takes don't beat
them, the research isn't adding anything. A trade a review managed (closed early or extended) is also compared
with what its original plan would have made. `brief()` puts all of it on one page for the next pass to read.
"""

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import mean, median

import pandas as pd

from .. import bars, journal
from ..config import CT, NY
from ..universe import ETFS
from . import book, proposals

TAGS = {
    "right_reason": "made money, and for the reason given",
    "right_luck": "made money, but not for the reason given",
    "thesis_broke": "lost money because the reasoning was wrong",
    "noise": "lost money though the reasoning held: the stop, the tape or the timing",
}
SMALL = 30                    # below this many results a group's average is an anecdote
MIN_RANKED = 4                # a night's ranking is scored once this many of its ranked ideas have finished
RANK_NIGHTS = 20              # about how many scored nights a ranking needs before its average means something
LESSONS = "lessons.md"
# Every verdict's stop is replayed on the order job's own checks (entries.STOP_EVERY, every 10 minutes in regular
# hours, until 20 minutes before the close) from stored 5-minute bars, and again at each close: the owner,
# 2026-11-22, so takes and passes are scored the way the book actually trades. A day whose 5-minute bars haven't
# been pulled waits INTRADAY_WAIT calendar days, then is checked at its close only.
INTRADAY_FROM = date(2026, 11, 16)    # the restart: verdicts before it are scored on closes
INTRADAY_WAIT = 5


def _text(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _write(kind: str, name: str, payload: dict) -> Path:
    path = proposals.root() / kind / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, default=journal._json_default) + "\n")
    return path


# ---- Post-mortems ---------------------------------------------------------------------------------------------
def postmortems() -> dict[str, dict]:
    return {m["trade_id"]: m for p in sorted((proposals.root() / "postmortems").glob("*.json"))
            for m in json.loads(p.read_text())["postmortems"]}


def record_postmortems(draft: dict, now: datetime | None = None) -> Path:
    """Tag finished automatic trades and say what each taught. The tag has to agree with the result: the
    question it answers is whether the reasoning, not the P&L, was right.

    draft: {"postmortems": [{"trade_id": "...", "tag": "right_reason" | "right_luck" | "thesis_broke" | "noise",
                             "what_happened": "...", "lesson": "..."}]}
    """
    now = now or datetime.now(NY)
    closed = {r["id"]: r for r in book.rows() if r["state"] == "closed"}
    have = postmortems()
    new = []
    for m in draft.get("postmortems", []):
        who = str(m.get("trade_id"))
        r = closed.get(who)
        if not r:
            raise ValueError(f"{who} is not a closed trade")
        if who in have or who in {x["trade_id"] for x in new}:
            raise ValueError(f"{who} already has a post-mortem")
        if m.get("tag") not in TAGS or not _text(m.get("what_happened")) or not _text(m.get("lesson")):
            raise ValueError(f"{who}: needs tag ({'/'.join(TAGS)}), what_happened and lesson")
        won = (r["bps"] or 0) > 0
        if won != m["tag"].startswith("right"):
            raise ValueError(f"{who} {'made' if won else 'lost'} {abs(r['bps'] or 0):.0f} bp, so its tag is one of: "
                             + ", ".join(t for t in TAGS if t.startswith("right") == won))
        new.append({"trade_id": who, "proposal_id": r.get("proposal_id"), "name": r["name"], "setup": r["setup"], "bps": r["bps"],
                    "tag": m["tag"], "what_happened": m["what_happened"].strip(), "lesson": m["lesson"].strip(), "at": now.isoformat()})
    if not new:
        raise ValueError("the draft has no postmortems")
    path = proposals.root() / "postmortems" / f"{now.date()}.json"
    old = json.loads(path.read_text())["postmortems"] if path.exists() else []
    return _write("postmortems", str(now.date()), {"postmortems": [*old, *new]})


def needs_postmortem() -> list[dict]:
    have = postmortems()
    return [r for r in book.rows() if r["state"] == "closed" and r["id"] not in have
            and not (r["reason"] or "").startswith("broken")]


# ---- Results ----------------------------------------------------------------------------------------------
def _verdict(p: dict) -> str:
    return p["decision"] if p.get("reviewed", True) else "not reviewed"


def intraday_symbols(today: date | None = None) -> list[str]:
    """The legs of every verdict whose plan may still need 5-minute bars: recorded since the restart, within
    the last 60 days. `fr bars --intraday` pulls them."""
    today = today or date.today()
    since = max(INTRADAY_FROM, today - timedelta(days=60)).isoformat()
    return sorted({s for p in proposals.every() if p["session"] >= since for s in [*p["long"], *p["short"]]})


def _checks(day: date, after: datetime | None = None) -> list[datetime]:
    """The order job's stop checks on `day`, New York time: every STOP_EVERY from 10 minutes after the open to
    20 minutes before the close (entries.entry_end), and only after `after` (when the pass was acted on)."""
    from . import entries
    step = entries.STOP_EVERY
    t, end = datetime.combine(day, time(9, 30), NY) + step, datetime.combine(day, entries.entry_end(day), NY)
    out = []
    while t <= end:
        if after is None or t > after:
            out.append(t)
        t += step
    return out


def _intraday_hit(day: date, legs: list[str], w: dict, entry: dict, pct: float, ix: dict, after: datetime | None,
                  today: date) -> tuple[str, tuple[datetime, float] | None]:
    """Replay a spread stop on `day`'s checks. ("hit", (when, return)) at the first check past it, ("clear", None)
    if none was, ("wait", None) while the day's 5-minute bars aren't stored yet, ("none", None) once they aren't
    coming. A check's price is the close of the last 5-minute bar that finished by then."""
    close = datetime.combine(day, journal.close_time(day), NY)
    open_ = datetime.combine(day, time(9, 30), NY)
    series = {}
    for s in legs:
        x = ix.get(s)
        x = x[(x.index >= open_) & (x.index < close)] if x is not None else None
        if x is None or x.empty or x.index[-1] < close - timedelta(minutes=5):
            return ("none", None) if (today - day).days > INTRADAY_WAIT else ("wait", None)
        series[s] = x
    for t in _checks(day, after):
        seen = {s: x[x.index <= t - timedelta(minutes=5)] for s, x in series.items()}
        if any(v.empty for v in seen.values()):
            continue
        ret = sum(w[s] * (seen[s].iloc[-1] / entry[s] - 1) for s in legs)
        if ret * 100 <= pct:
            return "hit", (t, ret)
    return "clear", None


def _mark_outcome(p: dict, px: pd.DataFrame, ix: dict | None = None, today: date | None = None) -> dict | None:
    """The plan result of a verdict from one of the three daily passes: from each leg's price when the order job
    first acted on the pass (entries marks: the same moment for a take and a pass) to the close where its stop
    is hit or its time limit ends, before costs. A morning or midday verdict is already partway through its
    session, so that day's close only checks the stop and the time limit counts from the next session, as it
    does for the trade itself. None until the pass has been marked; status "open" while sessions remain."""
    from . import entries

    session = date.fromisoformat(p["session"])
    m = entries.marks(session, p["pass"])
    if not m:
        return None
    legs = [*p["long"], *p["short"]]
    entry = m["prices"]
    if lost := [s for s in [*legs, "SPY"] if s not in entry]:
        return {"status": "unpriced", "note": f"no price for {', '.join(lost)} when the {p['pass']} pass was acted on"}
    horizon = p["plan"]["horizon_days"]
    first = entries.first_session(p["pass"], session)
    days = ([session] if first != session else []) + journal.sessions_from(first, horizon)
    close = {s: g.assign(day=g["date"].dt.date).set_index("day")["close"] for s, g in px[px["symbol"].isin([*legs, "SPY"])].groupby("symbol")}
    w = journal._weights({"long": p["long"] or None, "short": p["short"] or None})
    stop = p["plan"].get("stop") or {}
    replay = ix is not None and "spread_pct" in stop and session >= INTRADAY_FROM
    today = today or date.today()
    at = datetime.fromisoformat(m["at"]).astimezone(NY) if m.get("at") else None
    path, status, note, intraday = [], "open", "", "replayed" if replay else None
    for d in days:
        if any(s not in close or d not in close[s].index for s in [*legs, "SPY"]) or not journal._bars_settled(d):
            break
        if replay:
            how, hit = _intraday_hit(d, legs, w, entry, stop["spread_pct"], ix, at if d == session else None, today)
            if how == "wait":
                break
            if how == "none":
                intraday = "closes only on some days: no 5-minute bars"
            if hit:
                when, ret = hit
                path.append((d, ret, close["SPY"][d] / entry["SPY"] - 1))
                status = "stopped"
                note = (f"Stopped at {when.astimezone(CT):%-I:%M %p} CT on {d}: down {abs(ret) * 100:.1f}% "
                        f"at a 10-minute check, past the {abs(stop['spread_pct']):g}% stop")
                break
        ret = sum(w[s] * (close[s][d] / entry[s] - 1) for s in legs)
        path.append((d, ret, close["SPY"][d] / entry["SPY"] - 1))
        hits = [f"{r['symbol']} closed at {close[r['symbol']][d]:,.2f} ({journal.rule_text(r)})"
                for r in stop.get("rules", []) if journal._rule_hit(r, close[r["symbol"]][d])]
        if "spread_pct" in stop and ret * 100 <= stop["spread_pct"]:
            hits.append(f"down {abs(ret) * 100:.1f}% from the pass's price, past the {abs(stop['spread_pct']):g}% stop")
        if hits:
            status, note = "stopped", f"Stopped at the close of {d}: " + "; ".join(hits)
            break
        if d == days[-1]:
            status, note = "time", f"Held to the time limit, {horizon} session{'s' if horizon > 1 else ''}"
    if not path:
        return {"status": "open", "note": "its first close hasn't settled"}
    d, ret, spy = path[-1]
    rets = [x[1] for x in path]
    return {"status": status, "note": note, "exit_date": d.isoformat(), "sessions": len(path), "horizon_days": horizon, "intraday": intraday,
            "bps": round(ret * 1e4, 1), "spy_bps": round(spy * 1e4, 1), "best_bps": round(max(rets) * 1e4, 1),
            "worst_bps": round(min(rets) * 1e4, 1), "hit": bool(ret > 0)}


def outcomes() -> list[dict]:
    """One row per proposal: the verdict, the result of its own plan on daily bars (`plan_bps`, before costs,
    None until it finishes), and for a take that was traded, the paper result after commissions."""
    props = proposals.every()
    if not props:
        return []
    paper = {r["proposal_id"]: r for r in book.rows() if r.get("proposal_id")}
    tags = postmortems()
    symbols = sorted({s for p in props for s in [*p["long"], *p["short"]]} | {"SPY"})
    px = bars.load(symbols)
    ix = bars.load_intraday([s for s in symbols if s in set(intraday_symbols())])
    out = []
    for p in props:
        plan = None
        if p["plan"] and (p["long"] or p["short"]) and not px.empty:
            plan = _mark_outcome(p, px, ix)
        done = bool(plan) and plan["status"] in ("time", "stopped")
        r = paper.get(p["id"])
        reason = (r or {}).get("reason") or ""
        managed = "early exit" if reason.startswith(("review:", "midday check:")) else "extended" if (r or {}).get("extended") else None
        m = tags.get((r or {}).get("id"))
        out.append({
            "id": p["id"], "session": p["session"], "name": p["name"], "kind": p["kind"], "setup": p["setup"], "direction": p["direction"],
            "source": p["source"], "pass": p.get("pass"), "verdict": _verdict(p), "decision": p["decision"], "conviction": p["conviction"], "reason": p["reason"],
            "rank": p.get("rank"), "ranked": p.get("ranked"), "recorded_at": p.get("recorded_at"),
            "legs": " / ".join(filter(None, [", ".join(p["long"]) and f"long {', '.join(p['long'])}", ", ".join(p["short"]) and f"short {', '.join(p['short'])}"])),
            "horizon_days": (p["plan"] or {}).get("horizon_days"), "stop_pct": ((p["plan"] or {}).get("stop") or {}).get("spread_pct"),
            "stats": p.get("stats"), "wrong_if": p.get("wrong_if"), "reasoning": p.get("reasoning"), "hedge_why": p.get("hedge_why"),
            "why": (p.get("signal") or {}).get("why"),
            "plan_status": plan["status"] if plan else None, "plan_bps": plan["bps"] if done else None,
            "plan_so_far_bps": plan["bps"] if plan and not done and "bps" in plan else None,
            "plan_note": plan.get("note") if plan else None, "plan_exit": plan.get("exit_date") if done else None,
            "trade_id": r and r["id"], "state": r and r["state"], "paper_bps": r["bps"] if r and r["state"] == "closed" else None,
            "exit_reason": reason or None, "managed": managed, "tag": m and m["tag"], "lesson": m and m["lesson"],
        })
    return out


def _stat(values: list[float]) -> dict:
    return {"n": len(values), "mean": round(mean(values), 1), "median": round(median(values), 1),
            "winners": round(sum(v > 0 for v in values) / len(values), 2)} if values else {"n": 0}


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation, with tied values sharing their average rank. None when either side doesn't vary."""
    def ranks(vs):
        order = sorted(range(len(vs)), key=lambda i: vs[i])
        out, i = [0.0] * len(vs), 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vs[order[j + 1]] == vs[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return out

    a, b = ranks(xs), ranks(ys)
    ma, mb = mean(a), mean(b)
    va, vb = sum((x - ma) ** 2 for x in a), sum((y - mb) ** 2 for y in b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb) ** 0.5 if va and vb else None


def rank_table(rows: list[dict] | None = None) -> dict:
    """Whether the pass's ranking predicts results. Each ranked draft is one night: among its ideas whose plans
    have finished, the rank correlation between the pass's rank and the plan result, signed so that a positive
    number means the better-ranked ideas did better. Ideas judged on one night rise and fall together, so the
    night is the unit: the score is the nights' average, and `t` is that average over its standard error.
    Also the plan results of each night's top, middle and bottom third, pooled."""
    rows = outcomes() if rows is None else rows
    ranked = [r for r in rows if r.get("rank")]
    nights: dict[tuple, list[dict]] = {}
    for r in ranked:
        if r["plan_bps"] is not None:
            nights.setdefault((r["session"], r["recorded_at"]), []).append(r)
    scored = []
    for (session, _), rs in sorted(nights.items()):
        ic = _spearman([-r["rank"] for r in rs], [float(r["plan_bps"]) for r in rs]) if len(rs) >= MIN_RANKED else None
        if ic is not None:
            scored.append({"session": session, "finished": len(rs), "ranked": rs[0]["ranked"], "ic": round(ic, 3)})
    ics = [n["ic"] for n in scored]
    sd = (sum((v - mean(ics)) ** 2 for v in ics) / (len(ics) - 1)) ** 0.5 if len(ics) > 2 else 0
    third = lambda r: min(2, (r["rank"] - 1) * 3 // r["ranked"])  # noqa: E731
    done = [r for rs in nights.values() for r in rs]
    return {"ranked": len(ranked), "finished": len(done), "nights": len(scored), "nights_needed": RANK_NIGHTS,
            "ic": round(mean(ics), 3) if ics else None, "t": round(mean(ics) / (sd / len(ics) ** 0.5), 2) if sd else None,
            "thirds": {name: _stat([r["plan_bps"] for r in done if third(r) == k]) for k, name in enumerate(("top", "middle", "bottom"))},
            "by_night": scored}


def tables(rows: list[dict] | None = None) -> dict:
    """Averages of the finished proposals' plan results by verdict, conviction and setup, the paper results
    of the trades taken, the trades the review managed against their original plans, and post-mortem tags."""
    rows = outcomes() if rows is None else rows
    done = [r for r in rows if r["plan_bps"] is not None]
    by = lambda key: {k: _stat([r["plan_bps"] for r in done if key(r) == k]) for k in sorted({key(r) for r in done}, key=str)}  # noqa: E731
    traded = [r for r in rows if r["paper_bps"] is not None]
    managed = [r for r in traded if r["managed"] and r["plan_bps"] is not None]
    return {
        "proposals": len(rows), "finished": len(done),
        "by_verdict": by(lambda r: r["verdict"]),
        "by_conviction": by(lambda r: r["conviction"] if r["conviction"] is not None else "none"),
        "rank": rank_table(rows),
        "by_setup": {f"{setup} · {verdict}": s for (setup, verdict), s in by(lambda r: (r["setup"], "take" if r["verdict"] == "take" else "not taken")).items()},
        "paper": _stat([r["paper_bps"] for r in traded]),
        "managed": {kind: {"paper": _stat([r["paper_bps"] for r in managed if r["managed"] == kind]),
                           "plan": _stat([r["plan_bps"] for r in managed if r["managed"] == kind])}
                    for kind in sorted({r["managed"] for r in managed})},
        "by_tag": {tag: sum(r["tag"] == tag for r in rows) for tag in TAGS if any(r["tag"] == tag for r in rows)},
    }


def _line(label: str, s: dict) -> str:
    if not s.get("n"):
        return f"  {label:<34} none finished"
    return (f"  {label:<34} n={s['n']:<4} mean {s['mean']:+7.1f} bp  median {s['median']:+7.1f}  winners {s['winners']:.0%}"
            + ("   (small sample)" if s["n"] < SMALL else ""))


def brief() -> str:
    """One page for the start of a research pass: the record so far, the open trades, what's waiting for a
    post-mortem, and the standing lessons."""
    rows = outcomes()
    t = tables(rows)
    from .. import portfolio
    three = bool(portfolio.restart())
    lines = [f"{'Swing' if three else 'Automatic'} book record, {datetime.now(CT):%Y-%m-%d %-I:%M %p} CT: {t['proposals']} proposals, {t['finished']} with a finished plan result.",
             ("Plan result = the proposal's own plan from the price when its pass was acted on to its stop or time limit, at daily closes, before costs. "
              if three else "Plan result = the proposal's own plan on daily bars, first open to its stop or time limit, before costs. ")
             + "The passes are the control: the takes have to beat them.", "", "By verdict"]
    lines += [_line(k, s) for k, s in t["by_verdict"].items()] or ["  nothing finished yet"]
    lines += ["", "By conviction"] + ([_line(f"conviction {k}", s) for k, s in t["by_conviction"].items()] or ["  nothing finished yet"])
    rk = t["rank"]
    lines += ["", "By rank (1 = the best idea of its pass; the ranking works if better-ranked ideas do better)"]
    if rk["nights"]:
        lines.append(f"  rank correlation {rk['ic']:+.2f} on average over {rk['nights']} night{'s' if rk['nights'] != 1 else ''}"
                     + (f", t {rk['t']:+.1f}" if rk["t"] is not None else "")
                     + (f"   (an anecdote before about {RANK_NIGHTS} nights)" if rk["nights"] < RANK_NIGHTS else ""))
        lines += [_line(f"{name} third of each night", s) for name, s in rk["thirds"].items()]
    else:
        lines.append(f"  no ranked night has {MIN_RANKED} finished plans yet ({rk['ranked']} verdicts ranked so far)")
    lines += ["", "By setup"] + ([_line(k, s) for k, s in t["by_setup"].items()] or ["  nothing finished yet"])
    lines += ["", "Paper trades closed (after commissions)", _line("all", t["paper"])]
    for kind, s in t["managed"].items():
        lines += [_line(f"{kind}: paper result", s["paper"]), _line(f"{kind}: what the plan would have made", s["plan"])]
    if t["by_tag"]:
        lines += ["", "Post-mortem tags: " + ", ".join(f"{tag} {n}" for tag, n in t["by_tag"].items())]
    recent = sorted(postmortems().values(), key=lambda m: m["at"])[-10:]
    if recent:
        lines += ["", "Latest post-mortems"] + [f"  {m['trade_id']} {m['name']} ({m['setup']}, {m['bps']:+.0f} bp, {m['tag']}): {m['lesson']}" for m in recent]
    if waiting := needs_postmortem():
        lines += ["", "Closed trades with no post-mortem yet"] + [f"  {r['id']} {r['name']} ({r['setup']}): {r['bps']:+.0f} bp, {r['reason']}" for r in waiting]
    open_auto = [r for r in book.rows() if r["state"] in ("open", "planned")]
    if open_auto:
        lines += ["", "Open trades"]
        for r in open_auto:
            lines.append(f"  {r['id']} {r['name']} ({r['setup']}, {r['legs']}): session {r['session_number']} of {r['horizon_days']}, "
                         + (f"{r['bps']:+.0f} bp" if r["bps"] is not None else "no fills yet")
                         + (f", {r['stop_room_pct']:+.1f}% from its stop" if r["stop_room_pct"] is not None else "")
                         + (f". Wrong if: {r['wrong_if']}" if r.get("wrong_if") else ""))
    from . import entries
    if still := entries.waiting(entries.entry_session(datetime.now(NY))):
        lines += ["", "Takes still waiting to enter (review each: hold keeps trying, exit stands it down)"]
        for w in still:
            got = ", ".join(f"{s} {n}/{next(leg['shares'] for leg in w['legs'] if leg['symbol'] == s)}" for s, n in w["filled"].items())
            lines.append(f"  {w['id']} {w['name']} ({w['setup']}, from the {w.get('pass')} pass): filled {got}. Wrong if: {w.get('wrong_if')}")
    path = proposals.root() / LESSONS
    lines += ["", f"Standing lessons ({path})", path.read_text().strip() if path.exists() else "  none written yet"]
    return "\n".join(lines)


# ---- Choosing a hedge -------------------------------------------------------------------------------------
def hedges(symbols: list[str], top: int = 6) -> list[dict]:
    """How each ETF we track has moved with these names (equal weight) over 60 sessions, best match first, with
    SPY always shown. A hedge the names barely move with hedges nothing: it adds a second bet."""
    df = bars.load(sorted({*symbols, *ETFS}))
    rets = df.pivot_table(index="date", columns="symbol", values="close").sort_index().pct_change().iloc[-60:]
    if missing := [s for s in symbols if s not in rets]:
        raise ValueError(f"no bars for {', '.join(missing)}")
    a = rets[symbols].mean(axis=1, skipna=False)
    out = []
    for etf in ETFS:
        if etf in symbols or etf not in rets:
            continue
        both = a.notna() & rets[etf].notna()
        if both.sum() < 30 or not rets[etf][both].var():
            continue
        out.append({"hedge": etf, "corr_60d": round(float(a[both].corr(rets[etf][both])), 2),
                    "beta_60d": round(float(a[both].cov(rets[etf][both]) / rets[etf][both].var()), 2)})
    out.sort(key=lambda r: -r["corr_60d"])
    return out[:top] + [r for r in out[top:] if r["hedge"] == "SPY"]

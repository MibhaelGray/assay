"""What the book learns from: post-mortems, and every proposal's result.

    journal/paper/postmortems/<date>.json     a tag and a one-line lesson for each finished trade
    journal/paper/lessons.md                  the standing lessons, kept by the research pass and read at its start

Every proposal, taken or passed, is scored against its own plan (from the price when its pass was acted on to
the stop or the time limit, before costs), so the passes are a control for the takes: if the takes don't beat
them, the research isn't adding anything. A trade a review managed (closed early or extended) is also compared
with what its original plan would have made. `brief()` puts all of it on one page for the next pass to read.
"""

import json
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import mean, median

import pandas as pd

from .. import bars, journal, storage
from ..config import CT, NY
from ..universe import ETFS
from . import book, proposals, rules

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
EST_COST_BPS = 25.0           # transparent round-trip cost assumption (spread + commissions) for plan tests
UNINFLUENCED_FROM = "2026-11-26"  # first session where the judge ran without lessons.md / fr paper learn
CONVICTION_V2_FROM = "2026-11-30" # first session on scale 2 (redefined 4/5 conviction + required p_win)
P_WIN_BINS = ((5, 39), (40, 49), (50, 59), (60, 69), (70, 95))   # fixed before any p_win was scored
GATE_FOURS = 30               # settled scale-2 takes at conviction 4 or 5 before the sizing review
MONITOR_TAKES = 30            # collapse monitor window (latest scale-2 takes)
POWER_Z_ALPHA = 1.64485       # one-sided alpha = 0.05
POWER_Z_BETA = 0.84162        # 80% power
POWER_CLUSTER_DEFF = 1.25     # conservative inflation for within-session correlation across same-day takes
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
    return storage.atomic_write_text(path, json.dumps(payload, indent=1, default=journal._json_default) + "\n")


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
    closed = {r["id"]: r for r in book.rows(book.MAIN) if r["state"] == "closed"}
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
    return [r for r in book.rows(book.MAIN) if r["state"] == "closed" and r["id"] not in have
            and not (r["reason"] or "").startswith("broken")]


# ---- Results ----------------------------------------------------------------------------------------------
def _verdict(p: dict) -> str:
    return p["decision"] if p.get("reviewed", True) else "not reviewed"


def intraday_symbols(today: date | None = None) -> list[str]:
    """The legs of every verdict whose plan may still need 5-minute bars: recorded since the restart, within
    the last 60 days. `fr bars --intraday` pulls them."""
    today = today or datetime.now(NY).date()
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
                  today: date, scale: dict[str, float] | None = None) -> tuple[str, tuple[datetime, float] | None]:
    """Replay a spread stop on `day`'s checks. ("hit", (when, return)) at the first check past it, ("clear", None)
    if none was, ("wait", None) while the day's 5-minute bars aren't stored yet, ("none", None) once they aren't
    coming. A check's price is the close of the last 5-minute bar that finished by then.
    `scale` maps symbol -> f(day) / f(entry_day) so mid-hold dividends/splits stay exact on unadjusted 5m bars."""
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
        ret = sum(w[s] * ((seen[s].iloc[-1] * (scale.get(s, 1.0) if scale else 1.0)) / entry[s] - 1) for s in legs)
        if ret * 100 <= pct:
            return "hit", (t, ret)
    return "clear", None


def _day_ready(day: date, legs: list[str], close: dict, ix: dict | None, replay: bool, today: date) -> bool:
    """Whether `day` has settled enough that a surviving plan ending on `day` would have finished."""
    if not journal._bars_settled(day) or any(s not in close or day not in close[s].index for s in [*legs, "SPY"]):
        return False
    if not replay or (today - day).days > INTRADAY_WAIT:
        return True
    close_dt = datetime.combine(day, journal.close_time(day), NY)
    open_dt = datetime.combine(day, time(9, 30), NY)
    for s in legs:
        x = ix.get(s) if ix is not None else None
        x = x[(x.index >= open_dt) & (x.index < close_dt)] if x is not None else None
        if x is None or x.empty or x.index[-1] < close_dt - timedelta(minutes=5):
            return False
    return True


def _outright_and_hedged_legs(p: dict) -> tuple[tuple[list[str], list[str]], tuple[list[str], list[str]] | None]:
    """(outright_legs, hedged_legs) for a proposal. Factor pairs have no separate hedge."""
    from . import rules

    longs, shorts = list(p["long"]), list(p["short"])
    if p.get("kind") == "factor":
        return (longs, shorts), (longs, shorts)
    hedge = rules._hedge(longs, shorts)
    if hedge is None and p.get("kind") == "etf" and p.get("name") != "SPY":
        if longs == ["SPY"] and shorts:
            hedge = "SPY"
        elif shorts == ["SPY"] and longs:
            hedge = "SPY"
    outright = ([s for s in longs if s != hedge], [s for s in shorts if s != hedge]) if hedge else (longs, shorts)
    if longs and shorts:
        hedged: tuple[list[str], list[str]] | None = (longs, shorts)
    else:
        dh = (p.get("signal") or {}).get("hedge")
        if dh and dh not in longs and dh not in shorts:
            hedged = (longs, [dh]) if longs else ([dh], shorts)
        else:
            hedged = None
    return outright, hedged


def _default_legs_and_plan(p: dict) -> tuple[tuple[list[str], list[str]], dict]:
    """Mechanical default legs (hedged where available) and default plan (rules.default_plan) for apples-to-apples
    scoring of takes and passes under the exact same rules."""
    _, hedged = _outright_and_hedged_legs(p)
    legs = hedged if hedged is not None else (list(p["long"]), list(p["short"]))
    sig = p.get("signal") or {}
    plan = rules.default_plan({"kind": p.get("kind") or "stock", "setup": p["setup"], "numbers": sig.get("numbers") or {}})
    return legs, plan


def unfilled_reason(p: dict, entry_doc: dict | None) -> str:
    """Classify why a take never filled in the paper book: spread_liquidity, cap_skipped, gateway_fault, or other."""
    if not entry_doc:
        return "other"
    pid, kind = p["id"], p.get("pass")
    trade = next((t for t in entry_doc.get("trades", []) if t.get("proposal_id") == pid), None)
    text_parts: list[str] = []
    if trade:
        if trade.get("stood_down"):
            text_parts.append(str(trade["stood_down"]))
        tid = trade.get("id")
        for run in entry_doc.get("runs", []):
            for w in run.get("warnings", []):
                if tid and tid in w:
                    text_parts.append(w)
    else:
        exact: list[str] = []
        legacy: list[str] = []
        for run in entry_doc.get("runs", []):
            skips = run.get("skipped", [])
            has_pid = any("proposal_id" in s for s in skips)
            for s in skips:
                if s.get("proposal_id") == pid or s.get("name") == pid:
                    exact.append(str(s.get("reason") or ""))
                elif not has_pid and kind and s.get("pass") == kind:
                    legacy.append(str(s.get("reason") or ""))
        text_parts.extend(exact if exact else legacy)
    blob = " | ".join(text_parts).lower()
    if any(k in blob for k in ("10089", "10197", "in use somewhere else", "no market data subscription", "timed out", "couldn't be asked")):
        return "gateway_fault"
    if any(k in blob for k in ("spread wider", "no bid or ask", "no overnight", "filled nothing", "no fill", "not chasing", "no overnight fills")):
        return "spread_liquidity"
    if any(k in blob for k in ("gross would be", "net exposure", "already holds", "over 60 open", "halted", "paused", "conviction came first")):
        return "cap_skipped"
    return "other"


def _mark_outcome(p: dict, px: pd.DataFrame, ix: dict | None = None, today: date | None = None,
                  override_legs: tuple[list[str], list[str]] | None = None,
                  override_plan: dict | None = None) -> dict | None:
    """The plan result of a verdict from one of the three daily passes: from each leg's price when the order job
    first acted on the pass (entries marks: the same moment for a take and a pass) to the close where its stop
    is hit or its time limit ends, before costs. A morning or midday verdict is already partway through its
    session, so that day's close only checks the stop and the time limit counts from the next session, as it
    does for the trade itself. None until the pass has been marked; status "open" while sessions remain.

    `mature` is True only once the plan's scheduled time limit (`due`, days[-1]) has settled: an early stop-out
    records its final result on its own row at once, but waits for its time limit to enter the aggregate tables
    so stopped-out losers don't get counted ahead of surviving peers from the same pass. While `status == "open"`,
    an unsettled bar for a day inside `days` (from a live dashboard refresh) marks `bps` to the latest price."""
    from . import entries

    session = date.fromisoformat(p["session"])
    m = entries.marks(session, p["pass"])
    if not m:
        return None
    longs, shorts = override_legs if override_legs is not None else (p["long"], p["short"])
    legs = [*longs, *shorts]
    if not legs:
        return None
    entry = m["prices"]
    if lost := [s for s in [*legs, "SPY"] if s not in entry]:
        return {"status": "unpriced", "note": f"no price for {', '.join(lost)} when the {p['pass']} pass was acted on"}
    plan_cfg = override_plan if override_plan is not None else p["plan"]
    horizon = plan_cfg["horizon_days"]
    first = entries.first_session(p["pass"], session)
    days = ([session] if first != session else []) + journal.sessions_from(first, horizon)
    due = days[-1]
    sub = px[px["symbol"].isin([*legs, "SPY"])]
    close = {s: g.assign(day=g["date"].dt.date).set_index("day")["close"] for s, g in sub.groupby("symbol")}
    raw_close = (
        {s: g.assign(day=g["date"].dt.date).set_index("day")["raw_close"].dropna() for s, g in sub.groupby("symbol")}
        if "raw_close" in sub.columns else {}
    )

    def factor_on(sym: str, day: date) -> float:
        cs, rs = close.get(sym), raw_close.get(sym)
        if cs is None or rs is None or day not in cs.index or day not in rs.index:
            return 1.0
        r = float(rs.loc[day])
        return float(cs.loc[day]) / r if r > 0 else 1.0

    def anchor_factor(sym: str, anchor: date) -> float:
        cs, rs = close.get(sym), raw_close.get(sym)
        if cs is None or rs is None or rs.empty:
            return 1.0
        if anchor in rs.index and anchor in cs.index and rs.loc[anchor] > 0:
            return float(cs.loc[anchor]) / float(rs.loc[anchor])
        common = rs.index.intersection(cs.index)
        later = [d for d in sorted(common) if d >= anchor]
        pick = later[0] if later else (sorted(common)[-1] if len(common) else None)
        if pick is not None and rs.loc[pick] > 0:
            return float(cs.loc[pick]) / float(rs.loc[pick])
        return 1.0

    anchor_day = journal.previous_trading_day(session) if p.get("pass") in proposals.NIGHT_KINDS else session
    f0 = {s: anchor_factor(s, anchor_day) for s in [*legs, "SPY"]}
    adj_entry = {s: entry[s] * f0[s] for s in [*legs, "SPY"]}
    w = journal._weights({"long": longs or None, "short": shorts or None})
    stop = plan_cfg.get("stop") or {}
    replay = ix is not None and "spread_pct" in stop and session >= INTRADAY_FROM
    today = today or datetime.now(NY).date()
    at = datetime.fromisoformat(m["at"]).astimezone(NY) if m.get("at") else None
    path, status, note, intraday = [], "open", "", "replayed" if replay else None
    for d in days:
        if any(s not in close or d not in close[s].index for s in [*legs, "SPY"]) or not journal._bars_settled(d):
            break
        if replay:
            day_scale = {s: factor_on(s, d) / f0[s] for s in legs}
            how, hit = _intraday_hit(d, legs, w, entry, stop["spread_pct"], ix, at if d == session else None, today, scale=day_scale)
            if how == "wait":
                break
            if how == "none":
                intraday = "closes only on some days: no 5-minute bars"
            if hit:
                when, ret = hit
                path.append((d, ret, close["SPY"][d] / adj_entry["SPY"] - 1))
                status = "stopped"
                note = (f"Stopped at {when.astimezone(CT):%-I:%M %p} CT on {d}: down {abs(ret) * 100:.1f}% "
                        f"at a 10-minute check, past the {abs(stop['spread_pct']):g}% stop")
                break
        ret = sum(w[s] * (close[s][d] / adj_entry[s] - 1) for s in legs)
        path.append((d, ret, close["SPY"][d] / adj_entry["SPY"] - 1))
        hits = [f"{r['symbol']} closed at {close[r['symbol']][d] / f0[r['symbol']]:,.2f} ({journal.rule_text(r)})"
                for r in stop.get("rules", []) if journal._rule_hit(r, close[r["symbol"]][d] / f0[r["symbol"]])]
        if "spread_pct" in stop and ret * 100 <= stop["spread_pct"]:
            hits.append(f"down {abs(ret) * 100:.1f}% from the pass's price, past the {abs(stop['spread_pct']):g}% stop")
        if hits:
            status, note = "stopped", f"Stopped at the close of {d}: " + "; ".join(hits)
            break
        if d == due:
            status, note = "time", f"Held to the time limit, {horizon} session{'s' if horizon > 1 else ''}"
    # If the plan is still open and today's unsettled quote bar is stored (e.g. from a live refresh), mark it there.
    live_mark = None
    if status == "open":
        seen = {x[0] for x in path}
        for d in reversed(days):
            if d in seen:
                break
            if all(s in close and d in close[s].index for s in [*legs, "SPY"]):
                live_mark = (d, sum(w[s] * (close[s][d] / adj_entry[s] - 1) for s in legs), close["SPY"][d] / adj_entry["SPY"] - 1)
                break
    if not path and not live_mark:
        return {"status": "open", "mature": False, "due": due.isoformat(), "note": "its first close hasn't settled"}
    marks_path = [*path, live_mark] if live_mark else path
    d, ret, spy = marks_path[-1]
    rets = [x[1] for x in marks_path]
    mature = status == "time" or (status == "stopped" and (d == due or _day_ready(due, legs, close, ix, replay, today)))
    return {"status": status, "mature": mature, "due": due.isoformat(), "note": note, "exit_date": d.isoformat(),
            "sessions": len(path), "horizon_days": horizon, "intraday": intraday,
            "bps": round(ret * 1e4, 1), "spy_bps": round(spy * 1e4, 1), "best_bps": round(max(rets) * 1e4, 1),
            "worst_bps": round(min(rets) * 1e4, 1), "hit": bool(ret > 0)}


def outcomes() -> list[dict]:
    """One row per proposal: the verdict, the result of its own plan on daily bars (`plan_bps`, before costs,
    None until it finishes), both outright and hedged replays, identical mechanical default-plan scoring
    (`default_plan_bps`) for apples-to-apples take-vs-pass testing, fill status / unfilled reason, its door/route,
    and for a take that was traded, the paper result after commissions."""
    from ..screen import route_of
    from . import entries

    props = proposals.every()
    if not props:
        return []
    paper = {r["proposal_id"]: r for r in book.rows(book.MAIN) if r.get("proposal_id")}
    trades_by_pid = {t["proposal_id"]: t for t in book.trades()
                     if book.book_of(t) == book.MAIN and t.get("proposal_id")}
    entry_docs: dict[str, dict | None] = {}
    tags = postmortems()
    default_hedges = {h for p in props if (h := (p.get("signal") or {}).get("hedge"))}
    symbols = sorted({s for p in props for s in [*p["long"], *p["short"]]} | default_hedges | {"SPY"})
    px = bars.load(symbols)
    ix = bars.load_intraday([s for s in symbols if s in set(intraday_symbols()) | default_hedges])
    out = []
    for p in props:
        plan = outright = hedged = def_plan_res = None
        if p["plan"] and (p["long"] or p["short"]) and not px.empty:
            plan = _mark_outcome(p, px, ix)
            out_legs, hdg_legs = _outright_and_hedged_legs(p)
            own_legs = (list(p["long"]), list(p["short"]))
            outright = plan if out_legs == own_legs else _mark_outcome(p, px, ix, override_legs=out_legs)
            hedged = plan if hdg_legs == own_legs else (_mark_outcome(p, px, ix, override_legs=hdg_legs) if hdg_legs else None)
            def_legs, def_plan = _default_legs_and_plan(p)
            if def_legs == own_legs and def_plan == p["plan"]:
                def_plan_res = plan
            else:
                def_plan_res = _mark_outcome(p, px, ix, override_legs=def_legs, override_plan=def_plan)
        done = bool(plan) and plan["status"] in ("time", "stopped")
        out_done = bool(outright) and outright["status"] in ("time", "stopped")
        hdg_done = bool(hedged) and hedged["status"] in ("time", "stopped")
        def_done = bool(def_plan_res) and def_plan_res["status"] in ("time", "stopped")
        door, route = (p["door"], p["route"]) if p.get("door") and p.get("route") else route_of(p["setup"], p["kind"])
        r = paper.get(p["id"])
        tr = trades_by_pid.get(p["id"])
        fill_status = u_reason = None
        if p["decision"] == "take":
            if tr and tr.get("held"):
                fill_status = "filled"
            else:
                fill_status = "unfilled"
                sess = p["session"]
                if sess not in entry_docs:
                    entry_docs[sess] = entries.load(date.fromisoformat(sess))
                u_reason = unfilled_reason(p, entry_docs[sess])
        reason = (r or {}).get("reason") or ""
        managed = "early exit" if reason.startswith(("review:", "midday check:")) else "extended" if (r or {}).get("extended") else None
        m = tags.get((r or {}).get("id"))
        out.append({
            "id": p["id"], "session": p["session"], "name": p["name"], "kind": p["kind"], "setup": p["setup"],
            "door": door, "route": route, "direction": p["direction"],
            "source": p["source"], "pass": p.get("pass"), "verdict": _verdict(p), "decision": p["decision"],
            "conviction": p["conviction"], "p_win": p.get("p_win"), "scale": p.get("scale", 1),
            "judge": p.get("judge"), "custom": bool(plan) and def_plan_res is not plan, "reason": p["reason"],
            "rank": p.get("rank"), "ranked": p.get("ranked"), "recorded_at": p.get("recorded_at"),
            "legs": " / ".join(filter(None, [", ".join(p["long"]) and f"long {', '.join(p['long'])}", ", ".join(p["short"]) and f"short {', '.join(p['short'])}"])),
            "horizon_days": (p["plan"] or {}).get("horizon_days"), "stop_pct": ((p["plan"] or {}).get("stop") or {}).get("spread_pct"),
            "stats": p.get("stats"), "wrong_if": p.get("wrong_if"), "reasoning": p.get("reasoning"), "hedge_why": p.get("hedge_why"),
            "why": (p.get("signal") or {}).get("why"),
            "plan_status": plan["status"] if plan else None, "plan_bps": plan["bps"] if done else None,
            "spy_bps": plan.get("spy_bps") if done else None,
            "plan_mature": bool(plan and plan.get("mature")), "plan_due": plan.get("due") if plan else None,
            "plan_so_far_bps": plan["bps"] if plan and not done and "bps" in plan else None,
            "plan_note": plan.get("note") if plan else None, "plan_exit": plan.get("exit_date") if done else None,
            "default_plan_bps": def_plan_res["bps"] if def_done else None,
            "default_plan_mature": bool(def_plan_res and def_plan_res.get("mature")),
            "default_plan_so_far_bps": def_plan_res["bps"] if def_plan_res and not def_done and "bps" in def_plan_res else None,
            "outright_bps": outright["bps"] if out_done else None,
            "outright_mature": bool(outright and outright.get("mature")),
            "outright_so_far_bps": outright["bps"] if outright and not out_done and "bps" in outright else None,
            "hedged_bps": hedged["bps"] if hdg_done else None,
            "hedged_mature": bool(hedged and hedged.get("mature")),
            "hedged_so_far_bps": hedged["bps"] if hedged and not hdg_done and "bps" in hedged else None,
            "fill_status": fill_status, "unfilled_reason": u_reason,
            "trade_id": r and r["id"], "state": r and r["state"], "paper_bps": r["bps"] if r and r["state"] == "closed" else None,
            "exit_reason": reason or None, "managed": managed, "tag": m and m["tag"], "lesson": m and m["lesson"],
        })
    return out


def _settled(r: dict) -> bool:
    """Finished AND past its scheduled time limit, so early stop-outs don't enter averages ahead of surviving peers."""
    return r["plan_bps"] is not None and r.get("plan_mature", True)


def _current_bps(r: dict) -> float | None:
    """The plan's final result if finished, else its latest mark-to-market return while still open."""
    return r["plan_bps"] if r["plan_bps"] is not None else r.get("plan_so_far_bps")


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


def _t_pvalue(t_stat: float | None, df: int) -> float | None:
    """One-sided Student-t p-value for H1: mean > 0 (pure Python via Simpson's rule, zero external deps)."""
    if t_stat is None or df < 1 or not math.isfinite(t_stat):
        return None
    x = abs(t_stat)
    if x >= 50.0:
        return 0.0 if t_stat > 0 else 1.0
    if x == 0.0:
        return 0.5
    coeff = math.exp(math.lgamma((df + 1) / 2) - math.lgamma(df / 2)) / math.sqrt(df * math.pi)
    power = -(df + 1) / 2
    steps = 400
    h = x / steps
    pdf = lambda u: coeff * (1.0 + (u * u) / df) ** power  # noqa: E731
    total = pdf(0.0) + pdf(x)
    for i in range(1, steps):
        total += (4.0 if i % 2 else 2.0) * pdf(i * h)
    half_area = total * h / 3.0
    p = 0.5 - half_area if t_stat > 0 else 0.5 + half_area
    return round(min(1.0, max(0.0, p)), 4)


def _sample_sd(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    m = mean(values)
    return (sum((v - m) ** 2 for v in values) / (len(values) - 1)) ** 0.5


def _session_stat(values: list[float], max_lag: int = 4) -> dict:
    """Mean, session-clustered t-stat and one-sided p-value across session-level observations, plus Newey-West
    (Bartlett HAC) lag-adjusted t_hac/p_value_hac to account for overlapping multi-day holds."""
    n = len(values)
    if not n:
        return {"sessions": 0, "mean_bps": None, "t": None, "p_value": None, "t_hac": None, "p_value_hac": None}
    m = mean(values)
    sd = _sample_sd(values) if n >= 3 else None
    t_val = m / (sd / math.sqrt(n)) if sd and sd > 0 else None
    t_hac = None
    if n >= 3:
        dev = [v - m for v in values]
        g0 = sum(d * d for d in dev) / (n - 1)
        lags = min(max_lag, n - 2)
        lrv = g0
        for lag in range(1, lags + 1):
            w = 1.0 - lag / (lags + 1)
            gl = sum(dev[i] * dev[i - lag] for i in range(lag, n)) / (n - 1)
            lrv += 2.0 * w * gl
        if lrv > 0:
            t_hac = m / math.sqrt(lrv / n)
    return {"sessions": n, "mean_bps": round(m, 1),
            "t": round(t_val, 2) if t_val is not None else None,
            "p_value": _t_pvalue(t_val, n - 1),
            "t_hac": round(t_hac, 2) if t_hac is not None else None,
            "p_value_hac": _t_pvalue(t_hac, n - 1)}


def _trimmed_mean(values: list[float], pct: float = 0.05) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = int(len(s) * pct)
    if k == 0 and len(s) >= 5:
        k = 1
    core = s[k:len(s) - k] if k and len(s) > 2 * k else s
    return mean(core)


def _rank_summary(groups: list[tuple[str, list[tuple[dict, float]]]]) -> dict:
    scored = []
    for session, items in groups:
        ic = _spearman([-r["rank"] for r, _ in items], [v for _, v in items]) if len(items) >= MIN_RANKED else None
        if ic is not None:
            scored.append({"session": session, "finished": len(items), "ranked": items[0][0]["ranked"], "ic": round(ic, 3)})
    # Cluster by session: average multiple passes within the same session into one observation so 3 passes/day
    # don't inflate `t` as if their overlapping holds were independent.
    by_session: dict[str, list[float]] = {}
    for row in scored:
        by_session.setdefault(row["session"], []).append(row["ic"])
    session_ics = [mean(vs) for _, vs in sorted(by_session.items())]
    sd = (sum((v - mean(session_ics)) ** 2 for v in session_ics) / (len(session_ics) - 1)) ** 0.5 if len(session_ics) > 2 else 0
    third = lambda r: min(2, (r["rank"] - 1) * 3 // r["ranked"])  # noqa: E731
    done = [item for _, items in groups for item in items]
    return {"finished": len(done), "nights": len(session_ics), "passes": len(scored), "nights_needed": RANK_NIGHTS,
            "ic": round(mean(session_ics), 3) if session_ics else None,
            "t": round(mean(session_ics) / (sd / len(session_ics) ** 0.5), 2) if sd else None,
            "thirds": {name: _stat([v for r, v in done if third(r) == k]) for k, name in enumerate(("top", "middle", "bottom"))},
            "by_night": scored}


def rank_table(rows: list[dict] | None = None) -> dict:
    """Whether the pass's ranking predicts results. Each ranked draft is one pass: a pass enters the settled ranking
    only once every priceable idea in it has reached its scheduled time limit (so early stop-outs don't score a pass
    before its surviving ideas finish). `so_far` scores every pass right now using final results for finished ideas
    and latest mark-to-market returns for open ones."""
    rows = outcomes() if rows is None else rows
    ranked = [r for r in rows if r.get("rank")]
    passes: dict[tuple, list[dict]] = {}
    for r in ranked:
        passes.setdefault((r["session"], r["recorded_at"]), []).append(r)
    settled_groups, live_groups = [], []
    for (session, _), rs in sorted(passes.items()):
        priceable = [r for r in rs if r["plan_status"] not in (None, "unpriced")]
        if len(priceable) >= MIN_RANKED and all(_settled(r) for r in priceable):
            settled_groups.append((session, [(r, float(r["plan_bps"])) for r in priceable]))
        live = [(r, float(v)) for r in priceable if (v := _current_bps(r)) is not None]
        if len(live) >= MIN_RANKED:
            live_groups.append((session, live))
    return {"ranked": len(ranked), **_rank_summary(settled_groups), "so_far": _rank_summary(live_groups)}


def _default_settled(r: dict) -> bool:
    return r.get("default_plan_bps") is not None and r.get("default_plan_mature", True)


def main_test(rows: list[dict] | None = None, cost_bps: float = EST_COST_BPS) -> dict:
    """Pre-registered statistical test of PROJECT.md's main question: do takes beat passes after costs?

    Separates:
      - raw_gap_bps: custom take plan vs screen-default pass plan, after cost_bps
      - default_gap_bps: identical mechanical default plan for BOTH takes and passes, after cost_bps
      - plan_custom_bps: judge's custom stop/horizon/hedge minus default plan on takes
      - session_clustered: one takes-minus-passes observation per session (default plan, after cost_bps)
      - stratified: Mantel-Haenszel harmonic-weighted difference within (setup, direction) strata (default plan,
        after cost_bps), both pooled and session-clustered
      - outliers: ex-top-2 takes, 5% trimmed mean gap, and median gap (default plan, after cost_bps)
      - power: takes needed for 80% power at one-sided p < 0.05 given observed take/pass dispersion
      - execution: realized filled paper book vs unfilled takes broken out by reason
    """
    rows = outcomes() if rows is None else rows
    settled = [r for r in rows if _settled(r)]
    raw_t = [float(r["plan_bps"]) for r in settled if r["verdict"] == "take"]
    raw_p = [float(r["plan_bps"]) for r in settled if r["verdict"] == "pass"]
    raw_gap = round(mean(raw_t) - mean(raw_p) - cost_bps, 1) if raw_t and raw_p else None

    def_settled = [r for r in rows if _default_settled(r)]
    dt = [float(r["default_plan_bps"]) for r in def_settled if r["verdict"] == "take"]
    dp = [float(r["default_plan_bps"]) for r in def_settled if r["verdict"] == "pass"]
    def_gap = round(mean(dt) - mean(dp) - cost_bps, 1) if dt and dp else None

    custom_deltas = [float(r["plan_bps"]) - float(r["default_plan_bps"])
                     for r in rows if r["verdict"] == "take" and _settled(r) and _default_settled(r)]
    plan_custom = round(mean(custom_deltas), 1) if custom_deltas else None

    # Session-clustered default-plan take-minus-pass gap (after costs).
    by_sess: dict[str, list[dict]] = {}
    for r in def_settled:
        if r["verdict"] in ("take", "pass"):
            by_sess.setdefault(r["session"], []).append(r)
    sess_gaps: list[float] = []
    sess_strat_gaps: list[float] = []
    for _, grp in sorted(by_sess.items()):
        ts = [float(r["default_plan_bps"]) for r in grp if r["verdict"] == "take"]
        ps = [float(r["default_plan_bps"]) for r in grp if r["verdict"] == "pass"]
        if ts and ps:
            sess_gaps.append(mean(ts) - mean(ps) - cost_bps)
        # Within-session stratification by (setup, direction).
        strata_s: dict[tuple[str, str], tuple[list[float], list[float]]] = {}
        for r in grp:
            bucket = strata_s.setdefault((r["setup"], r["direction"]), ([], []))
            (bucket[0] if r["verdict"] == "take" else bucket[1]).append(float(r["default_plan_bps"]))
        w_sum = num = 0.0
        for st, sp in strata_s.values():
            if st and sp:
                w = (len(st) * len(sp)) / (len(st) + len(sp))
                w_sum += w
                num += w * (mean(st) - mean(sp))
        if w_sum > 0:
            sess_strat_gaps.append(num / w_sum - cost_bps)
    session_clustered = _session_stat(sess_gaps)

    # Pooled stratification by (setup, direction) across all settled sessions (default plan, after costs).
    pooled_strata: dict[tuple[str, str], tuple[list[float], list[float]]] = {}
    for r in def_settled:
        if r["verdict"] in ("take", "pass"):
            bucket = pooled_strata.setdefault((r["setup"], r["direction"]), ([], []))
            (bucket[0] if r["verdict"] == "take" else bucket[1]).append(float(r["default_plan_bps"]))
    w_sum = num = 0.0
    matched_strata = matched_t = matched_p = 0
    for st, sp in pooled_strata.values():
        if st and sp:
            matched_strata += 1
            matched_t += len(st)
            matched_p += len(sp)
            w = (len(st) * len(sp)) / (len(st) + len(sp))
            w_sum += w
            num += w * (mean(st) - mean(sp))
    stratified = {
        "strata": matched_strata, "takes": matched_t, "passes": matched_p,
        "mean_bps": round(num / w_sum - cost_bps, 1) if w_sum > 0 else None,
        "session_clustered": _session_stat(sess_strat_gaps),
    }

    # Outlier sensitivity on settled default-plan takes vs passes (after costs).
    ex_top_2 = None
    if len(dt) > 2 and dp:
        ex_top_2 = round(mean(sorted(dt)[:-2]) - mean(dp) - cost_bps, 1)
    trim_t, trim_p = _trimmed_mean(dt, 0.05), _trimmed_mean(dp, 0.05)
    trimmed_5 = round(trim_t - trim_p - cost_bps, 1) if trim_t is not None and trim_p is not None else None
    median_gap = round(median(dt) - median(dp) - cost_bps, 1) if dt and dp else None
    outliers = {"ex_top_2_bps": ex_top_2, "trimmed_5pct_bps": trimmed_5, "median_gap_bps": median_gap}

    # Power calculation: takes needed for 80% power at one-sided alpha = 0.05 given observed dispersion.
    sd_t = _sample_sd(dt)
    sd_p = _sample_sd(dp)
    takes_200 = takes_100 = None
    if sd_t and sd_t > 0:
        ratio = max(1.0, len(dp) / len(dt)) if dp and dt else 1.0
        eff_var = (sd_t ** 2 + ((sd_p ** 2) / ratio if sd_p else 0.0)) * POWER_CLUSTER_DEFF
        z_sum = POWER_Z_ALPHA + POWER_Z_BETA
        takes_200 = math.ceil((z_sum ** 2) * eff_var / (200.0 ** 2))
        takes_100 = math.ceil((z_sum ** 2) * eff_var / (100.0 ** 2))

    # Verdict uses the primary clustered p-value (preferring stratified session-clustered once >= 6 sessions match).
    primary = stratified["session_clustered"] if stratified["session_clustered"]["sessions"] >= 6 else session_clustered
    if not dt or (takes_200 is not None and len(dt) < takes_200) or primary["sessions"] < 10:
        verdict = "UNDERPOWERED — too early to call"
    elif primary["p_value"] is not None and primary["p_value"] < 0.05 and (primary["mean_bps"] or 0) > 0:
        verdict = "PASS (p < 0.05)"
    else:
        verdict = "NO SIGNIFICANT EDGE"
    power = {"take_sd_bps": round(sd_t, 1) if sd_t is not None else None, "n_takes": len(dt),
             "takes_for_200bp": takes_200, "takes_for_100bp": takes_100, "verdict": verdict}

    # Execution breakdown: filled vs unfilled takes (execution leak diagnostic).
    takes_all = [r for r in rows if r["verdict"] == "take"]
    filled_all = [r for r in takes_all if r.get("fill_status") == "filled"]
    unfilled_all = [r for r in takes_all if r.get("fill_status") == "unfilled"]
    closed_paper = [float(r["paper_bps"]) for r in filled_all if r.get("paper_bps") is not None]
    unfilled_settled = [float(r["plan_bps"]) for r in unfilled_all if _settled(r)]
    unfilled_live = [float(v) for r in unfilled_all if (v := _current_bps(r)) is not None]
    u_reasons: dict[str, int] = {}
    for r in unfilled_all:
        k = r.get("unfilled_reason") or "other"
        u_reasons[k] = u_reasons.get(k, 0) + 1
    execution = {
        "filled_takes": len(filled_all),
        "closed_paper": _stat(closed_paper),
        "unfilled_takes": len(unfilled_all),
        "unfilled_settled": _stat(unfilled_settled),
        "unfilled_so_far": _stat(unfilled_live),
        "unfilled_reasons": u_reasons,
    }

    # Second test (PROJECT.md): takes vs SPY over the exact same hold, after costs.
    vs_spy_takes = [float(r["plan_bps"]) - float(r["spy_bps"]) - cost_bps
                    for r in settled if r["verdict"] == "take" and r.get("spy_bps") is not None]
    by_sess_spy: dict[str, list[float]] = {}
    for r in settled:
        if r["verdict"] == "take" and r.get("spy_bps") is not None:
            by_sess_spy.setdefault(r["session"], []).append(float(r["plan_bps"]) - float(r["spy_bps"]) - cost_bps)
    paper_vs_spy = [float(r["paper_bps"]) - float(r["spy_bps"])
                    for r in filled_all if r.get("paper_bps") is not None and r.get("spy_bps") is not None]
    vs_spy = {
        "n_takes": len(vs_spy_takes),
        "plan_mean_bps": round(mean(vs_spy_takes), 1) if vs_spy_takes else None,
        "session_clustered": _session_stat([mean(v) for _, v in sorted(by_sess_spy.items())]),
        "paper_n": len(paper_vs_spy),
        "paper_mean_bps": round(mean(paper_vs_spy), 1) if paper_vs_spy else None,
    }

    # Regime split: pre-uninfluenced (before 2026-11-26, when judge read lessons.md) vs uninfluenced desk.
    def _regime_slice(subset: list[dict]) -> dict:
        rt = [float(r["default_plan_bps"]) for r in subset if r["verdict"] == "take"]
        rp = [float(r["default_plan_bps"]) for r in subset if r["verdict"] == "pass"]
        by_s: dict[str, list[dict]] = {}
        for r in subset:
            if r["verdict"] in ("take", "pass"):
                by_s.setdefault(r["session"], []).append(r)
        sg = []
        for _, grp in sorted(by_s.items()):
            ts = [float(r["default_plan_bps"]) for r in grp if r["verdict"] == "take"]
            ps = [float(r["default_plan_bps"]) for r in grp if r["verdict"] == "pass"]
            if ts and ps:
                sg.append(mean(ts) - mean(ps) - cost_bps)
        return {
            "n_takes": len(rt), "n_passes": len(rp),
            "default_gap_bps": round(mean(rt) - mean(rp) - cost_bps, 1) if rt and rp else None,
            "session_clustered": _session_stat(sg),
        }

    regimes = {
        "pre_uninfluenced": _regime_slice([r for r in def_settled if r["session"] < UNINFLUENCED_FROM]),
        "uninfluenced": _regime_slice([r for r in def_settled if r["session"] >= UNINFLUENCED_FROM]),
    }

    return {
        "cost_bps": cost_bps,
        "n_takes": len(raw_t), "n_passes": len(raw_p),
        "raw_gap_bps": raw_gap,
        "default_gap_bps": def_gap,
        "plan_custom_bps": plan_custom,
        "session_clustered": session_clustered,
        "stratified": stratified,
        "outliers": outliers,
        "power": power,
        "execution": execution,
        "vs_spy": vs_spy,
        "regimes": regimes,
    }


def _v2(r: dict) -> bool:
    return r.get("scale") == 2 and r.get("p_win") is not None


def _auc(scores: list[float], wins: list[bool]) -> float | None:
    """Probability a winner received a higher score than a loser (ties count half). None without both classes."""
    pos, neg = [s for s, w in zip(scores, wins) if w], [s for s, w in zip(scores, wins) if not w]
    if not pos or not neg:
        return None
    return sum((a > b) + 0.5 * (a == b) for a in pos for b in neg) / (len(pos) * len(neg))


def _p_score(rs: list[dict], key: str = "plan_bps") -> dict:
    if not rs:
        return {"n": 0}
    said, won = [r["p_win"] / 100 for r in rs], [bool(r[key] > 0) for r in rs]
    base = sum(won) / len(won)
    brier = mean((s - w) ** 2 for s, w in zip(said, won))
    auc = _auc(said, won)
    return {"n": len(rs), "said": round(mean(said), 3), "won": round(base, 3), "brier": round(brier, 4),
            "skill": round(1 - brier / (base * (1 - base)), 3) if 0 < base < 1 else None,
            "auc": round(auc, 3) if auc is not None else None}


def p_win_table(rows: list[dict] | None = None) -> dict:
    """Calibration and discrimination of scale-2 `p_win` on settled proposals (scored on `plan_bps > 0`,
    plus `takes_default` against `default_plan_bps > 0` for pooled ML target comparison)."""
    rows = outcomes() if rows is None else rows
    done = [r for r in rows if _v2(r) and _settled(r)]
    takes = [r for r in done if r["verdict"] == "take"]
    passes: dict[tuple, list[dict]] = {}
    for r in done:
        passes.setdefault((r["session"], r.get("recorded_at")), []).append(r)
    by_session: dict[str, list[tuple[float, float]]] = {}
    for (session, _), rs in sorted(passes.items()):
        if len(rs) >= MIN_RANKED and all(r.get("rank") is not None for r in rs):
            a = _spearman([r["p_win"] for r in rs], [r["plan_bps"] for r in rs])
            b = _spearman([-r["rank"] for r in rs], [r["plan_bps"] for r in rs])
            if a is not None and b is not None:
                by_session.setdefault(session, []).append((a, b))
    ics = [(mean(a for a, _ in v), mean(b for _, b in v)) for _, v in sorted(by_session.items())]
    return {
        "all": _p_score(done), "takes": _p_score(takes), "passes": _p_score([r for r in done if r["verdict"] != "take"]),
        "takes_default": _p_score([r for r in takes if r.get("default_plan_bps") is not None], "default_plan_bps"),
        "bins": {f"{lo}-{hi}": _p_score([r for r in done if lo <= r["p_win"] <= hi]) for lo, hi in P_WIN_BINS},
        "nights": len(ics), "ic": round(mean(a for a, _ in ics), 3) if ics else None,
        "rank_ic_same_rows": round(mean(b for _, b in ics), 3) if ics else None,
    }


def conviction_gate(rows: list[dict] | None = None) -> dict:
    """Pre-registered sizing review: once GATE_FOURS scale-2 takes at conviction 4+ settle, compare them with
    scale-2 3s (one difference per session that has both). If 4+ is not ahead, flatten `rules.CONVICTION_RISK`."""
    rows = outcomes() if rows is None else rows
    takes = [r for r in rows if r.get("scale") == 2 and r["verdict"] == "take" and _settled(r)]
    high, base = [r for r in takes if r["conviction"] >= 4], [r for r in takes if r["conviction"] == 3]
    diffs = []
    for s in sorted({r["session"] for r in high} & {r["session"] for r in base}):
        diffs.append(float(mean(r["plan_bps"] for r in high if r["session"] == s) - mean(r["plan_bps"] for r in base if r["session"] == s)))
    due = len(high) >= GATE_FOURS
    return {"fours": len(high), "threes": len(base), "needed": GATE_FOURS, "due": due,
            "high": _stat([r["plan_bps"] for r in high]), "three": _stat([r["plan_bps"] for r in base]),
            "sessions": len(diffs), "gap_bps": round(mean(diffs), 1) if diffs else None,
            "verdict": None if not due else "keep" if diffs and mean(diffs) > 0 else "flatten"}


def judge_monitor(rows: list[dict] | None = None) -> dict:
    """Collapse and drift monitor for scale-2 verdicts (reads verdicts only, never outcomes)."""
    rows = outcomes() if rows is None else rows
    v2 = sorted((r for r in rows if _v2(r)), key=lambda r: r.get("recorded_at") or "")
    takes = [r for r in v2 if r["verdict"] == "take"][-MONITOR_TAKES:]
    vals = [r["p_win"] for r in takes]
    flags = []
    if len(takes) == MONITOR_TAKES:
        high = sum(r["conviction"] >= 4 for r in takes) / len(takes)
        top = max(vals.count(v) for v in set(vals)) / len(vals)
        if high == 0:
            flags.append(f"no 4 or 5 in the last {MONITOR_TAKES} takes: conviction is back to one level")
        if high > 0.5:
            flags.append(f"{high:.0%} of the last {MONITOR_TAKES} takes are 4 or 5: 4 is the new 3")
        if len(set(vals)) < 5 or top > 0.4:
            flags.append(f"p_win on takes has collapsed: {len(set(vals))} distinct values, {top:.0%} on one value")
    passes: dict[tuple, list[dict]] = {}
    for r in v2:
        passes.setdefault((r["session"], r.get("recorded_at")), []).append(r)
    echo = [c for rs in list(passes.values())[-20:] if len(rs) >= MIN_RANKED and all(r.get("rank") is not None for r in rs)
            and (c := _spearman([r["p_win"] for r in rs], [-r["rank"] for r in rs])) is not None]
    if len(echo) >= 10 and median(echo) >= 0.95:
        flags.append(f"p_win repeats rank (median correlation {median(echo):.2f} over {len(echo)} passes)")
    sessions = sorted({r["session"] for r in v2})
    drift = None
    if len(sessions) >= 20:
        first, last = set(sessions[:10]), set(sessions[-10:])
        a, b = [r["p_win"] for r in v2 if r["session"] in first], [r["p_win"] for r in v2 if r["session"] in last]
        drift = round(mean(b) - mean(a), 1)
        if abs(drift) > 5:
            flags.append(f"average p_win moved {drift:+.1f} points between the first and latest 10 sessions")
    judges = sorted({str(r["judge"]) for r in v2 if r.get("judge")})
    if len(judges) > 1:
        flags.append(f"more than one judge model on scale 2: {', '.join(judges)}; score them apart")
    return {"takes": len(takes), "by_conviction": {c: sum(r["conviction"] == c for r in takes) for c in (3, 4, 5)},
            "p_win_takes": {"mean": round(mean(vals), 1), "low": min(vals), "high": max(vals), "distinct": len(set(vals))} if takes else {},
            "p_win_vs_rank": round(median(echo), 2) if echo else None, "drift": drift, "judges": judges, "flags": flags}


def _mode_table(rows: list[dict], prefix: str) -> dict:
    settled = [r for r in rows if r.get(f"{prefix}_bps") is not None and r.get(f"{prefix}_mature", True)]
    live = [(r, v) for r in rows if (v := (r.get(f"{prefix}_bps") if r.get(f"{prefix}_bps") is not None else r.get(f"{prefix}_so_far_bps"))) is not None]
    return {
        "settled": {k: _stat([r[f"{prefix}_bps"] for r in settled if r["verdict"] == k]) for k in sorted({r["verdict"] for r in settled})},
        "so_far": {k: _stat([v for r, v in live if r["verdict"] == k]) for k in sorted({r["verdict"] for r, _ in live})},
    }


def tables(rows: list[dict] | None = None) -> dict:
    """Averages of the settled proposals' plan results (past their scheduled time limit) by verdict, conviction,
    door, route, setup, and hedged vs outright, plus `so_far` across all priced proposals (open and finished,
    marked to latest prices), the paper results of the trades taken, the trades the review managed against
    their original plans, and post-mortem tags."""
    rows = outcomes() if rows is None else rows
    done = [r for r in rows if _settled(r)]
    early_stopped = sum(r["plan_bps"] is not None and not _settled(r) for r in rows)
    live = [(r, v) for r in rows if (v := _current_bps(r)) is not None]
    by = lambda key: {k: _stat([r["plan_bps"] for r in done if key(r) == k]) for k in sorted({key(r) for r in done}, key=str)}  # noqa: E731
    by_live = lambda key: {k: _stat([v for r, v in live if key(r) == k]) for k in sorted({key(r) for r, _ in live}, key=str)}  # noqa: E731
    traded = [r for r in rows if r["paper_bps"] is not None]
    managed = [r for r in traded if r["managed"] and r["plan_bps"] is not None]
    return {
        "proposals": len(rows), "finished": len(done), "early_stopped": early_stopped,
        "by_verdict": by(lambda r: r["verdict"]),
        "so_far": {"n": len(live),
                   "by_verdict": {k: _stat([v for r, v in live if r["verdict"] == k]) for k in sorted({r["verdict"] for r, _ in live})}},
        "by_conviction": by(lambda r: r["conviction"] if r["conviction"] is not None else "none"),
        "by_conviction_by_scale": {
            str(sc): {k: _stat([r["plan_bps"] for r in done if r.get("scale", 1) == sc and (r["conviction"] if r["conviction"] is not None else "none") == k])
                      for k in sorted({r["conviction"] if r["conviction"] is not None else "none" for r in done if r.get("scale", 1) == sc}, key=str)}
            for sc in sorted({r.get("scale", 1) for r in done})
        },
        "p_win": p_win_table(rows),
        "conviction_gate": conviction_gate(rows),
        "judge_monitor": judge_monitor(rows),
        "rank": rank_table(rows),
        "main_test": main_test(rows),
        "by_door": {f"{door} · {verdict}": s for (door, verdict), s in by(lambda r: (r["door"], "take" if r["verdict"] == "take" else "not taken")).items()},
        "by_route": {f"{route} · {verdict}": s for (route, verdict), s in by(lambda r: (r["route"], "take" if r["verdict"] == "take" else "not taken")).items()},
        "so_far_by_route": {f"{route} · {verdict}": s for (route, verdict), s in by_live(lambda r: (r["route"], "take" if r["verdict"] == "take" else "not taken")).items()},
        "by_mode": {"hedged": _mode_table(rows, "hedged"), "outright": _mode_table(rows, "outright")},
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


def _desk_sections() -> list[str]:
    """Operational book state only: open trades, waiting entries, and closed trades needing a post-mortem."""
    from . import entries

    lines: list[str] = []
    if waiting := needs_postmortem():
        lines += ["", "Closed trades with no post-mortem yet"] + [
            f"  {r['id']} {r['name']} ({r['setup']}): {r['bps']:+.0f} bp, {r['reason']}" for r in waiting
        ]
    open_auto = [r for r in book.rows(book.MAIN) if r["state"] in ("open", "planned")]
    lines += ["", "Open trades"]
    if open_auto:
        for r in open_auto:
            lines.append(f"  {r['id']} {r['name']} ({r['setup']}, {r['legs']}): session {r['session_number']} of {r['horizon_days']}, "
                         + (f"{r['bps']:+.0f} bp" if r["bps"] is not None else "no fills yet")
                         + (f", {r['stop_room_pct']:+.1f}% from its stop" if r["stop_room_pct"] is not None else "")
                         + (f". Wrong if: {r['wrong_if']}" if r.get("wrong_if") else ""))
    else:
        lines.append("  none")
    if still := entries.waiting(entries.entry_session(datetime.now(NY)), book.MAIN):
        lines += ["", "Takes still waiting to enter (review each: hold keeps trying, exit stands it down)"]
        for w in still:
            got = ", ".join(f"{s} {n}/{next(leg['shares'] for leg in w['legs'] if leg['symbol'] == s)}" for s, n in w["filled"].items())
            lines.append(f"  {w['id']} {w['name']} ({w['setup']}, from the {w.get('pass')} pass): filled {got}. Wrong if: {w.get('wrong_if')}")
    return lines


def desk() -> str:
    """What a research pass reads at its start (Folder 2 only): open trades, waiting entries, and closed trades
    needing a post-mortem. No scorecards, no setup/route win rates, no past post-mortems, and no lessons.md,
    so the judge stays uninfluenced while the verdict repository builds."""
    lines = [f"Swing book desk, {datetime.now(CT):%Y-%m-%d %-I:%M %p} CT: open trades, waiting entries, and closed trades needing a post-mortem."]
    lines += _desk_sections()
    return "\n".join(lines)


def _main_test_lines(mt: dict) -> list[str]:
    """Plain-text rendering of main_test() for the owner report."""
    lines = ["", f"Main test (takes minus passes, settled, after {mt['cost_bps']:g} bp est. round-trip cost)"]
    if mt["raw_gap_bps"] is None and mt["default_gap_bps"] is None:
        lines.append("  not enough settled takes and passes yet")
    else:
        if mt["raw_gap_bps"] is not None:
            lines.append(f"  {'Raw gap (custom vs default)':<34} {mt['raw_gap_bps']:+7.1f} bp   (n={mt['n_takes']} takes, {mt['n_passes']} passes)")
        sc = mt["session_clustered"]
        if mt["default_gap_bps"] is not None:
            t_txt = f"  clustered t={sc['t']:+.2f}  p={sc['p_value']:.3f}" if sc["t"] is not None else ""
            hac_txt = f"  HAC p={sc['p_value_hac']:.3f}" if sc.get("p_value_hac") is not None else ""
            lines.append(f"  {'Same default plan (pooled)':<34} {mt['default_gap_bps']:+7.1f} bp   (session mean {sc['mean_bps']:+.1f} bp over {sc['sessions']} sessions{t_txt}{hac_txt})")
        st = mt["stratified"]
        if st["mean_bps"] is not None:
            ssc = st["session_clustered"]
            st_txt = f"  clustered t={ssc['t']:+.2f}  p={ssc['p_value']:.3f}" if ssc["t"] is not None else ""
            hac_txt = f"  HAC p={ssc['p_value_hac']:.3f}" if ssc.get("p_value_hac") is not None else ""
            lines.append(f"  {'Stratified (setup, direction)':<34} {st['mean_bps']:+7.1f} bp   ({st['strata']} matched strata: {st['takes']} takes, {st['passes']} passes; {ssc['sessions']} sessions{st_txt}{hac_txt})")
        if mt["plan_custom_bps"] is not None:
            lines.append(f"  {'Judge plan customization':<34} {mt['plan_custom_bps']:+7.1f} bp   (custom stop/horizon/hedge minus default on takes)")
        vs = mt.get("vs_spy") or {}
        if vs.get("plan_mean_bps") is not None:
            vsc = vs["session_clustered"]
            vt_txt = f"  clustered t={vsc['t']:+.2f}  p={vsc['p_value']:.3f}" if vsc.get("t") is not None else ""
            paper_txt = f"; closed paper {vs['paper_mean_bps']:+.1f} bp (n={vs['paper_n']})" if vs.get("paper_mean_bps") is not None else ""
            lines.append(f"  {'Second test: takes vs SPY':<34} {vs['plan_mean_bps']:+7.1f} bp   (n={vs['n_takes']} takes{vt_txt}{paper_txt})")
        rg = mt.get("regimes") or {}
        reg_parts = []
        for key, lbl in (("pre_uninfluenced", f"pre-{UNINFLUENCED_FROM}"), ("uninfluenced", f"uninfluenced >={UNINFLUENCED_FROM}")):
            rv = rg.get(key) or {}
            if rv.get("default_gap_bps") is not None:
                reg_parts.append(f"{lbl}: {rv['default_gap_bps']:+.1f} bp (n={rv['n_takes']}t/{rv['n_passes']}p)")
        if reg_parts:
            lines.append(f"  {'Regime split (default plan)':<34} " + "   |   ".join(reg_parts))
        o = mt["outliers"]
        parts = [f"{lbl}: {val:+.1f} bp" for lbl, val in (
            ("ex-top-2", o["ex_top_2_bps"]), ("trimmed 5%", o["trimmed_5pct_bps"]), ("median", o["median_gap_bps"])
        ) if val is not None]
        if parts:
            lines.append(f"  {'Outlier sensitivity':<34} " + "   ".join(parts))
        pw = mt["power"]
        if pw["takes_for_200bp"] is not None:
            sd_lbl = f"Power (80% @ p<0.05, sd {pw['take_sd_bps']:.0f} bp)"
            lines.append(f"  {sd_lbl:<34} {pw['n_takes']} / ~{pw['takes_for_200bp']} takes for +200 bp  ({pw['n_takes']} / ~{pw['takes_for_100bp']} for +100 bp)")
        lines.append(f"  {'Verdict':<34} {pw['verdict']}")
    ex = mt["execution"]
    if ex["filled_takes"] or ex["unfilled_takes"]:
        cp = ex["closed_paper"]
        cp_txt = f"closed {cp['n']}: mean {cp['mean']:+.1f} bp, median {cp['median']:+.1f}, winners {cp['winners']:.0%}" if cp.get("n") else "none closed yet"
        lines.append(f"  {'Realized book (filled takes)':<34} {ex['filled_takes']} filled   ({cp_txt})")
        if ex["unfilled_takes"]:
            reasons = ", ".join(f"{k.replace('_', ' ')} {n}" for k, n in sorted(ex["unfilled_reasons"].items(), key=lambda x: -x[1]))
            us = ex["unfilled_settled"]
            u_txt = f"settled {us['n']} mean {us['mean']:+.1f} bp" if us.get("n") else f"so far {ex['unfilled_so_far']['n']} mean {ex['unfilled_so_far']['mean']:+.1f} bp" if ex["unfilled_so_far"].get("n") else "unpriced"
            lines.append(f"  {'Unfilled takes (execution leak)':<34} {ex['unfilled_takes']} unfilled   ({reasons}; {u_txt})")
    return lines


def brief() -> str:
    """Owner-only report card: the record so far (by verdict, conviction, rank, door/route, hedged vs outright,
    and setup), post-mortems, open trades, and standing lessons. Research passes run `fr paper desk` instead."""
    rows = outcomes()
    t = tables(rows)
    from .. import portfolio
    three = bool(portfolio.restart())
    waiting_note = f" ({t['early_stopped']} stopped early, waiting for their time limit)" if t["early_stopped"] else ""
    lines = [f"{'Swing' if three else 'Automatic'} book record (owner report), {datetime.now(CT):%Y-%m-%d %-I:%M %p} CT: {t['proposals']} proposals, {t['finished']} settled{waiting_note}.",
             ("Plan result = the proposal's own plan from the price when its pass was acted on to its stop or time limit, before costs. "
              if three else "Plan result = the proposal's own plan on daily bars, first open to its stop or time limit, before costs. ")
             + "Settled tables wait until an idea's time limit has passed, so early stop-outs don't count ahead of surviving peers. The passes are the control: the takes have to beat them."]
    lines += _main_test_lines(t["main_test"])
    lines += ["", "By verdict (settled)"]
    lines += [_line(k, s) for k, s in t["by_verdict"].items()] or ["  nothing settled yet"]
    if t["so_far"]["n"]:
        lines += ["", f"All ideas so far (open + finished, marked to latest price; n={t['so_far']['n']})"]
        lines += [_line(k, s) for k, s in t["so_far"]["by_verdict"].items()]
    lines += ["", "Hedged vs outright (apples-to-apples, same stop & time limit)"]
    for mode in ("hedged", "outright"):
        for scope in ("settled", "so_far"):
            bucket = t["by_mode"][mode][scope]
            if bucket:
                tag = "settled" if scope == "settled" else "all so far"
                for k, s in bucket.items():
                    lines.append(_line(f"{mode} ({tag}) · {k}", s))
    lines += ["", "By door (settled)"] + ([_line(k, s) for k, s in t["by_door"].items()] or ["  nothing settled yet"])
    lines += ["", "By route (settled)"] + ([_line(k, s) for k, s in t["by_route"].items()] or ["  nothing settled yet"])
    if t["so_far_by_route"]:
        lines += ["", "By route (all so far)"] + [_line(k, s) for k, s in t["so_far_by_route"].items()]
    by_sc = t.get("by_conviction_by_scale") or {}
    if len(by_sc) > 1 or "2" in by_sc:
        lines += ["", "By conviction (settled, separated by scale)"]
        for sc, bucket in by_sc.items():
            for k, s in bucket.items():
                lbl = f"scale 1 · conviction {k} (undivided)" if sc == "1" and k == 3 else f"scale {sc} · conviction {k}"
                lines.append(_line(lbl, s))
    else:
        lines += ["", "By conviction (settled)"] + ([_line(f"conviction {k}", s) for k, s in t["by_conviction"].items()] or ["  nothing settled yet"])
    cg = t.get("conviction_gate") or {}
    if cg.get("fours") or cg.get("threes"):
        status = cg["verdict"].upper() if cg.get("verdict") else f"{cg['fours']}/{cg['needed']} settled 4+ takes"
        gap_txt = f"  session gap {cg['gap_bps']:+.1f} bp ({cg['sessions']} sessions)" if cg.get("gap_bps") is not None else ""
        lines.append(f"  {'Sizing gate (scale 2: 4+ vs 3)':<34} {status}{gap_txt}")
    pw = t.get("p_win") or {}
    if (pw.get("all") or {}).get("n"):
        lines += ["", "p_win calibration (scale 2, settled)"]
        for lbl, key in (("all verdicts", "all"), ("takes (own plan)", "takes"), ("takes (default plan)", "takes_default"), ("passes", "passes")):
            s = pw.get(key) or {}
            if s.get("n"):
                sk = f"  skill {s['skill']:+.3f}" if s.get("skill") is not None else ""
                auc = f"  AUC {s['auc']:.3f}" if s.get("auc") is not None else ""
                lines.append(f"  {lbl:<34} n={s['n']:<4} said {s['said']:.0%}  won {s['won']:.0%}  brier {s['brier']:.4f}{sk}{auc}")
        for b_lbl, s in (pw.get("bins") or {}).items():
            if s.get("n"):
                lines.append(f"  {'bin ' + b_lbl + '%':<34} n={s['n']:<4} said {s['said']:.0%}  won {s['won']:.0%}")
        if pw.get("ic") is not None:
            lines.append(f"  {'within-pass Spearman (p_win/rank)':<34} p_win {pw['ic']:+.3f}  vs  rank {pw['rank_ic_same_rows']:+.3f} ({pw['nights']} nights)")
    jm = t.get("judge_monitor") or {}
    if jm.get("flags"):
        lines += ["", "Judge monitor flags (scale 2)"] + [f"  ! {f}" for f in jm["flags"]]
    rk = t["rank"]
    lines += ["", "By rank (1 = the best idea of its pass; settled passes only)"]
    if rk["nights"]:
        lines.append(f"  rank correlation {rk['ic']:+.2f} on average over {rk['nights']} night{'s' if rk['nights'] != 1 else ''}"
                     + (f", t {rk['t']:+.1f}" if rk["t"] is not None else "")
                     + (f"   (an anecdote before about {RANK_NIGHTS} nights)" if rk["nights"] < RANK_NIGHTS else ""))
        lines += [_line(f"{name} third of each night", s) for name, s in rk["thirds"].items()]
    else:
        lines.append(f"  no ranked pass has fully settled yet ({rk['ranked']} verdicts ranked so far)")
    lines += ["", "By setup (settled)"] + ([_line(k, s) for k, s in t["by_setup"].items()] or ["  nothing settled yet"])
    lines += ["", "Paper trades closed (after commissions)", _line("all", t["paper"])]
    for kind, s in t["managed"].items():
        lines += [_line(f"{kind}: paper result", s["paper"]), _line(f"{kind}: what the plan would have made", s["plan"])]
    if t["by_tag"]:
        lines += ["", "Post-mortem tags: " + ", ".join(f"{tag} {n}" for tag, n in t["by_tag"].items())]
    recent = sorted(postmortems().values(), key=lambda m: m["at"])[-10:]
    if recent:
        lines += ["", "Latest post-mortems"] + [f"  {m['trade_id']} {m['name']} ({m['setup']}, {m['bps']:+.0f} bp, {m['tag']}): {m['lesson']}" for m in recent]
    lines += _desk_sections()
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

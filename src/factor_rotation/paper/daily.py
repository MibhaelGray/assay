"""Recording one of the three daily passes: `fr paper propose <draft>` with "pass": "night", "morning" or "midday".

Since the 2026-11-15 redesign every pass is the same thing, three times a day:
  night    about 7:30 PM CT, Sunday to Thursday, on after-hours prices, for the next session
  morning  about 8:45 AM CT, on the pre-market and the first minutes of the session
  midday   about 12:00 PM CT, on the morning's trading
Each judges the live screen built for it (`fr screen --live <pass>`), gives every candidate a verdict, a
conviction and a rank, and reviews every trade the book has open or is still trying to enter. The order job
(entries.py) trades the takes and the exits as soon as the pass has recorded, usually within a minute.

    journal/paper/proposals/<session>.json      the verdicts, one list per session, each tagged with its pass
    journal/paper/pass_reviews/<session>.json   hold, exit or extend for each trade, per pass

A pass records once. Verdicts are never rewritten.
"""

import json
from datetime import date, datetime
from pathlib import Path

from .. import screen as screens
from ..config import NY
from . import book, entries, proposals, rules

ACTIONS = ("hold", "exit", "extend")
THESIS = ("intact", "weakened", "broken", "played_out")


def live_screen(session: date, kind: str) -> dict | None:
    path = screens.live_path(session, kind)
    return json.loads(path.read_text()) if path.exists() else None


def screen_now(now: datetime | None = None) -> dict | None:
    """The live screen of the pass that could record right now: the latest built, if more than one could."""
    now = now or datetime.now(NY)
    found = []
    for kind in proposals.NEW_PASSES:
        try:
            s = live_screen(proposals.pass_session(kind, now), kind)
        except ValueError:
            continue
        if s:
            found.append(s)
    return max(found, key=lambda s: s["built_at"]) if found else None


def to_review(session: date) -> dict[str, dict]:
    """Every trade a pass for `session` must decide on, by id: the open ones, and the session's takes still
    trying to enter (`waiting` True, with `filled` shares by symbol)."""
    out = {t["id"]: {**t, "waiting": False} for t in book.open_trades()}
    for t in entries.waiting(session):
        out[t["id"]] = {**out.get(t["id"], t), "waiting": True, "filled": t["filled"]}
    return out


def _reviews(raws: list, session: date) -> list[dict]:
    """One decision for every trade in `to_review`: hold, exit or extend. For a trade still waiting to enter,
    hold keeps trying and exit stands it down. Stops and time limits stay with the close job; a pass can only
    end a trade sooner, or ask for a winner on its last session to run a little longer."""
    trades = to_review(session)
    out = []
    for r in raws:
        who = str(r.get("trade_id"))
        t = trades.get(who)
        if not t:
            raise ValueError(f"{who} is not an open trade, or one waiting to enter")
        if who in {x["trade_id"] for x in out}:
            raise ValueError(f"{who} is reviewed twice")
        if r.get("action") not in ACTIONS or r.get("thesis") not in THESIS or not proposals._text(r.get("reason")):
            raise ValueError(f"{who}: needs action ({'/'.join(ACTIONS)}), thesis ({'/'.join(THESIS)}) and a reason")
        rec = {"trade_id": who, "action": r["action"], "thesis": r["thesis"], "reason": r["reason"].strip()}
        if r["action"] == "extend":
            n = r.get("sessions")
            planned = t.get("planned_horizon_days", t["horizon_days"])
            if t["waiting"] and not any(t["filled"].values()):
                raise ValueError(f"{who} hasn't entered yet, so there is nothing to extend: hold, or exit to stand it down")
            if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= rules.MAX_EXTENSION:
                raise ValueError(f"{who}: an extension is 1-{rules.MAX_EXTENSION} sessions")
            if t.get("extended"):
                raise ValueError(f"{who} has already been extended once")
            if book._session(t, session) < t["horizon_days"]:
                raise ValueError(f"{who}: only a trade on its last session can be extended; until then it is a hold")
            if planned + n > rules.MAX_HORIZON:
                raise ValueError(f"{who}: {planned} + {n} sessions is past the {rules.MAX_HORIZON}-session limit")
            rec["sessions"] = n
        out.append(rec)
    if missing := sorted(set(trades) - {r["trade_id"] for r in out}):
        raise ValueError(f"review every open or waiting trade (hold, exit or extend); missing: {', '.join(missing)}")
    return out


def record(draft: dict, now: datetime | None = None) -> tuple[Path, dict, list[str]]:
    """Record a pass: its verdicts on the live screen's candidates and its own ideas, and its reviews.
    Returns the proposals path, the session's file and notes for the pass.

    draft: {"session": "2026-11-17", "pass": "night" | "morning" | "midday", "summary": "...", "market": {...},
            "proposals": [{"candidate": "ABNB", "rank": 1, "decision": "take", "conviction": 3, "reason": "...", ...}],
            "reviews": [{"trade_id": "...", "action": "hold" | "exit" | "extend", "thesis": "...", "reason": "..."}]}
    """
    now = now or datetime.now(NY)
    kind = draft.get("pass")
    if kind not in proposals.NEW_PASSES:
        raise ValueError(f"pass must be one of: {', '.join(proposals.NEW_PASSES)}")
    session = proposals.pass_session(kind, now)
    if draft.get("session") != session.isoformat():
        raise ValueError(f"a {kind} pass recorded now is for session {session}, not {draft.get('session')}")
    doc = proposals.load(session) or {"session": session.isoformat(), "screen": None, "passes": [], "proposals": []}
    if any(p["pass"] == kind for p in doc["passes"]):
        raise ValueError(f"the {kind} pass for {session} is already recorded; a pass records once")
    screen = live_screen(session, kind)
    if not screen:
        raise FileNotFoundError(f"no live screen for the {kind} pass of {session}; run `uv run fr screen --live {kind}` first")
    cands = {c["name"]: (i, c) for i, c in enumerate(screen["candidates"], 1)}
    raws = draft.get("proposals", [])
    stamp = {"session": session.isoformat(), "pass": kind, "recorded_at": now.isoformat()}
    new, notes = [], []
    for raw, rank in zip(raws, proposals._ranks(raws)):   # every verdict ranked
        p = {**proposals._proposal(raw, session, screen, cands), **stamp, "rank": rank, "ranked": len(raws)}
        if p["name"] in {x["name"] for x in new}:
            raise ValueError(f"{p['name']} has two verdicts in this draft")
        new.append(p)
    order = sorted(new, key=lambda p: p["rank"])
    for a, b in zip(order, order[1:]):  # the rank is the conviction, drawn finer: the two can't disagree
        if b["conviction"] > a["conviction"]:
            raise ValueError(f"{b['name']} is ranked {b['rank']} with conviction {b['conviction']}, below {a['name']} at rank "
                             f"{a['rank']} with conviction {a['conviction']}: an idea with more conviction ranks higher")
    takes = [p for p in new if p["decision"] == "take"]
    if entries.PASS_MAX_NEW and len(takes) > entries.PASS_MAX_NEW:
        notes.append(f"{len(takes)} takes: the order job enters the {entries.PASS_MAX_NEW} with the most conviction, then by rank")
    judged = {p["name"] for p in new}
    # The owner, 2026-11-16: if 40 names pass the screen, 40 are judged and ranked. A draft that skips one is refused.
    if skipped := [f"{name} (screen #{rank})" for name, (rank, _) in cands.items() if name not in judged]:
        raise ValueError(f"every candidate on the screen needs a verdict and a rank; {len(skipped)} of {len(cands)} have none: "
                         f"{', '.join(skipped)}")
    reviews = _reviews(draft.get("reviews", []), session)
    for k, p in enumerate(new, len(doc["proposals"]) + 1):
        p["id"] = f"{session}-P{k:02d}"
    doc["proposals"] += new
    doc["screen"] = screen["as_of"]
    doc["passes"].append({"pass": kind, "at": now.isoformat(), "summary": str(draft.get("summary", "")), "market": draft.get("market", {}),
                          "proposals": len(new), "takes": len(takes), "reviews": len(reviews),
                          "prices_at": screen.get("prices_at"), "phase": screen.get("phase")})
    if reviews:   # written first: the order job acts once the proposals file names the pass
        path = proposals.root() / "pass_reviews" / f"{session}.json"
        old = json.loads(path.read_text()) if path.exists() else {"session": session.isoformat(), "passes": []}
        old["passes"].append({"pass": kind, "at": now.isoformat(), "reviews": reviews})
        book._write(path, old)
    path = book._write(proposals.root() / "proposals" / f"{session}.json", doc)
    return path, doc, notes

"""Scores the small-cap pass's verdicts: `fr smallcap score`. No pass reads this.

Every verdict, taken or passed, is scored in its own direction from its entry, held one to five sessions to the
close, against IWM (the small-cap ETF) over the same days. A night verdict enters at its session's open; a morning
or midday one at the live price it was recorded at (record.py), where on its first day only the close can trigger
its stop, since the day's low or high may come from before it was recorded: a long's result is the stock's return
less IWM's, a short's the other way round. Results are in basis points (1 bp = 0.01%), before costs; in this
universe getting in and out costs roughly 100 bp, so a hold has to clear that. A take is also scored on its own
plan: its time limit and its stop on the stock.

What it answers, once enough nights are in: do takes beat passes, do better-ranked names go on to do better, and
does thin analyst coverage matter. About 15 nights before it means anything.
"""

import json

import pandas as pd

from .. import bars
from . import HEDGE, screen

HOLDS = (1, 2, 3, 4, 5)
COST_BP = 100
THIN = 2   # analysts: this many or fewer is "thinly covered"


def _path(px: pd.DataFrame, symbol: str, session: str) -> pd.DataFrame:
    g = px[px["symbol"] == symbol].sort_values("date")
    return g[g["date"] >= pd.Timestamp(session)].head(max(HOLDS)).reset_index(drop=True)


def results(doc: dict, px: pd.DataFrame) -> list[dict]:
    """One row per verdict with its result at each hold that has happened."""
    session = doc["session"]
    hedge = _path(px, HEDGE, session)
    if hedge.empty or hedge["date"].iloc[0] != pd.Timestamp(session):
        return []
    out = []
    for v in doc["verdicts"]:
        own = _path(px, v["name"], session)
        if own.empty or own["date"].iloc[0] != pd.Timestamp(session):
            continue
        sign = 1 if v["direction"] == "long" else -1
        live = bool((v.get("entry") or {}).get("price") and (v.get("entry") or {}).get("hedge_price"))
        entry, hedge_entry = ((float(v["entry"]["price"]), float(v["entry"]["hedge_price"])) if live
                              else (float(own["open"].iloc[0]), float(hedge["open"].iloc[0])))
        n = min(len(own), len(hedge))
        held = {h: round(sign * ((own["close"].iloc[h - 1] / entry - 1) - (hedge["close"].iloc[h - 1] / hedge_entry - 1)) * 1e4)
                for h in HOLDS if h <= n}
        row = {"session": session, "key": screen.key(session, doc.get("pass") or "night"), "name": v["name"],
               "entry": entry, "hedge_entry": hedge_entry, "live_entry": live, "verdict": v["verdict"], "direction": v["direction"], "rank": v["rank"],
               "of": len(doc["verdicts"]), "conviction": v["conviction"], "setup": v["setup"], "news": v.get("news"),
               "screen_rank": v.get("screen_rank"), "screen": doc.get("screen_version", "2026-11-19 price-first"),
               "analysts": (v.get("numbers") or {}).get("analysts"), "held": held, "plan": None}
        if v["verdict"] == "take":
            limit, stop = v["plan"]["horizon_days"], v["plan"]["stop_pct"] / 100
            for i in range(min(limit, n)):
                level = entry * (1 - sign * stop)
                close = float(own["close"].iloc[i])
                if live and i == 0:
                    hit = close <= level if sign > 0 else close >= level
                else:
                    hit = own["low"].iloc[i] <= level if sign > 0 else own["high"].iloc[i] >= level
                last = i == limit - 1
                if hit or last:
                    opened = float(own["open"].iloc[i])
                    gapped = hit and i > 0 and (opened <= level if sign > 0 else opened >= level)
                    price = (close if live and i == 0 else opened if gapped else level) if hit else close
                    row["plan"] = {"result": round(sign * ((price / entry - 1) - (hedge["close"].iloc[i] / hedge_entry - 1)) * 1e4),
                                   "sessions": i + 1, "stopped": bool(hit), "exit_price": price}
                    break
        out.append(row)
    return out


def _rank_corr(rows: list[dict], hold: int) -> float | None:
    """Spearman correlation between the pass's ranking (best first) and the results at one hold."""
    pairs = [(-r["rank"], r["held"][hold]) for r in rows if hold in r["held"]]
    if len(pairs) < 5:
        return None
    s = pd.DataFrame(pairs, columns=["rank", "result"]).rank()
    c = s["rank"].corr(s["result"])
    return None if pd.isna(c) else float(c)


def measure(docs: list[dict] | None = None, px: pd.DataFrame | None = None) -> dict:
    if docs is None:
        docs = [json.loads(p.read_text()) for p in sorted(screen.VERDICTS.glob("*.json"))] if screen.VERDICTS.exists() else []
    if px is None:
        names = sorted({v["name"] for d in docs for v in d["verdicts"]} | {HEDGE})
        px = bars.load(names)
    rows = [r for d in docs for r in results(d, px)] if not px.empty else []

    def avg(pick, hold):
        got = [r["held"][hold] for r in rows if pick(r) and hold in r["held"]]
        return (round(sum(got) / len(got)), len(got)) if got else (None, 0)

    third = lambda r: 0 if r["rank"] <= r["of"] / 3 else 2 if r["rank"] > r["of"] * 2 / 3 else 1   # noqa: E731
    groups = {"all verdicts": lambda r: True, "takes": lambda r: r["verdict"] == "take", "passes": lambda r: r["verdict"] == "pass",
              "top third by rank": lambda r: third(r) == 0, "middle third": lambda r: third(r) == 1,
              "bottom third": lambda r: third(r) == 2,
              f"{THIN} analysts or fewer": lambda r: r["analysts"] is not None and r["analysts"] <= THIN,
              f"more than {THIN} analysts": lambda r: r["analysts"] is not None and r["analysts"] > THIN,
              **{s: (lambda r, s=s: r["setup"] == s) for s in sorted({r["setup"] for r in rows})},
              **{f"news: {x}": (lambda r, x=x: r.get("news") == x) for x in ("new", "stale", "none")},
              **{f"screen: {s}": (lambda r, s=s: r.get("screen") == s) for s in sorted({r.get("screen") for r in rows if r.get("screen")})},
              "longs": lambda r: r["direction"] == "long", "shorts": lambda r: r["direction"] == "short"}
    table = {label: {h: avg(pick, h) for h in HOLDS} for label, pick in groups.items()}
    nights = sorted({r["session"] for r in rows})
    edge = {}
    for h in HOLDS:
        corrs, gaps = [], []
        for night in nights:
            mine = [r for r in rows if r["session"] == night]
            if (c := _rank_corr(mine, h)) is not None:
                corrs.append(c)
            takes = [r["held"][h] for r in mine if r["verdict"] == "take" and h in r["held"]]
            rest = [r["held"][h] for r in mine if r["verdict"] == "pass" and h in r["held"]]
            if takes and rest:
                gaps.append(sum(takes) / len(takes) - sum(rest) / len(rest))
        edge[h] = {"rank_corr": round(sum(corrs) / len(corrs), 2) if corrs else None, "corr_nights": len(corrs),
                   "takes_less_passes": round(sum(gaps) / len(gaps)) if gaps else None, "gap_nights": len(gaps)}
    plans = [r["plan"] for r in rows if r["plan"]]
    return {"nights_recorded": len(docs), "nights_scored": len(nights), "verdicts": len(rows), "table": table, "edge": edge,
            "plans": {"n": len(plans), "average": round(sum(p["result"] for p in plans) / len(plans)) if plans else None,
                      "stopped": sum(1 for p in plans if p["stopped"])}, "rows": rows}


def report(m: dict) -> str:
    if not m["verdicts"]:
        return (f"Small-cap verdicts: {m['nights_recorded']} night{'s' if m['nights_recorded'] != 1 else ''} recorded, none scored yet. "
                "A verdict is scored once its session has traded.")
    cell = lambda v: f"{v[0]:+d}".rjust(7) if v[0] is not None else "      ."   # noqa: E731
    lines = [f"Small-cap verdicts: {m['verdicts']} scored from {m['nights_scored']} nights ({m['nights_recorded']} recorded). A shadow run: "
             "nothing was traded.",
             f"Results in bp against IWM (the small-cap ETF), in each verdict's own direction, from the session's open. Before costs "
             f"(about {COST_BP} bp to get in and out). An anecdote before about 15 nights; a dot means nothing yet.", "",
             "  sessions held".ljust(28) + "".join(str(h).rjust(7) for h in HOLDS) + "    verdicts at 1"]
    for label, by_hold in m["table"].items():
        if by_hold[1][1]:
            lines.append(f"  {label}".ljust(28) + "".join(cell(by_hold[h]) for h in HOLDS) + f"    {by_hold[1][1]}")
    e = m["edge"]
    num = lambda x, f: (f % x).rjust(7) if x is not None else "      ."   # noqa: E731
    lines += ["", "  The research's edge, worked night by night and averaged",
              "  rank correlation".ljust(28) + "".join(num(e[h]["rank_corr"], "%+.2f") for h in HOLDS),
              "    nights counted".ljust(28) + "".join(str(e[h]["corr_nights"]).rjust(7) for h in HOLDS),
              "  takes less passes, bp".ljust(28) + "".join(num(e[h]["takes_less_passes"], "%+d") for h in HOLDS),
              "    nights counted".ljust(28) + "".join(str(e[h]["gap_nights"]).rjust(7) for h in HOLDS)]
    p = m["plans"]
    if p["n"]:
        lines += ["", f"  Takes on their own plan (time limit and stop): {p['average']:+d} bp on average over {p['n']} finished or stopped, "
                      f"{p['stopped']} stopped out."]
    return "\n".join(lines)


# The book it would be if it traded (docs/small-cap-book.md, set 2026-11-20): $25,000, risk per trade as a share of
# the book over the take's stop, no position over 10% of the book or a quarter of a percent of the stock's daily
# dollar volume. The dashboard's dollar column uses it; nothing is traded.
CAPITAL = 25_000
RISK = {1: 0.005, 2: 0.005, 3: 0.005, 4: 0.0075, 5: 0.01}
MAX_POSITION = 0.10
MAX_VOLUME_SHARE = 0.0025


def size(conviction: int, stop_pct: float, dollar_volume: float | None) -> float:
    """Dollars a take would hold in the $25,000 book."""
    dollars = min(CAPITAL * RISK.get(conviction, 0.005) / (stop_pct / 100), CAPITAL * MAX_POSITION)
    if dollar_volume:
        dollars = min(dollars, dollar_volume * MAX_VOLUME_SHARE)
    return round(dollars, 2)


def take_names(docs: list[dict] | None = None) -> list[str]:
    """Every name the shadow run has taken, so the dashboard keeps their prices fresh."""
    if docs is None:
        docs = [json.loads(p.read_text()) for p in sorted(screen.VERDICTS.glob("*.json"))] if screen.VERDICTS.exists() else []
    return sorted({v["name"] for d in docs for v in d["verdicts"] if v["verdict"] == "take"})


def dashboard(docs: list[dict] | None = None, px: pd.DataFrame | None = None) -> list[dict]:
    """The shadow book for the dashboard's Portfolio view: one row per take, marked at the latest daily bar, and a
    summary. A take is `waiting` until its session has a bar, `open` while inside its time limit, then `finished`
    or `stopped`. Dollars are what the take would have made against IWM at its size in the $25,000 book (`size`);
    no order exists."""
    if docs is None:
        docs = [json.loads(p.read_text()) for p in sorted(screen.VERDICTS.glob("*.json"))] if screen.VERDICTS.exists() else []
    if not docs:
        return []
    if px is None:
        px = bars.load(sorted({v["name"] for d in docs for v in d["verdicts"]} | {HEDGE}))
    scored = {(r["key"], r["name"]): r for d in docs for r in results(d, px)} if not px.empty else {}
    takes = []
    for d in docs:
        hedge = _path(px, HEDGE, d["session"]) if not px.empty else pd.DataFrame()
        for v in d["verdicts"]:
            if v["verdict"] != "take":
                continue
            row = {"section": "smallcap_take", "session": d["session"], "name": v["name"], "company": v.get("company"),
                   "direction": v["direction"], "conviction": v["conviction"], "rank": v["rank"], "setup": v["setup"],
                   "horizon_days": v["plan"]["horizon_days"], "stop_pct": v["plan"]["stop_pct"], "thesis": v["thesis"],
                   "wrong_if": v.get("wrong_if"), "analysts": (v.get("numbers") or {}).get("analysts"),
                   "state": "waiting", "entry": None, "last": None, "sessions": 0, "own_pct": None, "result_bp": None, "pnl": None,
                   "position": size(v["conviction"], v["plan"]["stop_pct"], (v.get("numbers") or {}).get("dollar_volume"))}
            r = scored.get((screen.key(d["session"], d.get("pass") or "night"), v["name"]))
            own = _path(px, v["name"], d["session"]) if r else pd.DataFrame()
            if r and not own.empty:
                sign = 1 if v["direction"] == "long" else -1
                n = min(len(own), len(hedge), v["plan"]["horizon_days"])
                if r["plan"]:
                    result, n = r["plan"]["result"], r["plan"]["sessions"]
                    state = "stopped" if r["plan"]["stopped"] else "finished"
                else:
                    result, state = r["held"][n], "open"
                entry, last = r["entry"], float(own["close"].iloc[n - 1])
                row |= {"state": state, "entry": round(entry, 2), "last": round(last, 2), "sessions": n,
                        "own_pct": round(sign * (last / entry - 1) * 100, 2), "result_bp": result,
                        "pnl": round(row["position"] * result / 1e4, 2), "through": own["date"].iloc[n - 1].date().isoformat()}
            takes.append(row)
    m = measure(docs, px)
    done = [t for t in takes if t["result_bp"] is not None]
    summary = {"section": "smallcap_summary", "nights": len(docs), "verdicts": sum(len(d["verdicts"]) for d in docs),
               "takes": len(takes), "open": sum(1 for t in takes if t["state"] == "open"),
               "waiting": sum(1 for t in takes if t["state"] == "waiting"), "scored": len(done),
               "won": sum(1 for t in done if t["result_bp"] > 0),
               "average_bp": round(sum(t["result_bp"] for t in done) / len(done)) if done else None,
               "pnl": round(sum(t["pnl"] for t in done), 2) if done else None, "capital": CAPITAL,
               "return_pct": round(sum(t["pnl"] for t in done) / CAPITAL * 100, 2) if done else None,
               "takes_less_passes_bp": m["edge"][1]["takes_less_passes"], "edge_nights": m["edge"][1]["gap_nights"],
               "latest_session": docs[-1]["session"], "hedge": HEDGE}
    return [summary, *sorted(takes, key=lambda t: (t["session"], -t["rank"]), reverse=True)]

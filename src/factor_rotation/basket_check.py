"""Are the themed baskets real baskets? `fr baskets check`.

The owner's rule, 2026-11-16: a basket is a group of stocks that move together beyond what its hedge fund explains.
Over the last SESSIONS sessions, each member's daily return has its hedge's part taken out (the member's return less
its beta to the hedge times the hedge's return). A member belongs when that leftover correlates at least MEMBER_MIN
with the average leftover of the other members; a basket stands when its members average at least BASKET_MIN.
Re-run it monthly and before adding a basket (journal/reference/baskets.json). It changes nothing; it reports.
"""

import json

import numpy as np
import pandas as pd

from . import bars
from .market import basket_library

SESSIONS = 120
MEMBER_MIN = 0.25
BASKET_MIN = 0.35
MIN_DAYS = 80


def _returns(symbols: list[str]) -> pd.DataFrame:
    df = bars.load(symbols)
    if df.empty:
        return pd.DataFrame()
    px = df.pivot_table(index="date", columns="symbol", values="close").sort_index().iloc[-(SESSIONS + 1):]
    return px.pct_change().iloc[1:]


def scores(r: pd.DataFrame, hedge: str, members: list[str]) -> dict[str, float]:
    """member -> correlation of its hedge-removed return with the other members' average."""
    h = r[hedge]
    left = {}
    for s in members:
        y = r[s]
        ok = y.notna() & h.notna()
        beta = np.cov(y[ok], h[ok])[0, 1] / h[ok].var()
        left[s] = y - beta * h
    left = pd.DataFrame(left)
    return {s: float(left[s].corr(left[[o for o in members if o != s]].mean(axis=1))) for s in members}


def check(baskets: list[dict] | None = None) -> list[dict]:
    baskets = baskets if baskets is not None else basket_library()["baskets"]
    r = _returns(sorted({m["symbol"] for b in baskets for m in b["members"]} | {b["hedge"] for b in baskets}))
    out = []
    for b in baskets:
        members = [m["symbol"] for m in b["members"]]
        short = [s for s in members if s not in r or r[s].notna().sum() < MIN_DAYS]
        usable = [s for s in members if s not in short]
        sc = scores(r, b["hedge"], usable) if b["hedge"] in r and len(usable) >= 3 else {}
        avg = float(np.mean(list(sc.values()))) if sc else None
        out.append({"id": b["id"], "name": b["name"], "hedge": b["hedge"], "average": avg, "scores": sc,
                    "weak": sorted((s for s, v in sc.items() if v < MEMBER_MIN), key=sc.get), "too_little_history": short,
                    "passes": avg is not None and avg >= BASKET_MIN and all(v >= MEMBER_MIN for v in sc.values())})
    return out


def report(rows: list[dict]) -> str:
    lines = [f"Basket cohesion over the last {SESSIONS} sessions: each member's move beyond its hedge against the rest of its basket's. "
             f"Members need {MEMBER_MIN}, baskets an average of {BASKET_MIN}.", ""]
    for x in sorted(rows, key=lambda x: (x["passes"], x["average"] or 0)):
        avg = "n/a" if x["average"] is None else f"{x['average']:.2f}"
        line = f"{'ok  ' if x['passes'] else 'FAIL'}  {x['name']} (against {x['hedge']}): average {avg}"
        if x["weak"]:
            line += "; weak: " + ", ".join(f"{s} {x['scores'][s]:.2f}" for s in x["weak"])
        if x["too_little_history"]:
            line += "; under " + str(MIN_DAYS) + " sessions of bars: " + ", ".join(x["too_little_history"])
        lines.append(line)
    return "\n".join(lines)


def main(as_json: bool = False) -> None:
    rows = check()
    print(json.dumps(rows, indent=1) if as_json else report(rows))

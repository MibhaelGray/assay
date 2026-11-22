"""The small-cap universe grouped into baskets for the dashboard's Baskets view (its Small caps tab).

Every name in universe.json goes into one basket: its Nasdaq industry when four or more names share it, otherwise
"Other <sector>". Every basket is held against IWM (the small-cap ETF). Built in the shape of
journal/reference/baskets.json, so market._baskets draws it the same way as the themed baskets. Nothing is traded.
"""

from collections import defaultdict

from . import HEDGE, universe

MIN_MEMBERS = 4
SHORT = {"Trusts Except Educational Religious and Charitable": "Trusts", "EDP Services": "IT services",
         "Computer Software: Prepackaged Software": "Packaged software",
         "Computer Software: Programming Data Processing": "Software and data processing",
         "Finance: Consumer Services": "Consumer finance", "Auto Parts:O.E.M.": "Auto parts",
         "Mining & Quarrying of Nonmetallic Minerals (No Fuels)": "Nonmetallic mining"}


def _slug(text: str) -> str:
    return "sc-" + "".join(ch if ch.isalnum() else "-" for ch in text.lower()).strip("-").replace("--", "-")


def library(doc: dict | None = None) -> dict:
    """The small-cap baskets, as baskets.json holds the themed ones."""
    doc = doc if doc is not None else universe.load()
    if not doc:
        return {"baskets": []}
    by_industry: dict[str, list[dict]] = defaultdict(list)
    for r in doc["names"]:
        by_industry[r.get("industry") or ""].append(r)
    groups: dict[str, list[dict]] = defaultdict(list)
    for industry, rows in by_industry.items():
        if industry and len(rows) >= MIN_MEMBERS:
            groups[SHORT.get(industry, industry)].extend(rows)
        else:
            for r in rows:
                groups[f"Other {(r.get('sector') or 'unclassified').lower()}"].append(r)
    baskets = []
    for name, rows in sorted(groups.items(), key=lambda kv: (kv[0].startswith("Other"), kv[0])):
        rows = sorted(rows, key=lambda r: -(r.get("dollar_volume") or 0))
        caps = sorted(r["cap"] for r in rows)
        thin = sum(1 for r in rows if r.get("analysts") is not None and r["analysts"] <= 2)
        baskets.append({
            "id": _slug(name), "name": name, "hedge": HEDGE, "source": "the small-cap universe (smallcap/universe.py)",
            "story": (f"{len(rows)} US small caps worth ${caps[0] / 1e6:,.0f}M to ${caps[-1] / 1e6:,.0f}M, grouped by Nasdaq's industry "
                      f"label, held in equal weight against IWM (the small-cap ETF). {thin} of them have two analysts or fewer."),
            "watch": "A group-wide move against IWM usually means sector news: a peer's earnings, a contract or a policy change.",
            "members": [{"symbol": r["symbol"], "avg_usd_volume_90d": r.get("dollar_volume"),
                         "why": f"{r['name'].replace(' Common Stock', '')} · ${r['cap'] / 1e6:,.0f}M"
                                + ("" if r.get("analysts") is None else f" · {r['analysts']} analyst{'s' if r['analysts'] != 1 else ''}")}
                        for r in rows]})
    return {"snapshot_at": doc.get("built"), "baskets": baskets}

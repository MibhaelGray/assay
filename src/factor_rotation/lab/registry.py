"""Ideas, the ledger of everything run, and the run itself.

An idea is one file in ideas/ holding an `IDEA` and a `score(p, u, **params)` function. `fr lab run <id>` works out
every variant on the discovery and validation years, writes one line per variant to journal/lab/trials.jsonl and says
where each stands against the bar (lab.BAR). The ledger is only ever added to: a failed idea stays in it, because
the count of things tried is what the luck haircut (stats.deflated) is worked out from.

The holdout years are opened by `fr lab holdout <id> <variant>` alone: only for a variant that passed both earlier
periods, once per idea, and the look is written to journal/lab/holdout.jsonl whatever it shows.
"""

import hashlib
import importlib
import json
import pkgutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import BAR, MAX_VARIANTS, PERIODS, ROOT, engine, panel, stats
from ..config import CT

TRIALS = ROOT / "trials.jsonl"
HOLDOUT = ROOT / "holdout.jsonl"
RESULTS = ROOT / "results"


@dataclass
class Idea:
    id: str
    family: str          # reversal, momentum, earnings, volume, calendar, overnight, volatility, cross-asset, ...
    title: str
    why: str             # the reason it might work, written before it is run
    source: str = ""     # the paper, note or observation it came from
    market: str = "us"
    universe: dict = field(default_factory=dict)    # arguments to Panel.universe
    base: dict = field(default_factory=dict)        # engine and score settings every variant starts from
    variants: dict = field(default_factory=lambda: {"base": {}})   # name -> what it changes


def ideas() -> dict[str, tuple[Idea, object]]:
    """Every idea file: id -> (its IDEA, its module)."""
    from . import ideas as package
    out = {}
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        if hasattr(module, "IDEA"):
            out[module.IDEA.id] = (module.IDEA, module)
    return out


def _fingerprint(module) -> str:
    """Changes when the idea's own file changes or the engine's rules do."""
    return hashlib.sha1(Path(module.__file__).read_bytes() + str(engine.VERSION).encode()).hexdigest()[:10]


def _split(settings: dict) -> tuple[dict, dict, dict]:
    settings = dict(settings)
    universe = settings.pop("universe", {})
    eng = {k: settings.pop(k) for k in list(settings) if k in engine.KEYS}
    return universe, eng, settings


def backtest(idea: Idea, module, variant: str, holdout: bool = False) -> engine.Result:
    p = panel.load(idea.market, holdout=holdout)
    universe, eng, params = _split({**idea.base, **idea.variants[variant]})
    spec = {**idea.universe, **universe}
    if "funds" in spec:   # an idea about funds or the index itself: in play whenever the fund has a price
        funds = [panel.MARKETS[idea.market]["bench"]] if spec["funds"] == "bench" else spec["funds"]   # "bench": this market's hedge fund
        u = p.close[funds].notna()
    else:
        u = p.universe(**spec)
    score = module.score(p, u, **params)
    return engine.run(p, score.where(u.reindex(columns=score.columns, fill_value=False)), **eng)


def verdict(discovery: dict, validation: dict) -> str:
    """candidate: passes discovery and validation. faded: passed discovery, failed validation. weak: something there,
    under the bar. dead: nothing."""
    if "sharpe" not in discovery:
        return "no-data"
    d = BAR["discovery"]
    found = (discovery["sharpe"] >= d["sharpe"] and discovery["t"] >= d["t"] and discovery["sharpe_2x_cost"] >= d["sharpe_2x_cost"]
             and discovery["good_years"] >= d["good_years"])
    if found:
        return "candidate" if validation.get("sharpe", -9) >= BAR["validation"]["sharpe"] else "faded"
    return "weak" if discovery["sharpe"] >= 0.5 and validation.get("sharpe", -9) > 0 else "dead"


def all_trials() -> list[dict]:
    if not TRIALS.exists():
        return []
    return [json.loads(line) for line in TRIALS.read_text().splitlines() if line.strip()]


def latest() -> dict[tuple, dict]:
    """The newest line for each (idea, variant)."""
    return {(t["idea"], t["variant"]): t for t in all_trials()}


def tried() -> int:
    """How many distinct things have ever been run: each (idea, variant, version of its file) counts once."""
    return len({(t["idea"], t["variant"], t["code"]) for t in all_trials()})


def find(idea_id: str) -> tuple[Idea, object]:
    """An idea by id. `<id>@<market>` is the same idea file run on another market (rev_resid@jp)."""
    from dataclasses import replace
    base, _, market = idea_id.partition("@")
    idea, module = ideas()[base]
    return (replace(idea, id=idea_id, market=market), module) if market and market != idea.market else (idea, module)


def run(idea_id: str, only: str | None = None) -> list[dict]:
    """Run an idea's variants on discovery and validation and add them to the ledger."""
    idea, module = find(idea_id)
    if len(idea.variants) > MAX_VARIANTS:
        raise ValueError(f"{idea_id} has {len(idea.variants)} variants; the limit is {MAX_VARIANTS}. Split it into ideas with their own reasons.")
    code, rows = _fingerprint(module), []
    ROOT.mkdir(parents=True, exist_ok=True)
    for variant in idea.variants:
        if only and variant != only:
            continue
        result = backtest(idea, module, variant)
        periods = {name: stats.summary(result, *PERIODS[name]) for name in ("discovery", "validation")}
        row = {"at": datetime.now(CT).isoformat(timespec="seconds"), "idea": idea.id, "family": idea.family, "variant": variant,
               "market": idea.market, "settings": {**idea.base, **idea.variants[variant]}, "universe": idea.universe, "code": code,
               **periods, "verdict": verdict(periods["discovery"], periods["validation"])}
        with TRIALS.open("a") as f:
            f.write(json.dumps(row) + "\n")
        rows.append(row)
    pool = tried()
    for row in rows:
        d = row["discovery"]
        row["deflated"] = round(stats.deflated(d["daily_sharpe"], d["days"], d["skew"], d["kurt"], pool), 3) if "daily_sharpe" in d else None
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{idea.id}.json").write_text(json.dumps({"idea": idea.__dict__, "trials": rows}, indent=1))
    return rows


def holdout(idea_id: str, variant: str, force: bool = False) -> dict:
    """The one look at the holdout years."""
    idea, module = find(idea_id)
    last = latest().get((idea_id, variant))
    if not last or last["code"] != _fingerprint(module):
        raise ValueError("run the idea first: the ledger has no line for this variant as the file now stands")
    if last["verdict"] != "candidate" and not force:
        raise ValueError(f"{idea_id}:{variant} is '{last['verdict']}', not a candidate; the holdout is for candidates")
    looks = [json.loads(line) for line in HOLDOUT.read_text().splitlines()] if HOLDOUT.exists() else []
    if any(x["idea"] == idea_id for x in looks) and not force:
        raise ValueError(f"{idea_id} has had its holdout look; a second one is no longer a clean test")
    result = backtest(idea, module, variant, holdout=True)
    out = stats.summary(result, *PERIODS["holdout"])
    row = {"at": datetime.now(CT).isoformat(timespec="seconds"), "idea": idea_id, "variant": variant, "code": last["code"],
           "forced": force, "holdout": out, "passed": out.get("sharpe", -9) >= BAR["holdout"]["sharpe"]}
    with HOLDOUT.open("a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def board() -> list[dict]:
    """Every (idea, variant) as it last ran, best discovery Sharpe first, with the luck haircut."""
    pool = tried()
    looks = {(x["idea"], x["variant"]): x for x in ([json.loads(line) for line in HOLDOUT.read_text().splitlines()] if HOLDOUT.exists() else [])}
    rows = []
    for (idea, variant), t in latest().items():
        d, v = t["discovery"], t["validation"]
        look = looks.get((idea, variant))
        rows.append({"idea": idea, "variant": variant, "family": t["family"], "verdict": t["verdict"],
                     "disc_sharpe": d.get("sharpe"), "gross": d.get("sharpe_gross"), "disc_t": d.get("t"), "disc_2x": d.get("sharpe_2x_cost"), "good_years": d.get("good_years"),
                     "val_sharpe": v.get("sharpe"), "bp_trade": d.get("bp_per_trade"), "turnover": d.get("turnover"), "names": d.get("names"),
                     "deflated": round(stats.deflated(d["daily_sharpe"], d["days"], d["skew"], d["kurt"], pool), 3) if "daily_sharpe" in d else None,
                     "holdout": look["holdout"].get("sharpe") if look else None})
    return sorted(rows, key=lambda r: -(r["disc_sharpe"] if r["disc_sharpe"] is not None else -9))


def table(rows: list[dict], columns: list[str]) -> str:
    """A plain text table."""
    cells = [[("" if r.get(c) is None else str(r.get(c))) for c in columns] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) if cells else len(c) for i, c in enumerate(columns)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(columns, widths))]
    lines += ["  ".join(v.ljust(w) for v, w in zip(row, widths)) for row in cells]
    return "\n".join(lines)


def write_board() -> Path:
    """journal/lab/board.md: the standing of everything tried, rewritten after each run."""
    rows = board()
    pool = tried()
    cols = ["idea", "variant", "family", "verdict", "disc_sharpe", "gross", "disc_t", "disc_2x", "good_years", "val_sharpe", "bp_trade", "turnover", "names", "deflated", "holdout"]
    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    head = (f"# Research lab board\n\nRewritten by `fr lab run` and `fr lab board`; do not edit by hand. {pool} variants tried across "
            f"{len({r['idea'] for r in rows})} ideas: " + ", ".join(f"{n} {v}" for v, n in sorted(counts.items())) + ".\n\n"
            "Sharpe is after costs. `disc` = discovery years (2010-2021), `val` = validation (2022-2024), `gross` = before costs, `2x` = with costs doubled, "
            "`deflated` = chance the discovery Sharpe is real given everything tried (bar 0.95), `holdout` = 2025 on, one look.\n\n")
    body = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n" + "\n".join(
        "| " + " | ".join("" if r.get(c) is None else str(r.get(c)) for c in cols) + " |" for r in rows)
    path = ROOT / "board.md"
    path.write_text(head + body + "\n")
    return path

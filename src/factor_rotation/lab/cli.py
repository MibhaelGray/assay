"""`fr lab ...`: the lab's commands. None of them reaches the gateway or the paper account."""

import json

from . import registry

COLUMNS = ["variant", "verdict", "sharpe", "gross", "t", "2x", "years+", "val", "val_t", "ret", "vol", "dd", "beta", "bp/trade", "turn", "names", "deflated"]


def _line(row: dict) -> dict:
    d, v = row["discovery"], row["validation"]
    return {"variant": row["variant"], "verdict": row["verdict"], "sharpe": d.get("sharpe"), "gross": d.get("sharpe_gross"), "t": d.get("t"), "2x": d.get("sharpe_2x_cost"),
            "years+": d.get("good_years"), "val": v.get("sharpe"), "val_t": v.get("t"), "ret": d.get("ret"), "vol": d.get("vol"),
            "dd": d.get("drawdown"), "beta": d.get("beta"), "bp/trade": d.get("bp_per_trade"), "turn": d.get("turnover"),
            "names": d.get("names"), "deflated": row.get("deflated")}


def main(a) -> None:
    if a.job == "pull":
        from . import panel
        print(json.dumps(panel.pull(a.market, fresh=a.fresh)))
    elif a.job == "build":
        from . import panel
        print(json.dumps(panel.build(a.market)))
    elif a.job == "fetch":
        import importlib
        for name in a.args:
            if name == "earnings":
                from . import events, panel
                print(json.dumps({"source": "earnings", "fetched": events.pull(panel.load(a.market, holdout=True).dates)}))
            else:
                print(json.dumps(importlib.import_module(f"{__package__}.sources.{name}").pull()))
    elif a.job == "ideas":
        done = {i for i, _ in registry.latest()}
        for idea_id, (idea, _) in sorted(registry.ideas().items()):
            print(f"{'run ' if idea_id in done else 'new '} {idea_id:28} {idea.family:12} {idea.title}")
    elif a.job == "run":
        known = registry.ideas()
        done = {i for i, _ in registry.latest()}
        ids = [i for i in known if i not in done] if a.args == ["new"] else a.args
        for idea_id in ids:
            idea = registry.find(idea_id)[0]
            rows = registry.run(idea_id)
            print(f"\n{idea_id} ({idea.family}): {idea.title}")
            print(registry.table([_line(r) for r in rows], COLUMNS))
        print(f"\nboard: {registry.write_board()}")
    elif a.job == "board":
        rows = registry.board()
        print(registry.table(rows[:60], ["idea", "variant", "verdict", "disc_sharpe", "gross", "disc_t", "disc_2x", "good_years", "val_sharpe", "bp_trade", "deflated", "holdout"]))
        print(f"\n{registry.tried()} variants tried. board: {registry.write_board()}")
    elif a.job == "show":
        print((registry.RESULTS / f"{a.args[0]}.json").read_text())
    elif a.job == "session":
        from . import session
        out = session.run()
        print(f"lab session: {'ok' if out.get('ok') else 'NOTHING DONE'}; {out.get('variants_run', 0)} variants run, "
              f"{len(out.get('new_candidates') or [])} new candidates, {len(out.get('refused') or [])} refused, {out.get('minutes', 0)} min"
              f"{'; ' + out['error'] if out.get('error') else ''}")
        for r in out.get("refused") or []:
            print(f"  refused: {r}")
    elif a.job == "holdout":
        print(json.dumps(registry.holdout(a.args[0], a.args[1], force=a.force), indent=1))

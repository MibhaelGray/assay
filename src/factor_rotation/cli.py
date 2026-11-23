import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import bars, drawdown, journal
from .config import NY
from .universe import bar_universe


def _today() -> date:
    return datetime.now(NY).date()


def _trading_day(full: bool = False, closing: bool = False, now: datetime | None = None) -> bool:
    """`fr trading-day`: a trading day, and with `full` not a half day, and with `closing` now within the 30 minutes
    before today's close. The close job is started at 3:45 PM ET and at 12:45 PM ET; this lets the one that fits run."""
    now = now or datetime.now(NY)
    today = now.date()
    if not journal.is_trading_day(today) or (full and journal.is_half_day(today)):
        return False
    if closing:
        close = datetime.combine(today, journal.close_time(today), NY)
        return close - timedelta(minutes=30) <= now < close
    return True


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr)
    # ib_async logs every API error itself; the paper broker and gateway code report the ones that matter.
    logging.getLogger("ib_async").setLevel(logging.ERROR)
    logging.getLogger("ib_async.wrapper").setLevel(logging.CRITICAL)
    p = argparse.ArgumentParser(prog="fr")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("bars", help="pull daily adjusted bars")
    c.add_argument("--duration", default="5y")
    c.add_argument("--symbols")
    c.add_argument("--pending", action="store_true",
                   help="only names outside the standard universe: the paper book's legs and the basket watchlist")
    c.add_argument("--intraday", type=int, metavar="DAYS", default=None,
                   help="5-minute bars of the last DAYS sessions (at most 59) for every verdict still being scored, "
                        "so its stop is replayed on the order job's 10-minute checks")

    c = sub.add_parser("brief", help="build the nightly brief from stored data")
    c.add_argument("--as-of", default=None)

    c = sub.add_parser("trading-day", help="exit 0 if today is an NYSE trading day, else 1")
    c.add_argument("--full", action="store_true", help="and not a half day (the midday pass)")
    c.add_argument("--closing", action="store_true",
                   help="and now is in the close job's window, the 30 minutes before today's close (4:00 PM or 1:00 PM ET)")
    c = sub.add_parser("market", help="write tonight's market rows for the dashboard from the brief")
    c.add_argument("--as-of", default=None)
    c.add_argument("--out", type=Path, default=Path("market.json"))

    c = sub.add_parser("serve", help="serve the local dashboard with a working Refresh button")
    c.add_argument("--port", type=int, default=8765)

    sub.add_parser("earnings", help="refresh the earnings dates for the radar, from Yahoo Finance")
    sub.add_parser("gaps", help="rebuild the table of what stocks did after earnings gaps")
    sub.add_parser("scan", help="list tonight's stock and basket candidates")
    c = sub.add_parser("baskets", help="check: do the themed baskets' members move together beyond their hedge? (basket_check.py)")
    c.add_argument("action", choices=["check"])
    c.add_argument("--json", action="store_true")
    c = sub.add_parser("screen", help="check every name, basket and factor pair against the setups; write the shortlist")
    c.add_argument("--live", choices=["night", "late", "late2", "late3", "late4", "morning", "midday"], default=None,
                   help="the screen for one of the three daily passes, on the latest prices (after-hours, pre-market or the session so far)")

    c = sub.add_parser("paper", help="paper-trading jobs on the IBKR paper account (see paper/__init__.py)")
    c.add_argument("--execute", action="store_true",
                   help="a job that sends orders only looks when it is run by hand: it prints what it would send "
                        "and sends nothing. This makes it count, as the scheduled job does")
    c.add_argument("job", choices=["enter", "close", "sync", "status", "report", "halt", "resume",
                                   "propose", "postmortem", "learn", "hedges", "side", "holds", "depth"])
    c.add_argument("args", nargs="*", help="propose, postmortem: the draft JSON file; hedges: the symbols on one side; "
                                           "side: `report` to score the side facts instead of pulling them")
    c.add_argument("--flatten", action="store_true", help="close: exit every open trade at today's close")
    c.add_argument("--reason", default="", help="halt: why, kept in the HALT file")
    c.add_argument("--book", choices=["swing"],
                   help="resume: lift the book's 10%% pause (drawdown.py) instead of the HALT file")
    c = sub.add_parser("pass", help="run one of the swing book's research passes as a background job (see passes.py)")
    c.add_argument("name", choices=["night", "late", "morning", "midday"])
    c.add_argument("--shadow", action="store_true", help="a trial run: work on a copy of the journal with orders off")
    c = sub.add_parser("x", help="posts from the accounts the owner follows on X, read by a plain script (xfeed.py)")
    c.add_argument("job", choices=["login", "pull"])
    c.add_argument("--hours", type=float, default=None, help="pull: read this far back instead of to the last stored post")
    c.add_argument("--show", action="store_true", help="pull: run Chrome in a window you can watch")
    c = sub.add_parser("watch", help="the watchdog: phone alerts when a pass, a job or the paper gateway breaks (watch.py)")
    c.add_argument("--test", action="store_true", help="send one test alert and stop")
    c.add_argument("--telegram-chats", action="store_true", help="list the chats that messaged the Telegram bot, to find FR_TELEGRAM_CHAT")
    c = sub.add_parser("quotes", help="current prices from Yahoo for stocks, ETFs, indexes (VIX, TNX) and futures (ES=F, CL=F)")
    c.add_argument("names", nargs="+")
    c = sub.add_parser("dashboard", help="build a local copy of the dashboard (dashboard/local.html) and open it")
    c.add_argument("--as-of", default=None, help="brief date for tonight's market rows (default: the latest brief)")
    c.add_argument("--out", type=Path, default=None)
    c.add_argument("--no-open", action="store_true", help="build without opening it in the browser")
    c = sub.add_parser("publish", help="put a fresh copy of the dashboard on the public GitHub site")
    c.add_argument("--pull", action="store_true", help="first pull today's prices and the paper account, as the local Refresh button does")
    c.add_argument("--force", action="store_true", help="push even if the page hasn't changed")

    a = p.parse_args(argv)
    if a.cmd == "bars":
        from .market import basket_library
        from .paper import book
        watch = [m["symbol"] for b in basket_library()["baskets"] for m in b["members"]]
        extra = sorted({s for s in [*watch, *book.symbols_held()] if s not in set(bar_universe())})
        if a.intraday:
            from .paper import learn
            print(json.dumps(bars.collect_intraday(learn.intraday_symbols(), a.intraday)))
            return
        if a.pending:
            if not extra:
                print(json.dumps({"symbols": 0, "failed": []}))
                return
            print(json.dumps(bars.collect(extra, "2y")))
            return
        symbols = [s.strip().upper() for s in a.symbols.split(",")] if a.symbols else bar_universe() + extra
        print(json.dumps(bars.collect(symbols + ([] if a.symbols else list(bars.INDEXES)), a.duration)))
    elif a.cmd == "brief":
        as_of = date.fromisoformat(a.as_of) if a.as_of else _today()
        brief = journal.build_brief(as_of)
        print(f"journal/briefs/{as_of}.json  pairs={len(brief['factor_pairs'])} bars_through={brief['bars_through']}")
    elif a.cmd == "x":
        from . import xfeed
        if a.job == "login":
            xfeed.login()
        else:
            r = xfeed.pull(headless=False if a.show else None, hours=a.hours)
            print(json.dumps(r))
            sys.exit(0 if r["ok"] else 1)
    elif a.cmd == "watch":
        from . import watch
        if a.telegram_chats:
            print("\n".join(watch.telegram_chats()) or "no chats yet: send the bot a message in Telegram first")
        else:
            watch.test() if a.test else watch.run()
    elif a.cmd == "trading-day":
        sys.exit(0 if _trading_day(a.full, a.closing) else 1)
    elif a.cmd == "serve":
        from . import serve
        serve.run(a.port)
    elif a.cmd == "gaps":
        from .analytics import earnings_gaps
        df = earnings_gaps.events()
        rows = earnings_gaps.table(df)
        earnings_gaps.PATH.write_text(json.dumps({"events": len(df), "from": str(df["session"].min().date()),
                                                  "to": str(df["session"].max().date()), "rows": rows}, indent=1) + "\n")
        print(f"{earnings_gaps.PATH}  events={len(df)}")
    elif a.cmd == "pass":
        from . import passes
        out = passes.run(a.name, a.shadow)
        print(f"pass {a.name}{' (shadow)' if a.shadow else ''}: {'recorded ' + out['recorded'] if out.get('recorded') else 'NOTHING RECORDED'}"
              f"{'; ' + out['error'] if out.get('error') else ''} in {out.get('minutes', 0)} min, {out.get('agents') or 0} agents, {len(out.get('refused') or [])} refused")
        for r in out.get("refused") or []:
            print(f"  refused {r}")
        print(out.get("message") or "")
        sys.exit(0 if out["ok"] else 1)
    elif a.cmd == "quotes":
        from . import quotes
        print(quotes.text(quotes.fetch(a.names)))
    elif a.cmd == "baskets":
        from . import basket_check
        basket_check.main(a.json)
    elif a.cmd == "scan":
        from . import scan
        path, payload = scan.write()
        print(f"{path}  tonight={len(payload['reports']['tonight'])} traded={len(payload['reports']['traded_today'])} "
              f"settled={payload['bars_settled']}")
    elif a.cmd == "screen":
        from . import screen
        try:
            path, payload = screen.write_live(a.live) if a.live else screen.write()
        except (ValueError, RuntimeError) as e:
            sys.exit(f"fr screen refused: {e}")
        print(path)
        print(screen.summary(payload))
    elif a.cmd == "earnings":
        from . import earnings
        print(json.dumps(earnings.pull()))
    elif a.cmd == "market":
        from . import market
        as_of = date.fromisoformat(a.as_of) if a.as_of else _today()
        rows = market.build(as_of)
        a.out.write_text(json.dumps(rows, separators=(",", ":"), default=journal._json_default) + "\n")
        counts = {}
        for r in rows:
            counts[r["section"]] = counts.get(r["section"], 0) + 1
        print(f"{a.out}  " + " ".join(f"{k}={v}" for k, v in counts.items()))
    elif a.cmd == "dashboard":
        import webbrowser

        from . import localdash
        out = a.out or localdash.DEFAULT_OUT
        info = localdash.build(out, date.fromisoformat(a.as_of) if a.as_of else None)
        print(f"{info['out']}  market_rows={info['market_rows']} ({info['market']})")
        if not a.no_open:
            webbrowser.open(out.resolve().as_uri())
    elif a.cmd == "publish":
        from . import publish
        info = publish.run(pull=a.pull, force=a.force)
        what = "published" if info["pushed"] else "nothing changed, not pushed"
        extra = "".join(f"; {k}: {info[k]}" for k in ("paper", "failed") if info.get(k))
        print(f"{what} at {info['at']} ({info['market']}): {info['url']}{extra}")
    elif a.cmd == "paper":
        _paper(a)


def _looking(a) -> bool:
    """True when this run only looks. A tick started by deploy/run.sh is a scheduled one and counts; a tick run
    by hand writes nothing and sends nothing unless --execute says so."""
    return not (os.environ.get("FR_SCHEDULED") == "1" or getattr(a, "execute", False))


def _dry_tag(dry: bool, looking: bool) -> str:
    if looking:
        return " (a look: nothing sent; add --execute to trade)"
    return " (dry run: set FR_PAPER_SEND=1 to send)" if dry else ""


def _notify(job: str, warnings: list[str]) -> None:
    """Print a job's warnings and, on a Mac, raise a notification, so a failure isn't found days later in a
    log. Best effort: a notification that can't be shown changes nothing."""
    for w in warnings:
        print(f"  ! {w}")
    if not warnings or sys.platform != "darwin":
        return
    import subprocess

    text = warnings[0] if len(warnings) == 1 else f"{len(warnings)} warnings. First: {warnings[0]}"
    script = 'on run argv\ndisplay notification (item 1 of argv) with title (item 2 of argv)\nend run'
    try:
        subprocess.run(["osascript", "-e", script, text[:240], f"Paper {job}"], capture_output=True, timeout=10)
    except Exception:
        pass


def _keep_depth(session: str) -> None:
    """The pass's research depth, kept beside the verdicts (paper/depth.py); it never stops a pass recording."""
    try:
        from .paper import depth
        m = depth.measure()
        if m["measured"] and depth.keep(date.fromisoformat(session), m):
            for f in m["flags"]:
                print(f"  note: {f}")
            if m["thin"]:
                print(f"  note: research was thin for {', '.join(sorted(m['thin']))} (uv run fr paper depth)")
    except Exception as e:  # noqa: BLE001
        print(f"  note: research depth not kept ({type(e).__name__}: {e})")


def _keep_reports(session: str, kind: str) -> None:
    """Every research agent's report, kept for the record (paper/depth.py); it never stops a pass recording."""
    try:
        from .paper import depth
        if path := depth.keep_reports(date.fromisoformat(session), kind):
            print(f"  research reports kept in {path}")
    except Exception as e:  # noqa: BLE001
        print(f"  note: research reports not kept ({type(e).__name__}: {e})")


def _research(a) -> None:
    """The research pass's commands. They only read and write the journal; none of them reaches the gateway."""
    from .paper import learn, proposals

    def draft() -> dict:
        if len(a.args) != 1:
            sys.exit(f"usage: fr paper {a.job} <draft.json>")
        return json.loads(Path(a.args[0]).read_text())

    try:
        if a.job == "propose":
            d = draft()
            if d.get("pass") in proposals.NEW_PASSES:   # one of the three daily passes (paper/daily.py)
                from .paper import daily
                path, doc, notes = daily.record(d)
                mine = [p for p in doc["proposals"] if p.get("pass") == d["pass"]]
                takes = [p for p in mine if p["decision"] == "take"]
                print(f"{path}: the {d['pass']} pass for {doc['session']} is recorded: {len(mine)} verdicts, {len(takes)} takes, "
                      f"{doc['passes'][-1]['reviews']} trades reviewed. The order job trades them within about a minute.")
                for p in takes:
                    legs = " / ".join(filter(None, [p["long"] and "long " + ", ".join(p["long"]), p["short"] and "short " + ", ".join(p["short"])]))
                    print(f"  {p['id']}  {p['name'][:24]:<24} conviction {p['conviction']}  {p['plan']['horizon_days']}d, stop {abs(p['plan']['stop']['spread_pct']):g}%  {legs}")
                for n in notes:
                    print(f"  note: {n}")
                _keep_depth(doc["session"])
                _keep_reports(doc["session"], d["pass"])
                return
            sys.exit(f'fr paper propose refused: the draft needs "pass": one of {", ".join(proposals.NEW_PASSES)}')
        elif a.job == "postmortem":
            print(learn.record_postmortems(draft()))
        elif a.job == "learn":
            print(learn.brief())
        elif a.job == "hedges":
            if not a.args:
                sys.exit("usage: fr paper hedges SYMBOL [SYMBOL ...]  (several symbols are one equal-weight side)")
            print(f"{', '.join(a.args)} against each ETF, daily returns over 60 sessions:")
            for r in learn.hedges(a.args):
                print(f"  {r['hedge']:<6} correlation {r['corr_60d']:+.2f}  beta {r['beta_60d']:.2f}")
    except (ValueError, FileNotFoundError, FileExistsError, KeyError) as e:
        sys.exit(f"fr paper {a.job} refused: {e}")


def _paper(a) -> None:
    from .paper import book
    from .paper.broker import IBPaper, PaperSettings

    if a.job in ("propose", "postmortem", "learn", "hedges"):
        return _research(a)
    if a.job == "depth":
        # How much research each candidate got, from this session's transcripts (paper/depth.py). `args`: a session id.
        from .paper import depth
        return print(depth.report(depth.measure(a.args[0] if a.args else None)))
    if a.job == "holds":
        # Every idea scored at every hold from 1 to 10 sessions (paper/holds.py). Journal and bars only.
        from .paper import holds
        return print(holds.report())
    if a.job == "side":
        # Facts recorded beside the verdicts and kept from the research passes (paper/side.py). No gateway.
        from .paper import side
        if a.args == ["report"]:
            return print(side.report())
        r = side.record(dry=_looking(a))
        print(f"paper side {r['session']}: " + (r.get("note") or f"{len(r['pulled'])} names pulled, {len(r['failed'])} failed, {r['have']} already had"))
        if r["failed"]:
            print("  ! nothing from Yahoo for " + ", ".join(r["failed"]) + "; the next run tries again")
        return
    today = _today()
    if a.job in ("close", "sync") and not journal.is_trading_day(today):
        logging.info("market_closed date=%s", today)
        return
    # A close job that starts late (the Mac woke from sleep, say) must not send after the auction cutoff:
    # market-on-close by 3:50 PM ET. Missed exits go out the next session, since a trade past its time limit
    # exits at the next check.
    now = datetime.now(NY).strftime("%H:%M")
    if a.job == "close" and not ("15:30" <= now <= "15:49") and PaperSettings().send:
        ct = lambda hm: f"{(int(hm[:2]) - 1) % 12 or 12}:{hm[3:]} {'PM' if int(hm[:2]) - 1 >= 12 else 'AM'}"  # New York to Central
        print(f"paper close: {ct(now)} CT is outside {ct('15:30')}-{ct('15:49')} CT, so nothing was sent")
        sys.exit(1)
    looking = _looking(a) if a.job in ("enter", "close") else False
    if a.job == "enter":
        # The three-pass book's order job (paper/entries.py): trades a pass's takes and exits once it has recorded.
        from .paper import entries
        r = asyncio.run(entries.run(dry=looking))
        if not looking and (healed := entries.heal_gateway(r.get("no_quote") or {})):   # after the run: its connection is closed
            entries.retry_soon()
            r["warnings"].append(healed + "; the next try is in a minute or two.")
        if r.get("note") and not any(r[k] for k in ("passes", "orders", "warnings")):
            # launchd runs this every minute; a run with nothing to do stays out of the log
            return None if os.environ.get("FR_SCHEDULED") == "1" else print(f"paper enter: {r['note']}")
        tag = _dry_tag(r["dry_run"], looking)
        print(f"paper enter {r['at']}{tag}: {r['venue'] or 'no'} session, passes {', '.join(r['passes']) or 'none new'}, "
              f"{len(r['new'])} new trades, {len(r['exits'])} exits, {len(r['orders'])} orders" + (f"; {r['note']}" if r.get("note") else ""))
        for o in r["orders"]:
            print(f"  {o['trade_id']:<16} {o['action']:<4} {o['shares']:>6} {o['symbol']:<6} limit {o['limit']:.2f}  {o.get('status', '')}")
        for k in r["skipped"]:
            print(f"  skipped {k['name']}: {k['reason']}")
        _notify("enter", r["warnings"])
    elif a.job == "close":
        out = asyncio.run(book.close_session(today, flatten=a.flatten, dry=looking))
        tag = _dry_tag(out["dry_run"], looking)
        print(f"paper close {today}{tag}: {len(out['exits'])} exits, {len(out['orders'])} orders")
        for e in [*out["exits"], *out["retried"]]:
            print(f"  {e['trade_id']}: {e['reason']}")
        _notify("close", out["warnings"])
    elif a.job == "sync":
        out = asyncio.run(book.sync(today))
        print(f"paper sync {today}: {len(out['fills'])} fills, {len(out['positions'])} positions, net liquidation {out['net_liquidation']}")
        for m in out["mismatches"]:
            print(f"  ! {m}")
    elif a.job == "status":
        async def status():
            async with IBPaper(readonly=True) as b:
                return b.s.account, await b.net_liquidation(), await b.positions()
        account, nav, positions = asyncio.run(status())
        print(f"paper account {account} on port {PaperSettings().port}: net liquidation {nav}, {len(positions)} positions")
        print(f"orders {'ON (FR_PAPER_SEND=1)' if PaperSettings().send else 'off: dry runs only'}; "
              f"{'HALTED: ' + book.HALT.read_text().strip() if book.HALT.exists() else 'not halted'}")
        from . import portfolio
        for name in portfolio.running():
            d = drawdown.status(name)
            print(f"  {name} book ${d['value']:,.0f}, high ${d['peak']:,.0f}, pauses at ${d['pause_at']:,.0f}"
                  + (f"; PAUSED: {d['paused']}" if d["paused"] else ""))
        for m in book.reconcile(positions):
            print(f"  ! {m}")
    elif a.job == "report":
        print(book.summary_text())
        for w in book.alerts():
            print(f"  ! {w}")
        for r in book.rows():
            print(f"  {r['id']:<28} {r['state']:<8} {r['name'][:30]:<30} {r['bps'] if r['bps'] is not None else '-':>8} bp  {r['reason'] or ''}")
    elif a.job == "halt":
        book.HALT.parent.mkdir(parents=True, exist_ok=True)
        book.HALT.write_text((a.reason or "halted by hand") + "\n")
        print(f"{book.HALT} written: no new paper trades until fr paper resume")
    elif a.job == "resume" and a.book:
        d = drawdown.resume(a.book)
        print(f"{a.book} book resumed at ${d['value']:,.0f}; it pauses again at ${d['value'] - d['limit']:,.0f}")
    elif a.job == "resume":
        book.HALT.unlink(missing_ok=True)
        print("paper trading resumed")

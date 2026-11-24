"""The paper book's jobs against a stand-in broker and a temporary journal. No network, no gateway.

    uv run python -m unittest discover -s tests
"""

import asyncio
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from factor_rotation.paper import book, rules

DAY = date(2026, 11, 9)       # a Monday
NEXT = date(2026, 11, 10)


PASS_CASE = {"case_for": "a move", "case_against": "no story", "why_not": "nothing behind it"}

class Broker:
    """Records what it is asked to send. `fill` decides which sent orders fill (default: all, at 100)."""

    def __init__(self, fills=(), positions=None, orders=(), fill=lambda o: True, fail_place=False):
        self._fills, self._positions, self._orders = list(fills), dict(positions or {}), list(orders)
        self.fill, self.fail_place, self.sent, self.connections = fill, fail_place, [], 0

    async def __aenter__(self):
        self.connections += 1
        return self

    async def __aexit__(self, *exc):
        return None

    async def place(self, orders):
        if self.fail_place:
            raise ConnectionError("the gateway went away")
        self.sent += orders
        for o in orders:
            if self.fill(o):
                side = "BOT" if o["action"] == "BUY" else "SLD"
                self._fills.append(fill(o["trade_id"], o["symbol"], side, o["shares"], n=len(self._fills)))
                self._positions[o["symbol"]] = self._positions.get(o["symbol"], 0) + (o["shares"] if side == "BOT" else -o["shares"])
        return [{**o, "order_id": i, "status": "PreSubmitted", "note": ""} for i, o in enumerate(orders)]

    async def cancel(self, trade_ids):
        """Every sent order of these trades that didn't fill counts as working, once."""
        filled = {(f["order_ref"], f["side"]) for f in self._fills}
        working = [o for o in self.sent if o["trade_id"] in trade_ids and o not in getattr(self, "cancelled", [])
                   and (f"{o['trade_id']}|{o['symbol']}", "BOT" if o["action"] == "BUY" else "SLD") not in filled]
        self.cancelled = [*getattr(self, "cancelled", []), *working]
        return len(working)

    async def fills(self):
        return list(self._fills)

    async def orders(self):
        return list(self._orders)

    async def positions(self):
        return {s: v for s, v in self._positions.items() if v}

    async def net_liquidation(self):
        return 1_000_000.0


def fill(trade_id, symbol, side, shares, price=100.0, n=0):
    return {"order_ref": f"{trade_id}|{symbol}", "symbol": symbol, "side": side, "shares": float(shares), "price": price,
            "commission": 1.0, "time": None, "exec_id": f"{trade_id}-{symbol}-{side}-{n}"}


def pair(trade_id, long="AAA", short="HHH", entry=DAY, stop=None, horizon=4):
    return {"id": trade_id, "book": "dynamic", "source": "test", "name": long, "setup": "breakout", "direction": "long", "why": "",
            "entry_date": entry.isoformat(), "horizon_days": horizon, "stop": stop if stop is not None else {"spread_pct": -5.0},
            "notional": 2500, "hedge": short,
            "legs": [{"symbol": long, "side": "long", "shares": 25, "ref_price": 100.0},
                     {"symbol": short, "side": "short", "shares": 25, "ref_price": 100.0}]}


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "paper"
        for target, value in ((book, {"ROOT": self.root, "HALT": self.root / "HALT", "sleep": lambda s: None}),
                              (book.journal, {"JOURNAL": Path(tmp.name)})):
            for name, v in value.items():
                p = mock.patch.object(target, name, v)
                p.start()
                self.addCleanup(p.stop)
        p = mock.patch.object(book.asyncio, "sleep", mock.AsyncMock())
        p.start()
        self.addCleanup(p.stop)

    def write(self, kind, session, payload):
        book._write(self.root / kind / f"{session}.json", {"session": session.isoformat(), **payload})

    def read(self, kind, session):
        return json.loads((self.root / kind / f"{session}.json").read_text())

    def state(self, trade_id):
        return next(t["state"] for t in book.trades() if t["id"] == trade_id)


class Quotes(Case):
    def test_asks_again_only_for_what_is_missing(self):
        answers = [{}, {"AAA": 10.0}, {"HHH": 20.0}]
        asked = []
        with mock.patch.object(book, "_yahoo", lambda symbols: (asked.append(list(symbols)), answers.pop(0))[1]):
            self.assertEqual(book.quotes(["AAA", "HHH"], tries=4, wait=1), {"AAA": 10.0, "HHH": 20.0})
        self.assertEqual(asked, [["AAA", "HHH"], ["AAA", "HHH"], ["HHH"]])

    def test_gives_up_after_the_last_try(self):
        with mock.patch.object(book, "_yahoo", lambda symbols: {}):
            self.assertEqual(book.quotes(["AAA"], tries=3, wait=1), {})


class Orders(unittest.TestCase):
    def test_only_dead_orders_and_gateway_messages_are_warned_about(self):
        o = {"trade_id": "T1", "action": "SELL", "shares": 5, "symbol": "AAA", "note": ""}
        quiet = [{**o, "status": s} for s in ("PreSubmitted", "Submitted", "PendingSubmit", "Filled", "dry run")]
        self.assertEqual(book._order_warnings(quiet), [])
        self.assertIn("was not accepted (Cancelled)", book._order_warnings([{**o, "status": "Cancelled"}])[0])
        self.assertIn("held while shares are located", book._order_warnings([{**o, "status": "PreSubmitted", "note": "Order held while shares are located"}])[0])


def take(pid="P01", name="AAA", long=("AAA",), short=("HHH",), conviction=3, horizon=4, stop=-5.0, decision="take"):
    return {"id": f"{DAY}-{pid}", "session": DAY.isoformat(), "pass": "night", "name": name, "kind": "stock", "setup": "breakout",
            "direction": "long", "source": "screen 2026-11-06 #1", "signal": {"why": "broke out"}, "decision": decision,
            "conviction": conviction, "reason": "the reason", "wrong_if": "closes back under the breakout", "reviewed": True,
            "reasoning": None, "hedge_why": "its sector", "through_earnings": None, "stats": None,
            "long": list(long), "short": list(short), "plan": {"horizon_days": horizon, "stop": {"spread_pct": stop}}}


class Takes(unittest.TestCase):
    prices = {s: 100.0 for s in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "XLY")}

    def test_strongest_conviction_first_up_to_the_cap(self):
        names = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
        props = [take(f"P{i}", n, (n,), ("XLY",), conviction=c) for i, (n, c) in enumerate(zip(names, [3, 5, 3, 4, 3, 3]), 1)]
        trades, skipped = rules.proposal_trades(props, str(DAY), self.prices, set(), room=5)
        self.assertEqual([t["name"] for t in trades], ["BBB", "DDD", "AAA", "CCC", "EEE"])
        self.assertEqual([t["id"][-3:] for t in trades], ["A01", "A02", "A03", "A04", "A05"])
        self.assertIn("came first", skipped[0]["reason"])

    def test_skips_a_pass_and_a_name_already_held(self):
        props = [take("P2", "BBB", ("BBB",), ("XLY",), decision="pass"), take("P3", "CCC", ("CCC",), ("XLY",)), take("P4", "DDD", ("DDD",), ("XLY",))]
        trades, skipped = rules.proposal_trades(props, str(DAY), self.prices, {"CCC"})
        self.assertEqual([t["name"] for t in trades], ["DDD"])        # XLY is a shared hedge, not a clash
        self.assertEqual([k["reason"] for k in skipped], ["already holds CCC"])


class Close(Case):
    def run_close(self, day, broker, prices):
        with mock.patch.object(book, "quotes", lambda symbols, *a: {s: prices[s] for s in symbols if s in prices}):
            return asyncio.run(book.close_session(day, broker=broker))

    def held(self, trade_id="T1", **kw):
        self.write("entries", DAY, {"trades": [pair(trade_id, **kw)], "orders": [{"status": "PreSubmitted"}]})
        return Broker(fills=[fill(trade_id, "AAA", "BOT", 25), fill(trade_id, "HHH", "SLD", 25)], positions={"AAA": 25, "HHH": -25})

    def test_a_stop_rule_on_a_symbol_outside_the_trade_is_priced_and_fires(self):
        b = self.held(stop={"rules": [{"symbol": "SPY", "below": 700}]})
        out = self.run_close(DAY, b, {"AAA": 100.0, "HHH": 100.0, "SPY": 690.0})
        self.assertIn("stopped: SPY", out["exits"][0]["reason"])
        self.assertEqual({o["kind"] for o in b.sent}, {"MOC"})
        self.assertEqual(b.connections, 1)

    def test_says_when_a_stop_could_not_be_checked(self):
        b = self.held()
        out = self.run_close(DAY, b, {"AAA": 100.0})
        self.assertEqual((out["exits"], out["stops_not_checked"], b.sent), ([], ["T1"], []))
        self.assertIn("No price for HHH", out["warnings"][0])
        self.assertEqual(self.read("exits", DAY)["warnings"], out["warnings"])

    def test_holds_quietly_when_priced_and_inside_the_stop(self):
        out = self.run_close(DAY, self.held(), {"AAA": 100.0, "HHH": 100.0})
        self.assertEqual((out["exits"], out["stops_not_checked"], out["warnings"]), ([], [], []))

    def test_an_exit_that_never_filled_is_sent_again(self):
        b = self.held()
        b.fill = lambda o: False
        self.run_close(DAY, b, {"AAA": 80.0, "HHH": 100.0})          # stopped; the exits never fill
        self.assertEqual(self.state("T1"), "closing")
        b.sent, b.fill = [], lambda o: True
        out = self.run_close(NEXT, b, {})
        self.assertEqual([r["trade_id"] for r in out["retried"]], ["T1"])
        self.assertEqual(sorted((o["symbol"], o["action"], o["shares"]) for o in b.sent), [("AAA", "SELL", 25), ("HHH", "BUY", 25)])
        asyncio.run(book.sync(NEXT, b))
        self.assertEqual(self.state("T1"), "closed")

    def test_an_exit_whose_fills_were_missed_is_not_sent_twice(self):
        b = self.held()
        b.fill = lambda o: False
        self.run_close(DAY, b, {"AAA": 80.0, "HHH": 100.0})
        b.sent, b._positions = [], {}                                  # the exits did fill; only the record of them was lost
        out = self.run_close(NEXT, b, {})
        self.assertEqual((b.sent, out["retried"]), ([], []))
        self.assertIn("wasn't sent again", out["warnings"][0])


# ---- The research gate ------------------------------------------------------------------------------------
from datetime import datetime  # noqa: E402

import pandas as pd  # noqa: E402

from factor_rotation.config import NY  # noqa: E402
from factor_rotation import screen  # noqa: E402
from factor_rotation.paper import daily, entries, learn, proposals  # noqa: E402

SUNDAY_EVENING = datetime(2026, 11, 8, 19, 30, tzinfo=NY)
SCREEN = {"as_of": "2026-11-06", "next_session": DAY.isoformat(), "candidates": [
    {"name": "AAA", "kind": "stock", "setup": "breakout", "direction": "long", "hedge": "HHH", "why": "broke out on 2x volume",
     "chart": "uptrend", "numbers": {"atr_pct": 0.02}, "next_earnings": None, "score": 3.1},
    {"name": "BBB", "kind": "stock", "setup": "turn", "direction": "short", "hedge": "HHH", "why": "turned down",
     "chart": "stretched", "numbers": {"atr_pct": 0.04}, "next_earnings": None, "score": 2.4}]}
REASONING = {"what_changed": "a", "story": "b", "chart": "c", "coming": "d", "case_against": "e"}


def verdict(**kw):
    return {"candidate": "AAA", "decision": "take", "conviction": 4, "reason": "guidance raised and the breakout held",
            "reasoning": REASONING, "wrong_if": "closes back under 95", "hedge_why": "its sector fund",
            "plan": {"horizon_days": 4, "stop": {"spread_pct": -5}}, **kw}


def frame(closes: dict[str, list[float]], opens: dict[str, float] | None = None) -> pd.DataFrame:
    """Daily bars from DAY: each symbol opens at 100 (or `opens`) on DAY, then closes as listed, with SPY flat."""
    days = book.journal.sessions_from(DAY, max(len(v) for v in closes.values()))
    rows = []
    for sym, cs in {"SPY": [100.0] * len(days), **closes}.items():
        for d, c in zip(days, cs):
            rows.append({"symbol": sym, "date": pd.Timestamp(d), "open": (opens or {}).get(sym, 100.0) if d == DAY else c, "high": c, "low": c, "close": c, "volume": 1e6})
    return pd.DataFrame(rows)


class Research(Case):
    def setUp(self):
        super().setUp()
        self.write_screen(SCREEN)
        self.reports = {}
        for target, name, value in ((proposals.earnings, "load", lambda: {"names": self.reports}),
                                    (proposals.bars, "load", lambda symbols: self.bars),
                                    (book.journal, "_bars_settled", lambda d: True)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.bars = pd.DataFrame()

    def write_screen(self, payload):
        """The night pass's live screen for DAY (screen.live_path)."""
        path = screen.live_path(DAY, "night")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))

    def propose(self, *verdicts, judge_all=True):
        """Record the night pass for DAY. A candidate the test doesn't judge gets a conviction-1 pass ranked last, since
        a pass must judge every one; verdicts without ranks are ranked in the order given. The order job's marks are
        set at 100 for every leg, the price the bars in `frame` open at, so plan results read as in the bars."""
        verdicts = list(verdicts)
        if judge_all:
            judged = {v.get("candidate") for v in verdicts}
            screen_ = json.loads(screen.live_path(DAY, "night").read_text())
            verdicts += [{"candidate": c["name"], "decision": "pass", "conviction": 1, "reason": "not the test's subject"}
                         for c in screen_["candidates"] if c["name"] not in judged]
        if not any("rank" in v for v in verdicts):
            verdicts = [{**v, "rank": i} for i, v in enumerate(verdicts, 1)]
        verdicts = [{**v, "pass_case": PASS_CASE} if v.get("decision") == "pass" and "pass_case" not in v else v for v in verdicts]
        out = daily.record({"session": DAY.isoformat(), "pass": "night", "proposals": verdicts}, SUNDAY_EVENING)
        legs = {s for p in out[1]["proposals"] for s in [*p["long"], *p["short"]]} | {"SPY"}
        state = entries.load(DAY) or entries._blank(DAY)
        state["marks"]["night"] = {"at": SUNDAY_EVENING.isoformat(), "prices": {s: 100.0 for s in legs}}
        book._write(entries.path(DAY), state)
        return out


class Proposals(Research):
    def test_a_take_keeps_the_screen_numbers_and_a_pass_gets_the_screens_legs_and_plan(self):
        _, doc, _ = self.propose(verdict())
        a, b = doc["proposals"]
        self.assertEqual((a["id"], a["source"], a["signal"]["why"], a["long"], a["short"], a["plan"]),
                         (f"{DAY}-P01", "screen 2026-11-06 #1", "broke out on 2x volume", ["AAA"], ["HHH"], {"horizon_days": 4, "stop": {"spread_pct": -5.0}}))
        self.assertEqual((b["decision"], b["long"], b["short"], b["plan"]),
                         ("pass", ["HHH"], ["BBB"], {"horizon_days": 3, "stop": {"spread_pct": -6.0}}))

    def test_a_name_the_screen_left_out_because_the_book_holds_it_is_refused_with_the_reason(self):
        screen = {**SCREEN, "in_book": [{"name": "HELD", "kind": "stock", "setup": "breakout", "direction": "long", "score": 3.0,
                                         "why": "broke out", "in_book": "the book holds it (2026-11-09-A01)"}]}
        self.write_screen(screen)
        with self.assertRaisesRegex(ValueError, "left out of the screen.*the book holds it"):
            self.propose(verdict(candidate="HELD"))

    def test_the_screens_hedge_fit_is_kept_with_the_verdict(self):
        screen = json.loads(json.dumps(SCREEN))
        screen["candidates"][0].update(hedge_fit={"corr_60d": 0.7, "beta_60d": 1.1, "chosen": "the best match of 24 funds", "weak": False},
                                       moved_with=["CCC"], adjust={"tight_base": 0.15})
        self.write_screen(screen)
        _, doc, _ = self.propose(verdict())
        signal = doc["proposals"][0]["signal"]
        self.assertEqual((signal["hedge_fit"]["corr_60d"], signal["moved_with"], signal["adjust"]), (0.7, ["CCC"], {"tight_base": 0.15}))

    def test_a_pass_needs_only_a_verdict_and_gets_the_mechanical_plan(self):
        _, doc, notes = self.propose(verdict(), {"candidate": "BBB", "decision": "pass", "conviction": 2, "reason": "no story behind the move"})
        self.assertEqual((doc["proposals"][1]["plan"]["horizon_days"], notes), (3, []))

    def test_what_a_take_must_carry(self):
        for bad, message in ((verdict(conviction=2), "conviction 3 or more"), (verdict(reasoning={"story": "x"}), "reasoning"),
                             (verdict(wrong_if=""), "wrong_if"), (verdict(hedge_why=None), "hedge_why"),
                             (verdict(plan={"horizon_days": 11, "stop": {"spread_pct": -5}}), "1-10 sessions"),
                             (verdict(plan={"horizon_days": 4, "stop": {"spread_pct": -9}}), "between -1 and -8"),
                             (verdict(plan={"horizon_days": 4, "stop": {"spread_pct": -5, "rules": [{"symbol": "ZZZ", "below": 5}]}}), "a leg's symbol"),
                             (verdict(candidate="ZZZ"), "not a candidate"), (verdict(short=["AAA"], long=["AAA"]), "both sides"),
                             (verdict(long=["VIX"]), "index")):
            with self.assertRaisesRegex(ValueError, message):
                self.propose(bad)
        self.assertFalse((self.root / "proposals").exists())

    def test_sizing_risks_the_same_share_of_the_book_at_the_stop_and_more_on_conviction(self):
        size = lambda c, stop, **kw: rules.side_dollars(c, stop, 1_000_000, ["AAA"], ["HHH"], **kw)  # noqa: E731
        self.assertEqual([size(3, s)[0] for s in (-8, -5, -3)], [18_700, 30_000, 50_000])       # each loses $1,500 at its stop
        self.assertEqual([size(c, -5)[0] for c in (3, 4, 5)], [30_000, 45_000, 60_000])         # 1x, 1.5x, 2x the risk
        self.assertEqual(size(5, -3), (60_000, {"risk_dollars": 3000, "stop_pct": -3, "limited_by": "side cap"}))
        self.assertEqual(size(3, -5, volumes={"AAA": 8_000_000.0})[1]["limited_by"], "liquidity: AAA")
        self.assertEqual(size(3, -5, volumes={"AAA": 8_000_000.0})[0], 20_000)                  # 0.25% of $8M a day
        # A basket side splits its dollars, so two $8M names carry twice what one does; a $15,000 account scales down.
        self.assertEqual(rules.side_dollars(3, -5, 1_000_000, ["AAA", "BBB"], ["HHH"], {"AAA": 8e6, "BBB": 8e6})[0], 30_000)
        self.assertEqual(rules.side_dollars(3, -5, 15_000, ["AAA"], ["HHH"])[0], 450)   # to the dollar on a small book
        self.assertEqual(rules.side_dollars(3, -5, 25_000, ["AAA"], ["HHH"])[0], 750)

    def test_the_leg_hardest_to_fill_leads_and_the_others_only_follow_its_fills(self):
        t = {"legs": [{"symbol": "ARKK", "side": "long", "shares": 139, "ref_price": 72.0},
                      {"symbol": "RCAT", "side": "short", "shares": 3838, "ref_price": 2.6}], "hedge": None}
        self.assertEqual(rules.lead_leg(t, {"ARKK": 450e6, "RCAT": 27e6}), "RCAT")              # $10,000 is more of $27M a day
        self.assertEqual(rules.lead_leg(t, {"ARKK": 450e6}), "RCAT")                            # no volume on record: the hardest
        self.assertEqual(rules.lead_leg({**t, "hedge": "ARKK"}), "RCAT")                        # nothing known: the name, not its hedge
        t["lead"] = "RCAT"
        due = lambda f, **kw: [(leg["symbol"], n) for leg, n in rules.entry_due(t, f, **kw)]  # noqa: E731
        self.assertEqual(due({"ARKK": 0, "RCAT": 0}), [("RCAT", 3838)])                         # the lead alone at first
        self.assertEqual(due({"ARKK": 0, "RCAT": 100}), [("RCAT", 3738)])                       # 2.6% in: under a tenth, the hedge waits
        self.assertEqual(due({"ARKK": 0, "RCAT": 960}), [("ARKK", 34), ("RCAT", 2878)])         # a quarter in: 34 of 139
        self.assertEqual(due({"ARKK": 34, "RCAT": 3838}), [("ARKK", 105)])                      # the lead done: the hedge completes
        self.assertEqual(due({"ARKK": 0, "RCAT": 3838}, committed={"ARKK": 139}, lead=False), [])   # already on order

    def test_an_outright_take_also_says_why_not_hedged(self):
        with self.assertRaisesRegex(ValueError, "hedge_why"):
            self.propose(verdict(hedge=None, hedge_why=None))
        _, doc, _ = self.propose(verdict(hedge=None, hedge_why="its own story; the hedge would give up the market's move"))
        take = doc["proposals"][0]
        self.assertEqual((take["short"], take["hedge_why"]), ([], "its own story; the hedge would give up the market's move"))

    def test_a_take_through_earnings_must_say_why(self):
        self.reports = {"AAA": [{"date": "2026-11-10", "session": "after_close"}]}     # trades on session 3
        with self.assertRaisesRegex(ValueError, "AAA reports 2026-11-10 after close, which trades on session 3"):
            self.propose(verdict())
        self.propose(verdict(plan={"horizon_days": 2, "stop": {"spread_pct": -5}}))     # out before it
        (self.root / "proposals" / f"{DAY}.json").unlink()
        _, doc, _ = self.propose(verdict(through_earnings="the report is the point"))
        self.assertEqual(doc["proposals"][0]["through_earnings"], "the report is the point")

    def test_the_pass_can_choose_its_own_hedge_and_record_its_own_idea(self):
        own = verdict(candidate=None, name="Refiners", setup="catalyst", direction="long", long=["CCC", "DDD", "EEE"], short=["XLE"])
        _, doc, _ = self.propose(verdict(hedge="XLY"), own, {"candidate": "BBB", "decision": "take", "conviction": 3, "reason": "r", "reasoning": REASONING,
                                                             "wrong_if": "w", "hedge": None, "hedge_why": "its own story",
                                                             "plan": {"horizon_days": 2, "stop": {"spread_pct": -4}}})
        self.assertEqual([(p["name"], p["source"], p["long"], p["short"]) for p in doc["proposals"]],
                         [("AAA", "screen 2026-11-06 #1", ["AAA"], ["XLY"]), ("Refiners", "own", ["CCC", "DDD", "EEE"], ["XLE"]),
                          ("BBB", "screen 2026-11-06 #2", [], ["BBB"])])

    def test_the_evening_pass_ranks_every_verdict_and_the_rank_agrees_with_conviction(self):
        low = {"candidate": "BBB", "decision": "pass", "conviction": 2, "reason": "no story behind the move"}
        for bad in ([verdict(), low], [verdict(rank=1), low], [verdict(rank=1), {**low, "rank": 1}], [verdict(rank=1), {**low, "rank": 3}]):
            with self.assertRaisesRegex(ValueError, "rank every verdict in the draft from 1 .* to 2"):
                daily.record({"session": DAY.isoformat(), "pass": "night", "proposals": bad}, SUNDAY_EVENING)
        with self.assertRaisesRegex(ValueError, "AAA is ranked 2 with conviction 4, below BBB at rank 1 with conviction 2"):
            self.propose(verdict(rank=2), {**low, "rank": 1})
        self.assertFalse((self.root / "proposals").exists())
        _, doc, _ = self.propose(verdict(rank=1), {**low, "rank": 2})
        self.assertEqual([(p["name"], p["rank"], p["ranked"]) for p in doc["proposals"]], [("AAA", 1, 2), ("BBB", 2, 2)])

    def test_within_a_conviction_the_better_ranked_take_is_traded_first(self):
        second = take("P01", "AAA", conviction=3)
        first = take("P02", "BBB", long=("BBB",), short=("SPY",), conviction=3)
        strongest = take("P03", "CCC", long=("CCC",), short=("QQQ",), conviction=4)
        ranked = [{**second, "rank": 3}, {**first, "rank": 2}, {**strongest, "rank": 1}]
        prices = {s: 100.0 for s in ("AAA", "BBB", "CCC", "HHH", "SPY", "QQQ")}
        trades, _ = rules.proposal_trades(ranked, str(DAY), prices, set())
        self.assertEqual([t["name"] for t in trades], ["CCC", "BBB", "AAA"])

class Review(Research):
    """One automatic trade, long 100 AAA / short 100 HHH from 100, opened on DAY with a 2-session limit."""

    def setUp(self):
        super().setUp()
        t = {**pair("T1", horizon=2), "notional": 10_000, "proposal_id": f"{DAY}-P01", "conviction": 4, "wrong_if": "x"}
        t["legs"] = [{**leg, "shares": 100} for leg in t["legs"]]
        self.write("entries", DAY, {"trades": [t], "orders": [{"status": "PreSubmitted"}]})
        self.broker = Broker(fills=[fill("T1", "AAA", "BOT", 100), fill("T1", "HHH", "SLD", 100)], positions={"AAA": 100, "HHH": -100})
        asyncio.run(book.sync(DAY, self.broker))

    def review(self, session, **r):
        """A pass's review of the trade, as daily.record leaves it for the close job (journal/paper/pass_reviews/)."""
        self.write("pass_reviews", session, {"passes": [{"pass": "midday", "at": f"{session}T13:00:00-05:00",
                                                         "reviews": [{"trade_id": "T1", "thesis": "intact", "reason": "why", **r}]}]})

    def close(self, day, prices):
        with mock.patch.object(book, "quotes", lambda symbols, *a: {s: prices[s] for s in symbols if s in prices}):
            return asyncio.run(book.close_session(day, broker=self.broker))

    def test_an_early_exit_closes_the_trade_at_the_close(self):
        self.review(DAY, action="exit", thesis="broken", reason="guidance was withdrawn at noon")
        out = self.close(DAY, {"AAA": 99.0, "HHH": 100.0})
        self.assertEqual(out["exits"][0]["reason"], "review: guidance was withdrawn at noon")
        self.assertEqual(sorted((o["symbol"], o["action"], o["kind"]) for o in self.broker.sent), [("AAA", "SELL", "MOC"), ("HHH", "BUY", "MOC")])

    def test_a_hold_changes_nothing_and_a_stop_still_fires(self):
        self.review(DAY, action="hold")
        out = self.close(DAY, {"AAA": 94.0, "HHH": 100.0})
        self.assertIn("stopped: down 6.0%", out["exits"][0]["reason"])

    def test_a_winner_on_its_last_session_is_extended_once(self):
        self.close(DAY, {"AAA": 101.0, "HHH": 100.0})
        self.review(NEXT, action="extend", sessions=2, reason="the second leg of the story lands Thursday")
        out = self.close(NEXT, {"AAA": 103.0, "HHH": 100.0})
        self.assertEqual((out["exits"], out["extended"]), ([], [{"trade_id": "T1", "sessions": 2, "reason": "the second leg of the story lands Thursday"}]))
        t = book.open_trades()[0]
        self.assertEqual((t["horizon_days"], t["planned_horizon_days"], t["extended"]), (4, 2, True))
        third, fourth = book.journal.sessions_from(DAY, 4)[2:]
        self.assertEqual(self.close(third, {"AAA": 103.0, "HHH": 100.0})["exits"], [])
        self.review(fourth, action="extend", sessions=1)                         # a second extension is refused at the close
        self.assertIn("time limit: session 4 of 4; the extension asked for was refused: it was extended before",
                      self.close(fourth, {"AAA": 103.0, "HHH": 100.0})["exits"][0]["reason"])

    def test_a_loser_is_not_extended(self):
        self.close(DAY, {"AAA": 100.0, "HHH": 100.0})
        self.review(NEXT, action="extend", sessions=2)
        out = self.close(NEXT, {"AAA": 99.0, "HHH": 100.0})
        self.assertEqual(out["extended"], [])
        self.assertIn("time limit: session 2 of 2; the extension asked for was refused: it isn't in profit", out["exits"][0]["reason"])


class Learn(Research):
    def test_takes_and_passes_are_scored_the_same_way_and_set_side_by_side(self):
        self.propose(verdict(plan={"horizon_days": 2, "stop": {"spread_pct": -5}}),
                     {"candidate": "BBB", "decision": "pass", "conviction": 2, "reason": "no story"})
        self.bars = frame({"AAA": [101.0, 103.0, 110.0], "BBB": [102.0, 108.0, 90.0], "HHH": [100.0, 100.0, 100.0]})
        rows = {r["name"]: r for r in learn.outcomes()}
        self.assertEqual((rows["AAA"]["verdict"], rows["AAA"]["plan_status"], rows["AAA"]["plan_bps"]), ("take", "time", 300.0))
        # the pass, as the screen would have traded it: long HHH / short BBB, stopped at the second close, down 8%
        self.assertEqual((rows["BBB"]["verdict"], rows["BBB"]["plan_status"], rows["BBB"]["plan_bps"]), ("pass", "stopped", -800.0))
        t = learn.tables()
        self.assertEqual((t["by_verdict"]["take"], t["by_verdict"]["pass"]["mean"], t["by_conviction"][4]["n"]),
                         ({"n": 1, "mean": 300.0, "median": 300.0, "winners": 1.0}, -800.0, 1))
        self.assertIn("take", learn.brief())

    def test_the_ranking_is_scored_against_the_plan_results_night_by_night(self):
        names = ["AAA", "BBB", "CCC", "DDD"]
        screen = {**SCREEN, "candidates": [{**SCREEN["candidates"][0], "name": n} for n in names]}
        self.write_screen(screen)
        plan = {"horizon_days": 2, "stop": {"spread_pct": -8}}
        self.propose(verdict(plan=plan), *[{"candidate": n, "decision": "pass", "conviction": 2, "reason": "no story", "plan": plan} for n in names[1:]])
        self.assertEqual(learn.rank_table()["nights"], 0)                    # nothing has finished
        # ranked AAA, BBB, CCC, DDD and they finish +6%, +4%, -2%, -3%: the ranking was exactly right
        self.bars = frame({"AAA": [103.0, 106.0], "BBB": [102.0, 104.0], "CCC": [99.0, 98.0], "DDD": [99.0, 97.0], "HHH": [100.0, 100.0]})
        t = learn.rank_table()
        self.assertEqual((t["ranked"], t["finished"], t["nights"], t["ic"], t["t"]), (4, 4, 1, 1.0, None))
        self.assertEqual([t["thirds"][k]["mean"] for k in ("top", "middle", "bottom")], [500.0, -200.0, -300.0])
        self.assertIn("rank correlation +1.00 on average over 1 night", learn.brief())
        # the same results with the ranking upside down
        self.assertEqual(learn._spearman([-4, -3, -2, -1], [600.0, 400.0, -200.0, -300.0]), -1.0)
        self.assertIsNone(learn._spearman([1, 2, 3], [5.0, 5.0, 5.0]))

    def test_an_unfinished_plan_has_no_result_yet(self):
        self.propose(verdict())
        self.bars = frame({"AAA": [101.0], "HHH": [100.0]})
        (row, _) = learn.outcomes()
        self.assertEqual((row["plan_status"], row["plan_bps"], row["plan_so_far_bps"]), ("open", None, 100.0))
        self.assertEqual(learn.tables()["finished"], 0)

    def test_early_stop_waits_for_its_time_limit_before_entering_settled_tables(self):
        names = ["AAA", "BBB", "CCC", "DDD"]
        screen = {**SCREEN, "candidates": [{**SCREEN["candidates"][0], "name": n} for n in names]}
        self.write_screen(screen)
        plan = {"horizon_days": 3, "stop": {"spread_pct": -5}}
        self.propose(verdict(plan=plan), *[{"candidate": n, "decision": "pass", "conviction": 2, "reason": "no story", "plan": plan} for n in names[1:]])
        # After 1 day of a 3-day plan, BBB stops out at -7% while AAA/CCC/DDD are still open and up.
        self.bars = frame({"AAA": [104.0], "BBB": [93.0], "CCC": [102.0], "DDD": [101.0], "HHH": [100.0]})
        rows = {r["name"]: r for r in learn.outcomes()}
        self.assertEqual((rows["BBB"]["plan_status"], rows["BBB"]["plan_bps"], rows["BBB"]["plan_mature"]), ("stopped", -700.0, False))
        t = learn.tables()
        # Settled tables wait for day 3 so BBB doesn't bias passes alone; so_far includes all four right now.
        self.assertEqual((t["finished"], t["early_stopped"], t["by_verdict"]), (0, 1, {}))
        self.assertEqual((t["so_far"]["n"], t["so_far"]["by_verdict"]["take"]["mean"], t["so_far"]["by_verdict"]["pass"]["mean"]),
                         (4, 400.0, round((-700.0 + 200.0 + 100.0) / 3, 1)))
        self.assertEqual((t["rank"]["nights"], t["rank"]["so_far"]["nights"]), (0, 1))
        # Once day 3 settles, all four enter settled tables and settled ranking together.
        self.bars = frame({"AAA": [104.0, 105.0, 106.0], "BBB": [93.0, 105.0, 105.0], "CCC": [102.0, 103.0, 104.0], "DDD": [101.0, 100.0, 99.0], "HHH": [100.0] * 3})
        t = learn.tables()
        self.assertEqual((t["finished"], t["early_stopped"]), (4, 0))
        self.assertEqual((t["by_verdict"]["take"]["mean"], t["by_verdict"]["pass"]["mean"], t["rank"]["nights"]),
                         (600.0, round((-700.0 + 400.0 - 100.0) / 3, 1), 1))

    def test_unsettled_live_bar_marks_open_plan_so_far_without_finishing_it(self):
        self.propose(verdict(plan={"horizon_days": 1, "stop": {"spread_pct": -5}}))
        self.bars = frame({"AAA": [103.0], "HHH": [100.0]})
        with mock.patch.object(book.journal, "_bars_settled", lambda d: False):
            (row, _) = learn.outcomes()
        self.assertEqual((row["plan_status"], row["plan_bps"], row["plan_mature"], row["plan_so_far_bps"]), ("open", None, False, 300.0))

    def test_a_post_mortem_tag_has_to_agree_with_the_result(self):
        t = {**pair("T1", horizon=1), "proposal_id": f"{DAY}-P01"}
        self.write("entries", DAY, {"trades": [t], "orders": [{"status": "PreSubmitted"}]})
        b = Broker(fills=[fill("T1", "AAA", "BOT", 25), fill("T1", "HHH", "SLD", 25)], positions={"AAA": 25, "HHH": -25})
        with mock.patch.object(book, "quotes", lambda symbols, *a: {"AAA": 100.0, "HHH": 100.0}):
            asyncio.run(book.close_session(DAY, broker=b))                       # the time limit; exits fill at 100, so it loses its commissions
        asyncio.run(book.sync(DAY, b))
        self.assertEqual([r["id"] for r in learn.needs_postmortem()], ["T1"])
        m = {"trade_id": "T1", "what_happened": "went nowhere", "lesson": "a breakout with no follow-through by the first close is done"}
        with self.assertRaisesRegex(ValueError, "lost 16 bp, so its tag is one of: thesis_broke, noise"):
            learn.record_postmortems({"postmortems": [{**m, "tag": "right_reason"}]})
        learn.record_postmortems({"postmortems": [{**m, "tag": "noise"}]})
        self.assertEqual((learn.needs_postmortem(), learn.postmortems()["T1"]["tag"]), ([], "noise"))
        with self.assertRaisesRegex(ValueError, "already has a post-mortem"):
            learn.record_postmortems({"postmortems": [{**m, "tag": "noise"}]})


class Pnl(Case):
    def setUp(self):
        super().setUp()
        self.write("entries", DAY, {"trades": [pair("T1")]})
        self.bars = frame({"AAA": [104.0, 106.0], "HHH": [101.0, 102.0]})
        for target, name, value in ((book.bars, "load", lambda symbols: self.bars), (book.journal, "_bars_settled", lambda d: True)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def fills(self, session, *rows):
        """Each row is (symbol, side, price, UTC time or None)."""
        made = [{**fill("T1", s, side, 25, price=px, n=f"{session}{i}"), "time": at} for i, (s, side, px, at) in enumerate(rows)]
        self.write("fills", session, {"fills": made, "positions": {}, "net_liquidation": 1e6})

    def enter(self):
        self.fills(DAY, ("AAA", "BOT", 100.0, "2026-11-09T14:30:02+00:00"), ("HHH", "SLD", 100.0, "2026-11-09T14:30:02+00:00"))

    def test_nothing_filled_is_no_line(self):
        self.assertEqual(book.pnl_by_session(), [])
        self.assertEqual(book.pnl_intraday(), {"points": [], "missing": []})

    def test_starts_from_zero_and_marks_open_positions_at_each_close_after_commissions(self):
        self.enter()
        rows = book.pnl_by_session()
        self.assertEqual([r["date"] for r in rows], ["2026-11-06", "2026-11-09", "2026-11-10"])
        # long +4 and short -1 on 25 shares, less $2 of commissions; then long +6 and short -2
        self.assertEqual([r["pnl"] for r in rows], [0.0, 73.0, 98.0])

    def test_the_last_point_ties_to_the_trade_table(self):
        self.enter()
        self.assertEqual(book.pnl_by_session()[-1]["pnl"], sum(r["pnl"] for r in book.rows()))

    def test_a_closed_trade_stays_at_its_realized_result(self):
        self.enter()
        self.fills(NEXT, ("AAA", "SLD", 105.0, "2026-11-10T21:00:01+00:00"), ("HHH", "BOT", 102.0, "2026-11-10T21:00:01+00:00"))
        self.bars = frame({"AAA": [104.0, 106.0, 90.0], "HHH": [101.0, 102.0, 130.0]})
        days = book.journal.sessions_from(DAY, 3)
        rows = {r["date"]: r["pnl"] for r in book.pnl_by_session()}
        # +5 and -2 on 25 shares, less $4 of commissions; the later bars don't touch it
        self.assertEqual(rows[NEXT.isoformat()], 71.0)
        if days[2] <= datetime.now(NY).date():
            self.assertEqual(rows[days[2].isoformat()], 71.0)

    def test_a_position_entered_after_the_last_bar_sits_at_its_fill(self):
        self.bars = frame({"AAA": [104.0], "HHH": [101.0]})
        self.fills(NEXT, ("AAA", "BOT", 100.0, "2026-11-10T14:30:02+00:00"), ("HHH", "SLD", 100.0, "2026-11-10T14:30:02+00:00"))
        self.write("entries", DAY, {"trades": [pair("T1", entry=NEXT)]})
        rows = book.pnl_by_session()
        self.assertEqual((rows[-1]["date"], rows[-1]["pnl"]), (NEXT.isoformat(), -2.0))
        self.assertEqual(book.rows()[0]["pnl"], -2.0)

    def bar_series(self, values):
        """15-minute closes from 3:30 PM ET on DAY, one bar each."""
        idx = pd.date_range("2026-11-09 15:30", periods=len(values), freq="15min", tz=NY)
        return pd.Series(values, index=idx)

    def test_intraday_marks_each_bar_and_counts_the_closing_auction_in_the_last_one(self):
        self.enter()
        self.fills(NEXT, ("AAA", "SLD", 110.0, "2026-11-09T21:00:03+00:00"), ("HHH", "BOT", 100.0, "2026-11-09T21:00:03+00:00"))
        closes = {"SPY": self.bar_series([1.0, 1.0]), "AAA": self.bar_series([103.0, 109.0]), "HHH": self.bar_series([100.0, 100.0])}
        with mock.patch.object(book.bars, "intraday_closes", lambda symbols, sessions: closes):
            out = book.pnl_intraday()
        self.assertEqual(out["missing"], [])
        self.assertEqual([p["pnl"] for p in out["points"]], [73.0, 246.0])
        self.assertEqual(out["points"][0]["session"], "2026-11-09")

    def test_a_name_with_no_bars_is_reported_and_marked_at_its_fill(self):
        self.enter()
        closes = {"SPY": self.bar_series([1.0]), "AAA": self.bar_series([103.0])}
        with mock.patch.object(book.bars, "intraday_closes", lambda symbols, sessions: closes):
            out = book.pnl_intraday()
        self.assertEqual((out["missing"], out["points"][0]["pnl"]), (["HHH"], 73.0))


if __name__ == "__main__":
    unittest.main()


class SwingFillsOnly(unittest.TestCase):
    def test_the_intraday_books_fills_are_not_the_swing_books(self):
        from unittest import mock
        from factor_rotation.paper import book
        f = lambda ref: {"order_ref": ref, "symbol": ref.split("|")[1], "side": "SLD", "shares": 1.0, "price": 1.0,
                         "time": "2026-11-12T03:00:00+00:00", "exec_id": ref, "session": "2026-11-12"}
        by_ref = {("X-1111-2201-LIN", "LIN"): [f("X-1111-2201-LIN|LIN")], ("I-1112-AMD", "AMD"): [f("I-1112-AMD|AMD")],
                  ("2026-11-12-A02", "MU"): [f("2026-11-12-A02|MU")],
                  ("2026-11-16-A02", "CTVA"): [f("2026-11-16-A02|CTVA")]}   # another book's trade: the first book's, after a restart
        with mock.patch.object(book, "_fills_by_ref", lambda: by_ref), \
                mock.patch.object(book, "recorded", lambda: [{"id": "2026-11-12-A02"}]):
            self.assertEqual([x["symbol"] for x in book._book_fills()], ["MU"])




class PassCaseTests(Research):
    def test_a_pass_needs_its_short_case(self):
        with self.assertRaises(ValueError) as e:
            self.propose(verdict(), {"candidate": "BBB", "decision": "pass", "conviction": 2, "reason": "no story", "pass_case": {"case_for": "x"}})
        self.assertIn("pass_case", str(e.exception))
        _, doc, _ = self.propose(verdict())
        p = next(p for p in doc["proposals"] if p["decision"] == "pass")
        self.assertEqual(p["pass_case"], PASS_CASE)

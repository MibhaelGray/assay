"""The three daily passes: the live prices, the live screen's extra setups, recording a pass, the order job
that trades it, and how its verdicts are scored. A stand-in broker and a temporary journal; no network.

    uv run python -m unittest discover -s tests
"""

import asyncio
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from factor_rotation import live, screen
from factor_rotation.config import NY
from factor_rotation.paper import book, daily, early, entries, learn, proposals
from test_paper_book import DAY, NEXT, Broker, Case, fill, frame, take

SUNDAY_NIGHT = datetime(2026, 11, 8, 20, 45, tzinfo=NY)     # the night before DAY, a Monday: the overnight session
MORNING = datetime(2026, 11, 9, 11, 0, tzinfo=NY)           # 10:00 AM CT
MIDDAY = datetime(2026, 11, 9, 14, 15, tzinfo=NY)           # 1:15 PM CT


def five_minute(day: str, rows: list[tuple[str, float, float]]) -> pd.DataFrame:
    """5-minute bars: (New York time, close, volume), each bar flat at its close."""
    idx = pd.DatetimeIndex([pd.Timestamp(f"{day} {t}", tz=NY) for t, _, _ in rows])
    c = [r[1] for r in rows]
    return pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": [r[2] for r in rows]}, index=idx)


class Live(unittest.TestCase):
    def test_a_session_still_trading(self):
        s = live.snapshot(five_minute("2026-11-09", [("08:00", 99.0, 0), ("09:30", 100.0, 500), ("09:35", 102.0, 300), ("09:40", 101.0, 200)]))
        self.assertEqual((s["date"], s["phase"], s["price"]), (date(2026, 11, 9), "regular", 101.0))
        self.assertEqual(s["regular"], {"open": 100.0, "high": 102.0, "low": 100.0, "close": 101.0, "volume": 1000.0, "minutes": 15, "fraction": 0.07})
        self.assertEqual(s["pre"], {"last": 99.0, "bars": 1})
        bar = live.provisional_bar(s)
        self.assertAlmostEqual(bar["volume"], 1000 / 0.07)          # fifteen minutes' volume as a full day's worth
        self.assertEqual((bar["open"], bar["close"]), (100.0, 101.0))

    def test_after_hours_counts_bars_because_yahoo_gives_no_volume_there(self):
        s = live.snapshot(five_minute("2026-11-09", [("15:55", 100.0, 900), ("16:00", 97.0, 0), ("16:05", 96.0, 0), ("18:00", 95.0, 0)]))
        self.assertEqual((s["phase"], s["price"], s["post"]), ("post", 95.0, {"last": 95.0, "bars": 3}))
        self.assertEqual(s["regular"]["minutes"], 390)
        self.assertEqual(s["at"], "2026-11-09T18:05:00-05:00")

    def test_only_the_latest_day_counts_and_before_the_open_there_is_no_bar(self):
        df = pd.concat([five_minute("2026-11-06", [("15:55", 90.0, 100)]), five_minute("2026-11-09", [("08:00", 99.0, 0)])])
        s = live.snapshot(df)
        self.assertEqual((s["date"], s["phase"], s["regular"]), (date(2026, 11, 9), "pre", None))
        self.assertIsNone(live.provisional_bar(s))

    def test_the_volume_curve_runs_from_nothing_to_the_whole_day(self):
        self.assertEqual((live.volume_fraction(0), live.volume_fraction(390), live.volume_fraction(999)), (0.0, 1.0, 1.0))
        self.assertAlmostEqual(live.volume_fraction(45), 0.16)
        fr = [live.volume_fraction(m) for m in range(0, 391, 5)]
        self.assertEqual(fr, sorted(fr))


def daily_bars(n=300, last=None, volume=None):
    closes = np.full(n, 100.0)
    closes[::2] += 0.5                      # a quiet, flat name with an ATR of about 2%
    if last is not None:
        closes[-1] = last
    dates = pd.bdate_range(end="2026-11-09", periods=n)
    vol = np.full(n, 1e6)
    if volume is not None:
        vol[-1] = volume
    return pd.DataFrame({"date": dates, "symbol": "TEST", "open": closes, "high": closes * 1.01, "low": closes * 0.99, "close": closes, "volume": vol})


class LiveScreen(unittest.TestCase):
    def hits(self, f):
        return screen._symbol_hits("TEST", f, "SPY", pd.Series(dtype=float), 0.0, {}, None)

    def after_hours(self, last, bars):
        f = screen._features(daily_bars())
        snap = {"date": date(2026, 11, 9), "phase": "post", "price": last, "at": "2026-11-09T19:55:00-05:00",
                "regular": {"close": f["close"]}, "post": {"last": last, "bars": bars}, "pre": None}
        f["live"] = screen._live_of("TEST", f, snap, {"TEST": [{"date": "2026-11-09", "session": "after_close", "surprise_pct": -12.0}]})
        return f

    def test_an_after_hours_move_on_steady_trading_is_a_candidate(self):
        f = self.after_hours(94.0, bars=30)
        hit = next(h for h in self.hits(f) if h["setup"] == "live_move")
        self.assertEqual(hit["direction"], "short")
        self.assertIn("after-hours at 6:55 PM CT: 94.00", hit["why"])
        self.assertIn("reported today after close, EPS surprise -12%", hit["why"])
        self.assertAlmostEqual(f["live"]["move"], 94.0 / f["close"] - 1)

    def test_a_move_on_a_few_prints_is_not(self):
        self.assertNotIn("live_move", [h["setup"] for h in self.hits(self.after_hours(94.0, bars=3))])

    def test_a_small_move_is_not(self):
        self.assertNotIn("live_move", [h["setup"] for h in self.hits(self.after_hours(99.5, bars=40))])

    def test_a_session_still_trading_needs_heavy_volume_and_says_trading_not_closed(self):
        f = screen._features(daily_bars(last=107.0, volume=3e6))
        f["partial"] = {"minutes": 210, "fraction": 0.48}
        snap = {"date": date(2026, 11, 9), "phase": "regular", "price": 107.0, "at": "2026-11-09T13:00:00-05:00",
                "regular": {"close": 107.0}, "post": None, "pre": None}
        f["live"] = screen._live_of("TEST", f, snap, {})
        found = {h["setup"]: h for h in self.hits(f)}
        self.assertIn("projected to a full day", found["live_move"]["why"])
        self.assertIn("trading at 107.00, above the 52-week closing high", found["breakout"]["why"])
        quiet = screen._features(daily_bars(last=107.0, volume=1e6))
        quiet["live"] = screen._live_of("TEST", quiet, snap, {})
        self.assertNotIn("live_move", [h["setup"] for h in self.hits(quiet)])

    def test_a_name_judged_lately_comes_back_only_if_it_has_moved(self):
        was = {"session": "2026-11-09", "pass": "night", "decision": "pass", "setup": "turn", "direction": "long", "price": 100.0, "atr_pct": 0.02}
        cand = lambda price: {"name": "TEST", "kind": "stock", "setup": "turn", "direction": "long", "numbers": {"price": price}}  # noqa: E731
        self.assertIn("moved +0.5 ATR since", screen._judged_already(cand(101.0), was))
        self.assertIsNone(screen._judged_already(cand(96.0), was))
        basket = {"name": "Refiners", "kind": "basket", "setup": "turn", "direction": "long", "numbers": {}}
        self.assertIn("same setup", screen._judged_already(basket, was))
        self.assertIsNone(screen._judged_already({**basket, "direction": "short"}, was))


class Sessions(unittest.TestCase):
    def test_which_session_a_pass_belongs_to(self):
        at = lambda d, h, m: datetime(2026, 11, d, h, m, tzinfo=NY)  # noqa: E731
        self.assertEqual(proposals.pass_session("night", at(8, 20, 30)), DAY)        # Sunday night, for Monday
        self.assertEqual(proposals.pass_session("night", at(9, 20, 30)), NEXT)       # Monday night, for Tuesday
        self.assertEqual(proposals.pass_session("night", at(10, 1, 0)), NEXT)        # past midnight, still Tuesday's
        self.assertEqual(proposals.pass_session("night", at(13, 20, 30)), date(2026, 11, 16))   # Friday night, for Monday
        self.assertEqual(proposals.pass_session("morning", at(9, 9, 45)), DAY)
        self.assertEqual(proposals.pass_session("midday", at(9, 13, 0)), DAY)
        for kind, when in (("night", at(9, 11, 0)), ("morning", at(9, 8, 0)), ("morning", at(8, 10, 0)), ("midday", at(9, 15, 45))):
            with self.assertRaises(ValueError):
                proposals.pass_session(kind, when)

    def test_where_an_order_can_trade(self):
        at = lambda d, h, m: datetime(2026, 11, d, h, m, tzinfo=NY)  # noqa: E731
        self.assertEqual(entries.venue(at(9, 11, 0)), "regular")
        self.assertIsNone(entries.venue(at(9, 15, 45)))                # the last 20 minutes are the close job's
        self.assertEqual(entries.venue(at(9, 21, 0)), "overnight")
        self.assertEqual(entries.venue(at(10, 5, 0)), "extended")
        self.assertIsNone(entries.venue(at(10, 9, 20)))
        self.assertEqual((entries.entry_session(at(9, 15, 59)), entries.entry_session(at(9, 16, 0)), entries.entry_session(at(8, 21, 0))),
                         (DAY, NEXT, DAY))
        self.assertEqual((entries.first_session("night", DAY), entries.first_session("midday", DAY)), (DAY, NEXT))


def candidate(name, hedge="HHH", setup="live_move", direction="long"):
    return {"name": name, "kind": "stock", "setup": setup, "direction": direction, "strength": 2.5, "why": f"{name} is moving",
            "hedge": hedge, "hedge_fit": None, "score": 3.0, "chart": "", "numbers": {"price": 50.0, "atr_pct": 0.02}, "also": []}


def verdict(name, rank, decision="take", conviction=3, **kw):
    v = {"candidate": name, "rank": rank, "decision": decision, "conviction": conviction, "reason": "the reason", **kw}
    if decision == "take":
        v = {"reasoning": {k: "x" for k in ("what_changed", "story", "chart", "coming", "case_against")}, "wrong_if": "it gives the move back",
             "hedge_why": "its sector", "plan": {"horizon_days": 3, "stop": {"spread_pct": -5}}, **v}
    return v


class Pass(Case):
    """A journal with a live screen for a pass, Yahoo standing in at fixed prices, and a gateway that quotes."""

    def setUp(self):
        super().setUp()
        self.reports, self.bars, self.prices = {}, pd.DataFrame(), {"AAA": 50.0, "BBB": 20.0, "HHH": 100.0, "SPY": 500.0}
        for target, name, value in ((proposals.earnings, "load", lambda: {"names": self.reports}),
                                    (proposals.bars, "load", lambda symbols: self.bars),
                                    (book.journal, "_bars_settled", lambda d: True),
                                    (book, "PaperSettings", lambda: mock.Mock(send=True)),
                                    (entries.drawdown, "paused", lambda b: None)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def screen(self, kind, *names, session=DAY):
        path = screen.live_path(session, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"as_of": "2026-11-06", "session": session.isoformat(), "slot": kind, "live": True, "phase": "post",
                                    "prices_at": "2026-11-06T19:55:00-05:00", "candidates": [candidate(n) for n in names], "in_book": []}))

    def record(self, kind, now, *verdicts, reviews=()):
        return daily.record({"session": DAY.isoformat(), "pass": kind, "proposals": list(verdicts), "reviews": list(reviews)}, now)

    def enter(self, now, broker, books=None, missing=None):
        books = books if books is not None else {s: (p - 0.05, p + 0.05) for s, p in self.prices.items()}

        async def gateway(symbols, overnight):
            return {s: books[s] for s in symbols if s in books}, {s: w for s, w in (missing or {}).items() if s in symbols}
        return asyncio.run(entries.run(now, broker, quotes=gateway, prices=lambda symbols: {s: self.prices[s] for s in symbols if s in self.prices}))


class Record(Pass):
    def test_a_pass_records_its_verdicts_once_with_every_candidate_ranked(self):
        self.screen("night", "AAA", "BBB")
        path, doc, notes = self.record("night", SUNDAY_NIGHT, verdict("AAA", 1), verdict("BBB", 2, "pass", 2))
        self.assertEqual([(p["id"], p["pass"], p["decision"], p["rank"], p["source"]) for p in doc["proposals"]],
                         [(f"{DAY}-P01", "night", "take", 1, f"live screen {DAY} night #1"), (f"{DAY}-P02", "night", "pass", 2, f"live screen {DAY} night #2")])
        self.assertEqual((doc["passes"][0]["pass"], doc["passes"][0]["takes"]), ("night", 1))
        with self.assertRaisesRegex(ValueError, "already recorded"):
            self.record("night", SUNDAY_NIGHT, verdict("AAA", 1))

    def test_it_needs_its_own_live_screen_and_the_right_session(self):
        with self.assertRaisesRegex(FileNotFoundError, "fr screen --live morning"):
            self.record("morning", MORNING, verdict("AAA", 1))
        self.screen("night", "AAA")
        with self.assertRaisesRegex(ValueError, "is for session"):
            daily.record({"session": NEXT.isoformat(), "pass": "night", "proposals": [verdict("AAA", 1)]}, SUNDAY_NIGHT)
        with self.assertRaisesRegex(ValueError, "rank every verdict"):
            self.record("night", SUNDAY_NIGHT, {**verdict("AAA", 1), "rank": None})

    def test_a_candidate_it_skipped_is_recorded_as_not_reviewed(self):
        self.screen("night", "AAA", "BBB")
        _, doc, notes = self.record("night", SUNDAY_NIGHT, verdict("AAA", 1))
        self.assertEqual([(p["name"], p["reviewed"]) for p in doc["proposals"]], [("AAA", True), ("BBB", False)])
        self.assertIn("BBB (screen #2) had no verdict", notes[0])

    def test_the_next_pass_adds_to_the_same_session_and_must_review_what_is_open(self):
        self.screen("night", "AAA")
        self.record("night", SUNDAY_NIGHT, verdict("AAA", 1))
        self.enter(SUNDAY_NIGHT, Broker())
        self.screen("morning", "BBB")
        with self.assertRaisesRegex(ValueError, f"missing: {DAY}-S01"):
            self.record("morning", MORNING, verdict("BBB", 1, "pass", 2))
        hold = {"trade_id": f"{DAY}-S01", "action": "hold", "thesis": "intact", "reason": "nothing has changed"}
        _, doc, _ = self.record("morning", MORNING, verdict("BBB", 1, "pass", 2), reviews=[hold])
        self.assertEqual([(p["id"], p["pass"]) for p in doc["proposals"]], [(f"{DAY}-P01", "night"), (f"{DAY}-P02", "morning")])
        self.assertEqual(proposals.reviews(DAY)[f"{DAY}-S01"]["pass"], "morning")


class Enter(Pass):
    def night_take(self, *verdicts):
        self.screen("night", *[v["candidate"] for v in verdicts])
        self.record("night", SUNDAY_NIGHT, *verdicts)

    def test_a_take_is_traded_as_soon_as_the_pass_records_at_the_gateways_bid_and_ask(self):
        self.night_take(verdict("AAA", 1), verdict("BBB", 2, "pass", 2))
        b = Broker()
        r = self.enter(SUNDAY_NIGHT, b)
        self.assertEqual((r["venue"], r["passes"], r["new"]), ("overnight", ["night"], [f"{DAY}-S01"]))
        self.assertEqual([(o["symbol"], o["action"], o["shares"], o["kind"], o["ref_price"], o["limit"], o["venue"]) for o in b.sent],
                         [("AAA", "BUY", 200, "LMT", 50.05, 50.3, "overnight"), ("HHH", "SELL", 100, "LMT", 99.95, 99.45, "overnight")])
        saved = entries.load(DAY)
        t = saved["trades"][0]
        self.assertEqual((t["pass"], t["first_session"], t["proposal_id"]), ("night", DAY.isoformat(), f"{DAY}-P01"))
        self.assertEqual(saved["marks"]["night"]["prices"], {"AAA": 50.0, "BBB": 20.0, "HHH": 100.0, "SPY": 500.0})   # the pass too
        self.assertEqual(self.state(f"{DAY}-S01"), "open")
        self.assertEqual(self.enter(SUNDAY_NIGHT.replace(minute=46), b)["note"], "nothing new")
        self.assertEqual(len(b.sent), 2)

    def test_no_order_goes_out_without_a_quote_in_the_regular_session_either(self):
        self.screen("morning", "AAA")
        self.record("morning", MORNING, verdict("AAA", 1))
        b = Broker()
        r = self.enter(MORNING, b, books={}, missing={"AAA": "the account has no market data subscription for it over the API (IBKR error 10089)"})
        self.assertEqual((r["venue"], r["new"], b.sent), ("regular", [f"{DAY}-S01"], []))
        self.assertIn("IBKR error 10089", r["warnings"][0])
        self.assertIn("the whole trade waits", r["warnings"][0])
        self.assertEqual(self.enter(MORNING.replace(minute=5), b)["note"], "waiting: the next try is 15 minutes after the last")
        r = self.enter(MORNING.replace(minute=20), b)                     # quotes are back a quarter-hour later
        self.assertEqual([(o["symbol"], o["venue"]) for o in r["orders"]], [("AAA", "regular"), ("HHH", "regular")])
        self.assertEqual(entries.load(DAY)["trades"][0]["first_session"], NEXT.isoformat())

    def test_a_wide_spread_holds_the_whole_trade_until_part_of_it_has_filled(self):
        self.night_take(verdict("AAA", 1))
        b = Broker()
        r = self.enter(SUNDAY_NIGHT, b, books={"AAA": (49.0, 51.0), "HHH": (99.95, 100.05)})
        self.assertEqual(b.sent, [])
        self.assertIn("spread wider than 2% (AAA 4.0%)", r["warnings"][0])
        self.assertEqual([w["id"] for w in entries.waiting(DAY)], [f"{DAY}-S01"])

    def test_the_best_three_takes_of_a_pass_are_entered(self):
        names = ["AAA", "BBB", "CCC", "DDD"]
        self.prices.update({"CCC": 10.0, "DDD": 10.0})
        self.screen("night", *names)
        self.record("night", SUNDAY_NIGHT, *[verdict(n, i, hedge=None) for i, n in enumerate(names, 1)])
        r = self.enter(SUNDAY_NIGHT, Broker())
        self.assertEqual(len(r["new"]), entries.PASS_MAX_NEW)
        self.assertEqual([k["name"] for k in r["skipped"]], [f"{DAY}-P04"])

    def test_a_pass_can_stand_down_a_take_that_has_not_filled_and_exit_one_that_has(self):
        self.night_take(verdict("AAA", 1, hedge=None), verdict("BBB", 2, hedge=None))
        b = Broker(fill=lambda o: o["symbol"] == "AAA")
        self.enter(SUNDAY_NIGHT, b)
        self.assertEqual([w["id"] for w in entries.waiting(DAY)], [f"{DAY}-S02"])
        self.screen("morning")
        out = lambda tid, why: {"trade_id": tid, "action": "exit", "thesis": "broken", "reason": why}  # noqa: E731
        self.record("morning", MORNING, reviews=[out(f"{DAY}-S01", "the guide was cut"), out(f"{DAY}-S02", "it gapped away")])
        b.fill = lambda o: True
        r = self.enter(MORNING, b)
        self.assertEqual(r["stood_down"], [f"{DAY}-S02"])
        self.assertEqual([(e["trade_id"], e["reason"]) for e in r["exits"]], [(f"{DAY}-S01", "review: the guide was cut")])
        self.assertEqual([(o["symbol"], o["action"], o["venue"]) for o in r["orders"]], [("AAA", "SELL", "regular")])
        self.assertEqual(self.state(f"{DAY}-S01"), "closed")
        self.assertNotIn(f"{DAY}-S02", [t["id"] for t in book.trades()])   # stood down before anything filled: never in the book

    def test_an_exit_asked_for_at_night_waits_for_the_regular_session(self):
        self.write("plans", date(2026, 11, 6), {"trades": [take_trade("T1")]})
        self.write("fills", date(2026, 11, 6), {"fills": [fill("T1", "AAA", "BOT", 25), fill("T1", "HHH", "SLD", 25)]})
        self.screen("night")
        self.record("night", SUNDAY_NIGHT, reviews=[{"trade_id": "T1", "action": "exit", "thesis": "played_out", "reason": "the move is done"}])
        b = Broker()
        self.assertEqual(self.enter(SUNDAY_NIGHT, b)["orders"], [])
        r = self.enter(MORNING, b)
        self.assertEqual(sorted((o["symbol"], o["action"]) for o in r["orders"]), [("AAA", "SELL"), ("HHH", "BUY")])

    def test_a_halt_stops_the_entries_and_still_marks_the_pass(self):
        self.night_take(verdict("AAA", 1))
        book.HALT.parent.mkdir(parents=True, exist_ok=True)
        book.HALT.write_text("checking something\n")
        r = self.enter(SUNDAY_NIGHT, Broker())
        self.assertEqual((r["new"], r["skipped"][0]["reason"]), ([], "halted: checking something"))
        self.assertIn("night", entries.load(DAY)["marks"])


def take_trade(trade_id, entry=date(2026, 11, 6)):
    return {"id": trade_id, "book": "dynamic", "source": "test", "name": "AAA", "setup": "breakout", "direction": "long", "why": "",
            "entry_date": entry.isoformat(), "horizon_days": 4, "stop": {"spread_pct": -5.0}, "notional": 2500, "hedge": "HHH",
            "legs": [{"symbol": "AAA", "side": "long", "shares": 25, "ref_price": 100.0}, {"symbol": "HHH", "side": "short", "shares": 25, "ref_price": 100.0}]}


class Counting(Pass):
    def midday_trade(self):
        self.screen("midday", "AAA")
        self.record("midday", MIDDAY, verdict("AAA", 1))
        self.enter(MIDDAY, Broker())
        return next(t for t in book.trades() if t["id"] == f"{DAY}-S01")

    def test_a_midday_trade_counts_its_sessions_from_the_next_one(self):
        t = self.midday_trade()
        self.assertEqual((book._session(t, DAY), book._session(t, NEXT)), (0, 1))
        row = next(r for r in book.rows() if r["id"] == t["id"])
        self.assertEqual(row["exit_by"], book.journal.sessions_from(NEXT, 3)[-1].isoformat())

    def test_its_stop_is_still_checked_at_its_first_close(self):
        self.midday_trade()
        with mock.patch.object(book, "quotes", lambda symbols, *a: {"AAA": 94.0, "HHH": 100.0}):   # filled at 100
            out = asyncio.run(book.close_session(DAY, broker=Broker()))
        self.assertEqual([e["trade_id"] for e in out["exits"]], [f"{DAY}-S01"])
        self.assertIn("stopped", out["exits"][0]["reason"])

    def test_but_its_time_limit_is_not(self):
        self.midday_trade()
        with mock.patch.object(book, "quotes", lambda symbols, *a: {"AAA": 100.0, "HHH": 100.0}):   # the stand-in fills at 100
            out = asyncio.run(book.close_session(DAY, broker=Broker()))
        self.assertEqual(out["exits"], [])


class Scoring(Pass):
    def outcome(self, kind, now, closes):
        self.screen(kind, "AAA")
        self.record(kind, now, verdict("AAA", 1, "pass", 2))
        self.enter(now, Broker())
        self.bars = frame(closes)
        with mock.patch.object(learn.bars, "load", lambda symbols: self.bars):
            return learn.outcomes()[0]

    def test_a_verdict_is_scored_from_the_price_when_its_pass_was_acted_on(self):
        self.prices.update({"AAA": 100.0, "HHH": 100.0, "SPY": 100.0})
        o = self.outcome("night", SUNDAY_NIGHT, {"AAA": [101.0, 102.0, 104.0, 99.0], "HHH": [100.0] * 4})
        self.assertEqual((o["pass"], o["plan_status"], o["plan_bps"], o["plan_exit"]), ("night", "time", 400.0, book.journal.sessions_from(DAY, 3)[-1].isoformat()))

    def test_a_midday_verdict_checks_its_stop_that_day_and_counts_from_the_next(self):
        self.prices.update({"AAA": 100.0, "HHH": 100.0, "SPY": 100.0})
        o = self.outcome("midday", MIDDAY, {"AAA": [101.0, 102.0, 103.0, 105.0], "HHH": [100.0] * 4})
        self.assertEqual((o["plan_status"], o["plan_bps"]), ("time", 500.0))      # the entry day, then three sessions

    def test_a_stop_on_the_entry_day_ends_it(self):
        self.prices.update({"AAA": 100.0, "HHH": 100.0, "SPY": 100.0})
        o = self.outcome("midday", MIDDAY, {"AAA": [94.0, 110.0], "HHH": [100.0, 100.0]})
        self.assertEqual((o["plan_status"], o["plan_bps"]), ("stopped", -600.0))


class Restart(Case):
    def test_it_refuses_while_a_trade_is_open_and_only_looks_without_execute(self):
        from factor_rotation import portfolio
        from factor_rotation.paper import restart

        self.write("plans", DAY, {"trades": [take_trade("T1", DAY)]})
        self.write("fills", DAY, {"fills": [fill("T1", "AAA", "BOT", 25), fill("T1", "HHH", "SLD", 25)], "positions": {"AAA": 25, "HHH": -25}})
        (self.root / "lessons.md").write_text("- turn longs after a downgrade: 0 for 6\n")
        now = datetime(2026, 11, 9, 17, 0, tzinfo=NY)
        p = restart.run(execute=True, now=now)
        self.assertFalse(p["done"])
        self.assertIn("swing trade T1 AAA is open", p["blockers"])
        self.assertIn("the account held +25 AAA at the last sync (unknown)", p["blockers"])
        self.write("exits", DAY, {"exits": [{"trade_id": "T1", "reason": "flatten"}]})
        self.write("fills", NEXT, {"fills": [fill("T1", "AAA", "SLD", 25, n=2), fill("T1", "HHH", "BOT", 25, n=3)], "positions": {}})
        self.assertFalse(restart.run(now=now)["done"])                       # a look
        self.assertEqual((portfolio.capital("swing"), portfolio.running()), (900_000.0, ("swing", "intraday")))
        p = restart.run(execute=True, now=now)
        self.assertTrue(p["done"])
        self.assertTrue((self.root.parent / "archive" / "first-book-2026-11-09" / "paper" / "plans" / f"{DAY}.json").exists())
        self.assertEqual(book.trades(), [])                                  # the record starts again
        self.assertEqual((portfolio.capital("swing"), portfolio.running(), book.capital()), (1_000_000.0, ("swing",), 1_000_000.0))
        self.assertIn("turn longs after a downgrade", (self.root / "lessons.md").read_text())
        self.assertTrue(restart.run(execute=True, now=now)["already"])       # once


class Heal(unittest.TestCase):
    """A gateway stuck on IBKR's competing-session error is restarted, at most once an hour."""

    def test_only_when_every_quote_is_refused_for_a_competing_session(self):
        from factor_rotation.paper import entries
        stale = "the live IBKR login is in use somewhere else, and IBKR gives its quotes to one session at a time"
        self.assertIsNone(entries.heal_gateway({}))
        self.assertIsNone(entries.heal_gateway({"XSD": stale, "LUNR": "the account has no market data subscription for it over the API"}))

    def test_restarts_once_then_waits_an_hour(self):
        import subprocess
        from unittest import mock
        from factor_rotation.paper import book, entries
        stale = {"XSD": "the live IBKR login is in use somewhere else, and IBKR gives its quotes to one session at a time"}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(book, "ROOT", Path(d)), \
                mock.patch("shutil.which", return_value="/bin/docker"), \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            now = datetime(2026, 11, 16, 1, 0, tzinfo=NY)
            self.assertIn("restarted", entries.heal_gateway(stale, now))
            self.assertIsNone(entries.heal_gateway(stale, now + timedelta(minutes=30)))
            self.assertIn("restarted", entries.heal_gateway(stale, now + timedelta(minutes=61)))
            self.assertEqual(run.call_count, 2)

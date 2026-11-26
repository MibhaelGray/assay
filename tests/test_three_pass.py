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

from factor_rotation import live, screen, watch
from factor_rotation.config import NY
from factor_rotation.paper import book, daily, entries, learn, proposals
from factor_rotation.paper.broker import GuardError, check_order
from test_paper_book import DAY, NEXT, Broker, Case, fill, frame

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

    def overnight(self, last, volume=5000.0, bid=None, ask=None, spy_move=0.0):
        f = screen._features(daily_bars())
        q = {"bid": bid or last * 0.999, "ask": ask or last * 1.001, "last": last, "volume": volume}
        f["overnight"] = screen._overnight_of(q, f["close"], f["atr_pct"], spy_move, "2026-11-22T21:10:00-05:00")
        return f

    def test_an_overnight_move_net_of_spy_is_a_candidate(self):
        base = screen._features(daily_bars())
        f = self.overnight(base["close"] * (1 + 3 * base["atr_pct"]))
        hit = next(h for h in self.hits(f) if h["setup"] == "overnight_move")
        self.assertEqual(hit["direction"], "long")
        self.assertIn("overnight at 8:10 PM CT", hit["why"])
        self.assertIn("net of SPY", hit["why"])
        # The same move on a night SPY made all of it is the market, not the name.
        g = self.overnight(base["close"] * (1 + 3 * base["atr_pct"]), spy_move=3 * base["atr_pct"])
        self.assertNotIn("overnight_move", [h["setup"] for h in self.hits(g)])

    def test_an_overnight_quote_with_no_trade_or_a_wide_spread_does_not_count(self):
        base = screen._features(daily_bars())
        px = base["close"] * (1 + 3 * base["atr_pct"])
        self.assertIsNone(self.overnight(px, volume=0)["overnight"])
        self.assertIsNone(self.overnight(px, bid=px * 0.97, ask=px * 1.03)["overnight"])

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
        self.assertEqual(entries.venue(at(10, 17, 0)), "extended")     # Tuesday after-hours
        self.assertEqual(entries.venue(at(11, 2, 0)), "overnight")
        self.assertIsNone(entries.venue(at(11, 3, 55)))                # IBKR's overnight ends at 3:50 AM
        self.assertIsNone(entries.venue(at(13, 21, 0)))                # Friday night: no overnight session
        self.assertEqual(entries.venue(at(8, 20, 30)), "overnight")    # Sunday night is

    def test_an_order_buys_at_the_ask_and_sells_at_the_bid_and_needs_a_price_and_a_session(self):
        self.assertEqual(entries.crossing("BUY", (99.0, 100.0)), 100.0)
        self.assertEqual(entries.crossing("SELL", (99.0, 100.0)), 99.0)
        self.assertIsNone(entries.crossing("SELL", None))               # no book, no price, no order
        self.assertEqual(entries.limit_price("SELL", 99.0), 98.5)
        o = {"trade_id": "T", "symbol": "AAA", "action": "BUY", "shares": 5, "kind": "LMT"}
        with self.assertRaises(GuardError):
            check_order({**o, "venue": "overnight"})
        with self.assertRaises(GuardError):
            check_order({**o, "limit": 10.0, "venue": "lunch"})
        check_order({**o, "limit": 10.0, "venue": "overnight"})


def candidate(name, hedge="HHH", setup="live_move", direction="long"):
    return {"name": name, "kind": "stock", "setup": setup, "direction": direction, "strength": 2.5, "why": f"{name} is moving",
            "hedge": hedge, "hedge_fit": None, "score": 3.0, "chart": "", "numbers": {"price": 50.0, "atr_pct": 0.02}, "also": []}


def verdict(name, rank, decision="take", conviction=3, **kw):
    v = {"candidate": name, "rank": rank, "decision": decision, "conviction": conviction, "reason": "the reason", **kw}
    if decision == "take":
        v = {"reasoning": {k: "x" for k in ("what_changed", "story", "chart", "coming", "case_against")}, "wrong_if": "it gives the move back",
             "hedge_why": "its sector", "plan": {"horizon_days": 3, "stop": {"spread_pct": -5}}, **v}
    else:
        v = {"pass_case": {"case_for": "a move", "case_against": "no story", "why_not": "nothing behind it"}, **v}
    return v


class Pass(Case):
    """A journal with a live screen for a pass, Yahoo standing in at fixed prices, and a gateway that quotes."""

    def setUp(self):
        super().setUp()
        self.reports, self.bars, self.prices = {}, pd.DataFrame(), {"AAA": 50.0, "BBB": 20.0, "HHH": 100.0, "SPY": 500.0}
        self.sent = []   # phone messages (watch.send), kept here instead of sent
        for target, name, value in ((proposals.earnings, "load", lambda: {"names": self.reports}),
                                    (proposals.bars, "load", lambda symbols: self.bars),
                                    (book.journal, "_bars_settled", lambda d: True),
                                    (book, "PaperSettings", lambda: mock.Mock(send=True)),
                                    (entries.drawdown, "paused", lambda b: None),
                                    (watch, "send", lambda title, text: self.sent.append((title, text)))):
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

    def enter(self, now, broker, books=None, missing=None, marks=None):
        """`marks` are the stop check's prices (book.quotes); every leg is at 100, where the stand-in fills, unless given."""
        books = books if books is not None else {s: (p - 0.05, p + 0.05) for s, p in self.prices.items()}

        async def gateway(symbols, overnight):
            return {s: books[s] for s in symbols if s in books}, {s: w for s, w in (missing or {}).items() if s in symbols}
        stop_prices = lambda symbols: {s: (marks or {}).get(s, 100.0) for s in symbols}  # noqa: E731
        return asyncio.run(entries.run(now, broker, quotes=gateway, prices=lambda symbols: {s: self.prices[s] for s in symbols if s in self.prices},
                                       stop_prices=stop_prices))


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

    def test_a_draft_that_skips_a_candidate_is_refused(self):
        self.screen("night", "AAA", "BBB")
        with self.assertRaisesRegex(ValueError, r"1 of 2 have none: BBB \(screen #2\)"):
            self.record("night", SUNDAY_NIGHT, verdict("AAA", 1))
        _, doc, _ = self.record("night", SUNDAY_NIGHT, verdict("AAA", 1), verdict("BBB", 2, "pass", 2))
        self.assertEqual([(p["name"], p["rank"]) for p in doc["proposals"]], [("AAA", 1), ("BBB", 2)])

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


class Slow(Broker):
    """Fills at most `take[symbol]` shares of each order in that symbol; every other symbol fills whole."""

    def __init__(self, take):
        super().__init__()
        self.take = take

    async def place(self, orders):
        self.sent += orders
        for o in orders:
            if n := min(o["shares"], self.take.get(o["symbol"], o["shares"])):
                self._fills.append(fill(o["trade_id"], o["symbol"], "BOT" if o["action"] == "BUY" else "SLD", n, n=len(self._fills)))
        return [{**o, "order_id": i, "status": "Submitted", "note": ""} for i, o in enumerate(orders)]


class Taking(Pass):
    def night_take(self, *verdicts):
        self.screen("night", *[v["candidate"] for v in verdicts])
        self.record("night", SUNDAY_NIGHT, *verdicts)


class Enter(Taking):

    def test_a_take_is_traded_as_soon_as_the_pass_records_at_the_gateways_bid_and_ask(self):
        self.night_take(verdict("AAA", 1), verdict("BBB", 2, "pass", 2))
        b = Broker()
        r = self.enter(SUNDAY_NIGHT, b)
        self.assertEqual((r["venue"], r["passes"], r["new"]), ("overnight", ["night"], [f"{DAY}-S01"]))
        self.assertEqual([(o["symbol"], o["action"], o["shares"], o["kind"], o["ref_price"], o["limit"], o["venue"]) for o in b.sent],
                         [("AAA", "BUY", 540, "LMT", 50.05, 50.3, "overnight"), ("HHH", "SELL", 270, "LMT", 99.95, 99.45, "overnight")])
        saved = entries.load(DAY)
        t = saved["trades"][0]
        self.assertEqual((t["pass"], t["first_session"], t["proposal_id"]), ("night", DAY.isoformat(), f"{DAY}-P01"))
        # Sized by risk: 0.15% of the $900,000 test book ($1,350) over the 5% stop is $27,000 a side.
        self.assertEqual((t["notional"], t["sizing"]), (27_000, {"risk_dollars": 1350, "stop_pct": -5.0, "limited_by": "risk"}))
        self.assertEqual(saved["marks"]["night"]["prices"], {"AAA": 50.0, "BBB": 20.0, "HHH": 100.0, "SPY": 500.0})   # the pass too
        self.assertEqual(self.state(f"{DAY}-S01"), "open")
        self.assertEqual(self.enter(SUNDAY_NIGHT.replace(minute=46), b)["note"], "nothing new")
        self.assertEqual(len(b.sent), 2)

    def test_a_stronger_idea_and_a_tighter_stop_get_more_and_a_thin_leg_gets_less(self):
        self.prices["CCC"] = 10.0
        self.screen("night", "AAA", "BBB", "CCC")
        self.record("night", SUNDAY_NIGHT, verdict("AAA", 1, conviction=5, hedge=None),
                    verdict("BBB", 2, hedge=None, plan={"horizon_days": 3, "stop": {"spread_pct": -8}}), verdict("CCC", 3, hedge=None))
        books = {s: (p - 0.01, p + 0.01) for s, p in self.prices.items()}

        async def gateway(symbols, overnight):
            return {s: books[s] for s in symbols}, {}
        r = asyncio.run(entries.run(SUNDAY_NIGHT, Broker(), quotes=gateway, prices=lambda symbols: {s: self.prices[s] for s in symbols},
                                    volumes=lambda symbols: {"CCC": 4_000_000.0, "AAA": 900_000_000.0}))
        self.assertEqual(r["skipped"], [])
        sized = {t["name"]: (t["notional"], t["sizing"]["limited_by"]) for t in entries.load(DAY)["trades"]}
        # conviction 5 risks twice as much ($2,700 over 5%), capped at 6% of the book; an 8% stop gets $1,350 / 8%;
        # a leg trading $4M a day holds the trade to a quarter of a percent of that.
        self.assertEqual(sized, {"AAA": (54_000, "risk"), "BBB": (16_800, "risk"), "CCC": (10_000, "liquidity: CCC")})

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

    def test_a_wide_overnight_spread_queues_for_835_ct_and_enters_then(self):
        self.night_take(verdict("AAA", 1))
        b = Broker()
        # 1.0% wide overnight (> 0.5% overnight cap): deferred to 8:35 AM CT instead of killed
        r = self.enter(SUNDAY_NIGHT, b, books={"AAA": (49.75, 50.25), "HHH": (99.95, 100.05)})
        self.assertEqual(b.sent, [])
        self.assertEqual((r["passed"], r["deferred"]), ([], [f"{DAY}-S01"]))
        self.assertIn("spread wider than 0.5% (AAA 1.0%), so the trade queues for 8:35 AM CT", r["warnings"][0])
        # Quietly waits during overnight and 9:31 AM ET (8:31 AM CT)
        self.assertEqual(self.enter(SUNDAY_NIGHT.replace(minute=50), b)["note"], "queued trades wait for 8:35 AM CT")
        self.assertEqual(self.enter(datetime(2026, 11, 9, 9, 31, tzinfo=NY), b)["note"], "queued trades wait for 8:35 AM CT")
        # At 9:35 AM ET (8:35 AM CT), enters immediately in regular hours
        r = self.enter(datetime(2026, 11, 9, 9, 35, tzinfo=NY), b)
        self.assertEqual([(o["symbol"], o["venue"]) for o in r["orders"]], [("AAA", "regular"), ("HHH", "regular")])
        self.assertEqual(self.state(f"{DAY}-S01"), "open")

    def test_a_wide_regular_spread_retries_then_passes_on_the_trade(self):
        self.screen("morning", "AAA")
        self.record("morning", MORNING, verdict("AAA", 1))
        b = Broker()
        wide = {"AAA": (49.75, 50.25), "HHH": (99.95, 100.05)}  # AAA 1.0% wide > 0.5% regular-hours cap
        r1 = self.enter(MORNING, b, books=wide)
        self.assertEqual((b.sent, r1["passed"]), ([], []))
        self.assertIn("spread wider than 0.5% (AAA 1.0%) (try 1 of 3)", r1["warnings"][0])
        self.assertEqual(self.enter(MORNING + timedelta(minutes=1), b, books=wide)["note"],
                         "waiting: the next try is 3 minutes after the last")
        r2 = self.enter(MORNING + timedelta(minutes=3), b, books=wide)
        self.assertEqual((b.sent, r2["passed"]), ([], []))
        self.assertIn("try 2 of 3", r2["warnings"][0])
        r3 = self.enter(MORNING + timedelta(minutes=6), b, books=wide)
        self.assertEqual((b.sent, r3["passed"]), ([], [f"{DAY}-S01"]))
        self.assertIn("spread wider than 0.5% (AAA 1.0%) in 3 tries, so the trade is passed on", r3["warnings"][0])
        self.assertEqual(entries.waiting(DAY), [])

    def test_a_wide_regular_spread_that_tightens_on_retry_enters_cleanly(self):
        self.screen("morning", "AAA")
        self.record("morning", MORNING, verdict("AAA", 1))
        b = Broker()
        wide = {"AAA": (49.65, 50.35), "HHH": (99.95, 100.05)}  # AAA 1.4% wide (like MMS)
        r1 = self.enter(MORNING, b, books=wide)
        self.assertEqual((b.sent, r1["passed"]), ([], []))
        r2 = self.enter(MORNING + timedelta(minutes=3), b)       # spread tightens to 0.2%
        self.assertEqual(r2["passed"], [])
        self.assertEqual(self.state(f"{DAY}-S01"), "open")
        self.assertNotIn("wide_tries", entries.load(DAY)["trades"][0])

    def test_pending_review_exits_do_not_block_new_takes_on_the_gross_cap(self):
        # Fill the $900k test book (1.5x cap = $1,350,000 gross) with an open trade ($1,320k gross)
        big = {
            "id": "T_BIG", "book": "dynamic", "source": "test", "name": "ZZZ", "setup": "breakout", "direction": "long",
            "why": "", "entry_date": date(2026, 11, 6).isoformat(), "horizon_days": 4, "stop": {"spread_pct": -5.0},
            "notional": 660_000, "hedge": "YYY",
            "legs": [{"symbol": "ZZZ", "side": "long", "shares": 6600, "ref_price": 100.0},
                     {"symbol": "YYY", "side": "short", "shares": 6600, "ref_price": 100.0}],
        }
        self.write("entries", date(2026, 11, 6), {"trades": [big]})
        self.write("fills", date(2026, 11, 6), {"fills": [fill("T_BIG", "ZZZ", "BOT", 6600), fill("T_BIG", "YYY", "SLD", 6600)]})
        self.prices.update({"ZZZ": 100.0, "YYY": 100.0})
        # Morning pass exits T_BIG and takes AAA ($54k gross, which would exceed $1,350k if T_BIG were still counted)
        self.screen("morning", "AAA")
        self.record("morning", MORNING, verdict("AAA", 1),
                    reviews=[{"trade_id": "T_BIG", "action": "exit", "thesis": "played_out", "reason": "done"}])
        # Even if IBKR 10197 blocks quotes during this run so T_BIG hasn't exited yet, AAA is NOT skipped on gross cap
        refused = {s: "the live IBKR login is in use somewhere else (IBKR error 10197)" for s in ("AAA", "HHH", "ZZZ", "YYY")}
        b = Broker()
        r = self.enter(MORNING, b, books={}, missing=refused)
        self.assertEqual(r["skipped"], [])
        self.assertEqual(r["new"], [f"{DAY}-S01"])
        # Once quotes return 3 minutes later, T_BIG exits and AAA enters cleanly
        r2 = self.enter(MORNING + timedelta(minutes=3), b)
        self.assertEqual([e["trade_id"] for e in r2["exits"]], ["T_BIG"])
        self.assertEqual((self.state("T_BIG"), self.state(f"{DAY}-S01")), ("closed", "open"))

    def test_gross_cap_allows_up_to_one_and_a_half_times_capital(self):
        # On the $900k test book, 1.5x gross cap is $1,350,000: $1,250k open + $54k new fits, a second $54k skips
        existing = {
            "id": "T_EX", "book": "dynamic", "source": "test", "name": "ZZZ", "setup": "breakout", "direction": "long",
            "why": "", "entry_date": date(2026, 11, 6).isoformat(), "horizon_days": 4, "stop": {"spread_pct": -5.0},
            "notional": 625_000, "hedge": "YYY",
            "legs": [{"symbol": "ZZZ", "side": "long", "shares": 6250, "ref_price": 100.0},
                     {"symbol": "YYY", "side": "short", "shares": 6250, "ref_price": 100.0}],
        }
        self.write("entries", date(2026, 11, 6), {"trades": [existing]})
        self.write("fills", date(2026, 11, 6), {"fills": [fill("T_EX", "ZZZ", "BOT", 6250), fill("T_EX", "YYY", "SLD", 6250)]})
        self.prices.update({"ZZZ": 100.0, "YYY": 100.0})
        self.screen("morning", "AAA", "BBB")
        self.record("morning", MORNING, verdict("AAA", 1, hedge="SPY"), verdict("BBB", 2, hedge="SPY"),
                    reviews=[{"trade_id": "T_EX", "action": "hold", "thesis": "intact", "reason": "holding"}])
        r = self.enter(MORNING, Broker())
        self.assertEqual(r["new"], [f"{DAY}-S01"])
        self.assertEqual(len(r["skipped"]), 1)
        self.assertIn("over $1,350,000", r["skipped"][0]["reason"])

    def test_chasing_guard_stands_down_a_deferred_trade_that_already_ran_away(self):
        self.night_take(verdict("AAA", 1))  # atr_pct=0.02, marked at AAA=50, HHH=100
        b = Broker()
        self.enter(SUNDAY_NIGHT, b, books={"AAA": (49.0, 51.0), "HHH": (99.95, 100.05)})
        # Overnight AAA jumped +3% (+1.5 ATR > 1.0 ATR) to 51.50 against flat HHH: do not chase at 8:35 AM CT
        r = self.enter(datetime(2026, 11, 9, 9, 35, tzinfo=NY), b, books={"AAA": (51.45, 51.55), "HHH": (99.95, 100.05)})
        self.assertEqual((b.sent, r["passed"]), ([], [f"{DAY}-S01"]))
        self.assertIn("not chasing", r["warnings"][0])

    def test_no_overnight_market_queues_for_regular_hours_but_a_gateway_fault_waits(self):
        nothing = {"AAA": "the gateway sent no bid or ask within 8 seconds and gave no reason"}
        refused = {"AAA": "the live IBKR login is in use somewhere else (IBKR error 10197)"}
        spy = {"SPY": (599.95, 600.05), "HHH": (99.95, 100.05)}
        self.night_take(verdict("AAA", 1))
        r = self.enter(SUNDAY_NIGHT, Broker(), books={"HHH": (99.95, 100.05)}, missing=nothing)   # SPY unquoted too: the gateway
        self.assertEqual((r["passed"], r["deferred"]), ([], []))
        r = self.enter(SUNDAY_NIGHT.replace(hour=21, minute=1), Broker(), books=spy, missing=refused)   # a refusal is a fault -> 3 min retry
        self.assertEqual((r["passed"], r["deferred"]), ([], []))
        self.assertIn("waits for the next try", r["warnings"][0])
        self.assertEqual(self.enter(SUNDAY_NIGHT.replace(hour=21, minute=2), Broker())["note"], "waiting: the next try is 3 minutes after the last")
        b = Broker()
        r = self.enter(SUNDAY_NIGHT.replace(hour=21, minute=5), b, books=spy, missing=nothing)
        self.assertEqual((b.sent, r["passed"], r["deferred"]), ([], [], [f"{DAY}-S01"]))
        self.assertIn("queues for 8:35 AM CT", r["warnings"][0])

    def test_a_late_pass_by_hand_is_a_second_night_pass_and_its_takes_are_traded(self):
        self.screen("night", "AAA")
        self.record("night", SUNDAY_NIGHT, verdict("AAA", 1, "pass", 2))
        later = SUNDAY_NIGHT.replace(hour=22)
        self.screen("late", "BBB")
        self.record("late", later, verdict("BBB", 1, hedge=None))
        with self.assertRaisesRegex(ValueError, "already recorded"):
            self.record("late", later, verdict("BBB", 1, hedge=None))
        r = self.enter(later, Broker())
        self.assertEqual((r["passes"], r["new"]), (["night", "late"], [f"{DAY}-S01"]))
        t = entries.load(DAY)["trades"][0]
        self.assertEqual((t["pass"], t["first_session"], t["name"]), ("late", DAY.isoformat(), "BBB"))

    def test_a_second_late_pass_records_under_the_next_free_name(self):
        self.screen("night", "AAA")
        self.record("night", SUNDAY_NIGHT, verdict("AAA", 1, "pass", 2))
        later = SUNDAY_NIGHT.replace(hour=22)
        self.assertEqual(proposals.next_late(later), "late")
        self.screen("late", "CCC")
        self.record("late", later, verdict("CCC", 1, "pass", 2))
        self.assertEqual(proposals.next_late(later), "late2")
        self.screen("late2", "BBB")
        self.record("late2", later, verdict("BBB", 1, hedge=None))
        r = self.enter(later, Broker())
        self.assertEqual(r["passes"], ["night", "late", "late2"])
        t = entries.load(DAY)["trades"][0]
        self.assertEqual((t["pass"], t["first_session"], t["name"]), ("late2", DAY.isoformat(), "BBB"))

    def test_a_trade_that_never_fills_overnight_queues_for_regular_hours_after_three_tries(self):
        self.night_take(verdict("AAA", 1))
        b = Slow({"AAA": 0})                                                # the quote is there; nothing fills against it
        self.enter(SUNDAY_NIGHT, b)
        self.enter(SUNDAY_NIGHT.replace(hour=21, minute=0), b)
        self.assertIsNone(entries.load(DAY)["trades"][0].get("defer_regular"))
        r = self.enter(SUNDAY_NIGHT.replace(hour=21, minute=15), b)
        t = entries.load(DAY)["trades"][0]
        self.assertIn("no fill in 3 overnight tries", t["defer_regular"])
        self.assertEqual((r["passed"], r["deferred"]), ([], [t["id"]]))
        sent = len(b.sent)
        self.enter(SUNDAY_NIGHT.replace(hour=21, minute=30), b)             # waits quietly overnight
        self.assertEqual(len(b.sent), sent)

    def test_regular_hours_tries_midpoint_then_steps_to_ask_with_tight_slip(self):
        self.screen("morning", "AAA")
        self.record("morning", MORNING, verdict("AAA", 1, hedge=None))
        # Broker only fills if limit reaches the ask (50.05), so midpoint (50.00) cancels and crossing fills at 50.05 * 1.0015 = 50.13
        b = Broker(fill=lambda o: o["limit"] >= 50.05)
        r = self.enter(MORNING, b, books={"AAA": (49.95, 50.05)})
        self.assertEqual([(o["priced_on"], o["limit"]) for o in r["orders"]],
                         [("gateway midpoint", 50.0), ("gateway bid/ask", 50.13)])
        self.assertEqual(self.state(f"{DAY}-S01"), "open")

    def test_the_hedge_is_never_ordered_while_the_lead_has_not_filled(self):
        self.night_take(verdict("AAA", 1))
        b = Slow({"AAA": 0})
        r = self.enter(SUNDAY_NIGHT, b)
        self.assertEqual([(o["symbol"], o["shares"]) for o in b.sent], [("AAA", 540)])          # the lead alone
        # It hit the ask and nothing filled, so the order is cancelled in the same run: nothing is left resting.
        self.assertEqual((r["cancelled_unfilled"], [o["symbol"] for o in b.cancelled]), (1, ["AAA"]))
        self.assertEqual(entries.load(DAY)["trades"][0]["lead"], "AAA")
        self.enter(SUNDAY_NIGHT.replace(minute=59), b)                                          # 14 minutes on: not due yet
        self.enter(SUNDAY_NIGHT.replace(hour=21, minute=0), b)                                  # the quarter-hour try
        self.assertEqual([(o["symbol"], o["shares"]) for o in b.sent], [("AAA", 540), ("AAA", 540)])

    def test_the_other_legs_follow_the_share_of_the_lead_that_has_filled_and_are_tried_every_minute(self):
        self.night_take(verdict("AAA", 1))
        b = Slow({"AAA": 135})                                                                  # a quarter of the lead at a time
        self.enter(SUNDAY_NIGHT, b)
        sent = lambda: [(o["symbol"], o["action"], o["shares"]) for o in b.sent]  # noqa: E731
        self.assertEqual(sent(), [("AAA", "BUY", 540), ("HHH", "SELL", 67)])                    # a quarter of 270, rounded down
        self.assertIn("a minute after", self.enter(SUNDAY_NIGHT + timedelta(seconds=30), b)["note"])
        self.enter(SUNDAY_NIGHT + timedelta(minutes=1), b)
        self.assertEqual(sent()[2:], [("AAA", "BUY", 405), ("HHH", "SELL", 68)])                # half in: 135 of 270
        # A hedge that can't be sent holds the lead back too, so the stock doesn't run ahead of it.
        r = self.enter(SUNDAY_NIGHT.replace(minute=55), b, books={"AAA": (49.95, 50.05), "HHH": (95.0, 105.0)})
        self.assertEqual(len(b.sent), 4)
        self.assertIn("the rest of the trade waits", r["warnings"][0])

    def test_every_take_of_a_pass_is_entered_until_the_book_is_full(self):
        names = ["AAA", "BBB", "CCC", "DDD"]
        self.prices.update({"CCC": 10.0, "DDD": 10.0})
        self.screen("night", *names)
        self.record("night", SUNDAY_NIGHT, *[verdict(n, i, hedge=None) for i, n in enumerate(names, 1)])
        r = self.enter(SUNDAY_NIGHT, Broker())
        self.assertEqual((len(r["new"]), r["skipped"]), (4, []))   # no count cap; the book's capital is the limit (rules.apply_caps)

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
        self.write("entries", date(2026, 11, 6), {"trades": [take_trade("T1")]})
        self.write("fills", date(2026, 11, 6), {"fills": [fill("T1", "AAA", "BOT", 25), fill("T1", "HHH", "SLD", 25)]})
        self.screen("night")
        self.record("night", SUNDAY_NIGHT, reviews=[{"trade_id": "T1", "action": "exit", "thesis": "played_out", "reason": "the move is done"}])
        b = Broker()
        self.assertEqual(self.enter(SUNDAY_NIGHT, b)["orders"], [])
        r = self.enter(MORNING, b)
        self.assertEqual(sorted((o["symbol"], o["action"]) for o in r["orders"]), [("AAA", "SELL"), ("HHH", "BUY")])

    def held_trade(self):
        """T1: long AAA, short HHH, 25 shares each at 100, a 5% stop, entered the session before DAY."""
        self.write("entries", date(2026, 11, 6), {"trades": [take_trade("T1")]})
        self.write("fills", date(2026, 11, 6), {"fills": [fill("T1", "AAA", "BOT", 25), fill("T1", "HHH", "SLD", 25)]})
        self.prices.update({"AAA": 94.0, "HHH": 100.0})

    def test_a_trade_past_its_stop_comes_out_during_the_day_not_at_the_close(self):
        self.held_trade()
        r = self.enter(MORNING, Broker(), marks={"AAA": 94.0})        # down 6% against a 5% stop
        self.assertEqual([(e["trade_id"], e["at"]) for e in r["exits"]], [("T1", "10:00 AM CT")])
        self.assertEqual(r["exits"][0]["reason"], "stopped: down 6.0% at 10:00 AM CT, past the 5% stop")
        self.assertEqual(sorted((o["symbol"], o["action"], o["venue"]) for o in r["orders"]), [("AAA", "SELL", "regular"), ("HHH", "BUY", "regular")])
        self.assertEqual(self.state("T1"), "closed")
        with mock.patch.object(book, "quotes", lambda symbols, *a: {"AAA": 90.0, "HHH": 100.0}):
            self.assertEqual(asyncio.run(book.close_session(DAY, broker=Broker()))["exits"], [])   # nothing left for the close

    def test_a_trade_inside_its_stop_stays_and_the_check_waits_its_turn(self):
        self.held_trade()
        r = self.enter(MORNING, Broker(), marks={"AAA": 96.0})        # down 4%
        self.assertEqual((r["exits"], r["orders"]), ([], []))
        self.assertEqual(entries.load(DAY)["last_stop_check"], MORNING.isoformat())
        soon = self.enter(MORNING + timedelta(minutes=1), Broker(), marks={"AAA": 90.0})
        self.assertEqual(soon["exits"], [])                           # a minute later: not due yet
        later = self.enter(MORNING + entries.STOP_EVERY, Broker(), marks={"AAA": 90.0})
        self.assertEqual([e["trade_id"] for e in later["exits"]], ["T1"])

    def test_stops_are_not_checked_outside_the_regular_session(self):
        self.held_trade()
        r = self.enter(SUNDAY_NIGHT, Broker(), marks={"AAA": 80.0})
        self.assertEqual((r["exits"], r["orders"]), ([], []))

    def test_a_stop_exit_with_no_quote_is_not_sent_and_is_tried_again(self):
        self.held_trade()
        r = self.enter(MORNING, Broker(), marks={"AAA": 94.0}, books={"HHH": (99.95, 100.05)})
        self.assertEqual((r["exits"], r["orders"]), ([], []))
        self.assertIn("its stop exit wasn't sent", " ".join(r["warnings"]))
        self.assertEqual(self.state("T1"), "open")
        again = self.enter(MORNING + entries.STOP_EVERY, Broker(), marks={"AAA": 94.0})
        self.assertEqual([e["trade_id"] for e in again["exits"]], ["T1"])

    def test_stops_check_every_ten_minutes_in_the_opening_half_hour_too(self):
        # The same step all day (2026-11-26): the opening half-hour was every two minutes from 11-23 to 11-25.
        self.held_trade()
        t0 = datetime(2026, 11, 9, 9, 32, tzinfo=NY)
        self.enter(t0, Broker(), marks={"AAA": 96.0})
        self.assertEqual(self.enter(t0 + timedelta(minutes=2), Broker(), marks={"AAA": 90.0})["exits"], [])
        later = self.enter(t0 + entries.STOP_EVERY, Broker(), marks={"AAA": 90.0})
        self.assertEqual([e["trade_id"] for e in later["exits"]], ["T1"])

    def test_stops_use_gateway_quotes_first_and_fall_back_to_yahoo(self):
        self.held_trade()
        # Gateway quotes AAA at 93.90/94.10 (mid 94.0, past 5% stop) and misses HHH -> Yahoo fills HHH at 100.0
        async def gw(symbols, overnight):
            return ({"AAA": (93.9, 94.1)} if "AAA" in symbols else {}), {}
        with mock.patch.object(book, "quotes", lambda syms: {s: 100.0 for s in syms}):
            px = asyncio.run(entries._gateway_stop_prices(["AAA", "HHH"], gw))
        self.assertEqual(px, {"AAA": 94.0, "HHH": 100.0})

    def test_the_phone_hears_how_a_pass_was_traded_once_and_in_counts_only(self):
        self.night_take(verdict("AAA", 1, hedge=None), verdict("BBB", 2, hedge=None))
        self.enter(SUNDAY_NIGHT, Broker(), books={"AAA": (49.95, 50.05), "BBB": (19.0, 21.0)})   # BBB 10% wide -> queues
        self.assertEqual(self.sent, [("Night pass traded", "2 takes: 1 in, 1 queued for 8:35 AM CT.")])
        self.enter(SUNDAY_NIGHT + timedelta(minutes=1), Broker())
        self.assertEqual(len(self.sent), 1)                      # the pass is told once, not every minute

    def test_a_pass_with_no_takes_says_so(self):
        self.night_take(verdict("AAA", 1, "pass", 2))
        self.enter(SUNDAY_NIGHT, Broker())
        self.assertEqual(self.sent, [("Night pass traded", "The night pass took no trades.")])

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


class Heal(unittest.TestCase):
    def test_after_a_restart_the_next_run_tries_again_at_once(self):
        from unittest import mock
        from factor_rotation.paper import book, entries
        with tempfile.TemporaryDirectory() as d, mock.patch.object(book, "ROOT", Path(d)):
            now = datetime(2026, 11, 17, 9, 0, tzinfo=NY)
            session = entries.entry_session(now)
            book._write(entries.path(session), {"session": session.isoformat(), "last_attempt": now.isoformat(), "runs": [], "trades": []})
            entries.retry_soon(now)
            self.assertIsNone(entries.load(session)["last_attempt"])

    """A gateway stuck on IBKR's competing-session error is restarted, at most once an hour."""

    def test_only_when_every_quote_is_refused_for_a_competing_session_or_gateway_hang(self):
        from factor_rotation.paper import entries
        stale = "the live IBKR login is in use somewhere else, and IBKR gives its quotes to one session at a time"
        hung = "the gateway couldn't be asked for quotes (TimeoutError)"
        self.assertIsNone(entries.heal_gateway({}))
        self.assertIsNone(entries.heal_gateway({"XSD": stale, "LUNR": "the account has no market data subscription for it over the API"}))
        self.assertIsNone(entries.heal_gateway({"XSD": hung, "LUNR": "the account has no market data subscription for it over the API"}))
        with tempfile.TemporaryDirectory() as d, mock.patch.object(book, "ROOT", Path(d)), \
                mock.patch("shutil.which", return_value="/bin/docker"), \
                mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            self.assertIn("restarted", entries.heal_gateway({"XSD": hung}, datetime(2026, 11, 16, 1, 0, tzinfo=NY)))
            self.assertEqual(run.call_count, 1)

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


class HalfDayTests(unittest.TestCase):
    """The day after Thanksgiving closes at 1:00 PM ET: the close job runs at 12:45 PM ET, entries stop at 12:40 PM
    ET, and the midday pass doesn't run."""

    def test_calendar(self):
        from factor_rotation import journal
        self.assertTrue(journal.is_half_day(date(2026, 11, 27)))
        self.assertFalse(journal.is_half_day(date(2026, 11, 25)))
        self.assertEqual(journal.close_time(date(2026, 11, 27)).hour, 13)
        self.assertEqual(journal.close_time(date(2026, 11, 25)).hour, 16)

    def test_close_job_window(self):
        from factor_rotation.cli import _trading_day
        at = lambda d, h, m: datetime(2026, 11, d, h, m, tzinfo=NY)  # noqa: E731
        self.assertTrue(_trading_day(closing=True, now=at(27, 12, 45)))
        self.assertFalse(_trading_day(closing=True, now=at(27, 15, 45)))
        self.assertTrue(_trading_day(closing=True, now=at(25, 15, 45)))
        self.assertFalse(_trading_day(closing=True, now=at(25, 12, 45)))
        self.assertFalse(_trading_day(full=True, now=at(27, 9, 0)))
        self.assertTrue(_trading_day(full=True, now=at(25, 9, 0)))

    def test_entry_window(self):
        from factor_rotation.paper import entries
        at = lambda h, m: datetime(2026, 11, 27, h, m, tzinfo=NY)  # noqa: E731
        self.assertEqual(entries.venue(at(12, 30)), "regular")
        self.assertIsNone(entries.venue(at(12, 41)))
        self.assertEqual(entries.venue(at(13, 30)), "extended")
        self.assertIsNone(entries.venue(at(17, 30)))
        self.assertEqual(entries.entry_session(at(13, 30)), date(2026, 11, 30))
        self.assertEqual(entries.venue(datetime(2026, 11, 25, 15, 30, tzinfo=NY)), "regular")

    def test_watchdog_skips_midday(self):
        from factor_rotation import watch
        self.assertFalse(watch._pass_days("full", date(2026, 11, 27), lambda d: True))
        self.assertTrue(watch._pass_days("full", date(2026, 11, 25), lambda d: True))


class IntradayStopReplayTests(unittest.TestCase):
    """Every verdict's stop is replayed on the order job's 10-minute checks from 5-minute bars (the owner, 2026-11-22)."""

    def _bars(self, day, prices):
        idx = pd.date_range(datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30), periods=len(prices), freq="5min", tz=NY)
        return pd.Series(prices, index=idx)

    def test_checks_every_ten_minutes_until_twenty_before_close(self):
        from factor_rotation.paper import learn
        c = learn._checks(date(2026, 11, 20))
        self.assertEqual((c[0].hour, c[0].minute), (9, 40))
        self.assertEqual((c[-1].hour, c[-1].minute), (15, 40))
        h = learn._checks(date(2026, 11, 27))
        self.assertEqual((h[-1].hour, h[-1].minute), (12, 40))

    def test_dip_past_stop_is_caught_even_if_it_recovers(self):
        from factor_rotation.paper import learn
        day = date(2026, 11, 20)
        prices = [100.0] * 78
        prices[3] = 93.0        # the 9:45 bar, finished by the 9:50 check
        ix = {"AAA": self._bars(day, prices)}
        how, hit = learn._intraday_hit(day, ["AAA"], {"AAA": 1.0}, {"AAA": 100.0}, -6.0, ix, None, date(2026, 11, 22))
        self.assertEqual(how, "hit")
        self.assertEqual((hit[0].hour, hit[0].minute), (9, 50))
        self.assertAlmostEqual(hit[1], -0.07)

    def test_missing_bars_wait_then_fall_back(self):
        from factor_rotation.paper import learn
        day = date(2026, 11, 20)
        args = (day, ["AAA"], {"AAA": 1.0}, {"AAA": 100.0}, -6.0, {}, None)
        self.assertEqual(learn._intraday_hit(*args, date(2026, 11, 22))[0], "wait")
        self.assertEqual(learn._intraday_hit(*args, date(2026, 12, 5))[0], "none")

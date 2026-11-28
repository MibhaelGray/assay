"""Regression tests for the comprehensive Assay audit fixes. No network, no gateway.

    uv run python -m unittest discover -s tests
"""

import asyncio
import json
import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace as N
from unittest import mock

import pandas as pd

from factor_rotation import bars, edgar, journal, live, passes, portfolio, quotes, routine, storage, xfeed
from factor_rotation.config import NY
from factor_rotation.paper import book, broker, depth, entries, gateway, holds, learn, proposals, rules, side


def at(d: date, h: int, m: int = 0) -> datetime:
    return datetime.combine(d, time(h, m), NY)


class EdgarAndTickerFixes(unittest.TestCase):
    def test_html_to_text_recovers_after_display_none_div_and_void_tags(self):
        html = (
            "<html><head><meta charset='utf-8'><title>8-K</title></head>"
            "<body>"
            "<div style='display: none'><ix:header><ix:hidden><div>hidden nested</div></ix:hidden></ix:header></div>"
            "<p>Revenue rose 22% year over year to $1.4 billion.<br>Guidance raised.</p>"
            "</body></html>"
        )
        text = edgar.html_to_text(html)
        self.assertIn("Revenue rose 22% year over year", text)
        self.assertIn("Guidance raised.", text)
        self.assertNotIn("hidden nested", text)

    def test_cached_doc_text_refetches_empty_cache_and_never_caches_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(edgar, "_cache_dir", return_value=root):
                doc_path = root / "docs" / "0001_ex991.htm.txt"
                doc_path.parent.mkdir(parents=True, exist_ok=True)
                doc_path.write_text("")  # corrupted 0-byte file from earlier bug
                calls = []

                def fake_fetch(url):
                    calls.append(url)
                    return b"<html><body><p>Actual press release body.</p></body></html>"

                text = edgar._cached_doc_text(1, "0001", "ex991.htm", "https://www.sec.gov/x", 5000, fetcher=fake_fetch)
                self.assertEqual(len(calls), 1)
                self.assertIn("Actual press release body.", text)
                self.assertIn("Actual press release body.", doc_path.read_text())

                # Empty response must not be cached on disk
                empty_path = root / "docs" / "0002_empty.htm.txt"
                out = edgar._cached_doc_text(1, "0002", "empty.htm", "https://www.sec.gov/e", 5000, fetcher=lambda _: b"<div></div>")
                self.assertEqual(out, "")
                self.assertFalse(empty_path.exists())

    def test_share_class_symbols_work_across_journal_bars_edgar_broker_gateway(self):
        for sym in ("BRK B", "BF B", "MOG A", "BRK.B"):
            self.assertIsNotNone(journal.SYMBOL.match(sym))
        self.assertEqual(bars._ticker("BRK B"), "BRK-B")
        self.assertEqual(bars._ticker("BRK.B"), "BRK-B")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(edgar, "_cache_dir", return_value=Path(tmp)):
                ciks = edgar.ticker_ciks(fetcher=lambda _: json.dumps({"0": {"ticker": "BRK-B", "cik_str": 1067983}}).encode())
                self.assertEqual(ciks["BRK B"], 1067983)
                self.assertEqual(ciks["BRK.B"], 1067983)
                self.assertEqual(ciks["BRK-B"], 1067983)

        # gateway._contracts preserves caller's symbol ("BRK B" or "BRK.B")
        m = gateway.IBMarket.__new__(gateway.IBMarket)

        class FakeIB:
            async def qualifyContractsAsync(self, *contracts):
                return [N(symbol=c.symbol, conId=42, secType="STK") for c in contracts]

        m.ib = FakeIB()
        out_space = asyncio.run(m._contracts(["BRK B"], overnight=False))
        out_dot = asyncio.run(m._contracts(["BRK.B"], overnight=False))
        self.assertIn("BRK B", out_space)
        self.assertIn("BRK.B", out_dot)


class RawCloseAdjustmentFixes(unittest.TestCase):
    def test_bars_frame_stores_raw_close_and_merge_recent_backfills_legacy_schema(self):
        idx = pd.to_datetime(["2026-11-23", "2026-11-24", "2026-11-25"])
        raw = pd.DataFrame({
            "Open": [100.0, 102.0, 104.0],
            "High": [101.0, 103.0, 105.0],
            "Low": [99.0, 101.0, 103.0],
            "Close": [100.0, 102.0, 104.0],
            "Adj Close": [95.0, 96.9, 98.8],
            "Volume": [1000.0, 1000.0, 1000.0],
        }, index=idx)
        df = bars._frame("DIV", raw, repair=False)
        self.assertIn("raw_close", df.columns)
        self.assertAlmostEqual(df["raw_close"].iloc[-1], 104.0)
        self.assertAlmostEqual(df["close"].iloc[-1], 98.8)

        # Merging into an older parquet without raw_close must backfill cleanly with no NaNs
        legacy = pd.DataFrame({
            "symbol": ["DIV", "DIV"],
            "date": pd.to_datetime(["2026-11-20", "2026-11-23"]),
            "open": [94.0, 95.0], "high": [95.0, 95.95], "low": [93.0, 94.05],
            "close": [94.0, 95.0], "volume": [1000.0, 1000.0],
        })
        merged = bars._merge_recent(df, legacy)
        self.assertIsNotNone(merged)
        self.assertFalse(merged["raw_close"].isna().any())

    def test_learn_and_holds_are_immune_to_retroactive_dividend_adjustment(self):
        # Stock trades at unadjusted 100 on Nov 17 -> 102 on Nov 18 (real return +2.00%).
        # Later a 5% dividend adjusts historical closes by 0.95 (close=95 -> 96.9, raw_close=100 -> 102).
        # Pass mark recorded at entry is unadjusted 100.0. Must still score +200 bp, NOT -310 bp!
        px = pd.DataFrame([
            {"symbol": "AAA", "date": pd.Timestamp("2026-11-16"), "open": 95.0, "close": 95.0, "raw_close": 100.0},
            {"symbol": "AAA", "date": pd.Timestamp("2026-11-17"), "open": 95.0, "close": 96.9, "raw_close": 102.0},
            {"symbol": "SPY", "date": pd.Timestamp("2026-11-16"), "open": 500.0, "close": 500.0, "raw_close": 500.0},
            {"symbol": "SPY", "date": pd.Timestamp("2026-11-17"), "open": 500.0, "close": 500.0, "raw_close": 500.0},
        ])
        prop = {
            "id": "2026-11-17-P01", "session": "2026-11-17", "pass": "night",
            "long": ["AAA"], "short": [], "plan": {"horizon_days": 1, "stop": {"spread_pct": -5.0}},
        }
        mark_doc = {"at": at(date(2026, 11, 16), 21, 0).isoformat(), "prices": {"AAA": 100.0, "SPY": 500.0}}
        with mock.patch.object(entries, "marks", return_value=mark_doc), \
                mock.patch.object(journal, "_bars_settled", return_value=True):
            res = learn._mark_outcome(prop, px, ix=None, today=date(2026, 11, 20))
        self.assertAlmostEqual(res["bps"], 200.0, places=1)

        # Also check holds._path gives +200 bp!
        px_map = {
            "AAA": {date(2026, 11, 16): (95.0, 95.0, 100.0), date(2026, 11, 17): (95.0, 96.9, 102.0)},
        }
        with mock.patch.object(entries, "marks", return_value=mark_doc):
            held, stopped, src = holds._path(prop, px_map, lambda _: True, today=date(2026, 11, 20))
        self.assertEqual(src, "pass_mark")
        self.assertAlmostEqual(held[0], 200.0, places=1)
        self.assertAlmostEqual(stopped[0], 200.0, places=1)


class ExecutionAndRiskFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for p in (
            mock.patch.object(journal, "JOURNAL", self.root),
            mock.patch.object(book, "ROOT", self.root / "paper"),
            mock.patch.object(entries.asyncio, "sleep", mock.AsyncMock()),
            mock.patch.object(entries, "_tell"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_partial_fill_stop_uses_filled_notional(self):
        trade = {
            "id": "T1", "notional": 10_000,
            "stop": {"spread_pct": -5.0},
            "legs": [{"symbol": "AAA", "side": "long", "shares": 100, "ref_price": 100.0}],
        }
        # Only 50 of 100 shares filled ($5,000 actual notional). Stock drops -5.2% to $94.80 (-$260).
        # Against planned $10,000 that looked like -2.6% (missed stop!); against filled $5,000 it IS -5.2%!
        held = [{"symbol": "AAA", "side": "long", "shares": 50, "entry_price": 100.0}]
        reason = rules.stop_reason(trade, held, {"AAA": 94.80}, "10:00 AM CT")
        self.assertIsNotNone(reason)
        self.assertIn("5.2%", reason)

    def test_apply_caps_includes_proposal_id_and_unfilled_reason_does_not_cross_contaminate(self):
        open_t = [{"id": "O1", "legs": [{"symbol": "XXX", "side": "long", "shares": 100, "ref_price": 100.0}]}]
        new_t = [{"id": "N1", "proposal_id": "P01", "legs": [{"symbol": "YYY", "side": "long", "shares": 100, "ref_price": 100.0}]}]
        kept, skipped = rules.apply_caps(new_t, open_t, max_gross=15_000, max_symbol=50_000)
        self.assertEqual(kept, [])
        self.assertEqual(skipped[0]["proposal_id"], "P01")

        # P02 (same pass) must NOT inherit P01's cap skip reason when proposal_id is present
        entry_doc = {"trades": [], "runs": [{"skipped": [{"name": "N1", "proposal_id": "P01", "pass": "night", "reason": "gross would be $20,000"}]}]}
        self.assertEqual(learn.unfilled_reason({"id": "P01", "pass": "night"}, entry_doc), "cap_skipped")
        self.assertEqual(learn.unfilled_reason({"id": "P02", "pass": "night"}, entry_doc), "other")

    def test_unfilled_intraday_exit_stays_open_for_retry(self):
        day = date(2026, 11, 17)
        # Seed an open trade with entry fills
        trade = {
            "id": "2026-11-17-S01", "book": "swing", "name": "AAA", "setup": "breakout", "direction": "long",
            "entry_date": day.isoformat(), "first_session": day.isoformat(), "horizon_days": 3, "notional": 10_000,
            "stop": {"spread_pct": -4.0}, "why": "x", "wrong_if": "y", "source": "s", "pass": "night", "lead": "AAA",
            "legs": [{"symbol": "AAA", "side": "long", "shares": 100, "ref_price": 100.0}],
        }
        book._write(entries.path(day), {
            "session": day.isoformat(), "handled": ["night"], "trades": [trade], "marks": {},
            "exits": [], "runs": [], "last_attempt": None,
        })
        book._write(book.ROOT / "fills" / f"{day}.json", {
            "session": day.isoformat(), "pulled_at": at(day, 9, 31).isoformat(),
            "fills": [{"order_ref": "2026-11-17-S01|AAA", "symbol": "AAA", "side": "BOT", "shares": 100.0,
                       "price": 100.0, "commission": 1.0, "time": at(day, 9, 31).isoformat(), "exec_id": "E1"}],
            "positions": {"AAA": 100.0}, "net_liquidation": 1_000_000.0, "marks": {},
        })
        # Broker rejects / fails to fill the intraday stop exit
        class RejectExitBroker:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def cancel(self, ids): return 0
            async def place(self, orders):
                return [{**o, "order_id": 1, "status": "Cancelled", "note": "rejected"} for o in orders]
            async def fills(self): return []

        async def q(syms, overnight):
            return {s: (94.0, 94.1) for s in syms}, {}

        rec = asyncio.run(entries.run(
            now=at(day, 10, 0), broker=RejectExitBroker(),
            quotes=q, stop_prices=lambda syms: {s: 94.0 for s in syms},
        ))
        self.assertEqual(rec["exits"], [])
        self.assertTrue(any("filled nothing" in w for w in rec["warnings"]))
        # Trade MUST remain "open", not stuck in "closing"
        saved = entries.load(day)
        self.assertEqual(saved["exits"], [])
        self.assertEqual(book.trades()[0]["state"], "open")

    def test_overnight_wants_out_does_not_spin_and_holiday_eve_has_no_overnight_venue(self):
        # Wednesday evening before Thanksgiving 2026-11-26 -> no overnight venue!
        self.assertIsNone(entries.venue(at(date(2026, 11, 25), 21, 0)))
        # Thanksgiving evening before Black Friday 2026-11-27 -> overnight venue IS open!
        self.assertEqual(entries.venue(at(date(2026, 11, 26), 21, 0)), "overnight")

        # Overnight run with a review exit (wants_out) and no pending entries must exit early with "nothing new"
        day = date(2026, 11, 17)
        trade = {
            "id": "2026-11-16-S01", "book": "swing", "name": "AAA", "setup": "breakout", "direction": "long",
            "entry_date": "2026-11-16", "first_session": "2026-11-16", "horizon_days": 3, "notional": 10_000,
            "stop": {"spread_pct": -4.0}, "why": "x", "wrong_if": "y", "source": "s", "pass": "night", "lead": "AAA",
            "legs": [{"symbol": "AAA", "side": "long", "shares": 100, "ref_price": 100.0}],
        }
        book._write(entries.path(date(2026, 11, 16)), {
            "session": "2026-11-16", "handled": ["night"], "trades": [trade], "marks": {},
            "exits": [], "runs": [], "last_attempt": None,
        })
        book._write(book.ROOT / "fills" / "2026-11-16.json", {
            "session": "2026-11-16", "pulled_at": at(date(2026, 11, 16), 16, 20).isoformat(),
            "fills": [{"order_ref": "2026-11-16-S01|AAA", "symbol": "AAA", "side": "BOT", "shares": 100.0,
                       "price": 100.0, "commission": 1.0, "time": at(date(2026, 11, 16), 10, 0).isoformat(), "exec_id": "E1"}],
            "positions": {"AAA": 100.0}, "net_liquidation": 1_000_000.0, "marks": {},
        })
        book._write(entries.path(day), {
            "session": day.isoformat(), "handled": ["night"], "trades": [], "marks": {},
            "exits": [], "runs": [], "last_attempt": at(date(2026, 11, 16), 21, 0).isoformat(),
        })
        book._write(book.ROOT / "proposals" / f"{day}.json", {
            "session": day.isoformat(), "passes": [{"pass": "night", "at": at(date(2026, 11, 16), 20, 45).isoformat()}], "proposals": [],
        })
        book._write(book.ROOT / "pass_reviews" / f"{day}.json", {
            "session": day.isoformat(), "passes": [{"pass": "night", "reviews": [{"trade_id": "2026-11-16-S01", "action": "exit", "reason": "broken"}]}],
        })
        rec = asyncio.run(entries.run(now=at(date(2026, 11, 16), 22, 0), dry=True))
        self.assertEqual(rec["note"], "nothing new")

    def test_backup_close_does_not_crash_on_existing_dead_exits_and_late_limit_skips_wide_spread(self):
        from factor_rotation import cli
        day = date(2026, 11, 27)
        # Existing exits file with a DEAD order: backup close trigger must exit cleanly without FileExistsError
        book._write(book.ROOT / "exits" / f"{day}.json", {
            "session": day.isoformat(), "exits": [{"trade_id": "T1", "reason": "time limit"}],
            "orders": [{"trade_id": "T1", "symbol": "AAA", "kind": "MOC", "status": "Inactive"}],
        })
        with mock.patch.object(cli, "_today", return_value=day), \
                mock.patch.object(cli, "datetime") as dt, \
                mock.patch.object(book, "close_session") as cs:
            dt.now.return_value = at(day, 12, 46)
            dt.combine = datetime.combine
            cli._paper(N(job="close", execute=True, flatten=False, args=[]))
        cs.assert_not_called()

        # late_limit skips broken wide spread (> 2%) instead of crossing a stub quote
        day2 = date(2026, 11, 30)
        trade = {
            "id": "2026-11-25-S01", "book": "swing", "name": "AAA", "setup": "breakout", "direction": "long",
            "entry_date": "2026-11-25", "first_session": "2026-11-25", "horizon_days": 1, "notional": 10_000,
            "stop": {"spread_pct": -5.0}, "why": "x", "wrong_if": "y", "source": "s", "pass": "night", "lead": "AAA",
            "legs": [{"symbol": "AAA", "side": "long", "shares": 100, "ref_price": 100.0}],
        }
        book._write(entries.path(date(2026, 11, 25)), {
            "session": "2026-11-25", "handled": ["night"], "trades": [trade], "marks": {},
            "exits": [], "runs": [], "last_attempt": None,
        })
        book._write(book.ROOT / "fills" / "2026-11-25.json", {
            "session": "2026-11-25", "pulled_at": at(date(2026, 11, 25), 16, 20).isoformat(),
            "fills": [{"order_ref": "2026-11-25-S01|AAA", "symbol": "AAA", "side": "BOT", "shares": 100.0,
                       "price": 100.0, "commission": 1.0, "time": at(date(2026, 11, 25), 10, 0).isoformat(), "exec_id": "E1"}],
            "positions": {"AAA": 100.0}, "net_liquidation": 1_000_000.0, "marks": {},
        })

        class FakeBroker:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def fills(self): return []
            async def positions(self): return {"AAA": 100.0}
            async def place(self, orders): return orders
            async def cancel(self, ids): return 0

        async def wide_q(syms, overnight):
            return {s: (90.0, 110.0) for s in syms}, {}  # 20% stub spread

        with mock.patch.object(book, "quotes", return_value={"AAA": 100.0}):
            out = asyncio.run(book._close_once(day2, book.ROOT / "exits" / f"{day2}.json",
                                               broker=FakeBroker(), dry=False, late_limit=True, quotes_fn=wide_q))
        self.assertEqual(out["orders"], [])
        self.assertTrue(any("spread 20.0% > 2%" in w for w in out["warnings"]))

    def test_portfolio_all_excludes_retired_small_book(self):
        day = date(2026, 11, 23)
        book._write(book.ROOT / "book.json", {"started": "2026-11-15", "capital": {"swing": 1_000_000}})
        book._write(entries.path(day), {
            "session": day.isoformat(), "handled": ["night"],
            "trades": [
                {"id": "2026-11-23-S01", "book": "dynamic", "name": "AAA", "setup": "breakout", "direction": "long",
                 "entry_date": day.isoformat(), "horizon_days": 3, "notional": 10_000, "why": "x", "wrong_if": "y", "source": "s",
                 "legs": [{"symbol": "AAA", "side": "long", "shares": 100, "ref_price": 100.0}]},
                {"id": "2026-11-23-M01", "book": "small", "name": "BBB", "setup": "breakout", "direction": "long",
                 "entry_date": day.isoformat(), "horizon_days": 3, "notional": 500, "why": "x", "wrong_if": "y", "source": "s",
                 "legs": [{"symbol": "BBB", "side": "long", "shares": 5, "ref_price": 100.0}]},
            ],
            "marks": {}, "exits": [], "runs": [], "last_attempt": None,
        })
        book._write(book.ROOT / "fills" / f"{day}.json", {
            "session": day.isoformat(), "pulled_at": at(day, 16, 20).isoformat(),
            "fills": [
                {"order_ref": "2026-11-23-S01|AAA", "symbol": "AAA", "side": "BOT", "shares": 100.0, "price": 100.0, "commission": 1.0, "time": at(day, 10, 0).isoformat(), "exec_id": "E1"},
                {"order_ref": "2026-11-23-M01|BBB", "symbol": "BBB", "side": "BOT", "shares": 5.0, "price": 100.0, "commission": 1.0, "time": at(day, 10, 0).isoformat(), "exec_id": "E2"},
            ],
            "positions": {"AAA": 100.0}, "net_liquidation": 1_000_000.0, "marks": {},
        })
        all_fills = portfolio.fills("all")
        self.assertEqual([f["symbol"] for f in all_fills], ["AAA"])


class HalfDayAndCalendarFixes(unittest.TestCase):
    def test_black_friday_half_day_across_journal_live_proposals_routine(self):
        bf = date(2026, 11, 27)  # Black Friday: 1:00 PM ET close
        self.assertTrue(journal.is_half_day(bf))
        self.assertEqual(live.session_minutes(bf), 210)

        # journal._bars_settled: 1:20 PM ET on Black Friday IS settled (15 min after 1:00 PM ET close)
        with mock.patch.object(journal.storage, "modified", return_value=at(bf, 13, 20)):
            self.assertTrue(journal._bars_settled(bf))

        # live.snapshot: bar at 12:55 PM ET ends at 1:00 PM ET (fraction = 1.0); bar at 1:05 PM ET is post!
        idx = pd.DatetimeIndex([at(bf, 9, 30), at(bf, 12, 55), at(bf, 13, 5)])
        df = pd.DataFrame({"Open": [10, 11, 11.2], "High": [11, 11.5, 11.3], "Low": [9.9, 10.9, 11.1],
                           "Close": [10.5, 11.1, 11.25], "Volume": [100, 200, 0]}, index=idx)
        snap = live.snapshot(df)
        self.assertEqual(snap["phase"], "post")
        self.assertEqual(snap["regular"]["minutes"], 210)
        self.assertEqual(snap["regular"]["fraction"], 1.0)

        # proposals.pass_session rejects midday on Black Friday
        with self.assertRaises(ValueError):
            proposals.pass_session("midday", at(bf, 13, 0))

        # routine.check marks pass-midday off and schedules paper-close at 11:45 AM CT (12:45 PM ET)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(journal, "JOURNAL", root), \
                    mock.patch.object(routine, "LOGS", root / "logs"), \
                    mock.patch.object(routine.storage, "modified", return_value=None):
                rows = {r["key"]: r for r in routine.check(bf, at(bf, 12, 50))}
                self.assertEqual(rows["pass-midday"]["state"], "off")
                self.assertEqual(rows["paper-close"]["time"], "11:41 AM CT")
                self.assertEqual(rows["paper-close"]["state"], "running")
                self.assertEqual(rows["paper-sync"]["time"], "12:20 PM CT")

                # Thanksgiving eve (Wed Nov 25): pass-night must be "off" because tomorrow is Thanksgiving
                eve = date(2026, 11, 25)
                eve_rows = {r["key"]: r for r in routine.check(eve, at(eve, 21, 0))}
                self.assertEqual(eve_rows["pass-night"]["state"], "off")

    def test_reports_inside_accounts_for_morning_first_session_shift(self):
        # Morning pass on Tuesday Nov 17 with horizon=2 holds through Thu Nov 19 close (first_session=Wed Nov 18).
        # An earnings report AMC Wed Nov 18 (trades Thu Nov 19) is inside the 2-session hold!
        fake_earnings = {"names": {"AAA": [{"date": "2026-11-18", "session": "after_close"}]}}
        with mock.patch.object(proposals.earnings, "load", return_value=fake_earnings):
            hits = proposals.reports_inside(["AAA"], date(2026, 11, 17), horizon=2, kind="morning")
            self.assertEqual(len(hits), 1)
            self.assertIn("AAA", hits[0])


class IsolationDepthQuotesSideXfeedStatsFixes(unittest.TestCase):
    def test_passes_disallowed_blocks_secrets_and_scorecards(self):
        self.assertIn("Read(.env)", passes.DISALLOWED)
        self.assertIn("Read(**/lessons.md)", passes.DISALLOWED)
        self.assertIn("Bash(uv run fr paper learn*)", passes.DISALLOWED)
        self.assertIn("Bash(uv run fr paper enter*)", passes.DISALLOWED)

    def test_passes_skips_already_recorded_and_logs_early_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jroot = root / "journal"
            logs = root / "pass-logs"
            prop = jroot / "paper" / "proposals" / "2026-11-27.json"
            prop.parent.mkdir(parents=True, exist_ok=True)
            prop.write_text(json.dumps({"session": "2026-11-27", "passes": [{"pass": "morning", "at": "2026-11-27T10:00:00-05:00"}], "proposals": []}))
            with mock.patch.object(journal, "JOURNAL", jroot), \
                    mock.patch.object(passes, "LOGS", logs), \
                    mock.patch("factor_rotation.watch.send") as send, \
                    mock.patch.object(passes, "subprocess") as sub:
                # Already recorded -> returns ok=True without alerting or invoking Claude
                with mock.patch.object(passes, "datetime") as dt:
                    dt.now.return_value = at(date(2026, 11, 27), 10, 15)
                    out = passes.run("morning")
                self.assertTrue(out["ok"])
                self.assertTrue(out["already_recorded"])
                send.assert_not_called()
                sub.run.assert_not_called()
                # Outside window -> writes failure log to data/pass-logs so it is never invisible
                with mock.patch.object(passes, "datetime") as dt, \
                        mock.patch.object(passes, "binary", return_value="/bin/echo"), \
                        mock.patch.dict("os.environ", {"FR_CLAUDE_TOKEN": "tok"}):
                    dt.now.return_value = at(date(2026, 11, 27), 7, 45)
                    fail_out = passes._run("morning")
                self.assertFalse(fail_out["ok"])
                self.assertIn("not the time", fail_out["error"])
                self.assertTrue((logs / "2026-11-27-morning.json").exists())

    def test_depth_ignores_failed_tool_calls_index_md_and_duplicate_packet_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "agent-1.jsonl"
            rows = [
                {"type": "user", "message": {"content": "NAMES: AAA\nResearch AAA."}},
                {"type": "assistant", "message": {"model": depth.AGENT_MODEL, "content": [
                    {"type": "tool_use", "id": "t1", "name": "WebFetch", "input": {"url": "https://blocked.example"}},
                    {"type": "tool_use", "id": "t2", "name": "WebFetch", "input": {"url": "https://ok.example"}},
                    {"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": "data/pass-packets/2026-11-25-night/INDEX.md"}},
                    {"type": "tool_use", "id": "t4", "name": "Read", "input": {"file_path": "data/pass-packets/2026-11-25-night/AAA.md"}},
                    {"type": "tool_use", "id": "t5", "name": "Read", "input": {"file_path": "data/pass-packets/2026-11-25-night/AAA.md"}},
                ]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "403 Forbidden"},
                    {"type": "tool_result", "tool_use_id": "t2", "is_error": False, "content": "OK"},
                ]}},
            ]
            p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            a = depth._agent(p)
            # t1 failed (ignored), t2 ok (1 read), t3 INDEX.md (ignored), t4 AAA.md (1 packet read), t5 dup AAA.md (ignored)
            self.assertEqual(a["reads"], 2)
            self.assertEqual(a["packet_reads"], 1)

    def test_quotes_renders_after_hours_prices(self):
        fake = {
            "NVDA": {
                "regularMarketPrice": 140.0, "regularMarketPreviousClose": 138.0, "regularMarketChangePercent": 1.45,
                "regularMarketTime": int(at(date(2026, 11, 25), 16, 0).timestamp()),
                "marketState": "POST", "postMarketPrice": 147.0, "postMarketChangePercent": 5.0,
                "postMarketTime": int(at(date(2026, 11, 25), 17, 15).timestamp()),
            }
        }
        with mock.patch.object(bars, "_quotes", return_value=fake):
            rows = quotes.fetch(["NVDA"])
        self.assertEqual(rows[0]["post_market"], 147.0)
        self.assertIn("after-hours 147.00 (+5.00%)", quotes.text(rows))

    def test_side_pull_uses_yahoo_ticker_and_avoids_intraday_lookahead(self):
        seen = []

        class FakeTicker:
            def __init__(self, sym):
                seen.append(sym)
                self.info = {"quoteType": "EQUITY", "currentPrice": 120.0, "previousClose": 100.0, "targetMeanPrice": 110.0}
                self.eps_trend = None
                self.eps_revisions = None
                self.upgrades_downgrades = None
                self.insider_purchases = None

        with mock.patch("yfinance.Ticker", FakeTicker):
            got = side.pull("BRK B", at(date(2026, 11, 25), 11, 15))
        self.assertEqual(seen, ["BRK-B"])
        self.assertFalse(got["pre_open"])
        # Uses previousClose (100) instead of intraday currentPrice (120) -> target_gap = 110/100 - 1 = +0.10
        self.assertAlmostEqual(got["facts"]["target_gap"], 0.10, places=4)

    def test_xfeed_audit_continues_past_missing_profile_and_stops_on_rate_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handles_file = root / "handles.txt"
            handles_file.write_text("missing_user\ngood_user\n")

            def fake_html(s, h):
                return ""

            def fake_uid(s, guest, h):
                return None if h == "missing_user" else "999"

            def fake_call(s, guest, op, vars_, qids):
                return {"data": {"user": {"result": {"timeline": {"timeline": {"instructions": [
                    {"entries": [{"content": {"itemContent": {"tweet_results": {"result": {
                        "rest_id": "123", "details": {"full_text": "hello", "created_at_ms": 1764000000000},
                        "core": {"user_results": {"result": {"core": {"screen_name": "good_user"}}}},
                    }}}}}]}
                ]}}}}}}

            with mock.patch.object(xfeed, "root", return_value=root), \
                    mock.patch.object(xfeed, "_profile_html", fake_html), \
                    mock.patch.object(xfeed, "_user_id", fake_uid), \
                    mock.patch.object(xfeed, "_guest_call", fake_call):
                res = xfeed.audit(limit=10, pause=0.0, handles=handles_file)
            self.assertTrue(res["ok"])
            self.assertEqual(res["done"], 1)
            self.assertEqual(res["failed"], ["missing_user"])

    def test_learn_main_test_includes_hac_vs_spy_and_regime_split(self):
        rows = []
        for i, sess in enumerate(("2026-11-20", "2026-11-23", "2026-11-24", "2026-11-26", "2026-11-27")):
            rows.append({
                "id": f"{sess}-P01", "session": sess, "verdict": "take", "setup": "breakout", "direction": "long",
                "plan_bps": 200.0 + i * 10, "plan_mature": True, "default_plan_bps": 180.0 + i * 10,
                "default_plan_mature": True, "spy_bps": 50.0, "fill_status": "filled", "paper_bps": 170.0,
            })
            rows.append({
                "id": f"{sess}-P02", "session": sess, "verdict": "pass", "setup": "breakout", "direction": "long",
                "plan_bps": 20.0, "plan_mature": True, "default_plan_bps": 20.0,
                "default_plan_mature": True, "spy_bps": 50.0, "fill_status": None, "paper_bps": None,
            })
        mt = learn.main_test(rows)
        self.assertIsNotNone(mt["session_clustered"]["p_value_hac"])
        self.assertEqual(mt["vs_spy"]["n_takes"], 5)
        self.assertAlmostEqual(mt["vs_spy"]["plan_mean_bps"], 220.0 - 50.0 - learn.EST_COST_BPS, places=1)
        self.assertEqual(mt["regimes"]["pre_uninfluenced"]["n_takes"], 3)
        self.assertEqual(mt["regimes"]["uninfluenced"]["n_takes"], 2)
        rendered = "\n".join(learn._main_test_lines(mt))
        self.assertIn("Second test: takes vs SPY", rendered)
        self.assertIn("Regime split (default plan)", rendered)
        self.assertIn("HAC p=", rendered)

    def test_conviction_v2_p_win_validation_stamping_and_learn_metrics(self):
        from factor_rotation.paper import daily
        from factor_rotation import screen as screens

        day = date(2026, 11, 30)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sc = {
                "as_of": "2026-11-27", "session": day.isoformat(), "slot": "night", "live": True, "phase": "post",
                "prices_at": "2026-11-27T13:00:00-05:00",
                "candidates": [
                    {"name": "AAA", "kind": "stock", "setup": "earnings_gap_held", "direction": "long",
                     "why": "gap", "chart": "c", "numbers": {"atr_pct": 0.03}, "hedge": "XLK"},
                    {"name": "BBB", "kind": "stock", "setup": "turn", "direction": "long",
                     "why": "turn", "chart": "c", "numbers": {"atr_pct": 0.03}, "hedge": "XLK"},
                ],
                "in_book": [],
            }
            with mock.patch.object(journal, "JOURNAL", root), \
                    mock.patch.object(book, "ROOT", root / "paper"), \
                    mock.patch.object(proposals.earnings, "load", return_value={"names": {}}), \
                    mock.patch.object(proposals, "pair_stats", return_value=None):
                sp = screens.live_path(day, "night")
                sp.parent.mkdir(parents=True, exist_ok=True)
                sp.write_text(json.dumps(sc))

                def base_draft(p1, p2):
                    return {
                        "session": day.isoformat(), "pass": "night", "summary": "s", "market": {},
                        "proposals": [
                            {"candidate": "AAA", "rank": 1, "decision": "take", "conviction": 4, "reason": "strong",
                             "reasoning": {k: "x" for k in journal.REASONING}, "wrong_if": "w", "hedge_why": "h",
                             "plan": {"horizon_days": 3, "stop": {"spread_pct": -5.0}}, **p1},
                            {"candidate": "BBB", "rank": 2, "decision": "pass", "conviction": 2, "reason": "overlap",
                             "pass_case": {"case_for": "a", "case_against": "b", "why_not": "c"}, **p2},
                        ],
                        "reviews": [],
                    }

                sun = at(date(2026, 11, 29), 20, 45)
                # Missing, out-of-range, float, and bool p_win must all be refused
                for bad in ({}, {"p_win": 4}, {"p_win": 96}, {"p_win": 55.0}, {"p_win": True}):
                    with self.assertRaisesRegex(ValueError, "p_win"):
                        daily.record(base_draft(bad, {"p_win": 45}), now=sun)

                # Take under 50 (45) and pass over 50 (62, ranked below take) are both accepted and stamped scale=2 + judge
                with mock.patch.dict(os.environ, {"FR_JUDGE_MODEL": "claude-fable-5-2"}):
                    _, doc, _ = daily.record(base_draft({"p_win": 45}, {"p_win": 62}), now=sun)
                self.assertEqual([p["p_win"] for p in doc["proposals"]], [45, 62])
                self.assertEqual([p["scale"] for p in doc["proposals"]], [2, 2])
                self.assertEqual([p["judge"] for p in doc["proposals"]], ["claude-fable-5-2", "claude-fable-5-2"])
                self.assertEqual(doc["passes"][0]["scale"], 2)
                self.assertEqual(doc["passes"][0]["judge"], "claude-fable-5-2")

        # Verify learn.py scale-2 scoring, conviction gate, and collapse monitor
        synth = []
        for idx in range(30):
            sess = f"2026-12-{(idx % 15) + 1:02d}"
            synth.append({
                "id": f"T4-{idx}", "session": sess, "verdict": "take", "conviction": 4, "scale": 2,
                "p_win": 58 + (idx % 6), "rank": 1, "ranked": 4, "recorded_at": f"{sess}T21:00:00-05:00",
                "judge": "claude-fable-5-2", "plan_bps": 250.0, "default_plan_bps": 220.0, "plan_mature": True,
            })
            synth.append({
                "id": f"T3-{idx}", "session": sess, "verdict": "take", "conviction": 3, "scale": 2,
                "p_win": 50 + (idx % 5), "rank": 2, "ranked": 4, "recorded_at": f"{sess}T21:00:00-05:00",
                "judge": "claude-fable-5-2", "plan_bps": 80.0, "default_plan_bps": 70.0, "plan_mature": True,
            })
            synth.append({
                "id": f"P1-{idx}", "session": sess, "verdict": "pass", "conviction": 2, "scale": 2,
                "p_win": 42, "rank": 3, "ranked": 4, "recorded_at": f"{sess}T21:00:00-05:00",
                "judge": "claude-fable-5-2", "plan_bps": -40.0, "default_plan_bps": -40.0, "plan_mature": True,
            })
            synth.append({
                "id": f"P2-{idx}", "session": sess, "verdict": "pass", "conviction": 1, "scale": 2,
                "p_win": 32, "rank": 4, "ranked": 4, "recorded_at": f"{sess}T21:00:00-05:00",
                "judge": "claude-fable-5-2", "plan_bps": -120.0, "default_plan_bps": -120.0, "plan_mature": True,
            })
        pwt = learn.p_win_table(synth)
        self.assertEqual(pwt["all"]["n"], 120)
        self.assertGreater(pwt["all"]["auc"], 0.9)
        cg = learn.conviction_gate(synth)
        self.assertTrue(cg["due"])
        self.assertEqual(cg["verdict"], "keep")
        self.assertAlmostEqual(cg["gap_bps"], 170.0, places=1)
        # Collapsed all-3 / single-p_win monitor check
        collapsed = [{**r, "conviction": 3, "p_win": 55} for r in synth if r["verdict"] == "take"][:30]
        jm = learn.judge_monitor(collapsed)
        self.assertTrue(any("no 4 or 5" in f for f in jm["flags"]))
        self.assertTrue(any("p_win on takes has collapsed" in f for f in jm["flags"]))

class Audit1127(unittest.TestCase):
    def test_a_pass_numbers_its_trades_past_the_highest_id_in_use_not_the_count(self):
        from datetime import date as _date
        from unittest import mock as _mock
        from factor_rotation.paper import book as _book, entries as _entries, rules as _rules
        # The night pass kept S01 and S03 (the caps dropped S02). Counting would give the next pass S03 again.
        state = {"trades": [{"id": "2026-12-01-S01"}, {"id": "2026-12-01-S03"}], "marks": {"morning": {"prices": {}}}}
        seen = {}

        def proposal_trades(takes, day, prices, used, start, **kw):
            seen["start"] = start
            return [], []

        with _mock.patch.object(_book, "capital", return_value=1_000_000), \
                _mock.patch.object(_rules, "proposal_trades", side_effect=proposal_trades):
            _entries.make_trades(_book.MAIN, "morning", [{"id": "p4", "long": ["DDD"], "short": ["IWM"]}], _date(2026, 12, 1),
                                 state, [], lambda symbols: {}, {"new": [], "skipped": []}, None)
        self.assertEqual(seen["start"], 4)
        self.assertEqual(_entries._id_number("2026-11-25-S03"), 3)

    def test_an_overnight_sweep_with_no_spy_quote_counts_as_no_sweep(self):
        from factor_rotation import screen as _screen
        good = {"bid": 99.9, "ask": 100.1, "last": 100.0, "volume": 5000.0}
        blank = {"bid": None, "ask": None, "last": None, "volume": None}
        whole = {"at": "t", "quotes": {"AAA": good, "SPY": good}}
        self.assertIs(_screen._checked_sweep(whole), whole)
        cut = _screen._checked_sweep({"at": "t", "quotes": {"AAA": good, "SPY": blank, "XLK": blank}})
        self.assertNotIn("quotes", cut)
        self.assertIn("no usable SPY quote", cut["why"])
        self.assertIn("2 of 3", cut["why"])
        self.assertEqual(_screen._checked_sweep({"why": "down"}), {"why": "down"})


if __name__ == "__main__":
    unittest.main()

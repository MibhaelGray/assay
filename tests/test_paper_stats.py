"""Tests for look-ahead-free holds (real IBKR fills + pass marks + filled/unfilled split) and learn.main_test().

    uv run python -m unittest tests/test_paper_stats.py
"""

import unittest
from datetime import date, datetime, timedelta
from unittest import mock

import pandas as pd

from factor_rotation.config import NY
from factor_rotation.paper import entries, holds, learn, proposals

from test_paper_book import DAY, Case, fill, frame, pair


def _intraday(symbol: str, day: date, prices: list[tuple[int, int, float]]) -> pd.Series:
    """5-minute close series for `symbol` on `day` (NY time). Each tuple is (hour, minute, close)."""
    idx = pd.DatetimeIndex([datetime(day.year, day.month, day.day, h, m, tzinfo=NY) for h, m, _ in prices])
    return pd.Series([p for _, _, p in prices], index=idx)


class HoldsRealFills(Case):
    def setUp(self):
        super().setUp()
        self.bars = pd.DataFrame()
        self.ix: dict = {}
        for target, name, value in (
            (learn.bars, "load", lambda symbols: self.bars),
            (learn.bars, "load_intraday", lambda symbols: self.ix),
            (learn.journal, "_bars_settled", lambda d: True),
        ):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _seed_midday_pass(self, day: date, props: list[dict], marks: dict[str, float],
                          trades: list[dict] | None = None, runs: list[dict] | None = None,
                          fills_rows: list[dict] | None = None, exits_rows: list[dict] | None = None):
        self.write("proposals", day, {
            "session": day.isoformat(), "pass": "midday",
            "passes": [{"pass": "midday", "at": f"{day}T13:15:00-05:00"}],
            "proposals": props,
        })
        entries.path(day).parent.mkdir(parents=True, exist_ok=True)
        self.write("entries", day, {
            "session": day.isoformat(), "handled": ["midday"],
            "trades": trades or [], "exits": exits_rows or [], "runs": runs or [],
            "marks": {"midday": {"at": f"{day}T13:16:00-05:00", "prices": marks}},
        })
        if fills_rows is not None:
            self.write("fills", day, {"session": day.isoformat(), "fills": fills_rows, "positions": {}, "net_liquidation": 1e6})

    def test_midday_pass_has_no_session_open_lookahead_and_uses_real_ibkr_fills(self):
        # AAA opens at 100, rallies to 110 by midday, closes day 0 at 110, day 1 at 112.2 (+200 bp from 110).
        # Filled take entered at 110.50 on IBKR and exited at 112.20 (+153.8 bp gross minus $2 commission).
        # Pass BBB opens at 100, rallies to 110 by midday, closes day 0 at 110, day 1 at 110 (0 bp from 110, NOT +1000 bp!).
        day0 = date(2026, 11, 18)
        day1 = date(2026, 11, 19)
        props = [
            {"id": f"{day0}-P01", "session": day0.isoformat(), "pass": "midday", "name": "AAA", "kind": "stock",
             "setup": "live_move", "direction": "long", "long": ["AAA"], "short": ["HHH"], "decision": "take",
             "conviction": 3, "rank": 1, "ranked": 3, "reviewed": True, "source": "screen", "reason": "catalyst",
             "recorded_at": f"{day0}T13:15:00-05:00",
             "plan": {"horizon_days": 1, "stop": {"spread_pct": -5}},
             "signal": {"hedge": "HHH", "numbers": {"atr_pct": 0.03}}},
            {"id": f"{day0}-P02", "session": day0.isoformat(), "pass": "midday", "name": "CCC", "kind": "stock",
             "setup": "live_move", "direction": "long", "long": ["CCC"], "short": ["HHH"], "decision": "take",
             "conviction": 3, "rank": 2, "ranked": 3, "reviewed": True, "source": "screen", "reason": "wide spread",
             "recorded_at": f"{day0}T13:15:00-05:00",
             "plan": {"horizon_days": 1, "stop": {"spread_pct": -5}},
             "signal": {"hedge": "HHH", "numbers": {"atr_pct": 0.03}}},
            {"id": f"{day0}-P03", "session": day0.isoformat(), "pass": "midday", "name": "BBB", "kind": "stock",
             "setup": "live_move", "direction": "long", "long": ["BBB"], "short": ["HHH"], "decision": "pass",
             "conviction": 2, "rank": 3, "ranked": 3, "reviewed": True, "source": "screen", "reason": "chasing",
             "recorded_at": f"{day0}T13:15:00-05:00",
             "plan": {"horizon_days": 1, "stop": {"spread_pct": -5}},
             "signal": {"hedge": "HHH", "numbers": {"atr_pct": 0.03}}},
        ]
        t1 = {**pair("2026-11-18-S01", horizon=1), "entry_date": day0.isoformat(), "first_session": day1.isoformat(),
              "proposal_id": f"{day0}-P01", "notional": 11050.0,
              "legs": [{"symbol": "AAA", "side": "long", "shares": 100, "ref_price": 110.0},
                       {"symbol": "HHH", "side": "short", "shares": 110, "ref_price": 100.0}]}
        t2 = {**pair("2026-11-18-S02", horizon=1), "name": "CCC", "entry_date": day0.isoformat(),
              "proposal_id": f"{day0}-P02", "stood_down": "passed: spread wider than 0.5% (CCC 4.0%)"}
        entry_fills = [
            fill("2026-11-18-S01", "AAA", "BOT", 100, price=110.50, n="1"),
            fill("2026-11-18-S01", "HHH", "SLD", 110, price=100.00, n="2"),
            fill("2026-11-18-S01", "AAA", "SLD", 100, price=112.20, n="3"),
            fill("2026-11-18-S01", "HHH", "BOT", 110, price=100.00, n="4"),
        ]
        self._seed_midday_pass(
            day0, props, {"AAA": 110.0, "BBB": 110.0, "CCC": 110.0, "HHH": 100.0, "SPY": 500.0},
            trades=[t1, t2], fills_rows=entry_fills,
            exits_rows=[{"trade_id": "2026-11-18-S01", "book": "swing", "reason": "time limit: session 1 of 1"}],
        )
        # Daily bars: day0 open=100, close=110 (big morning rally BEFORE midday pass); day1 close: AAA=112.2, BBB=110.0, CCC=115.5
        self.bars = pd.DataFrame([
            {"symbol": "AAA", "date": pd.Timestamp(day0), "open": 100.0, "high": 111.0, "low": 100.0, "close": 110.0, "volume": 1e6},
            {"symbol": "AAA", "date": pd.Timestamp(day1), "open": 110.0, "high": 113.0, "low": 109.0, "close": 112.2, "volume": 1e6},
            {"symbol": "BBB", "date": pd.Timestamp(day0), "open": 100.0, "high": 111.0, "low": 100.0, "close": 110.0, "volume": 1e6},
            {"symbol": "BBB", "date": pd.Timestamp(day1), "open": 110.0, "high": 111.0, "low": 109.0, "close": 110.0, "volume": 1e6},
            {"symbol": "CCC", "date": pd.Timestamp(day0), "open": 100.0, "high": 111.0, "low": 100.0, "close": 110.0, "volume": 1e6},
            {"symbol": "CCC", "date": pd.Timestamp(day1), "open": 110.0, "high": 116.0, "low": 110.0, "close": 115.5, "volume": 1e6},
            {"symbol": "HHH", "date": pd.Timestamp(day0), "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1e6},
            {"symbol": "HHH", "date": pd.Timestamp(day1), "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1e6},
            {"symbol": "SPY", "date": pd.Timestamp(day0), "open": 500.0, "high": 500.0, "low": 500.0, "close": 500.0, "volume": 1e6},
            {"symbol": "SPY", "date": pd.Timestamp(day1), "open": 500.0, "high": 500.0, "low": 500.0, "close": 500.0, "volume": 1e6},
        ])

        by_name = {r["name"]: r for r in holds.rows()}
        # BBB (pass): anchored at midday mark 110.0, NOT day0 open 100.0 -> 0.0 bp on day1
        self.assertEqual(by_name["BBB"]["entry_source"], "pass_mark")
        self.assertEqual(by_name["BBB"]["held"], [0.0])
        # AAA (filled take): anchored at real IBKR fill 110.50 -> (112.2/110.5 - 1) * 1e4 = +153.8 bp
        self.assertEqual(by_name["AAA"]["entry_source"], "ibkr_fill")
        self.assertTrue(by_name["AAA"]["filled"])
        self.assertAlmostEqual(by_name["AAA"]["held"][0], 153.8, places=1)
        # CCC (unfilled take): anchored at pass mark 110.0 -> +500.0 bp, tagged as spread_liquidity
        self.assertFalse(by_name["CCC"]["filled"])
        self.assertEqual(by_name["CCC"]["unfilled_reason"], "spread_liquidity")
        self.assertAlmostEqual(by_name["CCC"]["held"][0], 500.0, places=1)

        t = holds.table()["so_far"]
        self.assertTrue(t["has_fills"])
        self.assertEqual(t["unfilled_reasons"], {"spread_liquidity": 1})
        self.assertEqual(t["held"]["filled takes"][1]["n"], 1)
        self.assertEqual(t["held"]["unfilled takes"][1]["n"], 1)
        rep = holds.report()
        self.assertIn("filled takes", rep)
        self.assertIn("unfilled takes", rep)
        self.assertIn("spread liquidity 1", rep)

    def test_midday_day0_intraday_stop_is_replayed_in_holds(self):
        day0 = date(2026, 11, 18)
        day1 = date(2026, 11, 19)
        props = [
            {"id": f"{day0}-P01", "session": day0.isoformat(), "pass": "midday", "name": "AAA", "kind": "stock",
             "setup": "live_move", "direction": "long", "long": ["AAA"], "short": ["HHH"], "decision": "pass",
             "conviction": 2, "rank": 1, "ranked": 1, "reviewed": True, "source": "screen", "reason": "test",
             "recorded_at": f"{day0}T13:15:00-05:00",
             "plan": {"horizon_days": 2, "stop": {"spread_pct": -5}},
             "signal": {"hedge": "HHH", "numbers": {"atr_pct": 0.03}}},
        ]
        self._seed_midday_pass(day0, props, {"AAA": 100.0, "HHH": 100.0, "SPY": 500.0})
        self.bars = pd.DataFrame([
            {"symbol": "AAA", "date": pd.Timestamp(day0), "open": 100.0, "high": 100.0, "low": 93.0, "close": 98.0, "volume": 1e6},
            {"symbol": "AAA", "date": pd.Timestamp(day1), "open": 98.0, "high": 105.0, "low": 98.0, "close": 104.0, "volume": 1e6},
            {"symbol": "HHH", "date": pd.Timestamp(day0), "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1e6},
            {"symbol": "HHH", "date": pd.Timestamp(day1), "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1e6},
            {"symbol": "SPY", "date": pd.Timestamp(day0), "open": 500.0, "high": 500.0, "low": 500.0, "close": 500.0, "volume": 1e6},
            {"symbol": "SPY", "date": pd.Timestamp(day1), "open": 500.0, "high": 500.0, "low": 500.0, "close": 500.0, "volume": 1e6},
        ])
        # Intraday drop to 94 (-6%, past -5% stop) at 2:00 PM ET on day0, then rebounds by close to 98 and day1 to 104
        self.ix = {
            "AAA": _intraday("AAA", day0, [(13, 55, 94.0), (15, 55, 98.0)]),
            "HHH": _intraday("HHH", day0, [(13, 55, 100.0), (15, 55, 100.0)]),
        }
        row = holds.rows()[0]
        self.assertEqual(row["held"], [400.0])
        self.assertEqual(row["stopped"], [-600.0])


class MainStatTest(unittest.TestCase):
    def test_student_t_pvalue_matches_known_critical_values(self):
        self.assertAlmostEqual(learn._t_pvalue(2.015, 5), 0.05, places=3)
        self.assertEqual(learn._t_pvalue(0.0, 5), 0.5)
        self.assertAlmostEqual(learn._t_pvalue(-2.015, 5), 0.95, places=3)
        self.assertLess(learn._t_pvalue(4.0, 10), 0.002)
        self.assertIsNone(learn._t_pvalue(None, 5))

    def test_rank_table_clusters_multiple_passes_on_the_same_session(self):
        # Two passes (night & midday) on the same session must count as 1 session (`nights=1`), not 2.
        rows = []
        for rec, bps_list in (("2026-11-18T20:00:00-05:00", [400, 300, 200, 100]),
                              ("2026-11-19T12:00:00-05:00", [100, 200, 300, 400])):
            for i, bps in enumerate(bps_list, 1):
                rows.append({"session": "2026-11-19", "recorded_at": rec, "rank": i, "ranked": 4,
                             "plan_status": "time", "plan_bps": float(bps), "plan_mature": True})
        rk = learn.rank_table(rows)
        self.assertEqual((rk["nights"], rk["passes"]), (1, 2))
        self.assertAlmostEqual(rk["ic"], 0.0, places=3)  # +1.0 and -1.0 averaged within the same session
        self.assertIsNone(rk["t"])

    def test_main_test_stratification_removes_stretched_mix_shift_and_checks_outliers(self):
        # Construct 4 sessions where:
        #   - Inside breakout/long, takes and passes BOTH average +100 bp (zero stock-picking skill!)
        #   - Many stretched/long passes average -200 bp (never taken)
        # Raw gap looks huge (+150 bp before cost), but stratified gap on identical default plan is 0 - cost!
        rows = []
        for idx, sess in enumerate(("2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19")):
            # 1 breakout long take (+100 default, +150 custom)
            rows.append({
                "session": sess, "verdict": "take", "setup": "breakout", "direction": "long",
                "plan_bps": 150.0, "plan_mature": True,
                "default_plan_bps": 100.0 + (400.0 if idx == 0 else 0.0), "default_plan_mature": True,
                "fill_status": "filled", "unfilled_reason": None, "paper_bps": 120.0,
            })
            # 1 breakout long pass (+100 default)
            rows.append({
                "session": sess, "verdict": "pass", "setup": "breakout", "direction": "long",
                "plan_bps": 100.0, "plan_mature": True,
                "default_plan_bps": 100.0, "default_plan_mature": True,
                "fill_status": None, "unfilled_reason": None, "paper_bps": None,
            })
            # 2 stretched long passes (-200 default)
            for _ in range(2):
                rows.append({
                    "session": sess, "verdict": "pass", "setup": "stretched", "direction": "long",
                    "plan_bps": -200.0, "plan_mature": True,
                    "default_plan_bps": -200.0, "default_plan_mature": True,
                    "fill_status": None, "unfilled_reason": None, "paper_bps": None,
                })
        mt = learn.main_test(rows, cost_bps=25.0)
        # Takes default avg = (500 + 100 + 100 + 100)/4 = 200; all passes avg = (100 - 200 - 200)/3 = -100 -> pooled default gap = +275 after 25 cost
        self.assertAlmostEqual(mt["default_gap_bps"], 275.0, places=1)
        # Stratified inside breakout/long: take avg 200 vs pass avg 100 -> +75 after 25 cost (mix shift from stretched removed!)
        self.assertEqual(mt["stratified"]["strata"], 1)
        self.assertAlmostEqual(mt["stratified"]["mean_bps"], 75.0, places=1)
        # Ex-top-2 removes the +500 outlier take -> remaining takes avg 100 vs passes avg -100 -> +175 after cost
        self.assertAlmostEqual(mt["outliers"]["ex_top_2_bps"], 175.0, places=1)
        self.assertEqual(mt["session_clustered"]["sessions"], 4)
        self.assertIsNotNone(mt["power"]["takes_for_200bp"])
        self.assertIn("UNDERPOWERED", mt["power"]["verdict"])
        self.assertEqual(mt["execution"]["filled_takes"], 4)
        self.assertEqual(mt["execution"]["unfilled_takes"], 0)

    def test_unfilled_reason_classifier(self):
        p = {"id": "2026-11-25-P03", "pass": "night"}
        self.assertEqual(learn.unfilled_reason(p, {"trades": [], "runs": [{"skipped": [{"name": "2026-11-25-S03", "pass": "night", "reason": "gross would be $1,035,089, over $1,000,000"}]}]}), "cap_skipped")
        self.assertEqual(learn.unfilled_reason(p, {"trades": [{"id": "T1", "proposal_id": p["id"], "stood_down": "passed: spread wider than 2%"}], "runs": []}), "spread_liquidity")
        self.assertEqual(learn.unfilled_reason(p, {"trades": [{"id": "T1", "proposal_id": p["id"]}], "runs": [{"warnings": ["T1: IBKR error 10089"]}]}), "gateway_fault")


if __name__ == "__main__":
    unittest.main()

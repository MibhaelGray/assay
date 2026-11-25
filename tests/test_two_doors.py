"""Tests for the Two-Doors screen (EDGAR filings, no-move-bar earnings, peer read-throughs),
the uninfluenced judge desk (`fr paper desk`), and owner-only by-route + hedged-vs-outright scoring."""

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from factor_rotation import edgar, passes, screen
from factor_rotation.config import NY
from factor_rotation.paper import book, daily, entries, learn, proposals
from test_paper_book import DAY, NEXT, Case, frame


FORM4_BUY_XML = b"""<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Jane Doe</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector><isOfficer>1</isOfficer><isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>Chief Financial Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-11-23</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>2500</value></transactionShares>
        <transactionPricePerShare><value>50.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-11-23</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>51.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


def quiet_bars(symbol: str, n: int = 260, end: str = "2026-11-24", last_ret: float = 0.0, seed: int = 1) -> pd.DataFrame:
    dates = pd.bdate_range(end=end, periods=n)
    rng = np.random.default_rng(seed)
    closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.004, n))
    closes[-1] = closes[-2] * (1.0 + last_ret)
    return pd.DataFrame({
        "date": dates, "symbol": symbol, "open": closes,
        "high": closes * 1.01, "low": closes * 0.99, "close": closes, "volume": np.full(n, 2e6),
    })


class EdgarClient(unittest.TestCase):
    def test_parse_form4_keeps_only_code_p_acquired(self):
        parsed = edgar.parse_form4(FORM4_BUY_XML)
        self.assertEqual(len(parsed["owners"]), 1)
        self.assertTrue(parsed["owners"][0]["officer"])
        self.assertEqual(len(parsed["buys"]), 1)
        self.assertAlmostEqual(parsed["buys"][0]["value"], 125_000.0)

    def test_fetch_recent_classifies_filings_and_caches_form4(self):
        tickers = {"0": {"ticker": "AAA", "cik_str": 1001}, "1": {"ticker": "BBB", "cik_str": 1002},
                   "2": {"ticker": "CCC", "cik_str": 1003}, "3": {"ticker": "DDD", "cik_str": 1004}}
        efts_8k = {"hits": {"total": {"value": 3}, "hits": [
            {"_id": "0001-26-000001:a8k.htm", "_source": {"sequence": 1, "ciks": ["0000001001"], "form": "8-K",
                                                          "items": ["4.02", "9.01"], "file_date": "2026-11-24", "adsh": "0001-26-000001"}},
            {"_id": "0001-26-000002:b202.htm", "_source": {"sequence": 1, "ciks": ["0000001002"], "form": "8-K",
                                                           "items": ["2.02", "9.01"], "file_date": "2026-11-24", "adsh": "0001-26-000002"}},
            {"_id": "0001-26-000003:routine.htm", "_source": {"sequence": 1, "ciks": ["0000001003"], "form": "8-K",
                                                              "items": ["7.01"], "file_date": "2026-11-24", "adsh": "0001-26-000003"}},
        ]}}
        efts_13d = {"hits": {"total": {"value": 3}, "hits": [
            {"_id": "0002-26-000001:c13d.htm", "_source": {"sequence": 1, "ciks": ["0000001003"], "form": "SCHEDULE 13D",
                                                           "items": [], "file_date": "2026-11-24", "adsh": "0002-26-000001"}},
            {"_id": "0002-26-000002:c13da.htm", "_source": {"sequence": 1, "ciks": ["0000001003"], "form": "SCHEDULE 13D/A",
                                                            "items": [], "file_date": "2026-11-24", "adsh": "0002-26-000002"}},
            {"_id": "0002-26-000003:dnt.htm", "_source": {"sequence": 1, "ciks": ["0000001004"], "form": "NT 10-Q",
                                                          "items": [], "file_date": "2026-11-24", "adsh": "0002-26-000003"}},
        ]}}
        efts_f4 = {"hits": {"total": {"value": 1}, "hits": [
            {"_id": "0003-26-000001:xslF345X06/form4.xml", "_source": {"sequence": 1, "ciks": ["0000009999", "0000001002"], "form": "4",
                                                                      "items": [], "file_date": "2026-11-24", "adsh": "0003-26-000001"}},
        ]}}

        def fake_fetch(url: str) -> bytes:
            if "company_tickers.json" in url:
                return json.dumps(tickers).encode()
            if "forms=8-K" in url:
                return json.dumps(efts_8k).encode()
            if "SC+13D" in url or "SC%2013D" in url:
                return json.dumps(efts_13d).encode()
            if "forms=4" in url:
                return json.dumps(efts_f4).encode()
            if url.endswith("/form4.xml"):
                return FORM4_BUY_XML
            raise AssertionError(f"unexpected url: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(edgar, "_cache_dir", return_value=Path(tmp)):
                res = edgar.fetch_recent(["AAA", "BBB", "CCC", "DDD"], date(2026, 11, 24), fetcher=fake_fetch)
        self.assertTrue(res["ok"])
        self.assertEqual(res["counts"], {"8k_material": 1, "8k_earnings": 1, "13d": 1, "late": 1, "form4_buys": 1})
        self.assertEqual([e["setup"] for e in res["by_symbol"]["AAA"]], ["edgar_8k"])
        self.assertEqual(sorted(e["setup"] for e in res["by_symbol"]["BBB"]), ["edgar_earnings", "edgar_insider_buy"])
        self.assertEqual([e["setup"] for e in res["by_symbol"]["CCC"]], ["edgar_13d"])   # 13D/A ignored
        self.assertEqual([e["setup"] for e in res["by_symbol"]["DDD"]], ["edgar_late_filing"])

    def test_fetch_recent_degrades_gracefully_on_error(self):
        def boom(_url):
            raise RuntimeError("SEC_USER_AGENT isn't set")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(edgar, "_cache_dir", return_value=Path(tmp)):
                res = edgar.fetch_recent(["AAA"], date(2026, 11, 24), fetcher=boom)
        self.assertFalse(res["ok"])
        self.assertEqual(res["by_symbol"], {})


class TwoDoorsScreen(unittest.TestCase):
    def test_door1_edgar_earnings_and_peer_readthrough_keep_full_technicals(self):
        df = pd.concat([quiet_bars("SPY", seed=1), quiet_bars("XLK", seed=2), quiet_bars("AAA", last_ret=-0.005, seed=3),
                        quiet_bars("BBB", last_ret=0.002, seed=4), quiet_bars("PEER", last_ret=0.001, seed=5)], ignore_index=True)
        edgar_events = {
            "ok": True, "window": ["2026-11-22", "2026-11-24"],
            "counts": {"8k_material": 1, "8k_earnings": 1, "13d": 0, "late": 0, "form4_buys": 0},
            "by_symbol": {
                "AAA": [{"setup": "edgar_8k", "direction": "short", "filed": "2026-11-24",
                         "url": "https://www.sec.gov/Archives/edgar/data/1/a/8k.htm",
                         "why": "filed 8-K on 2026-11-24: past financials can no longer be relied on (8-K 4.02)"}],
                "BBB": [{"setup": "edgar_earnings", "direction": "long", "filed": "2026-11-24",
                         "url": "https://www.sec.gov/Archives/edgar/data/2/b/202.htm",
                         "why": "filed 8-K Item 2.02 (earnings release) on 2026-11-24"}],
            },
        }
        baskets = {"baskets": [{"id": "b1", "name": "Test Software", "hedge": "XLK",
                                "members": [{"symbol": "AAA"}, {"symbol": "PEER"}, {"symbol": "BBB"}]}]}
        with mock.patch.object(screen, "basket_library", return_value=baskets), \
             mock.patch.object(screen.earnings, "load", return_value={"names": {}}), \
             mock.patch.object(screen, "_book_names", return_value={}):
            out = screen._run(df, edgar_events=edgar_events)
        by_name = {c["name"]: c for c in out["candidates"]}
        self.assertIn("AAA", by_name)
        self.assertEqual((by_name["AAA"]["door"], by_name["AAA"]["route"], by_name["AAA"]["setup"]),
                         ("disclosure", "edgar_filing", "edgar_8k"))
        self.assertEqual(by_name["AAA"]["primary_links"], ["https://www.sec.gov/Archives/edgar/data/1/a/8k.htm"])
        self.assertIn("ATR", by_name["AAA"]["chart"])          # full technicals still attached
        self.assertIn("rsi", by_name["AAA"]["numbers"])

        self.assertIn("BBB", by_name)
        self.assertEqual((by_name["BBB"]["door"], by_name["BBB"]["route"], by_name["BBB"]["setup"]),
                         ("disclosure", "earnings", "earnings_report"))
        self.assertEqual(by_name["BBB"]["primary_links"], ["https://www.sec.gov/Archives/edgar/data/2/b/202.htm"])

        self.assertIn("PEER", by_name)
        self.assertEqual((by_name["PEER"]["door"], by_name["PEER"]["route"], by_name["PEER"]["setup"]),
                         ("disclosure", "peer_readthrough", "peer_readthrough"))
        self.assertEqual(by_name["PEER"]["direction"], "short")
        self.assertIn("AAA", by_name["PEER"]["why"])
        self.assertIn("ATR", by_name["PEER"]["chart"])

    def test_route_of_maps_both_doors_cleanly(self):
        self.assertEqual(screen.route_of("edgar_8k", "stock"), ("disclosure", "edgar_filing"))
        self.assertEqual(screen.route_of("earnings_report", "stock"), ("disclosure", "earnings"))
        self.assertEqual(screen.route_of("peer_readthrough", "stock"), ("disclosure", "peer_readthrough"))
        self.assertEqual(screen.route_of("breakdown", "etf"), ("market_move", "macro_etf_shock"))
        self.assertEqual(screen.route_of("breakdown", "stock"), ("market_move", "volume_shock"))
        self.assertEqual(screen.route_of("stretched", "stock"), ("market_move", "chart_control"))
        self.assertEqual(screen.route_of("catalyst", "own"), ("own", "own"))


class CleanJudgeAndOwnerReport(Case):
    def test_passes_allow_desk_and_block_learn_and_lessons(self):
        self.assertIn("Bash(uv run fr paper desk)", passes.READING)
        self.assertNotIn("Bash(uv run fr paper learn)", passes.READING)
        self.assertNotIn("lessons.md", passes.HEADLESS.split("Never run")[0])

    def test_desk_shows_only_operational_state_and_learn_scores_by_route_and_mode(self):
        # Write a live screen with one Door 1 EDGAR candidate and one Door 2 stock candidate
        sc = {
            "as_of": "2026-11-06", "session": DAY.isoformat(), "slot": "night", "live": True,
            "prices_at": "2026-11-08T19:00:00-05:00", "phase": "post", "in_book": [],
            "candidates": [
                {"name": "AAA", "kind": "stock", "setup": "edgar_13d", "door": "disclosure", "route": "edgar_filing",
                 "direction": "long", "why": "initial 13D", "chart": "chart", "numbers": {"atr_pct": 0.03},
                 "hedge": "XLK", "primary_links": ["https://www.sec.gov/Archives/edgar/data/1/13d.htm"]},
                {"name": "BBB", "kind": "stock", "setup": "stretched", "door": "market_move", "route": "chart_control",
                 "direction": "long", "why": "stretched", "chart": "chart", "numbers": {"atr_pct": 0.03}, "hedge": "XLK"},
            ],
        }
        p = screen.live_path(DAY, "night")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(sc))

        # Record a pass: AAA taken OUTRIGHT, BBB passed (default hedged against XLK)
        draft = {
            "session": DAY.isoformat(), "pass": "night", "summary": "test", "market": {"ES": "+0.1%"},
            "proposals": [
                {"candidate": "AAA", "rank": 1, "decision": "take", "conviction": 3, "reason": "activist stake",
                 "reasoning": {"what_changed": "13D", "story": "activist", "chart": "flat", "coming": "none", "case_against": "risk"},
                 "wrong_if": "fund exits", "hedge": None, "hedge_why": "outright bet on activist",
                 "plan": {"horizon_days": 1, "stop": {"spread_pct": -5.0}}},
                {"candidate": "BBB", "rank": 2, "decision": "pass", "conviction": 2, "reason": "no catalyst",
                 "pass_case": {"case_for": "oversold", "case_against": "no news", "why_not": "stretched"}},
            ],
            "reviews": [],
        }
        with mock.patch.object(proposals, "pair_stats", return_value=None):
            daily.record(draft, now=datetime(2026, 11, 8, 20, 40, tzinfo=NY))

        # Write marks and bars so AAA +5%, XLK +2%, BBB -1% (outright AAA = +500bp, hedged AAA = +300bp)
        entries_doc = {"session": DAY.isoformat(), "handled": ["night"], "trades": [], "exits": [], "runs": [],
                       "marks": {"night": {"at": "2026-11-08T20:41:00-05:00",
                                           "prices": {"AAA": 100.0, "BBB": 100.0, "XLK": 100.0, "SPY": 500.0}}}}
        entries.path(DAY).parent.mkdir(parents=True, exist_ok=True)
        entries.path(DAY).write_text(json.dumps(entries_doc))
        (proposals.root() / "lessons.md").write_text("- SECRET STANDING LESSON DO NOT SHOW JUDGE\n")

        px = frame({"AAA": [105.0, 105.0], "BBB": [99.0, 99.0], "XLK": [102.0, 102.0], "SPY": [505.0, 505.0]})

        with mock.patch.object(learn.bars, "load", return_value=px), \
             mock.patch.object(learn.bars, "load_intraday", return_value={}), \
             mock.patch.object(learn.journal, "_bars_settled", return_value=True):
            rows = learn.outcomes()
            t = learn.tables(rows)
            owner_report = learn.brief()
            judge_desk = learn.desk()

        # Verify proposal kept route, door, and primary_links
        by_name = {r["name"]: r for r in rows}
        self.assertEqual((by_name["AAA"]["door"], by_name["AAA"]["route"]), ("disclosure", "edgar_filing"))
        self.assertAlmostEqual(by_name["AAA"]["plan_bps"], 500.0, places=1)
        self.assertAlmostEqual(by_name["AAA"]["outright_bps"], 500.0, places=1)
        self.assertAlmostEqual(by_name["AAA"]["hedged_bps"], 300.0, places=1)
        # BBB was passed with default hedge XLK (3-session default horizon, so marked so_far)
        self.assertAlmostEqual(by_name["BBB"]["hedged_so_far_bps"], -300.0, places=1)
        self.assertAlmostEqual(by_name["BBB"]["outright_so_far_bps"], -100.0, places=1)

        # Owner report has route + hedged vs outright + lessons
        self.assertIn("By route (settled)", owner_report)
        self.assertIn("Hedged vs outright", owner_report)
        self.assertIn("edgar_filing · take", t["by_route"])
        self.assertIn("SECRET STANDING LESSON", owner_report)

        # Judge desk has NONE of the scorecards, routes, post-mortem history, or lessons
        self.assertIn("Swing book desk", judge_desk)
        self.assertIn("Open trades", judge_desk)
        self.assertNotIn("By verdict", judge_desk)
        self.assertNotIn("By setup", judge_desk)
        self.assertNotIn("By route", judge_desk)
        self.assertNotIn("Latest post-mortems", judge_desk)
        self.assertNotIn("SECRET STANDING LESSON", judge_desk)


if __name__ == "__main__":
    unittest.main()

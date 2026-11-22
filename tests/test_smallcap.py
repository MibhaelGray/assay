import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import pandas as pd

from factor_rotation.config import NY
from factor_rotation.smallcap import book, edgar, record, score, screen, sources, universe


def listing(symbol="ABCD", cap=400e6, country="United States", name="Abcd Inc. Common Stock", industry="Restaurants", sector="Consumer Discretionary"):
    return {"symbol": symbol, "name": name, "cap": cap, "country": country, "sector": sector, "industry": industry}


def bars_for(symbol, closes, volumes=None, opens=None, end="2026-11-19"):
    days = pd.bdate_range(end=end, periods=len(closes))
    closes = [float(c) for c in closes]
    opens = opens or [closes[0], *closes[:-1]]
    return pd.DataFrame({"symbol": symbol, "date": days, "open": opens, "high": [max(o, c) * 1.01 for o, c in zip(opens, closes)],
                         "low": [min(o, c) * 0.99 for o, c in zip(opens, closes)], "close": closes,
                         "volume": volumes or [100_000] * len(closes)})


class Universe(unittest.TestCase):
    def test_the_band_the_country_and_the_industries(self):
        self.assertTrue(universe.eligible(listing()))
        self.assertFalse(universe.eligible(listing(cap=200e6)))
        self.assertFalse(universe.eligible(listing(cap=1.2e9)))
        self.assertFalse(universe.eligible(listing(country="Israel")))
        self.assertFalse(universe.eligible(listing(name="Abcd Inc. Warrants")))
        self.assertTrue(universe.eligible(listing(name="United Community Rights Group Common Stock".replace("Rights ", ""))))
        for industry in ("Biotechnology: Pharmaceutical Preparations", "Major Banks", "Savings Institutions", "Real Estate Investment Trusts"):
            self.assertFalse(universe.eligible(listing(industry=industry)), industry)
        self.assertTrue(universe.eligible(listing(industry="Property-Casualty Insurers", sector="Finance")))

    def test_build_keeps_only_names_that_trade_and_carries_analyst_counts(self):
        px = pd.concat([bars_for("OKCO", [10] * 30, [200_000] * 30), bars_for("THIN", [10] * 30, [50_000] * 30),
                        bars_for("CHEAP", [1.5] * 30, [2_000_000] * 30)])
        with tempfile.TemporaryDirectory() as d, mock.patch.object(universe, "PATH", Path(d) / "u.json"), \
                mock.patch.object(universe, "ROOT", Path(d)), mock.patch.object(universe.bars, "load", return_value=px):
            doc = universe.build(datetime(2026, 11, 19, 18, tzinfo=NY), listing=[listing("OKCO"), listing("THIN"), listing("CHEAP")],
                                 analysts={"OKCO": 1}, pull=False)
            self.assertEqual([(r["symbol"], r["analysts"], r["dollar_volume"]) for r in doc["names"]], [("OKCO", 1, 2_000_000)])
            again = universe.build(datetime(2026, 11, 26, 18, tzinfo=NY), listing=[listing("OKCO")], analysts={}, pull=False)
            self.assertEqual(again["names"][0]["analysts"], 1)   # a count under four weeks old is kept, not asked for again
            self.assertFalse(universe.stale(datetime(2026, 11, 27, tzinfo=NY)))
            self.assertTrue(universe.stale(datetime(2026, 12, 4, tzinfo=NY)))


PAGE = """<table class="tinytable"><thead><tr><th>X</th></tr></thead><tbody>
<tr><td>D</td><td><a>2026-11-19 20:07:49</a></td><td>2026-11-18</td><td><a onmouseover="Tip('x', DELAY, 1)" onmouseout="UnTip()">ABCD</a></td>
<td>Abcd Inc.</td><td>Doe Jane</td><td>CEO</td><td>P - Purchase</td><td>$10.00</td><td>+8,000</td><td>50,000</td><td>+19%</td><td>+$80,000</td><td></td><td></td><td></td><td></td></tr>
<tr><td></td><td>2026-11-18 09:00:00</td><td>2026-11-17</td><td><a>ABCD</a></td><td>Abcd Inc.</td><td>Roe Sam</td><td>Dir</td><td>P - Purchase</td>
<td>$10.00</td><td>+3,000</td><td>9,000</td><td>+50%</td><td>+$30,000</td><td></td><td></td><td></td><td></td></tr>
<tr><td></td><td>2026-11-19 09:00:00</td><td>2026-11-18</td><td><a>EFGH</a></td><td>Efgh Inc.</td><td>Parent Co</td><td>10%</td><td>P - Purchase</td>
<td>$5.00</td><td>+100,000</td><td>900,000</td><td>+12%</td><td>+$500,000</td><td></td><td></td><td></td><td></td></tr>
<tr><td></td><td>2026-11-19 09:00:00</td><td>2026-11-18</td><td><a>EFGH</a></td><td>Efgh Inc.</td><td>Seller Al</td><td>CFO</td><td>S - Sale</td>
<td>$5.00</td><td>-1,000</td><td>0</td><td>-100%</td><td>-$5,000</td><td></td><td></td><td></td><td></td></tr>
</tbody></table>"""


FORM4 = """<ownershipDocument>
 <reportingOwner><reportingOwnerId><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
  <reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer><officerTitle>Chief Financial Officer</officerTitle></reportingOwnerRelationship></reportingOwner>
 <nonDerivativeTable>
  <nonDerivativeTransaction><transactionDate><value>2026-11-18</value></transactionDate><transactionCoding><transactionCode>P</transactionCode></transactionCoding>
   <transactionAmounts><transactionShares><value>4000</value></transactionShares><transactionPricePerShare><value>15</value></transactionPricePerShare>
   <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction>
  <nonDerivativeTransaction><transactionDate><value>2026-11-18</value></transactionDate><transactionCoding><transactionCode>S</transactionCode></transactionCoding>
   <transactionAmounts><transactionShares><value>100</value></transactionShares><transactionPricePerShare><value>15</value></transactionPricePerShare>
   <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction>
 </nonDerivativeTable></ownershipDocument>"""


def buy(symbol, insider, value, accepted, ten_pct_only=False, title=""):
    return {"symbol": symbol, "insider": insider, "value": value, "title": title, "director": not title, "ten_pct_only": ten_pct_only,
            "filed": accepted[:10], "accepted": accepted}


class Insiders(unittest.TestCase):
    def test_a_form4_purchase_is_read_and_a_sale_is_not(self):
        doc = edgar.parse_form4(FORM4)
        self.assertEqual(doc["owners"][0]["title"], "Chief Financial Officer")
        self.assertEqual([(b["shares"], b["value"]) for b in doc["buys"]], [(4000.0, 60000.0)])

    def test_groups_need_fifty_thousand_from_one_or_twenty_five_from_two_and_something_new(self):
        since = datetime(2026, 11, 18, 21, tzinfo=edgar.timezone.utc)
        rows = [buy("ONE", "Doe Jane", 60000, "2026-11-19T15:00:00+00:00", title="Chief Financial Officer"),
                buy("TWO", "A", 15000, "2026-11-19T15:00:00+00:00"), buy("TWO", "B", 12000, "2026-11-17T15:00:00+00:00"),
                buy("SMALL", "C", 30000, "2026-11-19T15:00:00+00:00"),
                buy("OWNER", "Parent Co", 900000, "2026-11-19T15:00:00+00:00", ten_pct_only=True),
                buy("OLD", "D", 90000, "2026-11-17T15:00:00+00:00")]
        groups = screen.insider_groups(rows, since)
        self.assertEqual(sorted(groups), ["ONE", "TWO"])
        self.assertTrue(groups["ONE"]["cfo"])
        self.assertEqual((groups["TWO"]["value"], len(groups["TWO"]["buyers"])), (27000.0, 2))

    def test_openinsider_page_still_parses_for_the_fallback(self):
        self.assertEqual(len(sources.parse_insiders(PAGE)), 3)


class Filings(unittest.TestCase):
    def doc(self, *filings):
        return {"cik": 1, "sic": 3944, "filings": [{"form": f, "filed": at[:10], "accepted": at, "items": items, "accession": "x", "doc": "d"}
                                                    for f, at, items in filings]}

    def test_events_since_the_close(self):
        since = datetime(2026, 11, 19, 21, tzinfo=edgar.timezone.utc)
        ev = edgar.events(self.doc(("8-K", "2026-11-20T12:00:00.000Z", ["2.02", "9.01"]), ("SCHEDULE 13D", "2026-11-20T13:00:00.000Z", []),
                                   ("SCHEDULE 13D/A", "2026-11-20T13:00:00.000Z", []), ("8-K", "2026-11-20T14:00:00.000Z", ["3.01"]),
                                   ("NT 10-Q", "2026-11-19T12:00:00.000Z", [])), since)
        self.assertEqual(ev["earnings"], "2026-11-20T12:00:00+00:00")
        self.assertEqual(sorted(f["what"] for f in ev["filing"]),
                         ["activist stake: initial Schedule 13D", "delisting notice or listing-rule failure (8-K 3.01)"])
        self.assertEqual(len(ev["eight_k"]), 2)

    def test_sec_records_leave_out_reits_banks_and_funds(self):
        self.assertEqual(edgar.left_out({"sic": 6798, "filings": []}), "REIT (SEC industry code 6798)")
        self.assertIn("registered fund", edgar.left_out({"sic": None, "filings": [{"form": "N-CSR"}]}))
        self.assertIsNone(edgar.left_out({"sic": 3944, "filings": [{"form": "10-Q"}]}))


class Screen(unittest.TestCase):
    def setUp(self):
        flat = [10.0] * 39
        self.px = pd.concat([
            bars_for("IWM", [100.0] * 40),
            bars_for("EARN", [*flat, 11.0], [100_000] * 39 + [150_000]),       # reported, +10% on ordinary volume
            bars_for("NEWS", [*flat, 8.8], [100_000] * 39 + [400_000]),        # no report, -12% on four times volume
            bars_for("QUIET", [*flat, 10.2]),
            bars_for("LOUD", [*flat, 11.0], [100_000] * 39 + [110_000]),       # +10% with no volume and no report: nothing
            bars_for("BUY", [*flat, 10.0]),
            bars_for("OLD", flat, end="2026-11-18")])                          # no bar for the day
        self.names = [{"symbol": s, "name": f"{s} Inc.", "cap": 500e6, "dollar_volume": 2e6, "analysts": 1, "sector": "x", "industry": "y"}
                      for s in ("EARN", "NEWS", "QUIET", "LOUD", "BUY", "OLD")]
        self.insiders = {"BUY": {"value": 250000.0, "buyers": {"Doe Jane": {"title": "CEO", "value": 250000.0}}, "new": True,
                                 "last_filed": "2026-11-19"}}

    def find(self, skip=frozenset(), tonight=frozenset()):
        return screen.candidates(self.names, self.px, date(2026, 11, 19), {"EARN"}, set(tonight), self.insiders, set(skip))

    def test_each_setup_is_found_and_the_rest_are_not(self):
        found, counts = self.find()
        self.assertEqual({c["name"]: c["setup"] for c in found}, {"EARN": "earnings_reaction", "NEWS": "news_move", "BUY": "insider_buying"})
        self.assertEqual(counts["stale"], 1)
        news = next(c for c in found if c["name"] == "NEWS")
        self.assertEqual((news["numbers"]["ret_pct"], news["numbers"]["vs_iwm_pct"], news["numbers"]["volume_ratio"]), (-12.0, -12.0, 4.0))
        self.assertIn("-12.0% on the day", news["why"])

    def test_a_name_judged_recently_or_reporting_tonight_is_left_out(self):
        found, counts = self.find(skip={"EARN"}, tonight={"NEWS"})
        self.assertEqual(([c["name"] for c in found], counts["judged_recently"], counts["reports_tonight"]), (["BUY"], 1, 1))

    def test_the_list_runs_filings_then_earnings_then_price_moves(self):
        many = [{"name": f"E{i}", "setup": "earnings_reaction", "strength": 9 - i} for i in range(3)] + \
               [{"name": "M0", "setup": "news_move", "strength": 20}, {"name": "I0", "setup": "insider_buying", "strength": 1},
                {"name": "F0", "setup": "filing", "strength": 2}]
        self.assertEqual([c["name"] for c in screen.shortlist(many, most=4)], ["F0", "I0", "E0", "E1"])

    def test_a_filing_puts_a_quiet_name_on_the_list_and_lets_a_judged_name_back(self):
        ev = {"QUIET": {"filing": [{"what": "late quarterly report notice (NT 10-Q)", "at": "2026-11-19T22:00:00+00:00"}], "eight_k": []},
              "EARN": {"filing": [{"what": "material impairment (8-K 2.06)", "at": "2026-11-19T22:00:00+00:00"}], "eight_k": [{}]}}
        judged = {"EARN": datetime(2026, 11, 19, 12, tzinfo=NY), "NEWS": datetime(2026, 11, 19, 12, tzinfo=NY)}
        found, counts = screen.candidates(self.names, self.px, date(2026, 11, 19), {"EARN"}, set(), self.insiders, judged, events=ev)
        by = {c["name"]: c for c in found}
        self.assertEqual(by["QUIET"]["setup"], "filing")
        self.assertEqual((by["EARN"]["setup"], by["EARN"]["also"]), ("filing", ["earnings_reaction"]))
        self.assertNotIn("NEWS", by)   # judged this morning, nothing new since
        self.assertEqual((counts["judged_recently"], counts["back_on_new_filing"]), (1, 1))
        self.assertEqual([c["name"] for c in found][:2], ["EARN", "QUIET"])   # tier A first, then by the move's size

    def test_no_hedge_bar_for_the_day_stops_the_screen(self):
        with self.assertRaises(RuntimeError):
            screen.candidates(self.names, self.px[self.px["symbol"] != "IWM"], date(2026, 11, 19), set(), set(), {}, set())

    def test_the_screen_reads_the_last_close_and_is_for_the_next_session(self):
        self.assertEqual(screen.sessions(datetime(2026, 11, 19, 17, 30, tzinfo=NY)), (date(2026, 11, 19), date(2026, 11, 20)))
        self.assertEqual(screen.sessions(datetime(2026, 11, 20, 0, 30, tzinfo=NY)), (date(2026, 11, 19), date(2026, 11, 20)))
        self.assertEqual(screen.sessions(datetime(2026, 11, 20, 17, 30, tzinfo=NY)), (date(2026, 11, 20), date(2026, 11, 23)))


def verdict(name, rank, take=False, direction="long", **more):
    v = {"name": name, "verdict": "take" if take else "pass", "direction": direction, "conviction": 3, "rank": rank,
         "thesis": "a story", "dilution": "nothing found"}
    if take:
        v |= {"plan": {"horizon_days": 3, "stop_pct": 6}, "wrong_if": "guidance is cut"}
    return v | more


class LiveEntry(unittest.TestCase):
    def test_a_long_enters_at_the_ask_and_its_hedge_short_at_the_bid_with_yahoo_standing_in(self):
        books = lambda xs: ({"AAA": (9.9, 10.1), "IWM": (259.9, 260.1)}, {"BBB": "no book (IBKR error 10197)"})   # noqa: E731
        yahoo = lambda xs: {"BBB": 5.0}   # noqa: E731
        got = record.entries([{"name": "AAA", "direction": "long"}, {"name": "BBB", "direction": "short"}],
                             datetime(2026, 11, 20, 13, 30, tzinfo=NY), books=books, yahoo=yahoo)
        self.assertEqual((got["AAA"]["price"], got["AAA"]["hedge_price"], got["AAA"]["source"], got["AAA"]["spread_pct"]), (10.1, 259.9, "gateway", 2.0))
        self.assertEqual((got["BBB"]["price"], got["BBB"]["hedge_price"], got["BBB"]["source"]), (5.0, 260.1, "yahoo"))
        self.assertIn("10197", got["BBB"]["why_no_book"])

    def test_a_live_pass_runs_only_in_regular_hours_and_is_for_today(self):
        self.assertEqual(screen.sessions(datetime(2026, 11, 20, 13, 30, tzinfo=NY), "midday"), (date(2026, 11, 20), date(2026, 11, 20)))
        for when in (datetime(2026, 11, 20, 8, 0, tzinfo=NY), datetime(2026, 11, 20, 16, 30, tzinfo=NY), datetime(2026, 11, 21, 12, 0, tzinfo=NY)):
            with self.assertRaises(ValueError):
                screen.sessions(when, "morning")
        self.assertEqual(screen.key("2026-11-20", "midday"), "2026-11-20-midday")
        self.assertEqual(screen.key("2026-11-20", "night"), "2026-11-20")
        self.assertAlmostEqual(screen.day_share(datetime(2026, 11, 20, 9, 45, tzinfo=NY)), 0.07)


class Record(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        for name in ("SCREENS", "VERDICTS"):
            patcher = mock.patch.object(screen, name, root / name.lower())
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)
        screen.SCREENS.mkdir()
        shown = {"session": "2026-11-20", "prices_from": "2026-11-19", "candidates": [
            {"name": n, "company": f"{n} Inc.", "setup": "news_move", "why": "moved", "numbers": {"analysts": 1}} for n in ("AAA", "BBB", "CCC")]}
        (screen.SCREENS / "2026-11-20.json").write_text(json.dumps(shown))
        self.draft = root / "draft.json"

    def run_draft(self, verdicts, now="2026-11-19T19:00"):
        self.draft.write_text(json.dumps({"session": "2026-11-20", "market": "a weak tape", "verdicts": verdicts}))
        return record.record(self.draft, datetime.fromisoformat(now).replace(tzinfo=NY))

    def test_a_full_draft_is_kept_with_what_the_screen_saw(self):
        doc = self.run_draft([verdict("BBB", 2, news="stale"), verdict("AAA", 1, take=True, news="new"), verdict("CCC", 3, direction="short", news="none")])
        kept = json.loads((screen.VERDICTS / "2026-11-20.json").read_text())
        self.assertEqual([(v["name"], v["verdict"], v["setup"]) for v in kept["verdicts"]],
                         [("AAA", "take", "news_move"), ("BBB", "pass", "news_move"), ("CCC", "pass", "news_move")])
        self.assertTrue(kept["shadow"])
        self.assertIn("1 takes, 2 passes", record.summary(doc))
        self.assertEqual(set(screen.judged_recently(date(2026, 11, 21))), {"AAA", "BBB", "CCC"})
        self.assertEqual(kept["verdicts"][0]["news"], "new")

    def test_a_price_move_needs_the_news_label(self):
        with self.assertRaises(ValueError) as e:
            self.run_draft([verdict("AAA", 1, news="new"), verdict("BBB", 2, news="maybe"), verdict("CCC", 3, news="none")])
        self.assertIn("BBB: a price-move candidate needs news", str(e.exception))

    def test_everything_wrong_is_named_at_once(self):
        bad = [verdict("AAA", 1, take=True, wrong_if=""), verdict("BBB", 1, direction="up"), verdict("ZZZ", 3)]
        with self.assertRaises(ValueError) as e:
            self.run_draft(bad)
        for part in ("no verdict for: CCC", "not on tonight's screen: ZZZ", "ranks must be 1 to 3", "BBB: direction must be long or short",
                     "AAA: a take needs wrong_if"):
            self.assertIn(part, str(e.exception))
        self.assertFalse(screen.VERDICTS.exists())

    def test_nothing_is_recorded_once_the_session_has_opened(self):
        with self.assertRaises(ValueError) as e:
            self.run_draft([verdict("AAA", 1), verdict("BBB", 2), verdict("CCC", 3)], now="2026-11-20T09:31")
        self.assertIn("has opened", str(e.exception))


class Score(unittest.TestCase):
    def test_each_verdict_is_scored_in_its_direction_against_iwm_from_the_open(self):
        def path(symbol, opens, closes):
            return bars_for(symbol, closes, opens=opens, end="2026-11-24")   # sessions 11-20, 11-23, 11-24
        px = pd.concat([path("IWM", [100, 100, 101], [100, 101, 102]), path("UP", [10, 10.5, 11], [10.5, 11, 12]),
                        path("DOWN", [10, 9.9, 9.4], [9.9, 9.4, 9.0]), path("SAME", [10, 10, 10.1], [10, 10.1, 10.2])])
        doc = {"session": "2026-11-20", "verdicts": [
            {**verdict("UP", 1, take=True), "setup": "news_move", "numbers": {"analysts": 1}},
            {**verdict("SAME", 2), "setup": "news_move", "numbers": {"analysts": 6}},
            {**verdict("DOWN", 3, take=True, direction="short"), "setup": "insider_buying", "numbers": {"analysts": 0}},
            {**verdict("GONE", 4), "setup": "news_move", "numbers": {}}]}
        rows = {r["name"]: r for r in score.results(doc, px)}
        self.assertEqual(sorted(rows), ["DOWN", "SAME", "UP"])                    # no bars for GONE: not scored
        self.assertEqual(rows["UP"]["held"], {1: 500, 2: 900, 3: 1800})          # +5% less IWM's 0%, +10% less 1%, +20% less 2%
        self.assertEqual(rows["DOWN"]["held"], {1: 100, 2: 700, 3: 1200})        # a short: the fall, plus what IWM gained
        self.assertEqual(rows["SAME"]["held"], {1: 0, 2: 0, 3: 0})
        self.assertEqual(rows["UP"]["plan"], {"result": 1800, "sessions": 3, "stopped": False, "exit_price": 12.0})
        m = score.measure([doc], px)
        self.assertEqual((m["table"]["takes"][1], m["table"]["passes"][1], m["edge"][1]["takes_less_passes"]), ((300, 2), (0, 1), 300))
        self.assertEqual(m["table"]["2 analysts or fewer"][3], (1500, 2))
        self.assertIn("takes less passes", score.report(m))

    def test_a_take_that_hits_its_stop_ends_there(self):
        px = pd.concat([bars_for("IWM", [100, 100], opens=[100, 100], end="2026-11-23"),
                        bars_for("DROP", [9.0, 8.5], opens=[10, 9.0], end="2026-11-23")])
        doc = {"session": "2026-11-20", "verdicts": [{**verdict("DROP", 1, take=True), "setup": "news_move", "numbers": {}}]}
        plan = score.results(doc, px)[0]["plan"]
        self.assertEqual((plan["result"], plan["sessions"], plan["stopped"], round(plan["exit_price"], 2)), (-600, 1, True, 9.4))

    def test_a_live_entry_is_scored_from_its_price_and_its_first_day_low_is_ignored(self):
        px = pd.concat([bars_for("IWM", [100, 100], opens=[100, 100], end="2026-11-23"),
                        bars_for("DIP", [10.6, 11], opens=[10, 10.6], end="2026-11-23")])
        px.loc[(px["symbol"] == "DIP") & (px["date"] == pd.Timestamp("2026-11-20")), "low"] = 9.0   # before it was recorded
        entry = {"price": 10.2, "hedge_price": 100.0, "at": "2026-11-20T13:30:00-05:00", "source": "gateway"}
        doc = {"session": "2026-11-20", "pass": "midday", "verdicts": [{**verdict("DIP", 1, take=True), "setup": "news_move", "numbers": {}, "entry": entry}]}
        r = score.results(doc, px)[0]
        self.assertEqual((r["key"], r["live_entry"], r["held"], r["plan"]), ("2026-11-20-midday", True, {1: 392, 2: 784}, None))

    def test_the_dashboard_follows_each_take_and_waits_for_one_with_no_bar(self):
        px = pd.concat([bars_for("IWM", [100, 101], opens=[100, 100], end="2026-11-23"),
                        bars_for("UP", [10.5, 11], opens=[10, 10.5], end="2026-11-23")])
        docs = [{"session": "2026-11-20", "verdicts": [{**verdict("UP", 1, take=True), "setup": "news_move", "numbers": {"analysts": 1}},
                                                       {**verdict("LATER", 2, take=True), "setup": "news_move", "numbers": {}},
                                                       {**verdict("NO", 3), "setup": "news_move", "numbers": {}}]}]
        summary, first, second = score.dashboard(docs, px)
        self.assertEqual((first["name"], first["state"], first["sessions"], first["entry"], first["last"], first["result_bp"], first["position"], first["pnl"]),
                         ("UP", "open", 2, 10.0, 11.0, 900, 2083.33, 187.5))   # 0.5% of $25,000 over a 6% stop
        self.assertEqual((second["name"], second["state"], second["pnl"]), ("LATER", "waiting", None))
        self.assertEqual((summary["takes"], summary["open"], summary["waiting"], summary["won"], summary["average_bp"]), (2, 1, 1, 1, 900))
        self.assertEqual(score.take_names(docs), ["LATER", "UP"])
        self.assertEqual(summary["return_pct"], 0.75)

    def test_a_take_is_sized_by_risk_and_capped(self):
        self.assertEqual(score.size(3, 10, None), 1250.0)          # 0.5% of $25,000 over a 10% stop
        self.assertEqual(score.size(5, 10, None), 2500.0)          # 1%
        self.assertEqual(score.size(5, 5, None), 2500.0)           # $5,000 by risk, capped at 10% of the book
        self.assertEqual(score.size(3, 10, 200_000), 500.0)        # a quarter of a percent of $200,000 a day

    def test_the_book_view_holds_each_take_against_iwm_and_closes_it_at_its_stop(self):
        px = {"IWM": bars_for("IWM", [100, 100], opens=[100, 100], end="2026-11-23"),
              "SPY": bars_for("SPY", [500, 500, 500], end="2026-11-23"),
              "UP": bars_for("UP", [10.5, 11], opens=[10, 10.5], end="2026-11-23"),
              "DROP": bars_for("DROP", [9.0, 8.5], opens=[10, 9.0], end="2026-11-23")}
        docs = [{"session": "2026-11-20", "verdicts": [{**verdict("UP", 1, take=True), "setup": "news_move", "numbers": {}},
                                                       {**verdict("DROP", 2, take=True), "setup": "news_move", "numbers": {}},
                                                       {**verdict("NO", 3), "setup": "news_move", "numbers": {}}]}]
        out = book.rows(docs, px, now=datetime(2026, 11, 23, 17, tzinfo=NY))
        acct = next(r for r in out if r["section"] == "sc_portfolio_accounts")
        trades = {r["name"]: r for r in out if r["section"] == "sc_paper"}
        self.assertEqual((trades["UP"]["state"], trades["DROP"]["state"]), ("open", "closed"))
        self.assertEqual(trades["UP"]["legs"], "+208 UP, −20.83 IWM")            # $2,083 at the open of 10 and of 100
        self.assertEqual(trades["UP"]["pnl"], 208.0)
        self.assertEqual(trades["DROP"]["pnl"], -124.8)                           # 208 shares stopped at 9.40
        self.assertIn("Stopped", trades["DROP"]["reason"])
        self.assertEqual((acct["books"][0]["realized"], acct["books"][0]["open"], acct["pnl"]), (-124.8, 208.0, 83.2))
        self.assertEqual([r["pnl"] for r in out if r["section"] == "sc_portfolio_pnl"], [0.0, -20.8, 83.2])
        self.assertEqual({r["symbol"] for r in out if r["section"] == "sc_portfolio_position"}, {"UP", "IWM"})

    def test_nothing_scored_yet_says_so(self):
        self.assertIn("none scored yet", score.report(score.measure([], pd.DataFrame())))


if __name__ == "__main__":
    unittest.main()


class Baskets(unittest.TestCase):
    def test_groups_by_industry_and_folds_small_ones_into_other(self):
        from factor_rotation.smallcap import baskets
        names = [{"symbol": f"H{i}", "name": f"Home {i} Common Stock", "industry": "Homebuilding", "sector": "Consumer",
                  "cap": 3e8 + i * 1e8, "dollar_volume": 1e6 * i, "analysts": i} for i in range(1, 5)]
        names += [{"symbol": "ODD", "name": "Odd Co", "industry": "Rare Thing", "sector": "Industrials",
                   "cap": 5e8, "dollar_volume": 2e6, "analysts": None}]
        lib = baskets.library({"built": "2026-11-20", "names": names})
        by_name = {b["name"]: b for b in lib["baskets"]}
        self.assertEqual(set(by_name), {"Homebuilding", "Other industrials"})
        home = by_name["Homebuilding"]
        self.assertEqual(home["id"], "sc-homebuilding")
        self.assertEqual(home["hedge"], "IWM")
        self.assertEqual([m["symbol"] for m in home["members"]], ["H4", "H3", "H2", "H1"])   # most traded first
        self.assertEqual(home["members"][0]["why"], "Home 4 · $700M · 4 analysts")
        self.assertIn("2 of them have two analysts or fewer", home["story"])
        self.assertEqual(lib["baskets"][-1]["name"], "Other industrials")   # "Other" groups sort last

    def test_empty_universe(self):
        from factor_rotation.smallcap import baskets
        self.assertEqual(baskets.library({}), {"baskets": []})


class NotOperating(unittest.TestCase):
    def row(self, symbol, name, industry="Industrial Machinery/Components"):
        return {"symbol": symbol, "name": name, "industry": industry, "country": "United States", "cap": 5e8}

    def test_funds_trusts_and_listed_names_are_left_out(self):
        self.assertFalse(universe.eligible(self.row("ZZZ", "Some Municipal Income Trust Common Stock")))
        self.assertFalse(universe.eligible(self.row("ZZY", "Example Opportunities Fund Inc.")))
        self.assertFalse(universe.eligible(self.row("NMFC", "New Mountain Finance Corporation Common Stock")))
        self.assertEqual(universe.not_operating(self.row("GOOD", "Gladstone Commercial Corporation")), "REIT")

    def test_operating_companies_stay(self):
        self.assertTrue(universe.eligible(self.row("FNKO", "Funko Inc. Class A Common Stock")))
        self.assertTrue(universe.eligible(self.row("TRMK", "Trustmark Holdings Inc.")))   # "trust" only as a whole word

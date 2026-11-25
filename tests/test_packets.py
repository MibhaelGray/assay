"""Tests for free SEC EDGAR primary-source packets and `fr edgar read` (no network)."""

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from factor_rotation import edgar
from factor_rotation.config import NY
from factor_rotation.paper import packets


class EdgarTextAndReadTests(unittest.TestCase):
    def test_html_to_text_strips_scripts_styles_and_trailing_boilerplate(self):
        filler = "Strong Q3 revenue growth of 12% driven by core services and margin expansion. " * 60
        html = f"""
        <html><head><title>Ignored</title><style>.a{{color:red}}</style></head>
        <body>
          <script>var x = 1;</script>
          <h1>Acme Q3 Results</h1>
          <p>{filler}</p>
          <p>Forward-Looking Statements</p>
          <p>This legal boilerplate should be trimmed once enough content is captured.</p>
        </body></html>
        """
        text = edgar.html_to_text(html, max_chars=8000, trim_boilerplate=True)
        self.assertIn("Acme Q3 Results", text)
        self.assertNotIn("var x = 1", text)
        self.assertNotIn("color:red", text)
        self.assertNotIn("legal boilerplate should be trimmed", text)

    def test_read_url_rejects_non_sec_hosts_and_reads_sec_html(self):
        with self.assertRaises(ValueError):
            edgar.read_url("https://www.benzinga.com/news/123")
        with self.assertRaises(ValueError):
            edgar.read_url("http://www.sec.gov/Archives/edgar/data/1/a.htm")
        out = edgar.read_url(
            "https://www.sec.gov/Archives/edgar/data/1/a.htm",
            fetcher=lambda url: b"<html><body><p>Revenue rose 15% to $2.4B.</p></body></html>",
        )
        self.assertEqual(out, "Revenue rose 15% to $2.4B.")


class PacketBuildTests(unittest.TestCase):
    def test_build_fetches_exhibit_99_1_and_writes_market_stock_basket_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edgar_dir = root / "edgar"

            tickers_json = json.dumps({
                "0": {"cik_str": 1613103, "ticker": "MDT", "title": "Medtronic"},
                "1": {"cik_str": 1801198, "ticker": "LEGN", "title": "Legend Biotech"},
            }).encode()
            mdt_sub = json.dumps({
                "filings": {"recent": {
                    "form": ["8-K"],
                    "filingDate": ["2026-11-24"],
                    "accessionNumber": ["0001613103-26-000010"],
                    "primaryDocument": ["mdt-20261124.htm"],
                    "items": [["2.02", "9.01"]],
                }}
            }).encode()
            mdt_index = json.dumps({
                "directory": {"item": [
                    {"name": "mdt-20261124.htm", "size": "2000"},
                    {"name": "ex99-1earningsrelease.htm", "size": "18000"},
                ]}
            }).encode()
            mdt_ex99 = b"<html><body><h1>Medtronic Q2 FY27 Results</h1><p>Revenue $9.364B, Non-GAAP EPS $1.34.</p></body></html>"

            legn_sub = json.dumps({
                "filings": {"recent": {
                    "form": ["6-K"],
                    "filingDate": ["2026-11-10"],
                    "accessionNumber": ["0001801198-26-000026"],
                    "primaryDocument": ["legn6k.htm"],
                    "items": [[]],
                }}
            }).encode()
            legn_index = json.dumps({
                "directory": {"item": [
                    {"name": "legn6k.htm", "size": "1500"},
                    {"name": "a991earningsreleaseq32026.htm", "size": "15000"},
                ]}
            }).encode()
            legn_ex99 = b"<html><body><p>Carvykti net trade sales $710M, up 36% YoY.</p></body></html>"

            responses = {
                edgar.TICKERS_URL: tickers_json,
                edgar.SUBMISSIONS_URL.format(cik=1613103): mdt_sub,
                edgar.INDEX_URL.format(cik=1613103, acc="000161310326000010"): mdt_index,
                edgar.ARCHIVE_URL.format(cik=1613103, acc="000161310326000010", doc="ex99-1earningsrelease.htm"): mdt_ex99,
                edgar.SUBMISSIONS_URL.format(cik=1801198): legn_sub,
                edgar.INDEX_URL.format(cik=1801198, acc="000180119826000026"): legn_index,
                edgar.ARCHIVE_URL.format(cik=1801198, acc="000180119826000026", doc="a991earningsreleaseq32026.htm"): legn_ex99,
            }

            screen_doc = {
                "session": "2026-11-25",
                "as_of": "2026-11-24",
                "candidates": [
                    {"name": "MDT", "kind": "stock"},
                    {"name": "Biotech basket", "kind": "basket", "members": [{"symbol": "LEGN"}]},
                    {"name": "SPY", "kind": "etf"},
                ],
            }

            with mock.patch.object(edgar, "_cache_dir", return_value=edgar_dir):
                res = packets.build(
                    "night",
                    screen_doc=screen_doc,
                    now=datetime(2026, 11, 24, 20, 30, tzinfo=NY),
                    root=root / "packets",
                    fetcher=lambda url: responses[url],
                    quotes_fn=lambda syms: [{"symbol": "SPY", "price": 720.1, "change_pct": -0.005, "phase": "regular"}],
                    rss_fn=lambda: [("Federal Reserve", "FOMC statement", "Tue, 24 Nov 2026")],
                )

            self.assertEqual(res["count"], 2)
            self.assertIn("MDT", res["packets"])
            self.assertIn("Biotech basket", res["packets"])
            self.assertNotIn("SPY", res["packets"])

            mdt_text = Path(res["packets"]["MDT"]).read_text()
            self.assertIn("ex99-1earningsrelease.htm", mdt_text)
            self.assertIn("Revenue $9.364B", mdt_text)

            basket_text = Path(res["packets"]["Biotech basket"]).read_text()
            self.assertIn("Carvykti net trade sales $710M", basket_text)

            market_text = Path(res["market"]).read_text()
            self.assertIn("2026-11-26", market_text)   # Thanksgiving holiday from NYSE calendar
            self.assertIn("2026-11-27", market_text)   # Black Friday half-day
            self.assertIn("Federal Reserve", market_text)

            index_text = Path(res["index"]).read_text()
            self.assertIn("MARKET.md", index_text)
            self.assertIn("MDT", index_text)


if __name__ == "__main__":
    unittest.main()

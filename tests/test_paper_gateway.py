"""The paper gateway's quotes: what counts as a bid and ask, and the reason kept for a name that has none.
No network, no gateway.

    uv run python -m unittest discover -s tests
"""

import asyncio
import unittest
from types import SimpleNamespace as N
from unittest import mock

from factor_rotation.paper import gateway

nan = float("nan")


class QuoteReasons(unittest.TestCase):
    def test_a_missing_quote_comes_with_the_gateways_reason(self):
        self.assertEqual(gateway._book(N(bid=99.9, ask=100.1)), (99.9, 100.1))
        self.assertEqual(gateway._book(N(bid=100.0, ask=100.0)), (100.0, 100.0))    # a locked book is still a quote
        for bid, ask in ((nan, nan), (-1.0, 100.1), (99.9, -1.0), (100.2, 100.1), (None, None)):
            self.assertIsNone(gateway._book(N(bid=bid, ask=ask)))
        why = gateway._why_no_book
        self.assertIn("live IBKR login is in use somewhere else", why(N(bid=nan, ask=nan), (10197, "No market data during competing live session")))
        self.assertIn("(IBKR error 10197)", why(N(bid=nan, ask=nan), (10197, "x")))
        self.assertIn("no market data subscription", why(N(bid=nan, ask=nan), (10168, "x")))
        self.assertEqual(why(N(bid=nan, ask=nan), (999, "Something new")), "Something new (IBKR error 999)")
        self.assertEqual(why(N(bid=99.9, ask=-1.0), None), "the gateway sent a bid and no ask")
        self.assertIn("no bid or ask within 8 seconds and gave no reason", why(N(bid=nan, ask=nan), None))

    def test_quotes_are_asked_for_again_unless_the_gateway_refused(self):
        asked = []

        class IB:
            def reqMktData(self, c, *a):
                asked.append(c.symbol)
                n = asked.count(c.symbol)
                if c.symbol == "LATE":                                     # its book shows up on the second ask
                    return N(contract=c, bid=10.0 if n > 1 else nan, ask=10.1 if n > 1 else nan, last=nan)
                if c.symbol == "REFUSED":
                    m.errors[c.conId] = (10197, "No market data during competing live session")
                return N(contract=c, bid=nan, ask=nan, last=nan)

            def cancelMktData(self, c):
                pass

        m = gateway.IBMarket.__new__(gateway.IBMarket)
        m.ib, m.errors = IB(), {}

        async def contracts(symbols, overnight):
            return {s: N(symbol=s, conId=i + 1) for i, s in enumerate(symbols)}
        m._contracts = contracts
        with mock.patch.object(gateway.asyncio, "sleep", mock.AsyncMock()):
            asyncio.run(m.quotes(["LATE", "REFUSED", "EMPTY"], False, tries=3, need_books=True))
        self.assertEqual(m.books, {"LATE": (10.0, 10.1)})
        self.assertEqual((asked.count("LATE"), asked.count("REFUSED"), asked.count("EMPTY")), (2, 1, 3))
        self.assertIn("live IBKR login", m.missing["REFUSED"])
        self.assertIn("gave no reason", m.missing["EMPTY"])

    def test_a_gateway_that_cannot_be_reached_gives_its_reason_for_every_name(self):
        class Down:
            async def __aenter__(self):
                raise ConnectionRefusedError("nothing is listening on 4002")

            async def __aexit__(self, *exc):
                pass
        with mock.patch.object(gateway, "IBMarket", Down), self.assertLogs(gateway.log, "WARNING"):
            books, missing = asyncio.run(gateway.gateway_books(["AAA", "HHH"], False))
        self.assertEqual(books, {})
        self.assertIn("ConnectionRefusedError: nothing is listening on 4002", missing["AAA"])
        self.assertEqual(set(missing), {"AAA", "HHH"})


if __name__ == "__main__":
    unittest.main()

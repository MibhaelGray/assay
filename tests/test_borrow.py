"""IBKR's stock-loan file: what a row becomes. No network.

    uv run python -m unittest discover -s tests
"""

import unittest

from factor_rotation import borrow

FILE = """#BOF|2026.11.23|19:20:44
#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|
049323AB4|USD|CB ATLAS FINL HLDGS IN D04-14-22 06.625% 27|557991763|XXXXXXX3AB46|3.6300|0.2500|2800000|BBG017QTG7P1|
BRK B|USD|BERKSHIRE HATHAWAY INC-CL B|72063691|XXXXXXX07026|3.5913|0.2887|>10000000|BBG000DWG505|
INTU|USD|INTUIT INC|270662|XXXXXXX21034|3.6186|0.2614|4200000|BBG000BH5DV1|
NORATE|USD|NO RATE CORP|1|XXXXXXX|NA|NA|1000|BBG0|
ZZZOF|USD|ZINC ONE RESOURCES INC|489941914|CA98959W2031|-9.2815|13.1615|2000|BBG000WJ4YQ5|
#EOF|5
"""


class Parse(unittest.TestCase):
    def test_a_row_is_its_fee_rebate_and_shares(self):
        out = borrow.parse(FILE)
        self.assertEqual((out["as_of"], out["complete"]), ("2026.11.23 19:20:44", True))
        self.assertEqual(out["rows"]["INTU"], {"fee": 0.2614, "rebate": 3.6186, "shares": 4200000})
        self.assertEqual(out["rows"]["ZZZOF"], {"fee": 13.1615, "rebate": -9.2815, "shares": 2000})
        self.assertEqual(out["rows"]["BRK B"], {"fee": 0.2887, "rebate": 3.5913, "shares": 10_000_000, "capped": True})
        self.assertNotIn("NORATE", out["rows"])                # no rate, no row

    def test_a_file_cut_short_says_so(self):
        self.assertFalse(borrow.parse(FILE.rsplit("#EOF", 1)[0])["complete"])


if __name__ == "__main__":
    unittest.main()

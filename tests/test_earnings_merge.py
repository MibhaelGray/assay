import unittest
from datetime import date

from factor_rotation.earnings import merge


def r(d):
    return {"date": d, "session": "after_close"}


class Merge(unittest.TestCase):
    def test_a_report_yahoo_drops_on_the_day_is_kept(self):
        out = merge([r("2026-08-26")], [r("2026-08-26"), r("2026-11-17")], date(2026, 11, 17))
        self.assertEqual([x["date"] for x in out], ["2026-08-26", "2026-11-17"])

    def test_a_moved_date_is_not_kept_twice(self):
        out = merge([r("2026-11-19")], [r("2026-11-17")], date(2026, 11, 17))
        self.assertEqual([x["date"] for x in out], ["2026-11-19"])

    def test_old_and_future_dropped_rows_are_not_kept(self):
        out = merge([r("2026-11-30")], [r("2026-10-01"), r("2026-12-20")], date(2026, 11, 17))
        self.assertEqual([x["date"] for x in out], ["2026-11-30"])


if __name__ == "__main__":
    unittest.main()

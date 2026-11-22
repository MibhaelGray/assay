"""The Today view's routine check: a step is done only when it left its record."""

import json
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from unittest import mock

from factor_rotation import journal, routine
from factor_rotation.config import NY

DAY = date(2026, 11, 10)  # a Tuesday


def at(h, m=0, d=DAY):
    return datetime.combine(d, time(h, m), NY)


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for p in (mock.patch.object(journal, "JOURNAL", self.root), mock.patch.object(routine, "LOGS", self.root / "logs"),
                  mock.patch.object(routine.storage, "modified", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def write(self, rel, doc):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))

    def state(self, key, now, day=DAY):
        return next(r for r in routine.check(day, now) if r["key"] == key)

    def test_a_pass_in_the_sessions_proposals_means_the_pass_ran(self):
        self.write(f"paper/proposals/{DAY}.json", {"passes": [{"pass": "morning", "at": at(10, 5).isoformat(), "proposals": 40, "takes": 3}]})
        r = self.state("pass-morning", at(11))
        self.assertEqual((r["state"], r["detail"], r["late_min"]), ("done", "40 verdicts, 3 takes", None))
        self.assertEqual(self.state("pass-midday", at(16))["state"], "missed")

    def test_before_its_time_a_step_is_upcoming_then_due_then_missed(self):
        self.assertEqual(self.state("paper-close", at(15))["state"], "upcoming")
        self.assertEqual(self.state("paper-close", at(15, 50))["state"], "running")
        self.assertEqual(self.state("paper-close", at(17))["state"], "missed")

    def test_a_job_that_logged_but_left_nothing_failed(self):
        (self.root / "logs").mkdir()
        local = at(15, 46).astimezone()  # the log is in the Mac's own time zone
        (self.root / "logs" / "paper-close.log").write_text(f"{local:%Y-%m-%d %H:%M:%S},1 ERROR boom\n")
        r = self.state("paper-close", at(17))
        self.assertEqual(r["state"], "failed")
        self.assertIn("boom", r["detail"])

    def test_a_log_from_another_day_is_not_counted(self):
        (self.root / "logs").mkdir()
        (self.root / "logs" / "paper-close.log").write_text(f"{at(15, 46, date(2026, 11, 11)).astimezone():%Y-%m-%d %H:%M:%S},1 ERROR boom\n")
        self.assertEqual(self.state("paper-close", at(17))["state"], "missed")

    def test_a_screen_without_a_brief_is_a_failed_nightly_job(self):
        self.write(f"screens/{DAY}.json", {"built_at": at(21, 20).isoformat()})
        r = self.state("nightly", at(22, 30))
        self.assertEqual(r["state"], "failed")
        self.assertIn("No brief", r["detail"])

    def test_the_night_pass_is_found_in_the_next_sessions_proposals(self):
        self.write("paper/proposals/2026-11-11.json", {"passes": [{"pass": "night", "at": at(20, 45).isoformat()}]})
        self.assertEqual(self.state("pass-night", at(23))["state"], "done")

    def test_a_late_pass_by_hand_is_not_the_night_pass(self):
        self.write("paper/proposals/2026-11-11.json", {"passes": [{"pass": "late", "at": at(22).isoformat()}]})
        self.assertEqual(self.state("pass-night", at(23, 30))["state"], "missed")

    def test_a_holiday_has_nothing_scheduled(self):
        holiday = date(2026, 11, 26)
        self.assertEqual(self.state("paper-close", at(12, d=holiday), holiday)["state"], "off")


if __name__ == "__main__":
    unittest.main()

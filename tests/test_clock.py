"""The schedule's clock (deploy/launchd/clock.py): timed jobs start on New York time, whatever zone the Mac is in.

    uv run python -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "launchd"))
import clock  # noqa: E402
import install  # noqa: E402

NY = ZoneInfo("America/New_York")


def ny(day: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=NY)


class ClockTests(unittest.TestCase):
    def test_morning_pass_starts_at_945_new_york_and_only_once(self):
        state = {"day": "2026-11-27", "started": []}
        early, state = clock.due(ny("2026-11-27", "09:44"), state)
        self.assertNotIn("pass-morning", early)
        jobs, state = clock.due(ny("2026-11-27", "09:45"), state)
        self.assertIn("pass-morning", jobs)
        again, state = clock.due(ny("2026-11-27", "09:46"), state)
        self.assertEqual(again, [])

    def test_the_macs_time_zone_changes_nothing(self):
        """6:45 AM in California, 8:45 AM in Chicago: the same instant, so the same jobs come due."""
        instant = datetime(2026, 11, 27, 14, 45, 20, tzinfo=timezone.utc)   # 9:45:20 AM New York
        orig_tz = os.environ.get("TZ")
        try:
            for zone in ("America/Los_Angeles", "America/Chicago", "Asia/Tokyo", "UTC"):
                os.environ["TZ"] = zone
                time.tzset()
                jobs, _ = clock.due(
                    instant.astimezone(NY),
                    {"day": "2026-11-27", "started": ["keep-awake 09:20", "gateway-preflight 09:35"]},
                )
                self.assertEqual(jobs, ["pass-morning"], msg=f"failed in zone {zone}")
        finally:
            if orig_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = orig_tz
            time.tzset()

    def test_first_run_starts_nothing_already_past(self):
        jobs, state = clock.due(ny("2026-11-27", "13:25"), {})
        self.assertEqual(jobs, [])
        self.assertIn("pass-morning 09:45", state["started"])
        self.assertNotIn("bars 16:30", state["started"])
        later, _ = clock.due(ny("2026-11-27", "16:30"), state)
        # By 16:30, 15:32 preflight and 15:41/15:46 close are past their catch-up windows; side-facts, paper-sync, bars are due
        self.assertIn("side-facts", later)
        self.assertIn("paper-sync", later)
        self.assertIn("bars", later)

    def test_a_new_day_catches_up_within_window_and_skips_stale_slots(self):
        """Mac slept from Thursday night to 11:20 AM Friday: morning pass (95m late <= 135m) still starts,
        09:35 preflight (105m late > 15m) is marked missed instead of firing stale."""
        items, newly_missed, state = clock.plan(
            ny("2026-11-27", "11:20"),
            {"day": "2026-11-26", "started": ["nightly 19:00"]},
        )
        jobs = [j for j, _ in items]
        self.assertIn("pass-morning", jobs)
        self.assertIn("keep-awake", jobs)
        self.assertIn("gateway-preflight", jobs)   # 11:15 is only 5m late
        self.assertIn("side-facts", jobs)          # 11:15 is due
        self.assertEqual(jobs.count("gateway-preflight"), 1)
        self.assertIn("gateway-preflight 09:35", newly_missed)
        self.assertEqual(state["day"], "2026-11-27")
        self.assertNotIn("nightly 19:00", state["started"])

    def test_stale_morning_pass_after_wake_at_4pm_is_marked_missed_not_crashed(self):
        """Waking at 16:00 ET must NOT fire pass-morning (which would crash outside its window); it records missed."""
        items, newly_missed, state = clock.plan(
            ny("2026-11-27", "16:00"),
            {"day": "2026-11-26", "started": []},
        )
        jobs = [j for j, _ in items]
        self.assertNotIn("pass-morning", jobs)
        self.assertNotIn("pass-midday", jobs)
        self.assertNotIn("paper-close", jobs)
        self.assertIn("pass-morning 09:45", state["missed"])
        self.assertIn("paper-close 15:41", state["missed"])
        self.assertIn("paper-close 15:46", state["missed"])

    def test_failed_kickstart_retries_and_records_error_then_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "clock.json"
            state_path.write_text(json.dumps({"day": "2026-11-27", "started": ["keep-awake 09:20", "gateway-preflight 09:35"]}))
            # First tick fails -> NOT marked started, error recorded
            s1 = clock.tick(ny("2026-11-27", "09:45"), state_path=state_path, starter=lambda j: "launchctl exited 113")
            self.assertNotIn("pass-morning 09:45", s1["started"])
            self.assertIn("pass-morning 09:45", s1["errors"])
            self.assertEqual(s1["retries"]["pass-morning 09:45"], 1)
            # Next tick succeeds -> marked started, error & retries cleared
            s2 = clock.tick(ny("2026-11-27", "09:45"), state_path=state_path, starter=lambda j: None)
            self.assertIn("pass-morning 09:45", s2["started"])
            self.assertNotIn("errors", s2)
            self.assertNotIn("retries", s2)

    def test_repeated_kickstart_failure_moves_to_missed_after_max_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "clock.json"
            state_path.write_text(json.dumps({"day": "2026-11-27", "started": ["keep-awake 09:20", "gateway-preflight 09:35"]}))
            s = {}
            for _ in range(clock.MAX_RETRIES):
                s = clock.tick(ny("2026-11-27", "09:45"), state_path=state_path, starter=lambda j: "boom")
            self.assertIn("pass-morning 09:45", s["missed"])
            self.assertIn("failed 5 times", s["missed"]["pass-morning 09:45"])
            self.assertNotIn("errors", s)

    def test_start_auto_bootstraps_unloaded_service_and_uses_k_for_keep_awake(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents = Path(tmp)
            for job in ("pass-morning", "keep-awake"):
                (agents / f"{install.LABEL.format(job)}.plist").write_text("<plist/>")
            calls = []

            def fake_run(cmd, **kw):
                calls.append(cmd)
                # Simulate unloaded service on first kickstart for pass-morning
                if cmd[:2] == ["launchctl", "kickstart"] and "pass-morning" in cmd[-1] and len(calls) == 1:
                    return mock.Mock(returncode=113, stdout="", stderr="Could not find service")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(clock, "AGENTS", agents), mock.patch.object(clock.subprocess, "run", side_effect=fake_run):
                self.assertIsNone(clock.start("pass-morning"))
                self.assertIsNone(clock.start("keep-awake"))
            self.assertEqual(calls[0][:2], ["launchctl", "kickstart"])
            self.assertEqual(calls[1][:2], ["launchctl", "bootstrap"])
            self.assertEqual(calls[2][:2], ["launchctl", "kickstart"])
            self.assertEqual(calls[3][:3], ["launchctl", "kickstart", "-k"])

    def test_weekend_days(self):
        saturday, _ = clock.due(ny("2026-11-28", "23:59"), {"day": "2026-11-28", "started": []})
        self.assertEqual(saturday, [])
        sunday, _ = clock.due(ny("2026-11-29", "20:30"), {"day": "2026-11-29", "started": []})
        self.assertEqual(set(sunday), {"keep-awake", "gateway-preflight", "pass-night"})

    def test_keep_awake_self_heals_only_in_active_window_when_dead(self):
        self.assertTrue(clock.in_awake_window(ny("2026-11-29", "19:55")))   # Sunday start
        self.assertFalse(clock.in_awake_window(ny("2026-11-29", "19:54")))  # Sunday before start
        self.assertTrue(clock.in_awake_window(ny("2026-11-27", "14:00")))   # Friday afternoon
        self.assertFalse(clock.in_awake_window(ny("2026-11-27", "20:00")))  # Friday evening off
        self.assertFalse(clock.in_awake_window(ny("2026-11-28", "12:00")))  # Saturday off

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "clock.json"
            pid_path = Path(tmp) / ".keep-awake.pid"
            # Scheduled slot already ran earlier today, then caffeinate died (stale/missing pid)
            state_path.write_text(json.dumps({
                "day": "2026-11-27",
                "started": ["keep-awake 09:20", "gateway-preflight 09:35", "pass-morning 09:45"],
            }))
            started = []
            clock.tick(ny("2026-11-27", "10:15"), state_path=state_path, starter=lambda j: started.append(j) or None, awake_pid=pid_path)
            self.assertEqual(started, ["keep-awake"])

            # When _awake_alive is True, tick does NOT restart keep-awake
            started.clear()
            with mock.patch.object(clock, "_awake_alive", return_value=True):
                clock.tick(ny("2026-11-27", "10:16"), state_path=state_path, starter=lambda j: started.append(j) or None, awake_pid=pid_path)
            self.assertEqual(started, [])

            # Saturday: does NOT start keep-awake even when dead
            started.clear()
            clock.tick(ny("2026-11-28", "12:00"), state_path=state_path, starter=lambda j: started.append(j) or None, awake_pid=pid_path)
            self.assertEqual(started, [])

    def test_every_scheduled_job_is_installed_without_a_calendar_and_the_clock_ticks(self):
        for job in install.SCHEDULE:
            doc = install.plist(job, "/usr/bin")
            self.assertNotIn("StartCalendarInterval", doc)
            self.assertNotIn("StartInterval", doc)
        self.assertEqual(install.plist("clock", "/usr/bin", install.EVERY["clock"])["StartInterval"], 30)
        self.assertTrue(set(install.SUNDAY) <= set(install.SCHEDULE))
        # Close job has primary + backup slots for both half days and full days
        self.assertEqual(install.SCHEDULE["paper-close"], ["12:41", "12:46", "15:41", "15:46"])
        # Keep-awake covers daytime and overnight
        self.assertEqual(install.SCHEDULE["keep-awake"], ["09:20", "16:40"])


if __name__ == "__main__":
    unittest.main()

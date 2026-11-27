import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from factor_rotation import watch
from factor_rotation.config import NY


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=NY)


class Passes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.logs = Path(self.dir.name)
        patcher = mock.patch.object(watch, "PASS_LOGS", self.logs)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)

    def log(self, name: str, kind: str, started: str, ok: bool = True):
        (self.logs / name).write_text(json.dumps({"pass": kind, "started": at(started).isoformat(), "ok": ok}))

    def test_a_recorded_pass_raises_nothing(self):
        self.log("2026-11-17-midday.json", "midday", "2026-11-17T13:00:07")
        found = watch.missing_passes(at("2026-11-17T14:30"), trading=lambda d: True)
        self.assertFalse([k for k, *_ in found if k.startswith("pass:midday")])

    def test_a_missing_pass_is_reported_after_pass_late_not_before(self):
        self.assertFalse([k for k, *_ in watch.missing_passes(at("2026-11-17T14:25"), trading=lambda d: True) if "midday" in k])
        found = watch.missing_passes(at("2026-11-17T14:40"), trading=lambda d: True)
        self.assertIn("pass:midday:2026-11-17", [k for k, *_ in found])

    def test_a_failed_pass_is_reported(self):
        self.log("2026-11-17-morning.json", "morning", "2026-11-17T09:45:05", ok=False)
        found = watch.missing_passes(at("2026-11-17T11:30"), trading=lambda d: True)
        self.assertIn("The morning pass failed", [t for _, t, _ in found])

    def test_no_pass_is_expected_on_a_holiday_or_a_friday_night(self):
        self.assertEqual(watch.missing_passes(at("2026-11-26T15:00"), trading=lambda d: False), [])
        weekdays = lambda d: d.weekday() < 5  # noqa: E731
        friday_night = watch.missing_passes(at("2026-11-20T22:30"), trading=weekdays)
        self.assertFalse([k for k, *_ in friday_night if k.startswith("pass:night")])

    def test_the_night_pass_is_expected_only_the_evening_before_a_session(self):
        # Thanksgiving 2026-11-26: no night pass on Wednesday evening, one on the holiday's own evening for Friday.
        nyse = lambda d: d.weekday() < 5 and d.isoformat() != "2026-11-26"  # noqa: E731
        eve = watch.missing_passes(at("2026-11-25T22:30"), trading=nyse)
        self.assertFalse([k for k, *_ in eve if k.startswith("pass:night")])
        holiday = watch.missing_passes(at("2026-11-26T22:30"), trading=nyse)
        self.assertIn("pass:night:2026-11-26", [k for k, *_ in holiday])

    def test_last_nights_pass_is_still_checked_after_midnight(self):
        found = watch.missing_passes(at("2026-11-18T01:00"), trading=lambda d: True)
        self.assertIn("pass:night:2026-11-17", [k for k, *_ in found])


class Quotes(unittest.TestCase):
    def runs(self, *warned):
        return {"runs": [{"at": at(t).isoformat(), "warnings": ["S01 X: no quote for X from the gateway because ... (IBKR error 10197) ..."] if w else []}
                         for t, w in warned]}

    def test_two_refused_runs_in_a_row_alert(self):
        with mock.patch("factor_rotation.paper.entries.load", return_value=self.runs(("2026-11-17T10:00", True), ("2026-11-17T10:05", True))):
            found = watch.refused_quotes(at("2026-11-17T10:06"))
        self.assertEqual(len(found), 1)
        self.assertIn("10197", found[0][2])

    def test_one_refused_run_or_a_recovery_does_not(self):
        with mock.patch("factor_rotation.paper.entries.load", return_value=self.runs(("2026-11-17T10:00", True), ("2026-11-17T10:05", False))):
            self.assertEqual(watch.refused_quotes(at("2026-11-17T10:06")), [])
        with mock.patch("factor_rotation.paper.entries.load", return_value=self.runs(("2026-11-17T10:05", True))):
            self.assertEqual(watch.refused_quotes(at("2026-11-17T10:06")), [])


class Gateway(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", {"FR_PAPER_ACCOUNT": "DU0000000"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_two_failed_probes_in_a_row_alert_and_one_does_not(self):
        state = {}
        down = lambda: "the paper gateway can't be reached (ConnectionRefusedError)"
        self.assertEqual(watch.gateway(at("2026-11-17T10:00"), state, down), [])
        self.assertEqual(len(watch.gateway(at("2026-11-17T10:05"), state, down)), 1)
        watch.gateway(at("2026-11-17T10:10"), state, lambda: None)
        self.assertEqual(state["gateway_fails"], 0)

    def test_the_nightly_reset_is_not_news(self):
        state = {"gateway_fails": 5}
        self.assertEqual(watch.gateway(at("2026-11-17T23:50"), state, lambda: "down"), [])
        self.assertEqual(state["gateway_fails"], 0)


class LostQuotes(unittest.TestCase):
    """The gateway is up but refused quotes because the live login is open somewhere else."""

    def setUp(self):
        patcher = mock.patch.dict("os.environ", {"FR_PAPER_ACCOUNT": "DU0000000"})
        patcher.start()
        self.addCleanup(patcher.stop)
        trading = mock.patch("factor_rotation.journal.is_trading_day", side_effect=lambda d: d.weekday() < 5)
        trading.start()
        self.addCleanup(trading.stop)

    refused = staticmethod(lambda: "the live IBKR login is in use somewhere else (IBKR error 10197)")

    def test_two_refusals_in_a_row_alert_and_one_does_not(self):
        state = {}
        self.assertEqual(watch.lost_quotes(at("2026-11-17T10:00"), state, self.refused), [])
        found = watch.lost_quotes(at("2026-11-17T10:05"), state, self.refused)
        self.assertEqual(len(found), 1)
        self.assertIn("log out", found[0][1])
        watch.lost_quotes(at("2026-11-17T10:10"), state, lambda: None)
        self.assertEqual(state["quote_fails"], 0)

    def test_not_checked_when_no_order_could_go_or_the_gateway_is_down(self):
        state = {}
        saturday = at("2026-11-21T12:00")
        for _ in range(2):
            self.assertEqual(watch.lost_quotes(saturday, state, self.refused), [])
        state = {"gateway_fails": 2}
        for _ in range(2):
            self.assertEqual(watch.lost_quotes(at("2026-11-17T10:00"), state, self.refused), [])

    def test_not_during_the_nightly_reset(self):
        state = {"quote_fails": 5}
        self.assertEqual(watch.lost_quotes(at("2026-11-17T23:45"), state, self.refused), [])
        self.assertEqual(state["quote_fails"], 0)


class Sending(unittest.TestCase):
    def test_the_same_alert_is_not_sent_again_within_the_repeat_window(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(watch, "STATE", Path(d) / "watch.json"), \
                mock.patch.object(watch, "failed_jobs", return_value=[("job:nightly:1", "Job failed: nightly", "x")]), \
                mock.patch.object(watch, "missing_passes", return_value=[]), \
                mock.patch.object(watch, "refused_quotes", return_value=[]), \
                mock.patch.object(watch, "gateway", return_value=[]), \
                mock.patch.object(watch, "lost_quotes", return_value=[]), \
                mock.patch.object(watch, "send") as send:
            watch.run(at("2026-11-17T10:00"), quiet=True)
            watch.run(at("2026-11-17T15:00"), quiet=True)
            self.assertEqual(send.call_count, 1)
            watch.run(at("2026-11-18T10:05"), quiet=True)
            self.assertEqual(send.call_count, 2)

    def test_telegram_sends_when_both_settings_are_set(self):
        resp = mock.MagicMock(); resp.__enter__.return_value.status = 200
        with mock.patch.dict("os.environ", {"FR_NTFY_TOPIC": "", "FR_TELEGRAM_TOKEN": "t0k", "FR_TELEGRAM_CHAT": "42"}), \
                mock.patch("urllib.request.urlopen", return_value=resp) as op, mock.patch("subprocess.run"):
            self.assertTrue(watch.send("Title", "body"))
        req = op.call_args[0][0]
        self.assertIn("/bott0k/sendMessage", req.full_url)
        self.assertEqual(json.loads(req.data)["chat_id"], "42")

    def test_telegram_needs_both_token_and_chat(self):
        with mock.patch.dict("os.environ", {"FR_TELEGRAM_TOKEN": "t0k", "FR_TELEGRAM_CHAT": ""}), mock.patch("urllib.request.urlopen") as op:
            self.assertFalse(watch.telegram("t", "x"))
            op.assert_not_called()

    def test_no_topic_means_no_phone_push(self):
        with mock.patch.dict("os.environ", {"FR_NTFY_TOPIC": "", "FR_TELEGRAM_TOKEN": ""}), mock.patch("urllib.request.urlopen") as op, \
                mock.patch("subprocess.run"):
            self.assertFalse(watch.send("t", "x"))
            op.assert_not_called()


if __name__ == "__main__":
    unittest.main()


def test_stopped_clock_alerts_after_ten_minutes(tmp_path, monkeypatch):
    """The clock (deploy/launchd/clock.py) starts every timed job, so the watchdog says when it stops ticking."""
    import json
    from datetime import datetime, timedelta
    from factor_rotation import watch
    from factor_rotation.config import NY
    now = datetime(2026, 11, 27, 13, 30, tzinfo=NY)
    monkeypatch.setattr(watch, "CLOCK", tmp_path / "clock.json")
    assert watch.stopped_clock(now) == []                      # never installed
    watch.CLOCK.write_text(json.dumps({"tick": (now - timedelta(minutes=2)).isoformat()}))
    assert watch.stopped_clock(now) == []
    watch.CLOCK.write_text(json.dumps({"tick": (now - timedelta(minutes=25)).isoformat()}))
    (key, title, text), = watch.stopped_clock(now)
    assert key == "clock:stopped" and "25 minutes" in text

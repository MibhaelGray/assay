import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from factor_rotation import journal, watch
from factor_rotation.config import NY
from factor_rotation.paper import book, entries


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=NY)


class Passes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.logs = Path(self.dir.name) / "pass-logs"
        self.logs.mkdir()
        self.journal = Path(self.dir.name) / "journal"
        for target, attr, val in ((watch, "PASS_LOGS", self.logs), (journal, "JOURNAL", self.journal)):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.dir.cleanup)

    def log(self, name: str, kind: str, started: str, ok: bool = True, **extra):
        (self.logs / name).write_text(json.dumps({"pass": kind, "started": at(started).isoformat(), "ok": ok, **extra}))

    def test_a_recorded_pass_raises_nothing(self):
        self.log("2026-11-17-midday.json", "midday", "2026-11-17T13:00:07")
        found = watch.missing_passes(at("2026-11-17T14:30"), trading=lambda d: True)
        self.assertFalse([k for k, *_ in found if "midday" in k])

    def test_a_missing_pass_alerts_after_start_late_and_escalates_after_pass_late(self):
        # 10 min after 13:00 -> not late yet
        self.assertFalse([k for k, *_ in watch.missing_passes(at("2026-11-17T13:10"), trading=lambda d: True) if "midday" in k])
        # 20 min after 13:00 with no running sentinel -> fast start alert
        early = watch.missing_passes(at("2026-11-17T13:20"), trading=lambda d: True)
        self.assertIn("pass-start:midday:2026-11-17", [k for k, *_ in early])
        # 100 min after 13:00 -> missing pass alert
        found = watch.missing_passes(at("2026-11-17T14:40"), trading=lambda d: True)
        self.assertIn("pass:midday:2026-11-17", [k for k, *_ in found])

    def test_a_running_pass_with_live_pid_does_not_false_alarm_until_stuck(self):
        self.log("2026-11-17-night.json", "night", "2026-11-17T20:30:05", ok=False, status="running", pid=os.getpid())
        # 110 minutes in, PID alive -> NO night alert (unlike before when a 125-min night pass false-alarmed at 95 min)
        self.assertEqual([k for k, *_ in watch.missing_passes(at("2026-11-17T22:20"), trading=lambda d: True) if "night" in k], [])
        # 160 minutes in, still running -> hung alert
        stuck = watch.missing_passes(at("2026-11-17T23:10"), trading=lambda d: True)
        self.assertIn("The night pass appears hung", [t for _, t, _ in stuck])

    def test_a_running_pass_with_dead_pid_alerts_crashed(self):
        self.log("2026-11-17-morning.json", "morning", "2026-11-17T09:45:05", ok=False, status="running", pid=999999)
        with mock.patch.object(watch, "_pid_alive", return_value=False):
            found = watch.missing_passes(at("2026-11-17T10:05"), trading=lambda d: True)
        self.assertIn("The morning pass crashed", [t for _, t, _ in found])

    def test_a_pass_recorded_in_proposals_raises_nothing_even_without_log(self):
        prop_path = self.journal / "paper" / "proposals" / "2026-11-17.json"
        prop_path.parent.mkdir(parents=True, exist_ok=True)
        prop_path.write_text(json.dumps({"session": "2026-11-17", "passes": [{"pass": "morning", "at": at("2026-11-17T10:05").isoformat()}], "proposals": []}))
        found = watch.missing_passes(at("2026-11-17T11:30"), trading=lambda d: True)
        self.assertFalse([k for k, *_ in found if "morning" in k])

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
                mock.patch.object(watch, "stopped_clock", return_value=[]), \
                mock.patch.object(watch, "missing_passes", return_value=[]), \
                mock.patch.object(watch, "missing_close", return_value=[]), \
                mock.patch.object(watch, "stuck_closing", return_value=[]), \
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


class ClockAndCloseWatch(unittest.TestCase):
    def test_stopped_clock_errors_and_missed_critical_jobs_alert(self):
        now = datetime(2026, 11, 27, 13, 30, tzinfo=NY)
        with tempfile.TemporaryDirectory() as d:
            clk = Path(d) / "clock.json"
            with mock.patch.object(watch, "CLOCK", clk):
                self.assertEqual(watch.stopped_clock(now), [])
                clk.write_text(json.dumps({"day": "2026-11-27", "tick": (now - timedelta(minutes=2)).isoformat()}))
                self.assertEqual(watch.stopped_clock(now), [])
                clk.write_text(json.dumps({
                    "day": "2026-11-27",
                    "tick": (now - timedelta(minutes=25)).isoformat(),
                    "errors": {"pass-morning 09:45": {"error": "launchctl exited 113"}},
                    "missed": {
                        "paper-close 12:41": "skipped past window",
                        "paper-close 15:41": "full-day slot on a half day",
                        "pass-midday 13:00": "full-day pass on a half day",
                        "pass-night 20:30": "friday night, tomorrow closed",
                        "gateway-preflight 09:35": "skipped",
                    },
                }))
                with mock.patch.object(book, "ROOT", Path(d) / "paper"):
                    found = {k: text for k, _, text in watch.stopped_clock(now)}
                self.assertIn("clock:stopped", found)
                self.assertIn("25 minutes", found["clock:stopped"])
                self.assertIn("clock:err:2026-11-27:pass-morning 09:45", found)
                self.assertIn("clock:missed:2026-11-27:paper-close 12:41", found)
                # Full-day close, midday on half day, Friday night pass, and preflight must NOT spam
                self.assertNotIn("clock:missed:2026-11-27:paper-close 15:41", found)
                self.assertNotIn("clock:missed:2026-11-27:pass-midday 13:00", found)
                self.assertNotIn("clock:missed:2026-11-27:pass-night 20:30", found)
                self.assertNotIn("clock:missed:2026-11-27:gateway-preflight 09:35", found)

    def test_missing_close_alerts_on_half_day_and_full_day_and_dead_orders(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "paper"
            with mock.patch.dict("os.environ", {"FR_PAPER_ACCOUNT": "DU0000000"}), \
                    mock.patch.object(book, "ROOT", root), \
                    mock.patch.object(journal, "JOURNAL", Path(d)):
                # Half day (Black Friday 2026-11-27, closes 13:00 ET): silent at 12:45, alerts at 12:52!
                self.assertEqual(watch.missing_close(at("2026-11-27T12:45")), [])
                found = watch.missing_close(at("2026-11-27T12:52"))
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0][0], "close:2026-11-27")
                # Once clean exits file is written, alert clears
                book._write(root / "exits" / "2026-11-27.json", {"session": "2026-11-27", "exits": [{"trade_id": "T1"}],
                                                                  "orders": [{"trade_id": "T1", "symbol": "AAA", "kind": "MOC", "status": "PreSubmitted"}]})
                self.assertEqual(watch.missing_close(at("2026-11-27T12:55")), [])
                # DEAD order in exits file STILL alerts
                book._write(root / "exits" / "2026-11-27.json", {"session": "2026-11-27", "exits": [{"trade_id": "T1"}],
                                                                  "orders": [{"trade_id": "T1", "symbol": "AAA", "kind": "MOC", "status": "Cancelled"}]})
                dead_found = watch.missing_close(at("2026-11-27T12:55"))
                self.assertEqual(len(dead_found), 1)
                self.assertIn("rejected/dead", dead_found[0][2])

    def test_stuck_closing_alerts_after_15_min_but_ignores_pending_today_moc(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "paper"
            with mock.patch.dict("os.environ", {"FR_PAPER_ACCOUNT": "DU0000000"}), \
                    mock.patch.object(book, "ROOT", root), \
                    mock.patch.object(journal, "JOURNAL", Path(d)):
                t = {"id": "T1", "book": "dynamic", "name": "AAA", "entry_date": "2026-11-25", "horizon_days": 3,
                     "legs": [{"symbol": "AAA", "side": "long", "shares": 100, "ref_price": 10.0}], "notional": 1000, "stop": {}}
                book._write(root / "entries" / "2026-11-25.json", {"session": "2026-11-25", "trades": [t], "exits": [], "runs": []})
                book._write(root / "fills" / "2026-11-25.json", {"session": "2026-11-25", "fills": [
                    {"order_ref": "T1|AAA", "symbol": "AAA", "side": "BOT", "shares": 100.0, "price": 10.0, "commission": 1.0, "exec_id": "E1"}
                ]})
                # Live MOC order placed at 12:41 on half day -> NOT stuck at 12:55!
                book._write(root / "exits" / "2026-11-27.json", {"session": "2026-11-27", "exits": [{"trade_id": "T1"}],
                                                                  "orders": [{"trade_id": "T1", "symbol": "AAA", "kind": "MOC", "status": "PreSubmitted"}]})
                self.assertEqual(watch.stuck_closing(at("2026-11-27T12:55")), [])
                # Still unfilled 25 minutes AFTER the 13:00 close -> stuck!
                self.assertEqual(len(watch.stuck_closing(at("2026-11-27T13:25"))), 1)


if __name__ == "__main__":
    unittest.main()

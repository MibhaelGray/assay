"""The schedule's clock (deploy/launchd/clock.py): timed jobs start on New York time, whatever zone the Mac is in."""

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "launchd"))
import clock  # noqa: E402
import install  # noqa: E402

NY = ZoneInfo("America/New_York")


def ny(day: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=NY)


def test_morning_pass_starts_at_945_new_york_and_only_once():
    state = {"day": "2026-11-27", "started": []}
    early, state = clock.due(ny("2026-11-27", "09:44"), state)
    assert "pass-morning" not in early
    jobs, state = clock.due(ny("2026-11-27", "09:45"), state)
    assert "pass-morning" in jobs
    again, state = clock.due(ny("2026-11-27", "09:46"), state)
    assert again == []


@pytest.mark.parametrize("zone", ["America/Los_Angeles", "America/Chicago", "Asia/Tokyo", "UTC"])
def test_the_macs_time_zone_changes_nothing(zone, monkeypatch):
    """6:45 AM in California, 8:45 AM in Chicago: the same instant, so the same jobs come due."""
    instant = datetime(2026, 11, 27, 14, 45, 20, tzinfo=timezone.utc)   # 9:45:20 AM New York
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    try:
        jobs, _ = clock.due(instant.astimezone(NY), {"day": "2026-11-27", "started": ["gateway-preflight 09:35"]})
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    assert jobs == ["pass-morning"]


def test_first_run_starts_nothing_already_past():
    jobs, state = clock.due(ny("2026-11-27", "13:25"), {})
    assert jobs == []
    assert "pass-morning 09:45" in state["started"] and "bars 16:30" not in state["started"]
    later, _ = clock.due(ny("2026-11-27", "16:30"), state)
    assert set(later) == {"side-facts", "gateway-preflight", "paper-close", "paper-sync", "bars"}


def test_a_new_day_owes_everything_since_midnight_and_starts_each_job_once():
    """The Mac slept from Thursday night to 11:20 AM Friday: both pre-flight times are past, the job starts once."""
    jobs, state = clock.due(ny("2026-11-27", "11:20"), {"day": "2026-11-26", "started": ["nightly 19:00"]})
    assert jobs.count("gateway-preflight") == 1 and "pass-morning" in jobs and "side-facts" in jobs
    assert state["day"] == "2026-11-27" and "nightly 19:00" not in state["started"]


def test_weekend_days():
    saturday, _ = clock.due(ny("2026-11-28", "23:59"), {"day": "2026-11-28", "started": []})
    assert saturday == []
    sunday, _ = clock.due(ny("2026-11-29", "20:30"), {"day": "2026-11-29", "started": []})
    assert set(sunday) == {"keep-awake", "gateway-preflight", "pass-night"}


def test_every_scheduled_job_is_installed_without_a_calendar_and_the_clock_ticks():
    for job in install.SCHEDULE:
        assert "StartCalendarInterval" not in install.plist(job, "/usr/bin") and "StartInterval" not in install.plist(job, "/usr/bin")
    assert install.plist("clock", "/usr/bin", install.EVERY["clock"])["StartInterval"] == 30
    assert set(install.SUNDAY) <= set(install.SCHEDULE)

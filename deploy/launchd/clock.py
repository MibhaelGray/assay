"""The schedule's clock: launchd runs this every 30 seconds, and it starts each job that has come due in New York time.

The timed jobs have no launchd calendar of their own. launchd's calendar goes by the Mac's time zone, and the agent
that keeps it holds on to the zone it started in: after the Mac moved from Central to Pacific time on 2026-11-25,
every timed job started two hours early, so the morning pass of 2026-11-27 was refused as too early and the close
job skipped itself. This clock reads New York time from the system clock, which is the same instant whatever zone
the Mac shows, so the schedule (install.py's SCHEDULE and SUNDAY) holds wherever the Mac is.

A job missed while the Mac slept starts on the first tick after it wakes, while it is still the same day in New
York; each job's own checks (run.sh, `fr trading-day`, the pass windows) decide whether a late start still goes on.
What has been started is kept in data/clock.json. With no such file (a first install) nothing already past is
started. Runs on the Mac's own python3 (3.9), standard library only.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from install import AGENTS, LABEL, REPO, SCHEDULE, SUNDAY

NY = ZoneInfo("America/New_York")
STATE = REPO / "data" / "clock.json"


def todays(day: date) -> list[tuple[str, str]]:
    """(New York time, job) for everything scheduled on this New York day, in time order."""
    table = SCHEDULE if day.weekday() < 5 else SUNDAY if day.weekday() == 6 else {}
    return sorted((t, job) for job, times in table.items() for t in times)


def due(now: datetime, state: dict) -> tuple[list[str], dict]:
    """The jobs to start at `now` (New York time), and the state to keep. A job with two times due starts once."""
    today, hhmm = now.date().isoformat(), now.strftime("%H:%M")
    past = [(job, f"{job} {t}") for t, job in todays(now.date()) if t <= hhmm]
    if not state.get("day"):
        started = {key for _, key in past}          # first run: what is already past was never this clock's to start
    elif state["day"] != today:
        started = set()                             # a new day: everything due since midnight is owed
    else:
        started = set(state.get("started", []))
    jobs = list(dict.fromkeys(job for job, key in past if key not in started))
    return jobs, {"day": today, "started": sorted(started | {key for _, key in past}),
                  "tick": now.isoformat(timespec="seconds")}


def start(job: str) -> str | None:
    """Start a job's launchd agent. Returns what went wrong, or None. A job that isn't installed is left alone."""
    label = LABEL.format(job)
    if not (AGENTS / f"{label}.plist").exists():
        return None
    r = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True)
    return None if r.returncode == 0 else (r.stderr or r.stdout).strip() or f"launchctl exited {r.returncode}"


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def main() -> None:
    now = datetime.now(NY)
    jobs, state = due(now, _read(STATE))
    # Written before anything starts: a tick that dies here skips a job, which is safer than starting it twice.
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, STATE)
    for job in jobs:
        problem = start(job)
        print(f"{now:%Y-%m-%d %H:%M:%S} New York: started {job}" + (f", but launchctl said: {problem}" if problem else ""),
              flush=True)


if __name__ == "__main__":
    main()

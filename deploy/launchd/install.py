"""Install the collector's schedule on a Mac as launchd agents.

The schedule is written in New York time and converted to this Mac's time zone, since launchd only
knows local time. Each job starts deploy/run.sh with /bin/bash, and the script keeps the Mac awake until
it finishes. Keep the repo outside ~/Documents, which launchd jobs can't read without Full Disk Access.
Logs go to ~/Library/Logs/factor-rotation/<job>.log.

    python3 deploy/launchd/install.py              # install or reinstall every job
    python3 deploy/launchd/install.py paper-open   # only the jobs named; reinstalling stops a job that is running
    python3 deploy/launchd/install.py --dry-run    # print the plists, change nothing
    python3 deploy/launchd/install.py --uninstall
    python3 deploy/launchd/install.py --design three-pass   # the three-pass swing book's jobs; removes the retired ones

Two schedules. The default is the first book's (one evening pass, entries at the open, the intraday book). `--design
three-pass` is the book restarted on 2026-11-15's design: its order job runs every minute and trades a pass's
verdicts as soon as they are recorded, so the jobs that waited for the open or acted on a midday check, and the
intraday book's ticks, are removed (RETIRED).
"""

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parents[2]
AGENTS = Path.home() / "Library" / "LaunchAgents"
LOGS = Path.home() / "Library" / "Logs" / "factor-rotation"
LABEL = "com.factor-rotation.{}"

EXT_EVENING = ["16:46", *[f"{h:02d}:{m:02d}" for h in range(17, 24) for m in (1, 16, 31, 46) if (h, m) != (20, 1)]]

# job -> times on NYSE weekdays, New York time
SCHEDULE = {
    "options-all": ["10:00", "15:30"],
    "options-etfs": ["11:00", "12:00", "13:00", "14:00"],
    "bars": ["16:30"],
    "nightly": ["19:00"],
    # Paper trading: entries into the opening auction, a check on their fills, exits into the closing auction,
    # then the fills. Market-on-open orders must be in by 9:28 AM and market-on-close by 3:50 PM; the entry
    # job starts at 9:15 AM so it can wait out a Mac that has just woken with no network.
    "paper-open": ["09:15"],
    "paper-check": ["09:40"],
    # The midday check's exits (book.midday_session): the check is a pass in the Claude app at about 12:15 PM ET.
    # Two runs, so a check recorded late is still acted on; the second is a no-op when the first did the work.
    "paper-midday": ["13:15", "14:15"],
    "paper-close": ["15:45"],
    "paper-sync": ["16:20"],
    # The takes' entries outside regular hours (paper/early.py): overnight after the evening pass, at the start of
    # the pre-market, and after the pre-open pass. Sunday night too, for Monday's takes (SUNDAY below).
    "paper-early": ["21:00", "04:05", "09:00"],
    # The intraday book: a minute after each 15-minute bar closes, from the 10:00 AM bar to the 3:45 PM one,
    # which is where every trade is out. Haiku's takes go to the paper account (intraday/orders.py).
    # The swing book's pre-close review as a background job, on trial: it works on a copy of the journal, and the
    # pass scheduled in the Claude app is still the one that counts.
    "pass-pre-close": ["15:00"],
    # Side facts for the night's verdicts (paper/side.py): after the evening pass, which has recorded as early as
    # 7:48 PM and as late as 11:15 PM ET, with two morning runs for a late pass or a failed pull. A run with
    # nothing left to pull does nothing, and none pulls after the open.
    "side-facts": ["21:30", "23:45", "06:00", "08:45"],
    "intraday-tick": [f"{h:02d}:{m:02d}" for h in range(10, 16) for m in (1, 16, 31, 46)],
    # The intraday book outside regular hours (intraday/extended.py): after-hours from 4:46 PM (once the 4:30 PM
    # daily bars are in), overnight, pre-market, and a 9:31 AM clean-up of anything still held.
    # Keeps the Mac from idle-sleeping from the close to 9:40 AM ET, for the extended-hours ticks and early entries.
    "keep-awake": ["16:40"],
    "intraday-ext": [*EXT_EVENING, *[f"{h:02d}:{m:02d}" for h in range(0, 4) for m in (1, 16, 31, 46) if (h, m) <= (3, 46)],
                     *[f"{h:02d}:{m:02d}" for h in range(4, 10) for m in (1, 16, 31, 46) if (4, 16) <= (h, m) <= (9, 16)], "09:31"],
}


# The three-pass swing book (paper/entries.py, paper/daily.py). The passes themselves are scheduled tasks in the
# Claude app at 7:30 PM (Sunday to Thursday), 8:45 AM and 12:00 PM Central. Here: the data jobs as before, the
# close job for stops and time limits, the sync, and the side facts about an hour and a half after each pass starts.
THREE_PASS = {
    "options-all": ["10:00", "15:30"],
    "options-etfs": ["11:00", "12:00", "13:00", "14:00"],
    "bars": ["16:30"],
    "nightly": ["19:00"],
    "paper-close": ["15:45"],
    "paper-sync": ["16:20"],
    "side-facts": ["22:15", "23:45", "11:15", "14:15"],
    "keep-awake": ["16:40"],
    # The three passes as background jobs (passes.py), since 2026-11-16: 7:30 PM, 8:45 AM and 12:00 PM Central.
    # Headless, so the judge is always Fable 5.2 at high effort whatever the Claude app is set to.
    "pass-night": ["20:30"],
    "pass-morning": ["09:45"],
    "pass-midday": ["13:00"],
}
THREE_PASS_SUNDAY = {"keep-awake": ["19:55"], "side-facts": ["22:15", "23:45"], "pass-night": ["20:30"]}
# job -> seconds between runs, every day. The order job looks for a newly recorded pass each minute; a run with
# nothing to do reads two small files and ends.
EVERY = {"paper-enter": 60}
RETIRED = ["paper-open", "paper-check", "paper-midday", "paper-early", "pass-pre-close", "intraday-tick", "intraday-ext"]

# job -> New York times on Sundays as well
SUNDAY = {"keep-awake": ["19:55"], "paper-early": ["21:00"], "side-facts": ["21:30", "23:45"], "intraday-ext": [t for t in EXT_EVENING if t >= "20:16"]}


def local_zone() -> ZoneInfo:
    link = os.path.realpath("/etc/localtime")
    return ZoneInfo(link.split("zoneinfo/")[-1]) if "zoneinfo/" in link else ZoneInfo("America/New_York")


def calendar(times: list[str], tz: ZoneInfo, week: date) -> list[dict]:
    """launchd StartCalendarInterval entries (Weekday 0 = Sunday) for Monday-Friday at each New York time."""
    entries = []
    monday = week - timedelta(days=week.weekday())
    for offset in range(5):
        for t in times:
            hour, minute = map(int, t.split(":"))
            local = datetime.combine(monday + timedelta(days=offset), datetime.min.time(), NY).replace(
                hour=hour, minute=minute).astimezone(tz)
            entries.append({"Weekday": (local.weekday() + 1) % 7, "Hour": local.hour, "Minute": local.minute})
    return entries


def sunday(times: list[str], tz: ZoneInfo, week: date) -> list[dict]:
    """launchd entries for Sunday at each New York time."""
    day = week - timedelta(days=week.weekday()) + timedelta(days=6)
    out = []
    for t in times:
        hour, minute = map(int, t.split(":"))
        local = datetime.combine(day, datetime.min.time(), NY).replace(hour=hour, minute=minute).astimezone(tz)
        out.append({"Weekday": (local.weekday() + 1) % 7, "Hour": local.hour, "Minute": local.minute})
    return out


def plist(job: str, entries: list[dict], path_env: str, every=None) -> dict:
    return {
        "Label": LABEL.format(job),
        "ProgramArguments": ["/bin/bash", str(REPO / "deploy" / "run.sh"), job],
        "WorkingDirectory": str(REPO),
        "EnvironmentVariables": {"PATH": path_env},
        **({"StartInterval": every} if every else {"StartCalendarInterval": entries}),
        "StandardOutPath": str(LOGS / f"{job}.log"),
        "StandardErrorPath": str(LOGS / f"{job}.log"),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--design", choices=["first", "three-pass"], default="first",
                   help="which book's schedule: the first book's (default) or the three-pass swing book's")
    p.add_argument("jobs", nargs="*", choices=[[], *SCHEDULE, *THREE_PASS, *EVERY], help="only these jobs (default: all)")
    a = p.parse_args()
    three = a.design == "three-pass"
    table, sundays = (THREE_PASS, THREE_PASS_SUNDAY) if three else (SCHEDULE, SUNDAY)
    every = {job: n for job, n in EVERY.items() if three and (not a.jobs or job in a.jobs)}
    schedule = {job: times for job, times in table.items() if not a.jobs or job in a.jobs}
    domain = f"gui/{os.getuid()}"
    if three and not a.jobs and not a.uninstall:
        for job in RETIRED:
            path = AGENTS / f"{LABEL.format(job)}.plist"
            if a.dry_run:
                print(f"would remove {path.name}")
            elif path.exists():
                subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
                path.unlink()
                print(f"removed {path.name} (retired with the first book)")

    if a.uninstall:
        for job in [*schedule, *every]:
            path = AGENTS / f"{LABEL.format(job)}.plist"
            subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
            path.unlink(missing_ok=True)
            print(f"removed {path.name}")
        return

    tz = local_zone()
    winter, summer = calendar(["10:00"], tz, date(2027, 1, 11)), calendar(["10:00"], tz, date(2027, 7, 12))
    if winter != summer:
        sys.exit(f"{tz.key} doesn't shift for daylight saving with New York; set the Mac to a US time zone first")
    uv = shutil.which("uv")
    if not uv:
        sys.exit("uv is not on PATH; install it first")
    path_env = ":".join(dict.fromkeys([str(Path(uv).parent), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                                       "/usr/sbin", "/sbin"]))
    LOGS.mkdir(parents=True, exist_ok=True)
    print(f"time zone {tz.key}; jobs scheduled in New York time")
    for job, times in schedule.items():
        doc = plist(job, calendar(times, tz, date.today()) + sunday(sundays.get(job, []), tz, date.today()), path_env)
        path = AGENTS / f"{doc['Label']}.plist"
        when = ", ".join(f"{e['Hour']:02d}:{e['Minute']:02d}" for e in doc["StartCalendarInterval"][: len(times)])
        if a.dry_run:
            print(f"\n{path}  (local {when}, Mon-Fri)\n{plistlib.dumps(doc).decode()}")
            continue
        AGENTS.mkdir(parents=True, exist_ok=True)
        subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
        path.write_bytes(plistlib.dumps(doc))
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
        print(f"installed {doc['Label']}: local {when}, Mon-Fri ({', '.join(times)} New York)")
    for job, seconds in every.items():
        doc = plist(job, [], path_env, seconds)
        path = AGENTS / f"{doc['Label']}.plist"
        if a.dry_run:
            print(f"\n{path}  (every {seconds} seconds)\n{plistlib.dumps(doc).decode()}")
            continue
        AGENTS.mkdir(parents=True, exist_ok=True)
        subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
        path.write_bytes(plistlib.dumps(doc))
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
        print(f"installed {doc['Label']}: every {seconds} seconds")


if __name__ == "__main__":
    main()

"""Install the collector's schedule on a Mac as launchd agents.

The schedule is written in New York time and converted to this Mac's time zone, since launchd only
knows local time. Each job starts deploy/run.sh with /bin/bash, and the script keeps the Mac awake until
it finishes. Keep the repo outside ~/Documents, which launchd jobs can't read without Full Disk Access.
Logs go to ~/Library/Logs/factor-rotation/<job>.log.

    python3 deploy/launchd/install.py              # install or reinstall every job
    python3 deploy/launchd/install.py pass-night   # only the jobs named; reinstalling stops a job that is running
    python3 deploy/launchd/install.py --dry-run    # print the plists, change nothing
    python3 deploy/launchd/install.py --uninstall
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

# job -> times on NYSE weekdays, New York time. The data jobs, the three passes (passes.py: headless, so the judge
# is always Fable 5.2 at high effort whatever the Claude app is set to), the close job for stops and time limits,
# the sync, and the side facts about an hour and a half after each pass starts. Option collection has no job since
# 2026-11-17 (owner's call): `fr options` still works by hand and deploy/run.sh still has its two cases.
SCHEDULE = {
    "bars": ["16:30"],
    "nightly": ["19:00"],
    "paper-close": ["15:45"],
    "paper-sync": ["16:20"],
    "side-facts": ["22:15", "23:45", "11:15", "14:15"],
    "keep-awake": ["16:40"],
    # 7:30 PM, 8:45 AM and 12:00 PM Central.
    "pass-night": ["20:30"],
    "pass-morning": ["09:45"],
    "pass-midday": ["13:00"],
    # 4:30 PM Central: the small-cap book's shadow pass, once today's daily bars are in.
    "pass-smallcap": ["17:30"],
    # 9:30 AM and 1:00 PM Central: its morning and midday passes, on the session so far (regular hours only).
    "pass-smallcap-morning": ["10:30"],
    "pass-smallcap-midday": ["14:00"],
}
# job -> New York times on Sundays as well: the night pass for Monday's session
SUNDAY = {"keep-awake": ["19:55"], "side-facts": ["22:15", "23:45"], "pass-night": ["20:30"]}
# job -> seconds between runs, every day. The order job looks for a newly recorded pass each minute; a run with
# nothing to do reads two small files and ends. The watchdog (watch.py) checks every five minutes for anything broken.
# The public site (publish.py) is refreshed every 15 minutes; GitHub allows about ten site builds an hour.
EVERY = {"paper-enter": 60, "watch": 300, "publish": 900}
# The research lab's unattended sessions (docs/research-lab.md): job -> {day (0 = Monday): New York times}. Placed
# clear of the trading passes so the two never share Claude's usage window: 11:00 PM Central on weeknights and
# Sunday (after the night pass), 7:00 PM Central on Friday, four on Saturday and two on Sunday morning. Twelve a week.
# Not part of "install every job": it is switched on by name (`install.py lab-session`) and off with --uninstall.
BY_DAY = {"lab-session": {0: ["00:00"], 1: ["00:00"], 2: ["00:00"], 3: ["00:00"], 4: ["00:00", "20:00"],
                          5: ["09:00", "13:00", "17:00", "21:00"], 6: ["09:00", "13:00"]}}

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


def by_day(days: dict[int, list[str]], tz: ZoneInfo, week: date) -> list[dict]:
    """launchd entries for the New York times listed under each day (0 = Monday)."""
    monday = week - timedelta(days=week.weekday())
    out = []
    for offset, times in days.items():
        for t in times:
            hour, minute = map(int, t.split(":"))
            local = datetime.combine(monday + timedelta(days=offset), datetime.min.time(), NY).replace(hour=hour, minute=minute).astimezone(tz)
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
    p.add_argument("jobs", nargs="*", choices=[[], *SCHEDULE, *EVERY, *BY_DAY], help="only these jobs (default: all but the lab's)")
    a = p.parse_args()
    every = {job: n for job, n in EVERY.items() if not a.jobs or job in a.jobs}
    schedule = {job: times for job, times in SCHEDULE.items() if not a.jobs or job in a.jobs}
    named = {job: days for job, days in BY_DAY.items() if job in a.jobs}
    domain = f"gui/{os.getuid()}"
    if a.uninstall:
        for job in [*schedule, *every, *named]:
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
        doc = plist(job, calendar(times, tz, date.today()) + sunday(SUNDAY.get(job, []), tz, date.today()), path_env)
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
    for job, days in named.items():
        doc = plist(job, by_day(days, tz, date.today()), path_env)
        path = AGENTS / f"{doc['Label']}.plist"
        count = len(doc["StartCalendarInterval"])
        if a.dry_run:
            print(f"\n{path}  ({count} runs a week)\n{plistlib.dumps(doc).decode()}")
            continue
        AGENTS.mkdir(parents=True, exist_ok=True)
        subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
        path.write_bytes(plistlib.dumps(doc))
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
        print(f"installed {doc['Label']}: {count} runs a week")
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

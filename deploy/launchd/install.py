"""Install the project's schedule on a Mac as launchd agents.

The schedule is written in New York time and holds whatever time zone the Mac is set to. launchd's own calendar
goes by the Mac's zone (and keeps a stale one after the zone changes), so the timed jobs are installed with no
calendar: the `clock` job (clock.py) runs every 30 seconds and starts each one when its New York time comes.
Each job starts deploy/run.sh with /bin/bash, and the script keeps the Mac awake until it finishes. Keep the repo outside ~/Documents, which launchd jobs can't read without Full Disk Access.
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
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AGENTS = Path.home() / "Library" / "LaunchAgents"
LOGS = Path.home() / "Library" / "Logs" / "factor-rotation"
LABEL = "com.factor-rotation.{}"

# job -> times on NYSE weekdays, New York time. The data jobs, the three passes (passes.py: headless, so the judge
# is always Fable 5.2 at high effort whatever the Claude app is set to), the close job for stops and time limits,
# the sync, and the side facts about an hour and a half after each pass starts.
SCHEDULE = {
    "bars": ["16:30"],
    "nightly": ["19:00"],
    "paper-close": ["15:45", "12:45"],   # the second is for half days (run.sh lets the one before the close run)
    "paper-sync": ["16:20"],
    "side-facts": ["22:15", "23:45", "11:15", "14:15"],
    "keep-awake": ["16:40"],
    # 10 min before each pass (7:20 PM, 8:35 AM, 11:50 AM CT), mid-morning (10:15 AM CT), and 5 min before close (2:40 PM / 11:40 AM CT).
    "gateway-preflight": ["09:35", "11:15", "12:40", "12:50", "15:40", "20:20"],
    # 7:30 PM, 8:45 AM and 12:00 PM Central.
    "pass-night": ["20:30"],
    "pass-morning": ["09:45"],
    "pass-midday": ["13:00"],
}
# job -> New York times on Sundays as well: the night pass for Monday's session
SUNDAY = {"keep-awake": ["19:55"], "side-facts": ["22:15", "23:45"], "gateway-preflight": ["20:20"], "pass-night": ["20:30"]}
# job -> seconds between runs, every day. The clock (clock.py) starts the timed jobs above. The order job looks for a newly recorded pass each minute; a run with
# nothing to do reads two small files and ends. The watchdog (watch.py) checks every five minutes for anything broken.
# The public site (publish.py) is refreshed every 15 minutes; GitHub allows about ten site builds an hour.
EVERY = {"clock": 30, "paper-enter": 60, "watch": 300, "publish": 900}


def plist(job: str, path_env: str, every=None) -> dict:
    """A job's launchd agent: started every `every` seconds, or with no trigger of its own (the clock starts it)."""
    return {
        "Label": LABEL.format(job),
        "ProgramArguments": ["/bin/bash", str(REPO / "deploy" / "run.sh"), job],
        "WorkingDirectory": str(REPO),
        "EnvironmentVariables": {"PATH": path_env},
        **({"StartInterval": every} if every else {}),
        "StandardOutPath": str(LOGS / f"{job}.log"),
        "StandardErrorPath": str(LOGS / f"{job}.log"),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("jobs", nargs="*", choices=[[], *SCHEDULE, *EVERY], help="only these jobs (default: all)")
    a = p.parse_args()
    every = {job: n for job, n in EVERY.items() if not a.jobs or job in a.jobs}
    schedule = {job: times for job, times in SCHEDULE.items() if not a.jobs or job in a.jobs}
    if schedule and not a.uninstall:
        every = {"clock": EVERY["clock"], **every}   # a timed job never starts without the clock
    domain = f"gui/{os.getuid()}"
    if a.uninstall:
        for job in [*schedule, *every]:
            path = AGENTS / f"{LABEL.format(job)}.plist"
            subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
            path.unlink(missing_ok=True)
            print(f"removed {path.name}")
        return

    uv = shutil.which("uv")
    if not uv:
        sys.exit("uv is not on PATH; install it first")
    path_env = ":".join(dict.fromkeys([str(Path(uv).parent), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                                       "/usr/sbin", "/sbin"]))
    LOGS.mkdir(parents=True, exist_ok=True)
    print("jobs scheduled in New York time, whatever time zone this Mac is set to")
    for job, times in schedule.items():
        doc = plist(job, path_env)
        path = AGENTS / f"{doc['Label']}.plist"
        when = ", ".join(times) + " New York, Mon-Fri" + (f"; {', '.join(SUNDAY[job])} Sunday" if job in SUNDAY else "")
        if a.dry_run:
            print(f"\n{path}  ({when}; started by the clock job)\n{plistlib.dumps(doc).decode()}")
            continue
        AGENTS.mkdir(parents=True, exist_ok=True)
        subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
        path.write_bytes(plistlib.dumps(doc))
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
        print(f"installed {doc['Label']}: {when}")
    for job, seconds in every.items():
        doc = plist(job, path_env, seconds)
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

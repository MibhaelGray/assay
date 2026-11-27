"""The schedule's clock: launchd runs this every 30 seconds, and it starts each job that has come due in New York time.

The timed jobs have no launchd calendar of their own. launchd's calendar goes by the Mac's time zone, and the agent
that keeps it holds on to the zone it started in: after the Mac moved from Central to Pacific time on 2026-11-25,
every timed job started two hours early, so the morning pass of 2026-11-27 was refused as too early and the close
job skipped itself. This clock reads New York time from the system clock, which is the same instant whatever zone
the Mac shows, so the schedule (install.py's SCHEDULE and SUNDAY) holds wherever the Mac is.

Hardening (2026-11-27 audit):
- Serialized with fcntl.flock + PID-unique atomic temp files so two ticks can never race or corrupt clock.json.
- Per-job catch-up windows (`MAX_LATE`): a job missed while the Mac slept catches up if it is still within its valid
  window; a stale slot past its window is recorded in `missed` (so watch.py alerts) instead of firing hours late and
  crashing with noisy errors.
- Only marks a slot `started` AFTER `launchctl kickstart` succeeds. If a service was unloaded (`Could not find service`
  / exit 113), automatically bootstraps its plist and retries. Transient kickstart errors retry up to `MAX_RETRIES`
  ticks and are recorded in `errors` so watch.py alerts immediately.
- `keep-awake` is kickstarted with `-k` so each daytime/overnight block cleanly replaces an older caffeinate.

Runs on the Mac's own python3 (3.9+), standard library only.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from install import AGENTS, LABEL, REPO, SCHEDULE, SUNDAY

NY = ZoneInfo("America/New_York")
STATE = REPO / "data" / "clock.json"
LOCK = REPO / "data" / ".clock.lock"
AWAKE_PID = REPO / "data" / ".keep-awake.pid"

# Maximum minutes past scheduled NY time a slot may still catch up if the Mac was asleep.
# None means any time remaining in the same New York day is valid.
MAX_LATE: dict[str, int | None] = {
    "pass-morning": 135,       # 09:45 -> up to 12:00 ET (leaves 30 min before 12:30 cutoff)
    "pass-midday": 120,        # 13:00 -> up to 15:00 ET (leaves 30 min before 15:30 cutoff)
    "pass-night": 205,         # 20:30 -> up to 23:55 ET
    "paper-close": 13,         # 12:41->12:54, 12:46->12:59, 15:41->15:54, 15:46->15:59 (never fires at or after close)
    "gateway-preflight": 15,   # no point running a preflight hours after its target job
    "keep-awake": 720,         # always re-arm sleep prevention after waking during an active block
    "bars": None,
    "paper-sync": None,
    "nightly": None,
    "side-facts": None,
}
CRITICAL = {"pass-morning", "pass-midday", "pass-night", "paper-close"}
MAX_RETRIES = 5
KILL_BEFORE_START = {"keep-awake"}


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def todays(day: date) -> list[tuple[str, str]]:
    """(New York time, job) for everything scheduled on this New York day, in time order."""
    table = SCHEDULE if day.weekday() < 5 else SUNDAY if day.weekday() == 6 else {}
    return sorted((t, job) for job, times in table.items() for t in times)


def plan(now: datetime, state: dict) -> tuple[list[tuple[str, list[str]]], list[str], dict]:
    """Decide what is due to start at `now` (New York time), what has expired past its catch-up window, and the base
    state to update after start attempts.

    Returns (due_jobs, newly_missed, next_state), where `due_jobs` is [(job, [slot_keys...]), ...] in schedule order.
    Slots in `due_jobs` are NOT marked `started` yet — callers mark them once `start(job)` succeeds.
    """
    today, hhmm = now.date().isoformat(), now.strftime("%H:%M")
    now_min = now.hour * 60 + now.minute
    past = [(t, job, f"{job} {t}") for t, job in todays(now.date()) if t <= hhmm]
    if not state.get("day"):
        # First install / no state file: treat everything already past as already handled so we never storm old jobs.
        started = {key for _, _, key in past}
        missed: dict[str, str] = {}
        errors: dict[str, dict] = {}
        retries: dict[str, int] = {}
    elif state["day"] != today:
        started = set()
        missed = {}
        errors = {}
        retries = {}
    else:
        started = set(state.get("started", []))
        missed = dict(state.get("missed", {}))
        errors = dict(state.get("errors", {}))
        retries = dict(state.get("retries", {}))

    by_job: dict[str, list[str]] = {}
    newly_missed: list[str] = []
    for t, job, key in past:
        if key in started or key in missed:
            continue
        limit = MAX_LATE.get(job)
        late = now_min - _minutes(t)
        if limit is not None and late > limit:
            missed[key] = f"skipped at {hhmm} ET ({late}m past {t} ET; catch-up limit is {limit}m)"
            errors.pop(key, None)
            retries.pop(key, None)
            newly_missed.append(key)
            continue
        by_job.setdefault(job, []).append(key)

    next_state = {
        "day": today,
        "started": sorted(started),
        "missed": dict(sorted(missed.items())),
        "errors": dict(sorted(errors.items())),
        "retries": dict(sorted(retries.items())),
        "tick": now.isoformat(timespec="seconds"),
    }
    return list(by_job.items()), newly_missed, next_state


def due(now: datetime, state: dict) -> tuple[list[str], dict]:
    """Convenience helper: the job names due at `now` and the state assuming every due job starts cleanly."""
    items, _, next_state = plan(now, state)
    started = set(next_state["started"])
    jobs = []
    for job, keys in items:
        jobs.append(job)
        started.update(keys)
    next_state["started"] = sorted(started)
    return jobs, next_state


def start(job: str) -> str | None:
    """Start a job's launchd agent. Returns what went wrong, or None. Auto-bootstraps if unloaded."""
    label = LABEL.format(job)
    plist_path = AGENTS / f"{label}.plist"
    if not plist_path.exists():
        return None
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{label}"
    cmd = ["launchctl", "kickstart", *(["-k"] if job in KILL_BEFORE_START else []), target]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        return None
    msg = (r.stderr or r.stdout).strip()
    if r.returncode == 113 or "Could not find service" in msg:
        boot = subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], capture_output=True, text=True)
        if boot.returncode == 0:
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                return None
            msg = (r.stderr or r.stdout).strip()
    return msg or f"launchctl exited {r.returncode}"


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=1) + "\n")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def in_awake_window(now: datetime) -> bool:
    """Active trading week when the Mac should stay awake: Sunday 19:55 ET through Friday 20:00 ET."""
    wd, hm = now.weekday(), (now.hour, now.minute)
    if wd < 4:
        return True
    if wd == 4:
        return hm < (20, 0)
    if wd == 6:
        return hm >= (19, 55)
    return False


def _awake_alive(pid_path: Path = AWAKE_PID) -> bool:
    """True if the PID recorded in `data/.keep-awake.pid` is alive and is a caffeinate process."""
    try:
        pid = int(pid_path.read_text().strip())
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True, timeout=5)
        return "caffeinate" in r.stdout
    except Exception:
        return True


def ensure_awake(now: datetime, pid_path: Path = AWAKE_PID, starter=None) -> bool:
    """Self-heal keep-awake after a mid-day reboot or crash during the active trading week. Returns True if restarted."""
    if not in_awake_window(now) or _awake_alive(pid_path):
        return False
    problem = (starter or start)("keep-awake")
    if problem is None:
        print(f"{now:%Y-%m-%d %H:%M:%S} New York: re-armed keep-awake (no live caffeinate)", flush=True)
        return True
    print(f"{now:%Y-%m-%d %H:%M:%S} New York: failed to re-arm keep-awake: {problem}", flush=True)
    return False


def tick(now: datetime | None = None, state_path: Path = STATE, starter=None, awake_pid: Path | None = None) -> dict:
    """Run one serialized clock tick: evaluate due jobs, start them, retry/record failures, and persist state."""
    now = now or datetime.now(NY)
    starter = starter or start
    state = _read(state_path)
    items, newly_missed, next_state = plan(now, state)
    started = set(next_state["started"])
    missed = dict(next_state["missed"])
    errors = dict(next_state["errors"])
    retries = dict(next_state["retries"])
    started_jobs: set[str] = set()

    for key in newly_missed:
        print(f"{now:%Y-%m-%d %H:%M:%S} New York: missed {key} ({missed[key]})", flush=True)

    for job, keys in items:
        problem = starter(job)
        if problem is None:
            started.update(keys)
            started_jobs.add(job)
            for key in keys:
                errors.pop(key, None)
                retries.pop(key, None)
            print(f"{now:%Y-%m-%d %H:%M:%S} New York: started {job} ({', '.join(keys)})", flush=True)
        else:
            for key in keys:
                n = retries.get(key, 0) + 1
                if n >= MAX_RETRIES:
                    missed[key] = f"launchctl failed {n} times: {problem}"
                    retries.pop(key, None)
                    errors.pop(key, None)
                else:
                    retries[key] = n
                    errors[key] = {"at": now.isoformat(timespec="seconds"), "tries": n, "error": problem}
            print(f"{now:%Y-%m-%d %H:%M:%S} New York: failed to start {job} ({', '.join(keys)}): {problem}", flush=True)

    if awake_pid is not None and "keep-awake" not in started_jobs:
        ensure_awake(now, pid_path=awake_pid, starter=starter)

    final_state = {
        "day": next_state["day"],
        "started": sorted(started),
        **({"missed": dict(sorted(missed.items()))} if missed else {}),
        **({"errors": dict(sorted(errors.items()))} if errors else {}),
        **({"retries": dict(sorted(retries.items()))} if retries else {}),
        "tick": now.isoformat(timespec="seconds"),
    }
    _write(state_path, final_state)
    return final_state


def main() -> None:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            tick(awake_pid=AWAKE_PID)
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    main()

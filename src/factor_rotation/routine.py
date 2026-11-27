"""The daily routine and whether each step ran, for the dashboard's Today view.

Each step is judged by what it leaves behind, not by its process: a research pass's entry in the session's
proposals, the exit check's file, the sync's fills. A launchd job that ran but left nothing is shown as failed
with the last line of its log. Times are New York.
"""

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import bars, journal, storage
from .config import CT, NY

LOGS = Path.home() / "Library" / "Logs" / "factor-rotation"

# key, time, name, what, who runs it, days, minutes before a step with nothing to show is called missed
STEPS = [
    ("pass-morning", time(9, 45), "Morning pass", "Judges the pre-market and the first minutes of trading. Its takes trade as soon as it records.", "research", "session", 120),
    ("pass-midday", time(13, 0), "Midday pass", "Judges the morning's trading and reviews every open trade.", "research", "full", 120),
    ("paper-close", time(15, 41), "Paper close", "Enforces stops and time limits; exits go as market-on-close orders.", "job", "session", 20),
    ("paper-sync", time(16, 20), "Paper sync", "Pulls the day's fills, positions and account value.", "job", "session", 20),
    ("bars", time(16, 30), "Bars", "Five years of daily prices for every name.", "job", "session", 30),
    ("nightly", time(19, 0), "Nightly job", "Brief, earnings dates, scan and screen.", "job", "session", 60),
    ("pass-night", time(20, 30), "Night pass", "Judges the after-hours screen and records take or pass for the next session.", "research", "eve", 120),
]


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except ValueError:
        return None


def _at(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s).astimezone(NY) if s else None


def _pass(session: date, kind: str) -> tuple[datetime | None, str | None]:
    """When that pass recorded for the session, and what it left: its verdicts and takes."""
    doc = _read(journal.JOURNAL / "paper" / "proposals" / f"{session}.json") or {}
    found = [p for p in doc.get("passes", []) if p.get("pass") == kind]
    if not found:
        return None, None
    p = min(found, key=lambda p: p["at"])
    note = f"{p['proposals']} verdicts, {p['takes']} takes" if "proposals" in p and "takes" in p else None
    return _at(p["at"]), note


def _evidence(key: str, day: date) -> tuple[datetime | None, str | None]:
    """When the step left its mark on `day`, and a note. (None, note) when it left something unusable."""
    paper = journal.JOURNAL / "paper"
    if key in ("pass-morning", "pass-midday"):
        return _pass(day, key[5:])
    if key == "pass-night":
        return _pass(journal.next_trading_day(day), "night")
    if key == "paper-close":
        doc = _read(paper / "exits" / f"{day}.json")
        if not doc:
            return None, None
        note = "; ".join(doc.get("warnings") or []) or None
        orders = doc.get("orders", 0)
        note = f"{len(doc.get('exits', []))} exits, {len(orders) if isinstance(orders, list) else orders} orders" + (f"; {note}" if note else "")
        return _at(doc.get("built_at")), note
    if key == "paper-sync":
        doc = _read(paper / "fills" / f"{day}.json")
        at = _at(doc and doc.get("pulled_at"))
        cutoff = time(13, 15) if journal.is_half_day(day) else time(16, 15)
        return (at, None) if at and at.time() >= cutoff else (None, None)
    if key == "bars":  # the file's time only shows the latest pull, so earlier days come from the job's own log
        m = storage.modified(bars.bars_path("SPY"))
        m = m.astimezone(NY) if m else None
        if m and m.date() == day and m.time() >= time(16, 25):
            return m, None
        done = [l for l in _log_lines("bars", datetime.combine(day, time(16, 25), NY), datetime.combine(day, time(23, 59), NY)) if "bars_done" in l]
        if done:
            return datetime.strptime(done[-1][:19], "%Y-%m-%d %H:%M:%S").astimezone().astimezone(NY), None
        return None, None
    if key == "nightly":
        screen = _read(journal.JOURNAL / "screens" / f"{day}.json")
        brief = (journal.JOURNAL / "briefs" / f"{day}.json").exists()
        if not screen:
            return None, ("brief written but no screen" if brief else None)
        if not brief:
            return None, f"No brief: the job stopped early. A screen was built later, at {_at(screen.get('built_at')).astimezone(CT):%-I:%M %p} CT"
        return _at(screen.get("built_at")), None
    return None, None


def _log_lines(key: str, since: datetime, until: datetime) -> list[str]:
    """A launchd job's timestamped log lines between two times. The log is in the Mac's own time zone."""
    path = LOGS / f"{key}.log"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines()[-2000:]:
        try:
            at = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").astimezone().astimezone(NY)
        except ValueError:
            continue
        if since <= at < until:
            out.append(line.strip())
    return out


def _log_tail(key: str, since: datetime) -> str | None:
    """The last line the job logged on the day it was due, from its start time on."""
    lines = _log_lines(key, since, datetime.combine(since.date() + timedelta(days=1), time(0), NY))
    return lines[-1][:240] if lines else None


def _due(days: str, day: date) -> bool:
    if days == "session":
        return journal.is_trading_day(day)
    if days == "full":
        return journal.is_trading_day(day) and not journal.is_half_day(day)
    return journal.is_trading_day(day + timedelta(days=1))  # evening before any trading day


def check(day: date, now: datetime) -> list[dict]:
    """Each step's state on `day`: done, missed, failed, running, upcoming or off (not scheduled that day)."""
    out = []
    for key, at, name, what, who, days, grace in STEPS:
        if key == "paper-close" and journal.is_trading_day(day):
            step_at = (datetime.combine(day, journal.close_time(day)) - timedelta(minutes=19)).time()
        elif key == "paper-sync" and journal.is_trading_day(day):
            step_at = (datetime.combine(day, journal.close_time(day)) + timedelta(minutes=20)).time()
        else:
            step_at = at
        due = datetime.combine(day, step_at, NY)
        row = {"key": key, "time": f"{due.astimezone(CT):%-I:%M %p} CT", "name": name, "what": what, "who": who, "day": day.isoformat()}
        if not _due(days, day):
            out.append({**row, "state": "off", "detail": "Not scheduled on this day"})
            continue
        when, note = _evidence(key, day)
        if when:
            late = (when - due).total_seconds() / 60
            out.append({**row, "state": "done", "at": when.isoformat(), "late_min": round(late) if late > grace else None, "detail": note})
        elif now < due:
            out.append({**row, "state": "upcoming", "detail": note})
        elif now < due + timedelta(minutes=grace):
            out.append({**row, "state": "running", "detail": note or "Due; nothing recorded yet"})
        else:
            tail = _log_tail(key, due)   # every step is a launchd job with its own log, the passes included
            failed = bool(tail or note)
            out.append({**row, "state": "failed" if failed else "missed",
                        "detail": note or (f"Ran but left nothing. Last log line: {tail}" if tail else "Nothing recorded")})
    return out


def rows(now: datetime | None = None) -> list[dict]:
    """The routine for the last session before today and for today, one `routine` row per step and day."""
    now = now or datetime.now(NY)
    today = now.date()
    before = journal.previous_trading_day(today)
    return [{"section": "routine", **r} for d in (before, today) for r in check(d, now)]

"""The watchdog: `fr watch`, every five minutes. It sends a phone alert when something the book depends on breaks.

The owner asked on 2026-11-17 to hear about failures at once. That morning the paper gateway lost IBKR and orders
sat for an hour before anyone looked. Alerts go to the ntfy app (https://ntfy.sh) on the topic in FR_NTFY_TOPIC, to
Telegram through the owner's bot (FR_TELEGRAM_TOKEN, FR_TELEGRAM_CHAT), and as a Mac notification. The topic is a long random name, which is all that keeps it private, so an alert never carries
an account number, a position or a dollar amount: only what broke and where to look.

What it checks:
- a scheduled job that exited with an error (launchd keeps each job's last exit status);
- the schedule's clock (deploy/launchd/clock.py), which starts every timed job, hasn't ticked for 10 minutes;
- a scheduled research pass that hasn't recorded 75 minutes after it started, or whose log says it failed;
- the order job's last two runs both left a trade unsent for want of a quote from the gateway;
- the paper gateway doesn't answer, or answers but has lost IBKR, on two checks in a row;
- the gateway is up but refused quotes (IBKR 10197) on two checks in a row while an order could go out, because
  the owner's live IBKR login is open elsewhere, such as the phone app (since 2026-11-18).

The same alert isn't sent again for REPEAT. Nothing here changes the book; it only reports.
"""

import asyncio
import json
import os
import subprocess
import sys
import urllib.request
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import storage
from .config import NY

STATE = Path("data/watch.json")
REPEAT = timedelta(hours=3)
PASS_START_LATE = timedelta(minutes=15)   # alert quickly if a scheduled pass never even started
PASS_LATE = timedelta(minutes=95)         # legacy finished-log deadline when no running sentinel exists
PASS_STUCK = timedelta(minutes=155)       # past passes.TIMEOUT (150 min) while still marked running
PASS_LOGS = Path("data/pass-logs")
CLOCK = Path("data/clock.json")     # written by deploy/launchd/clock.py on every tick, 30 seconds apart
CLOCK_LATE = timedelta(minutes=10)
CLOCK_CRITICAL = {"pass-morning", "pass-midday", "pass-night", "paper-close"}
CLOSING_STUCK = timedelta(minutes=15)
LABEL = "com.factor-rotation."
# The scheduled passes, New York time: (pass, start, which days). Night runs Sunday to Thursday; the other two on
# trading days. Kept beside deploy/launchd/install.py, which starts them.
PASSES = (("night", time(20, 30), "sun-thu"), ("morning", time(9, 45), "trading"), ("midday", time(13, 0), "full"))   # not on a half day
# IBKR's nightly reset drops every gateway for a while; a gateway that is down then is not news.
RESET = (time(23, 30), time(1, 15))
# Gateway messages that mean it is up but has lost IBKR's servers.
LOST = {1100, 2110}
PROBE_CLIENT = 79
QUOTE_CLIENT = 78         # the quote check's own connection (paper/gateway.py's is 77)
QUOTE_SYMBOL = "SPY"
LOGGED_IN_ELSEWHERE = "10197"   # IBKR's error when the live login is open somewhere else (gateway.QUOTE_REFUSED)


# ---- sending -------------------------------------------------------------------------------------------------

def telegram(title: str, text: str) -> bool:
    """Send through the owner's Telegram bot if FR_TELEGRAM_TOKEN and FR_TELEGRAM_CHAT are set. True if it went out."""
    token, chat = os.environ.get("FR_TELEGRAM_TOKEN", "").strip(), os.environ.get("FR_TELEGRAM_CHAT", "").strip()
    if not token or not chat:
        return False
    body = json.dumps({"chat_id": chat, "text": f"{title}\n{text}", "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"telegram send failed: {type(e).__name__}", file=sys.stderr)   # never print the URL: it holds the token
        return False


def telegram_chats() -> list[str]:
    """The chats that have messaged the bot lately (Telegram's getUpdates), to find FR_TELEGRAM_CHAT once."""
    token = os.environ.get("FR_TELEGRAM_TOKEN", "").strip()
    if not token:
        return []
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15) as r:
        data = json.loads(r.read())
    chats = {}
    for u in data.get("result", []):
        c = (u.get("message") or u.get("my_chat_member") or {}).get("chat") or {}
        if c.get("id"):
            chats[str(c["id"])] = c.get("username") or c.get("first_name") or c.get("title") or ""
    return [f"{i} ({n})" for i, n in chats.items()]


def send(title: str, text: str) -> bool:
    """Push to ntfy and Telegram, whichever are set up, and raise a Mac notification. True if any phone push went out."""
    sent = telegram(title, text)
    topic = os.environ.get("FR_NTFY_TOPIC", "").strip()
    if topic:
        server = os.environ.get("FR_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        req = urllib.request.Request(f"{server}/{topic}", data=text.encode(), method="POST",
                                     headers={"Title": title.encode("ascii", "replace").decode(), "Priority": "high", "Tags": "warning"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                sent = 200 <= r.status < 300 or sent
        except Exception as e:
            print(f"ntfy push failed: {e!r}", file=sys.stderr)
    if sys.platform == "darwin":
        script = 'on run argv\ndisplay notification (item 1 of argv) with title (item 2 of argv)\nend run'
        try:
            subprocess.run(["osascript", "-e", script, text[:240], title], capture_output=True, timeout=10)
        except Exception:
            pass
    return sent


def _phone_set() -> bool:
    """Whether a phone channel (ntfy, or Telegram's token and chat) is set up at all."""
    env = lambda k: os.environ.get(k, "").strip()  # noqa: E731
    return bool(env("FR_NTFY_TOPIC") or (env("FR_TELEGRAM_TOKEN") and env("FR_TELEGRAM_CHAT")))


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    storage.atomic_write_text(STATE, json.dumps(state, indent=1, sort_keys=True))


# ---- checks: each returns a list of (key, title, text) ----------------------------------------------------------

def failed_jobs() -> list[tuple[str, str, str]]:
    """Scheduled jobs whose last run exited non-zero, from `launchctl list`."""
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []
    found = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[2].startswith(LABEL):
            continue
        job, status = parts[2][len(LABEL):], parts[1]
        if (job, status) == ("keep-awake", "-15"):   # stopped on purpose (launchctl kickstart -k, or by hand)
            continue
        if status not in ("0", "-") and job != "watch":
            found.append((f"job:{job}:{status}", f"Job failed: {job}",
                          f"The {job} job's last run exited with status {status}. Its log is ~/Library/Logs/factor-rotation/{job}.log."))
    return found


def _missed_slot_matters(slot: str, day: date, trading) -> bool:
    """Whether a critical slot marked missed by clock.py was actually due to run on `day` and still hasn't completed."""
    from . import journal
    from .paper import book

    parts = slot.split()
    job = parts[0]
    hhmm = parts[1] if len(parts) > 1 else ""
    if job not in CLOCK_CRITICAL:
        return False
    if job == "paper-close":
        if not trading(day):
            return False
        half = journal.is_half_day(day)
        if hhmm.startswith("12:") and not half:
            return False
        if hhmm.startswith("15:") and half:
            return False
        return not book.close_complete(day)[0]
    kind = job.removeprefix("pass-")
    days_kind = {"night": "sun-thu", "morning": "trading", "midday": "full"}.get(kind, "trading")
    if not _pass_days(days_kind, day, trading):
        return False
    return not _pass_recorded(kind, day, trading)


def stopped_clock(now: datetime, trading=None) -> list[tuple[str, str, str]]:
    """The clock that starts the timed jobs has stopped ticking, failed to kickstart a job, or skipped a critical
    job past its catch-up window. No file means it was never installed here."""
    try:
        doc = json.loads(CLOCK.read_text())
        tick = datetime.fromisoformat(doc["tick"])
    except (OSError, ValueError, KeyError):
        return []
    if trading is None:
        from .journal import is_trading_day as trading
    found: list[tuple[str, str, str]] = []
    if now - tick > CLOCK_LATE:
        found.append(("clock:stopped", "The schedule's clock has stopped",
                      f"No timed job (passes, close, nightly) will start until it runs again. It last ticked "
                      f"{int((now - tick).total_seconds() // 60)} minutes ago. Reinstall it with "
                      "`python3 deploy/launchd/install.py clock`; its log is ~/Library/Logs/factor-rotation/clock.log."))
    today = now.date().isoformat()
    if doc.get("day") == today:
        for slot, err in (doc.get("errors") or {}).items():
            job = slot.split()[0]
            msg = err.get("error") if isinstance(err, dict) else str(err)
            found.append((f"clock:err:{today}:{slot}", f"Schedule failed to start {job}",
                          f"The clock couldn't start {slot}: {msg}. Check ~/Library/Logs/factor-rotation/clock.log."))
        for slot, why in (doc.get("missed") or {}).items():
            if _missed_slot_matters(slot, now.date(), trading):
                job = slot.split()[0]
                found.append((f"clock:missed:{today}:{slot}", f"Missed scheduled job: {job}",
                              f"The clock missed {slot} ({why}). Check ~/Library/Logs/factor-rotation/clock.log."))
    return found


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _pass_recorded(kind: str, day: date, trading) -> bool:
    """True if `kind` actually recorded in `journal/paper/proposals/<session>.json`."""
    from .paper import proposals
    if kind == "night":
        session = day + timedelta(days=1)
        if not trading(session):
            return False
    else:
        session = day
    doc = proposals.load(session)
    return bool(doc and any(p.get("pass") == kind for p in doc.get("passes", [])))


def _pass_days(kind: str, day: date, trading) -> bool:
    if kind == "sun-thu":   # the night pass: the evening before a session (run.sh skips it when tomorrow is closed)
        return trading(day + timedelta(days=1))
    if kind == "full":
        from .journal import is_half_day
        return trading(day) and not is_half_day(day)
    return trading(day)


def missing_passes(now: datetime, trading=None) -> list[tuple[str, str, str]]:
    """A scheduled pass that never started (`PASS_START_LATE`), crashed/hung while running, or finished with ok=False."""
    if trading is None:
        from .journal import is_trading_day as trading
    logs = []
    for f in PASS_LOGS.glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if d.get("started") and not d.get("shadow"):
            logs.append((d.get("pass"), datetime.fromisoformat(d["started"]), d))
    found = []
    for kind, start, days in PASSES:
        for back in (0, 1):   # tonight's night pass, and last night's if it ran past midnight
            day = now.date() - timedelta(days=back)
            begin = datetime.combine(day, start, NY)
            if not (begin + PASS_START_LATE <= now < begin + timedelta(hours=12)) or not _pass_days(days, day, trading):
                continue
            if _pass_recorded(kind, day, trading):
                continue
            mine = [d for p, at, d in logs if p == kind and begin - timedelta(minutes=10) <= at <= begin + timedelta(hours=3)]
            when = begin.astimezone(_ct()).strftime("%-I:%M %p CT")
            if any(d.get("ok") for d in mine):
                continue
            if not mine:
                elapsed_min = int((now - begin).total_seconds() // 60)
                if now >= begin + PASS_LATE:
                    late_min = int(PASS_LATE.total_seconds() // 60)
                    found.append((f"pass:{kind}:{day}", f"No {kind} pass",
                                  f"The {kind} pass due at {when} has no log {late_min} minutes later. Check ~/Library/Logs/factor-rotation/pass-{kind}.log."))
                else:
                    found.append((f"pass-start:{kind}:{day}", f"{kind.capitalize()} pass did not start",
                                  f"The {kind} pass due at {when} has not started after {elapsed_min} minutes. Check clock.log and pass-{kind}.log."))
                continue
            latest = max(mine, key=lambda d: d["started"])
            if latest.get("status") == "running":
                pid = latest.get("pid")
                started_at = datetime.fromisoformat(latest["started"])
                if pid and not _pid_alive(pid):
                    found.append((f"pass:{kind}:{day}", f"The {kind} pass crashed",
                                  f"The {kind} pass that started at {when} (pid {pid}) exited without finishing. Check pass-{kind}.log."))
                elif now - started_at > PASS_STUCK:
                    mins = int((now - started_at).total_seconds() // 60)
                    found.append((f"pass:{kind}:{day}", f"The {kind} pass appears hung",
                                  f"The {kind} pass that started at {when} is still running after {mins} minutes. Check pass-{kind}.log."))
            else:
                why = latest.get("error") or "it did not record"
                found.append((f"pass:{kind}:{day}", f"The {kind} pass failed",
                              f"The {kind} pass that started at {when} failed: {why[:160]}. Its log is in data/pass-logs."))
    return found


def missing_close(now: datetime, trading=None) -> list[tuple[str, str, str]]:
    """Alert if today's close job hasn't completed cleanly by the 10-minute MOC cutoff (15:50 ET full, 12:50 ET half)."""
    from . import journal
    from .paper import book

    if not os.environ.get("FR_PAPER_ACCOUNT"):
        return []
    today = now.date()
    is_open = trading(today) if trading is not None else journal.is_trading_day(today)
    if not is_open:
        return []
    close_dt = datetime.combine(today, journal.close_time(today), NY)
    deadline = close_dt - timedelta(minutes=10)
    if not (deadline <= now < close_dt + timedelta(hours=4)):
        return []
    ok, why = book.close_complete(today)
    if ok:
        return []
    # If the book has no open or closing trades AND exits file isn't there, still alert because paper-close should always write exits/<today>.json.
    when_ct = close_dt.astimezone(_ct()).strftime("%-I:%M %p CT")
    return [(f"close:{today}", "Paper close missed or failed",
             f"Today's close job (market closes {when_ct}) has not completed cleanly: {why}. Check ~/Library/Logs/factor-rotation/paper-close.log.")]


def stuck_closing(now: datetime, trading=None) -> list[tuple[str, str, str]]:
    """Alert if any trade sits in `closing` state with unexited shares past CLOSING_STUCK (excluding live today MOCs
    waiting for the closing auction)."""
    from . import journal
    from .paper import book, entries

    if not os.environ.get("FR_PAPER_ACCOUNT"):
        return []
    today = now.date()
    is_open = trading(today) if trading is not None else journal.is_trading_day(today)
    if not is_open:
        return []
    close_dt = datetime.combine(today, journal.close_time(today), NY)
    moc_today = entries._today_moc_exits(today) if now < close_dt + timedelta(minutes=20) else set()
    stuck = []
    for t in book.trades():
        if t["state"] != "closing" or t["id"] in moc_today:
            continue
        if not any(h["shares"] > h["exit_shares"] for h in t["held"]):
            continue
        ex = t.get("exit") or {}
        ex_session = ex.get("session")
        if ex_session and ex_session < today.isoformat():
            # Stuck from an earlier day: alert once market is open past 9:45 AM ET or after close
            if now.time() >= time(9, 45):
                stuck.append(t["id"])
        else:
            # Today's exit: alert if after close + 20m, or if an intraday exit has been closing for > CLOSING_STUCK
            if now >= close_dt + timedelta(minutes=20):
                stuck.append(t["id"])
            elif ex.get("intraday"):
                # Check latest entry run mentioning this exit or any run > CLOSING_STUCK old
                doc = entries.load(today) or {}
                first_at = next((datetime.fromisoformat(r["at"]) for r in doc.get("runs", [])
                                 if any(e.get("trade_id") == t["id"] for e in r.get("exits", []))), None)
                if first_at and now - first_at >= CLOSING_STUCK:
                    stuck.append(t["id"])
    if not stuck:
        return []
    return [(f"closing:stuck:{today}:{','.join(sorted(stuck))}", "Trades stuck closing",
             f"{len(stuck)} trade(s) still have unexited shares in closing state. Check paper-enter.log and fr paper report.")]


def _ct():
    from .config import CT
    return CT


def refused_quotes(now: datetime) -> list[tuple[str, str, str]]:
    """The order job's last two runs, both in the last 20 minutes, each left a trade unsent for want of a quote."""
    from .paper import entries
    state = entries.load(entries.entry_session(now)) or {}
    runs = [r for r in state.get("runs", []) if now - datetime.fromisoformat(r["at"]) <= timedelta(minutes=20)]
    if len(runs) < 2 or not all(any("no quote" in w for w in r.get("warnings", [])) for r in runs[-2:]):
        return []
    first = next(w for w in runs[-1]["warnings"] if "no quote" in w)
    why = "another session is using the market data (IBKR 10197)" if "10197" in first else "the gateway gave no quote"
    return [(f"quotes:{now.date()}", "Orders waiting: no quotes",
             f"The order job couldn't price a trade on its last two tries: {why}. Trades wait until it can.")]


async def _probe() -> str | None:
    """None if the paper gateway answers and is connected to IBKR, else what is wrong."""
    from .paper.broker import IBPaper, PaperSettings
    s = PaperSettings()
    s = type(s)(host=s.host, port=s.port, account=s.account, client_id=PROBE_CLIENT, send=False)
    try:
        async with IBPaper(s, readonly=True) as b:
            await asyncio.sleep(2)
            lost = [e for e in b.errors if e["code"] in LOST]
            if lost:
                return f"the paper gateway is running but has lost IBKR ({lost[-1]['code']})"
            await asyncio.wait_for(b.ib.accountSummaryAsync(s.account), 25)
    except asyncio.TimeoutError:
        return "the paper gateway connects but doesn't answer requests"
    except Exception as e:
        return f"the paper gateway can't be reached ({type(e).__name__})"
    return None


def gateway(now: datetime, state: dict, probe=None) -> list[tuple[str, str, str]]:
    """Two failed probes in a row, outside IBKR's nightly reset."""
    t = now.time()
    if t >= RESET[0] or t < RESET[1] or not os.environ.get("FR_PAPER_ACCOUNT"):
        state["gateway_fails"] = 0
        return []
    problem = probe() if probe else asyncio.run(_probe())
    state["gateway_fails"] = state.get("gateway_fails", 0) + 1 if problem else 0
    if problem and state["gateway_fails"] >= 2:
        return [(f"gateway:{now.date()}", "Paper gateway down",
                 f"{problem[0].upper()}{problem[1:]}, on two checks five minutes apart. `docker restart deploy-ib-gateway-paper-1` usually fixes it.")]
    return []


async def _quote_probe() -> str | None:
    """IBKR's refusal if the gateway is refused quotes because the live login is open somewhere else, else None.
    A gateway that can't be reached, or a quiet market with no bid and ask, is not this check's business."""
    from .paper.gateway import IBMarket
    try:
        async with IBMarket(client_id=QUOTE_CLIENT) as m:
            await asyncio.wait_for(m.quotes([QUOTE_SYMBOL], overnight=False, need_books=True), 30)
            why = m.missing.get(QUOTE_SYMBOL, "")
    except Exception:  # noqa: BLE001 - the gateway check reports a gateway that is down
        return None
    return why if LOGGED_IN_ELSEWHERE in why else None


def lost_quotes(now: datetime, state: dict, probe=None) -> list[tuple[str, str, str]]:
    """The gateway refused quotes on two checks in a row because the owner's live IBKR login is open elsewhere
    (the phone app, Client Portal, TWS). Checked whenever an order could go out (entries.venue), outside IBKR's
    nightly reset, and only while the gateway itself is up: then no order can go, stop exits included, until the
    other session logs out (the owner asked for this alert on 2026-11-18)."""
    from .paper.entries import venue
    t = now.time()
    if t >= RESET[0] or t < RESET[1] or not os.environ.get("FR_PAPER_ACCOUNT") or venue(now) is None or state.get("gateway_fails"):
        state["quote_fails"] = 0
        return []
    problem = probe() if probe else asyncio.run(_quote_probe())
    state["quote_fails"] = state.get("quote_fails", 0) + 1 if problem else 0
    if problem and state["quote_fails"] >= 2:
        return [(f"quotes-lost:{now.date()}", "Gateway has no quotes: log out of IBKR",
                 "The paper gateway is refused quotes (IBKR error 10197) because your live IBKR login is open somewhere "
                 "else: the phone app, Client Portal or TWS. Log out there and quotes come back within a minute. Until "
                 "then no order can go out, stop exits included.")]
    return []


# ---- the job --------------------------------------------------------------------------------------------------

def run(now: datetime | None = None, probe=None, quiet: bool = False, quote_probe=None,
        check_gateway: bool = False) -> list[str]:
    now = now or datetime.now(NY)
    state = _state()
    # Gateway connectivity and 10197 quote checks moved to scheduled pre-flight checks before each pass and close
    # (paper/gateway.py:preflight) so the watchdog isn't polling the gateway every 5 minutes while the owner trades
    # on their phone. Pass check_gateway=True (or probe/quote_probe) to run them here explicitly.
    found = [*failed_jobs(), *stopped_clock(now), *missing_passes(now), *missing_close(now), *stuck_closing(now), *refused_quotes(now)]
    if check_gateway or probe is not None:
        found += gateway(now, state, probe)
    if (check_gateway or quote_probe is not None) and not any(k.startswith("quotes:") for k, _, _ in found):
        found += lost_quotes(now, state, quote_probe)
    sent_at = state.setdefault("sent", {})
    out = []
    for key, title, text in found:
        last = sent_at.get(key)
        wait = timedelta(days=1) if key.startswith("job:") else REPEAT   # a job's status stands until its next run
        if last and now - datetime.fromisoformat(last) < wait:
            continue
        # A push that didn't go out (no network, as when the close job itself fails) is tried again on the next run.
        if send(title, text) is not False or not _phone_set():
            sent_at[key] = now.isoformat(timespec="seconds")
        out.append(f"{title}: {text}")
    # forget alerts older than a day, so the file stays small
    state["sent"] = {k: v for k, v in sent_at.items() if now - datetime.fromisoformat(v) < timedelta(days=2)}
    state["checked_at"] = now.isoformat(timespec="seconds")
    _save(state)
    if not quiet:
        for line in out:
            print(line)
    return out


def test() -> None:
    ok = send("Assay test", "Phone alerts are working. You'll hear here when a pass, a job or the gateway breaks.")
    print("sent" if ok else "not sent: set FR_NTFY_TOPIC, or FR_TELEGRAM_TOKEN and FR_TELEGRAM_CHAT, in .env")

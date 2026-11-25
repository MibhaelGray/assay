"""The watchdog: `fr watch`, every five minutes. It sends a phone alert when something the book depends on breaks.

The owner asked on 2026-11-17 to hear about failures at once. That morning the paper gateway lost IBKR and orders
sat for an hour before anyone looked. Alerts go to the ntfy app (https://ntfy.sh) on the topic in FR_NTFY_TOPIC, to
Telegram through the owner's bot (FR_TELEGRAM_TOKEN, FR_TELEGRAM_CHAT), and as a Mac notification. The topic is a long random name, which is all that keeps it private, so an alert never carries
an account number, a position or a dollar amount: only what broke and where to look.

What it checks:
- a scheduled job that exited with an error (launchd keeps each job's last exit status);
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

from .config import NY

STATE = Path("data/watch.json")
REPEAT = timedelta(hours=3)
PASS_LATE = timedelta(minutes=75)
PASS_LOGS = Path("data/pass-logs")
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


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1, sort_keys=True))


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
        if status not in ("0", "-") and job != "watch":
            found.append((f"job:{job}:{status}", f"Job failed: {job}",
                          f"The {job} job's last run exited with status {status}. Its log is ~/Library/Logs/factor-rotation/{job}.log."))
    return found


def _pass_days(kind: str, day: date, trading) -> bool:
    if kind == "sun-thu":
        return day.weekday() in (6, 0, 1, 2, 3)
    if kind == "full":
        from .journal import is_half_day
        return trading(day) and not is_half_day(day)
    return trading(day)


def missing_passes(now: datetime, trading=None) -> list[tuple[str, str, str]]:
    """A scheduled pass that hasn't recorded PASS_LATE after its start, or whose log says it failed."""
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
            if not (begin + PASS_LATE <= now < begin + timedelta(hours=12)) or not _pass_days(days, day, trading):
                continue
            mine = [d for p, at, d in logs if p == kind and begin - timedelta(minutes=10) <= at <= begin + timedelta(hours=3)]
            when = begin.astimezone(_ct()).strftime("%-I:%M %p CT")
            if not mine:
                found.append((f"pass:{kind}:{day}", f"No {kind} pass",
                              f"The {kind} pass due at {when} has no log 75 minutes later. Check ~/Library/Logs/factor-rotation/pass-{kind}.log."))
            elif not any(d.get("ok") for d in mine):
                found.append((f"pass:{kind}:{day}", f"The {kind} pass failed",
                              f"The {kind} pass that started at {when} did not record. Its log is in data/pass-logs."))
    return found


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

def run(now: datetime | None = None, probe=None, quiet: bool = False, quote_probe=None) -> list[str]:
    now = now or datetime.now(NY)
    state = _state()
    found = [*failed_jobs(), *missing_passes(now), *refused_quotes(now), *gateway(now, state, probe)]
    if not any(k.startswith("quotes:") for k, _, _ in found):   # the order job's own alert already says it
        found += lost_quotes(now, state, quote_probe)
    sent_at = state.setdefault("sent", {})
    out = []
    for key, title, text in found:
        last = sent_at.get(key)
        wait = timedelta(days=1) if key.startswith("job:") else REPEAT   # a job's status stands until its next run
        if last and now - datetime.fromisoformat(last) < wait:
            continue
        send(title, text)
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

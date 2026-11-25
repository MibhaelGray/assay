"""The paper gateway's bid and ask: the only prices an order may be sent on.

No order goes out without the gateway's own quote for it, in any session (the owner's rule; paper/entries.py).
`gateway_books` asks for every symbol at once and says, for each one it couldn't get, why: the gateway's own
refusal in plain words (QUOTE_REFUSED), or what it sent instead. The connection is read-only and has its own
client id, so it never collides with the order connection (paper/broker.py).
"""

import asyncio
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta

import numpy as np

from ..config import CT, NY
from .broker import IBPaper, PaperSettings

log = logging.getLogger(__name__)

DATA_CLIENT_ID = 77         # the gateway quotes' own connection
PREFLIGHT_CLIENT_ID = 76    # pre-flight health check's own connection
QUOTE_TRIES = 3             # asks of the gateway for a bid and ask before a leg is left to wait
SNAPSHOT_BATCH = 50
GATEWAY_LOST_CODES = {1100, 2110}
RESTART_WAIT = 15           # seconds after docker restart for IBC to log back in (~5 s in practice)


class IBMarket:
    """Snapshot quotes from the paper gateway, read-only, on its own client id."""

    def __init__(self, client_id: int | None = None):
        s = PaperSettings()
        self.host, self.port = s.host, s.port
        self.client_id = client_id or DATA_CLIENT_ID
        self.ib = None

    async def __aenter__(self):
        from ib_async import IB
        self.ib = IB()
        self.errors: dict[int, tuple[int, str]] = {}   # conId -> the gateway's last refusal for it
        self.ib.errorEvent += self._on_error
        await self.ib.connectAsync(self.host, self.port, clientId=self.client_id, readonly=True, timeout=15)
        self.ib.reqMarketDataType(1)
        return self

    def _on_error(self, req_id, code, message, contract):
        """Keeps what the gateway said about a contract, so a missing quote comes with its reason."""
        if contract is not None and getattr(contract, "conId", 0) and code not in QUOTE_NOISE:
            self.errors[contract.conId] = (code, message)

    async def __aexit__(self, *exc):
        if self.ib:
            self.ib.disconnect()

    async def _contracts(self, symbols: list[str], overnight: bool) -> dict:
        from ib_async import Contract, Stock
        out = {}
        for i in range(0, len(symbols), SNAPSHOT_BATCH):
            chunk = [Stock(s.replace(".", " "), "SMART", "USD") for s in symbols[i:i + SNAPSHOT_BATCH]]
            for c in await asyncio.wait_for(self.ib.qualifyContractsAsync(*chunk), 60):
                if c and getattr(c, "conId", 0):
                    out[c.symbol.replace(" ", ".")] = Contract(conId=c.conId, exchange="OVERNIGHT") if overnight else c
        return out

    async def quotes(self, symbols: list[str], overnight: bool, tries: int = 1, need_books: bool = False) -> dict[str, float]:
        """symbol -> latest price. Also sets `books` (symbol -> bid, ask) and `missing` (symbol -> why it has no
        bid and ask). `need_books` waits for the bid and ask, not just a price; `tries` asks again for the names
        still without a book, unless the gateway refused them for a reason that asking again won't change."""
        contracts = await self._contracts(symbols, overnight)
        out = {}
        self.books: dict[str, tuple[float, float]] = {}
        self.missing: dict[str, str] = {s: "IBKR has no contract for it" for s in symbols if s not in contracts}
        why = {}
        todo = list(contracts.items())
        for attempt in range(tries):
            if attempt:
                await asyncio.sleep(QUOTE_RETRY_WAIT)
            for i in range(0, len(todo), SNAPSHOT_BATCH):
                batch = todo[i:i + SNAPSHOT_BATCH]
                for _, c in batch:
                    self.errors.pop(c.conId, None)
                tickers = [(s, self.ib.reqMktData(c, "", False, False)) for s, c in batch]
                for _ in range(QUOTE_WAITS):
                    await asyncio.sleep(0.5)
                    if all((_book(t) if need_books else _price(t)) or t.contract.conId in self.errors for _, t in tickers):
                        break
                out.update({s: p for s, t in tickers if (p := _price(t))})
                self.books.update({s: b for s, t in tickers if (b := _book(t))})
                why.update({s: _why_no_book(t, self.errors.get(t.contract.conId)) for s, t in tickers if not _book(t)})
                for _, tk in tickers:
                    self.ib.cancelMktData(tk.contract)
            todo = [(s, c) for s, c in todo if s not in self.books and self.errors.get(c.conId, (0, ""))[0] not in QUOTE_REFUSED]
            if not todo:
                break
        self.missing.update({s: w for s, w in why.items() if s not in self.books})
        self._contracts_cache = contracts
        return out


    async def sweep(self, symbols: list[str], wait: float = 4.0) -> dict[str, dict]:
        """symbol -> its overnight bid, ask, last trade and volume so far, for every name IBKR has a contract for.
        One look per batch, no retries: the night screen's overnight sweep (screen.py)."""
        contracts = await self._contracts(symbols, overnight=True)
        out = {}
        items = list(contracts.items())
        for i in range(0, len(items), SNAPSHOT_BATCH):
            tickers = [(s, self.ib.reqMktData(c, "", False, False)) for s, c in items[i:i + SNAPSHOT_BATCH]]
            await asyncio.sleep(wait)
            for s, t in tickers:
                out[s] = {k: (float(v) if v is not None and np.isfinite(v) and v > 0 else None)
                          for k, v in (("bid", t.bid), ("ask", t.ask), ("last", t.last), ("volume", t.volume))}
                self.ib.cancelMktData(t.contract)
        return out


QUOTE_WAITS = 16            # of 0.5 s: how long one request waits for its quote
QUOTE_RETRY_WAIT = 5        # seconds before asking again for the names still without a bid and ask
QUOTE_NOISE = {2104, 2106, 2107, 2108, 2119, 2158, 300, 10167}   # farm status lines, notices, the cancel of a refused request
# The gateway's refusals that asking again won't change, in plain words.
QUOTE_REFUSED = {
    10197: "the live IBKR login is in use somewhere else, and IBKR gives its quotes to one session at a time",
    10168: "the account has no market data subscription for it over the API",
    10089: "the account has no market data subscription for it over the API",
    10090: "the account has no market data subscription for it over the API",
    10091: "the account has no market data subscription for it over the API",
    354: "the account has no market data subscription for it over the API",
    200: "IBKR has no such contract on this venue",
}


def _book(t) -> tuple[float, float] | None:
    """The ticker's bid and ask when both are there and the bid isn't over the ask."""
    bid, ask = t.bid, t.ask
    if bid is None or ask is None or not (np.isfinite(bid) and np.isfinite(ask)):
        return None
    return (float(bid), float(ask)) if bid > 0 and ask >= bid else None


def _why_no_book(t, error: tuple[int, str] | None) -> str:
    """Why a name has no bid and ask, in a sentence fragment that follows "because"."""
    if error:
        code, message = error
        return f"{QUOTE_REFUSED.get(code) or message.strip()} (IBKR error {code})"
    have = lambda v: v is not None and np.isfinite(v) and v > 0   # noqa: E731
    bid, ask = have(t.bid), have(t.ask)
    if bid and ask:
        return f"its bid ({t.bid:g}) is above its ask ({t.ask:g})"
    if bid or ask:
        return f"the gateway sent {'a bid and no ask' if bid else 'an ask and no bid'}"
    return f"the gateway sent no bid or ask within {QUOTE_WAITS * 0.5:g} seconds and gave no reason"


CANARY = "SPY"   # quoted beside every overnight request: with SPY quoted, the gateway is working, so a name it
                 # sends nothing for has no overnight market


def no_market(reason: str) -> bool:
    """Whether a missing quote's reason means the market has no bid and ask for it, not that the gateway failed:
    nothing or one side sent with no refusal, or no contract on the venue. Only to be trusted when CANARY was quoted."""
    return reason.startswith("the gateway sent") or "(IBKR error 200)" in reason


def _price(t) -> float | None:
    for v in (t.last, (t.bid + t.ask) / 2 if t.bid and t.ask and t.bid > 0 and t.ask > 0 else None):
        if v is not None and np.isfinite(v) and v > 0:
            return float(v)
    return None


async def gateway_books(symbols: list[str], overnight: bool) -> tuple[dict[str, tuple[float, float]], dict[str, str]]:
    """(symbol -> bid, ask; symbol -> why it has none) from the paper gateway in the session that is trading."""
    try:
        async with IBMarket() as m:
            await m.quotes(symbols, overnight, tries=QUOTE_TRIES, need_books=True)
            return m.books, m.missing
    except Exception as e:  # noqa: BLE001 - no quotes means no orders at this run, not a failed run
        log.warning("gateway_quotes_failed %r", e)
        why = f"the gateway couldn't be asked for quotes ({type(e).__name__}{': ' + str(e) if str(e) else ''})"
        return {}, {s: why for s in symbols}


def _find_docker() -> str | None:
    return shutil.which("docker") or next(
        (p for p in ("/usr/local/bin/docker", "/opt/homebrew/bin/docker",
                     os.path.expanduser("~/.docker/bin/docker"),
                     "/Applications/Docker.app/Contents/Resources/bin/docker") if os.path.exists(p)),
        None,
    )


def restart_container(cause: str, now: datetime | None = None,
                      cooldown: timedelta = timedelta(minutes=15)) -> tuple[bool, str | None]:
    """Restart the paper IB Gateway Docker container with a cooldown so nothing reboot-loops.
    Returns (restarted_ok, message_or_None_if_in_cooldown)."""
    from . import book

    now = now or datetime.now(NY)
    stamp = book.ROOT / "gateway_restart.json"
    last = (book._read(stamp) or {}).get("at")
    if last and cooldown.total_seconds() > 0 and now - datetime.fromisoformat(last) < cooldown:
        return False, None
    docker = _find_docker()
    name = os.environ.get("FR_PAPER_GATEWAY_CONTAINER", "deploy-ib-gateway-paper-1")
    if not docker:
        return False, f"{cause}, and docker wasn't found to restart it"
    book._write(stamp, {"at": now.isoformat(timespec="seconds"), "why": cause})
    done = subprocess.run([docker, "restart", name], capture_output=True, text=True, timeout=120)
    if done.returncode == 0:
        return True, f"the paper gateway was restarted ({cause})"
    return False, f"tried to restart the paper gateway ({cause}), but docker said: {done.stderr.strip()[:200]}"


async def _probe_health(check_quotes: bool = True) -> tuple[str, str | None]:
    """Check gateway connectivity and (optionally) whether quotes are blocked by a competing live login (10197).
    Returns (kind, detail) where kind is 'ok', 'down', or '10197'."""
    s = PaperSettings()
    s = type(s)(host=s.host, port=s.port, account=s.account, client_id=PREFLIGHT_CLIENT_ID, send=False)
    try:
        async with IBPaper(s, readonly=True) as b:
            await asyncio.sleep(1.5)
            lost = [e for e in b.errors if e["code"] in GATEWAY_LOST_CODES]
            if lost:
                return "down", f"gateway lost IBKR ({lost[-1]['code']})"
            await asyncio.wait_for(b.ib.accountSummaryAsync(s.account), 20)
    except asyncio.TimeoutError:
        return "down", "gateway connects but timed out on account summary"
    except Exception as e:  # noqa: BLE001
        return "down", f"gateway unreachable ({type(e).__name__})"
    if not check_quotes:
        return "ok", None
    from .entries import venue
    overnight = venue(datetime.now(NY)) == "overnight"
    try:
        async with IBMarket(client_id=PREFLIGHT_CLIENT_ID) as m:
            await asyncio.wait_for(m.quotes([CANARY], overnight=overnight, need_books=True), 25)
            why = m.missing.get(CANARY, "")
            if "10197" in why:
                return "10197", why
    except Exception as e:  # noqa: BLE001
        return "down", f"quote probe failed ({type(e).__name__})"
    return "ok", None


def preflight(label: str, *, check_quotes: bool = True, notify: bool = True, now: datetime | None = None,
              probe=None, restarter=None, sleeper=None, sender=None) -> dict:
    """Deterministic pre-flight check before a scheduled pass or close job.
    Auto-restarts Docker silently if the gateway is down/frozen or stuck on stale 10197; notifies Telegram
    only if it is still unhealthy or if a live phone/TWS login (10197) is actively holding market data."""
    from .. import watch

    now = now or datetime.now(NY)
    probe_fn = probe or (lambda: asyncio.run(_probe_health(check_quotes=check_quotes)))
    restart_fn = restarter or (lambda why: restart_container(why, now=now, cooldown=timedelta(minutes=5)))
    sleep_fn = sleeper or time.sleep
    send_fn = sender or watch.send

    kind, detail = probe_fn()
    if kind == "ok":
        return {"ok": True, "status": "ok", "restarted": False, "label": label, "at": now.isoformat(timespec="seconds")}

    restarted, msg = restart_fn(f"pre-flight before {label}: {detail or kind}")
    if restarted:
        sleep_fn(RESTART_WAIT)
        kind, detail = probe_fn()
        if kind == "ok":
            return {"ok": True, "status": "recovered", "restarted": True, "note": msg,
                    "label": label, "at": now.isoformat(timespec="seconds")}

    if notify:
        when = now.astimezone(CT).strftime("%-I:%M %p CT")
        if kind == "10197":
            send_fn(
                f"{label}: close IBKR on phone",
                f"{label} is coming up (checked {when}), and IBKR is refusing quotes (error 10197) because your live "
                "login is open on your phone, browser or TWS. Log out there so paper quotes work.",
            )
        else:
            send_fn(
                f"{label}: paper gateway down",
                f"Checked before {label} ({when}) and tried restarting the paper gateway, but it still isn't healthy: "
                f"{detail or msg or kind}.",
            )
    return {"ok": False, "status": kind, "restarted": restarted, "detail": detail or msg,
            "label": label, "at": now.isoformat(timespec="seconds")}

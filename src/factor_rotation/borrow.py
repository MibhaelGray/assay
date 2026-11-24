"""What it costs to borrow a US stock to short it, from IBKR's public stock-loan file.

IBKR publishes one file for US names (about 20,000 rows, refreshed every 15 minutes or so) on an FTP server that
needs no account: the yearly fee a short seller pays, the rebate, and how many shares are there to borrow. No
gateway and no login, so it works whether or not the gateway is up.

    fetch() -> {"as_of": "2026.11.23 19:20:44", "host": ..., "rows": {"INTU": {"fee": 0.2614, "rebate": 3.6186,
                                                                             "shares": 4200000}, ...}}

`fee` and `rebate` are percent a year. `shares` is the count IBKR shows as available; it stops counting at
10,000,000 (">10000000" in the file), which is stored as 10,000,000 with "capped": True. Symbols are IBKR's, the
project's own form ("BRK B"). `as_of` is the file's own stamp, kept as written.

Recorded beside the verdicts as side facts (paper/side.py) since 2026-11-23. No research pass reads it.
"""

import io
import logging
from ftplib import FTP

log = logging.getLogger(__name__)

HOSTS = ("ftp2.interactivebrokers.com", "ftp3.interactivebrokers.com")   # the same file; one is tried after the other
USER = "shortstock"                                                       # IBKR's public login for this file, no password
FILE = "usa.txt"
CAP = 10_000_000


def parse(text: str) -> dict:
    """The file's rows by symbol. Lines are SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|, with a
    #BOF line carrying the date and time, a #SYM header and a #EOF line carrying the row count."""
    as_of, rows, count = None, {}, None
    for line in text.splitlines():
        cells = line.split("|")
        if line.startswith("#BOF"):
            as_of = " ".join(cells[1:3]).strip() or None
        elif line.startswith("#EOF"):
            count = int(cells[1]) if len(cells) > 1 and cells[1].strip().isdigit() else None
        elif line.startswith("#") or len(cells) < 8 or cells[1] != "USD":
            continue
        else:
            try:
                fee, rebate = float(cells[6]), float(cells[5])
            except ValueError:
                continue          # "NA": IBKR has no rate for the name right now
            avail = cells[7].strip()
            capped = avail.startswith(">")
            try:
                shares = int(float(avail.lstrip(">")))
            except ValueError:
                continue
            rows[cells[0].strip()] = {"fee": fee, "rebate": rebate, "shares": min(shares, CAP) if capped else shares,
                                      **({"capped": True} if capped else {})}
    return {"as_of": as_of, "rows": rows, "complete": count is not None}


def fetch(timeout: float = 45) -> dict:
    """Download and parse the file. Raises if no host answers or the file arrives cut short (no #EOF line)."""
    errors = []
    for host in HOSTS:
        try:
            buf = io.BytesIO()
            with FTP(host, timeout=timeout) as ftp:
                ftp.login(USER, "")
                ftp.retrbinary(f"RETR {FILE}", buf.write)
            out = parse(buf.getvalue().decode("latin-1"))
            if not out["complete"] or not out["rows"]:
                raise ValueError("the file arrived cut short")
            log.info("borrow_file host=%s rows=%d as_of=%s", host, len(out["rows"]), out["as_of"])
            return {"as_of": out["as_of"], "host": host, "rows": out["rows"]}
        except Exception as e:  # noqa: BLE001 - try the other host, then say what each said
            errors.append(f"{host}: {type(e).__name__}: {e}"[:160])
    raise RuntimeError("IBKR's stock-loan file could not be read (" + "; ".join(errors) + ")")

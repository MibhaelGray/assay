"""Start the paper book again from zero: `fr paper restart`.

The owner's decision, 2026-11-15: the swing book moves to three passes a day on live prices, the intraday book
is shut down, every position is closed, and the record starts fresh with exactly $1,000,000 in the swing book.
The first book's files aren't deleted. They move, whole, to journal/archive/first-book-<date>/ (paper/ and
intraday/), where nothing reads them, and a new journal/paper/ starts with:

    book.json    {"started", "design": "three-pass", "capital": {"swing": 1000000.0}, "archive"}
    lessons.md   the first book's standing lessons, carried over under a heading that says where they came from

book.json is what switches the rest of the code over: one book, its capital, the 10% pause measured on it
($100,000), a gross limit of the same size, and scoring from each pass's own prices (portfolio.restart()).

It refuses while either book still holds anything or has an order working, so the order is: close everything
(`fr paper close --flatten --execute` at the close, after the intraday book's last trade is out), let the
4:20 PM ET sync pull the fills, then restart. Run by hand it only says what it would do; `--execute` does it.
"""

import json
import shutil
from datetime import datetime

from .. import journal
from ..config import NY
from . import book

CAPITAL = 1_000_000.0
DESIGN = "three-pass"


def blockers() -> list[str]:
    """Why the book can't be restarted yet: anything it still holds or is still trading."""
    out = [f"swing trade {t['id']} {t['name']} is {t['state']}" for t in book.trades() if t["state"] in ("planned", "open", "closing")]
    fills = sorted((book.ROOT / "fills").glob("*.json"))
    last = json.loads(fills[-1].read_text()) if fills else {}
    out += [f"the account held {v:+g} {s} at the last sync ({last.get('pulled_at', 'unknown')[:16]})" for s, v in sorted((last.get("positions") or {}).items()) if v]
    return out


def plan(now: datetime | None = None) -> dict:
    now = now or datetime.now(NY)
    archive = journal.JOURNAL / "archive" / f"first-book-{now.date()}"
    moves = [(journal.JOURNAL / name, archive / name) for name in ("paper", "intraday") if (journal.JOURNAL / name).exists()]
    return {"at": now.isoformat(timespec="seconds"), "archive": archive, "moves": moves, "blockers": blockers(),
            "already": json.loads((book.ROOT / "book.json").read_text()) if (book.ROOT / "book.json").exists() else None}


def run(execute: bool = False, now: datetime | None = None, carry: bool = False) -> dict:
    """Archive the first book and start the new one. Returns the plan, with `done` True when it was carried out.

    `carry` restarts although the first book still holds positions (the owner asked for the switch on a Sunday
    night, 2026-11-15, when they couldn't be closed). They stay in the account outside the new book, written into
    book.json as "legacy", and are closed from the archive: FR_JOURNAL_DIR=<archive> fr paper close --flatten --execute."""
    p = plan(now)
    p["done"] = False
    fills = sorted((book.ROOT / "fills").glob("*.json"))
    last = json.loads(fills[-1].read_text()) if fills else {}
    p["legacy"] = {"positions": {s: v for s, v in sorted((last.get("positions") or {}).items()) if v},
                   "as_of": last.get("pulled_at"), "trades": [t["id"] for t in book.trades() if t["state"] in ("planned", "open", "closing")],
                   "close": f"FR_JOURNAL_DIR={p['archive']} uv run fr paper close --flatten --execute, at a close"} if carry and p["blockers"] else None
    if p["already"] or (p["blockers"] and not carry) or not execute:
        return p
    if p["archive"].exists():
        raise FileExistsError(f"{p['archive']} already exists")
    lessons = book.ROOT / "lessons.md"
    carried = lessons.read_text().strip() if lessons.exists() else ""
    p["archive"].mkdir(parents=True)
    for src, dst in p["moves"]:
        shutil.move(str(src), str(dst))
    book.ROOT.mkdir(parents=True)
    book._write(book.ROOT / "book.json", {"started": p["at"], "design": DESIGN, "capital": {"swing": CAPITAL},
                                          "archive": str(p["archive"]), **({"legacy": p["legacy"]} if p["legacy"] else {})})
    if carried:
        (book.ROOT / "lessons.md").write_text(f"From the first book (to {p['at'][:10]}, one pass a night on closing prices; its record is in "
                                              f"{p['archive']}). Keep, change or drop each as the new record shows:\n\n{carried}\n")
    p["done"] = True
    return p

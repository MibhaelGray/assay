"""The small-cap book: thinly covered US companies worth $250M to $1B (docs/small-cap-book.md).

A shadow run since 2026-11-19: one pass a day after the close screens the universe, researches each candidate
and records a verdict, a direction, a conviction and a rank on every one. No order is placed. `fr smallcap score`
sets the verdicts against what the stocks went on to do, which decides whether the trading side gets built.

    universe.py   who is in: size, price, volume and industry rules, rebuilt weekly
    sources.py    the free feeds: Nasdaq's listing and earnings calendar, OpenInsider's purchases
    screen.py     the nightly candidates: earnings reactions, news moves, insider buying
    record.py     checks the pass's draft and keeps the verdicts
    score.py      every verdict scored at holds of one to five sessions, against IWM

Everything is kept under journal/smallcap/. Nothing here touches the paper account or the swing book's files.
"""

from .. import journal

ROOT = journal.JOURNAL / "smallcap"
HEDGE = "IWM"   # the small-cap ETF: the book's one hedge, and what every verdict is scored against

"""Keep the tests off the real journal's book.

journal/paper/book.json switches the code to the restarted, three-pass book (portfolio.restart()). Tests that
don't build their own journal assume the first book's defaults, so the repo's own book.json is hidden from every
test; a test that writes a book.json into its own temporary journal still sees it.
"""

from pathlib import Path

import pytest

from factor_rotation import portfolio

REAL = (Path(__file__).resolve().parent.parent / "journal" / "paper" / "book.json")


@pytest.fixture(autouse=True)
def _no_real_book(monkeypatch):
    real = portfolio.restart

    def restart():
        return None if (portfolio.book.ROOT / "book.json").resolve() == REAL else real()

    monkeypatch.setattr(portfolio, "restart", restart)

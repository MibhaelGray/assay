"""The research lab: test many trading ideas on history, quickly, and keep an honest count (docs/research-lab.md).

Started 2026-11-22 at the owner's request: a way to run a lot of research directions for trades held one to five
sessions, in the US or any market IBKR opens to a US resident, until something real turns up. It is separate from
the paper books: it has its own price history under data/lab/, places no orders and reads nothing from the journal's
trading record.

How it fits together:
  panel.py     the price history for one market (a wide table per field) and who was tradable on each day
  feat.py      building blocks ideas share (returns, ranks, residuals, volatility)
  engine.py    turns an idea's daily scores into a portfolio held 1-5 sessions and its daily return after costs
  stats.py     what the return series is worth: Sharpe, t, drawdown, by year, and the haircut for how many ideas were tried
  ideas/       one small file per idea: why it might work, its few variants, and a score() function
  registry.py  the ledger: every variant ever run is one line in journal/lab/trials.jsonl, failures included
  run.py       runs an idea on the discovery and validation years, records it and gives a verdict against the bar

The years are split three ways and the split is fixed (PERIODS). Ideas are found on `discovery`, confirmed on
`validation` with nothing changed, and only then get one look at `holdout`, which every other command leaves out.
"""

from pathlib import Path

from ..config import settings

DATA = Path(settings().data_root) / "lab"          # prices and caches: big, rebuilt from the web, not in git
ROOT = Path("journal") / "lab"                      # the ledger, results and notes: small, kept in git
START = "2010-01-01"
PERIODS = {"discovery": ("2010-01-01", "2021-12-31"), "validation": ("2022-01-01", "2024-12-31"),
           "holdout": ("2025-01-01", "2100-01-01")}
HOLDOUT_START = PERIODS["holdout"][0]
# The bar, fixed before the first idea was run. `candidate` = passes discovery and validation; only a candidate may
# spend its one holdout look.
BAR = {"discovery": {"sharpe": 1.0, "t": 3.0, "sharpe_2x_cost": 0.5, "good_years": 0.7},
       "validation": {"sharpe": 0.7},
       "holdout": {"sharpe": 0.5},
       "deflated": 0.95}
MAX_VARIANTS = 12   # per idea: enough to check the neighbours of a setting, too few to fish with

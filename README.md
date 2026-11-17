# ETF Factor Rotation

A research and paper-trading project. It collects daily bars from Yahoo Finance and option chains from IBKR for the largest ETFs and stocks, turns them into positioning, volatility and technical signals, and screens about 1,060 names for trade setups. Three times a day a Claude research pass judges the screen and ranks every candidate. A paper book trades only its best one to three ideas, and every verdict and its reasoning is kept and scored.

What the project is for, and the rules it is built by: [PROJECT.md](PROJECT.md). Read that first.

## How it runs

Everything runs on one Mac through launchd (times Central, trading days):

| When | Job | What it does |
|---|---|---|
| 9:00 AM, 2:30 PM; ETFs hourly 10 AM–1 PM | `fr options` | option chains from IBKR into `data/` |
| 3:30 PM | `fr bars` | daily bars from Yahoo |
| 6:00 PM | nightly | brief, earnings dates, scan and screen; commits and pushes `journal/` |
| 7:30 PM (Sun–Thu) | night pass | judges the after-hours screen for the next session |
| 8:45 AM | morning pass | judges the pre-market and first minutes of trading |
| 12:00 PM | midday pass | judges the morning's trading, reviews open trades |
| every minute | `fr paper enter` | sends a recorded pass's takes and exits to the paper account |
| 2:45 PM | `fr paper close` | stops and time limits, into the closing auction |
| 3:20 PM | `fr paper sync` | fills, positions and account value |

Each pass runs Claude Code headless (`fr pass <name>`). The judge is Fable 5.2 at high effort, and its research agents are Opus 5.5 at medium effort. Each pass screens on live prices (`fr screen --live`), researches the candidates on the web, gives every one a verdict, a conviction (1–5) and a rank, and records them with `fr paper propose`. The pass never sends an order itself.

## The swing book

One paper book of $1,000,000 on an IBKR paper account, live since the night of 2026-11-15:

- A pass takes its best one to three ideas at conviction 3 or more, $10,000 a side, each with its own stop (1–8%) and time limit (1–10 sessions). The rules are in the `swing-pass` skill and `paper/rules.py`.
- Every order needs the paper gateway's own bid and ask. A leg whose spread is over 2% isn't crossed. Entries can go in after hours, overnight and pre-market; exits go in regular hours.
- A book down 10% of its capital from its high pauses itself until `fr paper resume`. `fr paper halt` stops new entries by hand.

## Safety

- **Paper only.** `paper/broker.py` is the only code that sends orders. It refuses anything but a paper port and a paper (DU) account.
- **By-hand runs are dry runs.** A job run by hand prints what it would send unless given `--execute`. Set `FR_PAPER_SEND=0` to be sure.
- **Passes can't trade.** A pass runs with a fixed allowlist of commands. It records verdicts; only the order job trades them.

## Quick start

```bash
uv sync
uv run fr bars                # backfill five years of daily bars (no IBKR needed)
uv run fr serve               # dashboard at http://localhost:8765, with a Refresh button
uv run fr dashboard           # or a static copy built from the journal
uv run fr paper status        # the account against the book (read-only)
uv run fr baskets check       # is every themed basket a real basket?
uv run python -m unittest discover -s tests
```

Setting up the collector (IBKR login, gateway, launchd): [docs/operations.md](docs/operations.md#collector-setup-macos).

## Dashboard

`fr serve` serves `dashboard/page.html`, which has these pages:

- **Today**
- **Portfolio:** positions, P/L and closed trades
- **Swing research:** every verdict and its reasoning
- **Market:** regime, factor board, dealer gamma and forced flows
- **Baskets:** a table or a treemap heat map
- **Earnings**
- **Hormuz**
- **Chart**

The same page is published to claude.ai as the [Factor Rotation Ledger](https://claude.ai/artifact/TVknCZmSXjToEPbmMGPSKv).

## Layout

| Path | Holds |
|---|---|
| `src/factor_rotation/` | the `fr` command and everything behind it |
| `journal/` | what the project decides, in git: briefs, screens, research notes, the paper book (`journal/paper/`), reference lists such as `baskets.json`, and the first book's archive |
| `data/` | market data on the collector's disk, not in git: bars, option chains, pass logs |
| `dashboard/` | the dashboard page and its local wrapper |
| `deploy/` | launchd and systemd schedules, `run.sh`, the gateway's docker-compose file, the pass prompts |
| `.claude/skills/swing-pass/` | what a research pass is told |

## More detail

- [docs/operations.md](docs/operations.md): data sources, every job, the headless passes, collector setup, moving and backup
- [docs/signals.md](docs/signals.md): options data, forced flows, the screen and universe, themed baskets, the dashboard, the Portfolio view
- [docs/paper-trading.md](docs/paper-trading.md): order rules and guards, and the first book (2026-11-09 to 11-15)
- [docs/intraday-book.md](docs/intraday-book.md): the intraday book, archived at the restart

# ETF Factor Rotation

A research and paper-trading project with one book, the swing book. It collects daily bars from Yahoo Finance for the largest ETFs and stocks, turns them into technical and forced-flow signals, and screens about 1,240 names for trade setups. Three times a day a Claude research pass judges the screen and ranks every candidate. A paper book trades only the ideas it has conviction in, and every verdict and its reasoning is kept and scored.

What the project is for, and the rules it is built by: [PROJECT.md](PROJECT.md). Read that first.

## How it runs

Everything runs on one Mac through launchd (times Central, trading days):

| When | Job | What it does |
|---|---|---|
| 3:30 PM | `fr bars` | daily bars from Yahoo |
| 6:00 PM | nightly | brief, earnings dates, scan and screen; commits and pushes `journal/` |
| 7:30 PM (Sun–Thu) | night pass | judges the after-hours screen for the next session |
| 8:45 AM | morning pass | judges the pre-market and first minutes of trading |
| 12:00 PM | midday pass | judges the morning's trading, reviews open trades |
| every minute | `fr paper enter` | sends a recorded pass's takes and exits to the paper account |
| every 10 min, 8:40 AM-2:40 PM | `fr paper enter` | stops: a trade past its stop exits at once, hitting the bid or ask |
| 2:45 PM | `fr paper close` | time limits, and any stop exit not sent during the day, into the closing auction |
| 3:20 PM | `fr paper sync` | fills, positions and account value |
| every 5 minutes | `fr watch` | phone alert (ntfy app and Telegram) when a job fails, a pass doesn't record, quotes are refused (including by an IBKR login open on your phone) or the gateway is down |
| every 15 minutes | `fr publish --pull` | refreshes the public copy of the dashboard |

Each pass runs Claude Code headless (`fr pass <name>`). The judge is Fable 5.2 at high effort, and its research agents are Sonnet 5.5 at medium effort, one to a name, with about 2,500 web searches a pass between them. Each pass screens on live prices (`fr screen --live`), researches the candidates on the web, gives every one a verdict, a conviction (1–5) and a rank, and records them with `fr paper propose`. The pass never sends an order itself.

## The swing book

One paper book of $1,000,000 on an IBKR paper account, live since the night of 2026-11-15:

- A pass judges up to 25 candidates (no more than 6 of one kind of setup) and takes every idea at conviction 3 or more, each with its own stop (1–8%) and time limit (1–10 sessions).
- Each trade is sized by its risk: it loses about 0.15% of the book if stopped at conviction 3, up to twice that at conviction 5. A typical trade is 3% of the book a side; none is over 6%. The rules are in the `swing-pass` skill and `paper/rules.py`.
- A trade's hardest-to-fill leg is ordered first; its other legs only follow what that leg has filled, so a stuck order never leaves a hedge on its own.
- Every order needs the paper gateway's own bid and ask. Inside a 2% spread an order hits the bid or the ask, and whatever doesn't fill at once is cancelled, so nothing is left resting. If any leg's spread is over 2% when the trade is due, the trade is passed on: it never enters (since 2026-11-18, trades fill at once or not at all). Overnight, a leg with no overnight market is passed on the same way, and the night screen drops names over a 2% overnight spread before the pass researches them. Entries can go in after hours, overnight and pre-market; exits go in regular hours.
- A $25,000 book (`small`) trades the same takes as the $1,000,000 swing book, sized on its own capital; the dashboard's Portfolio view switches between them.
- A book down 10% of its capital from its high pauses itself until `fr paper resume --book swing` (or `small`). `fr paper halt` stops new entries by hand.

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
uv run fr paper learn         # the record so far: takes against passes, by conviction, rank and setup
uv run fr baskets check       # is every themed basket a real basket?
uv run python -m unittest discover -s tests
```

Setting up the Mac (paper gateway, launchd): [docs/operations.md](docs/operations.md#setup-macos).

## Dashboard

`fr serve` serves `dashboard/page.html`, which has these pages:

- **Today**
- **Portfolio:** positions, P/L and closed trades
- **Swing research:** every verdict and its reasoning
- **Market:** regime, factor board and forced flows
- **Baskets:** a table or a treemap heat map
- **Earnings**
- **Chart**

The same page is published every 15 minutes to a [public site](https://mibhaelgray.github.io/etf-factor-rotation/), and an older copy is on claude.ai as the [Factor Rotation Ledger](https://claude.ai/artifact/TVknCZmSXjToEPbmMGPSKv).

## Layout

| Path | Holds |
|---|---|
| `src/factor_rotation/` | the `fr` command and everything behind it |
| `src/factor_rotation/paper/` | the swing book: recording a pass, the order job, the broker guards, scoring |
| `journal/` | what the project decides, in git: briefs, screens, research notes, the paper book (`journal/paper/`), reference lists such as `baskets.json`, and the first book's archive |
| `data/` | market data on this Mac's disk, not in git: bars, pass logs and drafts |
| `dashboard/` | the dashboard page and its local wrapper |
| `deploy/` | the launchd schedule (`launchd/install.py`), `run.sh`, the paper gateway's docker-compose file, the pass prompts |
| `.claude/skills/swing-pass/` | what a research pass is told |

## More detail

- [docs/operations.md](docs/operations.md): data sources, every job, the headless passes, setup and backup
- [docs/signals.md](docs/signals.md): forced flows, the screen and universe, themed baskets, the dashboard, the Portfolio view
- [docs/paper-trading.md](docs/paper-trading.md): order rules and guards, how the book learns, and setting up the paper gateway
- [docs/future-ideas.md](docs/future-ideas.md): ideas kept for later; nothing there is approved

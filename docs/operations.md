# Operations

How the project runs day to day: where the data comes from, every scheduled job, the headless research passes, and setting up the collector. Back to the [README](../README.md).

## Data and jobs

Data collection runs on the collector: a Mac that stays on, plugged in and logged in, with the IB Gateway running for option pulls. It can be a second Mac or the main one; the nightly job stashes any edits in progress around its `git pull` and commits only the files it writes. The main Mac runs the research passes and records their verdicts.

Bars and earnings dates come from Yahoo Finance through `yfinance`, with no login. Option chains, and the borrow figures that arrive with them, come from IBKR and are the only thing that needs the gateway; without it, those sections of the brief and dashboard are empty and everything else runs. Option data doesn't inform a trade decision. The IBKR connector, which doesn't need the gateway, supplies the night's market numbers, pre-market quotes and live marks. Web research feeds the research passes as context, not as stored data.

Since 2026-11-16 the live IBKR account subscribes to US market data (the US Securities Snapshot and Futures Value Bundle and the US Equity and Options Add-On Streaming Bundle, non-professional) and shares it with the paper account, so the paper gateway quotes stocks in regular hours over the API as well as outside them (before that, regular-hours quotes were refused with IBKR error 10089). The paper account borrows the live login's data, so logging in to the live account anywhere else (Client Portal in a browser, the mobile app, TWS) cuts the gateway's quotes off with error 10197. Log out there and restart the gateway (`docker restart deploy-ib-gateway-paper-1`); `fr paper enter` restarts it on its own when it sees that error.

Yahoo is unofficial and can miss a split on its first day. `fr bars` repairs that for ETFs (QUAL and MTUM on 2026-11-05) and lists any other move of 40% or more in the last five sessions as `suspect`, so read its output.

| Command | Runs on | When (ET, trading days) | Writes |
|---|---|---|---|
| `fr options --group all` | collector | 10:00, 15:30 | `options_raw/`, `options_underlying/`, `exposures/`, `exposures_by_strike/` |
| `fr options --group etfs` | collector | 11:00–14:00 hourly | same, SPX and the ETFs only |
| `fr bars` | collector | 16:30 | `bars_daily/`, from Yahoo: the ETFs and stocks, the leveraged funds, AGG, and VIX, VIX3M and the 10-year yield |
| `deploy/run.sh nightly` | collector | 19:00 | runs `fr brief` and the rows below, commits what it and the day's passes wrote under `journal/`, pushes |
| `fr brief` | collector | in the nightly job | `journal/briefs/<as_of>.json`: regime, factor pairs, technicals, forced flows |
| `fr earnings` | collector | in the nightly job | `journal/reference/earnings.json`: earnings dates for every stock the screen covers, from Yahoo Finance: the largest stocks, basket members and held names nightly, the wider tier 150 a night (see "The universe" in [signals.md](signals.md#the-screen-and-the-dashboard)) |
| `fr gaps` | collector | in the nightly job | `journal/reference/earnings_gaps.json`: what stocks did after earnings gaps, against SPY |
| `fr scan` | collector | in the nightly job | `journal/scans/<as_of>.json`: tonight's stock and basket candidates |
| `fr screen` | collector | in the nightly job | `journal/screens/<as_of>.json`: every stock, ETF, basket and factor pair checked against the setups in `journal.SETUPS`; the best 20 with what triggered each and a one-line chart verdict. Where a research pass looks |
| `fr paper side` | Mac | 10:15 PM and 11:45 PM (Sun-Thu), 11:15 AM and 2:15 PM ET | `journal/paper/side/`: side facts from Yahoo for each stock in the night's verdicts (short interest, estimate changes, ratings and targets, insider dealing), pulled once before the open. No research pass reads them; `fr paper side report` scores each fact against how the stocks then did, night by night |
| `fr paper holds` | Mac | any time | every idea, taken or passed, scored as if held 1 to 10 sessions, with and without its stop: where the result builds, and how long the takes' and the ranking's edge lasts. Rebuilt from the proposals and the bars each time; nothing stored. No research pass runs it |
| `fr paper depth` | Mac, inside a pass | before judging | how much research each candidate got, read from the pass's own Claude Code transcripts: each name's agents, searches and pages opened, names with no agent or sharing a crowded one, and the pass's and agents' models against the rule (Fable 5.2 high judges, Opus 5.5 medium researches). `fr paper propose` keeps the night's measurement in `journal/paper/research/` |
| `fr baskets check` | anywhere | monthly, and before adding a basket | prints whether each themed basket is a real basket: over 120 sessions, each member's daily return after its hedge's part is taken out must correlate at least 0.25 with the rest of its basket's, and the basket must average 0.35 (`basket_check.py`). It changes nothing; `--json` for the numbers |
| `fr market` | Mac | after `git pull` | tonight's market rows for the dashboard: factor board, regime, dealer gamma, forced flows |
| `fr serve` | Mac | any time | serves the dashboard at http://localhost:8765 with a Refresh button that updates every panel in about 2.5 seconds: the paper account's fills, today's bar for every name from one bulk Yahoo quote (a five-day pull runs behind the first refresh each day to catch dividends and splits), the book's 15-minute P/L, and the brief and screen rebuilt from those bars for display only (a refresh never writes them to the journal; the research passes read the 7:00 PM ET ones). Earnings dates over 12 hours old update in the background, so they can't hold it up |
| `fr dashboard` | anywhere | any time | `dashboard/local.html`, a local copy of the dashboard built from the journal, and opens it |

**The swing book (live since the night of 2026-11-15).** `fr paper restart --execute` archived the first book and the intraday book to `journal/archive/first-book-2026-11-15/` and started one swing book with $1,000,000 (`journal/paper/book.json`). Three research passes a day judge the live screen, and an order job trades what they record (the `swing-pass` skill; prompts in `deploy/passes/swing-*.md`; schedule from `python3 deploy/launchd/install.py --design three-pass`). The rules a pass works under are in the skill and PROJECT.md: it ranks every candidate and the best one to three takes at conviction 3 or more are entered, $10,000 a side, each with its own time limit (1-10 sessions) and stop (1-8%).

| Command | Runs on | When (Central) | Writes |
|---|---|---|---|
| `fr pass night\|morning\|midday` | Mac (launchd `pass-night`, `pass-morning`, `pass-midday`) | 7:30 PM (Sun-Thu), 8:45 AM, 12:00 PM | runs the pass headless (see "Research passes outside the Claude app"); its result in `data/pass-logs/<session>-<pass>.json`, its research note in `journal/research/<session>-<pass>.md` |
| `fr screen --live night\|morning\|midday` | Mac, inside a pass | at the start of each pass | `journal/screens/live/<session>-<pass>.json`: the whole universe screened on the latest Yahoo prices (after-hours, pre-market or regular session) |
| `fr paper propose <draft>` | Mac, inside a pass | when the pass finishes | the pass's verdicts and ranks in `journal/paper/proposals/`, and its hold, exit or extend for each open trade in `journal/paper/pass_reviews/` |
| `fr paper enter` | collector | every minute | `journal/paper/entries/`: once a pass has recorded, its best 1 to 3 takes as limit orders at IBKR's bid or ask, and the exits it asked for. No quote, or a spread over 2%: it waits and retries every 15 minutes until 2:40 PM |
| `fr paper close` | collector | 2:45 PM | `journal/paper/exits/`: stops and time limits, sent into the closing auction |
| `fr paper sync` | collector | 3:20 PM | `journal/paper/fills/`: the day's fills, commissions, positions and account value |

The night pass skips Friday nights; the morning and midday passes run only on trading days. A by-hand order labelled `cleanup-<trade id>` (no `|SYMBOL`) is counted against that trade in the fill's own symbol, as the CTVA cover of 2026-11-16 was, so a sync that rewrites the day's fills can't reopen it.

The dashboard page lives in `dashboard/page.html`, the same file published to claude.ai as the [Factor Rotation Ledger](https://claude.ai/artifact/TVknCZmSXjToEPbmMGPSKv). The Refresh button only appears under `fr serve`; the claude.ai page can't run a pull. A refresh during the session stores a partial bar for the day, so the screen and the proposals' plan results wait for a session until its bars have been pulled after 4:15 PM ET. `fr dashboard` wraps it with `dashboard/shim.js` (a stand-in for the claude.ai runtime) and `dashboard/tokens.css`, and embeds tonight's market rows, so it opens from disk with no server. Tonight's market rows need the option data in `data/`, so on a machine without it that section shows as failed. Charts load d3 from a CDN.

Market data lives on the collector's disk under `FR_DATA_ROOT` (default `data/` in the repo). The journal (briefs, scans, screens and the paper book) travels through GitHub: the nightly job commits and pushes what it wrote and what the day's research passes recorded.

## Research passes outside the Claude app

Since 2026-11-16 the swing book's three passes run as launchd jobs, not as Claude app tasks: `deploy/run.sh pass-night` (and `pass-morning`, `pass-midday`) runs `fr pass <name>` (`passes.py`), which starts Claude Code headless with the pass's prompt from `deploy/passes/`. A headless run pins the models, which the app can't: the judge runs on Fable 5.2 at high effort (`--model claude-fable-5-2 --effort high`) and every research agent on Opus 5.5 at medium effort (`CLAUDE_CODE_SUBAGENT_MODEL=claude-opus-5-5`, and the prompt tells it to spawn agents with `model: "opus"`, `effort: "medium"`); `fr paper depth` checks both in the record. It signs in with `FR_CLAUDE_TOKEN` from `.env` (from `claude setup-token`) and finds the Claude Code installed by the desktop app.

The run uses `--permission-mode dontAsk` with an allowlist: it can read files, search the web, spawn research agents, write its draft to `data/pass-drafts/`, write its research note under `journal/research/` and edit `journal/paper/lessons.md`, and run only `fr screen --live <pass>`, `fr paper propose` (its own draft), `fr paper postmortem`, `fr paper depth`, `fr paper hedges` and `fr paper status`. Everything else is refused. It has no IBKR connector, so market numbers come from `uv run fr quotes SPY QQQ IWM ^VIX ^TNX CL=F ES=F NQ=F` (Yahoo). A pass has 150 minutes. Each run's result (turns, agents, minutes, anything refused, the final message) is in `data/pass-logs/<session>-<pass>.json` and its output in `~/Library/Logs/factor-rotation/pass-<name>.log`.

`fr pass <name> --shadow` runs a pass on a copy of the journal in `data/shadow-journal` with `FR_PAPER_SEND=0`, so it records and trades nothing that counts: use it to try a prompt change. The first book's pre-close review ran this way from 2026-11-10 (the `pass-pre-close` job, now retired).

## Collector setup (macOS)

1. **IBKR login:** the account must be on IBKR Pro (Lite accounts can't use the API). Create a second username in Client Portal (Settings → Users & Access Rights) and subscribe it to US options (OPRA) and US equity market data. Its own subscriptions mean your own TWS or phone sessions never cut off the collector. Expect an IB Key approval on your phone about once a week, when the gateway's session expires.
2. **Software:** install Docker Desktop (Settings → General → Start Docker Desktop when you sign in), `uv` and git. Clone this repo, set `git config user.name` and `user.email` in it, and give the Mac an SSH key with write access to the GitHub repo and no passphrase, so the nightly job can push unattended.
3. **Settings:** copy `.env.example` to `.env` and fill in the login.
4. **Start the gateway:** `docker compose -f deploy/docker-compose.yml up -d`. It restarts with Docker Desktop.
5. **Keep it awake on power.** Jobs that come due while the Mac sleeps run late, when it wakes, so the snapshots would be mistimed. In System Settings, turn on automatic login, and under Battery → Options turn on "Prevent automatic sleeping on power adapter when the display is off". Or from a terminal:
   ```bash
   sudo pmset -c sleep 0 disksleep 0
   ```
   Leave the lid open, or closed with an external display attached; a closed lid without one sleeps regardless. Each job also runs under `caffeinate`, so the Mac stays awake until it finishes.
6. **Install the schedules:** `python3 deploy/launchd/install.py --design three-pass` (the live swing book; without the flag it installs the first book's jobs). It converts the New York schedule to the Mac's time zone (rerun it if that changes) and installs one launchd agent per job. `--dry-run` shows the plists without installing, `--uninstall` removes them.
7. **First run:** `uv run fr bars` (backfills 5 years of bars, no gateway needed), then `uv run fr options --symbols SPY,QQQ` to check permissions and timing.

Logs: `~/Library/Logs/factor-rotation/<job>.log`. To run a job now: `launchctl kickstart gui/$(id -u)/com.factor-rotation.nightly`.

A Linux box works too: `deploy/systemd/` holds the same schedule as systemd timers, with the repo at `/opt/factor-rotation`.

## Moving the collector to another Mac

1. Set up the new Mac with steps 1–6 above, but don't start the gateway yet.
2. On the old one: `python3 deploy/launchd/install.py --uninstall`, then `docker compose -f deploy/docker-compose.yml down`. One gateway per username, so stop this one first.
3. Copy the market data across (Remote Login on, on the new Mac): `rsync -az data/ newmac.local:<repo path>/data/`. Past option chains can't be re-downloaded, so check the file counts match before deleting anything.
4. On the new Mac: start the gateway, install the schedules and kickstart `bars` to check it connects.

## Backup

The collector's disk holds the only copy of the market data, and past option chains can't be re-downloaded. Copy it to the main Mac now and then (turn on System Settings → General → Sharing → Remote Login on the collector first):

```bash
rsync -az collector.local:<repo path>/data/ ~/Documents/"ETF Factor Rotation"/data/
```

# Paper trading

The swing book's order rules and guards, how it learns, and the paper gateway. The jobs and their times are in [operations.md](operations.md#data-and-jobs). Back to the [README](../README.md).

The swing book trades on an IBKR **paper** account, so its trades get real fills, slippage, borrow and commissions. It takes only what a research pass believes in: each pass judges every candidate on its live screen, and its takes are traded with the plan the pass wrote. The first book (2026-11-09 to 11-15) and the intraday book were retired at the restart of 2026-11-15; their records are in `journal/archive/first-book-2026-11-15/` and their code is in git history before 2026-11-17.

## Order rules

- **No order without the gateway's own bid and ask**, in any session. A buy is priced at the ask and a sell at the bid, as a limit 0.5% beyond so a small move still fills (`entries.LIMIT_SLIP`). Until 2026-11-13 a missing quote fell back to Yahoo's price; that morning one side of a pair went in alone.
- **Nothing rests.** Whatever an order didn't fill at once is cancelled before the run ends, and the next try hits again.
- **A 2% spread limit** (`rules.MAX_CROSS_SPREAD`). A trade with any leg wider than that when it is due is passed on and never enters. Overnight, so is a leg with no overnight market. Only a gateway fault (no quote while the gateway is down or refused) waits and is retried every 15 minutes until 2:40 PM CT.
- **The hardest leg leads** (`rules.lead_leg`, `rules.entry_due`). The leg whose dollars are largest against its daily dollar volume is ordered in full; the others only follow the share of it that has filled, so a stuck order never leaves a hedge on its own.
- **Sessions.** Entries can go in after hours (3:00 to 7:00 PM CT), on IBKR's overnight venue (7:00 PM to 2:50 AM CT, Sunday to Thursday nights), pre-market (3:00 to 8:10 AM CT) and in the regular session until 2:40 PM CT. Exits go in the regular session only.
- **Stops and time limits.** A plan is the pass's call inside outer limits in `paper/rules.py`: a time limit of 1 to 10 sessions and a stop between 1% and 8% of one side. Stops are checked every ten minutes in regular hours on Yahoo's latest prices, and a trade past its stop exits at once at the bid or ask. At 2:45 PM CT the close job sends time limits, and any stop exit that couldn't be sent, as market-on-close orders. That is the one exception to the quote rule: a trade past its stop must not be left open for want of a quote.
- **Reviews.** Every pass gives every open trade a hold, an exit or an extension. An exit needs a named reason (the thesis broke or played out). An extension is 1 to 3 sessions, once, on a trade's last session, and the close job grants it only if the trade is in profit. A review can never widen a stop or keep a loser past its time limit.
- **Limits.** Each trade is sized by its risk (`rules.side_dollars`; see [operations.md](operations.md#data-and-jobs)). No side is over 6% of the book or 0.25% of a leg's daily dollar volume, net exposure to one symbol stays under 10% of the book, gross exposure stays inside the book's capital, and the book holds at most 60 open trades.

Prices for sizing, stops and scoring come from Yahoo, asked more than once because a Mac that has just woken may have no network yet. If no price arrives, that trade's stop isn't checked that time and the job says so. Each warning, and any order the gateway cancels or comments on, is written in the job's file, shown on the dashboard and in `fr paper report`, and raised as a Mac notification.

## How the book learns

Every proposal, taken or passed, is scored against its own plan from the prices at the moment its pass was acted on (`marks` in `journal/paper/entries/`), to its stop or time limit, before costs. Its stop is replayed on the order job's own ten-minute checks from 5-minute bars that the nightly job stores (`fr bars --intraday`, `learn._intraday_hit`, since 2026-11-22), and checked again at each close; a day whose 5-minute bars never arrive is checked at its close only. A take's actual IBKR result is kept beside it (`paper_bps`). So the passes are the control for the takes. Every pass also ranks every verdict from best to worst (`fr paper propose` refuses a draft without it), and `learn.rank_table()` scores the ranking: the rank correlation between rank and plan result within each pass, averaged, with the results of each pass's top, middle and bottom third.

Each closed trade gets a post-mortem from the night pass (`fr paper postmortem`): right for the reason given, right by luck, the reasoning broke, or noise, with a one-line lesson. `journal/paper/lessons.md` holds the standing lessons the pass keeps.

`fr paper learn` prints all of it on one page: results by verdict, conviction, rank and setup, open trades with what would prove each wrong, and the lessons. Every pass reads it first, because the pass remembers nothing between runs. `fr paper hedges SYMBOL` lists each ETF's 60-session correlation and beta with a name, for choosing what goes on the other side.

## Guards

- **`paper/broker.py` is the only code that can send an order.** It refuses unless the port is a paper port (4002 or 7497), `FR_PAPER_ACCOUNT` is a DU account, and every account on the gateway is a paper account. Orders can only be limit orders at the bid or ask, or market-on-close.
- **A run by hand only looks.** An order job (`fr paper enter`, `close`) run by hand prints the orders it would send and sends none. `deploy/run.sh` exports `FR_SCHEDULED=1`, so the scheduled jobs trade as usual, and `--execute` makes a run by hand count.
- **`FR_PAPER_SEND`.** Without `FR_PAPER_SEND=1` every job is a dry run. `.env` supplies that setting whenever it isn't already set, so put `FR_PAPER_SEND=0` on the command line to be sure.
- **`fr paper halt --reason "..."`** stops new entries (exits still go out) until `fr paper resume`.
- **The 10% pause** (`drawdown.py`, PROJECT.md) does the same on its own when the book is down 10% of its capital from its high ($100,000 on the $1,000,000). It writes `journal/paper/PAUSE-swing`, the order job checks it before sending entries, and it stays until `fr paper resume --book swing`, which starts the high again from the book's value then.
- **`fr paper close --flatten --execute`** exits everything at today's close.
- **`fr paper status`** connects read-only and compares the account's positions with the book; `fr paper report` prints the results.

**Two books, one set of decisions** (since 2026-11-22). `journal/paper/book.json` gives each book its capital: `swing` $1,000,000 and `small` $25,000. The order job enters every take in both, sized on each book's capital; a small-book trade has `"book": "small"` and an id with `M` (`2026-11-23-M01`) where the swing book's has `S`, and its orders carry that id, so the fills stay apart. Passes review swing trades only and `proposals.reviews` applies each decision to the small twin (same `proposal_id`). Each book has its own caps and its own 10% pause (`PAUSE-small`, `fr paper resume --book small`).

The book is a sub-account of the paper account: its value is its $1,000,000 of capital plus the realized and open profit and loss from its own fills, after commissions. Whatever the account is worth beyond the book is shown as "unassigned"; it should move slowly, and a jump means a position or cost the book doesn't account for.

Tests for the paper jobs run against a stand-in broker: `uv run python -m unittest discover -s tests`.

## The paper gateway

Setup is in [operations.md](operations.md#setup-macos). The gateway runs in Docker (`deploy/docker-compose.yml`), logged in with the paper username from `.env.paper`. It publishes the paper API on 127.0.0.1:4002 only, with the API's read-only switch off and the order precaution dialogs bypassed. `docker logs -f deploy-ib-gateway-paper-1` shows the login.

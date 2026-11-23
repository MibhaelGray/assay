# What this project is for

Written 2026-11-12 from a discussion with Michael. This is the reference for staying on track: new work should
serve what is written here. Change this file when the intent changes.

Brought up to date on 2026-11-22: the three-pass restart agreed on 2026-11-15 has been live since that night, so
it is now the description of the book, and the first book is history.

## The question

**How far can AI research be trusted?** Specifically: can AI research, working with the right technical
signals and data sources, produce alpha?

## Where it is going

An AI-run trading book that manages real money, reached in three stages:

1. **Paper, automatic (now).** The system decides and trades on an IBKR paper account by itself, to build a
   track record quickly.
2. **Real money, approved.** Michael approves every real trade. The real account would be about $15,000.
3. **Real money, self-managing.** The system manages the book almost entirely itself.

Along the way it is also three things at once: a research assistant that surfaces good trades, an experiment
whose answer matters whichever way it comes out, and something to build and learn from.

## What "it works" means

- **Consistently beating the S&P 500's return without taking on excessive risk**: an above-average Sharpe ratio.
- **The S&P 500 is the benchmark** however the book chooses to trade (pairs, outright longs, outright shorts).
- **Real signal means statistically significant**, not a good-looking week. The measure:
  - *Main test:* the trades the research takes beat the ones it passes on, with under a 5% chance the gap is luck.
  - *Second test:* the book's return after costs is reliably above zero and above the S&P's over the same days.
  - *Early test (added 2026-11-13):* every pass ranks every candidate it judges, best to worst, and the
    better-ranked ones go on to do better. It uses every verdict (up to 25 a pass since 2026-11-22; about forty from 11-16), not only
    the takes, so it can speak in a few weeks. It is an early read on the research's judgment, not a substitute
    for the main test.
  - The test and the bar are fixed in advance and checked on a schedule (weekly), not whenever it looks good.
- **Excessive risk: a 10% drop.** If the book falls 10% of its capital from its high, $100,000 on the $1,000,000,
  trading pauses and we work out what to do differently. The pause is automatic (`drawdown.py`): the book opens
  no new trades, its exits still go out, and it stays paused until resumed by hand with
  `fr paper resume --book swing`.

## How it is built

**Technical signals find the candidates; AI research decides; code trades.** There is one book, the swing book:
$1,000,000 on the IBKR paper account, trades held 1 to 10 sessions. It is the closest to how Michael trades.

- **Three full passes a day, each the same thing:** 7:30 PM CT (Sunday to Thursday, for the next session),
  8:45 AM CT and 12:00 PM CT. Code screens the whole universe (about 1,240 names since 2026-11-18, when big
  US-listed foreign companies such as TSMC were added) on the latest prices, whether after-hours, pre-market or
  the regular session, never the prior close. A research agent studies each shortlisted name. The judge gives
  every candidate a verdict, a conviction and a rank, adds up to three ideas of its own, and reviews every open
  trade. A second night pass (`late`) can be run by hand for names the night pass did not judge.
- **Each pass judges up to 25 candidates** (since 2026-11-22; 40 from 11-16, 20 before), and no kind of setup
  gets more than 6 of them, so the list can be shorter. At 40, a third of all verdicts went to stretched names the
  judge took once in 161, and most names came back thinly researched. Every candidate gets a verdict and a rank.
- **One judge, the same every time.** The passes run as background jobs outside the Claude app, so the models are
  pinned: the judge is Fable 5.2 at high effort, and each research agent is Sonnet 5.5 at medium effort.
- **Research stays focused, and is left to its own judgment.** One agent a name with the same bare brief every
  time, about 8 searches and 5 pages read each, checked by `fr paper depth` before the pass judges; a thin name
  gets one more agent, once (2026-11-22, now that a pass has 25 names; from 11-19 a thin name was only noted). Nobody tells an agent what to read or the judge how to weigh it. What is
  fixed is the setup and what gets recorded and scored.
- **Every verdict is recorded before the trade and scored afterwards**, taken or passed, so the passes are the
  control group. A take keeps the judge's full case, a pass a short one (for it, against it, why not), and every
  research agent's report is kept in `journal/research/agents/` (since 2026-11-22, for the record; no pass reads them). Every verdict is scored from the prices at the moment its pass recorded, its stop replayed on the same
  ten-minute checks the book makes (from stored 5-minute bars, since 2026-11-22), and every pass's
  ranking is scored.
- **Trades go out as soon as the pass records.** Every take of the pass (since 2026-11-16; the best 1 to 3 before
  that), strongest conviction first until the book's capital is in use, as orders that hit IBKR's own bid or ask.
  Nothing is left resting: what an order doesn't fill at once is cancelled, and the next try hits again. The
  research pass never sizes a trade or sends an order; the order job (`fr paper enter`, every minute) is the only
  place its decisions become orders.
- **No order without IBKR's quote, in regular hours too.** A trade with any leg over a 2% spread, or overnight
  with no overnight market at all, is passed on and never retried (the owner, 2026-11-18). So is a trade whose orders hit the
  bid or ask three times with nothing filled: a quote with nothing behind it isn't chased (2026-11-22). Only a gateway fault
  (no quote while the gateway is down or refused) waits and is tried again every 15 minutes until 2:40 PM CT.
- **The night pass sees overnight prices** (since 2026-11-22). Its screen quotes the whole universe on IBKR's
  overnight venue, and a name that has moved 1.5 ATR or more net of SPY, on a real overnight trade inside a 2%
  spread, is a candidate, so news from the weekend or late evening reaches the list. Every sweep is kept beside the
  screen.
- **The night pass researches only what can trade overnight.** Its screen drops candidates whose own legs are
  over 2% wide on IBKR's overnight venue, or have no overnight market, and fills the list from the next names.
- **Every trade risks the same share of the book** (set 2026-11-16). A trade's size is its risk budget over its
  stop: 0.15% of the book at conviction 3, one and a half times that at 4 and twice at 5. A typical trade
  (conviction 3, 5% stop) is 3% of the book a side. No side is over 6% of the book, or over a quarter of a
  percent of its stock's daily dollar volume, and net exposure to one symbol stays under 10% of the book. The
  rule is in shares of the book, so it sizes a $15,000 account the same way. Takes and passes are still scored
  per trade, in percent, so the main test doesn't depend on size.
- **Holds stay 1 to 10 sessions.** A trade entered during the day counts its time limit from the next session.
  Stops are checked by code every ten minutes from 8:40 AM to 2:40 PM CT (every two minutes from 2026-11-18 to 11-22), and a trade past its stop exits at once
  at the bid or ask (since 2026-11-18). Time limits, and a stop exit that couldn't get a quote, leave at the
  2:45 PM CT close. On a half day (12:00 PM CT close, such as the day after Thanksgiving) everything moves three
  hours earlier: entries and stop checks stop at 11:40 AM CT, the close job runs at 11:45 AM CT, and there is no
  midday pass (since 2026-11-22).
- **The system watches itself.** A watchdog (`fr watch`, every five minutes) messages the owner's phone when a
  pass, a job or the paper gateway breaks, and each pass says when it starts, records or fails. Alerts never
  carry account numbers, positions or dollars.

Why three passes: the first book's four kinds of pass and seven order jobs were hard to follow, and a pass
judging off the prior day's close was acting on old prices.

## Only the swing book

The project is the swing book and its dashboard, and nothing else (the owner, 2026-11-22). New work has to serve
the swing book to belong here.

Two records are kept beside the swing book's verdicts, and no pass reads either: `fr paper side` records Yahoo
facts (short interest, estimate changes, ratings, insider dealing) beside each night's verdicts, and
`fr paper holds` scores every idea at holds of 1 to 10 sessions. Both are kept to be tested later, and the tests
only work while no pass has seen them.

## History

- **The first book (2026-11-09 to 2026-11-15).** One evening pass on closing prices with a pre-open and a
  pre-close pass, every trade a flat $10,000 a side, and an intraday book beside it (signals from 15-minute bars,
  judged by Haiku, flat by the close). Both were closed at the restart, and the record began again from zero.
  Their files are kept in `journal/archive/first-book-2026-11-15/` as a closed "before" period, and the intraday
  book's code was removed.
- **Removed on 2026-11-22**, to keep the project on the swing book: a small-cap shadow book ($250M to $1B
  companies, verdicts recorded from 2026-11-19 with no trades), a research lab that tested ideas on history
  (started that day), and the option-chain collector (last run 2026-11-05; the chains it stored are still in
  `data/`). Their code and records are in git history before that date.

## Data

Prices, volume and earnings dates come from Yahoo. Every order is priced from IBKR's own bid and ask on the
paper gateway. The AI reads the web itself during each pass; there is no news sentiment score.

If there is no real signal in the next few weeks, we start testing more data sources. Short interest is the
first: it is already being recorded beside the verdicts (side facts, above) so it can be tested before any pass
uses it. The rest is to be decided then.

## Working rules already set

- Paper only until stage 2. Nothing places a live order.
- Scheduled passes trade and log. A pass run by hand records nothing and sends nothing unless told to.
- Reasoning, plan and "wrong if" are written before the trade.
- Take trades to learn: a night with no takes shows nothing.
- Option data doesn't inform a trade decision.
- Orders fill at once: buy at the ask, sell at the bid, unless the spread is over 2%, when nothing is sent. No
  order is left resting: what doesn't fill at once is cancelled.
- A trade is in the book from its first order, and the book adds up to the account.
- No leg gets ahead of the hardest one to fill (set 2026-11-16): a trade's hardest leg is ordered first and its
  other legs only follow what that leg has filled, so a stuck order never leaves the book holding a hedge on its
  own.

## Not decided yet

- What has to be true to move from stage 1 to 2, and from 2 to 3.
- What we do if the test fails after a fair run.
- Whether the research passes should lean toward outright trades now that the S&P is the benchmark: the screen
  gives every candidate a hedge by default, and a hedged book can do well and still trail a rising S&P.
- Which data sources to test after short interest.
- Whether a stop or time-limit exit may go out when IBKR gives no quote. As built it may: the close check judges
  the stop on Yahoo's latest price and sends a market-on-close order, the one exception to the quote rule.

Settled since this list was first written: the paper gateway does quote US stocks in regular hours, since the
live account's market-data subscription of 2026-11-16.

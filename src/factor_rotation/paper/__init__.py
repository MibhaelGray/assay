"""Paper trading on IBKR: the ideas traded for real fills on a paper account, never a live one.

One swing book of $1,000,000 (journal/paper/book.json, written by `fr paper restart`), run by three research
passes a day on live prices. A pass gives every candidate on its screen a verdict, a conviction and a rank
(daily.py, proposals.py); only the takes are traded, each with the plan the pass wrote, inside the outer limits
in rules.py. Each pass also reviews every open trade, and the book learns from every result (learn.py).

The jobs:
- every minute  `fr paper enter`  the order job (entries.py): once a pass has recorded, sizes its takes and sends
                                  them at the paper gateway's bid or ask (gateway.py), in whatever session is
                                  trading, and sends the exits a pass asked for.
- 3:45 PM ET    `fr paper close`  checks every open trade against its stop and time limit on current prices
                                  and sends market-on-close exits. An earlier exit that never filled goes again.
- 4:20 PM ET    `fr paper sync`   pulls the day's fills, commissions and positions from the paper account.

Beside these, `fr paper side` records facts about each night's ideas that no pass reads, to test later whether
they would have helped (side.py). `fr paper holds` scores every idea at every hold from 1 to 10 sessions, to
learn which hold period pays (holds.py); no pass runs that either.
`fr paper depth` measures how much research each candidate got from the pass's own transcripts (depth.py).

Everything lands in journal/paper/, and each job's record lists its warnings in plain words; the dashboard and
`fr paper report` show the latest. broker.py holds the guards that keep this off any live account.
FR_PAPER_SEND=1 normally comes from .env, which is read whenever the variable isn't already set: to run a
job by hand without sending, set FR_PAPER_SEND=0 on the command line.

The first book (2026-11-09 to 11-15, one pass a night, entries at the open) and the intraday book were retired
at the restart; their records are in journal/archive/ and their code is in git history before 2026-11-17.
"""

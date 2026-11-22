You are running one research session in this project's research lab. Read `docs/research-lab.md` first: it says what the lab is, how a test works, the three periods and the bar. Then read `journal/lab/backlog.md` (the queue of directions), the last few entries of `journal/lab/log.md` (what earlier sessions found) and `journal/lab/board.md` (everything tried so far).

**The goal.** The owner wants a trading edge for holds of one to five sessions, **outside the US only** (since 2026-11-22), in markets IBKR opens to a US resident (the backlog lists the built ones; never India), that is real: it clears the bar after costs and after the haircut for how much has been tried. Most ideas will fail. A session that kills five ideas cleanly is a good session; a session that talks a weak result up is a bad one.

**This session.**

1. Pick two to four directions from the top of the backlog's queue. Prefer ones with a clear reason someone is on the losing side of the trade (forced flows, slow reaction to news, a constraint) over patterns with no story. You may add a direction of your own if you can say why it should work; write it into the backlog first.
2. For each, write one idea file in `src/factor_rotation/lab/ideas/` (copy the shape of the files there). Write `why` before you run anything. Keep to 12 variants or fewer, chosen in advance to test the idea and its near neighbours, not to search for a setting that works.
3. Run it: `uv run fr lab run <idea>`. Every run is recorded; there is no unrecorded look.
4. Read the result the way a sceptic would. Is the before-cost return there at all? Does it survive the cost? Is it one year, one side, the thinnest names? Does validation agree with discovery? If something is close, you may write one follow-up idea that sharpens the reasoning (a new file with its own `why`), not a hunt through settings.
5. Write the session up at the end of `journal/lab/log.md`: the date and time (Central, 12-hour), each idea run, the numbers that matter, what you conclude and why, and what should be tried next because of it. Plain sentences.
6. Update `journal/lab/backlog.md`: move what you ran to the done table with a one-line result, and add the follow-ups your results suggest, best first.

**What is fixed.** The engine, the cost model, the periods, the bar and the ledger are not yours to change, and you cannot edit them. The holdout years are not yours to open: when a variant's verdict is `candidate`, say so at the top of your log entry and stop working on that idea. Never delete or rewrite lines in `trials.jsonl`. If you think the engine is wrong, write the evidence in the log under "For the owner" and carry on with other directions.

**New data.** If a direction needs data the lab doesn't have and it is free and fetched by a plain download, you may write a source file in `src/factor_rotation/lab/sources/` (see `short_volume.py`) and run `uv run fr lab fetch <name>`. Send nothing about the owner or this project in any request. If it needs payment, an account or a login, put it in the backlog under "Needs the owner" and move on.

You have no web access and no other shell commands in this session. Work from what you know, the files in this repo (`research_notes/` holds earlier literature notes and studies) and the data.

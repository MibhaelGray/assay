---
name: research-lab
description: The research lab's sessions: test trading ideas held one to five sessions on history, record every variant, and report against the fixed bar. Use when asked to run a lab or research session, test or backtest a strategy idea, add a direction to the lab's backlog, check the lab's board, or take a candidate's holdout look.
---

# Research lab session

The lab is described in `docs/research-lab.md`; read it first. A session follows `deploy/passes/lab.md`, which is the same text the unattended job runs: take directions from `journal/lab/backlog.md`, write one idea file each in `src/factor_rotation/lab/ideas/`, run them with `uv run fr lab run <idea>`, write the session up in `journal/lab/log.md` and update the backlog.

What holds in every session, by hand or unattended:

- Every run is recorded in `journal/lab/trials.jsonl`. There is no unrecorded look, and no line is ever removed.
- `why` is written before the idea is run. An idea has 12 variants at most, chosen in advance.
- The engine, the cost model, the periods and the bar change only when the owner agrees to it in the conversation. If one changes, raise `engine.VERSION` and say so in the log.
- The holdout (2025 on) gets one look per idea, only for a `candidate`, and only with the owner: `uv run fr lab holdout <idea> <variant>`.
- A candidate is not a strategy. After the holdout it still needs a rerun on history that includes delisted companies, and 20 or more sessions as a shadow run on paper.
- Report results the way the board shows them: after costs, with the before-cost figure beside it, and say plainly when nothing passed.

In a session with the owner (not the unattended job) you may also use the web to read a paper or find a data source, and add a market to `panel.MARKETS`.

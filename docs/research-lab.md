# The research lab

Started 2026-11-22. A way to test a lot of trading ideas on history, quickly, for trades held one to five sessions, and to keep an honest count of everything tried. It is separate from the paper books: it has its own price history, places no orders and reads nothing from the trading record.

Code: `src/factor_rotation/lab/`. Record: `journal/lab/`. Data: `data/lab/` (not in git; rebuilt from the web).

## What a test is

An idea is one small file in `lab/ideas/`. It says why the idea might work (written before it is run), which stocks it looks at, a few variants, and a `score()` function that gives every stock a number each day from data known at that day's close. The lab turns the scores into a portfolio and works out what it would have earned:

- It enters at the next session's open, holds 1 to 5 sessions, and leaves at a close or an open. A new slice of the book goes on every day.
- Every trade pays a cost set by how much the stock trades: 2 basis points each way for the most traded names, up to 60 for the thinnest (`engine.COST_BPS`). Shorts pay a borrow fee. Each result is also shown with costs doubled.
- A long-short book is half long and half short. A one-sided book can be hedged with SPY.

`fr lab run <idea>` runs every variant and prints one line each.

## The three periods

| Period | Years | Used for |
|---|---|---|
| Discovery | 2010 to 2021 | Finding ideas. Look as much as you like. |
| Validation | 2022 to 2024 | Checking an idea found on discovery, with nothing changed. |
| Holdout | 2025 on | One look per idea, only for an idea that passed the first two. |

Every command but `fr lab holdout` loads prices that stop at the end of 2024, so nothing can see the holdout years by accident.

## The bar

Fixed on 2026-11-22, before the first idea was run (`lab.BAR`). All figures are after costs.

| Stage | To pass |
|---|---|
| Discovery | Sharpe 1.0 or more; t of 3 or more; Sharpe 0.5 or more with costs doubled; up in 70% of years |
| Validation | Sharpe 0.7 or more, same settings |
| Luck haircut | 0.95 or more (below) |
| Holdout | Sharpe 0.5 or more, one look |
| Forward | 20 or more sessions as a shadow run on paper before it trades anything |

A variant's verdict is `candidate` (passes discovery and validation), `faded` (passed discovery only), `weak` (something there, under the bar) or `dead`.

**The luck haircut.** Test enough ideas and one will look good by chance. Every variant ever run is a line in `journal/lab/trials.jsonl`, failures included, and the ledger is only added to. The `deflated` column is the chance a variant's discovery Sharpe is real given how many variants have been tried (the deflated Sharpe ratio of Bailey and Lopez de Prado). It falls for every idea as the count grows. That is the price of searching widely, and it is why each idea is limited to 12 variants.

## What the history can't show

- **Companies that no longer trade.** Yahoo drops a delisted stock, so every past year holds only names that survived to today. This flatters anything that buys beaten-down stocks. A candidate found here has to be confirmed forward on paper, or on bought data that includes delisted names, before it is believed.
- **Hard-to-borrow shorts.** The borrow fee is a flat estimate. A short-side result in small names is unconfirmed until IBKR's borrow list has been checked.
- **Intraday prices.** Entries are at the open or the close. Nothing here can test an entry at 10:30 AM.
- **Earnings report times.** The calendar gives the date but not whether a report came before the open or after the close, so earnings ideas decide from that day's own move and volume.

## Commands

```bash
uv run fr lab pull            # prices from Yahoo, 2010 on (about 3,300 US stocks and 40 funds)
uv run fr lab fetch earnings  # earnings report dates and surprises from Nasdaq's calendar
uv run fr lab fetch short_volume   # FINRA's daily short-sale volume, August 2018 on
uv run fr lab ideas           # every idea file, and whether it has been run
uv run fr lab run <idea>      # run an idea's variants and add them to the ledger ("new" = every idea not yet run)
uv run fr lab board           # everything tried, best first (also written to journal/lab/board.md)
uv run fr lab holdout <idea> <variant>   # the one look at 2025 on
uv run fr lab session         # one unattended research session (below)
```

## Adding an idea

Copy any file in `lab/ideas/`. Fill in `why` before running it. `score(p, u, **params)` gets the panel `p` (price tables: `p.close`, `p.open`, `p.volume`, `p.cc` for close-to-close returns and so on) and `u`, the table of which stocks are tradable each day. It returns a table of scores: higher means buy, a gap means not in play. Shared building blocks are in `lab/feat.py`. A row of the score may use data up to that row's close and nothing later: `shift(1)` looks back, `shift(-1)` looks ahead and is only right for calendar facts known in advance.

Settings in `base` and `variants` that the engine knows (`hold`, `side`, `select`, `frac`, `n`, `entry`, `exit`, `hedge`, `max_w`) go to the engine; the rest go to `score()`. A new data source is one file in `lab/sources/` with a `pull()`.

## Research sessions

`fr lab session` runs one unattended session: Claude Code, headless, on the prompt in `deploy/passes/lab.md`. It takes the next directions from `journal/lab/backlog.md`, writes the idea files, runs them, and writes what it found to `journal/lab/log.md`. It can edit only idea files, source files and `journal/lab/`; it cannot change the engine, the bar or the ledger's rules, cannot open the holdout, has no web access and cannot reach the gateway. When a variant becomes a candidate it sends a phone alert, and the holdout look is taken with the owner.

## Markets

Since 2026-11-22 the lab works **outside the US only**, in markets IBKR opens to a US resident. `panel.MARKETS` lists the built ones; `us` stays for the record.

| Market | Names | Hedge | Cost added a side | Shorting at IBKR |
|---|---|---|---|---|
| `jp` Tokyo | TOPIX members (JPX list) | 1306 | 6 bp commission | Yes |
| `hk` Hong Kong | Main Board and GEM shares (HKEX list) | 2800 | 16 bp: stamp duty 0.10% each side, commission, levies | Yes |
| `kr` Korea | KOSPI (.KS) and KOSDAQ (.KQ) (KRX's KIND list) | 069500 | 18 bp: sell tax ~0.20% split over both sides, commission | No |
| `tw` Taiwan | TWSE (.TW) and TPEx (.TWO) shares (TWSE ISIN lists) | 0050 | 25 bp: sell tax 0.30% split, commission | No |
| `au` Australia | ASX listed companies (ASX list) | STW | 8 bp commission | Yes |
| `ca` Canada | S&P/TSX Composite (Wikipedia) | XIU | 2 bp | Yes |
| `uk` London | FTSE 100 and 250 (Wikipedia; prices in pence) | ISF | 30 bp: stamp duty 0.5% on buys split, commission | Yes |

These are added to the liquidity-based cost in `engine.COST_BPS`. A tax paid on one side only is split evenly, which overcharges a short leg and undercharges a long one a little. Lists are fetched by `lab/listings.py` (`uv run --with openpyxl --with lxml python -c "from factor_rotation.lab import listings; listings.write('hk')"`), then `uv run fr lab pull --market hk`. In markets without shorting, test long-only or long against the hedge fund (standing for an index future). `min_price` is in US dollars everywhere, so lower it for Hong Kong, where many large companies trade under US$5.

Open at IBKR, not built: Euronext, Germany, Switzerland, China A-shares (Stock Connect), Saudi. Closed: India.

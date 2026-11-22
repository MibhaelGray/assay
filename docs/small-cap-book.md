# Small-cap book: design

Agreed with Michael on 2026-11-19. Steps 1 to 4 of the build order (the shadow run) were built the same night:
`src/factor_rotation/smallcap/`, the pass prompt `deploy/passes/smallcap.md` and the launchd job `pass-smallcap`
(4:30 PM CT on trading days). On 2026-11-20 two live passes were added in regular hours, `pass-smallcap-morning`
(9:30 AM CT) and `pass-smallcap-midday` (1:00 PM CT), so the book works like the swing book's three passes without
the overnight and pre-market ones. The trading side (step 5) is not built.

## Why

`PROJECT.md` asks how far AI research can be trusted. The research should be worth most where few
professionals are looking, so this book puts the same research judge on thinly covered US companies.
It is a second experiment beside the swing book, not a replacement for it.

## The universe

- **Size:** $250M to $1B in market value (set by Michael).
- **Tradeable:** price $2 or more, and over $1M traded on a median day.
- **Left out:** biotech and pharma, banks and savings institutions, and REITs. Other finance stays in.
- **Where the list comes from:** Nasdaq's screener for names, market values and industries; Yahoo for
  daily bars. Rebuilt weekly.

Measured on 2026-11-19 (2,121 US stocks under $3B, three months of daily bars):

| | Value |
|---|---|
| Names in the universe | about 420 (459 before removing REITs) |
| Median traded a day | $6.0M |
| Estimated spread | about 1.0% (from daily highs and lows, not live quotes) |
| Median analysts | 3 |
| Names with two analysts or fewer | 39% (sample of 83) |

Why this band: below $150M most names can't be traded (under half trade $1M a day), and above $1B
coverage jumps to a median of six analysts. Coverage is flat from $250M to $1B.

## What the screen looks for

1. **Earnings and guidance reactions:** a gap on a report, held or faded.
2. **Company news:** contracts, buybacks, guidance changes, with a price and volume reaction.
3. **Insider buying:** open-market purchases by officers and directors (SEC Form 4).

Each candidate also carries its analyst count, so results can later be compared between thinly and
well covered names.

## The pass

Three passes on trading days, the swing book's design in regular hours only:

| Pass | Time (CT) | Prices | Entry |
|---|---|---|---|
| morning | 9:30 AM | the first hour's trading | the live price when the pass records |
| midday | 1:00 PM | the day so far | the live price when the pass records |
| night | 4:30 PM | the close | the next session's open |

A live entry is a long at the gateway's ask or a short at its bid, with IWM on the other side at its own bid or ask
(Yahoo's last price if the gateway has no quote). A name judged earlier in the same session isn't screened again.
Each pass uses one research agent per name,
a judge that gives every candidate a verdict, a conviction and a rank, and scoring of every verdict
whether taken or not.

One addition for this universe: every write-up must cover dilution risk (cash runway, open
share-sale programs, warrants and convertibles).

## Trading rules

- **Regular hours only.** No after-hours, overnight or pre-market entries: these names have no usable
  market then.
- **Instant fills or pass**, as in the swing book. Nothing rests.
- **Spread limit: 1.5% a leg** (provisional; to be checked against live gateway quotes before it is fixed).
- **Longs and shorts.** A short needs IBKR to show shares available to borrow.
- **Hedge: IWM** (the small-cap ETF), one hedge for the whole book, sized to its net exposure. No
  per-name hedges.
- **Capital: $25,000** (Michael, 2026-11-20). Enough that commissions and IWM's share price stay small next to
  the ~1% spread, and small enough that every fill is realistic in these names. It sits inside the paper account
  as a virtual sub-account, like the swing book. The 10% pause applies ($2,500).
- **Sizing:** risk per trade as a share of the book, over the stop: 0.5% at conviction 1 to 3, 0.75% at 4, 1% at
  5. On a typical 10% stop that is $1,250 to $2,500 a position. No position over 10% of the book ($2,500), and
  none over a quarter of a percent of the stock's daily dollar volume. Larger than the swing book's 0.15%
  because small caps need wider stops and the book holds fewer names.
- **No overlap:** a name the swing book holds or is entering is skipped.

## How it starts: a shadow run

For the first three weeks the pass runs every night and records and scores every verdict, and places
no trades. Reasons:

- The judge's edge is unproven in any universe so far. This tests it here at no risk.
- It shows whether research on thinly covered names comes back usable.
- It measures real spreads and borrow before any order depends on them.

After three weeks, the check: do better-ranked names go on to do better, and do takes beat passes?
If yes, trading turns on. If not, we decide whether to change the universe or stop.

## Build order

1. Universe builder and weekly refresh.
2. Screen for the three setups.
3. Pass prompt and schedule (shadow: record, no orders).
4. Scoring report for this book.
5. Trading side (order job, IWM hedge, borrow check), only after the shadow check.

## As built (2026-11-19)

- The universe came to 425 names (366 since 2026-11-20, when 59 closed-end funds, business development companies, REITs and other non-operating companies were taken out; `universe.NOT_OPERATING` and a name rule keep them out of each rebuild); 150 of the 418 with a known count have two analysts or fewer.
- Screen thresholds, against IWM: an earnings reaction is 3% and one typical daily range; a news move is 5%, one
  and a half ranges and twice usual volume; insider buying is $100,000 in a week by officers or directors (a
  holder who is only a 10% owner doesn't count), or two insiders and $50,000. Up to 20 candidates a night.
- Insider purchases come from OpenInsider's screener (SEC EDGAR refuses requests without a contact address).
- Earnings dates come from Nasdaq's calendar. A name due to report after today's close is left out as a news move.
- Verdicts: take or pass, a direction on every one, conviction, rank, thesis and a dilution note; a take adds a
  time limit of 1 to 5 sessions, a stop of 2% to 15% on the stock, and `wrong_if`.
- Scoring (`fr smallcap score`): from the session's open, against IWM, at holds of 1 to 5 sessions, before costs.
- Live passes (2026-11-20): the screen projects today's volume to a full day before testing it; the verdicts file
  is `verdicts/<session>-<pass>.json` (the night pass keeps `verdicts/<session>.json`); a live entry's stop on its
  first day can only be hit by the close.
- Dashboard: Portfolio → Small-cap book shows the shadow book the way the swing book is shown, every take held
  against the same dollars of IWM. Baskets → Small caps groups the universe by Nasdaq industry (an industry with
  fewer than four names goes into "Other <sector>"), 52 baskets against IWM, with the same table and heat map as
  the themed baskets (`smallcap/baskets.py`).
- First check: three weeks after the first recorded night (about 2026-12-11).

## Screen v2: filings first (built 2026-11-20)

From the research in `reports/Small cap screen design.md`: moves with identifiable news tend to continue and moves
without it tend to fade, and no technical signal covers a 1% round trip, so names now come in mainly because
something was disclosed. Four routes, one ranked list (`smallcap/screen.py`, `smallcap/edgar.py`):

| Tier | Route | Rule |
|---|---|---|
| A | filing | since the last session's close: an initial Schedule 13D, NT 10-K / NT 10-Q, or an 8-K item 1.03, 2.04, 2.05, 2.06, 3.01 or 4.02 |
| A | insider buying | Form 4 code P by officers or directors within 5 sessions: one buyer $50,000+, or two or more $25,000+ together; one filed since the last close |
| B | earnings | reported since the last close, however the stock moved (Nasdaq's calendar plus earnings 8-Ks, item 2.02); the night pass includes after-close reports already filed |
| C | price move | no report; 5% beyond the beta-implied move (beta to IWM over 120 sessions, pulled a third toward 1), 1.0 typical range, 1.5x volume |

- Within a tier, by the move's size in typical ranges; up to 20. Shown to the judge in a shuffled order; the screen's
  own order is kept as `screen_order` and on each verdict as `screen_rank`.
- A name judged in the last three sessions comes back only if a filing or report arrived after its verdict.
- Each candidate carries base rates from the own-data studies (new-high fade, extended-drop follow-through, the
  no-news up-move fade, the overnight-then-intraday giveback, no earnings drift), shown only on big-move days.
- A price-move verdict must carry `news`: `new`, `stale` or `none`. `fr smallcap score` splits by it and by screen
  version (each verdict carries `screen_version`), with the old bar kept as the `old_news_bar` tag.
- Data: SEC EDGAR, with the owner's contact in `.env` as `SEC_USER_AGENT` (never printed or published), about 370
  requests a pass at 8 a second with retries; OpenInsider only if EDGAR is down. The weekly universe rebuild also
  drops REITs, banks, pharma, blank-check shells and registered funds by the SEC's industry codes and form types.
- Not built: the analyst route (IBKR's free Briefing Analyst Actions feed), borrow status for shorts, and
  time-of-day volume curves. First dry run (2026-11-20 night): 13 candidates against 1 at the midday pass.

## Open questions

- The spread limit's final number.
- Whether to trade only names with two analysts or fewer (about 150 names), or just record the count.

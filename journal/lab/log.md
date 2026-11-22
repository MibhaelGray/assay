# Research lab log

One entry per research session, newest last. What was run, the numbers that matter, what it means, what to try next. Sharpe figures are after costs unless they say "before costs"; "discovery" is 2010-2021 and "validation" 2022-2024.

## 2026-11-22, 1:00 AM CT: first session (with the owner's session, by hand)

**Set up.** Prices for 3,301 US stocks and 40 funds from 2010, earnings reports from Nasdaq's calendar (217,549 reports), FINRA daily short-sale volume from August 2018. The engine was checked against a by-hand calculation of the one-day reversal spread and agrees.

**Run: 18 ideas, 141 variants. No candidates.** Nothing passed discovery, so nothing went on to the holdout.

What died, and why it matters:

- **Every plain price-and-volume idea in liquid stocks.** Reversal (plain, on high-VIX days, on quiet volume, without earnings names), new highs, big-news-day follow-through, RSI dips, sector leaders, overnight persistence, weekday seasonality. Several have a real before-cost return (reversal without earnings names 0.68, weekday seasonality 0.35-0.5, short-sale pressure 0.7-1.05) but it is 3 to 6 basis points a round trip against a cost of 8 to 25. The edge in these is smaller than the spread. This agrees with the small-cap study of 2026-11-20.
- **Following the earnings reaction.** Dead in every form, including when the surprise and the reaction agree. Before costs it is close to zero, which is not what the literature says; the likeliest reason is that half the reports have no surprise figure and the reaction-day rule is crude. Worth one sharper retest (backlog), not more.
- **Index timing rules.** Turn of the month, option-expiry week, buying fear, overnight-only: 0.3 to 0.95 on discovery, none holding up on validation in the same form. They are mostly ways of being long the market part of the time.

What is alive, in order:

1. **Owning high-attention stocks over their earnings report, hedged with SPY** (`earn_premium:over_2d_high_volume_names`). In at the close before the report date, out two closes later, only names whose past reports drew the most extra volume. Discovery 0.76 (before costs 1.24), up in 10 of 12 years; validation 0.82, up in all three years. About 7 names a day, 15 bp a round trip after costs. Under the bar on discovery (needs 1.0, and 0.29 with costs doubled says it is cost-sensitive). It leans on knowing the report date a day ahead, which is true in practice (dates are confirmed weeks out) but the calendar here records the date as it happened.
2. **Daily short-sale pressure** (`short_pressure:fade_*`). The informed-seller story wins over the market-maker story: stocks with unusually heavy shorting underperform over the next 1-5 sessions, before-cost Sharpe 0.7 to 1.05 on 2018-2021 and 0.45 on 2022-2024. As a daily long-short book it turns over far too fast to pay. It may be worth more as a slow filter than as a trade.
3. **Month end, shares against bonds** (`month_end_rebalance`). 0.3-0.6 on discovery, 0.8-1.7 on validation. Too few trades a year to be sure of anything.

**For the owner.**

- Costs decide almost everything here. The lab charges half a spread each way because the book's rule is to hit the bid or ask. Orders in the opening and closing auctions pay no spread. If auction orders are allowed, ideas 1 and 2 above roughly double after costs, and the whole class of close-to-close ideas gets cheaper. This is the single decision that would change the most results.
- The history has only companies that still trade. Before any candidate is believed it should be rerun on data that includes dead companies.

**Next.** See the backlog queue. First: sharpen the earnings-report premium (the one idea with the same sign in 13 of 15 years), then index additions, then Japan.

## 2026-11-22, 1:30 AM CT: second session

**CANDIDATE: `month_end_bonds:ief_last3`.** Own the 7-10 year Treasury fund (IEF) from the close four sessions before month end to the month's last close. It passes discovery and validation. Its luck haircut is 0.925, under the 0.95 bar. The holdout has not been opened and I have stopped working on this idea; the one look at 2025 on is the owner's (`uv run fr lab holdout month_end_bonds ief_last3`).

**Run: 5 ideas, 30 variants.** One candidate, one faded, 7 weak, 21 dead. The ledger also gained 10 trials of `rev_resid@jp` at 1:20 AM from another session; they are not mine, they are all dead on the board, and they count toward the haircut. The ledger stands at 181 variants.

### 1. `month_end_bonds` (6 variants): candidate, with caveats

I added this direction to the backlog before running it. The reason: bond indexes are rebuilt at the month's last close and index funds must buy long-dated Treasuries then, so the buying is forced and dated (Hartley and Schwarz 2019).

| Variant | Discovery | Before costs | t | Costs doubled | Years up | Validation | Verdict |
|---|---|---|---|---|---|---|---|
| IEF, last 3 sessions | 1.19 | 1.30 | 4.22 | 1.08 | 12 of 12 | 0.94 | candidate |
| TLT, last 3 sessions | 1.03 | 1.08 | 3.69 | 0.98 | 12 of 12 | 0.55 | faded |
| TLT, last 5 sessions | 0.98 | 1.01 | 3.47 | 0.94 | 11 of 12 | 0.34 | weak |
| TLT, last session only | 0.84 | 0.92 | 2.90 | 0.76 | 10 of 12 | -0.06 | dead |
| Short TLT, first 3 sessions of the month | 0.50 | 0.55 | 1.75 | 0.45 | 8 of 12 | -0.28 | dead |
| TLT, every other day (the control) | 0.16 | 0.18 | 0.57 | 0.14 | 5 of 12 | -0.91 | dead |

What supports it:

- It is not just owning bonds. On discovery TLT earned 5.2% a year from the last three sessions of each month (14% of the days) and 2.2% a year from all the other days. On validation the other days lost 15.5% a year (2022 alone lost 34%) while the last three sessions made 3.2% a year, up in each of the three years.
- IEF was up in every one of the 15 years. Validation by year: 4.2%, 1.2%, 3.1%.
- It costs almost nothing to trade: one fund, 12 round trips a year, 1 bp each way. Doubling the cost takes 1.19 to 1.08.
- Funds have no missing-company problem, so the survivor caveat on the stock ideas does not apply.

What a sceptic says:

- The haircut is 0.925, under the bar. With 181 variants tried, by my arithmetic it takes a discovery Sharpe of roughly 1.3 to clear 0.95; this has 1.19.
- Validation is weaker than discovery in every form. TLT fell from 1.03 to 0.55 and the last-session-only version went to zero. The paper is from 2019 and the effect may be shrinking. The validation t for IEF is 1.67: three years cannot confirm anything alone.
- Two of six variants passed discovery, and IEF passed validation where TLT did not. I did not choose IEF over TLT in advance.
- It is small. IEF made 23 bp a round trip before costs, 2.6% a year on the money used, with one position for 36 sessions a year. It would need Treasury futures or borrowing to matter, and that is untested.
- The reverse trade (short in the first three sessions of the new month) does not work, so the story of buying pressure that then lifts is only half there.

### 2. The earnings-report premium, sharpened (`earn_premium_sharp`, 6 variants; `earn_premium_window`, 4 variants): still weak, not a candidate

The trade is the lead from the first session: own high-attention names over their report, hedged with SPY.

| Variant | Discovery | Before costs | t | Costs doubled | Validation |
|---|---|---|---|---|---|
| The lead (first session): in the close before, out two closes later | 0.76 | 1.24 | 2.51 | 0.29 | 0.82 |
| (a) Names trading $50M a day or more | 0.46 | 0.69 | 1.53 | 0.23 | 0.54 |
| (a) Names trading $200M or more | 0.53 | 0.62 | 1.76 | 0.44 | 0.66 |
| (a) The other half, $10M to $50M | 0.70 | 1.20 | 2.41 | 0.21 | 0.71 |
| (c) Skip names up over 10% in the week before | 0.78 | 1.26 | 2.57 | 0.30 | 1.00 |
| (a) and (c) together | 0.42 | 0.65 | 1.39 | 0.19 | 0.69 |
| (b) Enter one session earlier | 0.86 | 1.27 | 2.75 | 0.44 | 0.81 |
| Enter three sessions before the report date | 0.93 | 1.30 | 2.93 | 0.56 | 0.83 |
| Enter four sessions before | 0.88 | 1.21 | 2.79 | 0.55 | 0.72 |
| Only the session before the report | -0.78 | 0.75 | -2.57 | -2.28 | -0.44 |
| Only the two sessions before | -0.04 | 0.85 | -0.13 | -0.93 | -0.03 |

What it shows:

- **Liquidity does not rescue it.** The premium shrinks with the cost. Before costs it is 1.20 in the less traded half and 0.69 in the more traded half; after costs they are 0.70 and 0.46. That fits the story (the premium is larger where shorting is harder) and it means there is no cheap corner to move to.
- **The run-up filter does nothing.** It removes about 3% of the trades and discovery moves from 0.76 to 0.78.
- **Entering earlier helps a little, and less than it looks.** The days before the report earn about 5 bp a day before costs, too little to trade alone and worth having once the round trip is paid. Entering three sessions ahead gives 0.93 with a t of 2.93, the closest this trade has come. But 2020 supplies 13.75 points of that variant's 31 points of discovery return, and in the two before-the-report-only variants 2020 made 13% and 16% while no other year made more than 0.3%. Most of the gain from the earlier entry is one year in which high-attention names beat SPY week after week.
- **The premium itself is steady.** All eight variants that hold through the report are positive after costs in both periods: 0.42 to 0.93 on discovery, 0.54 to 1.00 on validation.

Conclusion: this is one result, a premium worth about 0.8 to 0.9 after costs, under the bar on Sharpe and t in every form, with a haircut no better than 0.68. Seventeen variants have now been run on it. I do not treat the three-session entry as an improvement to carry forward, and more sharpening would be a settings hunt. The only thing left that would change it is cost: both ends of the trade are closes, and before costs it is 1.2 to 1.3.

### 3. `short_slow` (6 variants): dead

Discovery here is only November 2018 to 2021, so a pass was never possible; the run could only kill the direction or keep it.

- The 20-day average of the short share against the stock's own norm has no before-cost return: 0.04, or 1 bp a round trip. The 10-day average has 0.28 (4 bp). In the 300 most traded names it is -0.03, and the short side alone is -0.15.
- Shorting only the most extreme single days (three deviations over the stock's norm) earns 0.1 bp a round trip before costs.
- The plain 20-day level across stocks is the only positive one: 0.50 before costs and 0.13 after on discovery, 0.67 after costs on validation. A slow level sorted across stocks is as likely to be a trait of the stock (size, sector, volatility) as a flow, and three years is too short to tell.

Conclusion: whatever a day of heavy shorting knows is in the price within about a week. Averaging the signal removes it, and the daily signal costs more to trade than it earns. A filter built from the slow versions would have nothing in it, so I did not test one.

### 4. `div_runup` (8 variants): dead

Ex-dates and amounts were read from the price history (the step between the total-return close and the split-only close). The count is about 8.5 ex-dates a day in the universe, which is what regular quarterly payers should give.

- Owning payers into the ex-date, hedged with SPY, loses before costs: -0.32 for the three sessions through the date, -0.47 for the three sessions before it, -0.66 in the high-yield names where the effect should be largest.
- The ex-day alone earns 1.0 bp a round trip before costs (0.32) against a cost of about 6.
- Shorting for three sessions after the date earns 0.43 before costs and -0.47 after.
- Long before the date and short after it, which takes out the tilt toward dividend payers, earns 0.06 before costs.

Conclusion: the losing long legs and the winning short leg are dividend payers lagging SPY through 2010-2021, not anything about the date. With that removed there is no run-up and no fall in liquid US names since 2010. On validation the long-short version is 0.60 before costs and -0.57 after, and no variant is positive after costs in either period.

**For the owner.**

- **The holdout look for `month_end_bonds:ief_last3` is yours to take or leave.** It gets one look. The case for taking it: the trade is forced, dated, cheap and has no survivor problem. The case against: the haircut is under the bar, and I added and ran the idea in the same session. If it holds, it still needs 20 sessions on paper, and a decision on how it would be sized, since 2.6% a year unlevered is not worth a slot.
- **Auction orders** now decide the earnings premium and nothing else is likely to. Entry and exit are both at the close. Before costs the trade is 1.2 to 1.3; closing-auction orders would not make the cost zero (commission, and the auction can move against a known buyer), so the honest test is a cost setting for auction orders that you agree to, then one rerun.
- I found nothing wrong in the engine.

**Next.** See the backlog. The one thing that passed is a forced, dated flow in a fund that costs 1 bp to trade, and everything in single stocks died on costs again. So the queue now leads with other flows of that kind: Treasury auction days, index changes, the Fed. Month end in other bond funds (LQD, HYG) waits until after the holdout look, so that the candidate's test stays clean.

## 2026-11-22, 1:45 AM CT: notes from the owner's session (by hand)

**Tokyo is built as a second market** (`jp`: about 1,670 TOPIX names from JPX's list, prices from 2010 in yen, dollars traded worked out at each day's exchange rate, hedge the TOPIX fund 1306, IBKR's commission of about 6 bp a side added to every trade). Any idea file runs there as `<idea>@jp`.

**`rev_resid@jp` (10 variants): dead after costs, strong before.** The one-day reversal entered at the next open earns a before-cost Sharpe of 1.95 in Tokyo against 0.32 in the US, and the three-session version 0.86 against 0.47. After the spread and the commission every variant is below -1.3. So the effect the literature describes is there and is several times the US one; it is a cost problem, not a signal problem. The `top300` variant printed the same numbers as the base one because only about 225 Tokyo names trade $20M a day, so it was not a separate test.

**A caution on `month_end_bonds:ief_last3` beyond the ones in the entry above.** The first session had already run `month_end_rebalance` (SPY against TLT at month end) and its log said validation was strong (0.8 to 1.7). The second session read that log before it wrote `month_end_bonds`. So the validation years were not unseen for this idea: part of what it shows had been looked at. That makes the holdout and a forward run on paper the only clean tests it has left.

**The unattended session worked as built:** 9.9 minutes, 30 variants of its own, one command refused (a pipe into python, correctly), log and backlog written. It sent the candidate alert to the phone at about 1:25 AM.


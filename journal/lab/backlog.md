# Research lab backlog

The queue of directions for the research lab (docs/research-lab.md). Sessions take from the top of the queue, move what they ran to the done table, and add follow-ups. Anyone may add a direction; say why it should work.

## Queue (best first)

**Foreign markets only, from 2026-11-22.** The owner asked that the lab look only outside the US, and only at markets IBKR opens to a US resident. Built: `jp` Tokyo, `hk` Hong Kong, `kr` Korea, `tw` Taiwan, `au` Australia, `ca` Canada, `uk` London (`panel.MARKETS`; run any idea file on one as `<idea>@<market>`). Open at IBKR but not built yet: Euronext, Germany, Switzerland, China A-shares through Stock Connect, Saudi. India is closed to US residents: never build it. IBKR allows shorting in Japan, Hong Kong, the UK, Canada and Australia only; in Korea and Taiwan test long-only or long against the index fund (the hedge then stands for an index future). Costs differ a lot: Canada 2 bp a side, Tokyo 6, Australia 8, Hong Kong 16, Korea 18, Taiwan 25, London 30 (stamp duty), so prefer trades held longer or a cheap market.

1. **Reversal across the built markets.** The US had almost no one-day reversal (0.32 before costs); Tokyo had 1.95. Run `rev_resid` on `hk`, `kr`, `tw`, `au`, `ca` as it stands (no changes, so it is one clean question per market) and see where the before-cost return is largest relative to that market's cost. Then one follow-up with a reason where it is close: fewer, bigger trades (largest moves only, top names by turnover) to cut cost.
2. **Price-limit markets.** Taiwan's daily limit is 10% and Korea's 30%. A stock that closes at its limit had more buyers than the limit let through, so part of the move is delayed to the next session (the "magnet" and spill-over effect studied in Taiwan). Test buying limit-up closes (and, in Taiwan, avoiding limit-down) for 1-2 sessions, long-only hedged with the index fund. Nothing to fetch: the limit is the prior close times 1.10.
3. **Asia after the US close.** Tokyo, Hong Kong, Seoul and Taipei open after New York closes. The gap at the open prices the US move; test whether the rest of the day (open to close) continues it, by sector, against each market's own fund. Needs the US panel read inside a foreign idea (both panels load the same way).
4. **Index reviews abroad.** Hang Seng (quarterly), ASX 200 (quarterly), KOSPI 200 (June and December), FTSE 100/250 (quarterly), Nikkei 225 and TOPIX reviews are announced ahead and index funds trade at the effective close. A forced, dated flow of the kind that passed in US bonds. Needs the dates as a source file (each index provider's announcements; Wikipedia's change lists give effective dates for some).
5. **Japanese dividend record dates.** Most Tokyo companies have a March or September year end, so record dates cluster on two days a year; index funds reinvest the dividends they are owed through futures around the ex-date. Test the sessions around the end of March and September in Tokyo, and the reinvestment flow on and after the ex-date.
6. **Foreign investor flows in Taiwan and Korea.** TWSE and KRX publish each day's net buying by foreign institutions for every stock (free). Foreign buying in these markets is large and persistent, and locals follow it. Test buying the names with the heaviest foreign net buying for 1-5 sessions. Source file needed: TWSE's daily "three major institutional investors" table (one download a day, from 2012).
7. **Hong Kong southbound flows.** Mainland investors buy Hong Kong shares through Stock Connect, and HKEX publishes each name's Connect holdings each day. Mainland money tends to chase; test whether rising Connect holdings lead over 1-5 sessions. Source: HKEX's daily shareholding disclosures (free).
8. **Tokyo reversal at lower cost.** `rev_resid@jp` is strong before costs. One version: the 225 most traded names, only their largest moves, held 3-5 sessions so cost is paid less often.
9. **Earnings reports abroad.** The US earnings premium was weak but real before costs. Australia's reporting season (February and August) and Tokyo's (late April-May, late October-November) are concentrated on few days. Needs report dates; no free calendar found yet, so note in "Needs the owner" if none turns up.
10. **London: short-side ideas only.** Stamp duty is paid on purchases, so anything that buys often loses 50 bp a round trip. A short-led idea (fading the FTSE 250's biggest daily rises, hedged with the FTSE fund) pays only commission on the short leg.

## Parked: US directions

Kept for the record, not to be worked while the lab is foreign-only. The US candidate's holdout look (`month_end_bonds:ief_last3`) stays with the owner under "Needs the owner".

1. **Treasury auction days.** Dealers must take down each 10-year and 30-year auction and sell or hedge ahead of it, so yields tend to rise into the auction and fall after it (Lou, Yan, Zhang 2013). The dates are announced ahead. Source needed: auction dates from TreasuryDirect's auction data (free, a plain download; one source file). Test short IEF or TLT for the 2-3 sessions before a 10-year or 30-year auction and long for the 2-3 after, with all other days as the control. Month-end sessions overlap with `month_end_bonds`; leave them out so this is a separate test.
2. **Index additions and deletions.** S&P 500, 400 and 600 changes are announced several days before they take effect and index funds must buy at the effective close. Wikipedia's change tables give the effective date only, not the announcement date, so a test that enters "after the announcement" from those tables would be buying before some announcements. Either get the announcement dates (S&P's press releases) or test only the leg that needs none: the last two sessions into the effective close, and the reverse for the three sessions after it.
3. **Before the Fed.** The index has earned most of its excess return in the 24 hours before scheduled FOMC announcements (Lucca and Moench 2015); later studies say it faded after 2015. Needs the meeting dates as a small source file (public, fixed a year ahead; the Fed's calendar pages). Test SPY from the close before to the close of the day, and IEF over the same window.
4. **Japan from the US close.** Tokyo opens after New York closes. Test whether US sector moves predict the same sector in Tokyo from the open to the close (not the gap, which is priced). The `jp` market is built (2026-11-22: about 1,670 TOPIX names, hedge 1306, costs include IBKR's 6 bp commission a side); any idea file runs there as `uv run fr lab run <idea>@jp`. This one needs US sector returns read into a Tokyo idea, so it needs both panels. Japan is shortable at IBKR (862 names on the borrow file). Another session ran `rev_resid@jp` at 1:20 AM on 2026-11-22, so a `jp` market may already be built.
5. **Reversal in Hong Kong, and Tokyo at lower cost.** `rev_resid@jp` is dead after costs but strong before them (one-day reversal 1.95 before costs, against 0.32 in the US). Hong Kong needs a market entry (symbols, `.HK`, hedge 2800.HK). For Tokyo the open question is cost, not signal: a version that trades only the 225 most traded names' largest moves, so fewer and bigger trades, is the one follow-up with a reason.
6. **Month end in other bond funds.** Corporate and high-yield bond indexes are rebuilt at month end the same way as the Treasury index. LQD and HYG are in the panel. **Wait for the owner's holdout look at `month_end_bonds` first**, so the candidate's test is not followed by a search through its neighbours.
7. **Secondary offerings.** A stock that prices an overnight share offering opens down and often recovers part of it over the following days as the selling pressure clears. Source: SEC EDGAR 424B filings (the small-cap book's `edgar.py` already talks to EDGAR).
8. **Insider buying clusters, mid caps.** The small-cap study found officer and director purchases beat IWM by 55-110 bp over 1-5 sessions, about zero after a 1% cost, with two-or-more-insiders the best cut. Costs are a fifth of that in $1B-$10B names; test there.
9. **Month end, shares against bonds, sharpened.** `month_end_rebalance` was weak on discovery (0.3-0.6) and strong on validation (0.8-1.7). One follow-up with a reason: quarter ends only, where the rebalancing is largest.
10. **Sector funds after a breadth extreme.** When nearly every stock in a sector closes down on the day, buy the sector fund for 1-3 sessions. Built from the panel's own stocks and sector labels.
11. **The earnings-report premium: parked.** Seventeen variants across three ideas give one answer: in high-attention names held through the report, 0.76 to 0.93 on discovery and 0.72 to 1.00 on validation (0.42 to 0.53 in the most traded names), under the bar in every form. Liquidity, a run-up filter and earlier entry have been tried. Do not sharpen it further. Reopen only if the owner sets a cost for closing-auction orders (below): then one rerun of `earn_premium:over_2d_high_volume_names` at that cost, nothing else changed.
12. **The level of short selling across stocks.** `short_slow:level20` (lightly shorted names over heavily shorted ones, 20-day level) was 0.50 before costs and 0.13 after on 2018-2021, 0.67 after costs on 2022-2024. Probably a trait of the stock, not a flow. One test would settle it: the same sort within sector and size groups. Low priority; three years of discovery cannot pass the bar.
13. **Gaps at the open.** A large overnight gap with no earnings report behind it, faded or followed from the open to the close. Needs an engine entry at the same day's open, which the owner has to approve (it assumes trading at the opening print a moment after seeing it).
14. **52-week lows that hold.** `breakout`'s short side and `news_day_drift` were dead; skip unless a new reason turns up.

## Needs the owner

- **The holdout look for `month_end_bonds:ief_last3`** (candidate, 2026-11-22). Discovery 1.19, validation 0.94, luck haircut 0.925 against a bar of 0.95. One look for the idea: `uv run fr lab holdout month_end_bonds ief_last3`. If it is taken and passes, the next decisions are sizing (2.6% a year unlevered; Treasury futures would be the tool) and a 20-session shadow run.
- **Auction orders.** The lab charges half a spread each way because the book's rule is to hit the bid or ask. Orders in the opening and closing auctions pay no spread, which would roughly double the after-cost return of the close-to-close ideas. Allowed or not? After the second session this decides the earnings-report premium (before costs 1.2 to 1.3, after costs 0.76 to 0.93). What is needed is a cost figure for an auction order that the owner agrees to, not zero.

- **History with delisted companies.** Yahoo has only survivors. Before any candidate is believed it should be rerun on data that includes dead companies (Norgate Data, about $630 a year, or Sharadar through Nasdaq Data Link). A purchase, so the owner's call; not needed until there is a candidate.
- **An entry at the same day's open** for gap ideas (item 8).
- **Intraday bars** (30 or 60 minute, several years) for anything that enters mid-session. IBKR's gateway can supply them slowly; Polygon and others sell them.

## Done

| Date | Idea | Result |
|---|---|---|
| 2026-11-22 | `rev_resid` plain residual reversal, top 1,000 names | Dead. Before costs Sharpe 0.3-0.5 at 3-5 sessions, about 3-6 bp a trade; costs are 8-12 bp. One-day version has no before-cost return at all. |
| 2026-11-22 | `rev_vix` reversal only when the VIX is high | Dead. Better than plain (0.45 with VIX over 25) but far under the bar; validation flat. |
| 2026-11-22 | `rev_volume` quiet moves fade, loud moves follow | Dead, both halves. |
| 2026-11-22 | `overnight_persist` stocks that rise overnight | Dead. Turns the book over daily; cost is many times the return. |
| 2026-11-22 | `news_day_drift` follow a huge move on huge volume | Dead in both directions, at every hold. |
| 2026-11-22 | `breakout` first new high on volume | Dead. |
| 2026-11-22 | `rsi2_pullback` dip in an uptrend | Dead. |
| 2026-11-22 | `sector_lead` small names follow sector leaders | Dead after costs (Sharpe below -1.5). |
| 2026-11-22 | `etf_rev` sector fund reversal | Dead: no before-cost return. |
| 2026-11-22 | `turn_of_month` | Weak. SPY from 3 days before to 3 after month end: 0.68 discovery, 0.66 validation, but it is mostly being long the market. |
| 2026-11-22 | `overnight_index` | Weak. QQQ and IWM overnight 0.7 on discovery, 0.1-0.4 on validation; half the market's risk, and one basis point more cost erases it. |
| 2026-11-22 | `vix_fear_buy` | Weak. Buying when VIX is over VIX3M: 0.3 discovery, 1.1 validation. Buying when calm: 0.95 discovery, 0.33 validation. No consistent rule. |
| 2026-11-22 | `month_end_rebalance` | Weak, see queue item 11. |
| 2026-11-22 | `weekday_season` | Dead after costs. Before costs 0.35-0.5, so the effect exists and is far too small. |
| 2026-11-22 | `opex_week` | Dead. |
| 2026-11-22 | `earn_drift` follow the earnings reaction | Dead in every form; before costs near zero. |
| 2026-11-22 | `earn_premium` own stocks over the report | Weak, and the best lead: high-attention names 0.76 discovery, 0.82 validation. See queue item 1. |
| 2026-11-22 | `rev_no_news` reversal without earnings names | Dead after costs; 0.68 before costs. |
| 2026-11-22 | `short_pressure` FINRA daily short share | Dead as a daily book; fading heavy shorting is 0.7-1.05 before costs. See queue item 2. |
| 2026-11-22 | `short_market` market-wide short share to time SPY | Dead: 0.7-0.9 on 2018-2021, negative on 2022-2024. |
| 2026-11-22 | `earn_premium_sharp` the earnings premium in liquid names, without run-ups, entered a session earlier | Weak. Liquid names 0.46 (the premium shrinks with the cost); run-up filter no change (0.78); one session earlier 0.86 discovery, 0.81 validation. |
| 2026-11-22 | `earn_premium_window` the days before the report alone, and entry 3 or 4 sessions ahead | Weak. Days before alone lose after costs (0.75-0.85 before). Entry 3 sessions ahead 0.93 discovery (t 2.93), 0.83 validation, but 2020 is 45% of the discovery return. Parked, see queue item 11. |
| 2026-11-22 | `short_slow` 10- and 20-day short-sale pressure, held 5 sessions | Dead. The slow versions have no before-cost return (0.04 at 20 days, 0.28 at 10); extreme single days earn nothing either. |
| 2026-11-22 | `div_runup` own dividend payers into the ex-date, short after | Dead. Before costs: -0.32 into the date, 1 bp on the ex-day, 0.06 for long-before against short-after. |
| 2026-11-22 | `month_end_bonds` Treasuries over the last sessions of the month | **Candidate: `ief_last3`** (1.19 discovery, 0.94 validation, haircut 0.925). TLT last 3 faded (1.03, 0.55). Holdout not opened; see "Needs the owner". |
| 2026-11-22 | `rev_resid@jp` reversal in Tokyo | Dead after costs (below -1.3 in every variant); before costs 0.57 to 1.95, several times the US figure. See queue item 5. |

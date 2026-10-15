# Pre-registered tests at 13 Nov: does consistent weekly profit predict the next month, and does the big-profit rule hide Rising traders? (written 2026-10-15, before any data for these tests was collected)

## Test 1: the question

Wallets profitable in most of their active weeks over the past 12 weeks: are they more likely than other wallets
to make money next month? And is that money from new trades, not only from positions they already held?

The answer decides whether Signals may show a factual **"consistently profitable"** label as a reason to look
at a trader. The owner approved this as research only (15 Oct): nothing in Signals or the SDK changes because of
this test. A pass lets the owner *consider* the label. It never makes a trader "skilled", "copyable" or
recommended.

## Why this test (already seen: exploratory, not this test)

`worth_research.py` (15 Oct, scratch analysis, not on main) tested about a dozen signals other than the 5-minute
edge. Pools were the existing research files (`scoring79-scores`, `overlooked-scores`, `selective-bets`), plus a
top-up of 650 public `/v2/user-pnl` reads. Wallets already in a recommended tier were excluded. The test
selected on the earlier window, judged on the later one, and applied BH across 143 tests.

The only signal that held in more than one pool was this one: ≥ 2/3 of active weeks profitable over the 12 weeks
before selection.

| Pool (exploratory) | Selected / eligible | Next-period profit > 0, selected vs rest | BH q | Next-month 5-min edge, selected |
|---|---|---|---|---|
| Volume board (A 17 Aug–16 Sep, B to 13 Oct) | 37 / 109 | 68% vs 35% (+33pp, 95% +14 to +52) | 0.024 | +1.27¢ (+0.53 to +2.03) |
| Overlooked (A to 12 Sep, B to 12 Oct) | 26 / 132 | 58% vs 20% (+38pp, +17 to +58) | 0.012 | −0.15¢ (−0.86 to +0.73): none |
| Selective, 52 weeks before 14 Jul, next 91 days | 55 / 172 | 65% vs 31% (+35pp, +19 to +50) | 0.012 | — (settled excess +1.4¢ vs +0.9¢: none) |
| … selective, only wallets still trading in O | 25 / 82 | 60% vs 35% (+25pp, +2 to +48) | 0.30 | — |
| Frequent, 12 or 52 weeks | 26 / 94, 20 / 104 | +9pp, +13pp | 0.57–0.72 | — |

Why that isn't enough:

1. **Thresholds were chosen with earlier results partly in view.** The analysis wasn't pre-registered.
2. **The groups are small** (20–55 wallets).
3. **There is one selection period per pool.**
4. **Part of the selective result is carry.**
   - Wallets that didn't trade in O at all still showed O profit, from positions they already held.
   - Plain "profitable over S" in the selective band was driven entirely by wallets not trading in O: 56% vs 26%.
5. **It overlaps plain profit.** Among wallets already profitable, the consistent ones did better next period
   in every pool (+18 to +28pp), but none of those three comparisons was significant on its own.

None of those wallets, and none of those windows, are reused here.

## Dates (fixed)

- **T** = 2026-11-13 00:00 UTC.
  - This is the day the scheduled fix-8 routine (`scoring79_followup.py`) runs. That routine is separate and
    unchanged by this test.
- **Selection:** everything is measured on data before T. The 12 weeks before T run 2026-08-21 to T.
- **Outcome B** (volume board, overlooked): T to T + 30 d, ending 2026-12-13 00:00 UTC.
- **Outcome O** (selective): T to T + 91 d, ending 2027-02-12 00:00 UTC.
- **Snapshot at T.** Pools are drawn, P&L histories read and tiers scored between T and T + 24 h, then saved
  before any outcome exists.
  - If that slips, T moves to the actual draw day.
  - The move is recorded as an amendment before any outcome is read.
- **Outcome reads:**
  - B: within 7 days after B ends.
  - O: within 7 days after O ends.
  - 5-minute prices are only kept for ~60 days, so B's 5-minute scoring can't wait longer.

## Pools (fresh draws, no hindsight)

None of the 200 + 310 + 400 wallets already analysed are eligible. Every pool definition below uses only data
available at T.

| Pool | Draw at T | Size drawn | Notes |
|---|---|---|---|
| **V: volume board** | Random draw (seed 1113) from Polymarket's all-time *volume* top 1,000 | 400 | Volume measures activity, not outcome. Profit boards are never read for selection. |
| **U: overlooked** | On no leaderboard read at T (the same boards as `overlooked_candidates.py`); seen in 3+ of 300 open markets sampled across volume bands; median trade $5–$2,000 | 300 | Found from trades *before* T, so unlike the earlier pool it doesn't require activity in B. Stopping in B is reported. |
| **S: selective** | Wallets in the last 500 trades of 240 markets that closed in the 90 days before T (60 per volume band) | 300 | 1–7 markets bought in the 30 days before T, ≥ 20 single markets in the 365 days before T, and a buy in the 90 days before T. Same classification rules (and 10-page cap) as `selective-preregistered.md`. |

## The signal (fixed)

**Profit curve:**
- Source: Polymarket's `/v2/user-pnl` (`interval=all`), one read per wallet at T.
- Trading profit at a time t = `realized_pnl + unrealized_pnl` at the last point at or before t. This is the
  SDK's `_trading`: rewards, rebates and yield are left out.
- Before the first point it is 0.

**Weeks:**
- The 12 steps of 7 days ending at T. This is the SDK's `known_steps(T − 84 d, T, 7 d)`, the same weeks as the
  `pnl_weeks` shown in Signals.
- A week that ends before the history starts is left out.
- **Active week:** |weekly change| > $0.50.

**CONSIST:**
- At least 6 active weeks, and
- at least 2/3 of the active weeks with a change > 0.

**Eligible (per pool):**
- ≥ 6 active weeks;
- not in a recommended tier at T;
- not `no_view` (market makers and arbitrage);
- V and U only: at least one buy in the 30 days before T.

**Tier at T:**
- Scored with uselayer at its version on T, `score(end=T, days=30, sample=60)`. The version is recorded.
- "Recommended" means proven, quiet or rising, or inactive from one of those (`RECOMMENDED`).
- `no_view` wallets are reported separately as a sensitivity check only.

**Selected** = eligible and CONSIST. **Rest** = eligible and not CONSIST.

**Controls, reported but not tested for the bar:**
- `PROFIT30`: trading profit over the 30 days before T > 0.
- **Within-profitable:** among wallets with `PROFIT30`, CONSIST vs not. This is the "does consistency add
  anything beyond profit" question.
- **Sensitivity:** thresholds ≥ 1/2 and ≥ 3/4, and a 52-week version, each reported. None of them can rescue a
  failed primary.

## Outcomes (fixed)

### Primary 1: account profit
- **Measure:** B trading profit > 0, i.e. the change in trading profit from T to the end of B (O for pool S),
  from a fresh `/v2/user-pnl` read.
- **Reported:** share of wallets (Wilson range), median, and the difference selected − rest.
- **Includes:** marks on positions already held at T.

### Primary 2: profit on new positions (carry handled)
- **Which positions count:** for each outcome token the wallet *first* bought after T (no buy, sell or
  holding of that token in the 365 days before T, from `/activity`).
- **New-position profit** = sells + redemptions + value at the end of B/O − buys.
  - Value = shares held × the last price before the end (1 or 0 if settled).
  - Parlays (`isCombo`) are counted at redemption only, unredeemed counted as 0, as in fix 9.
- **Outcome:** new-position profit > 0, among wallets with at least one new position.
- **Also reported:** the share of wallets with no new position (not trading).
- **Why:** this separates "keeps making money on new trades" from "their book kept paying".

### Secondary (each reported on its own; never part of the bar)
- **5-minute edge in B** (pools V and U):
  - pooled per independent event, from `score(end=B_end, days=30, sample=60)`;
  - range from a bootstrap over wallets;
  - absolute level for the selected group, and the difference vs rest.
- **Settled excess:** payout − price per event, over bets placed in B/O that had settled by the read.
- **Still trading:** any buy in B/O.
- **Pool S only:**
  - the 12-hour move (as in `selective-preregistered.md`);
  - profit > 0 among wallets still trading in O.
- **Copy returns:** never computed for this test. The label is not about copying.

## Comparison and statistics (fixed)

- **Effect:** selected − rest of eligible, per pool. Pools are never pooled for the bar.
- **CI:** bootstrap over wallets, each group resampled on its own, 2,000 resamples, seed 1, 95% percentile
  range.
- **p:** permutation of the selected/rest labels over wallets, 2,000 shuffles, seed 1, two-sided.
- **Multiple testing:**
  - **Primary family:** Benjamini–Hochberg over the 6 primary tests (3 pools × primaries 1 and 2), q ≤ 0.10.
  - **Secondary family:** BH separately over all secondary tests. Reported, never used for the bar.
- **Minimum size:** a pool with fewer than 20 selected wallets with the outcome is "too few to judge" and
  counts as not passing.

## Success bar (decided now)

**Pool pass:** the pool passes when all of these hold:
1. Primary 1's difference has a 95% CI excluding 0 and BH q ≤ 0.10;
2. the selected group's B profit > 0 share is ≥ 50% (more likely than not, in absolute terms);
3. ≥ 20 selected wallets.

**Carry check:** in each passing pool, primary 2's difference must be > 0 (point estimate).
- If its CI also excludes 0 with q ≤ 0.10, the profit is attributable to new trades.
- If primary 2's difference is ≤ 0, that pool's pass is attributed to carry and **does not count**.

**Overall pass:** at least 2 of the 3 pools pass with the carry check.

## What a pass allows, and what a fail means

**Pass:**
- The owner may consider a factual label such as "Profitable in 9 of 12 active weeks".
  - The counts are always shown.
  - It is placed with the track record.
- **Never** "skilled", "sharp", "copyable", a tier, Recommended, or a Feed alert reason.
- If primary 2 met its CI bar too, the wording may say the profit continued on new trades. Otherwise it
  describes the account's profit only.
- If the 5-minute-edge secondary is positive in absolute terms (CI > 0) in pool V only, the page still may not
  imply skill. Skill stays the 5-minute test's job.

**Fail:**
- No label.
- Consistency stays a plain descriptor ("X of N weeks up", as now), with no claim about the future.

**Mixed** (one pool passes, or passes only through carry):
- No label.
- The result is written up, and any re-test needs its own pre-registration with a new T.

## Known limits

- **One selection date** and one 30-day (91-day) outcome.
  - Market conditions after 13 Nov (NFL/NBA season, no US election) may not resemble Aug–Oct.
- **Unrealized marks:**
  - Polymarket's P&L includes them.
  - Large accounts' weeks can swing on marks of a few big positions.
  - Primary 2 handles carry in B, but the selection weeks themselves include marks.
- **Reconstruction:** new-position profit is rebuilt from `/activity`.
  - Tokens traded before the 365-day lookback, or past the activity read cap, could be misclassified as new.
  - The count of capped wallets is reported.
- **Pool coverage:**
  - The volume board leans to large, long-lived accounts.
  - The overlooked pool comes from open-market trades, which favours frequent traders.
  - The selective pool comes from closed markets' last trades.
- **Survivors only:** wallets that stopped before T aren't in any pool.
- **Unequal trading:** "consistently profitable" can be passive (one long position marking up weekly). The
  share of selected wallets with ≤ 3 buys in the 12 weeks is reported.

## Test 2, separate: does the big-profit rule hide Rising traders?

Same T, same snapshot, its own family and bar. Nothing about test 1 depends on it.

**The question.** uselayer's `_segment` checks `big_profit` (all-time trading profit ≥ $50K, 8+ events, not a z ≥ 2
steady edge) *before* `rising`. So a big winner whose skill verdict is "early" (edge > 0 with z ≥ 1, profitable,
steady) can never be Rising, while a smaller account with the same verdict is.
- **History:** the order dates from the first SDK version, when that rule meant "lucky". uselayer-sdk #20 (0.10.0)
  renamed it `big_profit` ("shows neither skill nor luck") and kept the order without discussing Rising.
- **Production (14 Oct):** 30 such wallets. Median z 1.50, median 45 events, 28 of 30 in a board's top 100, all
  "not copyable". Moving the rule after Rising would make 29 of them Rising (22 → 51).
- **Exploratory, not this test:** in the volume-board pool, the 9 wallets the rule held back kept a positive 5-minute
  edge the next month, +1.08¢ (95% +0.49 to +1.75), 9 of 9. Today's Rising in that pool had 4 wallets.

**Groups, at T, each scored with `score(end=T, days=30, sample=60)`:**
- **SW (held back):** skill "early", all-time trading profit ≥ $50K, edge z < 2. Today's segment is `big_profit`.
- **R (Rising today):** segment `rising`.
- **N (the rest of big_profit):** segment `big_profit` with skill "no".

**Where they come from:**
- pool V above;
- plus **pool T**: 300 wallets drawn at random (seed 1114) from the top 100 of each of the day, week and month profit
  and volume boards (overall and each category) read at T. That is where 28 of the 30 live.
- Wallets in both pools count once. Pool T is used only for test 2.

**Outcome:** the B (T → T + 30 d) 5-minute edge, pooled per event, from `score(end=B_end, days=30, sample=60)`.
Also reported, outside the bar:
- the share that kept a positive edge;
- B trading profit > 0;
- B copy result, reported only, never a criterion.

**Bar (decided now).** SW passes when all of these hold:
1. at least 12 SW wallets have a B edge;
2. SW's B edge has a 95% CI (bootstrap over wallets, 2,000 resamples, seed 1) above 0;
3. SW − R has a 95% CI lower bound above −0.5¢ (non-inferiority: not clearly worse than Rising);
4. no more than a quarter of SW wallets had an A-window edge above 5¢. That is the thin-market artifact; such
   wallets are also reported apart.

**What a pass allows:** the owner may consider moving the `big_profit` check after `rising` in uselayer. `lucky` stays
first. Big winners whose skill is "no" or "unknown" stay `big_profit`. It changes Rising's size (and so
Recommended and the Feed's alert volume), which the owner weighs too.

**What a fail means:** the order stays, and its reason is written into the SDK's comment.

**Too few** (under 12 SW wallets): no change. It counts as "not tested", not as a fail.

## Amendments

(None yet. Any change after T is recorded here with its date and what had been seen.)

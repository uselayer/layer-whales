# Broader discovery: selective traders (proposal, 2026-10-13; no code changed)

**Goal:** find more good traders, including someone making 3–5 thoughtful trades a month, without recommending
anyone on weaker evidence. Discovery should be broad, evaluation honest about what it can measure, and
recommendation narrow.

## 1. Why discovery and scoring favor frequent traders

Ranked by how much they exclude a selective trader. Line numbers are Signals `main` and uselayer `main`
(5a77fc5).

1. **Selective wallets are never scored at all.**
   - The directory's background evaluation (`directory.py:218`, `quick_look`) skips any wallet with fewer
     than 8 markets bought in the last 30 days ("not enough recent evidence").
   - It keeps them and re-checks daily, but never evaluates them.
   - So, to the owner's question 2: yes, they are excluded before their performance is looked at.
2. **The skill test can't run on them.**
   - It needs 8 independent events with a 5-minute price inside a 30-day window (`RISING_BETS`).
   - At 3–5 trades a month that is impossible, however good they are. Their verdict is "unknown".
   - Sharps need 25 events, or 40 on a big account.
3. **A longer window doesn't help today's test.**
   - Polymarket keeps 5-minute prices for only about 60 days.
   - In the frequency study's pool (non-leaderboard wallets), a 90-day window gave **zero** extra priced events
     to all 300 selective and sparse wallets. Frequent wallets got *fewer* (535 → 366), because the 60-bet
     sample spread onto unpriced bets.
4. **The finders see frequent traders first.**
   - **Market-trade discovery** (`find_candidates`):
     - reads each market's last 500 trades, so the chance of being seen grows with how often you trade;
     - ranks by dollars × number of markets seen;
     - keeps 100.
   - **Leaderboard discovery:** reads several boards, but the monthly profit board fills all 100 slots.
     Being on a board raises the bet bar from 25 to 40.
5. **The pool is rebuilt every 6 hours.**
   - The Worth following check scores 200 wallets found afresh and replaces the list.
   - A selective trader has to be re-found every time to stay visible.
   - The directory, which does sweep slow markets, feeds nothing to the page.
6. **The "not trading lately" rule is strict for them.** It removes anyone with no buy in 7 days. A 3-a-month
   trader has a buy in any 7 days only about half the time.
7. **Ranking:** rows sort by edge z, and z grows with the square root of the number of bets.

## 2. How many traders this leaves out (wallets outside the leaderboards)

| Pool (no leaderboard, no hindsight) | Frequent (8+ markets / 30 d) | Selective (<8 / 30 d, 8+ / longer) | Fewer than 8 a year |
|---|---|---|---|
| Frequency study: 4,680 wallets from 240 closed markets (8+ in 90 d) | 67% | 24% | 3% (3–7 in 90 d) |
| Top-20 holders of 40 open markets, off the monthly profit top 1,000 (1,070 wallets; `selective-holders-census.txt`) | 76% | 15% | 8% |
| … of those, positions of 10K+ contracts (168) | 43% | **24%** | **33%** |

- Among the largest off-board positions, **57% are held by wallets that trade fewer than 8 markets a month**.
- Today's method can't evaluate any of them.
- Selective holders still trade enough over a year to judge:
  - median 37 single-market buys in 12 months;
  - 113 of 163 have 20+;
  - 81 of 163 have 40+.
- **Caveat:** many "selective" wallets are paused traders, not steady ones. In the census, the median was 5
  markets in the last 90 days against 37 in 12 months. The evaluation has to tell the two apart (see 3B).

## 3. The proposal

### A. Find by position, not by frequency

- **Holders as a source.** Add Polymarket's market holders (`/holders`: top holders per outcome) as a
  `find_wallets` source, next to leaderboards and market trades.
  - Sample markets across activity bands, plus recently resolved ones.
  - A wallet is found by what it holds, whatever its trade count.
  - Probe: 10 of the top 16 holders in one large market bought under 8 markets in 30 days.
- **Make discovery the directory.** The directory becomes the pool for the Worth following check, instead of
  200 wallets re-found every 6 hours. Retire `find_candidates`' trade-count × breadth ranking.
- **Board size doesn't raise the bar.** Being on a leaderboard stops raising the bet requirement.

### B. Evaluate a long record when the 30-day test can't judge

**Eligibility:** fewer than 8 priced events in 30 days, but 20+ independent single-market events over the last
12 months, and at least one buy in the last 90 days. Paused wallets wait.

**Evidence that survives past 60 days** (checked 13 Oct):

1. **Settled outcomes against the price paid.** Settlement is known for 94–100% of older bets. The test is a
   calibration z, `Σ(outcome − price) / √Σ price(1 − price)`, per independent event.
   - The market's own price is the null.
   - The variance is known rather than estimated, so the z is valid on a few dozen bets.
   - Parlays stay out, as now.
2. **The price 12 hours to 1 day after the buy**, a "closing line" test.
   - Polymarket still serves 12-hour price steps for a market's whole life: one that closed 166 days ago
     still had them.
   - It is less noisy than settlement, but it is untested as a skill signal, so it is validated first (3D).
3. **Track record:**
   - 12-month trading profit and months up (Polymarket's P&L history goes back to the first trade);
   - parlays' settled lower bound, where it applies.

**Standards (not lowered):**

- **Per wallet,** the same as today: edge z ≥ 2, still positive without their two best bets, profit not
  resting on one win.
- **Across the batch,** because broad discovery tests many wallets: control the false discovery rate
  (Benjamini–Hochberg, q = 0.10) over every wallet evaluated in a sweep.
  - Without it, 10,000 wallets with no skill would give ~230 false passes at z ≥ 2.
- **Confirmation before any recommended tier.** A long-record pass is provisional until the next 10+
  independent bets after it keep the edge positive. Only then can it become a sharp.

**What is honestly detectable.** Few trades can only prove a big edge. On settled outcomes (≈0.45 standard
deviation a bet), z ≥ 2 needs:

| Edge per bet | Bets needed |
|---|---|
| 5¢ | ~324 |
| 10¢ | ~81 |
| 15¢ | ~36 |
| 20¢ | ~20 |

So at 4 trades a month, a year proves only edges of about 13¢ or more. The 12-hour price test, if it validates,
would need several times fewer bets. Either way, the page should say "too few trades to measure yet" rather
than imply there's no edge.

### C. Show them without overclaiming

Three statuses, outside Sharps and Rising and never in the Feed's Recommended until confirmed:

- **Selective: skill not yet measured.** Found and eligible, record shown (trades, profit, settled
  record), no verdict.
- **Selective: early record.** Passed the long-record bar; waiting for confirmation.
- **Selective sharp.** Confirmed out of sample. The only one that can be recommended.

Also:

- A **"Selective traders"** view on Traders, beside "See all checked traders".
- **Following them** is offered for research, as for anyone.
- **Not trading lately** scales with their own pace: no buy in the longer of 7 days or twice their usual gap.

### D. Validate before building (to pre-register, as the fixes 7–9 test was)

- **Pool:** off-board selective wallets from the holders census and the frequency study's selective band.
- **Selection:** long-record evidence from bets made Oct 2025 – Jul 2026.
- **Outcome:** Aug – Dec 2026. Profitability, persistence (settled and 12-hour edge, plus the 5-minute edge
  where priced) and copyability, each judged separately.
- **Ship B only if** long-record passes keep a positive edge out of sample, with a bootstrap lower bound > 0,
  and beat the baseline.
- **Frequency study:** its settlement-based rules (R-settle) are a first read of exactly this. Its results come
  first.

## 4. Effect on coverage and false positives

| Change | Coverage | False positives |
|---|---|---|
| A (holders, directory as pool) | More wallets found and kept, especially large off-board positions | None by itself: finding isn't recommending |
| B, evaluating rather than skipping | ~15–25% of off-board holders become evaluable (most selective ones have 20+ bets a year) | Held at today's per-wallet bar, plus FDR across the batch, plus out-of-sample confirmation |
| C (statuses) | Visible as "skill not yet measured" instead of absent | No one is called skilled on a long record alone until confirmed |
| Recommended tiers | Unchanged until D passes | Unchanged |

## 5. Order of work, smallest first (each its own PR, after approval)

1. **SDK.** Holders as a `find_wallets` source.
2. **SDK.** `long_record(wallet, months=12)`: settled-outcome calibration z, 12-hour move, 12-month profit, the
   eligibility rule. Evidence only, no tier.
3. **Pre-register and run D.**
4. **Signals.**
   - The directory scores eligible selective wallets with `long_record` instead of skipping them.
   - The "Selective traders" view with the three statuses.
   - Pace-scaled inactivity.
5. **Only if D passes:** confirmed selective sharps join recommendations.

## Result of the validation (2026-10-13; `selective-preregistered.md`, `selective-results.txt`)

**Pool:** 250 selective and 150 frequent wallets from markets that closed before 14 Jul, judged on the 91 days
after.

**Verdict, against the pre-registered bar: the long-record track is not supported for selective traders.**

| Selective band (250) | Result |
|---|---|
| `L-settle` (settled z ≥ 2) | picks 1 |
| `L-move` (12-hour z ≥ 2) | picks 8 |
| After FDR | 0 and 2 |
| Next 91 days, the picks with any settled bets | −11¢ a trade (6 events): too few to judge |
| Baseline, settled edge | +0.91¢ (−1.36 to +2.72) |
| Baseline, still trading in the next 91 days | 39% |
| Baseline, a 10th independent bet within 91 days | 10% |
| Picks reaching 10 bets within 91 days | 0 of 9 |
| `P-profit` (profitable over the past year), profitable in the next 91 days | 52%, against 39% for all |
| `P-profit`, edge | not clearly above zero |

**Frequent control (150):**

- `L-settle` picks kept their edge: +5.09¢ (+3.00 to +8.09), 3 wallets. FDR: 2 wallets, +5.97¢.
- The measure can work where there are bets to measure. It doesn't reach the traders it's meant for.

**Calibration:** the settled z is conservative (z ≥ 2 in 1–2% against 2.3% nominal). The 12-hour z runs a little
hot (5–6%), so the FDR step matters for it.

**What it means:**

1. **A per-trader skill verdict isn't supportable for most selective traders** with the data available. They
   make too few settled bets to show anything but a very large edge, and most pause for months.
2. **"10 more bets before confirming" isn't practical.** 90% of selective traders don't reach 10 in a quarter.
3. **Keep them discoverable and honestly labelled.** "Limited evidence", their record (bets, profit, settled
   results with the count) and Follow for research, with no skill verdict. That's what V1 ships.
4. **Don't build "Selective sharp", or the B track as specified.**
   - Holders as a discovery source (A) is still worth doing for coverage, as discovery, not as a skill claim.
   - A settled-outcome edge with its range can be shown as context, labelled "not enough to measure skill" until
     it reaches the frequent-trader bar.

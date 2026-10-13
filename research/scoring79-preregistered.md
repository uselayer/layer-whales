# Pre-registered test: audit fixes 7–9 (written 2026-10-13, before any window-B outcome was computed)

Fixes 7–9 from the 13 Oct tier audit change what counts as skill, so they are tested before anything changes.
Question 4 (30 vs 90 days, selective traders) is the `frequency-study` branch's pre-registered test
(`frequency-preregistered.md`); this one reads its results and doesn't re-run it.

- **7. Profit gate.** Skill "yes" today needs a profitable 30-day window. Should a measured pricing edge count
  without it?
- **8. Sharp on one window.** Should Sharp need the edge to repeat across periods, not one 30-day window?
- **9. Parlays.** Wallets whose sampled buys are parlays get no usable bets. How can they be evaluated without
  inventing evidence?

The method under test is uselayer 0.9.0's `_verdicts`, unchanged. Its tiers are taken as fixes 1–6 leave them
(SDK branch `tiers-skill-not-copy`): a tier from skill and record only, never from copying. Sharp is skill
"yes" over 40 events (big account) or 25 (small). Nothing in Signals or the SDK changes because of this test.

## Already seen before writing this

- The aggregate R1–R3 results of `overlooked-scores.jsonl.gz` (FINDINGS.md, 12 Oct): edge A→B correlation
  +0.06, R2 picks −1.47¢ copy in B. None of the splits below have been computed on it.
- For question 9: four wallets from today's live check (MumiaTriste, technician2, TheyAreTaking…,
  welcometowinner…).
  - MumiaTriste and technician2 have 100% parlay buys in 30 days.
  - Losing parlays are never redeemed (0 of 2,907 redemptions paid $0).
  - Parlays bought 21–28 days ago returned at least +16.8% for MumiaTriste and at least +1.6% for technician2,
    counting unredeemed parlays as lost.

## Windows (fixed dates)

T = 2026-09-16 00:00 UTC (A starts 57 days back, inside Polymarket's ~60 days of 5-minute prices).

- A = the 30 days before T (selection).
- A1 / A2 = its first and second 15 days.
- B = T to 2026-10-13 18:00 UTC (outcome, 27.75 days).

## Pools

1. **big** (primary): 200 wallets drawn at random (seed 79) from the top 1,000 of Polymarket's all-time
   *volume* leaderboard, read today.
   - Volume measures activity, not outcome. Today's *profit* boards rank on profit that overlaps B, so they
     aren't used.
   - Each wallet is scored by the unchanged 0.9.0 scorer with `sample=60`: `score(end=T, days=30)` and
     `score(end=B_end, days=27.75)`. Every checked bet is kept.
2. **overlooked** (replication): `overlooked-scores.jsonl.gz`, 310 wallets.
   - A = 13 Aug–12 Sep, B = 12 Sep–12 Oct, same sample size.
   - Its rules are recomputed from the kept bets.
   - Profit is `pnl_window` (Polymarket's figure, rewards in), since that run didn't keep trading-only profit.
3. **frequency part 2** (replication, if finished before analysis): read-only, same rules.

## Definitions on A (from the kept bets, 0.9.0's own functions)

- *n*: independent events with a 5-minute price.
- *edge*: the event-pooled 5-minute move.
- *trimmed*: the edge without the two best bets.
- *profitable*: A `trading_pnl_window` > 0.
- **EDGE** = takes a side, n ≥ 25, edge mean > 0, edge z ≥ 2, trimmed mean > 0. This is skill "yes" without
  the profit gate.
- **E+** = EDGE and profitable (today's skill "yes").
- **E−** = EDGE and not profitable: who fix 7 adds.
- **E−90** = E− whose 90-day trading profit at T is > 0.
- **S1** = E+ (today's single-window standard: Sharp once the bets bar is met).
- **S2** = S1 and edge mean > 0 in both A1 and A2, each with ≥ 8 events (replicates inside the window).
- **S3** = S1 and edge z ≥ 3 (a stricter single-window bar instead of replication).

## Outcomes on B (each reported on its own, with 95% ranges)

1. **Profitability**
   - Share of wallets with B trading profit > 0 (Wilson range) and the median.
   - Pooled return per dollar traded.
2. **Skill persistence**
   - Pooled B 5-minute edge per event. The range comes from a bootstrap over wallets (2,000 resamples,
     seed 1), so one heavy wallet can't carry a group.
   - Share of wallets with B edge mean > 0.
3. **Copyability**: pooled B copy per event (bought 1–6 min late at the ask, valued at 1 h, after the fee),
   with the same bootstrap.

Calibration, reported with the results: the share of wallets with B z ≥ 2 among those with A |z| < 1. The
nominal rate is 2.3% if z means what it says.

## Success bars (decided now)

**Skill bar:** the pooled B edge's bootstrap 95% lower bound is > 0. Any group under 10 wallets is reported
as "too few to judge", with its range.

- **Fix 7 (drop the profit gate)**
  - Supported if E− meets the skill bar.
  - Rejected if E− fails it while E+ meets it.
  - Otherwise inconclusive.
  - E−90 is reported the same way, as the middle option (a longer profit gate).
- **Fix 8 (require repetition)**
  - S2 is supported if S2 meets the skill bar and S1 \ S2 (the wallets it drops) doesn't, with a lower pooled
    B edge.
  - If the dropped wallets also meet the bar, the rule costs coverage for nothing: rejected.
  - The same applies to S3.
  - Fewer than 10 dropped wallets: inconclusive, and the November test below decides.
- **Fix 9 (parlays), descriptive plus one check**
  - Counts: how many wallets are mostly parlays, how many lose their sample to parlays, and how much of
    their trading parlays are.
  - Variant P: the same scorer, with parlay buys (`isCombo`) dropped *before* the 60 are picked. It is run for
    every wallet in pool 1 with a parlay in either window's sample.
    - Report how many events and verdicts change.
    - Report whether wallets that only become measurable under P meet the skill bar in B.
  - Mostly-parlay wallets (≥ 90% of A buys): a conservative settled parlay return.
    - Payout from REDEEM rows, unredeemed counted as lost.
    - Parlays bought in A, redeemed by B_end; and parlays bought in B up to 5 days before B_end.
    - The error is clustered by purchase day.
    - Does A's sign predict B's? Descriptive, never a skill verdict.

## Follow-up fixed now: the true multi-month test (run on or after 2026-11-13)

A and B bets are kept, so the November run only scores C = 2026-10-13 18:00 to 2026-11-12 18:00 for the same
wallets.

- **M1** = skill yes in B alone (today's method at that date).
- **M2** = EDGE in A and in B (both z ≥ 2).
- **M3** = edge mean > 0 in A and B, with pooled A+B z ≥ 2.83 (the two-window equivalent of z ≥ 2 each).

Outcomes and bars as above, on C. This is the test fix 8 actually asks for; this run's S2/S3 are its
within-window stand-in.

## Amendment 1 (2026-10-13, after the replication pool's results, before any primary-pool outcome)

**What had been seen:** `scoring79_analyze.py` run on the overlooked pool (replication) only.

- 7 of the 15 E− wallets there show a 5-minute "edge" of +24¢ to +35¢ on tiny trades, and each lost about
  $20K in B.
- These are the thin-market artifact FINDINGS.md already describes, not traders the gate wrongly excludes.
- No outcome of the primary pool (`scoring79-scores.jsonl`, still being scored) had been computed.

**Added split, on both pools:**

- **E−plaus** = E− with A edge mean ≤ 5¢ (a move a liquid market can plausibly make in 5 minutes).
- **E+plaus** = the same cut on E+.

The fix 7 bar is unchanged and applies to E−plaus as well: its pooled B edge must have a bootstrap lower bound
> 0. Labelled exploratory in the results, because the cut was suggested by the replication data.

# How accurate are today's recommended traders? Spread bias and chance (2026-10-15)

Investigation only: no scoring, threshold or page change. Data: Signals' 43 recommended traders as published on
2026-10-15 ~02:30 UTC (`/api/worth`, public), plus 342 control wallets from the research pools, each on its current
30 days and on the 30 days before. Scores use uselayer `main` (2c1d9be, the stable sample from uselayer-sdk#26) on
the same windows. Every checked bet is classified as a resting order (maker) or not (taker) from Polymarket's
taker-only trade feed (100% classified), and the 1-minute mid is read around each fill. 774 wallet-windows, 37,991 checked bets.

## 1. The bias, quantified

The skill test is `mid price 5–10 min later − the trader's fill price` (`whale_scores._check`). A maker's buy
fills at the bid, below the mid, so the test credits them half the spread before the price moves at all. A taker
pays the ask and is charged half the spread.

| Checked bets | Fill vs mid just before | Half-spread (SDK `half_spread`) | Production edge | Edge with the maker's half-spread removed |
|---|---|---|---|---|
| Recommended, maker fills (1,223) | +0.50¢ median | 0.52¢ | +2.34¢ | +1.83¢ |
| Controls, maker fills (9,027) | +1.00¢ median | 0.55¢ | −0.45¢ | −0.98¢ |

Per trader it decides the tier for passive bid-posters. Taking each trader's measured maker half-spread per bet
off **production's own** edge and z (`results.txt`, section 1b):

- **7 of the 14 Proven sharps have no edge left** (z from +2.4…+8.1 to −3.0…+0.75). For 5 of them the spread is
  112–198% of their whole measured edge: 0xcf7379b4, 0x2d6ac4f7, 0x200ffb57, 0xadfb6cba, 0xa697d0b3. Also
  0xfb07f485 (76%) and 0xd950ef37 (69%). Their checked bets are 98–100% maker fills.
- 1 Proven (0xa19cbaba) falls to Rising z (1.64). 6 Proven and all 4 Quiet stay sharps.
- **3 of the 25 Rising drop out**: 0x80a0da00, 0x8efa1ea8, 0x6cfce245. 22 stay.
- **In all, 10 of 43 (23%) rest on the spread.** None of them is luck in the usual sense: being a maker persists,
  so rescoring and next-month checks repeat it, and our earlier persistence evidence ("14 of 15 Sharps kept an
  edge") could not see it.

Production's `maker_share` (from recent fills overall) is not a usable proxy: 0x924379a7 shows 0.998 there, but
98% of its checked buys were taker fills ($213k single buys). A fix has to classify the checked bets themselves.

## 2. False positives: the estimate against the evidence

**Production scoring history.** Per-wallet `score_history` couldn't be read: auto mode blocks production DB
reads (`snapshot_history.py` is the read-only snapshot for the owner to run). The aggregate in `/api/monitoring`
(2026-10-13 → 10-15) shows 186 rescores of a recommended trader, 155 kept (83%), 64 ever recommended against 43
now, 11 re-entries. That fits luck moving traders in and out under 0.12.0's stride sampling. It can't separate
luck from skill: a trader with ≤60 usable buys gets the same bets every rescore, and spread capture repeats on
every rescore.

**Independent check instead:** the edge on the other 30 days, measured without the spread.

| Passers | Other-window edge > 0 | Traders with no current edge |
|---|---|---|
| Sharps, current edge (study, 30) | 83% | 54–57% |
| **Sharps, maker spread removed (study, 21)** | **100%** (67% at z ≥ 2) | 57% |
| **Rising, corrected (uselayer-sdk#28 scores, 19)** | **89%** (17 of 19) | 47% (n 74) |
| Corrected sharps (uselayer-sdk#28 scores, 13) | 85% | 47% |

- **Sharps:** the earlier "about 1 in 10 by chance" missed the real problem. Their failure is spread capture
  (section 1), and once that's removed the sharps that remain held up.
- **Rising: how many are chance can't be pinned down from these data.** The "about 4 in 10 (16–75%)" this
  section gave earlier is **withdrawn** (section 8).
  - It came from `(1 − positive share) / (1 − null share)`, which assumes a genuinely skilled trader *always*
    shows a positive edge the other month.
  - Corrected sharps replicate only 85% of the time, so that assumption fails. With the branch's own scores, the
    answer is about 20% if real traders always replicate, about 1% at 90%, and 0% at 85%.
  - What the data do show: Rising traders replicate far more often than traders with no edge (89% against 47%).

## 3. Corrections compared

Pooled study population (43 recommended + controls, 74 pass today). "Genuine" estimate for the removed =
checked × (prior-positive share − 0.57) / 0.43. That assumes real traders always replicate, which they don't
(section 2), so these counts are **lower bounds** on the real traders a rule removes. The direct measurement is the
"prior edge > 0" column: compare it with the 57% for traders with no current edge.

| Rule | Still pass | Removed | Removed with a prior edge > 0 | Estimated genuine removed |
|---|---|---|---|---|
| **Maker half-spread removed, thresholds unchanged** | 55 | 19 | 12 of 19 (≈ null rate) | **~3** |
| + Rising z ≥ 1.645 | 43 | +12 | 9 of 11 | ~6 of the extra 12 |
| + Rising z ≥ 2 | 37 | +18 | 12 of 15 | ~8 of the extra 18 |
| + Benjamini–Hochberg FDR 10% (p ≤ 0.010, z ≈ 2.3) | 33 | +22 | 16 of 19 | ~12 of the extra 22 |
| No spread fix, Rising z ≥ 2 | 51 | 23 | 15 of 20 | ~9 |
| Symmetric (also credit takers +½ spread) | adds passers | — | — | not recommended: takers' early move is partly their own buying (1m→5m sensitivity) |

**Two things follow:**
- **The spread fix removes almost only traders with no edge before.**
- **Every stricter Rising cut on top of it removes mostly traders who did have one.** At this sample size (≤60
  bets) a higher z cut costs more real traders than chance ones. FDR is the most expensive.

## 4. The stable-sampling work (uselayer-sdk#26 and #27, both merged, unreleased)

- **Different problems.** #26 and #27 choose *which* bets get checked; this fix changes *what each bet is measured
  against*. They don't overlap, and this one doesn't need re-measuring after them (results here already use
  #26's sample).
- **With #26 released, rescoring stops being a fresh draw.** A chance Rising then stays until its bets age out:
  fewer flips, same per-look chance rate. Rescores can't be used as confirmation either.
- **#27's 120-bet option is the lever for Rising's chance share.** It cut edge error 1.29¢ → 0.72¢ at +54% reads,
  without a threshold change. That decision belongs to that work, not this one.

## 5. Recommendation: the smallest defensible fix (built: uselayer-sdk#28, in review, not released)

In the SDK skill test, take the half-spread off bets that filled as resting orders:
`moved[300] − maker share of that buy × half_spread(mid)`.

- Uses `half_spread`, which the copy test already uses.
- No threshold change, and takers are left as they are, so it can only remove passers, never add one.
- Ship it in the next batch release with #26/#27. Re-measure the list after it's live before any Rising decision.

Leave the Rising thresholds alone for now. A tighter cut removes more real traders than chance ones at today's
sample size; more bets per score (#27) is the better lever.

## 6. Validation of the built correction (uselayer-sdk#28)

**Are we subtracting only what resting orders earned?**
- **Which fills rested.** Taken from Polymarket's taker-only trade feed. All 644 taker fills in a 10-wallet check
  matched an `/activity` fill exactly (same transaction, token and size), so the SDK classifies the fills it
  already read.
- **How much they earned (corrected 2026-10-15).** An earlier version said the correction "never takes off more
  than the order earned". **That was wrong.** Measured with the branch's own mid (the 5-minute tape) against
  1-minute prices, on 2,110 resting buys whose book didn't move around the fill:
  - **Over:** it takes off more than the measured gain on **55 (2.6%)**, by a median 1¢. That excess is **2.9%**
    of everything taken off.
  - **Under:** on books wider than two ticks it takes off less (mean measured 1.58¢ against 0.55¢), so on
    average it underestimates.
  - **Tiers:** capping every buy at its measured gain changes **no tier** among the 286 scores below, including
    all 8 changes (their z is unchanged).
  - **Moving books:** the 1-minute "gain" includes the price drifting into the order, so it isn't a clean
    reference there.
- **Only the resting part.** A partly-resting buy gets that share of the half-spread, and takers get nothing.

**Against independent history** (`validation/validation.txt`):
- **Scores:** 286 from the branch code: the 43 recommended on their production windows and their prior 30 days,
  and 100 controls on both windows.
- **"Existing":** the same bets and rules with the edge from the fill price. The rebuild matches the branch on all
  59 scores where nothing came off.
- **References:** from the *other* window, built from 1-minute mids, and not using the correction:

| Passers | Other window: mid-to-mid 5-min edge > 0 | mean z | Settlement vs mid, per bet |
|---|---|---|---|
| Existing rule | 61% (n 51) | +0.78 | +0.7¢ |
| **Corrected** | **88%** (n 32) | **+2.10** | **+3.9¢** |
| Removed by the correction | 16% (n 19) | −1.46 | −5.4¢ |
| Pass neither (8+ bets) | 46% (n 169) | −0.73 | −1.8¢ |

- **Removed traders do worse than no-skill wallets.** That fits resting bids that tend to fill as the price falls
  through them.
- **Ranking improves:** the current z's rank correlation with the other window's mid-to-mid edge goes from 0.29 to
  0.50 (n 210). Settlement is too noisy over 30 days to rank by (0.01 → 0.08).

**Who changes on today's list, and why** (branch run, production windows): 8 of 43, every one of them mostly
resting buys.

| Wallet | Was | Becomes | Resting buys | Spread / bet | Edge from fill → corrected | Prior month, spread-free |
|---|---|---|---|---|---|---|
| 0xcf7379b4 | Proven | Big profit, edge unproven | 100% | 0.49¢ | +0.32¢ z+2.8 → −0.17¢ z−1.5 | −0.21¢ z−3.0 |
| 0x2d6ac4f7 | Proven | Big profit, edge unproven | 100% | 0.47¢ | +0.28¢ z+3.4 → −0.19¢ z−2.2 | −1.15¢ z−1.4 |
| 0xadfb6cba | Proven | Big profit, edge unproven | 98% | 0.64¢ | +0.57¢ z+2.3 → −0.08¢ z−0.3 | −0.32¢ z−1.8 |
| 0x200ffb57 | Proven | Edge not proven | 100% | 0.58¢ | +1.26¢ z+2.2 → +0.67¢ z+1.2 | −0.30¢ z−3.7 |
| 0xfb07f485 | Rising | Edge not proven | 100% | 0.57¢ | +0.58¢ z+1.5 → +0.01¢ z+0.0 | −1.30¢ z−3.2 |
| 0x6cfce245 | Rising | Edge not proven | 94% | 0.23¢ | +0.12¢ z+1.4 → −0.11¢ z−1.1 | −1.60¢ z−1.1 |
| 0xf2f6af4f | Rising | Big profit, edge unproven | 64% | 0.36¢ | +0.58¢ z+3.3 → +0.22¢ z+1.0 | +5.82¢ z+0.9 (11 bets) |
| 0x80a0da00 | Rising | Big profit, edge unproven | 28% | 0.16¢ | +0.38¢ z+2.1 → +0.22¢ z+1.1 | −0.44¢ z−0.8 |

- **Six of the eight lost money against the mid in their prior month.** 0xf2f6af4f is the one plausible real edge
  removed, on 11 bets.
- **Their profit stays on record.** Most land in "Big profit, edge unproven": their profit is shown, and the
  "Beats the price" check now says how much of their buying rested and what the spread got them.
- **Why 8 here and 10 in section 1:**
  - Section 1b applied each trader's measured spread to production's own scores.
  - This run re-picks the bets with #26's stable sample, which already puts 0xd950ef37 and 0xa697d0b3 outside a
    recommended tier before any correction. That difference is #26's, not this fix's.
  - 0xa19cbaba stays Rising here.

**Cost: #26–#28 together against production** (`validation/bench.txt`). The same 73 wallets were scored live,
as production does, under each build at the same time, counting every data-API and Gamma read:

| Build | Reads per score | vs 0.12.0 |
|---|---|---|
| 0.12.0 (production) | 64.6 | — |
| + #26 | 72.5 | +12.3% |
| + #26 + #27 (SDK main now) | 72.8 | +12.7% |
| **+ #26 + #27 + #28** | **78.0** | **+20.7%** |

- **#26:** most of the increase. Its stable sample spreads checked bets across the window, so fewer share a
  price-history read (45.5 → 51.5).
- **#28:** 5.2 taker-feed reads.
- **Production:** about 56 reads per full score on 0.12.0 (Oct 15 so far: 28,150 background reads, 2,821 one-read
  screenings, 453 full scores), so about 68 after the three.
- **Throughput:** the same work needs about 19% more reads. At the fixed 4 reads/s, that's roughly **16% fewer
  evaluations an hour**.
- **"Check now":** about +2 s at 3 reads/s.
- **Classification:** 100% of checked buys classified.

**Maker profitability is kept, apart from skill:**
- `Bet.moved` still counts from their fill price.
- `edge_at_fill`, `spread_earned` and `maker_bets` record what they got and how.
- Polymarket's profit figures are untouched.
- Only the skill test (`edge`, `edge_verdict`, the tiers) stops counting the spread as foresight.

## 7. Rollout recommendation (nothing deployed)

1. **Review uselayer-sdk#28** (ready for review). #27 was merged on 2026-10-15 and main is merged into #28, with
   2,271 tests passing. Merging #28 doesn't release anything.
2. **Wait for Signals #88's measurement.** #88 deploys no earlier than 9:27 PM PT Oct 15, and its after-window
   needs uselayer 0.12.0 live for 24 h. So nothing from #26–#28 reaches production before ~9:30 PM PT Oct 16, and
   not before #88's comparison is posted. Releasing or bumping the lock inside that window would change tiers and
   reads mid-measurement.
3. **One batch release with #26 + #27 + #28,** so tiers reshuffle once. The owner approves PyPI.
4. **Signals lock bump PR,** with two changes:
   - `WORTH_FORMAT` 9 → 10.
   - Older scores in a recommended tier are treated as stale. Today `_consistent` keeps an older score whose
     `segment_of` tier matches, but `segment_of` can't apply this correction: an old score has no maker data. So
     those scores would keep their inflated tier until rescored. The rule is that they drop out until rescored.
     Recommended traders are rescored every 6 h.
5. **After deploy,** watch `/api/monitoring` → `qualification`. Expect about 8 dropouts (mostly to big_profit),
   marked as the release, not churn. Also watch full scores per hour: about 16% fewer is expected, from reads.
   Check the "Beats the price" text on one resting trader's page.
6. **Only then** revisit Rising, if the owner wants.

## 8. Retired estimates

The owner retired these on 2026-10-15. They came from an earlier session's placebo and persistence inference.
They never reached the repo, a PR or the site, and must not be cited:
- **"About 8–9 of the 35 recommended are likely chance"** (ranges 2–16 Rising, 0–7 Sharps).
- **"Rising: about 1 in 3 chance"** (31–38%).
- **"Sharps: about 1 in 10 chance"** (4–13%). This one was wrong in kind: the Sharp problem is spread capture
  (section 1), and with the spread removed, sharps held up out of sample.
- **The placebo pass rates** (5.4% at z ≥ 2, "2.4× nominal"). They rested on resampling with the spread bias still
  in.
- **"About 4 in 10 Rising are chance (16–75%)"**, from this study's first version. It assumed real traders always
  replicate (section 2), and the data can't pin the share down.
- **"Never takes off more than a resting order earned"**, from this study's first version. It does on 2.6% of
  cleanly measured resting buys (section 6), and that changes no tier.

What stands instead is section 2: Rising traders replicate far more often than traders with no edge (89% against
47%), but how many are chance isn't known. `research/scoring79-findings.md`'s "4–8% of no-signal wallets reach z ≥ 2" is a separate,
measured calibration and stays; it predates the spread finding.

## 9. Wording for the "1 in 3 Rising" warning (not published)

The evidence supports "some Rising traders will turn out to be chance; we can't yet say how many". It doesn't
support "1 in 3". Wording with direct support:

> Rising means early signs, not proof. Some Rising traders will turn out to be chance, and we can't yet say how many.

## Limits

- **Controls aren't the production population.** They're research-pool wallets, not production's 3,858 scored
  wallets, so the BH cut is computed on 385.
- **Small numbers.** Rising has 27 passers with prior data, and the bootstrap isn't seeded (ranges move ±2 points
  between runs).
- **The half-spread is a tick-based estimate** (0.5¢ / 1¢). A wider book earns makers more, so on average the fix
  takes off less than makers earn, though on 2.6% of cleanly measured resting buys it takes off more. Section 1b uses each trader's maker share and half-spread from a fresh stable sample of the same window,
  not production's exact bets.
- **Historical windows lack biggest win** (`biggest_win`), so the "lucky" rule is off for them.
- **The 1m→5m measure** (spread and own impact both removed) passes almost nobody. It is a sensitivity check, not
  a proposal: it discards real information priced in during the first minute.

Files: `spread_study.py` (collection, public Polymarket reads only), `analyze.py`, `chance_share.py`,
`exclusions.py`, `results.txt`, `rows.jsonl.gz`, `recommended-2026-10-15.json`, `snapshot_history.py`;
`validation/` (`validate_run.py` scores with the uselayer-sdk#28 branch and counts reads, `validate_analyze.py`,
`validation.txt`, `branch-scores.jsonl.gz`).

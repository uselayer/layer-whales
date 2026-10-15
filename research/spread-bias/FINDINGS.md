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

**Independent check instead:** the spread-neutral edge on the prior 30 days. Traders whose current z is near 0
show a positive prior edge 54–57% of the time (the null rate). Share by chance ≈ (1 − positive share) / (1 − null
rate), assuming real skill always shows; that likely overstates chance a little for weak real edges.

| Passers (study population) | Prior edge > 0 | Share by chance (90% range) |
|---|---|---|
| Sharps, current edge (30) | 83% | 36% (14–65%) |
| **Sharps, maker spread removed (21)** | **100%** (67% at z ≥ 2) | **≈0%** |
| Rising, current edge (37) | 81% | 41% (20–70%) |
| **Rising, maker spread removed (27)** | **81%** | **43% (16–75%)** |

- **Sharps:** the earlier "about 1 in 10 by chance" missed the real problem. Their failure is spread capture
  (section 1), and once that's removed the sharps that remain all held up.
- **Rising:** chance is real, but the size isn't pinned down. "About 1 in 3" is inside the range; so are 1 in 6
  and 3 in 4. n = 27, 13 of them current recommended.

## 3. Corrections compared

Pooled study population (43 recommended + controls, 74 pass today). "Genuine" estimate for the removed =
checked × (prior-positive share − 0.57) / 0.43.

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

## 4. The stable-sampling work (uselayer-sdk#26 merged/unreleased, #27 open)

- **Different problems.** #26 and #27 choose *which* bets get checked; this fix changes *what each bet is measured
  against*. They don't overlap, and this one doesn't need re-measuring after them (results here already use
  #26's sample).
- **With #26 released, rescoring stops being a fresh draw.** A chance Rising then stays until its bets age out:
  fewer flips, same per-look chance rate. Rescores can't be used as confirmation either.
- **#27's 120-bet option is the lever for Rising's chance share.** It cut edge error 1.29¢ → 0.72¢ at +54% reads,
  without a threshold change. That decision belongs to that work, not this one.

## 5. Recommendation: the smallest defensible fix

In the SDK skill test, take the half-spread off bets that filled as resting orders:
`moved[300] − maker_share_of_that_bet × half_spread(mid)`.

- Uses `half_spread`, which the copy test already uses.
- Classifying the checked bets takes one taker-only `/trades` read per 10 markets: about 6 reads per 60-bet score
  (~+12%).
- No threshold change, and takers are left as they are, so it can only remove passers, never add one.
- Expected effect on today's list: 10 of 43 out (7 Proven, 3 Rising), Sharps 18 → 10.
- Ship it in the next batch release with #26/#27. Re-measure the list after it's live before any Rising decision.

Leave the Rising thresholds alone for now. A tighter cut removes more real traders than chance ones at today's
sample size; more bets per score (#27) is the better lever.

## 6. Wording for the "1 in 3 Rising" warning (not published)

The evidence supports "some Rising traders will turn out to be chance; we can't yet say how many". It doesn't
support "1 in 3". Wording with direct support:

> Rising means early signs, not proof. Some Rising traders will turn out to be chance, and we can't yet say how many.

## Limits

- **Controls aren't the production population.** They're research-pool wallets, not production's 3,858 scored
  wallets, so the BH cut is computed on 385.
- **Small numbers.** Rising has 27 passers with prior data, and the bootstrap isn't seeded (ranges move ±2 points
  between runs).
- **The half-spread is a tick-based estimate** (0.5¢ / 1¢). A wider book earns makers more, so the fix is a lower
  bound. Section 1b uses each trader's maker share and half-spread from a fresh stable sample of the same window,
  not production's exact bets.
- **Historical windows lack biggest win** (`biggest_win`), so the "lucky" rule is off for them.
- **The 1m→5m measure** (spread and own impact both removed) passes almost nobody. It is a sensitivity check, not
  a proposal: it discards real information priced in during the first minute.

Files: `spread_study.py` (collection, public Polymarket reads only), `analyze.py`, `chance_share.py`,
`exclusions.py`, `results.txt`, `rows.jsonl.gz`, `recommended-2026-10-15.json`, `snapshot_history.py`.

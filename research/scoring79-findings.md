# Audit fixes 7–9: what the evidence says (2026-10-13)

## Basis

- **Rules:** fixed before any outcome was computed (`scoring79-preregistered.md`), plus amendment 1.
- **Raw results:** `scoring79-results.txt`; scores in `scoring79-scores.jsonl.gz`.
- **Method:** select on window A, judge on window B, with three outcomes kept apart: profitability, skill
  (5-minute edge), copyability.
- **Pools:**

| Pool | Wallets | Window A (selection) | Window B (outcome) | Used as |
|---|---|---|---|---|
| Volume board (random draw from all-time volume top 1,000) | 200; 65 with 25+ checked events | 17 Aug – 16 Sep | 16 Sep – 13 Oct | Primary |
| Overlooked | 310; 185 with 25+ | 13 Aug – 12 Sep | 12 Sep – 12 Oct | Replication |

**Headline limit:** every rule group is small (5–15 wallets per pool).

- Per pool, every group is under the pre-registered 10, so each verdict is formally "too few to judge".
- Pooled across both pools (exploratory) the groups reach 11–20, and the direction is the same in each pool
  unless noted.
- Treat what follows as the best available evidence, not proof.

## Fix 7: the 30-day profit gate inside skill

**Recommendation: keep the gate.**

| Picked on A | Wallets | B 5-min edge (95% range) | Kept an edge in B | B profitable | B copy |
|---|---|---|---|---|---|
| E+: edge z≥2 and profitable (today's skill "yes") | 15 | **+1.56¢** (+0.86 to +2.38) ✔ | 14/15 | 7/15 | −0.80¢ |
| E−: edge z≥2, lost money (what fix 7 adds) | 20 | **−0.14¢** (−2.13 to +1.24) ✘ | 12/20 | 6/20 | −2.25¢ |
| … E− with a plausible edge (≤ 5¢) | 11 | +0.48¢ (−0.02 to +1.02) ✘ | 7/11 | 6/11 | −1.14¢ |
| … E− with an edge > 5¢ | 9 | −1.92¢ (−8.62 to +2.86) ✘ | 5/9 | 0/9 | −5.51¢ |

Pooled across both pools (exploratory). Per pool:

- Primary: E+ +0.91¢ (7 wallets); E− +0.71¢ (5 wallets), both above zero.
- Replication: E+ +2.07¢ (8 wallets); E− −0.53¢ (15 wallets).

What it says:

1. **The gate does real work. It filters artifacts, not good traders.**
   - Almost half the wallets it keeps out (9 of 20) show a 5-minute "edge" above 5¢. That is the thin-market
     artifact from FINDINGS.md: tiny trades where the price history isn't what anyone could buy.
   - All 9 lost money the next month.
   - A losing month is cheap evidence that a large measured edge isn't real.
2. **Wallets with a plausible edge that the gate excludes are borderline.**
   - Their next-month edge (+0.48¢) is about a third of the E+ group's (+1.56¢), and its range touches zero.
   - Not enough to call them skilled. Not evidence that they aren't, either.
3. **Last month's profit is a weak guide to next month's profit.**
   - Volume-board wallets were profitable in B 50% of the time after a profitable A, and 48% after a losing A.
   - Small accounts: 41% against 22%.
   - So the gate earns its place as an artifact filter, not as "they're profitable".

Coverage on today's live 200:

- **If the gate were dropped:** 4 wallets would enter. AnonymousUsername, D8D5289c and AiBird would become
  Sharps, and PredictionYogi Rising. All four have plausible edges.
- **Under a 90-day gate instead:** only AnonymousUsername (+$125K over 90 days) and PredictionYogi
  (+$5.7K) would enter.
- That middle option had 2 wallets in the test: too few to judge.

**What to do instead:**

- Keep these wallets out of the tiers.
- Say why on their page: "the price moved their way (+1.75¢, z +2.1), but they lost from trading over the
  last 30 days". The tier stays "Edge not proven".
- Worth a separate test: a ceiling on a believable 5-minute edge (e.g. > 5¢ means "check the market's
  liquidity"). It is the biggest false-positive source seen here. Of the 15 E+ wallets, only 1 is above 5¢.

## Fix 8: Sharp on one 30-day window, or repeated

**Recommendation: keep one window for now. The November test decides.**

**Single-window passes held up.** Of the wallets that passed today's standard (E+), 14 of 15 kept a positive
edge the next month.

Persistence by A's z-score, both pools:

| A edge z | Wallets | Kept a positive edge in B |
|---|---|---|
| 1 to 2 | 36 | 27 |
| 2 to 3 | 15 | 12 |
| 3 or more | 26 | 20 |

- The z behaves roughly as stated. Of wallets with A |z| < 1, 4–8% reached z ≥ 2 in B, against 2.3% nominal.
- So one window at z ≥ 2 (plus ≥ 25/40 events and the trim check) is not producing many false skill calls.

**Stricter rules only removed real edges.**

| Rule | Wallets it drops | Their B edge |
|---|---|---|
| Edge positive in both halves of the window | 2 | +0.63¢, both still positive |
| Edge z ≥ 3 | 4 | +1.79¢ (+0.47 to +4.08), all still positive |

On the volume-board pool, the halves rule would cut Sharps (40+ events) from 6 to 4. No evidence it removes a
single false positive, so it costs coverage for nothing.

**The real two-month test is scheduled.**

- Fix 8 literally asks whether an edge in *two consecutive months* predicts the next.
- That needs a third month, and Polymarket keeps 5-minute prices for only ~60 days.
- The rules (M1 latest month only, M2 edge in both months, M3 pooled z ≥ 2.83) are fixed in the
  pre-registration.
- A cloud routine runs them on 2026-11-13 (`scoring79_followup.py`).

**A statistical point either way:**

- Requiring z ≥ 2 in two independent months cuts the null false-pass rate from ~2.3% to ~0.05%.
- It also roughly squares the power: a trader passing half their months would pass two in a row a quarter of
  the time.
- Today's data shows few false passes to remove, so the cost would land mostly on real traders.

**Skill is not profit, and not copy.**

- The single-window Sharps kept their edge, but only 7/15 made money in B.
- Copies lost money in every group: −0.8¢ to −2.3¢ a contract after fees.
- Nothing here supports presenting a Sharp's trades as a profitable signal.

## Fix 9: parlays

**Recommendation: change the sampling (cheap and safe). Give pure-parlay traders their own "can't be
price-tested" reason, with no skill verdict.**

**How parlays show up.** Each parlay fill carries `isCombo: True`. Parlays have no market listing and no price
history. Today they are dropped *after* the 60 are picked, so a parlay-heavy wallet can end up with nothing to
check.

**How often it matters.** It is rare, and for most affected wallets the sampling fix can't help:

| Where | Wallets with a parlay in the sample | Sample all or nearly all parlays |
|---|---|---|
| Live 200 | 10 | 3 |
| Volume board | 6 of 200 | 1 |
| Overlooked | 25 of 310 | 1 |

Of the live three, MumiaTriste (2,672 buys) and technician2 (4,279) are **100% parlays** over 30 days, and
TheyAreTaking… is 31 of 32.

**Dropping parlays before sampling (variant P):**

- Needs no extra reads (the flag is in the fills already) and doesn't affect wallets without parlays.
- On the volume-board pool it changed one verdict. Feromont went from 0 checked bets to 37; skill "unknown" →
  "no"; next-month edge −2.36¢.
- Mixed wallets gained 0–5 events. No other verdict changed.

**Pure-parlay traders can't be tested for skill with the 5-minute test:**

- Profit is still measured: Polymarket's P&L includes parlays. MumiaTriste: +$342K over 30 days, up 11 of 12
  weeks.
- There is also a settled record. Losing parlays are never redeemed (0 of 2,907 redemptions paid $0), so
  "redeemed ÷ spent", counting unredeemed as lost, is a lower bound on their return:
  - MumiaTriste: ≥ +16.8% on $605K in one week.
  - technician2: ≥ +1.6% on $1.47M.
  - Feromont: ≥ −9.4% on $2.18M, and lost $283K the next month. The record matched.
- This is evidence of profit per bet, not of beating the price. Parlays in one day share legs, so the error
  is clustered by day. It is untested for persistence.
- Use it as track-record context, never as a tier.
- Copying a parlay can't be priced: "Unknown", never offered.

**Coverage:**

- Pure-parlay wallets stay "Not enough data", with the reason "trades parlays, which can't be price-tested".
  Fixes 1–6 already count combos in that reason.
- Variant P moves about 1 in 200 wallets from "Not enough data" to a real verdict.

## Question 4: does a 90-day window find overlooked selective traders?

**Pending.** It is the `frequency-study` branch's pre-registered test (450 wallets across frequent, selective
and sparse bands). It was about halfway through part 1 at 21:40 UTC.

Already established there (amendment 1):

- Polymarket keeps 5-minute prices for only about 60 days.
- So a 90-day window adds nothing to today's skill test. Bets older than ~60 days get no price.
- A selective trader can only be judged on settlement (their own hold-to-settlement return) or on P&L,
  which is what that test does.

Until it reports, the only way the current test can see more of a selective trader's bets is a window up to
~55 days.

## Summary

| Fix | Do | Coverage (live 200) | False positives |
|---|---|---|---|
| 7 Profit gate | **Keep.** Say "edge seen, losing month" on the page instead | 0 change (dropping it: +3 Sharps, +1 Rising) | Dropping it would let in thin-market artifacts: 9 of 20 added, all lost money next month |
| 8 Repeat across months | **Don't require yet.** Decide on 13 Nov | 0 change (halves rule: −2 of 6 Sharps on the volume board) | None found among single-window passes: 14/15 kept their edge |
| 9 Parlays | **Pre-filter `isCombo` before sampling;** pure parlays get their own reason and P&L, no verdict | ~1 in 200 gains a verdict | None added: nothing is inferred for parlays |

Nothing in Signals or the SDK was changed. The SDK-side changes (7: none; 9: the pre-filter and the reason
text) build on uselayer `main` 5a77fc5 (0.10.0) or later.

## Decisions (owner, 13 Oct)

- **Fixes 7 and 8: unchanged, pending stronger evidence.** Sharps still need a profitable 30 days, and one
  window.
- **Profit and edge are kept apart in the evidence and on the page.**
  - uselayer 0.11 (uselayer-sdk #21) adds `edge_verdict`: the price test alone.
  - It also adds the tier "Edge, but lost money lately", for a measured edge with a losing window.
  - So a losing trader with an edge is never called edge-less.
- **Fix 9: parlays are left out before sampling** (#21).
  - Parlay-only traders get "Parlays: skill unmeasured" and stay visible with their profit (Signals draft #72).
- **Thresholds are revisited only after** the 13 Nov validation and the frequency study.

## Limitations

**Small samples.**

- Per pool, every rule group had 5–15 wallets: under the pre-registered 10, so formally "too few to judge".
- The headline numbers pool both pools (11–20 wallets per group). That pooling was not pre-registered, so it is
  exploratory.
- Ranges come from a bootstrap over wallets. With 15 wallets, one wallet moves a pooled mean noticeably.
- Of the volume-board draw, only 65 of 200 wallets had 25+ checked events in a window.
  - Most volume-board wallets are dormant.
  - So the "big account" evidence rests on a few dozen traders.

**One step ahead, one period.**

- Each test judges the month after selection, with one selection month (mid-Aug to mid-Sep). Market
  conditions in that period (NFL start, Fed, elections) may not repeat.
- "Kept an edge next month" is not "kept it for a year".

**Selection bias.**

- **Volume board.** It ranks on lifetime activity, so it leans to large, long-lived accounts. It can't see
  traders who are good and small.
- **Overlooked pool.** It required activity in currently open markets, i.e. activity in B (not profit).
- **Survivors only.** Neither pool contains wallets that stopped trading before selection.
- **Leaderboards today aren't usable.** Today's profit boards overlap the outcome window, so they could not be
  used as a pool without hindsight.
- **Frequent traders dominate both pools.** Wallets are found through recent trades, and the tests need 25+
  checked events. These results say little about selective traders; see the frequency study and the discovery
  proposal.

**Price history.**

- **Retention.**
  - Polymarket keeps 5-minute prices for only about 60 days: 98–100% of bets under 60 days old, 22% at 60–69
    days, ~0% after.
  - So the skill test can't look back further than ~2 months.
  - That's why the true two-month test (fix 8) has to wait for November.
- **The history is the mid price.**
  - It is the middle of the book in 5-minute steps, not what anyone could buy.
  - In thin markets a large "edge" is often an artifact. 9 of the 20 wallets fix 7 would have added had
    edges > 5¢, and all lost money the next month.
- **Copy returns are estimates.** The ask is inferred from the mid. Settled returns use only bets that had
  settled.
- **Parlays have no price history at all.** The settled-parlay return is a lower bound from redemptions
  (losers are never redeemed), not a price test.

**Measurement.**

- **The edge statistic.**
  - It pools per independent event (bets on one event count once).
  - The z treats events as independent, which correlated markets (one game's spread and total) partly
    aren't.
  - Calibration was close to nominal (4–8% against 2.3% for wallets with no signal), not exact.
- **Profit.**
  - The 30-day trading profit comes from Polymarket's hourly P&L history, including unrealized marks, which
    are noisy for big accounts.
  - The overlooked pool kept only Polymarket's total figure (rewards in).

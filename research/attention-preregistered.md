# Pre-registered test: which activity patterns deserve attention? (written 2026-10-14, before any outcome is computed)

Signals now says what stands out about a trade (`signals/notes.py`, PR #116): a new position or adding to one, a
size far above the trader's usual, a first trade after a quiet spell, a category none of their checked buys were
in. Alerts may later be prioritised by such patterns (PR #106 dry run: per-trader rarity, gated groups). Neither
has been tested against what happened next. This test asks one thing per pattern:

> **Is a trade with this pattern followed by different price movement than the same trader's ordinary trades?**

"Deserves attention" here means *informative about what the market did next*, measured within the trader, so a
skilled trader's general edge isn't mistaken for the pattern's. It does **not** mean "worth copying" (reported
separately, and expected to be ~0 or negative after costs, as in every study so far), and a null result is a
valid, publishable answer: it means the notes stay context, never a ranking.

Nothing in Signals or the SDK changes because of this test. Status: **awaiting the owner's sign-off before any
data is collected.**

## Already seen before writing this

- PR #106 (`notable-findings.md`): volumes only. NM-P ~410 alert-worthy buys a day, 72% new positions, 32% ≥ 2×
  typical; consensus among bot-like sports traders looked coincidental. No outcome was computed.
- #76 (draft): $10k+ buys by selective traders: 5-minute edge +2.7¢ (z 6) but a copier gets −0.6¢ (likely the
  buyer's own price impact); settlement +6.0¢ (z 2.2, hindsight-prone). Nothing split by the patterns below.
- scoring79 / overlooked: skill (5-minute edge) persists A→B; copying and profit don't.

## Windows (fixed dates)

- **H** (history, for each trader's baseline): 2026-08-15 00:00 → 2026-09-14 00:00 UTC (30 days).
- **B** (events and outcomes): 2026-09-14 00:00 → 2026-10-12 00:00 UTC (28 days), split **B1** / **B2** at
  2026-09-28 00:00 for replication. Inside Polymarket's ~60 days of 5-minute prices; B ends 2 days before
  collection so every 24-hour outcome is complete.
- Baselines for a trade at time *t* use only trades before *t* (H plus B up to *t*): no look-ahead.

## Pools

1. **recommended** (primary): every wallet in a recommended tier (Proven sharp, Quiet sharp, Rising) in the
   production check on the collection day, as listed by `/api/worth` (public). Expected ~35.
2. **control**: 60 wallets drawn at random (seed 116) from the same check's scored wallets *not* in a
   recommended tier, with ≥ 20 buys in B. Tests whether a pattern is informative regardless of skill.

Wallets tagged market maker or arbitrage by the SDK are left out of both (no view to read).

## Events and patterns (on B; each buy decision = fills on one outcome within 60 s, the SDK's unit)

Each buy decision in B, priced 5¢–95¢ at entry, is labelled with every pattern it has (not exclusive):

| # | Pattern | Definition (only data before the trade) |
|---|---|---|
| P1 | New position | held < 2% of the post-trade position before the decision (from trades since H start) |
| P2 | Big for them | decision $ ≥ 3× the trader's median first buy over the prior 30 days |
| P3 | Rare for them | at most 1 decision this big (or bigger) per day on average over the prior 30 days (#106's rarity, B = 1) |
| P4 | After a quiet spell | no trade of any kind in the prior 7 days |
| P5 | New category | none of the trader's prior-30-day decisions in this market's category (≥ 10 prior decisions) |
| P6 | Consensus | another pool trader bought the same outcome within the prior 6 h (1 h in Sports/Esports) |
| P7 | Against another | another pool trader bought the *opposite* outcome of the same market within the prior 6 h |
| P8 | Exposure jump | P1 or adding, and P2 (notes.py's interpretation trigger) |

The reference group for each pattern is the **same trader's B decisions without it**.

## Outcomes (signed in the trade's direction: + means the price moved toward their outcome)

1. **Primary — 24-hour move:** mid price 24 h after the decision's last fill minus their average fill price, in ¢.
2. 1-hour move (same).
3. Settlement: payout (0/1) minus fill price, for markets settled by collection day (reported with the share
   settled; hindsight-prone, never primary).
4. **What acting on it would have got** (reported, never a success criterion): bought 5 minutes after the last
   fill at the ask (`copy_entry` rule, + taker fee), valued at the 24 h mid.

## Analysis

- Per pattern and pool: the **within-trader difference** = mean outcome of pattern decisions − mean of the same
  trader's other decisions, pooled across traders weighted by decisions, with a bootstrap over traders (2,000
  resamples, seed 1) for the 95% range. Traders with < 3 pattern and < 3 other decisions don't count.
- A trader's own price impact is reported beside each pattern: the move at 1 minute. A pattern whose 24 h
  difference is no bigger than its 1-minute difference is impact, not information.
- 8 patterns × 2 pools × primary outcome = 16 tests: **Benjamini–Hochberg at q = 0.10** across them.

## Success bar (decided now)

A pattern **deserves attention** in a pool only if all three hold:

1. Its within-trader 24 h difference is > 0 and survives BH at q = 0.10.
2. The difference is > 0 in both B1 and B2.
3. The 24 h difference exceeds the 1-minute difference (not just their own impact).

Anything else is "context only". Whatever the result, nothing may say a pattern *predicts* profit: outcome 4 is
reported, not used, and a pattern that passes earns the right to rank or ping (an owner decision), not to be
called informed.

## What would change

- **Passes in recommended only:** candidates for prioritising alerts from those traders; still worded as
  "unusual for them", with the measured difference in "How we pick traders".
- **Passes in both pools:** a property of the pattern, not the trader; same.
- **Passes nowhere:** notes stay context (as shipped in #116); #106's rarity remains a volume tool only, and its
  doc says so.

## Data and cost (estimate)

Per wallet: trades over H + B (1–6 reads of 500; bots up to the SDK's 5,000-fill cap, flagged when capped),
5-minute price history per decision token (cached in `research/.cache`), Gamma markets for categories (shared).
~100 wallets ≈ 3–6k reads at a research rate below the per-host limit, a few hours. No production reads.

## Not in scope

Sells and exits (no outcome definition agreed yet), alert delivery, any threshold or scoring change, wallet
discovery.

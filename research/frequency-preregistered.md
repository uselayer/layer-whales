# Pre-registered test: 30-day vs 90-day evaluation, frequent vs selective traders

Written 2026-10-13, committed before any window-B outcome was scored.

## The question

Does Signals identify genuinely good traders, or only traders who trade often enough to satisfy its rules? The
scorer looks at 30 days, and needs 8 independent bets (`RISING_BETS`) before it can find any skill. A trader who
makes 5 careful bets a month can never be recommended, however good they are. Would a 90-day window find good
selective traders, and would what it finds hold up afterwards?

Three outcomes, judged separately, because they are different questions:

1. **Profitability**: do the traders picked keep making money?
2. **Repeatable skill**: does the advantage they showed persist?
3. **Copyability**: could a follower capture it after costs?

The goal is genuinely profitable traders, selective ones included, not only profitable copy trades. So a rule
can pass 1 and 2 and fail 3: that trader is worth showing, without offering copying.

Nothing in Signals or the SDK changes because of this until the evidence has been reviewed.

## Windows (fixed dates)

- T = 2026-08-14 00:00 UTC.
- A30 = the 30 days before T; A90 = the 90 days before T (selection).
- B = T to 2026-10-13 00:00 UTC, 60 days (outcome). Sixty rather than thirty, so selective traders have bets to
  measure.

Each wallet is scored three times by the **unchanged** uselayer 0.9.0 scorer, `sample=60`:
`score(end=T, days=30)`, `score(end=T, days=90)` and `score(end=B_end, days=60)`.

## Pool (no hindsight)

- **Markets:** closed Polymarket markets with an end date between 2026-06-15 and T, sampled across four volume
  bands ($1k–10k, $10k–100k, $100k–1M, $1M+).
- **Wallets:** everyone in those markets' last 500 trades with a trade in the 90 days before T. No leaderboard
  is used: today's boards rank on recent profit, which overlaps window B.
- **Activity:** no wallet is required to trade after T. Wallets that stop trading in B count as they are: their B
  profit is whatever their positions did, and their stopping is reported.
- **Frequency band,** from one read of each wallet's trades in A90 (before T only):
  - frequent: 8+ markets bought in A30
  - selective: fewer than 8 in A30, 8+ in A90
  - sparse: 3–7 in A90
- 150 wallets drawn at random per band (seed 13); fewer if the pool runs short, and the report says so.

## Selection rules, judged on A only

| Rule | Picks |
|---|---|
| `R30-rec` (today's method) | A30 segment is proven / quiet / rising / too_fast |
| `R30-skill` | A30 skill verdict is yes or early |
| `R90-rec` | A90 segment is proven / quiet / rising / too_fast, or `inactive` (the 7-day activity rule is what turns a recommended segment into `inactive`) |
| `R90-skill` | A90 skill verdict is yes or early |
| `R90-profit` (naive control) | A90 trading profit > 0 |
| `all` (baseline) | every wallet in the band |

## Outcomes, on B only

1. **Profitability** (per wallet): B trading P&L (`trading_pnl_window`, from Polymarket's P&L history; trading
   only, rewards apart).
   - Share of picks with B P&L > 0, with a 95% Wilson range.
   - Median B P&L.
   - Pooled return per dollar traded (sum of P&L / sum of volume).
   - Share still trading in B (1+ buy).
2. **Repeatable skill** (pooled per independent event over the picks' B bets):
   - Primary: 5-minute edge (the SDK's skill test, `moved[300]`).
   - Secondary: the trader's own hold-to-settlement return per contract (payout − their price), settled bets only.
3. **Copyability** (pooled per event):
   - Primary: copy, i.e. bought 1–6 minutes late at the estimated ask and valued at 1 hour, after the taker fee
     (`copy`).
   - Secondary: a copier holding to settlement (payout − price 1 minute later − fee), settled bets only.

Persistence: the correlation across wallets of A edge with B edge (8+ bets in both), for A30 and for A90, per band.

## Success bars (each outcome on its own)

A rule "works" on an outcome in a band when:

1. **Profitability:** its share with B P&L > 0 beats the band's `all` share (two-proportion z ≥ 2), and its
   median B P&L is > 0.
2. **Skill:** its pooled B 5-minute edge is > 0 with z ≥ 2.
3. **Copyability:** its pooled B copy is > 0 with z ≥ 2.

## Comparisons

- **Main:** `R90-skill` / `R90-rec` on **selective** traders, on each outcome. Do they find real traders that
  today's method can't see at all? (`R30-*` can't pick a selective trader: fewer than 8 bets in 30 days.)
- `R90-*` vs `R30-*` on **frequent** traders: does the longer window help or hurt where both can judge?
- Selective `R90-skill` vs frequent `R30-rec`: are the selective finds at least as good as today's picks?
- Every rule vs `R90-profit`: does the scorer beat "they made money last quarter"?

## Known limits

- One period (May–Oct 2026).
- Selective traders bring few bets, so their pooled outcomes will have wide ranges.
- 60 sampled bets per score.
- Copy is valued at the hour mark.
- The P&L history is Polymarket's hourly series.
- The pool is wallets in markets that closed in June–August, which leans towards short markets.
- An earlier test (`overlooked-preregistered.md`) found A→B persistence of +0.06 for 30-day windows. A null here is
  plausible and would itself be the answer.

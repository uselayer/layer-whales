# Pre-registered test: selective traders who bet big, on and off the leaderboards

Written 2026-10-13, committed before the pool was built or any wallet scored.

## Why

The frequent-vs-selective test (`frequency-preregistered.md`) found no hidden pool of good selective traders, but
its pool was mostly small accounts (median profit afterwards $0). This test asks the same question of the
population that matters: traders who make a few **large** bets a month, on the leaderboards and off them.

- Does their profitability persist?
- Does their advantage persist?
- Can a follower capture it?
- Does today's method overlook genuinely promising ones?

Scoring thresholds are not changed by this test.

## Who (decided from trades before T only)

- T = 2026-08-14 00:00 UTC.
- A = the 90 days before T, cut into three 30-day months.
- **Selective, large:** all three of these, in A:
  - bought in 9–21 different markets (about 3–7 a month);
  - bought in at least 2 of the 3 months, so not one burst;
  - median dollars bought per market of at least $1,000.
- **Exploratory subgroup:** median of at least $10,000.
- Every qualifying wallet is kept, up to 300 per source.

## Where they're found (two sources, reported apart and together)

**Market (no hindsight):**
- Every trade of $1,000 or more among the last 500 big trades of closed markets that ended between 2026-06-15
  and T.
- Markets are sampled across four volume bands ($10k–100k, $100k–1M, $1M–10M, $10M+), 100 per band.
- Only trades in A count.

**Leaderboard:**
- Polymarket's all-time boards as published today: profit and volume, top 2,000 each, and each category's
  all-time profit top 500.
- Today's monthly and weekly boards are not used: they rank on profit made inside window B.
- **Known bias:** all-time profit includes window B, so the profit-board wallets lean towards ones that did well
  afterwards. Volume-board wallets lean much less. Results are shown per board type: `lb-profit` (on an all-time
  profit board) and `lb-volume` (volume board only).
- A wallet found by both sources counts as leaderboard, and also appears in the market source's results.

## Scores (unchanged uselayer 0.9.0 scorer, `sample=60`)

| Window | Call |
|---|---|
| A (select) | `score(end=T, days=90)` |
| B (outcome) | `score(end=2026-10-13, days=60)`, fully priced |
| Today's method, at the time | `score(end=2026-09-13, days=30)` |
| Today's method, now | `score(end=2026-10-13, days=30)` |

`on_leaderboard` is passed as True for wallets from the leaderboard source and False for the market source. This
approximates the check's own list (which only matters for Proven vs Quiet).

## Selection rules (A only)

| Rule | Picks |
|---|---|
| `all` | every wallet |
| `R-profit` | A trading P&L > 0 |
| `R-profit10k` | A trading P&L ≥ $10,000 |
| `R-settle` | A own hold-to-settlement return (payout − their price, per contract, pooled per independent event): mean > 0, z ≥ 2, 8+ events |
| `R-settle1` | the same with z ≥ 1 (a looser bar, as their bets are few) |
| `R-both` | `R-profit` and `R-settle1` |

## Outcomes (B only), kept apart

1. **Profitability** (per wallet): B trading P&L.
   - Share > 0 (95% Wilson range), median, pooled return per dollar.
   - Counts making and losing $1,000+ and $10,000+.
   - Share still trading.
2. **Skill** (pooled per independent event):
   - Primary: own hold-to-settlement return. These traders mostly hold, so settlement is their advantage.
   - Secondary: 5-minute edge.
   - Persistence: correlation across wallets of A with B own settlement return (8+ events in both); Spearman of A
     with B trading P&L.
3. **Copyability** (pooled per event):
   - Primary: a copier buying 1 minute later and holding to settlement, after the taker fee.
   - Secondary: copy valued at the hour.

Every figure is reported with its sample size (wallets, events) and its 95% range or z.

## Success bars (each outcome on its own)

1. **Profitability:** a rule's share in profit beats the source's `all` share (two-proportion z ≥ 2) **and** its
   median B P&L is > 0.
2. **Skill:** pooled B own settlement return > 0 with z ≥ 2.
3. **Copyability:** pooled B copier-to-settlement > 0 with z ≥ 2.

## Does today's method overlook promising ones?

**Promising in hindsight** means either of:
- B own settlement return > 0 with z ≥ 2 on 8+ events;
- B trading P&L ≥ $10,000 with a return of at least 5% of the volume traded.

Reported:
- how many are promising, by source;
- how many of them today's method recommended (a RECOMMENDED segment) at 13 Sep or 13 Oct;
- how many each A rule picked in advance (recall), and how many of each rule's picks were promising (precision);
- what phase 3's quick check would label them now.

## Known limits

- One period.
- Selective traders bring 9–21 bets to A and fewer to B, so per-wallet results are noisy and the pooled figures
  carry the weight.
- The leaderboard source's hindsight bias (above).
- 60 checked bets per score.
- Settlement outcomes only for markets that have settled by 2026-10-13.

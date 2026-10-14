# Pre-registered test: can a long record identify skill in selective traders? (written 2026-10-13, before any outcome was computed)

## The question

The 30-day test can't judge someone who makes 3–5 trades a month. Can evidence that lasts longer than
Polymarket's ~60 days of 5-minute prices pick out such traders who do well afterwards, without picking too many
who don't? The two candidate measures are settled outcomes against the price paid, and the 12-hour price move.

The answer decides whether the long-record track in `selective-discovery-proposal.md` gets built. Nothing in
Signals or the SDK changes because of this test, and V1 doesn't wait for it.

## Dates (fixed)

- **T** = 2026-07-14 00:00 UTC.
- **S (selection)** = the 365 days before T.
- **O (outcome)** = T to 2026-10-13 00:00 UTC (91 days).

## Pool (no hindsight)

- **Markets:** 240 Polymarket markets that closed between 2026-06-01 and T, 60 from each of four volume bands
  (by total volume).
- **Wallets:** everyone in each market's last 500 trades. No leaderboard is read. Leaderboard membership today
  is reported for the picks, never used.
- **Classification:** each wallet is classified from its own trades before T only, in a seeded random order
  (seed 31).
- **Counting rule:** a market counts once; parlays (`isCombo`) never count.

| Band | Markets bought in the 30 days before T | Single markets in the 365 days before T | Other |
|---|---|---|---|
| **selective** | 1–7 | ≥ 20 | a buy in the 90 days before T |
| **frequent** (control) | ≥ 8 | ≥ 20 | — |

- Up to 250 selective and 150 frequent wallets are taken. Fewer if the pool runs short, and the report says
  so.
- Wallets that stop trading in O stay in, and their stopping is reported.

## Evidence on S (per wallet)

**Bets and events.**

- Bets are buys, merged as the SDK does: fills on one outcome within a minute are one decision, and only the
  first decision on an outcome counts.
- Prices must be between 2¢ and 98¢.
- Bets on the same event count once: the event's bets are averaged.
- A frequent wallet's S bets are thinned evenly to at most 120.

**Two measures:**

1. **Settled.**
   - Each bet's value is the payout of the outcome bought (1 or 0) minus the price paid. Settled bets only.
   - Calibration z = Σ_events d / √Σ_events p(1−p): the market's own price is the null.
   - An event's p(1−p) is the mean over its bets, counted as one bet.
2. **12-hour move.**
   - Each bet's value is the price at the first point at least 12 hours after the buy (within 36 hours)
     minus the price paid.
   - The price comes from Polymarket's whole-life price history (`interval=max`, 12-hour steps).
   - If the market settled first, the payout is used.
   - z = mean / (sd / √n) over events.

Both measures get a trimmed mean as well: the same mean without the wallet's two best bets.

**Track record:** trading profit over S, from Polymarket's profit history (trading only).

## Rules (judged on S only)

| Rule | Picks |
|---|---|
| `L-settle` | settled z ≥ 2, ≥ 20 settled events, trimmed mean > 0 |
| `L-move` | 12-hour z ≥ 2, ≥ 20 events with a 12-hour price, trimmed mean > 0 |
| `L-both` | both |
| `L-settle-FDR`, `L-move-FDR` | the same, but significant after Benjamini–Hochberg (q = 0.10) over every wallet in the band, one-sided |
| `P-profit` (naive control) | trading profit over S > 0 |
| `all` (baseline) | every wallet in the band |

## Outcomes on O (each separately; ranges from a bootstrap over wallets, 2,000 resamples, seed 1)

1. **Profitability.**
   - Share of wallets with O trading profit > 0 (Wilson range), and the median.
   - Share of wallets still trading in O.
2. **Skill persistence.**
   - Primary: the pooled settled edge per event, over bets placed in O that had settled by 2026-10-13.
   - Secondary: the pooled 12-hour move per event.
   - The share of wallets with a positive settled edge in O.
3. **Copyability.**
   - Buy at the first price point after their buy (0–12 hours later), hold to settlement, pay the market's
     taker fee.
   - Pooled per event.

## Success bars (decided now)

- **A rule identifies skill in a band when:**
  - its pooled O settled edge has a bootstrap lower bound > 0;
  - its point estimate is above the band's `all` baseline;
  - it has ≥ 10 wallets. Fewer is "too few to judge", reported with its range.
- **The long-record track is supported for selective traders when:**
  - `L-settle-FDR` or `L-move-FDR` meets that bar in the **selective** band;
  - and does at least as well as `P-profit` there (point estimate).
- **Comparison with the frequent band:** if the same rule meets the bar there, the measure works generally. If it
  does in frequent only, it doesn't reach the traders it's meant for.

## Practicality of "10 more bets before confirming"

For each rule's selective picks, report:

- days from T to their 10th independent event in O;
- the share reaching 10 within 30, 60 and 91 days;
- the share with fewer than 10 by the end of O.

Exploratory only: whether the sign of the first 10 O events agrees with the rest of O.

## False discoveries and calibration (reported with the results)

- The distribution of settled z and 12-hour z across all eligible wallets in each band.
- The share at z ≥ 2 against the nominal 2.3%.
- How many picks the FDR step removes.

## Known limits

- One selection date, one 91-day outcome.
- O's settled bets lean towards short markets. Long ones haven't settled.
- The pool comes from markets' last trades, which favours frequent traders.
- The 12-hour grid is coarse.
- A selective wallet has few O bets, so per-wallet results are noisy. The pooled bootstrap is the test.

## Amendment 1 (2026-10-13, during classification, before any outcome was computed)

**The classification read is capped at 10 pages (5,000 trades) per wallet.** A wallet trading one market all
day had stalled the run for an hour.

- A capped wallet that already shows 8+ markets in the 30 days and 20+ in the year is classified frequent.
- Any other capped wallet is left out as `capped`. The count is reported with the results.
- None of the rules, outcomes or bars change.

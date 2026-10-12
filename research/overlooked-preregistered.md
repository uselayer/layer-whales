# Pre-registered test (written 2026-10-12, before any window-B outcome was seen)

Question: can we find Polymarket traders the leaderboards don't list, and does following them pay a copier?

Pool: 250 random overlooked wallets (on no PnL/volume leaderboard read: monthly/weekly/all-time top 1,000/500,
8 category boards; seen in >=3 of 300 open markets sampled across the volume range; median trade $5-2,000),
plus 60 random leaderboard wallets as a control. Analysis keeps "small" = live volume < $1M and pnl < $100k.

Window A = the 30 days ending 30 days ago (selection). Window B = the last 30 days (outcome).
Each wallet scored by the unchanged SDK: score(w, end=now-30d) and score(w), sample=60.

Selection rules, judged on A only:
  R1 SDK followable: segment proven / quiet / rising
  R2 beats the price: 5-min edge mean > 0, z >= 2
  R3 copy-positive: copy mean > 0, z >= 1
Outcome, on B only, pooled per independent event across the selected wallets:
  primary   = copy (bought 1-6 min late, valued at 1 h, after taker fee), cents a contract
  secondary = hold-to-settlement copier return (payout - price 1-6 min later - fee), settled bets only
Success = a rule's B copy return > 0 with z >= 2 AND above the all-wallets baseline.

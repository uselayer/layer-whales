# Test C: would copying proven Polymarket traders have made money? (2026-10-07)

`smart_money_test.py --days 14 --wallets 100`: the top 100 Polymarket wallets by volume this month, split
by profit made before the 14-day window. "Proven" = the best third of those already in profit (22 wallets).
One entry per wallet per bet; settled markets only. Raw numbers: `results.json`, run log `run-14d-100w.log`.

| Group | Trader's own return | Price moved their way by 1 min / 5 min | Copy 1 min later, after fees | + 1¢ slippage |
|---|---|---|---|---|
| Proven (22 wallets) | +3.1% ± 5.1% | **+1.2¢ ± 0.7¢ / +2.1¢ ± 1.0¢** | +1.2% ± 17% (n=157) | −1.0% |
| Other in-profit (46) | −0.9% ± 4.0% | −0.3¢ / −0.1¢ | −13% ± 12% (n=251) | −15% |
| Not proven (28) | −6.8% ± 4.9% | −0.5¢ / −0.6¢ | −1.6% ± 10% (n=468) | −3.6% |

What it says:
1. Proven traders do beat the price: it moves 1–2¢ their way within 5 minutes, and only for them (rule 1 holds).
2. That move is the copier's cost: copying a minute later comes out around break-even after fees, with wide error bars.
3. Past profit alone barely separates winners: proven wallets' own return isn't clearly above zero over 14 days.

Limits:
- Price history came back for only 876 of 10,430 decisions (Polymarket's `/v2/prices-history` returned nothing
  for the rest), so the copy columns rest on a small sample. Fix this first (try the CLOB price history, or the
  market's own trades around each decision) before trusting copy returns.
- 14 days, settled markets only (mostly short sports markets); pool is top-by-volume, so quiet sharps aren't in it.

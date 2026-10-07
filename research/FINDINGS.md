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

# Worth following: which price test, and what the price source covers (2026-10-07)

`worth_following_run.py --wallets 160 --sample 60` scored 160 Polymarket wallets with `client.whales.discover()`:
80 from the leaderboards, 80 found trading in busy markets that the top 1,000 doesn't list.
`worth_following_calibrate.py` then split each wallet's checked bets in time and asked whether the earlier
half predicts the later half. Output: `worth-following-calibration.txt`.

**Price source.** Test C's gap was the request, not the data: `/v2/prices-history` with `bucketSeconds=60`
returns nothing; with only `token_id`, `start` and `end` it returns 5-minute steps (the price at the start of
each). Coverage: a 5-minute price for **5,767 of 5,778** bets checked (99.8%), 1-hour 5,740; 178 more were
combo (parlay) bets, which have no market listing or price history and are left out. Polymarket's order-book
API (`clob.polymarket.com/prices-history`, 1-minute points) covered 9,261 of 9,617 in a first run, but the SDK
bars that host from the package (`test_public_repo.py`), so the SDK uses the data API.

| Check after the buy | Early ↔ late correlation | Later bets of wallets that passed on their early bets | Everyone else |
|---|---|---|---|
| 1 min (first price ≥ 1 min: 1–6 min) | +0.59 | +0.99¢ (z 6.5) | +0.15¢ |
| 5 min (5–10 min) | +0.36 | **+0.80¢ (z 4.4)** | +0.35¢ |
| 15 min | +0.13 | +0.36¢ | +0.55¢ |
| 1 hour | +0.30 | +2.26¢ (z 2.0) | +0.56¢ |
| Copying 1–6 min late, after fees, by the hour | +0.23 | +3.05¢ (z 0.8, n=25) | −0.22¢ |

What it says:
1. "Beats the price" is a real, persistent skill at short horizons; the hour mark is mostly the noise of games
   playing out. The score uses the 5-minute move: it persists and is measured past the point a copier gets in.
2. Copying is the hard part. Whether a copier gains after the delay and the fee barely carries over from one
   half to the next. So few traders pass "copyable", and the page says so instead of promising returns.

Segments in that run (before the "lucky needs evidence" fix): 1 proven, 1 quiet, 3 rising, 16 too fast,
62 lucky, 34 no view, 43 no clear edge.

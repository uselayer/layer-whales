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

# Overlooked traders: can we find them, and does following them pay? (2026-10-12)

`overlooked_candidates.py` found wallets on no leaderboard we can read (monthly/weekly/all-time profit and volume,
8 category boards: 3,034 wallets excluded), seen trading in 3+ of 300 open markets sampled evenly across the
volume range, median trade $5–$2,000. `overlooked_score.py` scored 250 of them plus 60 random leaderboard
wallets with the unchanged scorer twice: window A (13 Aug–12 Sep) to select, window B (12 Sep–12 Oct) to see
what following the picks would have done. Rules and the success bar were written down before any window-B
result was seen: `overlooked-preregistered.md`. Output: `overlooked-results.txt`; raw scores
`overlooked-scores.jsonl.gz` (both windows, every checked bet); run log `overlooked-run.log`.

| Picked on window A by | Overlooked picks | Copy in window B, after fees | Holding to settlement |
|---|---|---|---|
| Everyone (baseline) | 250 | −0.63¢ ± 0.27¢ (11,217 events) | +0.31¢ ± 0.95¢ |
| R1 SDK tiers: proven / quiet / rising | 11 | −0.40¢ ± 2.01¢ (434) | −2.01¢ ± 4.86¢ |
| R2 beats the price (5-min edge, z ≥ 2) | 22 | **−1.47¢ ± 0.97¢** (817) | −1.33¢ ± 3.25¢ |
| R3 copying paid (copy z ≥ 1) | 15 | −0.03¢ ± 1.24¢ (660) | +0.94¢ ± 4.00¢ |

What it says:
1. Finding them works: 11 of 250 overlooked wallets (4%) get a following tier, about the leaderboard rate
   (2 of 60). 171 of the 250 are small accounts (volume < $1M, profit < $100k), which the 12 Oct Worth
   following run almost never reached (13 of 200).
2. Following them doesn't pay: no rule met the pre-registered bar (window-B copy > 0 with z ≥ 2). Picking on
   "beats the price" was worse than picking no one, and worst on small accounts (−2.43¢ ± 1.25¢, 12 wallets).
3. Nothing carries over month to month for these wallets: window A predicts window B at +0.06 for the 5-min
   edge and +0.06 for copy (197 wallets with 15+ bets in both). Leaderboard picks did keep their 5-min edge
   (R2: +2.19¢ ± 1.21¢ in B), but not a copier's gain (−0.49¢ ± 1.86¢).
4. Tiny trades in thin markets can pass as skill: the one overlooked Quiet sharp (about $2 a trade, Sports)
   showed +24¢ within 5 minutes in A and −21¢ in B (copy −8.6¢). Price history in thin markets is a poor
   stand-in for what a copier could buy.

Limits:
- Not proof it can't work: R1's margin can't rule out a real copy edge up to ~1.6¢ a contract. One two-month
  test, 30-day windows, 60 checked bets per wallet per window.
- Pool is wallets active in currently open markets, so it requires activity in window B (not success).
- Copy is valued at the hour mark with Polymarket's taker fee; holding to settlement is on the bets in the
  sample that settled.

## Frequent vs selective traders: does a longer window find good traders the 30-day rules miss? (13 Oct 2026)

**Question:** is Signals finding genuinely good traders, or only those who trade often enough for its rules? The
scorer needs 8 independent bets in 30 days before it can judge skill, so a trader who makes a few careful bets a
month can never be recommended.

**Design:** pre-registered in `frequency-preregistered.md`, amended once before any outcome was looked at.
- **Pool:** 450 wallets drawn without hindsight: everyone trading in closed markets that ended before 14 Aug, in
  four volume bands, with a trade in the 90 days before.
- **Bands,** from their trades before the cut-off, 150 per band:
  - frequent: 8+ markets in 30 days
  - selective: under 8 in 30 days, 8+ in 90
  - sparse: 3–7 in 90
- **Three outcomes, judged separately:**
  1. Profitability: trading P&L afterwards.
  2. Repeatable skill: their 5-minute edge, and their own hold-to-settlement return.
  3. Copyability: a copier 1–6 minutes late, after fees.
- **Part 1:** select on 30 or 90 days before 14 Aug, measure the next 60 days.
- **Part 2:** today's SDK method, selecting on 30 days before 13 Sep, measuring the next 30.
- **Output:** `frequency-results-1.txt`, `frequency-results-2.txt`. Raw scores: `frequency-scores*.jsonl.gz`.

**First, a data limit:** Polymarket's price history keeps 5-minute prices for about 60 days.

| Bet age | Bets with a 5-minute price |
|---|---|
| under 60 days | 98–100% |
| 60–69 days | 22% |
| 70+ days | ~0% |

Settlement results stay available. So a 90-day version of the SDK's skill and copy tests can't be computed from
Polymarket's data: `score(days=90)` silently judges only the last ~60 days. Part 1 therefore selected on what
lasts, the trader's own settlement return and their P&L.

| | Frequent | Selective | Sparse |
|---|---|---|---|
| Share of the 4,680 wallets checked (290 more had under 3 markets) | 67% | 24% | 3% |
| Still trading in the next 60 days | 76% | 30% | 27% |
| In profit over the next 60 days (everyone) | 20% | 14% | 19% |
| Made $100+ / lost $100+ over the next 60 days | 13 / 55 | 2 / 7 | 2 / 6 |
| Best 90-day rule's picks in profit afterwards | 50% (12 picks, z +2.4) | 20% (5 picks) | — |
| …of those picks, made $100+ | 4 | 0 | — |

**1. Profitability:**
- Picking frequent traders on a 90-day settlement return roughly doubles the share still in profit afterwards (50%
  vs 20%; with profit as well, 57%). That passes the pre-registered bar, but the median pick made about $0 and
  4 of 12 made $100+.
- For selective traders no rule passed. Of every selective trader any 30- or 90-day rule picked in Part 1, none
  made $100 in the next 60 days. As a group they lost money (−$9,960) and 70% stopped trading.
- In Part 2 (select on 14 Aug–13 Sep, measure the next 30 days), selective traders who had made money stayed in
  profit more often (42% vs 12%, z +3.4), but the median was again $0. Two of 19 made $100+.

**2. Repeatable skill:**
- Only one rule passed, for a group of 2 wallets (frequent, `R30-both`). Every other pick group's later 5-minute
  edge was negative (down to −5.4¢) or not distinguishable from zero (at most +3¢, on 1–2 wallets, or +0.2¢ on
  29). Their own settlement return was indistinguishable from zero.
- On adjacent, fully priced 30-day windows (Part 2), frequent traders' own settlement return carried over at
  +0.50 and their 5-minute edge at +0.30 (about 50 wallets each). That is the strongest persistence seen so far
  (the earlier test found +0.06), but on a small sample.

**3. Copyability:** no rule passed in any band. A copier's 1-hour result was negative for nearly every group (down
to −9¢ a contract) and never more than +0.4¢ (2-wallet groups). Holding a copy to settlement was never
distinguishable from zero. All as in the earlier test.

**Today's method:** in Part 2 it recommended 1 of 450 wallets. That wallet made $317, too few to judge.

What it says:
1. In this pool, the 8-bets rule isn't hiding a group of good selective traders. Selective traders are common
   (about a quarter of active wallets), but most stop trading within two months, and as a group they lose money.
   No rule, on 30 or 90 days of their record, picked ones who went on to make money that mattered.
2. A longer window can't simply be switched on: the SDK's skill and copy tests have no prices past ~60 days.
   Judging over 90 days would mean a different test (settlement return, P&L). On frequent traders that test picked
   more who stayed in profit, but not ones whose skill or copies held up.
3. Scoring standards stay as they are. Phase 3 keeps every wallet and labels selective ones "Not enough recent
   evidence", and they're scored the day they reach 8 bets.

Limits:
- **The pool is mostly small accounts** (median profit afterwards $0). A big selective bettor, say a few $50k
  political bets a month, is rare in a random draw of 150 and isn't answered here. That needs a pool drawn by
  account size before the cut-off.
- One period (May–Oct 2026).
- Selective picks are few (5–37 per rule), so their ranges are wide.
- 60 checked bets per score.
- Copy is valued at the hour mark.

## Selective traders who bet big, on and off the leaderboards (14 Oct 2026)

**Question:** the frequent-vs-selective test drew mostly small accounts. Do traders making a few **large** bets a
month keep making money, keep their advantage, and can a follower capture it? And does today's method miss
promising ones?

**Design:** pre-registered in `selective-large-preregistered.md`.
- **Who:** in the 90 days before 14 Aug, bought in 9–21 markets (3–7 a month), in 2+ of the 3 months, with a median
  of $1,000+ a market.
- **Found two ways:**
  - $1,000+ trades in 400 closed markets (no hindsight): 10,768 wallets checked, 462 qualified, 300 kept.
  - Today's all-time leaderboards: 5,331 wallets checked, 82 qualified (76 on a profit board, which leans to
    hindsight; 6 on a volume board only).
- **Total:** 382 wallets; median $2,594 a market; 0 errors.
- **Measured:** the next 60 days.
- **Output:** `selective-large-results.txt`. Raw scores: `selective-large-scores.jsonl.gz`.

| | Market source, no hindsight (336) | All-time profit boards (76, leans to hindsight) | Biggest: $10k+ a market (59, both) |
|---|---|---|---|
| In profit afterwards | 40% [35–46%] | 41% [30–52%] | 42% [31–55%] |
| Made / lost $10k+ | 8 / 9 | 10 / 8 | 8 / 5 |
| Total P&L afterwards | −$7k | +$1.51M | +$1.43M |
| **Skill:** own settlement return a contract | +1.4¢ ± 3.4 (475 events) | +4.5¢ ± 4.8 (235) | **+6.0¢ ± 5.2 (z +2.2, 136)** |
| **Skill:** 5-minute edge | **+2.7¢ ± 0.9 (z +6.0, 675)** | +0.3¢ ± 0.8 | +0.5¢ ± 0.5 |
| **Copy:** a copier 1 min late, held to settlement | −0.3¢ ± 3.2 | +3.2¢ ± 4.7 | +4.7¢ ± 5.3 (z +1.8) |
| **Copy:** valued at 1 hour | −0.6¢ ± 1.4 | −1.6¢ ± 2.1 | −1.7¢ ± 1.9 |
| Profit persistence (Spearman, A → B) | +0.12 [+0.02, +0.23] | +0.26 [+0.04, +0.46] | — |

**1. Profitability:**
- Big selective bettors do much better than the small ones of the earlier test (41% in profit afterwards vs 14%).
- Having made money before helps a little:
  - Market source: 56% vs 40% stayed in profit (z +3.4, 177 picks). That passes the bar, but the median was $3 and
    20 made $1k+ while 19 lost $1k+.
  - Having made $10k+ before didn't help (31%, 39 picks).
- On the profit boards, a strong settlement record before (6 picks) was followed by 5 of 6 in profit, median
  +$4.7k. That passes the bar, but it's 6 wallets on the side that leans to hindsight.

**2. Skill:**
- The only pass is the biggest bettors' settlement return: +6.0¢ a contract (z +2.2, 59 wallets, 136 events).
  That was the pre-registered exploratory subgroup.
- The signal comes mostly from the leaderboard wallets: market-source big bettors alone showed +0.9¢ ± 9.4.
- Across wallets, settlement returns before and after barely line up (+0.23, CI −0.30 to +0.65, 16 wallets).
- Market-source wallets beat the price 5 minutes after their buys (+2.7¢, z +6.0), but a copier a minute later
  gets none of it (−0.6¢ at the hour). With $2,500+ buys, that looks like their own buying moving the price, not
  foresight.

**3. Copyability:**
- No rule or group passed.
- Closest: copying the biggest bettors and holding to settlement, +4.7¢ ± 5.3 a contract (z +1.8), again mostly
  from leaderboard wallets.

**Does today's method overlook them?**
- It recommended 1 of 382 wallets, at 13 Sep or 13 Oct. With a median of 4 bets in the last 30 days, it can't
  judge them at all.
- 13 of 382 (3%) were promising in hindsight (settlement z ≥ 2, or $10k+ at 5%+ of volume). The method caught 1.
- No rule on their earlier record found those 13 better than chance:

| Rule | Picks | Promising (precision) | Base rate |
|---|---|---|---|
| `R-profit` | 208 | 7 (3%) | 3% |
| `R-profit10k` | 61 | 5 (8%) | 3% |
| settlement rules | 13–27 | 1 each | 3% |

What it says:
1. Big selective bettors are worth discovering and keeping: as a group they hold their own, and the biggest show
   the one skill signal that passed. But no rule picked the future winners from their earlier record, and a
   copier's gain isn't shown.
2. Today's method can't see them, by design: too few bets in 30 days. Phase 3 now keeps them, labelled "Not
   enough recent evidence".
3. The lead worth following up is the biggest bettors' settlement return. It needs a test without the
   leaderboard's hindsight lean: a larger market-source sample of $10k+ bettors, and a second period.

Limits:
- One period.
- 9–21 bets per wallet before and fewer after, so per-wallet results are noisy.
- The profit-board source includes window B in its ranking.
- 60 checked bets per score.
- Settlement only for markets settled by 13 Oct.
- The 5-minute edge of big buyers includes their own price impact.

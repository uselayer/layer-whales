# Which Telegram alerts are worth an immediate ping? Dry run, 2026-10-14

Research only: no change to `signals/app.py`, no deploy, no Railway variable written, nothing in production written.
Production's `follows.json` and `alerts.db` were read read-only (over `railway ssh`, sqlite `mode=ro`).

**Question.** #98 sends each followed trader's *first* 5 trades a day and summarises the rest every 3 hours. Would
"notable now, routine in the summary from the first one" send the trades that matter instead?

**Answer.**

- The hypotheses as worded (a new position, or ≥ N× the trader's typical size) don't work for the traders that cause
  the overload. A busy sports trader makes hundreds of new positions a day, and their sizes are heavy-tailed. NM-P has
  ~410 alert-worthy buys a day: 72% are new positions and 32% are ≥ 2× their $67 typical buy. Every floor (0, 0.5×,
  1×) and multiple (1.5×, 2×, 3×) still hits the 20 cap between 03:00 and 09:00 UTC and drops 220–430 trades a day.
  That's worse than today.
- What works is adding **rarity for that trader**. A trade is notable only if that trader made at most B alerts that
  big or bigger a day over the last 30 days. This is "unusual for them", but it also counts how often they trade.
  With B ≤ 3, NM-P sends 0–6 immediate alerts a day: $1.6–1.9k buys at ~25× typical, about once a day for them. The
  rest go into the summaries, and nothing is dropped. A trader who trades once a day keeps nearly every big or new
  trade.
- Consensus needs a gate too. Bot-like sports traders (AJSV ~1,270 alert-worthy buys a day, 0x06dc ~360, NM-P ~410)
  "agree" by coincidence. In the 18-trader scenario, 27 of 60 immediate alerts were groups.
- **Recommended starting rule** (below): a new position ≥ 0.5× typical, or ≥ 2× typical, *and* at most
  min(3, 10 / follows) a day this big for that trader. Sells: full exits (same rarity), a sale of something their
  buy alert told you about (as `_told`), and sells ≥ 2× typical that are rare. Groups ping only when at least one
  member's buy is notable.

## Data

- **Who follows whom (production, 2026-10-14 16:19 UTC):** one account (the owner) with Telegram, following NM-P
  (`0xcf73…0215`) and gopfan2 (`0xf2f6…5817`). Both were followed at 03:53–03:54 UTC on Oct 14, and `alerts.db`
  starts then. One other followed trader can't supply "at least 3", so two labelled **hypothetical** scenarios add the
  17 traders Signals recommends (production `worth-following.json`, segments `proven` and `rising`): "owner + all 17
  Recommended" (18 follows, within MAX_FOLLOWS = 20) and "Recommended without NM-P".
- **Trades:** `notable_pull.py`, one pull per wallet through the SDK's Polymarket client (`/activity`
  TRADE/MERGE/SPLIT/REDEEM/CONVERSION, paged by time) plus positions now, cached gzipped under `notable-cache/`
  (51 MB, not committed). History starts 2026-09-11, except AJSV (6.6 days) and Kyn008 (16.5 days), which were
  capped at 120k rows to spare Polymarket. AJSV makes ~15k fills a day, and its positions reached Polymarket's
  10,000-row offset limit.
- **Replay window:** 2026-10-12 00:00 → 2026-10-14 16:00 UTC (two full UTC days plus the day so far), with 12 h of
  warm-up. Stage 1 ran over each wallet's full history, so the 6-hour state and rarity are real.

## How the replay works (and how faithful it is)

`notable_replay.py` copies production at b31049e:
- **Alert pass** (`alert_pass`), per trader:
  - 30-second rounds, each seeing trades made up to 15 s before it.
  - `_add_up`: the `pending` table, keyed by transaction hash.
  - One buy alert per trader and outcome per 6 h, sells likewise.
  - No alert at a settled price (≤ 2¢ / ≥ 98¢), and no buy alert without an open position.
- **Delivery**, per person:
  - `_deliver`/`_cluster`: groups over 6 h, or 1 h for Sports/Esports by Gamma category.
  - `_send`: the 20 cap, counted before the per-trader limit, as production does.
  - `_told` (30 days) and `_summaries`, every 3 h after the first held trade.
- **Approximations:** the price now is the trader's own latest fill price of that outcome. "Has an open position"
  and "full exit" come from holdings worked out from their trades plus positions now (`Holdings`). Whale alerts
  aren't replayed; the owner has no interests saved, so production sends them none.

**Calibration against production** (`notable_calibrate.py`, `notable-calibration.txt`). Stage 1 started at the
follow time, Oct 14 03:54 → 16:00:

| | Production | Replay |
|---|---|---|
| Alert-worthy buys (`buys` table) | 280 | 289 |
| Matched: same trader, same outcome token, first fill within 2 min | 275 (98%) | |
| Dollars on matched buys | | ratio median 1.000, 96% within 1% |
| Alerts sent before the cap | 20 (19 NM-P, 1 gopfan2) | 20 (19 NM-P, 1 gopfan2) |
| Same trader and outcome among the 20 | 16 of 20 | |
| Cap reached | 05:14 (cap message) | 04:49 |

Production's trade-to-alert lag was 22 s at the median, matching the replay's 15–45 s. The cap-time difference
comes from the 4 alerts that differ.

## 1. Notifications a day

"Now" = buy, sell and group alerts sent immediately. "sum" = summaries (each counts 1 of the 20). Full tables for
all 32 rules: `notable-results.md`.

**Owner, live follows (NM-P + gopfan2), as if followed all three days:**

| Rule | Oct 12 | Oct 13 | Oct 14 (to 16:00) |
|---|---|---|---|
| before #98 | 20 now, **cap 01:40, 272 dropped** | 20 now, **cap 02:04, 439 dropped** | 20 now, **cap 02:53, 295 dropped** |
| today (#98) | 5 now + 8 sum | 10 now + 8 sum | 7 now + 5 sum |
| new ≥0.5× or ≥2× typical (no rarity) | 19 now + 1 sum, **cap 05:51, 246 dropped** | 19 + 1, **cap 04:48, 407 dropped** | 19 + 1, **cap 04:25, 275 dropped** |
| ≥2× typical only | 18 + 2, **cap 06:25, 239 dropped** | 18 + 2, **cap 07:58, 362 dropped** | 19 + 1, **cap 05:53, 248 dropped** |
| new position only (any size) | 19 + 1, **cap 03:18, 261 dropped** | 19 + 1, **cap 03:10, 427 dropped** | 20 + 0, **cap 03:08, 290 dropped** |
| **recommended** (rarity ≤ min(3, 10/follows), gated groups) | 2 now + 8 sum | 10 now + 8 sum | 1 now + 5 sum |

**NM-P alone:** before #98, cap at 01:40–02:53 every day. Today: 5 now + 5–8 summaries. Every relative variant
without rarity hits the cap by 03:00–09:00. Recommended: 2, 6, 0 now + 5–8 summaries, nothing dropped. Rarity
budgets of 1 / 2 / 3 / 5 a day give 0 / 0–4 / 0–6 / 1–9 immediate alerts a day for NM-P.

**Hypothetical: owner + all 17 Recommended (18 follows):**

| Rule | Oct 12 | Oct 13 | Oct 14 |
|---|---|---|---|
| today (#98) | 20 now, **cap 00:32, 8,125 dropped** | **cap 00:32, 7,076 dropped** | **cap 00:16, 4,724 dropped** |
| new ≥0.5× or ≥2× (no rarity) | **cap 00:12** | **cap 00:05** | **cap 00:08** |
| rarity ≤ 3/day, groups ungated | **cap 05:22** | **cap 04:00** | **cap 04:32** |
| + gated groups | **cap 08:35** | **cap 05:31** | **cap 06:22** |
| **recommended** (budget min(3, 10/18 = 0.56) a day) | 13 now + 7 sum, cap 21:04 | 16 + 4, cap 12:43 | 17 + 3, cap 11:14 |

With 18 follows, three of them bots, nothing fits in 20 a day. Under today's rule the 20 go to whatever arrives
first after midnight, mostly $5–$20 AJSV, NM-P and 0x06dc top-ups. Under the recommended rule they go to the trades
below, and the cap moves from ~00:30 to the afternoon or evening.

## 2. Examples

Production sent these (today = #98, as it runs now; "proposed" = recommended rule). Live account first, then the
hypothetical.

| Trader | UTC | Trade | $ | vs typical | Kind | Rarity (a day this big) | today | proposed |
|---|---|---|---|---|---|---|---|---|
| NM-P | Oct 12 00:00 | buy Ravens · Spread: Ravens (-3.5) | $4 | 0.1× | top-up | 354 | **now** | summary |
| NM-P | Oct 12 00:00 | buy Ravens · Spread: Falcons (-3.5) | $910 | 13.3× | top-up | 28 | **now** | summary |
| NM-P | Oct 12 12:09 | buy Rams · Spread: Rams (-3.5) | $1,662 | 24.2× | top-up | 2.3 | summary | **now** |
| NM-P | Oct 13 04:14 | buy Yes · Atlético de Madrid win on 10-13 | $1,878 | 27.7× | new | 1.1 | summary | **now** |
| gopfan2 | Oct 13 03:09 | buy No · Dan Sullivan wins Alaska Senate race | $5,169 | 19.7× | new | 0.3 | **now** | **now** |
| gopfan2 | Oct 13 09:17 | buy No · Republicans control the Senate (top-up) | $4 | 0.0× | top-up | 1.3 | **now** | summary |
| gopfan2 | Oct 13 21:05 | buy No · Dan Sullivan wins Alaska Senate race | $2,127 | 13.7× | top-up | 0.4 | summary | **now** |
| AJSV | Oct 11 23:59 | buy No · Democrats win Texas, Michigan and Maine… | $5 | 0.6× | top-up | 3,761 | **now** | summary |
| 0x06dc | Oct 12 00:00 | buy No · Ethereum above $2,400 on O… | $7 | 0.0× | top-up | 291 | **now** | summary |
| Kch-Temp | Oct 12 12:09 | buy Bills · Spread: Rams (-3.5) | $213,594 | 17.7× | new | 0.0 | dropped (cap) | **now** |
| classified | Oct 12 09:33 | buy Yes · US x Iran ceasefire continues through Oct | $15,526 | 31.8× | top-up | 0.1 | dropped (cap) | **now** |
| Wickier | Oct 12 07:52 | buy No · Republicans control the House | $8,106 | 83.6× | new | 0.1 | dropped (cap) | **now** |
| 0xfb07 | Oct 12 02:23 | buy BNK FEARX · LoL Demacia Cup | $10,200 | 36.9× | new | 0.3 | dropped (cap) | **now** |
| Marco-Rubio | Oct 12 00:36 | sell No · US ends Iranian blockade by Dec (full exit) | $1,375 | 6.5× | exit | 0.1 | dropped (cap) | **now** |
| flawfence | Oct 13 03:35 | sell No · 2026 Balance of Power: D Senate, D House (exit) | $7,411 | 90.6× | exit | 0.4 | dropped (cap) | **now** |
| Bigggggggg | Oct 13 02:40 | sell Yes · Flávio Bolsonaro wins 2nd round (trim) | $504 | 262× | trim | 0.3 | dropped (cap) | **now** |
| Dragontree | Oct 12 20:05 | buy Yes · Israel x Iran ceasefire (group: classified + Dragontree) | $4,692 | 15.9× | top-up | 0.3 | dropped (cap) | **now (group)** |
| Lolakis | Oct 12 00:03 | buy Yes · US x Yemen ceasefire (grouped today) | $20 | 0.0× | top-up | 14.7 | **now (group)** | summary (gated) |
| dromedaryy | Oct 13 03:44 | buy Rams · Bills vs. Rams (new) | $1,560 | 1.9× | new | 1.4 | dropped (cap) | summary |

All are replayed trades. The last three hypothetical rows show the trade-offs: gated groups, and a per-trader
budget of 0.56 a day with 18 follows.

## 3. What the proposed rule might miss

Live account (NM-P + gopfan2), Oct 12–14:
- **Big buys too common for NM-P to ping:** 329 at ≥ 2× typical went into summaries, the biggest $1,610 (23×
  typical, but NM-P makes ~3 that size a day). Their biggest trades come several a day, so B decides how many of
  them you see.
- **Big top-ups:** 95 at ≥ 1.5× typical, biggest $1,571 (NM-P, "No · Exact Score Coventry 1-2 Newcastle").
- **New positions just under the 0.5× floor:** 45, all ≤ $34. Nothing important.
- **Exits and trims:** none. NM-P never sells (it holds to settlement), and gopfan2 sold 4 times in 30 days.
- **Sent now today, not by the proposed rule:** 18, the biggest $910 (NM-P spread buys just after midnight; NM-P makes
  ~28 that size a day). **Sent now by the proposed rule, not today:** 9, all $1.6–2.1k (NM-P’s biggest of the day and a $2,127 gopfan2 Alaska Senate buy).

18-trader hypothetical, with what the per-person budget costs:
- **Big top-ups summarised:** 3,929, biggest $9,309 (0x06dc, Bitcoin dip to $72k, 67× typical, 0.8/day this big:
  over the 0.56 budget). Kyn008 $9,029 (29×, 0.7/day). With 18 follows these are the real misses. The richer summary
  below names them.
- **dromedaryy $1,560 new position** (1.9× typical, 1.4/day this big) went into a summary.

## 4. Simulated summaries

Production's format (`_summary_text`). Today, live account, Oct 13 19:29:

```
More from traders you follow, since 16:29 UTC:
• NM-P: 119 more trades, 119 buys ($32,253)
Each came after that trader's 5 alerts for the day. Every trade is in your Feed.
```

Proposed rule, same format, with the last line changed (the "after 5 alerts" reason no longer applies):

```
More from traders you follow, since 16:06 UTC:
• NM-P: 132 more trades, 132 buys ($30,748)
None was unusual for that trader (in size, a new market for them, or an exit). Every trade is in your Feed.
```

A proposed richer format, 18-trader hypothetical, Oct 12 15:02 (trimmed to 4 of 11 traders):

```
More from traders you follow, since 12:02 UTC:
• AJSV: 1337 buys ($30,608)
   Biggest buy: $511 No · Will Apple be the largest company in the world by market cap on October 31, 2026?
• 0x06dc…4524: 90 buys ($46,810)
   Biggest buy: $7,203 Yes · Will the price of Bitcoin be above $76,000 on October 16?
• classified: 8 buys ($11,311) and 3 sells ($2,113)
   Biggest buy: $4,897 No · Will there be no change in Fed interest rates after the October 2026 meeting?
   Biggest sale: $1,870 Yes · Will the Fed increase interest rates by 25 bps after the October 2026 meeting?
• Lolakis: 2 buys ($3,347) and 1 sell ($995)
   Biggest buy: $2,447 No · Will the Fed increase interest rates by 25 bps after the October 2026 meeting?
None was unusual for that trader (in size, a new market for them, or an exit). Every trade is in your Feed.
```

Production's count-only line ("119 more trades ($32,253)") says nothing you can act on. Naming each trader's biggest
trade costs one line and makes the summary readable. For NM-P alone there would still be 5–8 summaries a day,
which is a lot. A longer interval (e.g. 6 h), or one summary per day after the cap, is worth considering separately.

## 5. Recommendation

**Start with:**
- **Buy, notable** = (a new position ≥ 0.5× that trader's typical buy, **or** ≥ 2× typical) **and** at most
  B = min(3, 10 / follows) of their alerts a day were this big or bigger over the last 30 days.
  - Typical = median of their alert-sized buys (fills of one outcome within 30 min added up), over 30 days or their
    last 100 if that's thinner.
- **Sell, notable** = a sale of something that trader's buy alert told you about (`_told`, unchanged), **or** a full
  exit, **or** ≥ 2× their typical sell. Exits and big sells pass the same rarity test.
- **Group** pings only when at least one member's buy is notable. Otherwise it's routine.
- **Routine** goes to the 3-hour summary from the first one. Keep the 20 cap.
  - TRADER_DAILY isn't needed for the live account (rarity already keeps NM-P to ≤ 6). Keep it as a backstop on
    notable alerts if you want a hard ceiling. The backstop variant behaves like today for NM-P (5 a day), but picks
    the 5 by size, not by order.
- The 0.5× / 2× thresholds hardly matter once rarity is on: 1.5×, 2× and 3× give the same counts for the live
  account. Rarity does the work for busy traders; 2× and the new-position floor do it for quiet ones.

**What I'm unsure about:**
- **Dollars as the size measure.** NM-P's notable buys are often "No" on an exact score at 90–97¢: big dollars, a
  small possible gain. Dollars measure what's at risk, so they're defensible, but a $1.6k 95¢ "No" may be less
  interesting than a $300 longshot. Never downgrade for price *moves* (that rule is kept: the price now plays no
  part). The price *level* at entry is a separate question worth a vote-based look.
- **Rarity needs history production doesn't keep.** It needs each followed trader's 30 days of alert sizes. For NM-P
  that's ~12k alerts. One `/activity` read of 500 trades covers ~4 hours for NM-P, so production would need a
  backfill when someone follows (≈200 paced reads for NM-P, minutes) and a rolling table after that. A 7-day window
  would cut the backfill; not tested here.
- **Sells were barely tested by real follows.** NM-P never sells. The hypothetical traders' exits and big sells look
  right (flawfence $7.4k exit, Marco-Rubio $1.4k exit) but come from scenarios nobody follows.
- **At the cap, routine trades are dropped, not summarised** (`_send` checks the cap before holding). Under a notable
  rule, holding routine trades before the cap check would keep them in the next summary. That's a small change to
  decide separately.
- **Three days, one real account.** The hypothetical scenarios show the shape; real per-person budgets depend on what
  people follow.

**What to measure after shipping** (all already counted, per alert, in `/api/metrics`; also tag each alert with why
it went out: new, big, exit, told, group, so the counts split by reason):
- Useful / Not useful votes per reason, and per trader frequency band (busy vs quiet).
- `/go` taps per alert ("Check current price", "Their record"). Notable alerts should be tapped more than today's
  first-five.
- "Stop alerts" taps and unfollows within 7 days of an alert, which should fall.
- Days that hit the 20 cap, and at what hour. "Open your Feed" taps from summaries show whether routine trades are
  still wanted.

## Side findings

- `pending` is keyed by transaction hash (`INSERT OR IGNORE`), so a second fill in the same transaction isn't added to
  an alert's dollars. 0–9% of fills a trader makes (NM-P: 507 of 53,421). Matching production, the replay does the
  same.
- On Oct 14 the cap was reached at 05:14. `held` was empty and no summaries were sent that day: once
  the cap is reached nothing is held.

## Rebuild

```
uv run python research/notable_pull.py --since 2026-09-11 --replay-from 2026-10-11 <wallets…>    # ~20 min, paced
uv run python research/notable_replay.py --follows research/notable-scenarios.json \
    --from 2026-10-12 --to 2026-10-14T16:00 --json research/notable-results.json             # ~6 min, no reads
uv run python research/notable_report.py research/notable-results.json \
    --rule "new≥0.5× big≥2× rare≤min(3,10/follows) +exits gated-groups" --also "new≥0.5× big≥2×" > research/notable-results.md
uv run python research/notable_calibrate.py <read-only dump of alerts.db as JSON> --from 2026-10-14T03:54 --to 2026-10-14T16:00
```

The wallets are those in `notable-scenarios.json` (the cache is pulled with `--max-rows 120000`). Pulling now gives
newer trades, so re-run the replay with a later `--to`.

## Follow-up (2026-10-15)

The owner asked for 7- vs 30-day rarity, no penalty for following more traders, fewer and richer summaries, nothing dropped at the cap, and buys / sells / groups kept apart. Answers and a V1 proposal with API costs: `notable-v1-proposal.md` (script `notable_v1.py`, output `notable-v1-results.md`). This supersedes the recommendation above: the per-follows budget `min(3, 10 / follows)` is dropped for a fixed B = 3 per trader, and the cap is split 16 immediate + 4 summaries, so nothing is dropped.

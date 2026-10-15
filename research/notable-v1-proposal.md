# Notable alerts V1: proposal (research only, no production change)

Follow-up to `notable-findings.md`, after the owner's review on 2026-10-14. It answers five questions:
- 7 vs 30 days of rarity;
- no penalty for following more traders;
- fewer, richer summaries;
- nothing dropped at the cap;
- buys, sells and groups kept apart.

Every number comes from `notable_v1.py` (output `notable-v1-results.md`): real trades from Oct 12 to Oct 14 16:00
UTC, run through production's alert pipeline. The "+17 Recommended" scenarios are hypothetical. The goal stays
useful trader intelligence: the rule looks only at what the trader did (size, novelty, exits, agreement). Nothing
in it depends on price moves, profit or copyability.

## Answers

**1. 7 days vs 30 days.**
- Rarity can be computed from a trader's own trades, with no replay of the alert pass. I call the unit an "alert
  unit": fills of one outcome within 60 s, a new unit only 6 h after the last, never at a settled price. Over 30 days,
  units agree with the full alert pass on 203 of 221 notable trades, and add only 2 of their own. The 60-second
  window matters: summing 30 minutes of fills made units bigger than the alerts they're compared against, and NM-P
  dropped to 0 notable trades.
- 7 days vs 30 days of units: they agree on 168 trades; 7 days adds 19 and misses 53. The difference is bursts.
  classified spent the week placing $4–13k Iran and Fed bets several times a day. On 7 days those are their new
  normal (7.5 notable buys a day instead of 12.8); on 30 days they still stand out.
- Backfill cost when someone follows a trader: 7 days takes 2–4 reads for most traders, 23 for NM-P and 150 for
  AJSV (10.6k trades a day). 30 days takes 94 for NM-P and 613 for AJSV. For all 18 traders: 266 reads vs 1,014.
- **Proposal:** backfill 7 days, then keep each trader's units for up to 30 days. A newly followed trader starts on
  7 days and reaches 30 after three weeks, at the 7-day cost.

**2. Following more traders isn't penalized.** B is fixed per trader: at most 3 alerts a day this big for *that*
trader. #106's `min(3, 10 ÷ follows)` would have called 141 trades routine in the 18-follow scenario, including
Kch-Temp's $130,968 "Arsenal to win" buy, classified's $13,121 Fed buy and 0xfb07's $12,300 Galatasaray spread.
With a fixed B, those either ping or lead the next summary.

**3. Fewer, richer summaries.**

| Summaries | A day | Your account: summary length | 18 follows: summary length (max) | Wait for an unusual trade past the limit |
|---|---|---|---|---|
| every 3 h | ≤ 8 | ~230 chars | 3,043 | median 1.7 h, max 3 h |
| **every 6 h** | **≤ 4** | ~230 chars | 3,431 | **median 3.1 h, max 6 h** |
| every 12 h | ≤ 2 | ~310 chars | 3,634 | median 5.2 h, max 12 h |

Every 6 hours halves the summaries and keeps the wait for a missed unusual trade within 6 h. Every 12 hours is too
slow for unusual trades. All of these fit Telegram's 4,096 characters. The format:

```
Since 12:04 UTC, from traders you follow:
Unusual for them, past today's alert limit
• classified bought Yes at 82¢ ($13,121, new position) · Will there be no change in Fed interest rates after the Oct…
• 0xfb07…e2cc bought Galatasaray SK at 41¢ ($12,300, new position) · Spread: FC Barcelona (-1.5)
• classified sold Yes at 81¢ ($11,302, 51× their usual sell) · Will there be no change in Fed interest rates after the Oct…
  (at most 8, biggest first, then "+28 more unusual trades in your Feed")
Buys
• NM-P: 144 buys, $27,145. Largest $1,645 No · Exact Score: Racing Club de Lens 0 - 0 Sporting…
• 0x06dc…4524: 82 buys, $25,216. Largest $4,591 No · Will Bitcoin dip to $78,000 October 12-18?
Sells
• Lolakis: 7 sells, $1,959. Largest $963 Yes · US x Iran ceasefire continues through October 1… (exited)
Bought by 2+ traders you follow
• classified + AJSV: Yes · US-Iran Hormuz Agreement by October 31?
Every trade is in your Feed.
```

For your account, a summary is two lines: NM-P's count, dollars and largest buy, plus gopfan2's.

**4. Nothing dropped at the cap.**
- The 20 a day becomes **16 immediate alerts + up to 4 summaries**.
- A notable trade that arrives after the 16 goes at the top of the next summary, marked "unusual for them".
- Routine trades are always held for the summary, including after the limit. Production currently checks the cap
  first and drops them.
- Result: 0 dropped in every scenario. Today's rule drops 4,365–8,125 trades a day with 16–18 follows.

**5. Buys, sells and groups stay distinct.** They are separate alert kinds (as today: `buy`, `sell`, `group`),
separate sections in the summary, and separate counts. A group pings only when one member's buy is notable on its
own; otherwise it's a line under "Bought by 2+ traders you follow".

## Results for the recommended V1

V1: 7-day alert units (growing to 30), B = 3, 6-hour summaries, 16 immediate alerts. Immediate buys / sells /
groups per day, then summaries:

| Account | Oct 12 | Oct 13 | Oct 14 (to 16:00) |
|---|---|---|---|
| **Yours (NM-P + gopfan2)**, today's rule | 5 / 0 / 0 + 8 summaries | 10 / 0 / 0 + 8 | 7 / 0 / 0 + 5 |
| **Yours**, V1 | 1 / 0 / 0 + 4 | 8 / 0 / 0 + 4 | 1 / 0 / 0 + 3 |
| 18 follows (hypothetical), today's rule | 16 / 2 / 2, **8,125 dropped** | 15 / 3 / 2, **7,076 dropped** | 16 / 3 / 1, **4,724 dropped** |
| 18 follows (hypothetical), V1 | 4 / 9 / 3 + 4 (53 unusual in summaries) | 7 / 5 / 4 + 4 (74) | 7 / 4 / 5 + 3 (54) |

Why each went out (18 follows): new positions, 8–35× the trader's usual size, full exits, sales of something we
alerted, and gated groups. No trader got more than 5 immediate alerts in a day, so TRADER_DAILY = 5 can stay as a
backstop that this data never reached.

## The rule

```
unit      = a trader's fills of one outcome within 60 s; the next unit of it only 6 h later; none at ≤ 2¢ or ≥ 98¢
typical   = median of their units on that side (buys or sells) in the window
rare      = they made ≤ 3 units a day at least this big in the window (7 days at first, up to 30)
buy  now  = rare AND (new position ≥ 0.5× typical OR ≥ 2× typical)
           new position = no buy unit of this outcome in the window, and not among their open positions as read
                          the round before (the poller keeps them)
sell now  = sold something their buy alert told you about (_told, as today)
           OR rare AND (full exit OR ≥ 2× typical sell)
group now = 2+ followed traders on one outcome (GROUP_S / GROUP_FAST_S, as today) AND one member's buy is "buy now"
otherwise = into the summary
limits    = 16 immediate a day per person (past that, unusual trades go to the top of the summary);
            summaries every 6 h after the first held trade, at most 4 a day; TRADER_DAILY 5 kept as a backstop
```

## Implementation (`signals/app.py`, when approved)

1. **New table** `units (trader, side, outcome, start, usd, PRIMARY KEY (trader, side, outcome, start))` in
   alerts.db. Rows older than 30 days are pruned on each pass.
2. **Fill it from what the alert pass already reads.** In `alert_pass`, each new fill of a followed trader extends or
   starts a unit (same 60 s / 6 h rule). This needs no new Polymarket read.
3. **Backfill on follow.** When a trader with no units is first watched, page `/activity?type=TRADE` back 7 days
   (500 rows a read, by time). Use the `background` client and its `BACKGROUND_RATE` budget, the one the directory
   already uses. Until it finishes, that trader's alerts follow today's rule.
4. **Classify** in `_deliver` (buys and groups) and before `_send` for sells, from `units`. Record why in a new
   `alerts.why` column (new, big, exit, told, group), so `/api/metrics` can split votes, taps and stops by reason.
5. **`_send`:** hold routine trades *before* the cap check. The cap becomes 16 for buy/sell/group plus 4 summaries;
   a notable trade past 16 is held with `notable = 1`.
6. **`_summaries`:** every 6 h, sections as above, "unusual" rows first. A summary is no longer dropped at the cap,
   because its slots are reserved.
7. **Copy:** the welcome text and limit message describe the new rule (`CONNECTED`, `CAP_TEXT`).
8. **Tests** in `tests/test_alerts.py`: unit building, the backfill pager, classification, the 16+4 split, hold
   before cap, the summary layout and the 4,096 trim. Keep the at-most-once delivery rules as they are.
9. **Optional fix found on the way:** `pending` is keyed by transaction hash, so a second fill in one transaction
   isn't added (1–9% of fills). Key it by (transaction, token, fill index) instead.

## API cost

| | Polymarket reads |
|---|---|
| Steady state | **None extra.** Units come from the trades the poller already reads every 30 s. |
| Backfill per newly followed trader (7 days) | 2–4 for most traders; NM-P 23; heavy bots up to ~150 (AJSV). At 2–4 reads/s on the background budget: seconds to ~1 min. |
| Your two follows today (one-off) | NM-P 23 + gopfan2 3 = **26 reads** |
| Someone following all 18 traders tested | **266 reads** one-off (30 days would be 1,014) |
| After downtime longer than one 500-trade gap read | Page `/activity` from the last unit's time: the same cost as a backfill of that span |
| Storage | One row per unit: ~410 a day for NM-P; ~150k rows for 18 traders × 30 days |

No change to the Gamma, Layer or Telegram calls. Telegram messages per person stay at most 20 a day (16 + 4).

## What I'd measure after shipping

- Useful / Not useful votes by reason (`alerts.why`), and for busy vs quiet traders.
- `/go` taps per immediate alert and per summary. "Open your Feed" taps on summaries show whether the unusual
  section gets read.
- "Stop alerts" taps and unfollows within 7 days of an alert.
- How often the 16 is reached, and the wait for unusual trades past it. If it's reached often, the next lever is B
  per trader, not a smaller share per follow.

## Open questions

- **B = 3 a day.** Your account gets 1–8 immediate alerts a day with it. B = 2 gave 0–7.
- **Dollars at 90–97¢.** Big "No" bets near certainty still count as big. A vote-based look after launch is cheaper
  than guessing now.
- **Bursts on 7 days.** classified's week of large bets stops pinging once it becomes their normal. That's the
  intended behaviour of "unusual for them"; the 30-day window, reached after three weeks, softens it.

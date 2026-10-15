# Alerts: the simple plan

Replaces the "unusual for that trader" rule in `notable-v1-proposal.md`. Owner, 2026-10-16: follow a trader, get
every alert, and change it if you want fewer. Nothing is built yet.

## The plan

1. **Every trade by default.** Following a trader sends each of their trades. Today's tidying stays: buys of one
   outcome within 30 minutes come as one alert, one per outcome per 6 hours, nothing at a settled price.
2. **Per-trader choice,** next to Follow and in each alert's buttons:
   - **All trades** (default)
   - **Only bets over $___**
   - **Summary only** (one message every 6 hours)

   Trades that don't ping are still in the Feed, and in the 6-hour summary if you want one.
3. **A warning for very busy traders.** When you follow someone who trades more than ~20 times a day, Signals says
   so and suggests an amount:

   > NM-P trades about 440 times a day. Get every trade, or only their biggest, about 3 a day (bets over $1,700)?

   The amount comes from a one-time look at their last 7 days of trades, done only for busy traders.

## Numbers (from `alerts-simple-numbers.md`, 7 days to Oct 14)

| Trader | Every trade, a day | Suggested "over $X" (~3 a day) |
|---|---|---|
| NM-P (you follow) | ~440 | $1,679 |
| gopfan2 (you follow) | ~1 | not needed |
| AJSV | ~6,300 | $404 |
| 0x06dc…4524 | ~380 | $5,829 |
| classified | ~76 | $4,284 |
| Most others (9 of 18) | 2–11 | not needed |

The suggestion needs the 7-day look: one quick read (their last 500 trades) gave $10,285 for classified and $8,450
for Bigggggggg, against $4,284 and $1,857 from 7 days.

## Cost

- **Steady state:** no extra Polymarket reads. Signals already reads every followed trader's trades.
- **Following a busy trader:** a one-time 7-day look, 23 reads for NM-P and ~150 for the very busiest. Seconds to
  about a minute on the existing background budget. Quiet traders need none.
- **Telegram:** a follower who keeps "All trades" on a very busy trader gets a lot of messages; see the open question.

## Decided against

- **The hidden "unusual for them" rule.** It works, but users can't see or change it. Its one useful piece, the
  suggested amount, moves into the warning.
- **Today's "first 5 a day per trader".** It keeps whichever trades come first, not the ones you care about.

## Daily limit (decided by the owner, 2026-10-16)

**Raised from 20 to 200 alerts a day per person,** as protection against runaway volume only. The per-trader
choice and the busy-trader warning do the real work. Today's "first 5 a day per trader" limit goes. At 200 the
existing "limit reached, more tomorrow" message still goes out once. Someone who keeps every NM-P trade (~440 a day)
would still reach it, and the warning should say so.

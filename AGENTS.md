# This repo

Signals, on the uselayer SDK: signal discovery and trading intelligence, not a copy-trading app. Top traders on
Polymarket (Kalshi too when switched on, see below), a page per trader, cross-venue links (with Kalshi on), and
for every open trade what buying it now would really cost. It runs on the user's
machine (`uv run signals`) and is hosted at usesignals.app: the same engine with `SIGNALS_HOSTED=1` on Railway,
behind Vercel (see README, "Hosting").

- All venue reads, links and copies go through `client.whales` in the SDK. Don't add venue calls or matching
  logic here; put it in the SDK. One approved stopgap: `_open_positions` (the Feed's poller) calls the SDK's
  internals for a positions-only read until uselayer has a public one (uselayer-sdk#23); don't add others.
- Signals is Polymarket-only for now; the SDK stays multi-venue. `SIGNALS_VENUES` (`VENUES` in `app.py`, default
  `polymarket`; `polymarket,kalshi` adds Kalshi back) is the one switch: every read passes it to the SDK
  (`big_trades(venues=…)`, `top(venue=…)`), and a venue that's off is refused, never read and then hidden. The
  page gets it from `/api/config` and hides Kalshi's views (Top profit's venue choice, Kalshi trader pages,
  "Same person on the other venue?") while it's off.
- Keys come from the environment (hosted: Railway's service variables), are never logged, returned by the
  engine or written to disk.
- Trader directory (README, "Trader directory"): wallets found, and where, in the store's `directory.db`, with
  each one's evaluation (status, last scored, last active) and every score worked out. Found is not checked and
  not recommended, and a kept score is a record, not a pick: never say a found wallet is any good, and scoring and
  its thresholds stay as they are. Traders shows the directory only with `SIGNALS_DISCOVERY=directory` (phase 5,
  the owner's flag; `check`, the default, is the Worth following check's own 200): its latest fresh scores in the
  check's shape and order, through `_worth_load`, so the Feed, alerts and trader pages follow. At most
  `SIGNALS_DISCOVERY_LIMIT` (the page and the Feed's poller stay their size); Explore is never limited by it. The check keeps
  running beside it, so going back is one switch; `/api/discovery` compares the two (the owner's). Keeping a score
  costs no reads: it's written where a score is already made (`_worth_run`, `_score_of`, the launch import), and
  a score never replaces a newer one. It keeps wallets only (a follow keeps the wallet, never who follows). Its
  sweep reads through the SDK (`find_wallets`) at `DIRECTORY_RATE`, below the per-host limit with room for the
  Feed's poller, and holds while the Worth following check runs. Scoring a wallet on the spot ("Check now", a
  trader page) reads through `checker()`, on its own budget (`CHECK_RATE`, 3 a second); never through `reads`
  directly, so manual checks can't take the Feed's share. The background's and the checks' budgets give way (half
  rate) while the poller reads followed traders, so alerts come first. Pages and the check never write to it
  themselves: `_directory_note_later` and `_directory_later` (the writer thread; a failed write is never theirs).
- Counting (README, "Counting"): per alert and per follow, in the store's `alerts.db`, never anything personal
  beyond the signed-in account (no IPs, no user agents; an interest pick is counted with no id at all). Quotes
  (`quotes`: how each ended, Polymarket reads, milliseconds) and pages a wallet with no Polymarket profile stopped
  (`no_profile`) are counted with no id, wallet or trade at all: watch reads per quote, latency and rate limits
  there as traffic grows. Alert
  links go through `/go/<alert id>/<target>`, which redirects to Polymarket or Signals only, and still redirects
  when counting fails. `/api/metrics` is the owner's: locally, or with `SIGNALS_METRICS_KEY`; never public.
- Analytics (README, "Analytics"): PostHog on the page, off unless `SIGNALS_POSTHOG_KEY` is set, sent through
  `/lyr` (`web/vercel.json`). A signed-in person is identified by their Clerk user id only: never send PostHog a
  name, email, Telegram account or anything from `/api/me`, and keep fields and Clerk's menus masked in replays.
  It's for seeing how the page is used; Counting (above) stays the record of alerts and follows.
- Signals is a consumer website (usesignals.app); running it locally is for development. It places no orders:
  no sandbox, no in-app trading, no automatic copying, and it never holds anyone's keys or funds. Buying means
  "What buying now costs", then "Buy on Polymarket ↗", where the user buys it themselves. A session never
  places a real-money order, not even a test one.
- "What buying now costs" (`/api/trade/preview`, the page's `costBox`) is on every trade with a market,
  whoever made it: a quote only, never an order. Whether copying a trader is offered (`copy_offer`) is a separate
  record shown beside it, never a gate on it, and pricing a trade reads no score. The engine reads everything it
  shows, never trusting a price from the page: whether the market is closed or not taking orders comes from
  Polymarket's market status (the SDK's `preview_copy`), never from the price, and the amount is priced from
  the cheapest price for sale now, up to `BUY_LIMIT` (3¢) above it, a limit the page discloses. It shows the
  estimated cost, what's for sale within the limit, the fee, the estimated slippage and when the book was read
  (and that their trade's time is when it was made on Polymarket). Their price is context ("They paid 37¢, 12m ago, for context"),
  never a target. It's always an estimate: never imply a guaranteed fill (it may fill higher, in part, or not
  at all), and no green or "you can still get in" in it. A quote that fails shows no price: when the book moved
  between the reads (`price_changed`) it says "Price changed—refresh quote", never that nothing is for sale;
  "Only $X is for sale" is only for an amount bigger than the book holds within the limit. Transparency over a
  quote that always succeeds.
- Traders (the leaderboard, Top profit) and trader pages need no account; everything in the Feed (Following,
  Recommended, Big trades, an alert's "Check your price") is behind signing in (Clerk: Google, or Telegram as a
  custom OpenID Connect provider). Signed out, a Feed page is the sign-in page ("Sign in to see the Feed", Sign in,
  Browse traders), and the engine refuses its reads (`feed_reader`: `/api/live`, `/api/trading-now`, `/api/bet`)
  without a valid session. Without Clerk's key (running locally) nothing is gated. On a trader's page the Follow
  button says why you'd sign in ("Follow · get an alert after they trade"). Alerts go
  to Telegram (`alert_pass` in `app.py`). Signals is trader discovery and trading intelligence first, copying
  second: following is research, so every buy by a trader you follow alerts you, whatever the amount and whoever
  the trader (their buys of one outcome within 30 minutes add up, across rounds and restarts, shown as the total
  at the average price). Every alert gives the same facts for every trader (`_trade_text`): who bought which
  outcome at what price and for how much, the market, the price now, and when they traded with how long before
  the alert that was, measured ("Traded 14:02 UTC, 6 min before this alert."), never "just now" or "live".
  Outside the "can still get in" range it says "Now 60¢, price already moved.", whoever the trader. Copying comes
  after the facts, in one italic line (`COPY_LINE`), only for a trader copying is offered for (`copy_ok`) and
  only while the price is still in range; never a cost in the alert: costs, the fill, liquidity and risks are the
  trade page's. Every alert links "Check current price" (`#/check/<wallet>/<market>`, the trade's own page,
  neutral for every trader), and "Buy on Polymarket", which only opens the market. Sells by a trader you follow
  alert too, whatever the amount (`_sell_text`): who sold which outcome at what price and for how much, the market
  and when, measured; while they still hold some, the price now and "Check current price"; once they hold none,
  "They no longer hold it." and no price (never a made-up one). A sell is information only: never "sell too",
  never a copy line, never grouped, "Open on Polymarket" for the market. No alert at all when the price
  now is settled (2¢ or less, 98¢ or more), or a sell's price is. A restart (a deploy) neither skips nor repeats one:
  what each round looked at is kept in alerts.db (`watched`, `seen_trades`), so a trade made while it was down is
  alerted once after; only a new follow's existing trades are just noted. Delivery (`_send`, `_attempt`): each
  alert is kept with its message before Telegram is asked, and never sent twice. It's tried again (after Telegram's
  wait or 30 s, 1, 2, 4 min, for up to 30 minutes, after a restart too) only when Telegram surely didn't take it:
  it was never reached, or it said 429. When it may have (a 5xx, no answer once the request went out, the engine
  stopping mid-request) it's recorded as unknown, with why, never asked again. A refusal fails it, and a chat that
  blocked the bot is disconnected. Every trade of a trader you follow alerts unless you chose fewer for them
  (`alert_for` in follows.json: only bets over an amount, or a summary every 6 hours; `_wants`), and a sale of
  something their alert told you about always comes. Following a busy trader (over 20 alerts a day over the past
  week: `_measure_pace`, kept in alerts.db `pace`) says so with an amount about 3 of their bets a day reached, worded
  as an estimate from last week, never a promise. At most 200 alerts per person per UTC day (a safety limit), then
  one message that says when alerts resume (00:00 UTC, and how long until then) and nothing until the next day. When 2+ traders you follow buy the same outcome, each within 6 hours of the one before (1 hour in
  Sports and Esports), the second buy sends one grouped alert (each one's price and buy time, the price now, how
  long before the alert the latest buy was) and later buys edit it in place; it counts once. Every alert has
  "Useful / Not useful" (one vote per alert and account, changeable). Big trades ($10,000 or more) in your chosen
  markets alert you whoever the trader, with the same facts, at most 5
  a day within the daily cap. Only a choice made where the page asks (onboarding, or the Feed's "Your markets · Change")
  is kept on the account (per account like follows); Traders' chips only filter the view, and nothing saved or []
  (Skip, all markets) means no big-trade alerts. For a trader copying isn't offered for, an alert is never
  "you can still get in" and never green.
  Every alert and trade card keeps the link to the market, whoever the trader. Every alert, and the Feed, says
  how late it can be for where its trades come from, never a blanket figure and never "instant": a trader you
  follow is read on their own about every 30 seconds (Polymarket returns their trades there within seconds), so
  their alerts say "Signals typically reads followed traders' trades every 30 seconds" (`FOLLOW_DELAY`); big trades
  come from Polymarket's feed of every trade, which typically runs 3–5 minutes behind (`BIG_DELAY`, and the Feed's Big
  trades). A trade's time is its Polymarket timestamp (its transaction's block time), and every alert measures
  from it ("Traded 14:02 UTC, 6 min before this alert."). The engine checks Clerk's session token itself (public keys,
  no Clerk secret). Never let one account see or change another's follows, and never let an answer about a
  signed-in person be cached.
- Deploys and changes to the hosting (Railway, Vercel, the domain) only when the owner asks.
- Never say a link *is* someone: show the tier ("likely" / "possible") and the evidence.
- The page is a consumer app, not an analyst's spreadsheet. It helps people find traders worth following and
  understand why: who they are, how they trade, and what the evidence does and doesn't show. Before anyone buys,
  show what it would really cost them now (above): what their amount fills at, the fee, the slippage, what's for
  sale. Nothing on the page may imply that copying makes money. Two tabs (Feed, Traders).
  - Onboarding (first visit, once per browser) asks one question: which markets interest you (Politics, Sports,
    Crypto, Esports; Skip means all markets). Nothing else: no budget, goals or signup. The choice is kept in
    localStorage (and, when picked here or with the Feed's Change, on the account for big-trade alerts), every read and write in try/catch; if storage is blocked it holds for the visit, and no one is
    ever stuck on that screen.
  - Three separate readings, never merged: profit (track record), skill (the tier) and copying (copy status).
    The tier is skill and track record only (uselayer 0.10's `segment`; a kept older result is re-tiered with the
    SDK's `segment_of`): copying never moves a trader into, out of or between tiers or sections. A trader can be
    worth following for research when copying them doesn't pay, but nothing may present a trader's activity as a
    tested way to make money: following and the Feed are for learning what traders do, not a signal to buy
    (`NOT_A_TIP`).
  - Copying in plain words, for someone who has never heard of copy trading: say "buying after them" (what
    would you have made if you'd bought the same thing after them?), never "copies" as a thing that keeps,
    loses or gets in, and no "95% range", "ask" or "estimate" jargon on the page ("range", "the price you'd
    actually pay"). The four status names stay as they are.
  - Traders lists the traders we recommend (Proven sharp, Quiet sharp, Rising) in two sections by tier, from the
    engine's `section_of`: **Sharps** (Proven and Quiet) and **Rising**. Each section has the same columns, its
    own ranks and "Show more"; "Copyable only" filters on copy status (greyed out, with the "Copying is paused"
    callout above the list, while nobody is Copyable), and "See all checked traders" is one flat list. A
    trader's copy status is one of four words, the same everywhere (the engine's `copy_status`, carried as
    `copy_label` with `copy_ok`, and the page's `copyLabel`): **Copyable** (a recommended tier, and copies
    reliably above zero after fees), **Costly to copy** (the whole 95% range of what copies kept is below
    zero), **Copy not proven** (it isn't reliably above zero, or too few to tell) or **Not offered** (copies
    kept money, but the trader's own record doesn't pass the checks); a reason reads "Costly to copy: …". A
    trader who isn't Copyable gets no copy
    cue anywhere: their status in grey in the Copying column, and an open trade of theirs (their page, a Feed
    card, a trader page) says only "Now 52¢ · they paid 49¢", never green or "you can still get in". The
    "Buy on Polymarket" link, which only opens the market, "Check your price" and "What buying now costs" stay
    for everyone: neutral market data, not a copy cue. Every figure names its period: only profit and trend follow the 7D/30D/90D switch; consistency is 12 weeks,
    win record and usual trade the last 30 days. Consistency is "X of N weeks up", where a week counts profit
    taken plus the change in value of trades still open. A copy estimate prices the copy at the ask (uselayer
    0.8.1's `copy_entry`), never the middle price. A positive copy estimate that isn't reliably above zero says so
    ("If you'd bought after them: +0.7% after fees (range −1.2% to +2.6%), but we can't tell that from luck"),
    never shown as a gain.
    One bar: filters on the left (market chips = the onboarding choice, Copyable only), how you view the list on
    the right (search name or wallet, period, sort). Columns are grouped as Track record, Style and Copying.
    Recommendations never include Big profit, edge unproven; Profit rests on one win (big profit that's a loss
    without their biggest win: the only tier that says luck); Edge not proven; Not enough data (too few trades
    could be checked: never shown as a verdict on skill); or No view traders, and the checks are never loosened to
    fill the list: an empty result says so and names the interests that had no one. "See all checked traders" may
    show the rest, each marked.
  - Never a made-up number for a real trader on the hosted app. A figure the data doesn't have yet shows "—";
    sample values (`SIGNALS_MOCK=1`) are for reviewing the page locally only. "Copyable" is plain text: green
    stays for "you can still get in". The SDK's per-dollar figure is a return per dollar traded, never "ROI".
  - Words: say "trade", never "bet"; never "sandbox" or "paper". Every trade with a market has "Check your
    price", which opens what buying now costs; there is no "Copy trader". Following a trader (alerts) is how you
    hear about their next trades.
  - One idea per card. The market title is the biggest text; status is one line with a dot.
  - Color means something: green = you can still get in, grey = moved away, neutral = settled. Nothing else is
    colored.
  - Plain words, sentence case, one font (Host Grotesk, as on Layer's site), tabular numbers, whole cents (tenths
    only under 1¢ or when two prices would look equal). No UPPERCASE headers, no tooltip-on-header help.
  - Methodology (tests, z-scores, margins) stays out of the main view: in "How we pick traders" or a Details fold.
  - No wallet addresses on screen: unnamed traders get "Trader 0x91…76" and an identicon.
  - Must look good at 390px as well as desktop. One width: the header, Feed, Traders and trader profiles share
    the 1200px container (Top profit keeps a 680px reading column), so the account sits at the header's right and
    nothing moves between tabs. Feed cards are a grid: three across on desktop, one on a phone. Keep loading,
    empty and error states.
  - The Feed has two parts. **Following** (`#/`, the default, `/api/me/feed`): every trade from the last day of
    the traders you follow, whoever they are (following is also research). **Recommended** (`#/recommended`, `/api/live`): trades of $1,000 or more (`RECOMMENDED_MIN_USD`) from
    the traders in Sharps and Rising, with neutral cards for anyone not copy_ok. Recommended
    stays quiet (fewer, bigger trades) and never more prominent than Following: second in the switch, never the
    default. Both keep the switch "Only trades I can still get into", one card per outcome with "Also bought
    by", and the delay line. Big trades (`#/big`) is linked from the foot of Recommended. Traders' trades come from
    one background poller (`_live_loop`, one read at a time, in 30 s rounds: everyone followed each round,
    Recommended's every 60 s while the round's slot allows, oldest first, so a bigger pool is read less often,
    never faster). Per trader it reads their trades, and their open positions only when there's a trade from the
    last day to price (kept while their trades don't change, for 90 s); never the profile or stats. Polymarket's
    shared $1,000+ trades feed (one read for everyone) moves a Recommended trader who just traded to the front.
    Pages only read what it keeps, so a busy Feed never adds Polymarket reads: a view reads at most 3 traders the
    poller hasn't read yet itself ("Still reading N traders"), and "Updated" is when the oldest of it was read.
    The Feed never says "None of … traded" for traders it hasn't read: it says it's still reading them, or that
    they couldn't be read, and notes when some of it was last read over 5 minutes ago. Everyone followed is read
    once every round however many there are (Recommended keeps a floor of 4 reads a round); alerts read what the
    poller keeps, never on their own; a gap of more than 100 trades between reads is read further back (500);
    a rate limit ends the round.
  - Market chips live on Traders only. The Feed follows the same choice and says so in one line ("Your markets:
    Politics, Sports · Change"); Change opens the onboarding picker. Never a second set of filter chips.
  - A trade at 2¢ or less, 98¢ or more, or no longer held is settled (Won / Lost / Closed), never a bargain
    (`bet_status` in `app.py`). "You can still get in" only from 5¢ below to 3¢ above what they paid; further
    either way is "Price moved away" (a big drop means the market turned against them, not a bargain).
- Pull requests: sessions never merge. Only the owner merges, when they say "merge #N".

## Deploying (only when the owner asks)

- **Engine** (Railway project and service `signals`): `railway up --service signals` from the repo root. It
  builds the `Dockerfile`; the store is on the service's volume at `/data`. A deploy restarts the server, and a
  Worth following check that hasn't finished starts over (20–30 min).
- **From a worktree** (`.claude/worktrees/…`), run `railway up . --path-as-root --service signals`. Without
  `--path-as-root` it uploads the linked folder (the owner's main checkout), which can be stale: this briefly
  put old code live on 12 Oct.
- **After any deploy**, check the live page actually serves the new code: fetch https://usesignals.app and look
  for something only the new code has.
- **Forwarding layer** (Vercel project `signals`, holds usesignals.app): `vercel deploy --prod` inside `web/`.
  Only needed when `web/vercel.json` changes.
- A new checkout or folder isn't linked yet: run `railway link` (project and service `signals`) in the repo root
  and `vercel link --project signals` in `web/` first.
- Keys are set by the owner in Railway's service variables, never by a session.
- `railway.json` works until 2026-12-01; after that Railway needs `railway config migrate`.

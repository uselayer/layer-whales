# Signals

See the best traders on Polymarket, open one to see their trades, and see what copying a trade would really
cost you before you buy it on Polymarket.
Signals is Polymarket-only for now; the SDK also covers Kalshi, which `SIGNALS_VENUES=polymarket,kalshi` turns
back on (Kalshi traders, both venues' leaderboards, and whether a trader trades on the other venue too).

Built on the [uselayer](https://uselayer.sh) Python SDK (`client.whales`). Run it on your machine, or use the
hosted version at [usesignals.app](https://usesignals.app). Either way Signals places no orders: you buy on
Polymarket yourself.

## Run it

You need [uv](https://docs.astral.sh/uv/); it fetches Python 3.11+ if you don't have it.

```sh
git clone https://github.com/uselayer/signals
cd signals
uv sync
LAYER_API_KEY=lyr_... uv run signals
```

Then open http://127.0.0.1:8790.

- `SIGNALS_VENUES` (default `polymarket`): the venues Signals reads. `polymarket,kalshi` adds Kalshi; with it
  off, Signals never calls Kalshi.
- **Layer API key** (optional, with Kalshi on): used to find the same market on the other venue, to compare two
  traders' trades. Get one at uselayer.sh.
- `SIGNALS_PORT` (default 8790) and `SIGNALS_STORE_DIR` (default `~/.uselayer/signals`) change the port and
  where the Worth following results and price history live. `SIGNALS_WORTH_WALLETS` (default 200)
  is how many wallets Worth following checks.
- `SIGNALS_MOCK=1` fills the leaderboard's profit-history columns (weeks in profit, trend, 7D/90D) with made-up
  sample values, marked as such, to review the page before the SDK provides them. Local only: ignored hosted.

## What's on the page

Signals helps you find traders worth following and understand why: who they are, how they trade, and what the
evidence does and doesn't show. Before you copy a trade, it shows what that would really cost you: the price now
against theirs, what your amount fills at, the fee. That isn't a promise of profit.

The first visit asks one question, which markets interest you (Politics, Sports, Crypto, Esports, or Skip for all
markets), then opens Traders. The choice is kept in your browser; the market chips on Traders change it, and the Feed
follows it ("Your markets: … · Change" opens the same picker), so the two always agree. Then two tabs:

- **Feed**: one switch at the top, **Following** (the default) or **Recommended**.
  - **Following**: signed in, every trade over the last 24 hours from the traders you follow, whoever they are.
    Signed out, a short prompt to sign in.
  - **Recommended**: trades of $1,000 or more over the last 24 hours from the traders in Traders' two sections
    (Sharps, Rising). Quieter on purpose, and never the default. Their trades are research, not tips: we
    haven't found that buying after them pays, and the page says so.
  - Both: one card per trade, opened on your markets, three across on a desktop: the market, their pick, what they paid, the price now, how much they put in,
    and one status line. **You can still get in** means the price is between 5¢ below and 3¢ above what they
    paid; **Price moved away** means further either way. A bigger drop means
    the market has turned against them, so buying then is a different trade, not a bargain. A trade at 2¢ or
    less, 98¢ or more, or one they no longer hold is **Won**, **Lost** or **Closed**, and drops out of the default
    feed. Turn off "Only trades I can still get into" to see them all. Tap a card for the details and the
    trader. A trade you can still get into has **Check your price**: pick an amount and it's priced from
    Polymarket's order book right now (at most 3¢ above their price), with the fee, your price per contract
    against theirs, and what you get if it wins. **Buy on Polymarket ↗** opens the market there; Signals places
    no orders. It updates every 30 seconds; Polymarket shows trades a few minutes late.
  - **Big trades** (`#/big`, linked from the foot of Recommended; the old `#/traders/big` link still works): trades of $250 or more right now on
    Polymarket, one card per trade, opened on your markets like the rest of the Feed. A named trader's card
    opens their page. With Kalshi on, it shows every big trade on both venues instead ("Anonymous trader" when
    Kalshi doesn't name them), not filtered by your markets: the SDK has no categories for Kalshi markets, and
    almost all big trades are Kalshi's.
- **Traders**: a leaderboard of the Polymarket traders we recommend, in two sections by tier: **Sharps** (Proven
  sharp, Quiet sharp) and **Rising**. The tier is skill and track record only; copying is a separate copy status
  on every trader: **Copyable**, **Copy not proven** (not reliably above zero; a small positive estimate is never
  shown as a gain), **Costly to copy** (the whole range of what copies kept is below zero: usually the price moves
  their way before a copy gets in) or **Not offered**. A Proven sharp can be costly to copy.
  - One bar: on the left the market chips (your onboarding choice) and **Copyable only**; on the right
    **Search name or wallet**, the period (7D / 30D / 90D, once the SDK provides it) and the sort: Recommended
    (the checks' order), Most consistent, Profit, Win rate, Most active, Copy result.
  - Columns, grouped: **Track record** (12-week consistency, profit, trend, won X of Y settled in the last 30
    days), **Style** (their usual trade over the last 30 days and how often they trade), **Copying** (what a copy a few minutes late made
    after fees, as a share of what it paid). On a phone each trader is one card.
  - Weeks in profit, trend, 7D/90D and the "per $ traded" return come from the SDK's profit history (uselayer
    0.6). Until then they show "—" and **Most consistent** says "coming soon"; nothing is made up.
  - A trader matches your markets when it's the category they trade most or one they're strong in. Searching a
    full wallet that isn't in the list offers **Check this wallet** (scored in up to a minute), including your
    own. An empty result says so and offers **Show all markets**; the checks are never loosened to fill it.
  - The footer counts who's left out (edge not proven, not enough data, big profit with the edge unproven or
    resting on one win, market makers, arbitrage) and **See all
    checked traders** shows everyone but market makers and arbitrage, each marked. It also links to **Top
    profit** (Polymarket's leaderboard; both venues' with Kalshi on). "How we pick traders" explains the checks,
    the tiers and copying (its own tab) in plain words.
  - Wallets come from the leaderboards and from busy markets' trades. The first check takes 20–30 minutes and
    is kept in the store dir, so later launches show the last result at once while a new check runs in the
    background (on launch if it's over 6 hours old, then every 6 hours while the app is open). Scoring is
    `client.whales.discover()` / `score()` in the SDK.
  - **A trader's profile** (from the leaderboard or a wallet search): their market and last trade, a link to their
    Polymarket profile, their tier, and their copy status under Follow (Copyable, Copy not proven, Costly to copy
    or Not offered, with "Why?"). A stat strip (profit and per $ traded for the
    period, 12-week consistency, win record and usual trade over the last 30 days, wallet; a note says the period
    switch changes only profit), a profit chart for 7D / 30D / 90D with the stretches
    below their previous high shaded, and **Why they're here**: why they're on the list, their copy status and its reason, the SDK's
    own warnings as a callout, then a short table (markets, buys at, new markets, holds, last bought). Tabs: **Positions** (open, with "You can still get in", and
    closed), **Activity** (their recent buys and sells) and **Stats** (consistency, risk, style, execution), with
    the evidence (each check, how the price moved after their buys, by category, the trades checked) under **How
    we measure this**. The wallet is their cash (read on-chain) plus open positions; anything that can't be read shows "—", never $0.
  - **A leaderboard trader's page** (Top profit, Feed → Big trades), with Kalshi on, also asks **Same person on the other venue?**: accounts that may
    be the same person, each with the evidence (the same name or X handle on an account that really trades,
    and trades in the same market, the same way, within 5 minutes). It's an inference, never a confirmed
    identity.

Traders without a name show as "Trader 0x91…76" with a generated avatar: the page never shows wallet addresses.

## Hosting

usesignals.app is the same engine, run with `SIGNALS_HOSTED=1`:

- **Railway** runs the engine from the `Dockerfile` (one process, `railway.json`), with a volume at `/data` for
  the store. Set `LAYER_API_KEY` in the service's variables; keys never reach the browser.
- **Vercel** holds the domain and forwards every request to Railway (`web/vercel.json`, deployed from `web/`).
  Caching is off there.
- **Signing in, only to follow traders** (off until its keys are set): Clerk, with Google and Telegram. The
  page loads Clerk from the Frontend API its publishable key names, and sends Clerk's session token with
  `/api/me` requests; the engine checks it against Clerk's public keys. Follows are kept in
  `/data/follows.json`, by Clerk user id. Sandboxes kept from before (in `/data/visitors/<id>`) are left on
  the volume, unread, until the owner decides to delete them.
- **Alerts** on Telegram (off until `TELEGRAM_BOT_TOKEN` is set): "Connect Telegram" on a trader's page gives a
  one-time link to the bot; Start there connects the chat. Every 30 seconds the engine reads followed traders'
  trades (once per trader) and messages each connected follower about every new buy, whatever the amount and
  whoever the trader (split buys within 30 minutes add up): who bought what, at what price, the market, the price
  now, and when they traded with how long before the alert that was. A trader copying is offered for gets one more
  line while the price is still near theirs; costs stay on the trade's page, which "Check current price" opens.
  Every alert has "Useful / Not useful" and a "Stop alerts from …" button. Two or more followed traders buying the same outcome
  make one grouped message, edited in place as more buy. Big trades ($10,000 or more, from the SDK's big trades)
  in the markets someone picked (onboarding or the Feed's Change, saved to their account; Skip means none) alert them too. At most 20 alerts per person per UTC day. What
  was sent, the buys behind grouped alerts and the votes are kept in `/data/alerts.db` (SQLite). In the chat,
  `/list` shows who you follow and `/stop` turns alerts off. The bot uses long polling: no webhook to set up.
- **Trader directory** (phase 1: finding; phase 2: keeping scores): every Polymarket wallet Signals finds, and
  where, kept across runs in `/data/directory.db` (SQLite). Once a day the engine sweeps every Polymarket leaderboard (88: profit and
  volume, four periods, overall and each category, to rank ~10,050) and open markets at every level of activity
  (the SDK's `find_wallets`, uselayer 0.9): about 18,000 reads, roughly 2.5 hours. It stays out of the Feed's way:
  at most 3 reads a second (the SDK allows 8 per Polymarket host; the Feed's poller, one trader at a time, needs
  at most ~3.3), and none while the Worth following check runs, which can use all 8. Traders in the Feed's big
  trades, wallets opened that the last check didn't have (pasted into search, mostly) and followed wallets are kept
  too, at no extra reads, by a writer thread so no page waits for the directory; a follow keeps the wallet, never
  who follows it. At full size the file is roughly 300 MB (the volume holds 5 GB).
  Each wallet also has its evaluation: a status (found, never scored; scored; or failed, the last try failed, an
  earlier score kept), when it was last scored and last tried, its last activity (the latest trade of theirs
  whose time we know: a Feed trade, or the last buy a score saw; a leaderboard or market sighting never sets
  it), and the latest score's segment and main category. Every score worked out is kept, at no extra reads: the
  Worth following check's 200, a wallet scored on its page, and a try that failed. The latest score is kept whole
  (compressed, 30 checked bets, about 6 KB a wallet), every score's headline figures stay in its history, and the
  latest score's price test in each category in its own table. A score never replaces a newer one, so a wallet a
  later check leaves out keeps its last score. On each launch the last check's scores in `worth-following.json`
  are imported, as of when that check finished: the first time adds them, after that it changes nothing. Nothing
  is shown from the directory yet: the Worth following check still picks its own 200 wallets and the page reads
  its file. `/api/directory` gives the counts (wallets, new in a day, by source and category, sweeps; scored,
  failed and never scored, by segment and category, active in the last week) to the owner only, as
  `/api/metrics`. `SIGNALS_DIRECTORY=0` turns the sweep off; scores are kept either way.
- **Counting** (is any of this useful?): per alert and per follow, in `/data/alerts.db`, with nothing personal
  beyond the signed-in account (no IPs, no user agents): minutes from their trade to the alert, taps on an
  alert's links (each goes through `/go/<alert id>/<target>`, which counts the tap and redirects: to Polymarket
  or Signals only, and even when counting fails), "Stop alerts" taps and unfollows (and how many came within 7
  days of an alert about that trader), Useful / Not useful votes, follows per visitor who picks interests (the
  pick is counted anonymously, no id at all) and followers who connected Telegram. To read the summary:
  - locally: `curl http://127.0.0.1:8790/api/metrics` (from the same machine only);
  - hosted: the owner sets `SIGNALS_METRICS_KEY` in Railway's variables, then
    `curl -H "Authorization: Bearer $SIGNALS_METRICS_KEY" https://usesignals.app/api/metrics`. Without the key
    it answers 404; with no key set it's closed. `?days=7` changes the period (default 30).
- **Limits**: "Check again" on Worth following only starts a new check when the last one is over an hour old.
  Someone can follow up to 20 traders.

### Setting up sign-in and alerts (owner)

1. **Telegram bot**: in Telegram, @BotFather → `/newbot`. Put its token in Railway as `TELEGRAM_BOT_TOKEN`.
2. **Clerk**: in the Clerk dashboard, turn on Google (a production instance needs your own Google OAuth
   credentials) and make email optional (Telegram doesn't share one). Add Telegram as a custom provider
   (SSO connections → Add connection → For all users → Custom provider): discovery endpoint
   `https://oauth.telegram.org/.well-known/openid-configuration`, with the Client ID and Client Secret from
   @BotFather → your bot → Login Widget, where you also add Clerk's authorized redirect URL as an Allowed URL.
3. **Domain**: a Clerk production instance asks for DNS records on usesignals.app (its Frontend API, such as
   `clerk.usesignals.app`); add them where the domain's DNS is managed.
4. **Railway**: set `CLERK_PUBLISHABLE_KEY` (pk_live_…). No Clerk secret key is needed. `SIGNALS_SITE`
   (default `https://usesignals.app`) is the origin sign-ins must come from.

## Good to know

- With Kalshi on: Kalshi's trader data comes from the public endpoints behind kalshi.com's Leaderboard and profile pages.
  Kalshi doesn't document them, so they can change.
- Kalshi traders choose whether to show their trades. Most leaderboard whales hide them: their page says
  so, and their trades can't be shown. Traders under **Feed → Big trades** usually show theirs.
- Polymarket's data comes from its public data API; every wallet is public.

## License

MIT. See [LICENSE](LICENSE).

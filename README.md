# Signals

See the biggest traders on Kalshi and Polymarket, open one to see their trades, check whether they trade on the
other venue too, and copy their next trades in a sandbox.

Built on the [uselayer](https://uselayer.sh) Python SDK (`client.whales`). Run it on your machine, or use the
hosted version at [usesignals.app](https://usesignals.app). Either way copies run in a sandbox (real prices and
fees, no real money).

## Run it

You need [uv](https://docs.astral.sh/uv/); it fetches Python 3.11+ if you don't have it.

```sh
git clone https://github.com/uselayer/signals
cd signals
uv sync
LAYER_API_KEY=lyr_... uv run signals
```

Then open http://127.0.0.1:8790.

- **Layer API key** (optional but recommended): used to find the same market on the other venue, to compare two
  traders' trades. Get one at uselayer.sh.
- **Kalshi key** (optional): set `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` to copy Kalshi traders. Copying
  Polymarket traders needs no key.
- `SIGNALS_PORT` (default 8790) and `SIGNALS_STORE_DIR` (default `~/.uselayer/signals`) change the port and
  where the sandbox account, copy rules and Worth following results live. `SIGNALS_WORTH_WALLETS` (default 200)
  is how many wallets Worth following checks.
- `SIGNALS_MOCK=1` fills the leaderboard's profit-history columns (weeks in profit, trend, 7D/90D) with made-up
  sample values, marked as such, to review the page before the SDK provides them. Local only: ignored hosted.

## What's on the page

Signals helps you find traders worth following and understand why: who they are, how they trade, and what the
evidence does and doesn't show. Copying a trader in the sandbox is optional, a way to test them; it isn't a
promise of profit.

The first visit asks one question, which markets interest you (Politics, Sports, Crypto, Esports, or Skip for all
markets), then opens Traders. The choice is kept in your browser; the market chips on Traders change it. Then
three tabs:

- **Feed**: new trades over the last 24 hours from the traders we rate Proven sharp, Quiet sharp or Rising, one
  card per trade: the market, their pick, what they paid, the price now, how much they put in, and one status
  line. **You can still get in** means the price is between 5¢ below and 3¢ above what they paid (3¢ is the
  same limit copying uses); **Price moved away** means further either way. A bigger drop means the market has
  turned against them, so buying then is a different trade, not a bargain. A trade at 2¢ or less, 98¢ or more, or one they no longer hold is
  **Won**, **Lost** or **Closed**, and drops out of the default feed. Filter by category, or turn off "Only
  trades I can still get into" to see them all. Tap a card for the details, the trader and a link to Polymarket.
  **Copy trader** copies that trader's next trades in the sandbox; a trade they already made isn't copied.
  It updates every 30 seconds; Polymarket shows trades a few minutes late.
- **Traders**: a leaderboard of the Polymarket traders we recommend: Proven sharp, Quiet sharp, Rising, and
  Too fast to copy (the price moves within minutes of their buys, so a copy usually comes too late).
  - One bar: on the left the market chips (your onboarding choice) and **Copyable only**; on the right
    **Search name or wallet**, the period (7D / 30D / 90D, once the SDK provides it) and the sort: Recommended
    (the checks' order), Most consistent, Profit, Win rate, Most active, Copy result.
  - Columns, grouped: **Track record** (weeks in profit out of the last 12, profit, trend, won X of Y settled),
    **Style** (their usual trade and how often they trade), **Copying** (what a copy a few minutes late made
    after fees, as a share of what it paid). On a phone each trader is one card.
  - Weeks in profit, trend, 7D/90D and the "per $ traded" return come from the SDK's profit history (uselayer
    0.6). Until then they show "—" and **Most consistent** says "coming soon"; nothing is made up.
  - A trader matches your markets when it's the category they trade most or one they're strong in. Searching a
    full wallet that isn't in the list offers **Check this wallet** (scored in up to a minute), including your
    own. An empty result says so and offers **Show all markets**; the checks are never loosened to fill it.
  - The footer counts who's left out (Lucky traders, market makers, arbitrage, no clear edge) and **See all
    checked traders** shows everyone but market makers and arbitrage, each marked. It also links to **Top
    profit** (both venues' leaderboards) and **Big trades** (trades of $250 or more right now). "How we pick
    traders" explains the checks in plain words.
  - Wallets come from the leaderboards and from busy markets' trades. The first check takes 20–30 minutes and
    is kept in the store dir, so later launches show the last result at once while a new check runs in the
    background (on launch if it's over 6 hours old, then every 6 hours while the app is open). Scoring is
    `client.whales.discover()` / `score()` in the SDK.
  - **A trader's profile** (from the leaderboard or a wallet search): their market and last trade, a link to their
    Polymarket profile, and **Copy trader** when they're copyable. A stat strip (profit and per $ traded for the
    period, weeks in profit, won X of Y, usual trade, wallet), a profit chart for 7D / 30D / 90D with the stretches
    below their previous high shaded, and **Why they're here** in three plain lines (consistent, how they trade,
    can you copy them) plus the SDK's own warnings. Tabs: **Positions** (open, with "You can still get in", and
    closed), **Activity** (their recent buys and sells) and **Stats** (consistency, risk, style, execution), with
    the evidence (each check, how the price moved after their buys, by category, the trades checked) under **How
    we measure this**. The wallet is their cash (read on-chain) plus open positions; anything that can't be read shows "—", never $0.
  - **A leaderboard trader's page** (Top profit, Big trades) also asks **Same person on the other venue?**: accounts that may
    be the same person, each with the evidence (the same name or X handle on an account that really trades,
    and trades in the same market, the same way, within 5 minutes). It's an inference, never a confirmed
    identity.
- **My trades**: the traders you copy, saved on your machine (hosted: in your browser's own sandbox). Every new buy they make becomes a sandbox order in
  the same market where they made it: a Polymarket trader's on Polymarket, a Kalshi trader's on Kalshi (5
  contracts, at most 3¢ above their price), held until the market settles. A Polymarket copy fills at
  Polymarket's best price, with its taker fee, from its public market data. One row per copied trade with its
  status (open / won / lost) and profit after fees, with the total at the top. Trades that can't fill near
  their price are skipped with the reason.

Traders without a name show as "Trader 0x91…76" with a generated avatar: the page never shows wallet addresses.

## Hosting

usesignals.app is the same engine, run with `SIGNALS_HOSTED=1`:

- **Railway** runs the engine from the `Dockerfile` (one process, `railway.json`), with a volume at `/data` for
  the store. Set `LAYER_API_KEY` in the service's variables; keys never reach the browser.
- **Vercel** holds the domain and forwards every request to Railway (`web/vercel.json`, deployed from `web/`).
  Caching is off there, so each browser keeps its own cookie.
- **No signup**: each browser gets its own sandbox, found by an anonymous cookie and kept in
  `/data/visitors/<id>`. Clearing cookies or switching browsers starts a new one.
- **Limits**: a browser copies up to 5 traders at once (`SIGNALS_MAX_TRADERS`), up to 300 browsers copy at once
  (`SIGNALS_MAX_COPYING`), and copying pauses for a browser away two weeks until it's back. Trades of a trader
  copied by many browsers are read once per round. "Check again" on Worth following only starts a new check
  when the last one is over an hour old.

## Good to know

- Kalshi's trader data comes from the public endpoints behind kalshi.com's Leaderboard and profile pages.
  Kalshi doesn't document them, so they can change.
- Kalshi traders choose whether to show their trades. Most leaderboard whales hide them: their page says
  so, and they can't be copied directly. Traders under **Traders → Big trades** usually show theirs.
- Polymarket's data comes from its public data API; every wallet is public.

## License

MIT. See [LICENSE](LICENSE).

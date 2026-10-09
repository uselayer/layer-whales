# Layer Whales

See the biggest traders on Kalshi and Polymarket, open one to see their trades, check whether they trade on the
other venue too, and copy their next trades in a sandbox.

Built on the [uselayer](https://uselayer.sh) Python SDK (`client.whales`). It runs on your machine: nothing
is hosted, and copies run in a sandbox (real prices and fees, no real money).

## Run it

You need [uv](https://docs.astral.sh/uv/); it fetches Python 3.11+ if you don't have it.

```sh
git clone https://github.com/uselayer/layer-whales
cd layer-whales
uv sync
LAYER_API_KEY=lyr_... uv run whales-demo
```

Then open http://127.0.0.1:8790.

- **Layer API key** (optional but recommended): used to find the same market on the other venue, to compare two
  traders' trades. Get one at uselayer.sh.
- **Kalshi key** (optional): set `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` to copy Kalshi traders. Copying
  Polymarket traders needs no key.
- `WHALES_PORT` (default 8790) and `WHALES_STORE_DIR` (default `~/.uselayer/whales-demo`) change the port and
  where the sandbox account, copy rules and Worth following results live. `WHALES_WORTH_WALLETS` (default 200)
  is how many wallets Worth following checks.

## What's on the page

Three tabs. It answers one question: what are the best traders trading right now, and can you still get in?

- **Feed**: new trades over the last 24 hours from the traders we rate Proven sharp, Quiet sharp or Rising, one
  card per trade: the market, their pick, what they paid, the price now, how much they put in, and one status
  line. **You can still get in** means the price is at most 3¢ above what they paid (the same limit copying
  uses); **Price moved away** means more. A trade at 2¢ or less, 98¢ or more, or one they no longer hold is
  **Won**, **Lost** or **Closed**, and drops out of the default feed. Filter by category, or turn off "Only
  trades I can still get into" to see them all. Tap a card for the details, the trader and a link to Polymarket.
  **Copy trader** copies that trader's next trades in the sandbox; a trade they already made isn't copied.
  It updates every 30 seconds; Polymarket shows trades a few minutes late.
- **Traders**:
  - **Worth following**: Polymarket traders ranked by tier (Proven sharp, Quiet sharp, Rising, Too fast to
    copy, Lucky), with their profit over 30 days, whether they beat the odds, and what you'd have made a
    contract copying them. "How we pick traders" explains the checks in plain words. Market makers and
    arbitrage are left out. Wallets come from the leaderboards and from busy markets' trades. The first check
    takes 20–30 minutes and is kept in the store dir, so later launches show the last result at once while a
    new check runs in the background (on launch if it's over 6 hours old, then every 6 hours while the app is
    open). Scoring is `client.whales.discover()` / `score()` in the SDK.
  - **Top profit**: both venues' leaderboards in one list, by profit. Pick one venue to sort by volume
    (Kalshi counts contracts, Polymarket dollars, so the two aren't comparable).
  - **Big trades**: trades of $250 or more right now, with the trader's name when the venue shows it.
  - **A trader's page**: their profit, their open trades as cards, recent results, and a Copy trader button. The
    evidence (each check, how the price moved after their buys, their categories, the trades checked) is under
    **Details**. A leaderboard trader's page also asks **Same person on the other venue?**: accounts that may
    be the same person, each with the evidence (the same name or X handle on an account that really trades,
    and trades in the same market, the same way, within 5 minutes). It's an inference, never a confirmed
    identity.
- **My trades**: the traders you copy, saved on your machine. Every new buy they make becomes a sandbox order in
  the same market where they made it: a Polymarket trader's on Polymarket, a Kalshi trader's on Kalshi (5
  contracts, at most 3¢ above their price), held until the market settles. A Polymarket copy fills at
  Polymarket's best price, with its taker fee, from its public market data. One row per copied trade with its
  status (open / won / lost) and profit after fees, with the total at the top. Trades that can't fill near
  their price are skipped with the reason.

Traders without a name show as "Trader 0x91…76" with a generated avatar: the page never shows wallet addresses.

## Good to know

- Kalshi's trader data comes from the public endpoints behind kalshi.com's Leaderboard and profile pages.
  Kalshi doesn't document them, so they can change.
- Kalshi traders choose whether to show their trades. Most leaderboard whales hide them: their page says
  so, and they can't be copied directly. Traders under **Traders → Big trades** usually show theirs.
- Polymarket's data comes from its public data API; every wallet is public.

## License

MIT. See [LICENSE](LICENSE).

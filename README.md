# Layer Whales

See the biggest traders on Kalshi and Polymarket, open one to see their bets, check whether they trade on the
other venue too, and copy their new trades with paper money.

Built on the [uselayer](https://uselayer.sh) Python SDK (`client.whales`). It runs on your machine: nothing
is hosted, and copies are paper trades (real prices, fake money).

## Run it

You need [uv](https://docs.astral.sh/uv/); it fetches Python 3.11+ if you don't have it.

```sh
git clone https://github.com/uselayer/layer-whales
cd layer-whales
uv sync
LAYER_API_KEY=lyr_... uv run whales-demo
```

Then open http://127.0.0.1:8790.

- **Layer API key** (optional but recommended): used to find the same bet on the other venue, both to compare
  two traders' bets and to copy a Polymarket trade onto Polymarket US or Kalshi. Get one at uselayer.sh.
- **Kalshi key** (optional): set `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` to copy onto Kalshi. Copying
  onto Polymarket US needs no key.
- `WHALES_PORT` (default 8790) and `WHALES_STORE_DIR` (default `~/.uselayer/whales-demo`) change the port and
  where the paper account, copy rules and Worth following results live. `WHALES_WORTH_WALLETS` (default 200)
  is how many wallets Worth following checks.

## What's on the page

- **Top traders**: both venues' leaderboards in one list, by profit. Pick one venue to sort by volume
  (Kalshi counts contracts, Polymarket dollars, so the two aren't comparable).
- **Trading now**: trades of $250 or more right now, with the trader's name when the venue shows it.
- **A trader's page**: profit, volume, open positions and recent trades.
  - **Same person on the other venue?** Accounts that may be the same person, each with the evidence: the
    same name or X handle on an account that really trades, and bets on the same market, the same way,
    within 5 minutes. Busy bots and market makers that trade everything are filtered out. It's an inference,
    never a confirmed identity.
  - **Copy their buys**: adds them to your copy list (see **My copied trades**).
- **Worth following**: Polymarket traders sorted into proven sharps, quiet sharps (small accounts no
  leaderboard lists), rising, sharp but too fast to copy, and lucky big bettors, each with a one-line reason
  and the evidence. Market makers and arbitrage are left out. A trader's page shows each rule's check, how
  the price moved after their buys, their strong categories, what they're buying now, and the bets checked.
  Wallets come from the leaderboards and from busy markets' trades. The first check takes 20–30 minutes and
  is kept in the store dir, so later launches show the last result at once while a new check runs in the
  background (on launch if it's over 6 hours old, then every 6 hours while the app is open). Scoring is
  `client.whales.discover()` / `score()` in the SDK.
- **My copied trades**: the traders you copy, saved on your machine. Every new buy they make becomes a paper
  order on the same bet on Polymarket US (5 contracts, at most 3¢ above their price), held until the market
  settles. One row per copied trade: who, the bet, your price,
  open / won / lost, and profit after fees, with the total at the top. Polymarket bets with no matching market,
  or that can't fill near their price, are skipped with the reason.

## Good to know

- Kalshi's trader data comes from the public endpoints behind kalshi.com's Leaderboard and profile pages.
  Kalshi doesn't document them, so they can change.
- Kalshi traders choose whether to show their trades. Most leaderboard whales hide them: their page says
  so, and they can't be copied directly. Traders on the **Trading now** tab usually show theirs.
- Polymarket's data comes from its public data API; every wallet is public.

## License

MIT. See [LICENSE](LICENSE).

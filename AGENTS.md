# This repo

Signals, on the uselayer SDK: top traders on Kalshi and Polymarket, a page per trader, cross-venue links and
sandbox copy trading. It runs on the user's machine (`uv run signals`) and is hosted at usesignals.app: the same
engine with `SIGNALS_HOSTED=1` on Railway, behind Vercel (see README, "Hosting").

- All venue reads, links and copies go through `client.whales` in the SDK. Don't add venue calls or matching
  logic here; put it in the SDK.
- Keys come from the environment (hosted: Railway's service variables), are never logged, returned by the
  engine or written to disk.
- Paper mode only, hosted or not. Never run live mode or place a real-money order.
- Hosted, there is no signup: each browser has its own sandbox, found by an anonymous cookie. Never let one
  browser see or change another's sandbox, and never let a response that sets the cookie be cached.
- Deploys and changes to the hosting (Railway, Vercel, the domain) only when the owner asks.
- Never say a link *is* someone: show the tier ("likely" / "possible") and the evidence.
- The page is a consumer app, not an analyst's spreadsheet. It answers one question: "What are the best traders
  trading right now, and can I still get in?" Three tabs (Feed, Traders, My trades) and a "Sandbox" pill.
  - Words: say "trade", never "bet"; say "sandbox", never "paper money" or "paper" (real prices and fees, no real
    money). The copy button says "Copy trader" / "Copying trader ✓": copying copies a trader's next trades.
    Copying a trade they already placed is not built, so no label may imply it.
  - One idea per card. The market title is the biggest text; status is one line with a dot.
  - Color means something: green = you can still get in, grey = moved away, neutral = settled. Nothing else is
    colored.
  - Plain words, sentence case, one font (Host Grotesk, as on Layer's site), tabular numbers, whole cents (tenths
    only under 1¢ or when two prices would look equal). No UPPERCASE headers, no tooltip-on-header help.
  - Methodology (tests, z-scores, margins) stays out of the main view: in "How we pick traders" or a Details fold.
  - No wallet addresses on screen: unnamed traders get "Trader 0x91…76" and an identicon.
  - Must look good at 390px as well as desktop; feed width about 680px. Keep loading, empty and error states.
  - A trade at 2¢ or less, 98¢ or more, or no longer held is settled (Won / Lost / Closed), never a bargain
    (`bet_status` in `app.py`). "You can still get in" only from 5¢ below to 3¢ above what they paid; further
    either way is "Price moved away" (a big drop means the market turned against them, not a bargain).
- Pull requests: sessions never merge. Only the owner merges, when they say "merge #N".

## Deploying (only when the owner asks)

- **Engine** (Railway project and service `signals`): `railway up --service signals` from the repo root. It
  builds the `Dockerfile`; the store is on the service's volume at `/data`. A deploy restarts the server, and a
  Worth following check that hasn't finished starts over (20–30 min).
- **Forwarding layer** (Vercel project `signals`, holds usesignals.app): `vercel deploy --prod` inside `web/`.
  Only needed when `web/vercel.json` changes.
- A new checkout or folder isn't linked yet: run `railway link` (project and service `signals`) in the repo root
  and `vercel link --project signals` in `web/` first.
- Keys are set by the owner in Railway's service variables, never by a session.
- `railway.json` works until 2026-12-01; after that Railway needs `railway config migrate`.

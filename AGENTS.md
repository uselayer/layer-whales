# This repo

Signals, on the uselayer SDK: top traders on Polymarket (Kalshi too when switched on, see below), a page per
trader, cross-venue links (with Kalshi on) and sandbox copy trading. It runs on the user's machine (`uv run signals`) and is hosted at usesignals.app: the same
engine with `SIGNALS_HOSTED=1` on Railway, behind Vercel (see README, "Hosting").

- All venue reads, links and copies go through `client.whales` in the SDK. Don't add venue calls or matching
  logic here; put it in the SDK.
- Signals is Polymarket-only for now; the SDK stays multi-venue. `SIGNALS_VENUES` (`VENUES` in `app.py`, default
  `polymarket`; `polymarket,kalshi` adds Kalshi back) is the one switch: every read passes it to the SDK
  (`big_trades(venues=…)`, `top(venue=…)`), and a venue that's off is refused, never read and then hidden. The
  page gets it from `/api/config` and hides Kalshi's views (Top profit's venue choice, Kalshi trader pages,
  "Same person on the other venue?") while it's off.
- Keys come from the environment (hosted: Railway's service variables), are never logged, returned by the
  engine or written to disk.
- Paper mode only, hosted or not. Never run live mode or place a real-money order.
- Hosted, there is no signup: each browser has its own sandbox, found by an anonymous cookie. Never let one
  browser see or change another's sandbox, and never let a response that sets the cookie be cached.
- Deploys and changes to the hosting (Railway, Vercel, the domain) only when the owner asks.
- Never say a link *is* someone: show the tier ("likely" / "possible") and the evidence.
- The page is a consumer app, not an analyst's spreadsheet. It helps people find traders worth following and
  understand why: who they are, how they trade, and what the evidence does and doesn't show. Sandbox copying is
  optional, a way to test a trader; nothing on the page may imply that copying makes money. Three tabs (Feed,
  Traders, My trades) and a "Sandbox" pill.
  - Onboarding (first visit, once per browser) asks one question: which markets interest you (Politics, Sports,
    Crypto, Esports; Skip means all markets). Nothing else: no budget, goals or signup. The choice is kept in
    localStorage, every read and write in try/catch; if storage is blocked it holds for the visit, and no one is
    ever stuck on that screen.
  - Traders is one leaderboard of the traders we recommend (Proven sharp, Quiet sharp, Rising, Too fast to copy).
    One bar: filters on the left (market chips = the onboarding choice, Copyable only), how you view the list on
    the right (search name or wallet, period, sort). Columns are grouped as Track record, Style and Copying.
    Recommendations never include Lucky, No clear edge or No view traders, and the checks are never loosened to
    fill the list: an empty result says so and names the interests that had no one. "See all checked traders" may
    show the rest, each marked.
  - Never a made-up number for a real trader on the hosted app. A figure the data doesn't have yet shows "—";
    sample values (`SIGNALS_MOCK=1`) are for reviewing the page locally only. "Copyable" is plain text: green
    stays for "you can still get in". The SDK's per-dollar figure is a return per dollar traded, never "ROI".
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

# This repo

A demo on the uselayer SDK: top traders on Kalshi and Polymarket, a page per trader, cross-venue links and
sandbox copy trading. It runs only on the user's machine; nothing is hosted or deployed.

- All venue reads, links and copies go through `client.whales` in the SDK. Don't add venue calls or matching
  logic here; put it in the SDK.
- Keys come from the environment, are never logged, returned by the engine or written to disk.
- Paper mode only. Never run live mode or place a real-money order.
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
    (`bet_status` in `app.py`).
- Pull requests: sessions never merge. Only the owner merges, when they say "merge #N".

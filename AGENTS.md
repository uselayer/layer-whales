# This repo

A demo on the uselayer SDK: top traders on Kalshi and Polymarket, a page per trader, cross-venue links and
paper copy trading. It runs only on the user's machine; nothing is hosted or deployed.

- All venue reads, links and copies go through `client.whales` in the SDK. Don't add venue calls or matching
  logic here; put it in the SDK.
- Keys come from the environment, are never logged, returned by the engine or written to disk.
- Paper mode only. Never run live mode or place a real-money order.
- Never say a link *is* someone: show the tier ("likely" / "possible") and the evidence.
- Keep the page minimal, in the plain style of Layer's docs.
- Pull requests: sessions never merge. Only the owner merges, when they say "merge #N".

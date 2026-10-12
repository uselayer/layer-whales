"""Overlooked Polymarket traders: active across several markets, ordinary trade sizes, on no leaderboard.

Markets are sampled across the whole volume range (not only the busiest), so slow, mid-size markets are in.
Wallets are kept by how many markets they show up in, not by dollars. Also picks 60 leaderboard wallets as a
control for the same test. Read-only: public Polymarket data, no keys.

    uv run python research/overlooked_candidates.py --out research/overlooked-candidates.json
"""

from __future__ import annotations

import argparse
import json
import random
import statistics as st
import time
from collections import defaultdict
from pathlib import Path

from uselayer import Client
from uselayer.errors import VenueError

ap = argparse.ArgumentParser()
ap.add_argument("--out", default=str(Path(__file__).parent / "overlooked-candidates.json"))
args = ap.parse_args()

poly = Client(store=":memory:").whales._poly
random.seed(7)

listed: set[str] = set()
board: dict[str, str | None] = {}


def lb(order: str, period: str, n: int, category: str | None = None) -> None:
    for off in range(0, n, 50):
        try:
            rows = poly.leaderboard(order, period, 50, category=category, offset=off)
        except VenueError:
            return
        for r in rows:
            w = str(r.get("proxyWallet") or "").lower()
            if w:
                listed.add(w)
                board.setdefault(w, r.get("userName"))
        if len(rows) < 50:
            return


for order, period, n in (("PNL", "MONTH", 1000), ("PNL", "WEEK", 500), ("PNL", "ALL", 1000), ("VOL", "MONTH", 500), ("VOL", "ALL", 500)):
    lb(order, period, n)
for cat in ("SPORTS", "CRYPTO", "POLITICS", "ECONOMICS", "CULTURE", "WEATHER", "ESPORTS", "TECH"):
    lb("PNL", "MONTH", 200, cat)
print("leaderboard wallets excluded:", len(listed), flush=True)

markets = []
for off in range(0, 3000, 100):  # open markets across the volume range
    try:
        rows = poly._list(
            poly._profiles + "/markets",
            {"closed": "false", "limit": 100, "offset": off, "order": "volume24hr", "ascending": "false"},
        )
    except VenueError:
        break
    markets += [(off + i, m) for i, m in enumerate(rows) if m.get("conditionId") and float(m.get("volume24hr") or 0) > 200]
    if len(rows) < 100:
        break
print("open markets with >$200 traded today:", len(markets), flush=True)
# Stratified by volume rank: equal markets from each fifth of the range, so the tail is in.
markets.sort(key=lambda x: x[0])
k = len(markets) // 5
picked = [m for q in range(5) for m in random.sample(markets[q * k : (q + 1) * k], min(60, k))]
print("markets sampled:", len(picked), flush=True)

seen: dict[str, set[str]] = defaultdict(set)
usd: dict[str, list[float]] = defaultdict(list)
name: dict[str, str] = {}
for i, (_, m) in enumerate(picked):
    try:
        rows = poly.market_trades(m["conditionId"], limit=500)
    except VenueError:
        continue
    for r in rows:
        w = str(r.get("proxyWallet") or "").lower()
        if not w or w in listed:
            continue
        seen[w].add(m["conditionId"])
        usd[w].append(float(r.get("size") or 0) * float(r.get("price") or 0))
        if r.get("name") and not str(r["name"]).startswith("0x"):
            name[w] = r["name"]
    if i % 50 == 0:
        print(f"  read {i}/{len(picked)} markets, {len(seen)} wallets", flush=True)

cands = [
    {"wallet": w, "name": name.get(w), "markets": len(ms), "median_usd": round(st.median(usd[w]), 2)}
    for w, ms in seen.items()
    if len(ms) >= 2 and 5 <= st.median(usd[w]) <= 2000
]
random.shuffle(cands)
cands.sort(key=lambda c: -c["markets"])
controls = random.sample(sorted(board), 60)  # leaderboard wallets, same test, for comparison
Path(args.out).write_text(json.dumps({
    "made": time.time(), "listed": len(listed), "markets": len(picked), "wallets_seen": len(seen),
    "candidates": cands, "controls": [{"wallet": w, "name": board[w]} for w in controls],
}))  # fmt: skip
print("candidates (>=2 markets, median trade $5-2000):", len(cands), "| controls:", len(controls))

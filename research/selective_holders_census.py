"""Census: top holders of open markets across activity levels. How often do they trade, how many are on the
monthly profit leaderboard, and how many independent markets would a 12-month record give? Read-only, no outcomes.

    uv run --no-sync python holders_census.py OUT.json
"""
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

h = httpx.Client(timeout=30, headers={"user-agent": "layer-research"})
GAMMA, DATA = "https://gamma-api.polymarket.com", "https://data-api.polymarket.com"
random.seed(23)
now = time.time()


def get(url, **params):
    for k in range(4):
        r = h.get(url, params=params)
        if r.status_code == 200:
            return r.json()
        time.sleep(1 + k)
    return None


# Open markets by 24h volume, ranks spread across the range (busy to slow).
mk = []
for off in range(0, 2000, 100):
    mk += get(f"{GAMMA}/markets", closed="false", limit=100, offset=off, order="volume24hr", ascending="false") or []
mk = [m for m in mk if m.get("conditionId")]
bands = [mk[:50], mk[50:300], mk[300:1000], mk[1000:]]
pick = [m for b in bands for m in random.sample(b, min(10, len(b)))]
print("markets", len(mk), "picked", len(pick), flush=True)

board = set()
for off in range(0, 1000, 50):
    for r in get(f"{DATA}/v1/leaderboard", timePeriod="MONTH", orderBy="PNL", limit=50, offset=off) or []:
        board.add(str(r.get("proxyWallet")).lower())

holders = {}
for i, m in enumerate(pick):
    for t in get(f"{DATA}/holders", market=m["conditionId"], limit=20) or []:
        for x in t.get("holders") or []:
            w = str(x["proxyWallet"]).lower()
            holders.setdefault(w, {"wallet": w, "band": i // 10, "usd_pos": 0.0})
            holders[w]["usd_pos"] = max(holders[w]["usd_pos"], float(x.get("amount") or 0))
print("holders", len(holders), flush=True)


def one(w):
    rows, upper = [], int(now)
    for _ in range(6):  # up to 3,000 trades back, enough to tell frequent from selective
        page = get(f"{DATA}/activity", user=w, type="TRADE", limit=500, start=int(now - 365 * 86400), end=upper) or []
        rows += page
        if len(page) < 500:
            break
        upper = int(page[-1]["timestamp"]) - 1
    buys = [(int(r["timestamp"]), r["conditionId"], str(r.get("isCombo")) == "True") for r in rows if r.get("side") == "BUY"]
    n = lambda d: len({c for t, c, combo in buys if t >= now - d * 86400 and not combo})  # noqa: E731
    return {"m30": n(30), "m90": n(90), "m365": n(365), "capped": len(rows) >= 3000,
            "first_365": min((t for t, _, _ in buys), default=None)}


with ThreadPoolExecutor(6) as ex:
    for w, res in zip(holders, ex.map(one, holders)):
        holders[w] |= res | {"on_board": w in board}
json.dump(list(holders.values()), open(sys.argv[1], "w"))
print("done", flush=True)

"""The pool for selective-large-preregistered.md: wallets making a few large bets a month before T, from big trades
in closed markets (no hindsight) and from today's all-time leaderboards. Qualifying is decided from trades before
T only. Read-only: public Polymarket data, no keys.

    uv run python research/selective_large_candidates.py --out research/selective-large-candidates.json
"""

from __future__ import annotations

import argparse
import json
import random
import statistics as st
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from uselayer import Client
from uselayer.errors import VenueError

T = int(datetime(2026, 8, 14, tzinfo=UTC).timestamp())
DAY = 86400
BANDS = ((1e4, 1e5), (1e5, 1e6), (1e6, 1e7), (1e7, None))
MARKETS_PER_BAND = 100
MIN_TRADE = 1000
PER_SOURCE = 300
CATS = ("SPORTS", "CRYPTO", "POLITICS", "ECONOMICS", "CULTURE", "WEATHER", "ESPORTS", "TECH", "FINANCE", "MENTIONS")

ap = argparse.ArgumentParser()
ap.add_argument("--out", default=str(Path(__file__).parent / "selective-large-candidates.json"))
args = ap.parse_args()

poly = Client(store=":memory:").whales._poly
random.seed(17)

# ---- the market source: big trades before T in closed markets ----
markets: list[dict[str, Any]] = []
for lo, hi in BANDS:
    rows: list[dict[str, Any]] = []
    for off in range(0, 500, 100):
        q: dict[str, Any] = {"closed": "true", "end_date_min": "2026-06-15T00:00:00Z", "end_date_max": "2026-08-14T00:00:00Z",
                             "limit": 100, "offset": off, "volume_num_min": lo, "order": "volumeNum", "ascending": "false"}  # fmt: skip
        if hi:
            q["volume_num_max"] = hi
        try:
            got = poly._list(poly._profiles + "/markets", q)
        except VenueError as e:
            print("gamma:", e)
            break
        rows += [r for r in got if r.get("conditionId")]
        if len(got) < 100:
            break
    pick = random.sample(rows, min(MARKETS_PER_BAND, len(rows)))
    markets += [{"cid": r["conditionId"], "band": lo} for r in pick]
    print(f"band ${lo:,.0f}+: {len(rows)} closed markets, {len(pick)} picked", flush=True)

from_market: set[str] = set()
for m in markets:
    try:
        rows = poly.market_trades(m["cid"], limit=500, min_usd=MIN_TRADE)
    except VenueError:
        continue
    from_market |= {str(r.get("proxyWallet") or "").lower() for r in rows
                    if r.get("proxyWallet") and T - 90 * DAY < int(r.get("timestamp") or 0) <= T}  # fmt: skip
print(f"market source: {len(from_market)} wallets with a $1,000+ trade in the 90 days before T", flush=True)

# ---- the leaderboard source: today's all-time boards (monthly and weekly rank on window B: not used) ----
boards: dict[str, set[str]] = defaultdict(set)  # wallet -> boards it's on


def board(order: str, cat: str | None, places: int) -> None:
    name = f"{order} ALL {cat or 'OVERALL'}"
    for off in range(0, places, 50):
        try:
            rows = poly.leaderboard(order, "ALL", 50, category=cat, offset=off)
        except VenueError as e:
            print("board", name, e)
            return
        for r in rows:
            if r.get("proxyWallet"):
                boards[str(r["proxyWallet"]).lower()].add(name)
        if len(rows) < 50:
            return


board("PNL", None, 2000)
board("VOL", None, 2000)
for c in CATS:
    board("PNL", c, 500)
print(f"leaderboard source: {len(boards)} wallets on today's all-time boards", flush=True)


def qualify(w: str) -> dict[str, Any]:
    """From their trades in the 90 days before T (up to 1,500): markets bought, months active, dollars per market."""
    rows: list[dict[str, Any]] = []
    try:
        for off in (0, 500, 1000):
            got = poly.activity(w, start=T - 90 * DAY, end=T, limit=500, offset=off)
            rows += got
            if len(got) < 500:
                break
    except VenueError as e:
        return {"wallet": w, "error": repr(e)}
    per_market: dict[str, float] = defaultdict(float)
    months: set[int] = set()
    for r in rows:
        if str(r.get("side")).upper() != "BUY" or not r.get("conditionId"):
            continue
        per_market[r["conditionId"]] += float(r.get("usdcSize") or 0)
        months.add(min(2, (T - int(r["timestamp"])) // (30 * DAY)))
    n = len(per_market)
    med = st.median(per_market.values()) if per_market else 0.0
    ok = len(rows) < 1500 and 9 <= n <= 21 and len(months) >= 2 and med >= MIN_TRADE
    return {"wallet": w, "markets": n, "months": len(months), "median_usd": round(med, 2), "trades": len(rows),
            "qualifies": ok}  # fmt: skip


def source_of(w: str) -> str:
    if w not in boards:
        return "market"
    return "lb-profit" if any(b.startswith("PNL") for b in boards[w]) else "lb-volume"


pool = sorted(from_market | set(boards))
random.shuffle(pool)
kept: dict[str, list[dict[str, Any]]] = {"market": [], "lb-profit": [], "lb-volume": []}
checked: list[dict[str, Any]] = []
t0 = time.time()
with ThreadPoolExecutor(6) as ex:
    for i in range(0, len(pool), 60):
        for r in ex.map(qualify, pool[i : i + 60]):
            checked.append(r)
            if r.get("qualifies"):
                w = r["wallet"]
                r |= {"source": source_of(w), "also_market": w in from_market, "boards": sorted(boards.get(w, ()))}
                if len(kept[r["source"]]) < PER_SOURCE:
                    kept[r["source"]].append(r)
        print(f"checked {len(checked)}/{len(pool)} in {time.time() - t0:.0f}s: " +
              ", ".join(f"{k} {len(v)}" for k, v in kept.items()), flush=True)  # fmt: skip
        if all(len(v) >= PER_SOURCE for v in kept.values()):
            break

counts = {"checked": len(checked), "errors": sum("error" in r for r in checked),
          "qualify_market": sum(1 for r in checked if r.get("qualifies") and r["wallet"] in from_market),
          "qualify_board": sum(1 for r in checked if r.get("qualifies") and r["wallet"] in boards),
          "market_wallets": len(from_market), "board_wallets": len(boards)}  # fmt: skip
Path(args.out).write_text(json.dumps({"T": T, "markets": markets, "counts": counts, "groups": kept}, indent=1))
print("counts:", counts, "| kept:", {k: len(v) for k, v in kept.items()})

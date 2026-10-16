"""The pool for frequency-preregistered.md, built without hindsight. Wallets that traded in closed markets before T
(across four volume bands), each placed in a frequency band from one read of its trades in the 90 days before T.
Nothing after T is read. Read-only: public Polymarket data, no keys.

    uv run python research/frequency_candidates.py --out research/frequency-candidates.json
"""

from __future__ import annotations

import argparse
import json
import random
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from uselayer import Client
from uselayer.errors import VenueError

T = int(datetime(2026, 8, 14, tzinfo=UTC).timestamp())
DAY = 86400
BANDS = ((1e3, 1e4), (1e4, 1e5), (1e5, 1e6), (1e6, None))
MARKETS_PER_BAND = 60
PER_GROUP = 150
NEED = 8  # uselayer's RISING_BETS: markets bought before the scorer can judge skill

ap = argparse.ArgumentParser()
ap.add_argument("--out", default=str(Path(__file__).parent / "frequency-candidates.json"))
ap.add_argument("--max-classify", type=int, default=5000)
args = ap.parse_args()

poly = Client(store=":memory:").whales._poly
random.seed(13)

markets: list[dict] = []
for lo, hi in BANDS:
    rows: list[dict] = []
    for off in range(0, 500, 100):
        q = {"closed": "true", "end_date_min": "2026-06-15T00:00:00Z", "end_date_max": "2026-08-14T00:00:00Z",
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
    markets += [{"cid": r["conditionId"], "band": lo, "question": r.get("question")} for r in pick]
    print(f"band ${lo:,.0f}+: {len(rows)} closed markets, {len(pick)} picked", flush=True)

seen: dict[str, set[str]] = defaultdict(set)
for m in markets:
    try:
        rows = poly.market_trades(m["cid"], limit=500)
    except VenueError:
        continue
    for r in rows:
        w = str(r.get("proxyWallet") or "").lower()
        if w and T - 90 * DAY < int(r.get("timestamp") or 0) <= T:
            seen[w].add(m["cid"])
wallets = sorted(seen)
random.shuffle(wallets)
print(f"{len(wallets)} wallets traded in them in the 90 days before T", flush=True)


def classify(w: str) -> dict | None:
    """Markets bought in the 30 and 90 days before T, from their trades before T only (up to 1,000)."""
    rows: list[dict] = []
    try:
        for off in (0, 500):
            got = poly.activity(w, start=T - 90 * DAY, end=T, limit=500, offset=off)
            rows += got
            if len(got) < 500:
                break
    except VenueError as e:
        return {"wallet": w, "error": repr(e)}
    buys = [(int(r["timestamp"]), r.get("conditionId")) for r in rows if str(r.get("side")).upper() == "BUY"]
    b30 = len({c for t, c in buys if t > T - 30 * DAY})
    b90 = len({c for _, c in buys})
    cut = len(rows) >= 1000  # read cut short: they trade a lot, so the 30-day count is at least this
    band = "frequent" if b30 >= NEED else "selective" if b90 >= NEED and not cut else "sparse" if 3 <= b90 < NEED else None
    return {"wallet": w, "buys_30d": b30, "buys_90d": b90, "trades": len(rows), "cut": cut, "band": band}


groups: dict[str, list[dict]] = {"frequent": [], "selective": [], "sparse": []}
done, t0 = [], time.time()
with ThreadPoolExecutor(6) as ex:
    for i in range(0, min(len(wallets), args.max_classify), 60):
        for r in ex.map(classify, wallets[i : i + 60]):
            if r is None:
                continue
            done.append(r)
            if r.get("band") and len(groups[r["band"]]) < PER_GROUP:
                groups[r["band"]].append(r)
        print(f"classified {len(done)} in {time.time() - t0:.0f}s: " + ", ".join(f"{k} {len(v)}" for k, v in groups.items()), flush=True)
        if all(len(v) >= PER_GROUP for v in groups.values()):
            break

counts = defaultdict(int)
for r in done:
    counts[r.get("band") or ("error" if "error" in r else "too_few")] += 1
Path(args.out).write_text(json.dumps({"T": T, "markets": markets, "classified": dict(counts), "groups": groups}, indent=1))
print("classified:", dict(counts), "| kept:", {k: len(v) for k, v in groups.items()})

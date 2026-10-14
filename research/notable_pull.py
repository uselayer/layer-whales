"""Pull, once, what the notable-vs-routine alert dry run needs, and cache it raw under research/notable-cache/.

For each followed wallet: every TRADE / MERGE / SPLIT / REDEEM / CONVERSION row of Polymarket's /activity from
--since to now (paged by time: /activity refuses an offset past 5,000), and its positions now (every row, settled
ones included, for working back what it held before a trade). Then the Gamma market of every conditionId it traded
since --replay-from (for the category: Sports and Esports group over 1 h, not 6 h).

Reads go through the uselayer SDK's Polymarket client (the one the app's ``reads`` uses), one at a time with a pause
between them, so Polymarket isn't hammered. A wallet already cached is skipped unless --refresh. Every variant of the
replay (notable_replay.py) runs from this cache and reads nothing.

    uv run python research/notable_pull.py --since 2026-09-11 --replay-from 2026-10-11 0xcf73...0215 ...
"""

from __future__ import annotations

import argparse
import gzip
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from uselayer import Client

CACHE = Path(__file__).parent / "notable-cache"
TYPES = "TRADE,MERGE,SPLIT,REDEEM,CONVERSION"
PAUSE_S = 0.6  # between reads


def _ts(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp())


def _key(r: dict) -> tuple:
    return (r.get("transactionHash"), r.get("asset"), r.get("type"), r.get("side"), r.get("size"), r.get("timestamp"))


def pull_activity(poly, wallet: str, since: int, max_rows: int) -> list[dict]:
    """Every activity row from ``since`` to now, newest first, paged back by time; at most about ``max_rows`` (a
    wallet trading thousands of times a day then has fewer days of history, said in meta.json)."""
    out: dict[tuple, dict] = {}
    end: int | None = None
    while True:
        for attempt in range(4):
            try:
                rows = poly.activity(wallet, type=TYPES, start=since, end=end, limit=500)
                break
            except Exception as e:  # noqa: BLE001 — a flaky read: wait and try again, a few times
                print(f"  retry {attempt + 1}: {e}")
                time.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError(f"couldn't read {wallet} before {end}")
        time.sleep(PAUSE_S)
        new = [r for r in rows if _key(r) not in out]
        for r in rows:
            out[_key(r)] = r
        oldest = min((r["timestamp"] for r in rows), default=None)
        print(f"  {len(out):>7} rows, back to {datetime.fromtimestamp(oldest, UTC) if oldest else '-'}")
        if len(rows) < 500 or oldest is None or oldest <= since or len(out) >= max_rows:
            break
        if not new:  # a whole page within one second: step past it (rare; would lose that second's rest)
            print(f"  warning: 500+ rows at {oldest}; stepping a second back")
            end = oldest - 1
        else:
            end = oldest  # inclusive: the boundary second is read again and deduped
    return sorted(out.values(), key=lambda r: r["timestamp"])


def pull_positions(poly, wallet: str) -> list[dict]:
    """Every position row now (settled ones too), paged by offset."""
    out: list[dict] = []
    for offset in range(0, 10_000, 500):
        rows = poly._list(poly._data + "/positions", {"user": wallet, "limit": 500, "offset": offset,
                                                       "sizeThreshold": 0, "sortBy": "CURRENT"})  # fmt: skip
        time.sleep(PAUSE_S)
        out += rows
        if len(rows) < 500:
            break
    return out


# Only what the replay reads (the SDK's _polymarket_trade, Holdings), so the cache stays small enough to commit
ACTIVITY_KEYS = ("proxyWallet", "timestamp", "conditionId", "type", "size", "usdcSize", "transactionHash", "price",
                 "asset", "side", "outcomeIndex", "title", "slug", "eventSlug", "outcome", "name")  # fmt: skip
POSITION_KEYS = ("asset", "conditionId", "size", "oppositeAsset", "curPrice", "redeemable", "outcomeIndex", "title")


def write_jsonl(path: Path, rows: list[dict], keys: tuple[str, ...]) -> None:
    with gzip.open(path, "wt") as f:
        for r in rows:
            f.write(json.dumps({k: r[k] for k in keys if r.get(k) not in (None, "")}, separators=(",", ":")) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("wallets", nargs="+")
    ap.add_argument("--since", required=True, help="UTC day the history starts (30 days before the first replay day)")
    ap.add_argument("--replay-from", required=True, help="UTC day the replay starts (categories from here)")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--max-rows", type=int, default=120_000, help="per wallet: fewer days of history past this")
    a = ap.parse_args()
    CACHE.mkdir(exist_ok=True)
    reads = Client(store=":memory:")
    poly = reads.whales._poly
    for w in (w.lower() for w in a.wallets):
        act, pos = CACHE / f"activity-{w}.jsonl.gz", CACHE / f"positions-{w}.jsonl.gz"
        if act.exists() and pos.exists() and not a.refresh:
            print(f"{w}: cached")
            continue
        print(f"{w}: activity since {a.since}")
        pulled_at = time.time()
        rows = pull_activity(poly, w, _ts(a.since), a.max_rows)
        positions = pull_positions(poly, w)
        write_jsonl(act, rows, ACTIVITY_KEYS)
        write_jsonl(pos, positions, POSITION_KEYS)
        meta = json.loads((CACHE / "meta.json").read_text()) if (CACHE / "meta.json").exists() else {}
        meta[w] = {"pulled_at": pulled_at, "since": a.since, "activity_rows": len(rows), "positions": len(positions),
                   "history_from": datetime.fromtimestamp(rows[0]["timestamp"], UTC).isoformat() if rows else None}  # fmt: skip
        (CACHE / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True))
        print(f"{w}: {len(rows)} activity rows, {len(positions)} positions")
    # Categories of every market traded since the replay starts, one Gamma batch, cached
    cats_path = CACHE / "categories.json"
    cats = json.loads(cats_path.read_text()) if cats_path.exists() else {}
    want = set()
    for p in CACHE.glob("activity-*.jsonl.gz"):
        with gzip.open(p, "rt") as f:
            for line in f:
                r = json.loads(line)
                if r.get("type") == "TRADE" and r["timestamp"] >= _ts(a.replay_from) and r["conditionId"] not in cats:
                    want.add(r["conditionId"])
    if want:
        from uselayer.whale_scores import category_of

        print(f"categories: {len(want)} markets from Gamma")
        book = reads.whales.scorer().markets.get(sorted(want))
        for c in want:
            cats[c] = category_of(book[c]) if c in book else None
        cats_path.write_text(json.dumps(cats, indent=0, sort_keys=True))


if __name__ == "__main__":
    main()

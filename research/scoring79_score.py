"""The scoring for scoring79-preregistered.md (audit fixes 7–9). Read-only: public Polymarket data, no keys.

Pool: 200 wallets drawn from the top 1,000 of the all-time volume leaderboard. Each is scored by the unchanged
uselayer 0.9.0 scorer on window A (the 30 days before T) and window B (T to B_END), every checked bet kept.
Wallets with a parlay in either sample are scored again with parlay buys dropped before the 60 are picked
(variant P), and mostly-parlay wallets get their parlays' settled record (payouts from REDEEM rows).
Resumable: wallets already in --out are skipped.

    uv run python research/scoring79_score.py --out research/scoring79-scores.jsonl
    gzip -9 research/scoring79-scores.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from uselayer import Client
from uselayer import whale_scores as ws

T = datetime(2026, 9, 16, tzinfo=UTC)
B_END = datetime(2026, 10, 13, 18, tzinfo=UTC)
B_DAYS = (B_END - T).total_seconds() / 86400

ap = argparse.ArgumentParser()
ap.add_argument("--out", default=str(Path(__file__).parent / "scoring79-scores.jsonl"))
ap.add_argument("--candidates", default=str(Path(__file__).parent / "scoring79-candidates.json"))
ap.add_argument("--wallets", type=int, default=200)
ap.add_argument("--threads", type=int, default=4)
ap.add_argument("--cache", default=str(Path(__file__).parent / ".cache" / "prices"))
args = ap.parse_args()

client = Client(store=":memory:")
poly = client.whales._poly
sc = client.whales.scorer(cache_dir=args.cache)

# -- the pool, drawn once and kept --
cand_path = Path(args.candidates)
if cand_path.exists():
    pool = json.loads(cand_path.read_text())
else:
    board: list[dict[str, Any]] = []
    for off in range(0, 1000, 50):
        board += poly.leaderboard("VOL", "ALL", 50, offset=off)
    seen: dict[str, dict[str, Any]] = {}
    for i, r in enumerate(board):
        w = str(r.get("proxyWallet") or "").lower()
        if w and w not in seen:
            seen[w] = {"wallet": w, "name": r.get("userName"), "vol_rank": i + 1, "vol": r.get("vol")}
    random.seed(79)
    pool = {"read_at": datetime.now(UTC).isoformat(), "board_size": len(seen),
            "wallets": random.sample(sorted(seen.values(), key=lambda c: c["vol_rank"]), args.wallets)}  # fmt: skip
    cand_path.write_text(json.dumps(pool, indent=1))
print(f"pool: {len(pool['wallets'])} of {pool['board_size']} (read {pool['read_at']})", flush=True)

# -- variant P: parlay buys dropped before the sample is picked (this thread only) --
_tl = threading.local()
_decisions = ws._decisions


def _decisions_p(fills: list[dict[str, Any]]) -> list[Any]:
    if not getattr(_tl, "no_combo", False):
        return _decisions(fills)
    combo = {str(f.get("conditionId")) for f in fills if str(f.get("isCombo")) == "True"}
    out = _decisions(fills)
    _tl.counts = {"decisions": len(out), "combos": sum(1 for d in out if d.condition in combo)}
    return [d for d in out if d.condition not in combo]


ws._decisions = _decisions_p


def score(w: str, *, end: datetime, days: float, name: str | None, no_combo: bool = False) -> dict[str, Any]:
    _tl.no_combo, _tl.counts = no_combo, None
    try:
        d = sc.score(w, days=days, end=end, sample=60, name=name, on_leaderboard=True).to_dict(sample=True)
    finally:
        _tl.no_combo = False
    if no_combo:
        d["combo_counts"] = _tl.counts
    return d


# -- parlays' settled record --


def _rows(w: str, type_: str, start: int, end: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    upper = end
    while True:
        for off in range(0, 5000, 500):
            page = poly.activity(w, type=type_, start=start, end=upper, offset=off)
            out += page
            if len(page) < 500:
                return out
        nxt = int(out[-1]["timestamp"]) - 1
        if nxt <= start:
            return out
        upper = nxt


def parlay_record(w: str, buy_start: int, buy_end: int, redeem_end: int) -> list[dict[str, Any]]:
    """Each parlay bought in [buy_start, buy_end): its day, cost, contracts and what REDEEM rows paid by
    redeem_end (0 if never redeemed: losing parlays aren't redeemed, so this is a lower bound)."""
    buys = [r for r in _rows(w, "TRADE", buy_start, buy_end)
            if str(r.get("isCombo")) == "True" and str(r.get("side")).upper() == "BUY"]  # fmt: skip
    red = _rows(w, "REDEEM", buy_start, redeem_end)
    paid: dict[str, float] = defaultdict(float)
    for r in red:
        paid[str(r["conditionId"])] += float(r.get("usdcSize") or 0)
    by: dict[str, dict[str, Any]] = {}
    for b in sorted(buys, key=lambda r: int(r["timestamp"])):
        c = str(b["conditionId"])
        e = by.setdefault(c, {"day": datetime.fromtimestamp(int(b["timestamp"]), UTC).date().isoformat(),
                              "usd": 0.0, "size": 0.0, "paid": 0.0, "redeemed": c in paid})  # fmt: skip
        e["usd"] += float(b.get("usdcSize") or 0)
        e["size"] += float(b.get("size") or 0)
    for c, e in by.items():
        e["paid"] = round(paid.get(c, 0.0), 4)
        e["usd"], e["size"] = round(e["usd"], 4), round(e["size"], 4)
    return list(by.values())


out = Path(args.out)
done: set[str] = set()
if out.exists():
    done = {json.loads(line)["wallet"] for line in out.read_text().splitlines() if line.strip()}
t0 = time.time()


def one(c: dict[str, Any]) -> dict[str, Any] | None:
    w = c["wallet"]
    if w in done:
        return None
    r: dict[str, Any] = {"wallet": w, "group": "big", "found": c}
    try:
        r["A"] = score(w, end=T, days=30, name=c.get("name"))
        r["B"] = score(w, end=B_END, days=B_DAYS, name=c.get("name"))
        if (r["A"]["coverage"].get("combos") or 0) + (r["B"]["coverage"].get("combos") or 0) > 0:
            r["A_P"] = score(w, end=T, days=30, name=c.get("name"), no_combo=True)
            r["B_P"] = score(w, end=B_END, days=B_DAYS, name=c.get("name"), no_combo=True)
            cc = r["A_P"].get("combo_counts") or {}
            if cc.get("decisions") and cc["combos"] / cc["decisions"] >= 0.9:
                t, b = int(T.timestamp()), int(B_END.timestamp())
                r["parlays_A"] = parlay_record(w, t - 30 * 86400, t, b)
                r["parlays_B"] = parlay_record(w, t, b - 5 * 86400, b)
    except Exception as e:  # keep going; the row records it
        r["error"] = repr(e)
    return r


with ThreadPoolExecutor(args.threads) as ex, out.open("a") as f:
    for i, r in enumerate(ex.map(one, pool["wallets"])):
        if r is None:
            continue
        f.write(json.dumps(r, default=str) + "\n")
        f.flush()
        a, b = r.get("A") or {}, r.get("B") or {}
        print(f"{i + 1}/{len(pool['wallets'])} {time.time() - t0:5.0f}s A={a.get('segment', 'err'):8} n={a.get('bets')} "
              f"B={b.get('segment', 'err'):8} n={b.get('bets')} P={'A_P' in r} parl={'parlays_A' in r} "
              f"{r.get('error', '')[:80]}", flush=True)  # fmt: skip

"""Score each wallet in the selective-large pool with the unchanged SDK scorer (selective-large-preregistered.md):
the 90 days before T to select, the 60 days after for the outcome, and today's 30-day method at 13 Sep and 13 Oct.
Resumable: wallets already in --out are skipped.

    uv run python research/selective_large_score.py research/selective-large-candidates.json
    gzip -9 research/selective-large-scores.jsonl
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from uselayer import Client

T = datetime(2026, 8, 14, tzinfo=UTC)
T2 = datetime(2026, 9, 13, tzinfo=UTC)
B_END = datetime(2026, 10, 13, tzinfo=UTC)

ap = argparse.ArgumentParser()
ap.add_argument("candidates")
ap.add_argument("--out", default=str(Path(__file__).parent / "selective-large-scores.jsonl"))
ap.add_argument("--cache", default=str(Path(__file__).parent / ".cache" / "prices"))
args = ap.parse_args()

out = Path(args.out)
d = json.loads(Path(args.candidates).read_text())
pick = [c | {"group": g} for g, cs in d["groups"].items() for c in cs]
done = set()
if out.exists():
    done = {json.loads(line)["wallet"] for line in out.read_text().splitlines() if line.strip()}
sc = Client(store=":memory:").whales.scorer(cache_dir=args.cache)
t0 = time.time()


def one(c: dict[str, Any]) -> dict[str, Any] | None:
    if c["wallet"] in done:
        return None
    r: dict[str, Any] = {"wallet": c["wallet"], "group": c["group"], "found": c}
    lb = c["group"] != "market"  # on an all-time board today; the check's on_leaderboard is the top boards, close enough
    try:
        r["A"] = sc.score(c["wallet"], days=90, end=T, sample=60, on_leaderboard=lb).to_dict(sample=True)
        r["B"] = sc.score(c["wallet"], days=60, end=B_END, sample=60, on_leaderboard=lb).to_dict(sample=True)
        r["M_T2"] = sc.score(c["wallet"], days=30, end=T2, sample=60, on_leaderboard=lb).to_dict(sample=True)
        r["M_now"] = sc.score(c["wallet"], days=30, end=B_END, sample=60, on_leaderboard=lb).to_dict(sample=True)
    except Exception as e:  # keep going; the row records it
        r["error"] = repr(e)
    return r


with ThreadPoolExecutor(6) as ex, out.open("a") as f:
    for i, r in enumerate(ex.map(one, pick)):
        if r is None:
            continue
        f.write(json.dumps(r, default=str) + "\n")
        f.flush()
        a, b = r.get("A") or {}, r.get("B") or {}
        print(f"{i + 1}/{len(pick)} {time.time() - t0:5.0f}s {r['group']:9} A n={a.get('bets')} "
              f"P&L {a.get('trading_pnl_window')} → B n={b.get('bets')} P&L {b.get('trading_pnl_window')} "
              f"M_T2={(r.get('M_T2') or {}).get('segment')} M_now={(r.get('M_now') or {}).get('segment')} "
              f"{r.get('error', '')[:60]}", flush=True)  # fmt: skip

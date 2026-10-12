"""Score each candidate twice with the unchanged SDK scorer: window A (the 30 days ending 30 days ago) to
select, window B (the last 30 days) to see what following them would have done. 250 overlooked wallets with
3+ markets, plus the 60 leaderboard controls. Resumable: wallets already in --out are skipped. ~1 hour.

    uv run python research/overlooked_score.py research/overlooked-candidates.json --out research/overlooked-scores.jsonl
    gzip -9 research/overlooked-scores.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from uselayer import Client

ap = argparse.ArgumentParser()
ap.add_argument("candidates")
ap.add_argument("--out", default=str(Path(__file__).parent / "overlooked-scores.jsonl"))
ap.add_argument("--wallets", type=int, default=250)
ap.add_argument("--cache", default=str(Path(__file__).parent / ".cache" / "prices"))
args = ap.parse_args()

out = Path(args.out)
d = json.loads(Path(args.candidates).read_text())
random.seed(11)
pool = [c for c in d["candidates"] if c["markets"] >= 3]
pick = [c | {"group": "overlooked"} for c in random.sample(pool, min(args.wallets, len(pool)))]
pick += [c | {"group": "leaderboard"} for c in d["controls"]]
done = set()
if out.exists():
    done = {json.loads(line)["wallet"] for line in out.read_text().splitlines() if line.strip()}
sc = Client(store=":memory:").whales.scorer(cache_dir=args.cache)
end_a = datetime.now(UTC) - timedelta(days=30)
t0 = time.time()


def one(cand: dict[str, Any]) -> dict[str, Any] | None:
    if cand["wallet"] in done:
        return None
    r: dict[str, Any] = {"wallet": cand["wallet"], "group": cand["group"], "found": cand}
    lb = cand["group"] == "leaderboard"
    try:
        r["A"] = sc.score(cand["wallet"], days=30, end=end_a, sample=60, name=cand.get("name"), on_leaderboard=lb).to_dict(sample=True)
        r["B"] = sc.score(cand["wallet"], days=30, sample=60, name=cand.get("name"), on_leaderboard=lb).to_dict(sample=True)
    except Exception as e:  # keep going; the row records it
        r["error"] = repr(e)
    return r


with ThreadPoolExecutor(6) as ex, out.open("a") as f:
    for i, r in enumerate(ex.map(one, pick)):
        if r is None:
            continue
        f.write(json.dumps(r) + "\n")
        f.flush()
        a, b = r.get("A") or {}, r.get("B") or {}
        print(f"{i + 1}/{len(pick)} {time.time() - t0:5.0f}s {r['group'][:4]} A={a.get('segment', 'err'):8} n={a.get('bets')} "
              f"B={b.get('segment', 'err'):8} n={b.get('bets')} {r.get('error', '')[:60]}", flush=True)  # fmt: skip

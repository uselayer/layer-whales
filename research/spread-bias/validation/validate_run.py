"""Score wallets with the maker-spread branch, one at a time, counting every Polymarket read by endpoint.

    PYTHONPATH=<sdk-repo>/python/src uv run --frozen python validate_run.py wallets.json --out out.jsonl [--group G --end ISO]
"""
from __future__ import annotations

import argparse, json, time
from collections import Counter
from datetime import datetime
from pathlib import Path

from uselayer import Client
from uselayer.venues import polymarket_data as pd

ap = argparse.ArgumentParser()
ap.add_argument("wallets"); ap.add_argument("--out", required=True)
ap.add_argument("--group"); ap.add_argument("--end"); ap.add_argument("--limit", type=int)
args = ap.parse_args()

counts: Counter = Counter()
_get = pd.PolymarketData._get
def counted(self, url, params):
    path = url.split(".com", 1)[-1]
    counts[path + ("?market" if path == "/trades" and "market" in params else "")] += 1
    return _get(self, url, params)
pd.PolymarketData._get = counted

ws = json.loads(Path(args.wallets).read_text())[: args.limit]
out = Path(args.out)
done = {json.loads(l)["wallet"] for l in out.read_text().splitlines()} if out.exists() else set()
sc = Client(store=":memory:").whales.scorer()
with out.open("a") as f:
    for i, w in enumerate(ws):
        if w["wallet"] in done:
            continue
        end = datetime.fromisoformat(args.end or w["end"])
        counts.clear(); t0 = time.time()
        row = {"wallet": w["wallet"], "group": args.group or w["group"], "end": end.isoformat()}
        try:
            s = sc.score(w["wallet"], days=30, end=end, sample=60, on_leaderboard=bool(w.get("on_leaderboard")), name=w.get("name"))
            row["score"] = s.to_dict(sample=True)
        except Exception as e:
            row["error"] = repr(e)
        row["reads"] = dict(counts); row["secs"] = round(time.time() - t0, 1)
        f.write(json.dumps(row) + "\n"); f.flush()
        sd = row.get("score") or {}
        print(f"{i+1}/{len(ws)} {row['group']} {w['wallet'][:10]} {sd.get('segment','err')} reads={sum(counts.values())} taker={counts['/trades?market']}", flush=True)

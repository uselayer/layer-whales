"""Score wallets live (as production does) with whichever uselayer is on PYTHONPATH, counting every Polymarket
read (data API + Gamma, what Signals' budget counts) per score by endpoint.
    PYTHONPATH=<build>/python/src uv run --frozen python bench_run.py wallets.json --out out.jsonl
"""
import argparse, json, time
from collections import Counter
from pathlib import Path
from uselayer import Client
from uselayer.venues import polymarket_data as pd

ap = argparse.ArgumentParser(); ap.add_argument("wallets"); ap.add_argument("--out", required=True)
args = ap.parse_args()
counts = Counter()
_get = pd.PolymarketData._get
def counted(self, url, params):
    path = url.split(".com", 1)[-1]
    counts[path + ("?market" if path == "/trades" and "market" in params else "")] += 1
    return _get(self, url, params)
pd.PolymarketData._get = counted
sc = Client(store=":memory:").whales.scorer()
with open(args.out, "a") as f:
    for w in json.loads(Path(args.wallets).read_text()):
        counts.clear(); t0 = time.time(); row = {"wallet": w["wallet"], "group": w["group"]}
        try:
            s = sc.score(w["wallet"], days=30, sample=60, on_leaderboard=bool(w.get("on_leaderboard")), name=w.get("name"))
            row |= {"segment": s.segment, "bets": s.bets, "capped": s.fills_from is not None, "spread": s.coverage.get("spread", 0)}
        except Exception as e:
            row["error"] = repr(e)
        row |= {"reads": dict(counts), "secs": round(time.time() - t0, 1)}
        f.write(json.dumps(row) + "\n"); f.flush()

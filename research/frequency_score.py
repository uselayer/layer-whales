"""Score each wallet in the pool three times with the unchanged SDK scorer (frequency-preregistered.md): the 30 and
90 days before T to select, and the 60 days after T to see what came next. Resumable: wallets already in --out are
skipped. Several hours; run it in the background.

    uv run python research/frequency_score.py research/frequency-candidates.json --out research/frequency-scores.jsonl
    uv run python research/frequency_score.py research/frequency-candidates.json --part 2 --out research/frequency-scores-2.jsonl
    gzip -9 research/frequency-scores*.jsonl
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
B_END = datetime(2026, 10, 13, tzinfo=UTC)
T2 = datetime(2026, 9, 13, tzinfo=UTC)  # Part 2: the 30 days before to select, the 30 after to see what came next

ap = argparse.ArgumentParser()
ap.add_argument("candidates")
ap.add_argument("--out", default=str(Path(__file__).parent / "frequency-scores.jsonl"))
ap.add_argument("--part", type=int, default=1)
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
    try:
        if args.part == 2:  # amendment 1: today's method where its 5-minute tests can run
            r["A30"] = sc.score(c["wallet"], days=30, end=T2, sample=60).to_dict(sample=True)
            r["B"] = sc.score(c["wallet"], days=30, end=B_END, sample=60).to_dict(sample=True)
        else:
            r["A30"] = sc.score(c["wallet"], days=30, end=T, sample=60).to_dict(sample=True)
            r["A90"] = sc.score(c["wallet"], days=90, end=T, sample=60).to_dict(sample=True)
            r["B"] = sc.score(c["wallet"], days=60, end=B_END, sample=60).to_dict(sample=True)
    except Exception as e:  # keep going; the row records it
        r["error"] = repr(e)
    return r


with ThreadPoolExecutor(6) as ex, out.open("a") as f:
    for i, r in enumerate(ex.map(one, pick)):
        if r is None:
            continue
        f.write(json.dumps(r, default=str) + "\n")
        f.flush()
        a30, a90, b = r.get("A30") or {}, r.get("A90") or {"segment": "-"}, r.get("B") or {}
        print(f"{i + 1}/{len(pick)} {time.time() - t0:5.0f}s {r['group'][:4]} A30={a30.get('segment', 'err'):8} "
              f"n={a30.get('bets')} A90={a90.get('segment', 'err'):8} n={a90.get('bets')} B={b.get('segment', 'err'):8} "
              f"n={b.get('bets')} {r.get('error', '')[:60]}", flush=True)  # fmt: skip

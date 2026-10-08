"""Run client.whales.discover() and keep every scored bet, to calibrate and report the Worth following rules.

    uv run python research/worth_following_run.py --wallets 160 --sample 120 --out research/discovery.json
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from uselayer import Client

ap = argparse.ArgumentParser()
ap.add_argument("--wallets", type=int, default=160)
ap.add_argument("--sample", type=int, default=120)
ap.add_argument("--days", type=float, default=30)
ap.add_argument("--out", default=str(Path(__file__).parent / "discovery.json"))
ap.add_argument("--cache", default=str(Path(__file__).parent / ".cache" / "prices"))
args = ap.parse_args()

client = Client(store=":memory:")
client.whales.scorer(cache_dir=args.cache)
t0 = time.time()


def progress(done: int, total: int, s) -> None:  # type: ignore[no-untyped-def]
    if s is not None:
        print(f"{done}/{total} {time.time() - t0:5.0f}s {s.segment:9} {s.name[:24]:24} bets={s.bets} cov={s.coverage}", flush=True)
    else:
        print(f"{done}/{total} failed", flush=True)


d = client.whales.discover(wallets=args.wallets, sample=args.sample, days=args.days, on_progress=progress)
Path(args.out).write_text(json.dumps(d.to_dict(sample=True)))
print(json.dumps({"counts": d.counts, "coverage": d.coverage, "sources": d.sources, "failed": d.failed}, indent=2))

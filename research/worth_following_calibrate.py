"""Which horizon makes "beats the price" a skill test? Split each wallet's checked bets in time: score the
earlier half, then see whether it predicts the later half. A horizon that's mostly noise won't.

    uv run python research/worth_following_calibrate.py research/discovery.json
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict

data = json.load(open(sys.argv[1]))
H = ("60", "300", "900", "3600")


def per_event(bets, f):
    by = defaultdict(list)
    for b in bets:
        v = f(b)
        if v is not None:
            by[b["event"]].append(v)
    return [statistics.mean(v) for v in by.values()]


def mz(xs):
    if len(xs) < 3:
        return None, None
    m = statistics.mean(xs)
    se = statistics.stdev(xs) / math.sqrt(len(xs)) or 1e-9
    return m, m / se


def edge(h):
    return lambda b: b["moved"].get(h)


def copy(h):
    def f(b):
        if h not in b["moved"] or "60" not in b["moved"] or b["fee"] is None:
            return None
        return b["moved"][h] - b["moved"]["60"] - b["fee"]
    return f


def corr(xs, ys):
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs)); sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy) if sx and sy else float("nan")


scores = [s for s in data["scores"] if s["segment"] != "no_view"]
print(f"wallets {len(scores)} (no_view left out: {len(data['scores']) - len(scores)})")
for name, fn in (("edge", edge), ("copy", copy)):
    for h in H:
        if name == "copy" and h == "60":
            continue
        a_vals, b_vals, picked_b, rest_b = [], [], [], []
        for s in scores:
            bets = sorted(s["sample"], key=lambda b: b["at"])
            if len(bets) < 16:
                continue
            half = len(bets) // 2
            ma, za = mz(per_event(bets[:half], fn(h)))
            later = per_event(bets[half:], fn(h))
            mb, _ = mz(later)
            if ma is None or mb is None:
                continue
            a_vals.append(ma); b_vals.append(mb)
            (picked_b if za >= 2 and ma > 0 else rest_b).extend(later)
        pm, pz = mz(picked_b) if picked_b else (None, None)
        rm, rz = mz(rest_b) if rest_b else (None, None)
        fmt = lambda m, z: "n/a" if m is None else f"{m*100:+.2f}¢ (z {z:+.1f})"
        print(f"{name:4} {h:>4}s  wallets {len(a_vals):3}  corr(early, late) {corr(a_vals, b_vals):+.2f}  "
              f"late bets of early-passers: {fmt(pm, pz)} n={len(picked_b)}  others: {fmt(rm, rz)} n={len(rest_b)}")

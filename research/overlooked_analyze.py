"""The analysis in overlooked-preregistered.md: select on window A, measure on window B.

    uv run python research/overlooked_analyze.py research/overlooked-scores.jsonl.gz
"""

from __future__ import annotations

import gzip
import json
import math
import statistics as st
import sys
from collections import Counter, defaultdict
from typing import Any

opener = gzip.open if sys.argv[1].endswith(".gz") else open
with opener(sys.argv[1], "rt") as fh:
    lines = [line for line in fh if line.strip()]
errors = sum(1 for line in lines if "error" in json.loads(line))
R = [r for r in map(json.loads, lines) if "A" in r and "B" in r]


def small(r: dict[str, Any]) -> bool:
    b = r["B"]
    return (b["volume"] or 0) < 1e6 and (b["pnl"] or 0) < 1e5


def pooled(rows: list[dict[str, Any]], key: str) -> tuple[float, float, int] | None:
    """Window B's per-event stat, pooled over wallets: mean, standard error, events."""
    g = [r["B"][key] for r in rows if r["B"][key] and r["B"][key]["n"] > 0 and r["B"][key]["margin"] > 0]
    n = sum(s["n"] for s in g)
    if not n:
        return None
    m = sum(s["mean"] * s["n"] for s in g) / n
    se = math.sqrt(sum(((s["margin"] / 1.96) * s["n"]) ** 2 for s in g)) / n
    return m, se, n


def settle(rows: list[dict[str, Any]]) -> tuple[float, float, int] | None:
    """A copier holding to settlement: payout - price 1-6 min after their buy - taker fee, per event."""
    ev: list[float] = []
    for r in rows:
        by: dict[str, list[float]] = defaultdict(list)
        for b in r["B"]["sample"]:
            m1 = b["moved"].get("60")
            if b["payout"] is not None and m1 is not None and b["fee"] is not None:
                by[b["event"]].append(b["payout"] - (b["price"] + m1) - b["fee"])
        ev += [st.mean(v) for v in by.values()]
    if len(ev) < 3:
        return None
    return st.mean(ev), st.stdev(ev) / math.sqrt(len(ev)), len(ev)


def fmt(p: tuple[float, float, int] | None) -> str:
    if p is None:
        return "—"
    m, se, n = p
    return f"{m * 100:+.2f}¢ ± {1.96 * se * 100:.2f}¢ (z {m / se:+.1f}, {n} events)"


RULES = {
    "R1 SDK followable (proven/quiet/rising)": lambda a: a["segment"] in ("proven", "quiet", "rising"),
    "R2 beats the price (edge z>=2)": lambda a: bool(a["edge"] and a["edge"]["mean"] > 0 and a["edge"]["z"] >= 2),
    "R3 copy-positive (copy z>=1)": lambda a: bool(a["copy"] and a["copy"]["mean"] > 0 and a["copy"]["z"] >= 1),
}
print(f"scored in both windows: {len(R)}  (errors: {errors})")
for grp in ("overlooked", "leaderboard"):
    G = [r for r in R if r["group"] == grp]
    print(f"\n=== {grp}: {len(G)} wallets, small accounts {sum(map(small, G))}")
    print("  segments A:", dict(Counter(r["A"]["segment"] for r in G)))
    print("  segments B:", dict(Counter(r["B"]["segment"] for r in G)))
    print(f"  baseline B copy (all):      {fmt(pooled(G, 'copy'))}")
    print(f"  baseline B hold-to-settle:  {fmt(settle(G))}")
    for name, f in RULES.items():
        S = [r for r in G if f(r["A"])]
        print(f"  {name:42} picks {len(S):3} | B copy {fmt(pooled(S, 'copy'))} | B settle {fmt(settle(S))} | B edge {fmt(pooled(S, 'edge'))}")
        if grp == "overlooked":
            Ss = [r for r in S if small(r)]
            print(f"  {'   … small accounts only':42} picks {len(Ss):3} | B copy {fmt(pooled(Ss, 'copy'))} | B settle {fmt(settle(Ss))}")


def corr(xs: tuple[float, ...], ys: tuple[float, ...]) -> float:
    mx, my, sx, sy = st.mean(xs), st.mean(ys), st.pstdev(xs), st.pstdev(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs) / (sx * sy) if sx and sy else float("nan")


for key in ("edge", "copy"):
    P = [(r["A"][key]["mean"], r["B"][key]["mean"]) for r in R
         if r["A"][key] and r["B"][key] and r["A"]["bets"] >= 15 and r["B"]["bets"] >= 15]  # fmt: skip
    xs, ys = zip(*P)
    print(f"\nA→B correlation of {key} across wallets (>=15 bets both windows): {corr(xs, ys):+.2f}  n={len(P)}")

print("\nEvery overlooked wallet picked by R1, window A then B:")
for r in R:
    a, b = r["A"], r["B"]
    if r["group"] != "overlooked" or not RULES["R1 SDK followable (proven/quiet/rising)"](a):
        continue

    def c(s: dict[str, Any]) -> str:
        return "—" if not s["copy"] else f"{s['copy']['mean'] * 100:+.2f}¢ z{s['copy']['z']:+.1f}"

    def e(s: dict[str, Any]) -> str:
        return "—" if not s["edge"] else f"{s['edge']['mean'] * 100:+.2f}¢ z{s['edge']['z']:+.1f}"

    top = Counter(x["category"] for x in a["sample"]).most_common(1)[0][0] if a["sample"] else "?"
    usd = st.median([x["usd"] for x in a["sample"]]) if a["sample"] else 0
    print(f"  {a['name'][:20]:20} {top:8} ${usd:7.0f}/trade  A {a['segment']:8} n={a['bets']:2} edge {e(a)} copy {c(a)}"
          f"  →  B {b['segment']:8} n={b['bets']:2} edge {e(b)} copy {c(b)}  small={small(r)}")  # fmt: skip

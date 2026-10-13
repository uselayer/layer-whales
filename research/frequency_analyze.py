"""The analysis in frequency-preregistered.md: select on A30 / A90, measure on B, three outcomes kept apart.

    uv run python research/frequency_analyze.py research/frequency-scores.jsonl.gz
"""

from __future__ import annotations

import gzip
import json
import math
import statistics as st
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from typing import Any

from uselayer.whale_scores import RECOMMENDED

opener = gzip.open if sys.argv[1].endswith(".gz") else open
with opener(sys.argv[1], "rt") as fh:
    lines = [json.loads(line) for line in fh if line.strip()]
errors = [r for r in lines if "error" in r]
R = [r for r in lines if all(k in r for k in ("A30", "A90", "B"))]
Row = dict[str, Any]
Pooled = tuple[float, float, int] | None


# ---- outcomes ----


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return 0.0, 0.0
    p, d = k / n, 1 + z * z / n
    c, h = (p + z * z / (2 * n)) / d, z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def two_prop_z(k1: int, n1: int, k2: int, n2: int) -> float:
    if not n1 or not n2:
        return float("nan")
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2)) or 1e-9
    return (k1 / n1 - k2 / n2) / se


def profit(rows: list[Row]) -> dict[str, Any]:
    """1. Profitability: B trading P&L per wallet."""
    pnl = [r["B"]["trading_pnl_window"] for r in rows if r["B"].get("trading_pnl_window") is not None]
    vol = [r["B"].get("volume_window") or 0 for r in rows if r["B"].get("trading_pnl_window") is not None]
    up = sum(1 for p in pnl if p > 0)
    return {"n": len(pnl), "up": up, "share": up / len(pnl) if pnl else None, "range": wilson(up, len(pnl)),
            "median": st.median(pnl) if pnl else None, "per_dollar": sum(pnl) / sum(vol) if sum(vol) else None,
            "trading": sum(1 for r in rows if (r["B"].get("decisions") or 0) > 0)}  # fmt: skip


def pooled(rows: list[Row], key: str) -> Pooled:
    """A B per-event stat (edge, copy), pooled over wallets: mean, standard error, events."""
    g = [r["B"][key] for r in rows if r["B"].get(key) and r["B"][key]["n"] > 0 and r["B"][key]["margin"] > 0]
    n = sum(s["n"] for s in g)
    if not n:
        return None
    m = sum(s["mean"] * s["n"] for s in g) / n
    se = math.sqrt(sum(((s["margin"] / 1.96) * s["n"]) ** 2 for s in g)) / n
    return m, se, n


def per_event(rows: list[Row], value: Callable[[Row], float | None]) -> Pooled:
    ev: list[float] = []
    for r in rows:
        by: dict[str, list[float]] = defaultdict(list)
        for b in r["B"].get("sample") or []:
            v = value(b)
            if v is not None:
                by[b["event"]].append(v)
        ev += [st.mean(v) for v in by.values()]
    if len(ev) < 3:
        return None
    return st.mean(ev), (st.stdev(ev) / math.sqrt(len(ev))) or 1e-9, len(ev)


def trader_settle(b: Row) -> float | None:
    """2b. The trader's own hold-to-settlement return per contract."""
    return None if b.get("payout") is None else b["payout"] - b["price"]


def copier_settle(b: Row) -> float | None:
    """3b. A copier 1 minute late, holding to settlement, after the taker fee."""
    m1 = (b.get("moved") or {}).get("60")
    if b.get("payout") is None or m1 is None or b.get("fee") is None:
        return None
    return b["payout"] - (b["price"] + m1) - b["fee"]


def fmt(p: Pooled) -> str:
    if p is None:
        return "—"
    m, se, n = p
    return f"{m * 100:+.2f}¢ ±{1.96 * se * 100:.2f} (z {m / se:+.1f}, {n} ev)"


def passes(p: Pooled) -> bool:
    return p is not None and p[0] > 0 and p[0] / p[1] >= 2


# ---- rules, on A only ----


def skill(a: Row) -> bool:
    return a.get("skill") in ("yes", "early")


RULES: dict[str, Callable[[Row], bool]] = {
    "all": lambda r: True,
    "R30-rec": lambda r: r["A30"]["segment"] in RECOMMENDED,
    "R30-skill": lambda r: skill(r["A30"]),
    "R90-rec": lambda r: r["A90"]["segment"] in (*RECOMMENDED, "inactive"),
    "R90-skill": lambda r: skill(r["A90"]),
    "R90-profit": lambda r: (r["A90"].get("trading_pnl_window") or 0) > 0,
}


def corr(pairs: list[tuple[float, float]]) -> float:
    if len(pairs) < 3:
        return float("nan")
    xs, ys = zip(*pairs)
    mx, my, sx, sy = st.mean(xs), st.mean(ys), st.pstdev(xs), st.pstdev(ys)
    return sum((x - mx) * (y - my) for x, y in pairs) / len(pairs) / (sx * sy) if sx and sy else float("nan")


print(f"wallets scored in all three windows: {len(R)}  (errors: {len(errors)})")
for grp in ("frequent", "selective", "sparse"):
    G = [r for r in R if r["group"] == grp]
    if not G:
        continue
    base = profit(G)
    print(f"\n=== {grp}: {len(G)} wallets")
    print("  A30 segments:", dict(Counter(r["A30"]["segment"] for r in G)))
    print("  A90 segments:", dict(Counter(r["A90"]["segment"] for r in G)))
    print("  B   segments:", dict(Counter(r["B"]["segment"] for r in G)))
    for name, rule in RULES.items():
        S = [r for r in G if rule(r)]
        if not S:
            print(f"  {name:10} picks   0")
            continue
        p = profit(S)
        z = two_prop_z(p["up"], p["n"], base["up"], base["n"]) if name != "all" else float("nan")
        prof_ok = name != "all" and z >= 2 and (p["median"] or 0) > 0
        edge, tset = pooled(S, "edge"), per_event(S, trader_settle)
        copy, cset = pooled(S, "copy"), per_event(S, copier_settle)
        lo, hi = p["range"]
        print(f"  {name:10} picks {len(S):3} | 1 PROFIT {p['share'] or 0:4.0%} up [{lo:.0%}–{hi:.0%}] vs all z {z:+.1f}, "
              f"median ${p['median'] or 0:,.0f}, {(p['per_dollar'] or 0) * 100:+.2f}¢/$, still trading {p['trading']}/{len(S)}"
              f"{'  PASS' if prof_ok else ''}")  # fmt: skip
        print(f"  {'':10}           | 2 SKILL  edge {fmt(edge)}{'  PASS' if passes(edge) else ''} | own settle {fmt(tset)}")
        print(f"  {'':10}           | 3 COPY   copy {fmt(copy)}{'  PASS' if passes(copy) else ''} | copier settle {fmt(cset)}")
    for win in ("A30", "A90"):
        pairs = [(r[win]["edge"]["mean"], r["B"]["edge"]["mean"]) for r in G
                 if r[win].get("edge") and r["B"].get("edge") and r[win]["bets"] >= 8 and r["B"]["bets"] >= 8]  # fmt: skip
        print(f"  persistence {win}→B edge across wallets (8+ bets both): {corr(pairs):+.2f}  n={len(pairs)}")
        pp = [(r[win]["trading_pnl_window"], r["B"]["trading_pnl_window"]) for r in G
              if r[win].get("trading_pnl_window") is not None and r["B"].get("trading_pnl_window") is not None]  # fmt: skip
        ranks = {i: k for k, i in enumerate(sorted(range(len(pp)), key=lambda i: pp[i][0]))}
        ranks_b = {i: k for k, i in enumerate(sorted(range(len(pp)), key=lambda i: pp[i][1]))}
        print(f"  persistence {win}→B trading P&L, rank correlation: {corr([(ranks[i], ranks_b[i]) for i in range(len(pp))]):+.2f}  n={len(pp)}")

print("\nSelective traders picked by R90-skill, A90 then B:")
for r in R:
    if r["group"] != "selective" or not skill(r["A90"]):
        continue
    a, b = r["A90"], r["B"]

    def e(s: Row) -> str:
        return "—" if not s.get("edge") else f"{s['edge']['mean'] * 100:+.2f}¢ z{s['edge']['z']:+.1f}"

    print(f"  {str(a.get('name'))[:20]:20} A90 {a['segment']:9} n={a['bets']:2} edge {e(a)} P&L ${a.get('trading_pnl_window') or 0:>10,.0f}"
          f"  →  B {b['segment']:9} n={b['bets']:2} edge {e(b)} P&L ${b.get('trading_pnl_window') or 0:>10,.0f}")  # fmt: skip

"""The analysis in selective-large-preregistered.md: select on A (90 days before T), measure on B (60 days after),
three outcomes kept apart, by source; then whether today's method overlooks promising ones.

    uv run python research/selective_large_analyze.py research/selective-large-scores.jsonl.gz
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

Row = dict[str, Any]
Pooled = tuple[float, float, int] | None
opener = gzip.open if sys.argv[1].endswith(".gz") else open
with opener(sys.argv[1], "rt") as fh:
    lines = [json.loads(line) for line in fh if line.strip()]
errors = [r for r in lines if "error" in r]
R = [r for r in lines if all(k in r for k in ("A", "B", "M_T2", "M_now"))]


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


def per_event(scores: list[Row], value: Callable[[Row], float | None]) -> Pooled:
    """A per-bet value, averaged per independent event within each wallet, pooled over wallets."""
    ev: list[float] = []
    for s in scores:
        by: dict[str, list[float]] = defaultdict(list)
        for b in s.get("sample") or []:
            v = value(b)
            if v is not None:
                by[b["event"]].append(v)
        ev += [st.mean(v) for v in by.values()]
    if len(ev) < 3:
        return None
    return st.mean(ev), (st.stdev(ev) / math.sqrt(len(ev))) or 1e-9, len(ev)


def own_settle(b: Row) -> float | None:
    return None if b.get("payout") is None else b["payout"] - b["price"]


def copier_settle(b: Row) -> float | None:
    m1 = (b.get("moved") or {}).get("60")
    if b.get("payout") is None or m1 is None or b.get("fee") is None:
        return None
    return b["payout"] - (b["price"] + m1) - b["fee"]


def edge5(b: Row) -> float | None:
    return (b.get("moved") or {}).get("300")


def copy1h(b: Row) -> float | None:
    return b.get("copy")


def ok(p: Pooled, z: float = 2.0, n: int = 0) -> bool:
    return p is not None and p[2] >= n and p[0] > 0 and p[0] / p[1] >= z


def fmt(p: Pooled) -> str:
    if p is None:
        return "—"
    m, se, n = p
    return f"{m * 100:+.2f}¢ ±{1.96 * se * 100:.2f} (z {m / se:+.1f}, {n} ev)"


def pnl(s: Row) -> float:
    return s.get("trading_pnl_window") or 0.0


RULES: dict[str, Callable[[Row], bool]] = {
    "all": lambda r: True,
    "R-profit": lambda r: pnl(r["A"]) > 0,
    "R-profit10k": lambda r: pnl(r["A"]) >= 10_000,
    "R-settle": lambda r: ok(per_event([r["A"]], own_settle), 2, 8),
    "R-settle1": lambda r: ok(per_event([r["A"]], own_settle), 1, 8),
    "R-both": lambda r: pnl(r["A"]) > 0 and ok(per_event([r["A"]], own_settle), 1, 8),
}


def promising(r: Row) -> bool:
    """In hindsight (B only): settlement return z >= 2 on 8+ events, or $10k+ trading profit at 5%+ of volume."""
    b = r["B"]
    vol = b.get("volume_window") or 0
    return ok(per_event([b], own_settle), 2, 8) or (pnl(b) >= 10_000 and vol > 0 and pnl(b) / vol >= 0.05)


def report(name: str, S: list[Row], base: list[Row]) -> None:
    if not S:
        print(f"  {name:12} picks   0")
        return
    p = [pnl(r["B"]) for r in S]
    up = sum(x > 0 for x in p)
    bu = sum(pnl(r["B"]) > 0 for r in base)
    z = two_prop_z(up, len(p), bu, len(base)) if S is not base else float("nan")
    lo, hi = wilson(up, len(p))
    vol = sum(r["B"].get("volume_window") or 0 for r in S)
    prof_pass = S is not base and z >= 2 and st.median(p) > 0
    sk, e5 = per_event([r["B"] for r in S], own_settle), per_event([r["B"] for r in S], edge5)
    cs, c1 = per_event([r["B"] for r in S], copier_settle), per_event([r["B"] for r in S], copy1h)
    print(f"  {name:12} n={len(S):3} | 1 PROFIT {up / len(p):4.0%} up [{lo:.0%}–{hi:.0%}] (vs all z {z:+.1f}), "
          f"median ${st.median(p):,.0f}, {(sum(p) / vol * 100 if vol else 0):+.2f}¢/$, "
          f"$1k+ {sum(x >= 1e3 for x in p)}/−$1k {sum(x <= -1e3 for x in p)}, $10k+ {sum(x >= 1e4 for x in p)}/−$10k "
          f"{sum(x <= -1e4 for x in p)}, total ${sum(p):,.0f}, still trading {sum((r['B'].get('decisions') or 0) > 0 for r in S)}"
          f"{'  PASS' if prof_pass else ''}")  # fmt: skip
    print(f"  {'':12}       | 2 SKILL  own settle {fmt(sk)}{'  PASS' if ok(sk) else ''} | 5-min edge {fmt(e5)}")
    print(f"  {'':12}       | 3 COPY   copier settle {fmt(cs)}{'  PASS' if ok(cs) else ''} | 1-h copy {fmt(c1)}")


def corr(pairs: list[tuple[float, float]]) -> float:
    if len(pairs) < 3:
        return float("nan")
    xs, ys = zip(*pairs)
    mx, my, sx, sy = st.mean(xs), st.mean(ys), st.pstdev(xs), st.pstdev(ys)
    return sum((x - mx) * (y - my) for x, y in pairs) / len(pairs) / (sx * sy) if sx and sy else float("nan")


def ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out, i = [0.0] * len(xs), 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2
        i = j + 1
    return out


def corr_ci(r: float, n: int) -> str:
    if n < 4 or math.isnan(r) or abs(r) >= 1:
        return f"{r:+.2f} (n={n})"
    z, se = math.atanh(r), 1 / math.sqrt(n - 3)
    return f"{r:+.2f} [{math.tanh(z - 1.96 * se):+.2f}, {math.tanh(z + 1.96 * se):+.2f}] (n={n})"


def persistence(G: list[Row]) -> None:
    sp = [(a[0], b[0]) for r in G if (a := per_event([r["A"]], own_settle)) and (b := per_event([r["B"]], own_settle))
          and a[2] >= 8 and b[2] >= 8]  # fmt: skip
    print(f"  persistence A→B own settle return, across wallets (8+ events both): {corr_ci(corr(sp), len(sp))}")
    pp = [(pnl(r["A"]), pnl(r["B"])) for r in G]
    rho = corr(list(zip(ranks([a for a, _ in pp]), ranks([b for _, b in pp])))) if len(pp) >= 3 else float("nan")
    print(f"  persistence A→B trading P&L, Spearman: {corr_ci(rho, len(pp))}")


SOURCES = {
    "market (no hindsight)": lambda r: r["group"] == "market" or r["found"].get("also_market"),
    "lb-volume": lambda r: r["group"] == "lb-volume",
    "lb-profit (leans to hindsight)": lambda r: r["group"] == "lb-profit",
    "everyone": lambda r: True,
}
print(f"wallets scored in all four windows: {len(R)}  (errors: {len(errors)})")
print("by source found:", dict(Counter(r["group"] for r in R)))
for src, inside in SOURCES.items():
    G = [r for r in R if inside(r)]
    if not G:
        continue
    print(f"\n=== {src}: {len(G)} wallets (median $ a market in A: ${st.median(r['found']['median_usd'] for r in G):,.0f})")
    for name, rule in RULES.items():
        S = [r for r in G if rule(r)]
        report(name, S if name != "all" else G, G)
    big = [r for r in G if r["found"]["median_usd"] >= 10_000]
    report("$10k+/market", big, G)
    persistence(G)

print("\n=== does today's method overlook promising ones?")
for src, inside in SOURCES.items():
    G = [r for r in R if inside(r)]
    P = [r for r in G if promising(r)]
    rec = [r for r in P if r["M_T2"]["segment"] in RECOMMENDED or r["M_now"]["segment"] in RECOMMENDED]
    rec_all = sum(r["M_T2"]["segment"] in RECOMMENDED or r["M_now"]["segment"] in RECOMMENDED for r in G)
    print(f"  {src:32} promising {len(P):3} of {len(G):3} | today's method recommended {len(rec)} of them "
          f"(and {rec_all} of all {len(G)}) | M_now bets: median {st.median([r['M_now']['bets'] for r in P]) if P else '—'}")
    for name, rule in list(RULES.items())[1:]:
        S = [r for r in G if rule(r)]
        hit = sum(promising(r) for r in S)
        print(f"     {name:12} picked {len(S):3}: {hit} promising (precision {hit / len(S) if S else 0:.0%}, "
              f"recall {hit / len(P) if P else 0:.0%}; everyone's rate {len(P) / len(G) if G else 0:.0%})")
print("\nToday's method's segments for promising wallets (13 Oct):",
      dict(Counter(r["M_now"]["segment"] for r in R if promising(r))))  # fmt: skip
